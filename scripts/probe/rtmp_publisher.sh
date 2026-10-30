#!/usr/bin/env bash
# Publisher RTMP sintético — simula o push de vídeo do drone para o MediaMTX.
#
# O drone real publica H.264 por RTMP no path `drone`; este script reproduz
# exatamente esse contrato de ingestão sem depender do equipamento. É a
# contenção de bancada da Etapa 0 (E0-T3) e será reusado como publisher de
# referência nos testes de troca de fonte.
#
# Pipeline (só plugins L4T/Jetson, roda no host — não precisa de container):
#   videotestsrc ! video/x-raw,... ! nvvidconv ! video/x-raw(memory:NVMM)
#     ! nvv4l2h264enc ! h264parse ! flvmux streamable=true ! rtmpsink
#
# Exemplos:
#   ./scripts/probe/rtmp_publisher.sh                        # drone, infinito
#   ./scripts/probe/rtmp_publisher.sh --seconds 60           # 1 minuto
#   ./scripts/probe/rtmp_publisher.sh --path phone --fps 25  # outro path
#
# Roda em foreground de propósito: quem controla duração é o `timeout`/`&` do
# chamador.
set -euo pipefail

PATH_NAME="drone"
RUN_SECONDS=0
BITRATE=2500000
WIDTH=1280
HEIGHT=720
FPS=30
# Atenção: as portas do MediaMTX são publicadas no IP da LAN (deploy/
# docker-compose.yml, E0-T2), não em 0.0.0.0 — 127.0.0.1 é recusado aqui.
HOST="192.168.59.102"
PORT=1935
PATTERN="smpte"
# Padrões válidos do videotestsrc (`gst-inspect-1.0 videotestsrc`, enum
# GstVideoTestSrcPattern). Lista versionada de propósito: `--pattern` errado só
# falharia no meio do pipeline, com mensagem genérica do gst-launch.
KNOWN_PATTERNS="smpte snow black white red green blue checkers-1 checkers-2 checkers-4 checkers-8 circular blink smpte75 zone-plate gamut chroma-zone-plate solid-color ball smpte100 bar pinwheel spokes gradient colors smpte-rp-219"

usage() {
    cat <<'EOF'
Publisher RTMP sintético para o MediaMTX do Vision Stability Lab.

Uso:
  rtmp_publisher.sh [opções]

Opções:
  --path <nome>       path de publicação no MediaMTX (default: drone)
  --seconds <n>       duração em segundos; 0 = infinito (default: 0)
  --bitrate <bps>     bitrate do encoder H.264 (default: 2500000)
  --width <px>        largura do vídeo (default: 1280)
  --height <px>       altura do vídeo (default: 720)
  --fps <n>           quadros por segundo (default: 30)
  --host <ip>         host do MediaMTX (default: 192.168.59.102)
                      use 127.0.0.1 só se o MediaMTX estiver publicado em loopback
  --port <n>          porta RTMP do MediaMTX (default: 1935)
  --pattern <nome>    padrão do videotestsrc (default: smpte).
                      válidos: smpte, snow, black, white, red, green, blue,
                      checkers-1..8, circular, blink, smpte75, zone-plate,
                      gamut, chroma-zone-plate, solid-color, ball, smpte100,
                      bar, pinwheel, spokes, gradient, colors, smpte-rp-219
  -h, --help          esta ajuda

Publica em rtmp://<host>:<porta>/<path>. Roda em foreground; use `timeout` ou
`&` para controlar o tempo de vida.
EOF
}

die() {
    echo "erro: $*" >&2
    exit 2
}

need_value() {
    # $1 = flag, $2 = quantidade de argumentos restantes
    if [ "$2" -lt 2 ]; then
        die "a flag $1 exige um valor"
    fi
}

# `--pattern` vai direto para o videotestsrc: validar aqui evita que o erro
# apareça só quando o gst-launch já montou metade do pipeline.
known_pattern() {
    local candidate="$1" known
    for known in $KNOWN_PATTERNS; do
        if [ "$candidate" = "$known" ]; then
            return 0
        fi
    done
    return 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --path)      need_value "$1" "$#"; PATH_NAME="$2"; shift 2 ;;
        --seconds)   need_value "$1" "$#"; RUN_SECONDS="$2"; shift 2 ;;
        --bitrate)   need_value "$1" "$#"; BITRATE="$2"; shift 2 ;;
        --width)     need_value "$1" "$#"; WIDTH="$2"; shift 2 ;;
        --height)    need_value "$1" "$#"; HEIGHT="$2"; shift 2 ;;
        --fps)       need_value "$1" "$#"; FPS="$2"; shift 2 ;;
        --host)      need_value "$1" "$#"; HOST="$2"; shift 2 ;;
        --port)      need_value "$1" "$#"; PORT="$2"; shift 2 ;;
        --pattern)   need_value "$1" "$#"; PATTERN="$2"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        *)           die "argumento desconhecido: $1 (use --help)" ;;
    esac
done

# Validação: inteiros positivos onde faz sentido (evita erro obscuro no gst).
for pair in "seconds:$RUN_SECONDS" "bitrate:$BITRATE" "width:$WIDTH" \
            "height:$HEIGHT" "fps:$FPS" "port:$PORT"; do
    name="${pair%%:*}"
    value="${pair#*:}"
    case "$value" in
        ''|*[!0-9]*) die "--$name precisa ser inteiro não-negativo (recebido: '$value')" ;;
    esac
done
[ "$BITRATE" -gt 0 ] || die "--bitrate precisa ser maior que zero"
[ "$WIDTH" -gt 0 ] || die "--width precisa ser maior que zero"
[ "$HEIGHT" -gt 0 ] || die "--height precisa ser maior que zero"
[ "$FPS" -gt 0 ] || die "--fps precisa ser maior que zero"
[ "$PORT" -gt 0 ] || die "--port precisa ser maior que zero"
[ -n "$PATH_NAME" ] || die "--path não pode ser vazio"
if ! known_pattern "$PATTERN"; then
    die "--pattern desconhecido: '$PATTERN' (use --help; válidos: $KNOWN_PATTERNS)"
fi

command -v gst-launch-1.0 >/dev/null 2>&1 \
    || die "gst-launch-1.0 não encontrado no PATH"
command -v gst-inspect-1.0 >/dev/null 2>&1 \
    || die "gst-inspect-1.0 não encontrado no PATH"

# Checagem prévia dos elementos: sem isso o gst-launch falha com mensagem
# genérica ("no element ...") já no meio do pipeline.
MISSING=0
for element in videotestsrc nvvidconv nvv4l2h264enc h264parse flvmux rtmpsink; do
    if ! gst-inspect-1.0 "$element" >/dev/null 2>&1; then
        echo "erro: elemento GStreamer ausente: $element" >&2
        case "$element" in
            nvvidconv|nvv4l2h264enc)
                echo "      (plugin L4T do Jetson: verifique se o host é o Orin" >&2
                echo "       com os plugins NVIDIA instalados)" >&2
                ;;
        esac
        MISSING=1
    fi
done
[ "$MISSING" -eq 0 ] || die "instale/valide os plugins acima antes de publicar"

URI="rtmp://${HOST}:${PORT}/${PATH_NAME}"
echo "publicando $URI (${WIDTH}x${HEIGHT} @ ${FPS}fps, ${BITRATE}bps, pattern=${PATTERN}, ${RUN_SECONDS}s)" >&2

CMD=(gst-launch-1.0 -e
    videotestsrc is-live=true pattern="$PATTERN"
    ! "video/x-raw,width=${WIDTH},height=${HEIGHT},framerate=${FPS}/1"
    ! nvvidconv
    ! "video/x-raw(memory:NVMM),format=NV12"
    ! nvv4l2h264enc bitrate="$BITRATE" insert-sps-pps=true preset-level=1
    ! h264parse
    ! flvmux streamable=true
    ! rtmpsink location="$URI")

# O gst-launch roda como FILHO (não `exec`): assim o nome do script continua
# visível no `ps`, e `pkill -f rtmp_publisher.sh` encerra o publisher de
# verdade — o trap repassa o sinal ao pipeline, que fecha o FLV com EOS.
CHILD_PID=""
TIMER_PID=""

stop_pipeline() {
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        # INT, não TERM: o gst-launch faz EOS e o FLV é finalizado.
        kill -INT "$CHILD_PID" 2>/dev/null || true
    fi
}

# INT/TERM: `pkill -f rtmp_publisher.sh` e Ctrl-C. HUP: fechar o terminal — sem
# ele o gst-launch ficaria órfão publicando no MediaMTX.
trap stop_pipeline INT TERM HUP

"${CMD[@]}" &
CHILD_PID=$!

if [ "$RUN_SECONDS" -gt 0 ]; then
    ( sleep "$RUN_SECONDS"; stop_pipeline ) &
    TIMER_PID=$!
fi

set +e
wait "$CHILD_PID"
STATUS=$?
set -e

if [ -n "$TIMER_PID" ]; then
    kill "$TIMER_PID" 2>/dev/null || true
fi

# Encerramento por sinal (pkill / --seconds / Ctrl-C) não é falha do publisher.
if [ "$STATUS" -ge 128 ]; then
    exit 0
fi
exit "$STATUS"
