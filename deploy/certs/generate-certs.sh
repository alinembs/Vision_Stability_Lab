#!/usr/bin/env bash
# CA interna + certificado de servidor para a captura pelo celular (E0-T4).
#
# Porte de `arquo:infra/certs/generate-certs.sh` (branch
# `feature/SURVEY-110-m1-phone-ingest-spike`), adaptado ao Vision Stability Lab.
#
# **Por que CA própria (e não `mkcert`):** o onboarding do celular é "instale
# este arquivo", não "rode uma ferramenta nesta máquina e copie o resultado". A
# mesma CA assina o certificado do servidor que o MediaMTX (WHIP em 8889) e o
# nginx da página de captura (8444) montam em `/certs` (read-only).
#
# **Por que o IP deixou de ser fixo (2026-11-02):** o roteador de campo entregou
# outro endereço ao Jetson e o stack não subia (portas amarradas ao IP antigo).
# Agora o SAN é montado a partir dos IPs **atuais** do host e o script sabe
# reemitir só o certificado do servidor, mantendo a CA — um navegador que já
# confia na CA continua confiando depois da troca de IP, sem reinstalar nada.
# O nome estável (`vsl.local`, `<hostname>.local`) também entra no SAN para
# quem tiver mDNS na rede.
#
# **Nada do que este script gera entra no repositório**: `deploy/certs/` está no
# `.gitignore` da própria pasta. Chave privada de CA no Git é o incidente, não o
# risco. O único arquivo versionado ali é o `.gitignore`.
#
# Uso:
#   deploy/certs/generate-certs.sh                  # garante tudo (modo --ensure)
#   deploy/certs/generate-certs.sh --ensure         # idem, explícito
#   deploy/certs/generate-certs.sh --force          # regera a CA também
#   VSL_LAN_IP=192.168.60.1 deploy/certs/generate-certs.sh
#   VSL_CERT_DIR=/tmp/x deploy/certs/generate-certs.sh   # diretório alternativo
#
# Requisitos: `openssl` (a Orin tem). Não precisa de sudo.
set -euo pipefail

usage() {
  cat <<'EOF'
Uso: generate-certs.sh [--ensure | --force] [--help]

Garante a CA interna e o certificado de servidor (SAN com todos os IPv4 do
host + nomes locais) usados pelo MediaMTX (WHIP em 8889) e pelo nginx da
página de captura (8444).

Modelos:
  --ensure  (padrão) garante material válido para os IPs/nomes ATUAIS.
            Reaproveita a CA existente e só reemite o certificado do servidor
            quando o SAN mudou, quando ele expirou ou quando não foi assinado
            pela CA atual. Celulares que já confiam na CA continuam confiando.
  --force   regera TUDO, inclusive a CA. Isso invalida a CA já instalada nos
            navegadores/celulares: eles passam a recusar o servidor até
            receberem o novo `ca.crt`.
  --help    mostra esta ajuda.

Variáveis de ambiente (todas opcionais):
  VSL_CERT_DIR          diretório de saída (default: a pasta deste script)
  VSL_LAN_IP / LAN_IP   IPv4 extra a forçar no SAN ("auto"/vazio = detectar).
                        Os IPs das demais interfaces do host sempre entram.
  VSL_LOCAL_DOMAIN      domínio local no SAN (default: vsl.local)
  VSL_SERVER_CERT_DAYS  validade do certificado de servidor (default: 825)
  VSL_CA_CERT_DAYS      validade da CA (default: 3650)

Códigos de saída (usados pelo Makefile):
  0   nada mudou — o material já cobre o host
  10  certificado do servidor reemitido (CA mantida)
  11  CA e certificado regerados (reinstale o `ca.crt` nos clientes)
  2   erro de uso/validação (nada foi alterado)

Arquivos gerados (nenhum versionado):
  ca.crt      instalar no celular (Android: Configurações → Segurança → CA)
  ca.key      chave da CA — NUNCA sai desta máquina
  server.crt  certificado do servidor (montado em /certs nos containers)
  server.key  chave do servidor — NUNCA versionar
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${VSL_CERT_DIR:-$SCRIPT_DIR}"
LOCAL_DOMAIN="${VSL_LOCAL_DOMAIN:-vsl.local}"
# 825 dias: limite de validade de certificado de servidor aceito pelas
# plataformas móveis (regra da Apple desde 2020; o Android segue o mesmo teto
# prático). A CA pode ser de 10 anos porque é ela que fica instalada no device.
SERVER_DAYS="${VSL_SERVER_CERT_DAYS:-825}"
CA_DAYS="${VSL_CA_CERT_DAYS:-3650}"

MODE="ensure"
for arg in "$@"; do
  case "$arg" in
    --ensure) MODE="ensure" ;;
    --force) MODE="force" ;;
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
# diretório meio escrito. Valida antes de tocar em qualquer arquivo.
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

if ! is_domain "$LOCAL_DOMAIN"; then
  cat >&2 <<EOF
VSL_LOCAL_DOMAIN inválido: '$LOCAL_DOMAIN'
Esperado um nome de domínio sem esquema nem caminho (ex.: vsl.local).
Nada foi gerado — nenhum arquivo em $OUT_DIR foi tocado.
EOF
  exit 2
fi

IP_LIST=""
add_ip() {
  local ip="$1"
  is_ipv4 "$ip" || return 0
  case "$ip" in
    0.0.0.0 | 169.254.*) return 0 ;;
  esac
  IP_LIST="${IP_LIST}${ip}"$'\n'
}

# Todo IPv4 utilizável do host entra no SAN: o IP da LAN pode mudar (foi o que
# aconteceu em campo) e, no Jetson, coexistem Ethernet, Wi-Fi, USB e bridges.
EXPLICIT_IP="${VSL_LAN_IP:-${LAN_IP:-}}"
if [ -n "$EXPLICIT_IP" ] && [ "$EXPLICIT_IP" != "auto" ]; then
  if ! is_ipv4 "$EXPLICIT_IP"; then
    cat >&2 <<EOF
VSL_LAN_IP inválido: '$EXPLICIT_IP'
Esperado um IPv4 com cada octeto entre 0 e 255 (ex.: 192.168.59.102).
Nada foi gerado — nenhum arquivo em $OUT_DIR foi tocado.
EOF
    exit 2
  fi
  add_ip "$EXPLICIT_IP"
fi

if command -v ip >/dev/null 2>&1; then
  add_ip "$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{ for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit } }' ||
    true)"
  # Demais interfaces de verdade (eth0/wlan0/usb0/tailscale0...). As bridges do
  # Docker ficam de fora: `172.17-172.3x` nasce e morre com `docker compose`, e
  # incluí-las no SAN faria o certificado ser reemitido a cada `make up` sem
  # nenhum ganho — ninguém acessa o painel pelo IP da bridge.
  while read -r iface ip; do
    case "$iface" in
      lo | docker* | br-* | veth* | virbr* | tun* | tap*) continue ;;
    esac
    add_ip "$ip"
  done < <(ip -4 -o addr show 2>/dev/null |
    awk '{ split($4, a, "/"); print $2, a[1] }' || true)
elif command -v hostname >/dev/null 2>&1; then
  for candidato in $(hostname -I 2>/dev/null || true); do
    add_ip "$candidato"
  done
fi
# Loopback: `localhost` já é contexto seguro para `getUserMedia` no próprio host.
add_ip "127.0.0.1"

IP_SAN="$(printf '%s' "$IP_LIST" | sort -u | sed 's/^/IP:/')"
if [ -z "$IP_SAN" ]; then
  echo "Nenhum IPv4 encontrado no host — defina VSL_LAN_IP." >&2
  exit 2
fi

# Nome estável: sobrevive à troca de IP do DHCP e é o que o operador digita no
# navegador quando a rede tem mDNS (avahi no Jetson, Bonjour no macOS/Windows,
# Chrome/Safari no iOS).
NAME_LIST="DNS:$LOCAL_DOMAIN"$'\n'"DNS:localhost"$'\n'
HOSTNAME_VALUE="$(hostname 2>/dev/null || true)"
if is_domain "$HOSTNAME_VALUE"; then
  NAME_LIST="${NAME_LIST}DNS:${HOSTNAME_VALUE}"$'\n'
  case "$HOSTNAME_VALUE" in
    *.local) ;;
    *) NAME_LIST="${NAME_LIST}DNS:${HOSTNAME_VALUE}.local"$'\n' ;;
  esac
fi
NAME_SAN="$(printf '%s' "$NAME_LIST" | sort -u)"

DESIRED_SAN="$(printf '%s\n%s\n' "$NAME_SAN" "$IP_SAN" | sort -u)"
SAN_EXT="$(printf '%s\n' "$DESIRED_SAN" | paste -sd, -)"

command -v openssl >/dev/null 2>&1 || {
  echo "openssl não encontrado no PATH — instale o pacote openssl." >&2
  exit 2
}

CA_KEY="$OUT_DIR/ca.key"
CA_CERT="$OUT_DIR/ca.crt"
SRV_KEY="$OUT_DIR/server.key"
SRV_CERT="$OUT_DIR/server.crt"

# SAN que o certificado atual realmente tem, normalizado para o mesmo formato
# de `DESIRED_SAN` (o openssl imprime "IP Address:", nós usamos "IP:").
current_san() {
  [ -s "$SRV_CERT" ] || return 1
  openssl x509 -in "$SRV_CERT" -noout -ext subjectAltName 2>/dev/null |
    tail -n +2 |
    tr ',' '\n' |
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
      -e 's/^IP Address:/IP:/' |
    grep -v '^$' |
    sort -u
}

ca_ok() {
  [ -s "$CA_KEY" ] && [ -s "$CA_CERT" ] || return 1
  openssl x509 -in "$CA_CERT" -noout -checkend 0 >/dev/null 2>&1 || return 1
  [ "$(openssl x509 -in "$CA_CERT" -noout -modulus 2>/dev/null)" = \
    "$(openssl rsa -in "$CA_KEY" -noout -modulus 2>/dev/null)" ]
}

server_ok() {
  [ -s "$SRV_KEY" ] && [ -s "$SRV_CERT" ] || return 1
  openssl x509 -in "$SRV_CERT" -noout -checkend 0 >/dev/null 2>&1 || return 1
  [ "$(openssl x509 -in "$SRV_CERT" -noout -modulus 2>/dev/null)" = \
    "$(openssl rsa -in "$SRV_KEY" -noout -modulus 2>/dev/null)" ] || return 1
  openssl verify -CAfile "$CA_CERT" "$SRV_CERT" >/dev/null 2>&1 || return 1
  diff -q <(current_san) <(printf '%s\n' "$DESIRED_SAN") >/dev/null 2>&1
}

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

CA_READY=1
if [ "$MODE" = "force" ]; then
  CA_READY=0
elif ! ca_ok; then
  if [ -e "$CA_KEY" ] || [ -e "$CA_CERT" ]; then
    echo "[aviso] CA existente inválida/expirada/desparelhada — gerando nova" >&2
    echo "        (os clientes precisarão reinstalar deploy/certs/ca.crt)" >&2
  fi
  CA_READY=0
fi

if [ "$CA_READY" = "1" ] && server_ok; then
  echo "certificado já cobre o host atual (nada foi alterado):"
  echo "  SAN: $(printf '%s\n' "$DESIRED_SAN" | paste -sd, -)"
  exit 0
fi

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
GEN_SERIAL="$WORK_DIR/ca.srl"

# Chave privada nasce sem permissão para grupo/outros; os `chmod` do fim
# ajustam o que pode ser lido pelo container (que roda como root).
umask 077

SIGN_KEY="$CA_KEY"
SIGN_CERT="$CA_CERT"
CA_WAS_GENERATED=0

if [ "$CA_READY" = "0" ]; then
  # 1. CA interna (a mesma distribuída no onboarding dos celulares).
  openssl genrsa -out "$GEN_CA_KEY" 4096 >/dev/null 2>&1
  openssl req -x509 -new -nodes -key "$GEN_CA_KEY" -sha256 -days "$CA_DAYS" \
    -subj "/CN=Vision Stability Lab Internal CA/O=PR3VIA/OU=Edge Capture" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" \
    -out "$GEN_CA_CERT"
  SIGN_KEY="$GEN_CA_KEY"
  SIGN_CERT="$GEN_CA_CERT"
  CA_WAS_GENERATED=1
fi

# 2. Chave e CSR do servidor.
openssl genrsa -out "$GEN_SRV_KEY" 2048 >/dev/null 2>&1
openssl req -new -key "$GEN_SRV_KEY" \
  -subj "/CN=$LOCAL_DOMAIN/O=PR3VIA/OU=Edge Capture" \
  -out "$GEN_SRV_CSR"

# 3. SAN: é o que faz `https://<IP>` ser confiável sem DNS, e o que faz o
#    `curl --cacert` da validação passar sem `-k`. IPs e domínio chegam aqui já
#    validados (is_ipv4/is_domain) — é o que impede injeção de entrada extra na
#    extensão.
cat >"$GEN_EXT_FILE" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN_EXT
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

# `-CAserial` em WORK_DIR: não deixa `ca.srl` sobrando no diretório montado.
openssl x509 -req -in "$GEN_SRV_CSR" -CA "$SIGN_CERT" -CAkey "$SIGN_KEY" \
  -CAserial "$GEN_SERIAL" -CAcreateserial \
  -days "$SERVER_DAYS" -sha256 -extfile "$GEN_EXT_FILE" -out "$GEN_SRV_CERT"

rm -f "$GEN_SRV_CSR" "$GEN_EXT_FILE" "$GEN_SERIAL"

# 600 nas chaves: só o dono lê. 644 nos certificados: são públicos por natureza
# (o `ca.crt` vai para o celular) e o container precisa lê-los.
if [ "$CA_WAS_GENERATED" = "1" ]; then
  chmod 600 "$GEN_CA_KEY"
  chmod 644 "$GEN_CA_CERT"
fi
chmod 600 "$GEN_SRV_KEY"
chmod 644 "$GEN_SRV_CERT"

# Publicação: só agora o diretório de saída recebe material, e só o conjunto
# completo (mesmo filesystem -> rename atômico por arquivo). A CA só é
# substituída quando foi gerada agora — em `--ensure` ela é preservada.
if [ "$CA_WAS_GENERATED" = "1" ]; then
  mv -f "$GEN_CA_KEY" "$CA_KEY"
  mv -f "$GEN_CA_CERT" "$CA_CERT"
fi
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

if [ "$CA_WAS_GENERATED" = "1" ]; then
  echo
  echo "CA regerada: reinstale deploy/certs/ca.crt em todo navegador/celular." >&2
  exit 11
fi

echo
echo "Certificado do servidor reemitido; a CA não mudou — nada a reinstalar."
exit 10
