#!/usr/bin/env bash
# Teste de validação da Fase 3 — OSD + preview H.264/RTP.
#
# Executar DENTRO do container vision-stability-app:
#   docker exec -it vision-stability-app bash scripts/run_phase3_test.sh
#
# Variáveis opcionais (podem vir do ambiente):
#   TEST_DURATION_S   duração do teste em segundos (padrão 1800 = 30 min)
#   METRICS_INTERVAL_S intervalo das métricas (padrão 30)
#   PREVIEW_HOST/IP   destino do stream RTP (definido no compose)
#
# Log bruto é gravado em data/logs/phase3-30min-<data>.log para a avaliação.
set -euo pipefail

DURATION="${TEST_DURATION_S:-1800}"
METRICS_INTERVAL="${METRICS_INTERVAL_S:-30}"
STAMP="$(date +%Y-%m-%d)"
LOG="data/logs/phase3-30min-${STAMP}.log"

cd "$(dirname "$0")/.."
mkdir -p data/logs

echo "[run_phase3_test] duracao=${DURATION}s metrics_interval=${METRICS_INTERVAL}s log=${LOG}" >&2

TEST_DURATION_S="$DURATION" \
METRICS_INTERVAL_S="$METRICS_INTERVAL" \
python3 deepstream/pipeline_real.py 2>&1 | tee "$LOG"

exit "${PIPESTATUS[0]}"
