# Component Catalog — Vision Stability Lab

> **Natureza:** inventário do que existe HOJE. Não propõe componentes novos.
> **Data:** 2026-11-01 · **Commit:** `cd5bbfb` · **Branch:** `feature/multifonte`
> **Regra de classificação:**
> - **ATIVO** — está no caminho de execução de um serviço do compose.
> - **OPERACIONAL** — não roda em container, mas é executado por processo humano/script em campo.
> - **BANCADA** — instrumento de teste/medição das fases anteriores.
> - **LEGADO** — existe, pode ser importado, mas nada o executa.
> - **ÓRFÃO** — arquivo de configuração/dado sem nenhum leitor no código.

---

## 1. Núcleo de mídia — `deepstream/`

| Componente | Caminho | Responsabilidade | Entradas | Saídas | Estado | LOC | Situação |
|---|---|---|---|---|---|---|---|
| **pipeline_real.py** | `deepstream/pipeline_real.py` | Entrypoint real. Monta/linka pipeline GStreamer, 4 probes pyds, telemetria JSONL, ramo de preview descartável, reconexão da fonte, shutdown gracioso | ~33 env vars, `sources.yaml`, `data/config/active_source`, `infer.txt`, `config_tracker_IOU.yml` | RTSP→MediaMTX, UDP opcional, JSONL, exit 0/1/2 | `VisionPipeline` (pipeline, FpsCounter, `AreaMonitor`, budget/reconexão, estado do preview), `SESSION` global | **1215** | **ATIVO** (`deploy/Dockerfile:29`) |
| **sources.py** | `deepstream/sources.py` | Contrato declarativo de fontes: carga/validação do YAML, precedência da fonte ativa, fábrica depay/parse, mascaramento de URI. **Puro** (sem `gi`/`pyds`) | `sources.yaml`/`SOURCES_CONFIG`, env, `active_source` | `SourceList`/`SourceConfig`, `(depay, parser)` | Nenhum | 646 | **ATIVO** |
| **source_state.py** | `deepstream/source_state.py` | Identidade de sessão de fonte, `session_epoch` monotônico, namespace de telemetria, reset de contadores | `source_id`, `STARTED_AT` | `SessionState`, dict namespaceado | Epoch monotônico entre processos | 206 | **ATIVO** |
| **area_monitor.py** | `deepstream/area_monitor.py` | Regra temporal de ocupação: TTL de track, EMPTY/OCCUPIED, dwell, eventos limitados | `{id: bbox}`, timestamp | `{entered, exited, transition}`, `recent_events` | `tracks`, `area_state`, totais | 96 | **ATIVO** |
| **source_budget.py** | `deepstream/source_budget.py` | Orçamento de tentativas de reconexão com devolução após estabilidade. Puro | `max_attempts`, `reset_after_s`, timestamps | contagem / esgotamento | Tentativas + marca | 63 | **ATIVO** |
| **source_reconnect.py** | `deepstream/source_reconnect.py` | Máquina de estados pura da reconexão (stop/wait/backoff/exhausted), fatias com deadline | resultado de cada fatia | `STEP_STOP/START/RESUMED/RETRY/GIVE_UP` | Fase, tentativa, deadline | 217 | **ATIVO** |
| **bus_classifier.py** | `deepstream/bus_classifier.py` | Classificação pura de ERROR/EOS: `quit`/`reconnect`/`drop_preview`/`ignore`, ciente dos elementos do preview | `src_name`, tipo, detalhe | Ação | Nenhum | 138 | **ATIVO** |
| **graceful_shutdown.py** | `deepstream/graceful_shutdown.py` | `SIGTERM`/`SIGINT` via GLib (`unix_signal_add`), fallback `signal` + `idle_add` | Sinais | `_request_shutdown` no thread do loop | Handler instalado | 124 | **ATIVO** |
| **health_metrics.py** | `deepstream/health_metrics.py` | Coleta psutil: RSS/VMS, growth vs warm-up, threads, fds, RAM/CPU | — | dict `process`/`system` | `warmup_rss` | 41 | **ATIVO** |
| **rtsp_env.py** | `deepstream/rtsp_env.py` | Leitura fail-closed de `RTSP_URI` (correção do DIV-001) | `RTSP_URI` | URI ou `RtspUriError` | Nenhum | 56 | **ATIVO** (via fallback de `sources.py`) |
| **tegrastats_reader.py** | `deepstream/tegrastats_reader.py` | Parser de `tegrastats` do Jetson | linhas tegrastats | métricas | Estado de leitura | 27 | **LEGADO** — importado só por `pipeline.py:12` e `pipeline_rtsp_real.py:12`; `phase5_run.py` tem parser local próprio (`parse_tegrastats`, `:109`) |
| **tracker_state.py** | `deepstream/tracker_state.py` | Primeira versão do estado de tracks; substituída por `AreaMonitor` | tracks | estado | tracks | 52 | **LEGADO** |
| **pipeline.py** | `deepstream/pipeline.py` | Pipeline sintético (sem RTSP real) para validar fluxo de probe | — | log | — | 112 | **LEGADO** |
| **pipeline_rtsp_real.py** | `deepstream/pipeline_rtsp_real.py` | Pipeline com câmera real, geração anterior | `RTSP_URI` | log | — | 107 | **LEGADO** |
| **app.py** | `deepstream/app.py` | Wrapper que importa `pipeline.main` — **entrypoint enganoso** | — | — | — | 19 | **LEGADO** |
| **rtsp_*_test.py / phase1_*.py** (8 arquivos) | `deepstream/` | Sondas de bancada das fases 1–3 | `RTSP_URI` | stdout | — | 50–91 cada | **BANCADA** |

### 1.1 Configuração — `deepstream/config/`

| Arquivo | Situação | Evidência |
|---|---|---|
| `sources.yaml` | **ATIVO** — descritor das 3 fontes | `sources.py:458` |
| `infer.txt` | **ATIVO** — YOLO11m FP16, classe `person`, threshold 0.5 | `pipeline_real.py:362` |
| `config_tracker_IOU.yml` | **ATIVO** — `nvtracker` IOU | `pipeline_real.py:364` || `source.yaml` | **ÓRFÃO** — INI `[source0]`, `uri=rtsp://localhost:8554/live`; só citado em `README.md:654` | sem leitor |
| `osd.yml` | **ÓRFÃO** — estilo do OSD vem do código | sem leitor |
| `tracker_iou.ini` | **ÓRFÃO** — se autodeclara "referência canônica" não consumida | `tracker_iou.ini:1-5` |
| `tracker.yml.legacy` | **ÓRFÃO** — marcado como legado no próprio nome | sem leitor |

---

## 2. Dashboard — `dashboard/`

| Componente | Caminho | Responsabilidade | Entradas | Saídas | Estado | LOC | Situação |
|---|---|---|---|---|---|---|---|
| **backend/app.py** | `dashboard/backend/app.py` | API REST + WS, tailer do JSONL, replay, saúde derivada, serviço do frontend | `live.jsonl` (ro) | `/api/snapshot`, `/api/history`, `/api/events`, `/ws`, `/healthz`, `/`, `/static` | `DashboardState`: 480 métricas, 500 eventos, `current_run`, `subscribers` | 241 | **ATIVO** |
| **frontend/index.html** | `dashboard/frontend/` | Shell: lâmpadas de saúde, `<video>`, readouts, 2 canvases, painel, log de eventos | WS/REST | DOM | — | 115 | **ATIVO** |
| **frontend/app.js** | `dashboard/frontend/` | Player WHEP (sonda TLS 8889 → HLS 8888), gráficos canvas, WebSocket, cards | `/api/*`, `/ws`, MediaMTX | DOM | `localStorage.whep_url` | 559 | **ATIVO** |
| **frontend/capture.html + capture.js + codec-gate.js** | `dashboard/frontend/` | Página de captura do celular: `getUserMedia` + POST WHIP em `8889/phone/whip` | câmera do celular | stream WHIP | sessão WHIP | 179+696+429 | **ATIVO** (nginx 8443) |
| **frontend/style.css** | `dashboard/frontend/` | Estilo | — | — | — | 314 | **ATIVO** |
| **frontend/codec-gate-selftest.html** | `dashboard/frontend/` | Autoteste do gate de codec | — | — | — | 166 | **BANCADA** |
| **Dockerfile** | `dashboard/Dockerfile` | `python:3.11-slim` + uvicorn :8000 | — | — | — | 11 | **ATIVO** |

**[F]** O backend não importa nada de `deepstream/` nem de `scripts/` (`app.py:15-26`): o
acoplamento com o pipeline é **por dado (arquivo)**, não por código. É o melhor isolamento do repo.

---

## 3. Supervisão e operação — `scripts/`

| Componente | Caminho | Responsabilidade | Entradas | Saídas | Estado | LOC | Situação |
|---|---|---|---|---|---|---|---|
| **watchdog.py** | `scripts/watchdog.py` | Supervisor externo: 5 regras + crash-loop + `container_missing` + marcador de troca; restart via socket Docker | `live.jsonl`, `/proc/meminfo`, logs do container, `docker.sock`, `switch_in_progress` | `watchdog.jsonl`, `POST /containers/<id>/restart` | Janelas de baseline, cooldowns, contadores | 888 | **ATIVO** |
| **switch_source.sh** | `scripts/switch_source.sh` | Troca de fonte em 7 passos: valida id → guard de precedência → pré-check RTSP → grava `active_source` + marcador → recria container → espera `interval_metrics` do processo novo → rollback | id da fonte, `deploy/.env`, `sources.yaml` | `data/config/active_source`, `switch.jsonl`, exit 0–4 | rollback local | — | **OPERACIONAL** |
| **switch_source_lib.py** | `scripts/switch_source_lib.py` | Lógica pura/testável do script: parse de env, `resolve_source`, `precheck_url`, `wait_for_source`, `decide_rollback` | — | exit code, relatório | `WaitOutcome`, `RollbackDecision` | 805 | **OPERACIONAL** | 
| **probe/rtsp_read_probe.py** | `scripts/probe/` | Sonda de aceitação RTSP: fps/gaps, PASS/FAIL, mascara credencial | URI RTSP | JSON, exit code | — | ~760 | **OPERACIONAL** (pré-check da troca) |
| **probe/whip_probe.py** | `scripts/probe/` | Cliente WHIP: offer/answer, TLS, DELETE de sessão | URL WHIP | JSON, exit code | — | ~330 | **OPERACIONAL** (aceitação de celular) |
| **phase5_run.py** | `scripts/phase5_run.py` | Bancada Fase 5: janelas de JSONL + tegrastats + erros do container → sumário | JSONL, tegrastats | relatório | — | 371 | **BANCADA** |
| **mem_budget.sh** | `scripts/mem_budget.sh` | Amostragem de orçamento de memória (RSS, RAM %, LFB, OOM) | docker/meminfo/tegrastats | JSON por linha | — | — | **BANCADA** |
| **run_phase3_test.sh** | `scripts/run_phase3_test.sh` | Rodada de 30 min da Fase 3 | — | logs | — | — | **BANCADA** |
| **probe/rtmp_publisher.sh** | `scripts/probe/` | Publisher RTMP para o path `drone` | — | stream | — | — | **BANCADA** |
| **probe/ingest_report.py + ingest_report_lib.py** | `scripts/probe/` | Relatório de ingestão (latência/gaps) | — | JSON | — | — | **BANCADA** |

---

## 4. Infraestrutura — `deploy/` e `mediamtx/`

| Componente | Caminho | Responsabilidade | Situação |
|---|---|---|---|
| `docker-compose.yml` | `deploy/` | Orquestra 5 serviços; publica portas de mídia em `${LAN_IP}` e dashboard em `0.0.0.0` | **ATIVO** |
| `Dockerfile` | `deploy/` | Imagem do pipeline: base `deepstream-l4t:7.1-triton-multiarch`, wheel pyds 1.2.0 vendorizado, `CMD pipeline_real.py` | **ATIVO** |
| `Dockerfile.watchdog` | `deploy/` | `python:3.11-slim` + `watchdog.py` | **ATIVO** |
| `nginx/capture-web.conf` | `deploy/` | Serve a página de captura em 8443 TLS; `getUserMedia` exige contexto seguro | **ATIVO** |
| `certs/` | `deploy/` | CA interna + cert com SAN de IP (`generate-certs.sh`); **gitignored** exceto o gerador | **ATIVO** (artefato local) |
| `wheels/pyds-*.whl` | `deploy/` | Bindings pyds versionados (bom) | **ATIVO** (build) |
| `.env` / `.env.example` | `deploy/` | `.env` gitignored (credencial de câmera); `.env.example` é a única lista de vars obrigatórias | **ATIVO** |
| `mediamtx.yml` | `mediamtx/` | RTSP/RTMP/HLS/WebRTC; TLS obrigatório; paths `live`/`drone`/`phone` | **ATIVO** |

---

## 5. Dados e artefatos — `data/`

| Item | Papel | Versionado? |
|---|---|---|
| `data/logs/live.jsonl` | **Única persistência do sistema**; barramento de estado | Parcial (32 arquivos em `data/`) |
| `data/logs/watchdog.jsonl` | Auditoria do supervisor | — |
| `data/logs/switch.jsonl` | Auditoria de troca de fonte; **sem consumidor em código** | — |
| `data/config/active_source` | Fonte ativa (1 linha) | — |
| `data/config/switch_in_progress` | Marcador de silenciamento do watchdog (JSON com `expires_at`) | efêmero |
| `data/yolo11m.onnx` | Modelo fonte | via Git LFS/ignore |
| `data/model_b1_gpu0_fp16.engine` | **Engine TensorRT exigida em runtime; gitignored** (43,8 MB no host) | **NÃO** |
| `data/parser/libnvdsinfer_custom_impl_Yolo.so` | Parser custom exigido por `infer.txt:15`; não versionado | **NÃO** |
| `data/labels.txt` | Rótulos da classe única (`person`), referenciado por `infer.txt:6` | sim |

---

## 6. Componentes por camada (visão de responsabilidade)

| Camada | Componentes ativos | Acoplamento com outras camadas |
|---|---|---|
| **Mídia / domínio** | `pipeline_real`, `sources`, `source_state`, `area_monitor`, `source_budget`, `source_reconnect`, `bus_classifier`, `graceful_shutdown`, `health_metrics` | Nenhum saída de código; exporta JSONL |
| **Apresentação** | `dashboard/backend/app.py`, `frontend/*` | Lê JSONL (dado), busca vídeo no MediaMTX (protocolo) |
| **Supervisão** | `watchdog.py` | Lê JSONL + `docker.sock` (plano de controle) |
| **Operação** | `switch_source.sh` + `switch_source_lib.py`, `probe/*` | **Importa `deepstream/sources.py`** (direção invertida — ver `dependency-map.md` §4) |
| **Infra** | compose, Dockerfiles, nginx, MediaMTX, certs | Define portas, volumes, TLS, ordem de start |

---

## 7. Números de referência (verificados)

| Métrica | Valor |
|---|---|
| Arquivos rastreados | 130 |
| LOC de `pipeline_real.py` | 1215 (classe única `VisionPipeline`, 31 métodos) |
| LOC de `sources.py` | 646 |
| LOC de `watchdog.py` | 888 |
| LOC de `dashboard/backend/app.py` | 241 |
| LOC de `dashboard/frontend/app.js` | 559 |
| Serviços no compose | 5 |
| Módulos de teste | **13** arquivos / 420 funções `def test_` (11 usam `sys.path.insert`) |
| Tipos de evento no JSONL | **29 distintos** (contagem direta em `data/logs/live.jsonl`) |
| Eventos aceitos pela UI do dashboard | 14 (whitelist fechada) |
| Candidatos a entrypoint | 4 (1 ativo `pipeline_real.py`, 2 legados `pipeline.py`/`pipeline_rtsp_real.py`, 1 wrapper `app.py`) |
| Arquivos de config sem leitor | 4 (`source.yaml`, `osd.yml`, `tracker_iou.ini`, `tracker.yml.legacy`) |

---

## 8. Hipóteses e lacunas do catálogo

**[H]** `pipeline.py`, `pipeline_rtsp_real.py`, `app.py`, `phase1_*` e os 6 `rtsp_*_test.py` são
resíduos de fases anteriores sem uso operacional (evidência: nenhum é referenciado por
Dockerfile/compose/scripts). Não se verificou uso manual em runbooks/histórico.

**[H]** `source.yaml` (INI) é template herdado do `deepstream-app` mantido por inércia.

**[H]** `tracker_iou.ini` foi mantido como "referência canônica" de valores, mas nenhum código o
lê — o risco é edição no arquivo errado.

**[L]** Não há medição de LOC para `switch_source.sh`, `mem_budget.sh` e
`run_phase3_test.sh` (shell).

**[L]** Cobertura real de testes desconhecida — `pytest` não foi executado. Os 420 testes foram
contados estaticamente.
