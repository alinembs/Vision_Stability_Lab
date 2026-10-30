#!/usr/bin/env bash
# Troca a fonte ativa do Vision Stability Lab em um comando (E2-T3, D7).
#
# A fonte ativa é `data/config/active_source` (uma linha com o id de
# `deepstream/config/sources.yaml`). Trocar de fonte reinicia o pipeline (D4):
# nenhum estado atravessa fontes. Este script faz a sequência inteira e **prova**
# o resultado — sem ele, a troca é "editei o arquivo, recriei o container e
# espero que tenha pegado".
#
# Sequência (falha em qualquer passo aborta sem efeito colateral):
#   1. valida o id contra `deepstream/config/sources.yaml` (exit 2 se inválido);
#   2. guarda de precedência: `ACTIVE_SOURCE` no ambiente do container vence o
#      arquivo — se estiver preenchida, aborta com exit 3 (nada é escrito);
#   3. pré-check (D7): `rtsp_read_probe.py --seconds 5` exit 0 ou aborta com
#      exit 4 (nada é escrito, container intacto);
#   4. grava `data/config/active_source` de forma atômica (guarda o valor
#      anterior para rollback) e o marcador `data/config/switch_in_progress`;
#   5. recria o container do pipeline (`docker compose up -d --no-deps`);
#   6. espera até `--timeout` (default 90 s) por um `interval_metrics` novo com
#      `source_id` pedido **e** `fps_in > 0`;
#   7. pós-check: se não confirmou, restaura a fonte anterior, recria de novo e
#      sai 1 — nunca "PASS na fonte errada".
#
# Uso:
#   ./scripts/switch_source.sh camera
#   ./scripts/switch_source.sh drone --timeout 120
#   ./scripts/switch_source.sh drone --skip-precheck   # só no teste de ciclo E6
#   ./scripts/switch_source.sh --help
#
# Pré-requisitos: `deploy/.env` preenchido, MediaMTX no ar e — para drone e
# celular — o publisher publicando no path correspondente
# (`./scripts/probe/rtmp_publisher.sh --path drone`). O host `mediamtx` do YAML
# é o DNS do compose: no host, o pré-check troca por `MTX_HOST`/`LAN_IP`.
#
# Credencial: nenhuma URI sai na saída deste script; a sonda mascara o userinfo
# (`***@host:porta/path`, DIV-001).
#
# Um único container DeepStream: este script só recria `vision-stability-app`
# (regra global de containers DeepStream — nunca um segundo container do
# projeto).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
LIB="$SCRIPT_DIR/switch_source_lib.py"
PROBE="$SCRIPT_DIR/probe/rtsp_read_probe.py"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/deploy/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.yml}"
APP_CONTAINER="${APP_CONTAINER:-vision-stability-app}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-vision-app}"
CONFIG_DIR="$REPO_ROOT/data/config"
CONTROL_FILE="$CONFIG_DIR/active_source"
SWITCH_MARKER="$CONFIG_DIR/switch_in_progress"
TELEMETRY_FILE="${TELEMETRY_FILE:-$REPO_ROOT/data/logs/live.jsonl}"
SWITCH_LOG="${SWITCH_LOG:-$REPO_ROOT/data/logs/switch.jsonl}"
PRECHECK_SECONDS="${PRECHECK_SECONDS:-5}"

SWITCH_START="$(date +%s)"
# Estado do arquivo de controle anterior (preenchido em definitivo depois do
# pré-check; os defaults existem porque `record_switch` é chamado também nos
# caminhos que abortam antes de ler o controle).
PREVIOUS_EXISTS=0
PREVIOUS_VALUE=""

log() { printf '[switch] %s\n' "$*" >&2; }

# Resultado final: stdout é contrato (uma linha PASS/FAIL), o resto é log.
finish() {
    local line="$1"
    printf '%s\n' "$line"
    log "$line"
}

# `kv <bloco> <chave>`: lê o valor de uma linha `chave=valor` do bloco.
kv() {
    local block="$1" key="$2"
    printf '%s\n' "$block" | sed -n "s/^${key}=//p" | head -n1
}

die() {
    local code="$1" line="$2"
    finish "$line"
    exit "$code"
}

cleanup() {
    rm -f -- "$SWITCH_MARKER" "$CONTROL_FILE.tmp.$$" 2>/dev/null || true
}
trap cleanup EXIT

write_control() {
    local value="$1" tmp="$CONTROL_FILE.tmp.$$"
    mkdir -p -- "$CONFIG_DIR"
    printf '%s\n' "$value" >"$tmp"
    mv -f -- "$tmp" "$CONTROL_FILE"
}

write_marker() {
    local expires
    expires="$(date -u -d "+$(( ${TIMEOUT_S%.*} + 60 )) seconds" \
        +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p -- "$CONFIG_DIR"
    printf '{"to": "%s", "expires_at": "%s"}\n' "$SOURCE_ID" "$expires" \
        >"$SWITCH_MARKER"
}

recreate_app() {
    log "recriando o container $APP_CONTAINER (serviço $COMPOSE_SERVICE)"
    # `--force-recreate` é obrigatório: o arquivo de controle chega ao container
    # por volume (`../data`), não por configuração, então `up -d` puro responderia
    # "Container ... Running" e o pipeline **não** releria a fonte (D4 exige
    # processo novo).
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
        up -d --no-deps --force-recreate "$COMPOSE_SERVICE" >&2
}

record_switch() {
    local result="$1" reason="$2"
    mkdir -p -- "$(dirname -- "$SWITCH_LOG")"
    "$PYTHON" "$LIB" switch-record --to "$SOURCE_ID" \
        --previous "${PREVIOUS_VALUE:-}" --result "$result" \
        --reason "$reason" --probe "${PRECHECK_NOTE:-unknown}" \
        >>"$SWITCH_LOG" || true
}

# --- 0. argumentos (lógica pura e testada em scripts/switch_source_lib.py) ---
args_out=$("$PYTHON" "$LIB" args "$@") || exit $?
if [[ "$(kv "$args_out" help)" == "1" ]]; then
    printf '%s\n' "$args_out" | sed '/^help=/d'
    exit 0
fi
SOURCE_ID="$(kv "$args_out" id)"
SKIP_PRECHECK="$(kv "$args_out" skip_precheck)"
TIMEOUT_S="$(kv "$args_out" timeout)"

[[ -n "$SOURCE_ID" ]] || die 2 "FAIL reason=id_ausente (veja --help)"
[[ -f "$ENV_FILE" ]] || die 2 \
    "FAIL reason=env_ausente file=$ENV_FILE (copie deploy/.env.example)"

# --- 1. validação do id contra o sources.yaml ------------------------------
log "validando a fonte '$SOURCE_ID' contra deepstream/config/sources.yaml"
resolved=$("$PYTHON" "$LIB" resolve --id "$SOURCE_ID" --env-file "$ENV_FILE") \
    || exit 2
PRECHECK_URI="$(kv "$resolved" precheck_uri)"
MASKED_URI="$(kv "$resolved" masked_uri)"
CODEC="$(kv "$resolved" codec)"
TARGET_FPS="$(kv "$resolved" target_fps)"
log "fonte válida: id=$SOURCE_ID codec=$CODEC target_fps=$TARGET_FPS uri=$MASKED_URI"

# --- 2. guarda de precedência: ACTIVE_SOURCE vence o arquivo ---------------
container_active="$(docker exec "$APP_CONTAINER" printenv ACTIVE_SOURCE \
    2>/dev/null || true)"
env_active="$("$PYTHON" "$LIB" active-source-env --env-file "$ENV_FILE" \
    | sed -n 's/^value=//p' | head -n1)"
if ! guard_out=$("$PYTHON" "$LIB" guard \
    --container-value "$container_active" --env-value "$env_active" 2>&1); then
    # O bloco traz a mensagem acionável e uma linha de contrato (`blocked=1`)
    # que não interessa a quem lê o erro.
    printf '%s\n' "$guard_out" | grep -v '^blocked=' >&2 || true
    die 3 "FAIL source=$SOURCE_ID reason=active_source_no_ambiente (nada foi escrito; container intacto)"
fi

# --- 3. pré-check (D7) -----------------------------------------------------
if [[ "$SKIP_PRECHECK" == "1" ]]; then
    PRECHECK_NOTE="skipped"
    log "AVISO: pré-check pulado (--skip-precheck) — só para o teste de ciclo (E6)"
else
    log "pré-check: lendo $MASKED_URI por ${PRECHECK_SECONDS}s"
    if ! probe_json=$("$PYTHON" "$PROBE" --url "$PRECHECK_URI" \
        --seconds "$PRECHECK_SECONDS" --json 2>/dev/null); then
        probe_info=$("$PYTHON" "$LIB" probe-reason --report "$probe_json" \
            2>/dev/null || true)
        probe_reason="$(kv "$probe_info" reason)"
        probe_detail="$(kv "$probe_info" detail)"
        [[ -n "$probe_reason" ]] || probe_reason="no_frames_on_path"
        PRECHECK_NOTE="failed"
        record_switch "FAIL" "precheck:${probe_reason}"
        die 4 "FAIL source=$SOURCE_ID reason=$probe_reason detail=\"$probe_detail\" (pré-check reprovado; nada foi escrito; container intacto)"
    fi
    PRECHECK_NOTE="ok"
    log "pré-check PASS: a fonte está entregando vídeo"
fi

# --- 4. valor anterior + escrita atômica do controle -----------------------
if [[ -f "$CONTROL_FILE" ]]; then
    PREVIOUS_EXISTS=1
    PREVIOUS_VALUE="$(cat -- "$CONTROL_FILE")"
fi
log "fonte anterior: ${PREVIOUS_VALUE:-<vazio: default_source do YAML>}"

telemetry_offset=0
if [[ -f "$TELEMETRY_FILE" ]]; then
    telemetry_offset="$(stat -c%s -- "$TELEMETRY_FILE")"
fi

write_marker
write_control "$SOURCE_ID"
log "data/config/active_source = $SOURCE_ID"

# --- 5. recriação do container do pipeline --------------------------------
recreate_app

# --- 6. pós-check: telemetria da fonte pedida, com frames ------------------
log "aguardando interval_metrics de '$SOURCE_ID' com fps_in > 0 (até ${TIMEOUT_S}s)"
wait_out=$("$PYTHON" "$LIB" wait --source-id "$SOURCE_ID" \
    --timeout "$TIMEOUT_S" --offset "$telemetry_offset" \
    --since "$SWITCH_START" --telemetry "$TELEMETRY_FILE" 2>/dev/null) || true
wait_ok="$(kv "$wait_out" ok)"
observed="$(kv "$wait_out" source_id)"
wait_reason="$(kv "$wait_out" reason)"
first_frame="$(kv "$wait_out" first_frame_in)"
fps_in="$(kv "$wait_out" fps_in)"
last_error="$(kv "$wait_out" last_error)"

# --- 7. PASS ou rollback ---------------------------------------------------
if [[ "$wait_ok" == "1" ]]; then
    record_switch "PASS" "ok"
    finish "PASS source=$SOURCE_ID first_frame_in=${first_frame}s fps_in=$fps_in codec=$CODEC precheck=$PRECHECK_NOTE"
    exit 0
fi

log "pós-check NÃO confirmou a troca: $wait_reason"
rollback=$("$PYTHON" "$LIB" rollback --ok "$wait_ok" --reason "$wait_reason" \
    --previous "$PREVIOUS_VALUE" --expected "$SOURCE_ID" \
    --observed "$observed" 2>/dev/null || true)
log "rollback: $wait_reason — restaurando ${PREVIOUS_VALUE:-<sem arquivo: default_source>}"
if [[ "$PREVIOUS_EXISTS" == "1" ]]; then
    write_control "$PREVIOUS_VALUE"
else
    rm -f -- "$CONTROL_FILE"
fi
recreate_app
record_switch "FAIL" "$wait_reason"
finish "FAIL source=$SOURCE_ID reason=\"$wait_reason\" observed=\"${observed:-?}\" rollback=restored:${PREVIOUS_VALUE:-default_source} last_error=\"${last_error:-—}\""
exit 1
