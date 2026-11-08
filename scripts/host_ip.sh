#!/usr/bin/env bash
# Descobre o IPv4 da LAN deste host — sem IP fixo no repositório.
#
# Por que existe: em campo o roteador pode entregar outro endereço ao Jetson
# (aconteceu em 2026-11-02: o Orin Nano saiu de 192.168.59.129 para
# 192.168.0.2 e o `docker compose` falhou com
# `failed to bind host port 192.168.59.129:1935/tcp: cannot assign requested
# address`, porque as portas do MediaMTX estavam amarradas ao IP antigo). O
# Makefile usa este script para exportar `LAN_IP` **a cada** `make up`.
#
# Ordem de precedência:
#   1. `VSL_LAN_IP` / `LAN_IP` no ambiente (valor explícito vence; `auto` = ignora);
#   2. o `src` da rota default (`ip -4 route get 1.1.1.1`) — é o IP que o host
#      usa para sair para a LAN;
#   3. o primeiro IPv4 de `hostname -I` que não seja loopback/link-local.
#
# Uso:
#   scripts/host_ip.sh          # imprime o IPv4 (uma linha)
#   LAN_IP=10.0.0.7 scripts/host_ip.sh
#
# Nunca falha com exit != 0: se nada for encontrado, avisa no stderr e imprime
# `127.0.0.1` (publicação em loopback) para que `make down`/`make status`
# continuem funcionando. `make up` é bloqueado antes pelo `make doctor`, que
# exige um IP de LAN real.
set -euo pipefail

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

emit() {
  echo "$1"
  exit 0
}

# 1. Override explícito. Valor inválido é erro do operador, mas avisamos e
#    seguimos para a descoberta em vez de derrubar `make down`.
for candidate in "${VSL_LAN_IP:-}" "${LAN_IP:-}"; do
  [ -n "$candidate" ] || continue
  [ "$candidate" = "auto" ] && continue
  if is_ipv4 "$candidate"; then
    emit "$candidate"
  fi
  echo "host_ip.sh: ignorando '$candidate' (não é um IPv4 válido)" >&2
done

# 2. Rota default: o endereço da interface que fala com o resto da rede.
if command -v ip >/dev/null 2>&1; then
  candidate="$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{ for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit } }' ||
    true)"
  if is_ipv4 "$candidate"; then
    emit "$candidate"
  fi
fi

# 3. Qualquer interface com IPv4 utilizável (a ordem do `hostname -I` no Jetson
#    costuma ser eth0/wlan0, depois o USB (192.168.55.1) e as bridges do Docker).
if command -v hostname >/dev/null 2>&1; then
  for candidate in $(hostname -I 2>/dev/null || true); do
    case "$candidate" in
      127.* | 169.254.* | 0.0.0.0) continue ;;
    esac
    if is_ipv4 "$candidate"; then
      emit "$candidate"
    fi
  done
fi

echo "host_ip.sh: nenhum IPv4 de LAN encontrado — defina LAN_IP=<ip>" >&2
emit "127.0.0.1"
