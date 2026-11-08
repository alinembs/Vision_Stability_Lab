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
#      com o container **parado em standby** (o observador o para quando não há
#      fonte), o `docker exec` falha e a guarda simplesmente segue: standby não
#      bloqueia a troca;
#   3. pré-check (D7): `rtsp_read_probe.py --seconds 5` exit 0 ou aborta com
#      exit 4 (nada é escrito, container intacto);
#   4. grava `data/config/active_source` de forma atômica (guarda o valor
#      anterior para rollback) e reivindica o marcador
#      `data/config/switch_in_progress` com `noclobber` — o dashboard usa o
#      mesmo arquivo como mutex entre processos e só apaga o marcador que
#      criou; outra troca em curso aborta aqui com exit 3, sem tocar em nada.
#      O marcador **válido** de outro dono (observador/watchdog, dashboard) é
#      respeitado e nunca apagado: primeiro se cede, depois se repete. Expirado
#      é órfão (dono morto, `kill -9`): pode ser descartado, senão uma queda
#      deixaria a troca bloqueada para sempre;
#   5. recria o container do pipeline (`docker compose up -d --no-deps`) e lê o
#      `StartedAt` do processo novo — `up` também **inicia** container parado,
#      então a troca sobe o pipeline de volta do standby **depois** do
#      pré-check (nunca antes de saber que a fonte nova entrega vídeo);
#   6. espera até `--timeout` (default 90 s) por um `interval_metrics` **do
#      processo novo** com `source_id` pedido **e** `fps_in > 0`; o orçamento
#      conta a partir da recriação, não do início do script (pré-check e parada
#      não dizem nada sobre o pipeline novo);
#   7. pós-check: se não confirmou, restaura a fonte anterior, recria de novo e
#      sai 1 — nunca "PASS na fonte errada". "Do processo novo" é
#      `session_epoch >= StartedAt` do container: sem isso, uma troca para a
#      mesma fonte já ativa aprovaria com a métrica do processo anterior.
#
# Toda execução deixa uma linha em `data/logs/switch.jsonl` — inclusive a
# tentativa bloqueada pelo guard de precedência (exit 3).
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
# Sobrescrevível por ambiente para o teste de ponta a ponta do script poder
# trabalhar em diretório temporário (o default continua o da bancada). O nome é
# `SWITCH_CONFIG_DIR` **de propósito**, igual ao dashboard (`switch_control.py`)
# e ao compose de troca: um `CONFIG_DIR` herdado de perfil de shell/CI apontava
# o script para outro diretório, e o pipeline continuava lendo o antigo — troca
# que "passa" sem efeito (falha silenciosa).
SWITCH_CONFIG_DIR="${SWITCH_CONFIG_DIR:-$REPO_ROOT/data/config}"
CONTROL_FILE="$SWITCH_CONFIG_DIR/active_source"
SWITCH_MARKER="$SWITCH_CONFIG_DIR/switch_in_progress"
TELEMETRY_FILE="${TELEMETRY_FILE:-$REPO_ROOT/data/logs/live.jsonl}"
SWITCH_LOG="${SWITCH_LOG:-$REPO_ROOT/data/logs/switch.jsonl}"
PRECHECK_SECONDS="${PRECHECK_SECONDS:-5}"
# Dono do marcador `switch_in_progress`. O dashboard (`E4-T3`) usa o **mesmo**
# arquivo como mutex entre processos: quem apaga só remove o próprio marcador
# (H2 da revisão independente do E4, 2026-11-01).
SWITCH_OWNER="${SWITCH_OWNER:-script:$$:$(date +%s)}"

# Estado do arquivo de controle anterior (preenchido antes do guard de
# precedência, porque a tentativa bloqueada também é registrada em
# `switch.jsonl` com o `from` correto).
PREVIOUS_EXISTS=0
PREVIOUS_VALUE=""
# `StartedAt` do processo novo e epoch local logo depois da recriação: são a
# prova de qual processo escreveu a métrica do pós-check e o início do
# orçamento do `--timeout` (F1/F2).
RECREATED_AT=""
STARTED_AT=""
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
    # Remove só o marcador **desta** execução: o dashboard (`E4-T3`) usa o
    # mesmo arquivo como mutex entre processos, e apagar o marcador de outro
    # dono liberava o watchdog antes da hora (H2 da revisão do E4).
    if [[ -f "$SWITCH_MARKER" ]] \
        && grep -q "\"owner\": \"$SWITCH_OWNER\"" "$SWITCH_MARKER" 2>/dev/null; then
        rm -f -- "$SWITCH_MARKER" 2>/dev/null || true
    fi
    # Resto do descarte do órfão que não coube de volta. Na corrida tripla
    # (órfão expirado -> marcador válido de A durante o `mv` -> marcador válido
    # de B ocupando o path canônico antes do `mv -n` de volta), o marcador de A
    # fica preso em `switch_in_progress.stale.$$` e ninguém o recolhe: o `mv -n`
    # não sobrescreve B, e o dono de A já não consegue recolocá-lo (o `cleanup`
    # dele só olha o path canônico). Só o `.stale.$$` **deste** processo é
    # tratado aqui — `*.stale.*` de outro PID é de outra execução e continua
    # valendo a regra de não apagar arquivo alheio.
    #
    # O mutex é o path canônico: um marcador deslocado não protege mais nada.
    # Nenhum leitor (`switch_control.py`, `watchdog.py`) lê `*.stale.*`, então
    # mantê-lo só sujaria o diretório. O conteúdo vai para o log antes do `rm`
    # para preservar a evidência de quem era o marcador deslocado (`owner`,
    # `to`, `expires_at`). O path canônico **nunca** é tocado aqui: a regra de
    # não apagar marcador alheio vale para ele e continua valendo.
    local own_stale="$SWITCH_MARKER.stale.$$"
    if [[ -f "$own_stale" ]]; then
        log "marcador alheio deslocado: $(tr -d '\n' <"$own_stale" 2>/dev/null || true) (removendo $own_stale)"
        rm -f -- "$own_stale" 2>/dev/null || true
    fi
    rm -f -- "$CONTROL_FILE.tmp.$$" 2>/dev/null || true
}
trap cleanup EXIT

write_control() {
    local value="$1" tmp="$CONTROL_FILE.tmp.$$"
    mkdir -p -- "$SWITCH_CONFIG_DIR"
    printf '%s\n' "$value" >"$tmp"
    mv -f -- "$tmp" "$CONTROL_FILE"
}

marker_expired() {
    # Decisão pura e testada em `switch_source_lib.marker_expired` (exit 0 =
    # órfão, pode descartar; exit 1 = em curso). Honrar `expires_at` é o que
    # impede que um `kill -9` no meio da troca deixe o marcador e **nenhuma**
    # execução futura troque fonte até alguém apagar à mão.
    local file="${1:-$SWITCH_MARKER}"
    "$PYTHON" "$LIB" marker-expired --file "$file" \
        --ttl "$(( ${TIMEOUT_S%.*} + 60 ))" >/dev/null 2>&1
}

# Recusa a troca cedendo o mutex: usada tanto pelo marcador válido de outro
# dono quanto pelo válido que apareceu durante o descarte do órfão (TOCTOU).
# Nunca escreve `active_source` e sempre deixa a tentativa auditada.
refuse_busy() {
    log "outra troca de fonte em andamento; abortando"
    PRECHECK_NOTE="blocked"
    record_switch "FAIL" "troca_em_andamento"
    die 3 "FAIL source=$SOURCE_ID reason=troca_em_andamento (nada foi escrito; container intacto)"
}

write_marker() {
    local expires payload stale
    expires="$(date -u -d "+$(( ${TIMEOUT_S%.*} + 60 )) seconds" \
        +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p -- "$SWITCH_CONFIG_DIR"
    payload="$(printf '{"to": "%s", "expires_at": "%s", "owner": "%s", "origin": "script"}' \
        "$SOURCE_ID" "$expires" "$SWITCH_OWNER")"
    stale="$SWITCH_MARKER.stale.$$"
    # Reivindicação atômica (`set -C` = noclobber): se o arquivo já existe, é
    # porque outra troca (script ou dashboard) está em curso — ou é órfão de
    # uma execução morta. Só o órfão (expirado) é descartado.
    if ! (set -C; printf '%s\n' "$payload" >"$SWITCH_MARKER") 2>/dev/null; then
        if marker_expired; then
            log "marcador órfão (expirado) em $SWITCH_MARKER: descartando"
            # TOCTOU: entre a checagem acima e um `rm`, outro dono (observador,
            # dashboard) pode remover o órfão e criar um marcador **válido** no
            # mesmo path — e nós apagaríamos o mutex dele. `mv` é atômico e o
            # inode renomeado é de quem renomeou: revalida-se o arquivo movido.
            # Mesmo desenho de `switch_control._discard_stale_marker` (dashboard).
            if ! mv -f -- "$SWITCH_MARKER" "$stale" 2>/dev/null; then
                log "marcador desapareceu antes do descarte; reivindicando de novo"
            elif ! marker_expired "$stale"; then
                # Perdemos a corrida: o "órfão" virou marcador válido de outro
                # dono. Devolve ao lugar e cede. `mv -n` (não `-f`) de propósito:
                # se um terceiro marcador ocupou o path, não o sobrescrevemos.
                log "marcador virou válido durante o descarte: devolvendo e cedendo"
                mv -n -- "$stale" "$SWITCH_MARKER" 2>/dev/null || true
                if [[ -e "$stale" ]]; then
                    log "AVISO: $SWITCH_MARKER ocupado; marcador alheio não coube de volta e fica em $stale (será registrado no log e removido no cleanup)"
                fi
                refuse_busy
            else
                rm -f -- "$stale" 2>/dev/null || true
            fi
            if ! (set -C; printf '%s\n' "$payload" >"$SWITCH_MARKER") 2>/dev/null; then
                refuse_busy
            fi
        else
            refuse_busy
        fi
    fi
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
PRECHECK_MASKED_URI="$(kv "$resolved" precheck_masked_uri)"
CODEC="$(kv "$resolved" codec)"
TARGET_FPS="$(kv "$resolved" target_fps)"
log "fonte válida: id=$SOURCE_ID codec=$CODEC target_fps=$TARGET_FPS uri=$MASKED_URI"

# --- 1b. valor anterior do controle ----------------------------------------
# Lido antes do guard: a tentativa bloqueada também é auditada (F4) e a linha
# de `switch.jsonl` precisa do `from` real.
if [[ -f "$CONTROL_FILE" ]]; then
    PREVIOUS_EXISTS=1
    PREVIOUS_VALUE="$(cat -- "$CONTROL_FILE")"
fi

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
    # Contrato do módulo: uma linha por execução, PASS ou FAIL. Tentativa
    # bloqueada também é execução — sem isto a recusa não deixava auditoria.
    # `probe=blocked`: a sonda nem chegou a rodar (o guard veio antes, D7).
    PRECHECK_NOTE="blocked"
    record_switch "FAIL" "active_source_no_ambiente"
    die 3 "FAIL source=$SOURCE_ID reason=active_source_no_ambiente (nada foi escrito; container intacto)"
fi

# --- 3. pré-check (D7) -----------------------------------------------------
if [[ "$SKIP_PRECHECK" == "1" ]]; then
    PRECHECK_NOTE="skipped"
    log "AVISO: pré-check pulado (--skip-precheck) — só para o teste de ciclo (E6)"
else
    # A URI logada é a **sondada** (host da LAN), não a do YAML (`mediamtx`) —
    # logar outra URI manda o operador diagnosticar um endereço que a sonda
    # nunca abriu. Mascarada: DIV-001.
    log "pré-check: lendo $PRECHECK_MASKED_URI por ${PRECHECK_SECONDS}s"
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

# --- 4. escrita atômica do controle ---------------------------------------
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
RECREATED_AT="$(date +%s.%N)"
# `StartedAt` do processo novo: é o que prova qual processo escreveu a métrica
# do pós-check e onde começa o orçamento do `--timeout` (F1/F2). Se o inspect
# falhar, `--recreated-at` (relógio local) cobre o caso.
STARTED_AT="$(docker inspect --format '{{.State.StartedAt}}' "$APP_CONTAINER" \
    2>/dev/null || true)"
log "container recriado: started_at=${STARTED_AT:-?}"

# --- 6. pós-check: telemetria da fonte pedida, do processo novo, com frames -
log "aguardando interval_metrics de '$SOURCE_ID' (processo novo) com fps_in > 0 (até ${TIMEOUT_S}s após a recriação)"
wait_out=$("$PYTHON" "$LIB" wait --source-id "$SOURCE_ID" \
    --timeout "$TIMEOUT_S" --offset "$telemetry_offset" \
    --started-at "$STARTED_AT" --recreated-at "$RECREATED_AT" \
    --telemetry "$TELEMETRY_FILE" 2>/dev/null) || true
wait_ok="$(kv "$wait_out" ok)"
observed="$(kv "$wait_out" source_id)"
wait_reason="$(kv "$wait_out" reason)"
first_frame="$(kv "$wait_out" first_frame_in)"
fps_in="$(kv "$wait_out" fps_in)"
last_error="$(kv "$wait_out" last_error)"
session_epoch="$(kv "$wait_out" session_epoch)"

# --- 7. PASS ou rollback ---------------------------------------------------
if [[ "$wait_ok" == "1" ]]; then
    record_switch "PASS" "ok"
    finish "PASS source=$SOURCE_ID first_frame_in=${first_frame}s fps_in=$fps_in codec=$CODEC precheck=$PRECHECK_NOTE session_epoch=$session_epoch"
    exit 0
fi

log "pós-check NÃO confirmou a troca: $wait_reason"
# Rollback: `recreate_app` sobe o container de volta na fonte anterior **mesmo**
# quando a troca começou com o pipeline em standby (o observador o havia parado
# por falta de fonte). É deliberado — o FAIL precisa deixar o estado como
# estava, e "parado" não é o mesmo que "rodando na fonte anterior"; se a fonte
# anterior continua morta, o observador a para de novo no ciclo seguinte
# (volta ao standby). Não troque por `start`/`stop` condicional sem rever isso.
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
