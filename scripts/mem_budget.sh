#!/usr/bin/env bash
# Orçamento de memória do Vision Stability Lab — uma linha JSON por amostra.
#
# Uso:
#   bash scripts/mem_budget.sh              # imprime uma amostra
#   bash scripts/mem_budget.sh --loop 60 12 # 12 amostras a cada 60 s
#
# Campos:
#   rss_mb          RSS do processo do pipeline (null se o container não estiver no ar)
#   ram_used_pct    uso de RAM do sistema (memória unificada do Jetson)
#   lfb_total_mb    memória de framebuffer livre (fragmentação) via tegrastats
#   oom_killed      true se o container do pipeline morreu por OOM (INC-004)
#
# Gates do plano (docs/plano-multifonte-vsl.md):
#   1 fonte: RSS 750–1050 MB · RAM ≤ 70% (stack arquo parado) · lfb ≥ 400 MB · exit 137 = 0

set -uo pipefail

APP_CONTAINER="${APP_CONTAINER:-vision-stability-app}"
NOFRAME_LOG="$(dirname "$0")/../data/logs/live.jsonl"

_loop_interval=0
_loop_count=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --loop) _loop_interval="${2:-60}"; _loop_count="${3:-1}"; shift 3 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "erro: argumento desconhecido: $1 (use --help)" >&2; exit 2 ;;
    esac
done

_amostra() {
    local rss_mb="null" oom="null" exit_code="null" restarts="null" started_at="null"
    if docker inspect "$APP_CONTAINER" >/dev/null 2>&1; then
        oom=$(docker inspect --format '{{.State.OOMKilled}}' "$APP_CONTAINER" 2>/dev/null || echo null)
        exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$APP_CONTAINER" 2>/dev/null || echo null)
        restarts=$(docker inspect --format '{{.RestartCount}}' "$APP_CONTAINER" 2>/dev/null || echo null)
        started_at=$(docker inspect --format '{{.State.StartedAt}}' "$APP_CONTAINER" 2>/dev/null || echo null)
        if [[ "$(docker inspect --format '{{.State.Running}}' "$APP_CONTAINER" 2>/dev/null)" == "true" ]]; then
            # VmRSS do processo do pipeline, em MB. Não é `/proc/1`: com
            # `init: true` no compose o PID 1 é o `docker-init` (tini), e ler o
            # init daria ~1 MB — sempre abaixo do piso abaixo, RSS sempre null.
            # Valor abaixo de 100 MB indica leitura durante reinício do processo
            # (pipeline recém-criado): reporta null em vez de envenenar a série
            # — o campo `restarts` denuncia o ciclo.
            local pip_pid
            pip_pid=$(docker exec "$APP_CONTAINER" pgrep -f "^python3 .*pipeline_real" 2>/dev/null | head -1)
            rss_mb=$(docker exec "$APP_CONTAINER" awk '/^VmRSS:/{printf "%.0f", $2/1024}' "/proc/${pip_pid:-1}/status" 2>/dev/null || echo null)
            if [[ -z "$rss_mb" ]] || ! [[ "$rss_mb" =~ ^[0-9]+$ ]] || (( rss_mb < 100 )); then
                rss_mb="null"
            fi
        fi
    fi

    local ram_total ram_avail ram_used pct
    read -r ram_total ram_avail < <(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print t, a}' /proc/meminfo)
    ram_total=$((ram_total / 1024))
    ram_used=$(( (ram_total * 1024 - ram_avail) / 1024 ))
    pct=$(awk -v u="$ram_used" -v t="$ram_total" 'BEGIN{printf "%.1f", (u/t)*100}')

    # lfb livre: tegrastats imprime "lfb 123x4MB" (blocos de 4 MB)
    local lfb_mb="null"
    if command -v tegrastats >/dev/null 2>&1; then
        local linha lfb_raw blocos tam
        linha=$(timeout 3 tegrastats --interval 1000 2>/dev/null | head -1 || true)
        lfb_raw=$(grep -oP 'lfb \K[0-9]+x[0-9]+MB' <<<"$linha" || true)
        if [[ -n "$lfb_raw" ]]; then
            blocos="${lfb_raw%x*}"
            tam="${lfb_raw#*x}"; tam="${tam%MB}"
            lfb_mb=$((blocos * tam))
        fi
    fi

    local ds_containers
    ds_containers=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -ciE 'deepstream|vision-stability-app' || true)

    printf '{"ts":"%s","app":"%s","running":%s,"rss_mb":%s,"ram_used_mb":%s,"ram_total_mb":%s,"ram_used_pct":%s,"lfb_total_mb":%s,"oom_killed":%s,"exit_code":%s,"restarts":%s,"started_at":"%s","ds_containers":%s,"metrics_log_age_s":%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$APP_CONTAINER" \
        "$([[ "$rss_mb" != "null" ]] && echo true || echo false)" \
        "$rss_mb" "$ram_used" "$ram_total" "$pct" "$lfb_mb" \
        "$([[ "$oom" == "null" ]] && echo null || echo "$oom")" \
        "$([[ "$exit_code" == "null" ]] && echo null || echo "$exit_code")" \
        "$([[ "$restarts" == "null" ]] && echo null || echo "$restarts")" \
        "$([[ "$started_at" == "null" ]] && echo "" || echo "$started_at")" \
        "$ds_containers" \
        "$([[ -f "$NOFRAME_LOG" ]] && echo $(( $(date +%s) - $(stat -c %Y "$NOFRAME_LOG") )) || echo null)"
}

for ((i = 1; i <= _loop_count; i++)); do
    _amostra
    if (( i < _loop_count )); then sleep "$_loop_interval"; fi
done
