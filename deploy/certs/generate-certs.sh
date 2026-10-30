#!/usr/bin/env bash
# CA interna + certificado do servidor para a captura pelo celular (E0-T4).
#
# Porte de `arquo:infra/certs/generate-certs.sh` (branch
# `feature/SURVEY-110-m1-phone-ingest-spike`), adaptado ao Vision Stability Lab.
#
# Por que CA própria (e não `mkcert`): o onboarding do celular é "instale este
# arquivo", não "rode uma ferramenta nesta máquina e copie o resultado". A CA é a
# mesma para todos os devices; o certificado do servidor é um só — o MediaMTX
# (WHIP em 8889) e o nginx da página de captura (8443) montam o **mesmo**
# material em `/certs` (read-only).
#
# **Nada do que este script gera entra no repositório**: `deploy/certs/` está no
# `.gitignore` da própria pasta. Chave privada de CA no Git é o incidente, não o
# risco. O único arquivo versionado ali é o `.gitignore`.
#
# O SAN cobre os três jeitos de o celular chegar no servidor:
#   - `IP:<LAN>`        — laboratório sem DNS (o caminho provado em 2026-10-27);
#   - `DNS:vsl.local`   — campo, com DNS local apontando para o Jetson;
#   - `localhost`/`127.0.0.1` — o próprio host, onde `localhost` já é contexto
#     seguro para `getUserMedia`.
#
# Uso:
#   deploy/certs/generate-certs.sh                  # gera em deploy/certs/
#   VSL_LAN_IP=192.168.60.1 deploy/certs/generate-certs.sh --force
#   VSL_CERT_DIR=/tmp/x deploy/certs/generate-certs.sh   # diretório alternativo
#
# Requisitos: `openssl` (a Orin tem). Não precisa de sudo.
set -euo pipefail

usage() {
  cat <<'EOF'
Uso: generate-certs.sh [--force] [--help]

Gera a CA interna e o certificado de servidor (SAN de IP e de DNS) usados pelo
MediaMTX (WHIP em 8889) e pelo nginx da página de captura (8443).

Opções:
  --force   regera tudo, sobrescrevendo o material existente. Isso invalida a
            CA já instalada nos celulares: eles passam a recusar o servidor
            até receberem o novo `ca.crt`.
  --help    mostra esta ajuda.

Variáveis de ambiente (todas opcionais):
  VSL_CERT_DIR          diretório de saída (default: a pasta deste script)
  VSL_LAN_IP            IP da LAN no SAN (default: 192.168.59.102)
  VSL_LOCAL_DOMAIN      domínio local no SAN (default: vsl.local)
  VSL_SERVER_CERT_DAYS  validade do certificado de servidor (default: 825)
  VSL_CA_CERT_DAYS      validade da CA (default: 3650)

Arquivos gerados (nenhum versionado):
  ca.crt      instalar no celular (Android: Configurações → Segurança → CA)
  ca.key      chave da CA — NUNCA sai desta máquina
  server.crt  certificado do servidor (montado em /certs nos containers)
  server.key  chave do servidor — NUNCA versionar
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${VSL_CERT_DIR:-$SCRIPT_DIR}"
# IP da LAN onde o MediaMTX publica o WHIP e o nginx serve a página. É o IP do
# host no laboratório (enxd0c857009d17); em campo, sobrescreva no deployment.
LAN_IP="${VSL_LAN_IP:-192.168.59.102}"
LOCAL_DOMAIN="${VSL_LOCAL_DOMAIN:-vsl.local}"
# 825 dias: limite de validade de certificado de servidor aceito pelas
# plataformas móveis (regra da Apple desde 2020; o Android segue o mesmo teto
# prático). A CA pode ser de 10 anos porque é ela que fica instalada no device.
SERVER_DAYS="${VSL_SERVER_CERT_DAYS:-825}"
CA_DAYS="${VSL_CA_CERT_DAYS:-3650}"

FORCE=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE="1" ;;
    -h | --help) usage; exit 0 ;;
    *)
      echo "argumento desconhecido: $arg (use --help)" >&2
      exit 2
      ;;
  esac
done

# O SAN é montado por interpolação dentro do `server.ext`: valor não validado
# injeta entrada na extensão (`VSL_LAN_IP="1.2.3.4,DNS:evil.example"`) e valor
# sem sentido (`VSL_LAN_IP="nao-e-um-ip"`) só falha no meio da geração, com o
# diretório meio escrito — e `--force` como única saída, o que invalida a CA
# já instalada nos celulares. Valida antes de tocar em qualquer arquivo.
is_ipv4() {
  local ip="$1" octeto
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  local IFS=.
  for octeto in $ip; do
    # `10#` força base decimal: `08` não é octal inválido aqui.
    [ "$((10#$octeto))" -le 255 ] || return 1
  done
  return 0
}

# Domínio entra no SAN do mesmo jeito (`DNS:$LOCAL_DOMAIN`): só letras, dígitos,
# ponto e hífen. Sem espaço, vírgula, dois-pontos ou `$`.
is_domain() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]
}

if ! is_ipv4 "$LAN_IP"; then
  cat >&2 <<EOF
VSL_LAN_IP inválido: '$LAN_IP'
Esperado um IPv4 com cada octeto entre 0 e 255 (ex.: 192.168.59.102).
Nada foi gerado — nenhum arquivo em $OUT_DIR foi tocado.
EOF
  exit 2
fi

if ! is_domain "$LOCAL_DOMAIN"; then
  cat >&2 <<EOF
VSL_LOCAL_DOMAIN inválido: '$LOCAL_DOMAIN'
Esperado um nome de domínio sem esquema nem caminho (ex.: vsl.local).
Nada foi gerado — nenhum arquivo em $OUT_DIR foi tocado.
EOF
  exit 2
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl não encontrado no PATH — instale o pacote openssl." >&2
  exit 2
}

CA_KEY="$OUT_DIR/ca.key"
CA_CERT="$OUT_DIR/ca.crt"
SRV_KEY="$OUT_DIR/server.key"
SRV_CERT="$OUT_DIR/server.crt"

if [ -z "$FORCE" ]; then
  for existing in "$CA_KEY" "$CA_CERT" "$SRV_KEY" "$SRV_CERT"; do
    if [ -e "$existing" ]; then
      echo "já existe: $existing" >&2
      echo "nada foi alterado. Use --force para regerar (isso invalida a CA já instalada nos celulares)." >&2
      exit 1
    fi
  done
fi

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

# Geração atômica: tudo nasce num diretório temporário **dentro** de $OUT_DIR
# (mesmo filesystem, então a publicação é `rename`) e só o conjunto completo é
# movido no fim. Falha no meio (openssl ruim, disco cheio) não deixa
# `ca.key`/`ca.crt`/`server.csr` órfãos — que fariam o próximo run parar em
# "já existe" com material inutilizável e sem saída além de `--force`.
WORK_DIR="$(mktemp -d "$OUT_DIR/.tmp-certs.XXXXXX")"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT INT TERM

GEN_CA_KEY="$WORK_DIR/ca.key"
GEN_CA_CERT="$WORK_DIR/ca.crt"
GEN_SRV_KEY="$WORK_DIR/server.key"
GEN_SRV_CSR="$WORK_DIR/server.csr"
GEN_SRV_CERT="$WORK_DIR/server.crt"
GEN_EXT_FILE="$WORK_DIR/server.ext"

# Chave privada nasce sem permissão para grupo/outros; os `chmod` do fim
# ajustam o que pode ser lido pelo container (que roda como root).
umask 077

# 1. CA interna (a mesma distribuída no onboarding dos celulares).
openssl genrsa -out "$GEN_CA_KEY" 4096 >/dev/null 2>&1
openssl req -x509 -new -nodes -key "$GEN_CA_KEY" -sha256 -days "$CA_DAYS" \
  -subj "/CN=Vision Stability Lab Internal CA/O=PR3VIA/OU=Edge Capture" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -out "$GEN_CA_CERT"

# 2. Chave e CSR do servidor.
openssl genrsa -out "$GEN_SRV_KEY" 2048 >/dev/null 2>&1
openssl req -new -key "$GEN_SRV_KEY" -subj "/CN=$LOCAL_DOMAIN/O=PR3VIA/OU=Edge Capture" \
  -out "$GEN_SRV_CSR"

# 3. SAN: é o que faz `https://<IP>` ser confiável sem DNS, e o que faz o
#    `curl --cacert` da validação passar sem `-k`. `LAN_IP`/`LOCAL_DOMAIN`
#    chegam aqui já validados (is_ipv4/is_domain) — é o que impede injeção de
#    entrada extra na extensão.
cat >"$GEN_EXT_FILE" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$LOCAL_DOMAIN,DNS:localhost,IP:$LAN_IP,IP:127.0.0.1
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

openssl x509 -req -in "$GEN_SRV_CSR" -CA "$GEN_CA_CERT" -CAkey "$GEN_CA_KEY" -CAcreateserial \
  -days "$SERVER_DAYS" -sha256 -extfile "$GEN_EXT_FILE" -out "$GEN_SRV_CERT"

rm -f "$GEN_SRV_CSR" "$GEN_EXT_FILE"

# 600 nas chaves: só o dono lê. 644 nos certificados: são públicos por natureza
# (o `ca.crt` vai para o celular) e o container precisa lê-los.
chmod 600 "$GEN_CA_KEY" "$GEN_SRV_KEY"
chmod 644 "$GEN_CA_CERT" "$GEN_SRV_CERT"

# Publicação: só agora o diretório de saída recebe material, e só o conjunto
# completo (mesmo filesystem -> rename atômico por arquivo).
mv -f "$GEN_CA_KEY" "$CA_KEY"
mv -f "$GEN_CA_CERT" "$CA_CERT"
mv -f "$GEN_SRV_KEY" "$SRV_KEY"
mv -f "$GEN_SRV_CERT" "$SRV_CERT"

chmod 700 "$OUT_DIR"

echo "CA:          $CA_CERT  (instalar no celular — é este arquivo)"
echo "Certificado: $SRV_CERT"
echo "Chave:       $SRV_KEY   (NUNCA versionar; /certs/server.key nos containers)"
echo
echo "SAN:"
openssl x509 -in "$SRV_CERT" -noout -ext subjectAltName | sed 's/^/  /'
echo
echo "Impressão digital da CA (SHA-256), para conferir na tela do celular:"
openssl x509 -in "$CA_CERT" -noout -fingerprint -sha256 | sed 's/^/  /'
