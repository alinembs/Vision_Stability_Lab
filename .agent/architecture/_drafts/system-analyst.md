> ⚠️ **RASCUNHO SUPERADO.** Este arquivo é insumo de discovery da sessão de 2026-11-01 e
> **não é normativo**. A versão consolidada e revisada está em `.agent/architecture/*.md`.
> Contém pelo menos dois erros conhecidos e corrigidos no consolidado: whitelist de eventos do
> dashboard (aqui: 18 nomes; real: **14**) e `tegrastats_reader` atribuído a `phase5_run.py:109`
> (real: **LEGADO**, importado por `pipeline.py:12` e `pipeline_rtsp_real.py:12`).

# Vision Stability Lab — Discovery da arquitetura ATUAL (rascunho)

> **Status:** rascunho de discovery (`system-analyst`). Descreve apenas o que existe hoje.
> Nenhuma arquitetura-alvo, nenhuma proposta, nenhuma alteração de código.
>
> **Escopo verificado:** branch `feature/multifonte`, 130 arquivos rastreados.
> **Data da análise:** 2026-11-01.
> **Método:** Graphify (`graphify query`/`path`) como primeira camada, seguido de leitura
> direcionada de `deploy/`, `deepstream/`, `dashboard/`, `scripts/` e `mediamtx/`.
> Toda afirmação factual abaixo traz `arquivo:linha`. O que não foi confirmado está em
> **HIPÓTESES**; o que não foi possível determinar está em **LACUNAS**.

---

## 1. System map — fluxo de dados fim a fim

### 1.1 Caminho de vídeo (fonte → inferência → OSD → preview → MediaMTX → browser)

O entrypoint é `deepstream/pipeline_real.py` (`deploy/Dockerfile:29`), classe `VisionPipeline`
(`deepstream/pipeline_real.py:254`). Os elementos são criados no `__init__` e linkados
estaticamente; só o `rtspsrc` entra por `pad-added` (pad dinâmico).

| # | Estágio | Evidência (arquivo:linha) | Observação |
|---|---------|---------------------------|------------|
| 1 | **Resolução da fonte ativa** — `load_sources()` + `resolve_active_source()` | `deepstream/pipeline_real.py:278`; `deepstream/sources.py:458`, `:574` | Precedência: `ACTIVE_SOURCE` > `data/config/active_source` > `default_source` do YAML > 1ª `enabled` (`deepstream/sources.py:572-600`) |
| 2 | **Abertura de sessão** — `begin_session(source.id)` | `deepstream/pipeline_real.py:283`; `deepstream/source_state.py:85` | Gera `session_epoch` derivado de `STARTED_AT` (`deepstream/pipeline_real.py:107`) |
| 3 | **Codec da fonte** — `depay_factory(codec)` → `(depay, parser)` | `deepstream/pipeline_real.py:288`; `deepstream/sources.py:632-646` | `h265 → rtph265depay/h265parse`; `h264 → rtph264depay/h264parse` |
| 4 | **Ingestão RTSP** — `rtspsrc` (`location`, `latency`, `drop-on-latency=True`, `protocols`) | `deepstream/pipeline_real.py:349-352` | `protocols` = máscara de `SourceConfig.protocols` (`deepstream/sources.py:109-111`); YAML declara `transport: tcp` → 4 (`deepstream/config/sources.yaml:24`) |
| 5 | **Link dinâmico do pad de vídeo** | `deepstream/pipeline_real.py:446-453` | `pad-added` liga ao sink pad do depay; emite `rtsp_video_pad` |
| 6 | **Depayload + parse** | `deepstream/pipeline_real.py:353-354`, link `:388` | H.265 na câmera, H.264 em drone/celular (`deepstream/config/sources.yaml:23,46,61`) |
| 7 | **Decode HW** — `nvv4l2decoder` | `deepstream/pipeline_real.py:355`, link `:388-395` | Saída NVMM; decoder → request pad `sink_0` do mux |
| 8 | **Mux** — `nvstreammux(batch_size=1, 1280x720, batched-push-timeout=40000, live-source=True)` | `deepstream/pipeline_real.py:357-358` | `batch=1` é decisão D1 do ADR-0006 (`docs/adr/adr-0006-troca-de-fonte.md:45-49`) |
| 9 | **Inferência** — `nvinfer(config/config/infer.txt)` | `deepstream/pipeline_real.py:361-362`; config `deepstream/config/infer.txt` | YOLO11m ONNX→engine FP16 (`network-mode=2`), `batch-size=1`, `num-detected-classes=1`, parser `NvDsInferParseYolo`, threshold 0.5 |
| 10 | **Tracking** — `nvtracker(IOU)` | `deepstream/pipeline_real.py:364-373`; config `deepstream/config/config_tracker_IOU.yml` | `tracker_width=640, tracker_height=384`, `display_tracking_id=0` |
| 11 | **OSD** — `nvdsosd(display_bbox, display_text, process_mode=1/GPU)` | `deepstream/pipeline_real.py:375-376` | Estilo vem do código, não de `osd.yml` (ver §6) |
| 12 | **Split** — `tee` | `deepstream/pipeline_real.py:377`, links `:400-408` | Um único `tee` com 2 request pads |
| 13a | **Ramo de análise** — `queue` → `fakesink(sync=False, async=False, qos=False)` | `deepstream/pipeline_real.py:379-380`, link `:404-408` | Consome tudo; é onde vivem os probes |
| 14a | **Probe de entrada** (FPS de entrada + budget de reconexão) | `deepstream/pipeline_real.py:423`, callback `:455-465` | No sink pad de `nvinfer` |
| 15a | **Probe de processados** (pyds → `AreaMonitor` → eventos) | `deepstream/pipeline_real.py:425`, callback `:497-549` | Lê `NvDsBatchMeta`, filtra `class_id == 0` (person), chama `monitor.update()` sob `_monitor_lock` |
| 16a | **Regra temporal** — `AreaMonitor` (EMPTY/OCCUPIED, TTL, dwell) | `deepstream/area_monitor.py:11`, `update()` `:24` | TTL default 10 s (`deepstream/pipeline_real.py:133`); emite `track_entered`/`track_exited`/`area_transition` |
| 13b | **Ramo de preview** — `queue(leaky=downstream, max-size-buffers=1)` | `deepstream/pipeline_real.py:681-683`, link ao tee `:736-745` | Descartável por desenho (comentário `:16-21`) |
| 14b | `nvvideoconvert` | `deepstream/pipeline_real.py:684` | |
| 15b | `capsfilter video/x-raw(memory:NVMM),format=NV12` | `deepstream/pipeline_real.py:687-690` | Caps como objeto `Gst.Caps` |
| 16b | `nvv4l2h264enc(bitrate, idrinterval, insert-sps-pps=True)` | `deepstream/pipeline_real.py:691-693` | Defaults: 4 Mbps, IDR a cada 30 (`:142-143`) |
| 17b | `h264parse` | `deepstream/pipeline_real.py:694` | |
| 18b | **Publicação** — `rtspclientsink(location=rtsp://mediamtx:8554/live, protocols=4/TCP)` | `deepstream/pipeline_real.py:696-699` (transporte default `rtsp` = `:138`, URI default = `:139`) | Alternativa `udp`: `rtph264pay` → `udpsink 127.0.0.1:5600` (`:702-707`) |
| 19 | **MediaMTX** — path `live` (RTSP/hls/webrtc) | `mediamtx/mediamtx.yml:37-43`; portas 8554/8888/8889/8189 (`:2-5`, `:10-11`) | `webrtcAdditionalHosts: [192.168.59.102]` (`:18`) |
| 20 | **Browser** — dashboard (player WHEP) + HLS fallback | `dashboard/frontend/app.js:93-100` (WHEP `https://<host>:8889/live/whep`), `:76-77` (sonda 8889 TLS / 8888 HLS) | Fallback `localStorage.whep_url`; WebSocket em `${proto}://${location.host}/ws` (`:217`) |

**Fluxo resumido:**
```
rtspsrc ─▶ [rtph26xdepay/h26xparse] ─▶ nvv4l2decoder ─▶ nvstreammux(batch=1,1280x720)
        ─▶ nvinfer(YOLO11m fp16, person) ─▶ nvtracker(IOU) ─▶ nvdsosd(GPU)
        ─▶ tee ─┬─▶ queue ─▶ fakesink            (análise + probes pyds)
                └─▶ queue(leaky,1) ─▶ nvvideoconvert ─▶ capsfilter(NV12)
                    ─▶ nvv4l2h264enc ─▶ h264parse ─▶ rtspclientsink ─▶ MediaMTX /live
                                                          ─▶ WHEP :8889 / HLS :8888 ─▶ browser
```

### 1.2 Caminho de telemetria (pipeline → JSONL → dashboard → browser)

1. `log_event(event, **fields)` produz uma linha JSON e escreve **duas vezes**: em `stdout`
   (`logging`) e por append em `TELEMETRY_FILE` (`deepstream/pipeline_real.py:204-228`;
   escrita best-effort com `except OSError: pass` em `:224-226`).
2. `TELEMETRY_FILE` default = `<repo>/data/logs/live.jsonl` (`deepstream/pipeline_real.py:141-145`).
3. Namespace da sessão injetado em todo registro por `with_namespace(record, SESSION)`
   (`deepstream/pipeline_real.py:222`; `deepstream/source_state.py:188`).
4. Amostra periódica `interval_metrics` a cada `METRICS_INTERVAL_SECONDS=30`
   (`deepstream/pipeline_real.py:132`, timer `:1119`, emissão `:1047-1085`). Campos:
   `fps_in`, `fps_processed`, `fps_preview`, `preview_dropped_fps`, `persons_detected`,
   `active_tracks`, `area_state`, `entries_interval`, `exits_interval`, `avg_dwell_s`,
   `queue_buffers{queue_analyze,queue_preview}`, `rss_mb`, `memory_growth_mb`, `threads`,
   `open_fds`, `last_frame_age_s`, `session_epoch`.
5. Byte a byte, o arquivo é montado como volume em `../data:/app/data` (RW no pipeline,
   `deploy/docker-compose.yml:114-117`) e `../data:/app/data:ro` no dashboard (`:156-157`).
6. `dashboard/backend/app.py`:
   - `TELEMETRY_FILE` default `/app/data/logs/live.jsonl` (`:31-32`);
   - replay do arquivo existente no startup (`@app.on_event("startup")`, `:180-197`);
   - tailer assíncrono que tolera rotação/truncamento (`tail_telemetry`, `:132-166`), poll 0.5 s (`:36`);
   - `DashboardState.apply()` classifica registros e abre uma "run" a cada `startup` (`:42-84`);
   - ring buffers: `METRICS_HISTORY_MAX=480`, `EVENTS_HISTORY_MAX=500` (`:34-35`);
   - saúde derivada: `down` sem telemetria ou >270 s, `stale` >90 s, `degraded` se `fps_processed<=0` (`:94-106`);
   - HTTP: `/api/snapshot` (`:199`), `/api/history?minutes=N` (`:204`), `/api/events?limit=N` (`:210`), `/healthz` (`:230`), `/` serve `index.html` (`:235`), `/static` monta o frontend (`:239-240`);
   - WS `/ws`: envia snapshot inicial e mantém o cliente em `state.subscribers`, com broadcast por registro (`:169-178`, `:215-228`).
7. Frontend vanilla (`dashboard/frontend/index.html`, `app.js`) consome WS + REST e abre o player
   WHEP direto no MediaMTX — o vídeo **não** passa pelo backend.

**Contrato implícito:** o JSONL é append-only e é a única persistência do sistema
(`docs/architecture.md` "Sem persistência além do JSONL"; confirmado em `dashboard/backend/app.py:1-18`).

---

## 2. Catálogo de componentes

> "Ativo" = está no caminho de execução do container `vision-stability-app`/`vision-dashboard`.
> "Legado" = existe no repositório, é importável, mas nenhum serviço o executa.

### 2.1 Núcleo DeepStream (`deepstream/`)

| Componente | Caminho | Responsabilidade | Entradas | Saídas | Estado mantido | ~LOC | Situação |
|---|---|---|---|---|---|---|---|
| **pipeline_real.py** | `deepstream/pipeline_real.py` | Entrypoint real. Monta/linka o pipeline, probes pyds, telemetria JSONL, preview descartável, reconexão, shutdown | env vars (`RTSP_URI`, `ACTIVE_SOURCE`, `PREVIEW_*`, `SOURCE_*`, `METRICS_INTERVAL_S`, `PIPELINE_STOP_TIMEOUT_S`…), `config/sources.yaml`, `data/config/active_source`, `config/infer.txt`, `config/config_tracker_IOU.yml` | RTSP para MediaMTX/`live`, UDP opcional, JSONL, exit codes 0/1/2 | `VisionPipeline` (pipeline Gst, contadores FPS, `AreaMonitor`, budget/máquina de reconexão, estado do ramo de preview), `SESSION` global | 1215 | **ATIVO** (`deploy/Dockerfile:29`) |
| **sources.py** | `deepstream/sources.py` | Descritor declarativo das fontes + resolução da ativa + fábrica depay/parse + mascaramento de URI. Puro (sem `gi`/`pyds`) | `sources.yaml` ou `SOURCES_CONFIG`, env (`ACTIVE_SOURCE`, `RTSP_URI`, `RTSP_CODEC`, `VSL_SOURCE_SELECTION`, `MTX_HOST`), `data/config/active_source` | `SourceList`/`SourceConfig`, `Tuple(depay, parser)` | Nenhum (funções puras) | 646 | **ATIVO** |
| **source_state.py** | `deepstream/source_state.py` | Sessão de fonte: identidade, `session_epoch` monotônico, namespace da telemetria, reset de contadores de sessão. Puro | `source_id`, `STARTED_AT` | `SessionState`, dict com namespace | Epoch monotônico entre processos (`next_epoch`) | 206 | **ATIVO** |
| **area_monitor.py** | `deepstream/area_monitor.py` | Regra temporal de ocupação: tracks com TTL, EMPTY/OCCUPIED, dwell, eventos limitados | `current_tracks {id: bbox}`, timestamp | `{entered, exited, transition}`, `recent_events` | `tracks`, `area_state`, `total_entries/exits` | 96 | **ATIVO** |
| **source_budget.py** | `deepstream/source_budget.py` | Orçamento de tentativas de reconexão com devolução após estabilidade. Puro | `max_attempts`, `reset_after_s`, timestamps | contagem / booleano de esgotamento | Tentativas + marca de tempo | 63 | **ATIVO** |
| **source_reconnect.py** | `deepstream/source_reconnect.py` | Máquina de estados pura da reconexão do `rtspsrc` (fases stop/wait/backoff/exhausted), fatias com deadline | Resultados de cada fatia | `STEP_STOP/START/RESUMED/RETRY/GIVE_UP` | Fase, tentativa, deadline | 217 | **ATIVO** |
| **bus_classifier.py** | `deepstream/bus_classifier.py` | Classificação pura de mensagens ERROR/EOS do bus: `quit` / `reconnect` / `drop_preview` / `ignore`, ciente dos nomes de elemento do ramo de preview | `src_name`, tipo, detalhe | Ação | Nenhum | 138 | **ATIVO** |
| **graceful_shutdown.py** | `deepstream/graceful_shutdown.py` | `SIGTERM`/`SIGINT` pelo GLib (`unix_signal_add`), fallback `signal.signal` + `idle_add` | Sinais | Chamada de `_request_shutdown` no thread do loop | Handler instalado, sinal recebido | 124 | **ATIVO** |
| **health_metrics.py** | `deepstream/health_metrics.py` | Coleta psutil: RSS/VMS, growth vs warm-up, threads, fds; sistema (RAM, CPU) | — | dict `process`/`system` | `warmup_rss` | 41 | **ATIVO** |
| **rtsp_env.py** | `deepstream/rtsp_env.py` | Lê `RTSP_URI` do ambiente; erro explícito em vez de default com credencial (DIV-001) | `RTSP_URI` | URI ou `RtspUriError` | Nenhum | 56 | **ATIVO** (via fallback de `sources.py`) |
| **tegrastats_reader.py** | `deepstream/tegrastats_reader.py` | Parser de `tegrastats` do Jetson | linhas tegrastats | métricas | Estado de leitura | 27 | **NÃO integrado ao pipeline** (nenhum import em `pipeline_real.py`; usado por `scripts/phase5_run.py:109`) |
| **tracker_state.py** | `deepstream/tracker_state.py` | Primeira versão do estado de tracks (TTL/ocupação) | tracks | estado | tracks | 52 | **LEGADO** — importado só por `pipeline.py`/`pipeline_rtsp_real.py`, substituído por `area_monitor.py` |
| **pipeline.py** | `deepstream/pipeline.py` | Pipeline sintético sem RTSP real; "simulação de contagem para validação do fluxo do probe" (comentário `:20-22`) | — | log | — | 112 | **LEGADO/EXPERIMENTAL** |
| **pipeline_rtsp_real.py** | `deepstream/pipeline_rtsp_real.py` | Versão anterior com câmera real (usa `require_rtsp_uri`) | `RTSP_URI` | log | — | 107 | **LEGADO** |
| **app.py** | `deepstream/app.py` | Wrapper que chama `pipeline.main()` | — | — | — | 19 | **LEGADO** (não é o CMD) |
| **rtsp_\*.py / phase1_\*.py** | `rtsp_auto_test.py`, `rtsp_bin_*.py`, `rtsp_h265_test.py`, `rtsp_nvmm_test.py`, `rtsp_sig_test.py`, `phase1_test.py`, `phase1_local_test.py` | Sondas de bancada das fases 1–3 | `RTSP_URI` | stdout | — | 50–91 cada | **BANCADA/LEGADO** |
| **config/** | `deepstream/config/` | `sources.yaml` (ativo), `infer.txt` (ativo), `config_tracker_IOU.yml` (ativo). `source.yaml`, `osd.yml`, `tracker_iou.ini`, `tracker.yml.legacy` **não são lidos por nenhum `.py`** (grep de referências retornou vazio) | — | — | — | — | 3 ativos / 4 legado-referência |
| **tests/** | `deepstream/tests/` | 10 módulos pytest dos componentes puros + wiring do pipeline (`test_pipeline_source_wiring.py`, `test_no_hardcoded_secrets.py`, …) | — | — | — | — | Ativo em CI/teste, fora do runtime |

### 2.2 Dashboard (`dashboard/`)

| Componente | Caminho | Responsabilidade | Entradas | Saídas | Estado | ~LOC | Situação |
|---|---|---|---|---|---|---|---|
| **backend/app.py** | `dashboard/backend/app.py` | API REST + WS + tailer do JSONL + saúde derivada | `data/logs/live.jsonl` (ro) | `/api/*`, `/ws`, `/healthz`, `/`, `/static` | `DashboardState`: 480 métricas, 500 eventos, `current_run`, `subscribers` | 241 | **ATIVO** |
| **frontend/index.html** | `dashboard/frontend/index.html` | Shell: lâmpadas (pipeline/vídeo/memória/telemetria), `<video>`, readouts, 2 canvases de gráfico, painel de saúde, log de eventos | WS/REST | DOM | — | 115 | **ATIVO** |
| **frontend/app.js** | `dashboard/frontend/app.js` | Player WHEP (com sonda TLS 8889→HLS 8888), gráficos canvas, WebSocket, atualização de cards | `/api/*`, `/ws`, MediaMTX 8889/8888 | DOM | `localStorage.whep_url` | 559 | **ATIVO** |
| **frontend/capture.html + capture.js + codec-gate.js** | `dashboard/frontend/` | Página de captura do celular: `getUserMedia` + POST WHIP em `https://<lan>:8889/phone/whip` | câmera do celular | stream WHIP | sessão WHIP | 179+696+429 | **ATIVO** (servido pelo nginx em 8443) |
| **codec-gate-selftest.html** | `dashboard/frontend/` | Autoteste do gate de codec | — | — | — | 166 | BANCADA |
| **style.css** | `dashboard/frontend/style.css` | Estilo | — | — | — | 314 | ATIVO |
| **Dockerfile** | `dashboard/Dockerfile` | `python:3.11-slim` + uvicorn na 8000 | — | — | — | 11 | ATIVO |

### 2.3 Watchdog e scripts (`scripts/`)

| Componente | Caminho | Responsabilidade | Entradas | Saídas | Estado | ~LOC | Situação |
|---|---|---|---|---|---|---|---|
| **watchdog.py** | `scripts/watchdog.py` | Watchdog **externo**: lê JSONL, `/proc/meminfo`, logs do container e o `docker.sock`; reinicia o container. Regras 1–5 + crash-loop + `container_missing` + marcador de troca (`:1-45`) | `/app/data/logs/live.jsonl`, `/proc/meminfo`, `docker.sock`, `switch_in_progress` | `data/logs/watchdog.jsonl`, `POST /containers/<id>/restart` (`:509-533`) | Janelas de baseline RSS, FPS, RAM, restarts, cooldowns | 888 | **ATIVO** (container `vision-watchdog`) |
| **switch_source.sh** | `scripts/switch_source.sh` | Troca de fonte em 7 passos: valida id → guard de precedência → pré-check RTSP → grava `active_source` + marcador → recria container → espera `interval_metrics` do processo novo → rollback se falhar (`:1-49`) | id da fonte, `deploy/.env`, `sources.yaml` | `data/config/active_source`, `data/logs/switch.jsonl`, exit 0–4 | Rollback guardado localmente | ~? (não medido) | **ATIVO** (operacional, fora do runtime) |
| **switch_source_lib.py** | `scripts/switch_source_lib.py` | Lógica pura e testável do script (parse de env, `resolve_source`, `precheck_url`, `wait_for_source`, `decide_rollback`, `main`) | — | exit code / relatório | `WaitOutcome`, `RollbackDecision` | 805 | ATIVO (invocado pelo `.sh`) |
| **phase5_run.py** | `scripts/phase5_run.py` | Bancada da Fase 5: captura janelas de JSONL + tegrastats + erros do container, gera sumário | JSONL, tegrastats | relatório | — | 371 | BANCADA |
| **mem_budget.sh** | `scripts/mem_budget.sh` | Amostra de orçamento de memória (RSS, RAM %, LFB, OOM) | docker/meminfo/tegrastats | JSON por linha | — | — | BANCADA/observabilidade |
| **run_phase3_test.sh** | `scripts/run_phase3_test.sh` | Executa a rodada de 30 min da Fase 3 | — | logs | — | — | BANCADA |
| **probe/rtsp_read_probe.py** | `scripts/probe/` | Sonda RTSP de aceitação (mede fps/gaps, decide PASS/FAIL, mascara credencial) | URI RTSP | JSON/relatório, exit code | — | ~760 | ATIVO (usado pelo pré-check da troca) |
| **probe/whip_probe.py** | `scripts/probe/` | Cliente WHIP (offer/answer, TLS, DELETE de sessão) | URL WHIP | JSON, exit code | — | ~330 | ATIVO (aceitação de celular) |
| **probe/rtmp_publisher.sh** | `scripts/probe/` | Publisher RTMP para o path `drone` | — | stream | — | — | BANCADA |
| **probe/ingest_report.py / ingest_report_lib.py** | `scripts/probe/` | Relatório de ingestão (latência/gaps) | — | JSON | — | — | BANCADA |
| **tests/** | `scripts/tests/` | Testes de `switch_source_lib` e dos probes | — | — | — | — | TESTE |

### 2.4 Qual `pipeline*.py` é o entrypoint real? (prova)

| Candidato | Evidência | Veredito |
|---|---|---|
| `deepstream/pipeline_real.py` | `deploy/Dockerfile:29` → `CMD ["python3", "deepstream/pipeline_real.py"]`; único `CMD` do serviço que roda DeepStream (`deploy/docker-compose.yml:60-127`) | **ENTRYPOINT REAL** |
| `deepstream/pipeline.py` | Nenhuma referência em Dockerfile/compose/scripts; único importador é `deepstream/app.py:3` (`from pipeline import main as pipeline_main`), e `app.py` não é CMD de nada. Cabeçalho descreve probe "simulado" (`:20-22`) | Legado/experimental |
| `deepstream/pipeline_rtsp_real.py` | Nenhuma referência em Dockerfile/compose; importa `rtsp_env.require_rtsp_uri` (`:13`), que existe para "entrypoints de bancada" (`deepstream/rtsp_env.py:47-55`) | Legado |
| `deepstream/app.py` | Não é CMD; apenas delega a `pipeline.main()` | Legado |

Corroboração: `data/logs/live.jsonl` (9.658 linhas, última escrita 2026-11-01 12:26 UTC) contém os
eventos que **só** existem em `pipeline_real.py` (`interval_metrics` 5.565, `preview_branch_ready` 301,
`source_reconnect_action` 208, `source_active` 157, `shutdown_requested` 71).

---

## 3. Processos e containers em runtime

### 3.1 Serviços declarados (`deploy/docker-compose.yml`)

Todo o stack é **um único compose**, sem `networks:` explícita → rede bridge default do projeto
(`deploy-vision-*`, visível em `docker ps`). Não há `version:`, não há profiles.

| Serviço (linha) | Imagem / build | Container | Portas publicadas | Volumes | `depends_on` | Healthcheck | `restart` |
|---|---|---|---|---|---|---|---|
| `mediamtx` (`:2`) | `bluenviron/mediamtx:1.21.3` (pinado; `:2-5`) | `vision-mediamtx` | `${LAN_IP:-192.168.59.102}`: 8554/tcp, 1935/tcp, 8888/tcp, 8889/tcp, 8890/udp, 8189/udp, 8189/tcp (`:15-21`) | `../mediamtx/mediamtx.yml:/mediamtx.yml`, `../deploy/certs:/certs:ro` (`:22-29`) | — | — | `unless-stopped` |
| `capture-web` (`:31`) | `nginx:1.27-alpine` | `vision-capture-web` | `${LAN_IP}:8443:8443` (`:42`) | `nginx/capture-web.conf`, `../dashboard/frontend` (ro), `../deploy/certs` (ro) (`:43-48`) | — | `wget https://127.0.0.1:8443/capture.html` 60 s (`:49-56`) | `unless-stopped` |
| `vision-app` (`:60`) | build `deploy/Dockerfile` (base `nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch`, `deploy/Dockerfile:1`) | `vision-stability-app` | **nenhuma** (só consome a rede do compose) | `../deepstream:/app/deepstream`, `../data:/app/data`, `../scripts:/app/scripts` (`:114-117`) | `mediamtx` (`:118-119`) | mtime de `/app/data/logs/live.jsonl` < 180 s, 60 s, `start_period 180 s` (`:120-127`) | `unless-stopped` |
| `watchdog` (`:128`) | build `deploy/Dockerfile.watchdog` (`python:3.11-slim` + `scripts/watchdog.py`) | `vision-watchdog` | — | `../data:/app/data`, `/var/run/docker.sock` (`:139-141`) | `vision-app` (`:142-143`) | — | `unless-stopped` |
| `dashboard` (`:145`) | build `dashboard/Dockerfile` | `vision-dashboard` | `${DASHBOARD_PORT:-8081}:8000` (`:153`) | `../data:/app/data:ro`, `../dashboard:/app/dashboard` (`:156-157`) | — | — | `unless-stopped` |

Privilégios do pipeline: `init: true` (tini como PID 1, `:68-74`), `privileged: true`, `runtime: nvidia` (`:75-76`).

### 3.2 Variáveis de ambiente que definem comportamento (`vision-app`)

`RTSP_URI`, `RTSP_CODEC` (fallback legado), `ACTIVE_SOURCE`, `SOURCES_CONFIG`, `VSL_SOURCE_SELECTION`,
`MTX_HOST`, `LOG_LEVEL`, `PREVIEW_ENABLED`, `PREVIEW_TRANSPORT`, `PREVIEW_MTX_URI`, `PREVIEW_HOST`,
`PREVIEW_PORT`, `PREVIEW_BITRATE`, `PREVIEW_IDR_GOP`, `SOURCE_RECONNECT_MAX_ATTEMPTS`,
`SOURCE_RECONNECT_RESET_S`, `SOURCE_FRAME_TIMEOUT_S`, `SOURCE_RETRY_S`, `PIPELINE_STOP_TIMEOUT_S`
(`deploy/docker-compose.yml:77-112`). O código acrescenta outros com default próprio:
`METRICS_INTERVAL_S`, `TRACK_TTL_S`, `MAX_EVENTS`, `TEST_DURATION_S`, `PREVIEW_RETRY_S`,
`PREVIEW_VERIFY_S`, `PREVIEW_MAX_ATTEMPTS`, `SOURCE_STOP_TIMEOUT_S`, `SOURCE_RETRY_MAX_S`,
`SOURCE_RECONNECT_STEP_MS` (`deepstream/pipeline_real.py:126-199`).

Watchdog: `APP_CONTAINER`, `TELEMETRY_FILE`, `WATCHDOG_LOG`, `TARGET_FPS` (`:132-137`) e, no código,
`FRAME_TIMEOUT_S=60`, `NO_FRAME_ESCALATE_S=180`, `CRASH_LOOP_*`, `FPS_DEGRADE_RATIO=0.30`,
`RSS_GROWTH_RATIO=0.15`, `RAM_CRITICAL_PCT=90`, `CHECK_INTERVAL_S=10`, `WATCHDOG_GRACE_S=120`
(`scripts/watchdog.py:70-108`).

### 3.3 MediaMTX (`mediamtx/mediamtx.yml`)

- Protocolos: RTSP `:8554`, RTMP `:1935`, HLS `:8888`, WebRTC `:8889` (`:2-5`).
- ICE: `webrtcLocalUDPAddress: :8189` e `webrtcLocalTCPAddress: :8189` (`:10-11`);
  `webrtcIPsFromInterfaces: no` e `webrtcAdditionalHosts: [192.168.59.102]` (`:15`, `:18`).
- `rtspTransports: [tcp]` (`:22`) casa com `protocols=4` do `rtspclientsink` (`deepstream/pipeline_real.py:699`).
- TLS obrigatório no WHIP: `webrtcEncryption: yes`, cert/chave em `/certs` (`:31-33`);
  material em `deploy/certs/` (presente: `ca.crt`, `server.crt`, `server.key`, `generate-certs.sh`).
- `api: no` (`:35`). Paths: `live` (preview), `drone` (RTMP), `phone` (WHIP) (`:37-43`).
- SRT/MoQ escutam em 8890/8892/8893 segundo o log do container; só 8890/udp é publicado.

### 3.4 Estado observado no host (2026-11-01, `docker ps -a`)

| Container | Status observado |
|---|---|
| `vision-stability-app` | `Up 10 minutes (unhealthy)` |
| `vision-watchdog` | `Up About an hour` |
| `vision-dashboard` | `Up About an hour`, `0.0.0.0:8081->8000/tcp` |
| `vision-mediamtx` | **`Exited (255) About an hour ago`** |
| `vision-capture-web` | **`Exited (255) About an hour ago`** |

Consequência direta, com evidência: com o MediaMTX fora, o ramo de preview falha na publicação —
`data/logs/live.jsonl` (últimas linhas):
`{"event":"preview_branch_failed","element":"rtspclientsink0","error":"gst-resource-error-quark:
Could not open resource for reading and writing. (7)","action":"ramo de preview derrubado; análise segue"}`.
Isso exercita exatamente o caminho `ACTION_DROP_PREVIEW` de `deepstream/bus_classifier.py`
(`deepstream/pipeline_real.py:648-653`, `:789-831`) — a análise continua, o preview é retentado com backoff.
Também por isso `fps_processed` aparece `0.0` nessa amostra (a captura já havia caído quando o log foi escrito).

---

## 4. Subsistemas transversais

### 4.1 Troca de fonte (ADR-0006: três fontes, uma ativa)

- **Declaração:** `deepstream/config/sources.yaml` — `camera` (H.265, `uri_env: RTSP_URI`, gate p95 300 ms),
  `drone` (H.264, `rtsp://${MTX_HOST:-mediamtx}:8554/drone`, gate `report_only`), `phone` (H.264, `.../phone`,
  gate p95 300 ms); `default_source: camera` (`:13-56`). Máximo de fontes é validado
  (`deepstream/sources.py:458-480`, mensagem "máximo N fontes … UMA fonte ativa por vez").
- **Precedência:** `ACTIVE_SOURCE` (env) > `data/config/active_source` > `default_source` > 1ª `enabled`
  (`deepstream/sources.py:572-600`). Id inválido/desabilitado ou arquivo ilegível é **erro**, nunca fallback
  (`:537-571`, `:600+`).
- **Rollback nível 1:** `VSL_SOURCE_SELECTION=off` força o caminho legado `RTSP_URI`+`RTSP_CODEC`
  (`deepstream/sources.py:276-278`, `:471-481`; `deploy/.env.example:36-41`).
- **Troca = restart de processo, nunca reconfiguração em runtime** (D4): `scripts/switch_source.sh:1-49`.
  O estado de visão não atravessa fontes; o isolamento é por namespace `source_id` + `session_epoch`
  (`deepstream/source_state.py:1-30`).
- **Ponto de conexão com o watchdog:** o marcador `data/config/switch_in_progress` silencia Regra 1 e
  crash-loop durante a recriação (`scripts/watchdog.py:12-19`, `:755-790`).

### 4.2 Reconexão da fonte (dentro do processo)

- O bus **só decide e registra**; nenhum trabalho de GStreamer no callback (`deepstream/pipeline_real.py:607-660`;
  racional em `deepstream/source_reconnect.py:1-35`).
- `ACTION_RECONNECT` → `_on_source_failure()` (`:870-900`) → agenda `_request_source_reconnect`.
- Execução em fatias por timer GLib de 250 ms: `_reconnect_step` (`:901-938`) chamando
  `_run_reconnect_action` (`:939-971`) com `STEP_STOP`/`STEP_START`; a máquina pura decide fase, backoff
  exponencial (10 → 45 s) e desfecho (`deepstream/source_reconnect.py:36-50`; budget em `source_budget.py`).
- EOS da fonte é **contido no tee** por pad probe (`deepstream/pipeline_real.py:430-433`, `:471-496`):
  os dois ramos continuam vivos e voltam a receber buffers.
- `_check_rtsp_alive` (timer de 5 s, `:1120`, implementação `:999-1015`) cobre o caso "nenhum frame por
  `RTSP_IDLE_RECONNECT_S`=30 s" (`:152`).
- Desfecho: `STEP_RESUMED` → `rtsp_reconnect_resumed` (`:956`); `STEP_GIVE_UP` → `EXIT_SOURCE_LOST=2`
  (`:201`, `:966-967`).
- Observado em produção: 208 `source_reconnect_action`, 121 `rtsp_reconnect_started`, 7 `rtsp_reconnect_resumed`
  em `data/logs/live.jsonl`.

### 4.3 Reconexão do preview (ramo descartável)

- `_make_preview_elements` **recria** do zero a cada tentativa, por decisão explícita
  (`deepstream/pipeline_real.py:669-678`): um `rtspclientsink` que errou não volta a publicar num ciclo
  NULL→PLAYING.
- `_detach_preview_branch` (`:745`), `_preview_retry_delay` (`:772`), `_schedule_preview_retry` (`:776`),
  `_drop_preview_branch` (`:789`), `_retry_preview_branch` (`:813`), `_check_preview_alive` (`:832-869`).
- Backoff 15 s → 300 s, teto de 6 tentativas (`:185-196`); restauração exige ≥5 frames de preview
  (`PREVIEW_VERIFY_MIN_FRAMES`, `:192-194`) e `preview_branch_restored` (`:849`).
- Eventos: `preview_branch_failed`, `preview_branch_error`, `preview_branch_retry_scheduled`,
  `preview_branch_retry`, `preview_branch_retry_failed`, `preview_branch_restored`, `preview_branch_unavailable`
  (`:779-857`) — todos presentes em `live.jsonl`, exceto `preview_branch_unavailable`.

### 4.4 Watchdog (processo externo)

Regras e escalonamento (`scripts/watchdog.py:8-24`, implementação `:791-851`):

| Regra | Gatilho | Ação | Evidência |
|---|---|---|---|
| 1 | 60 s sem frame (`FRAME_TIMEOUT_S`) → `no_frames_detected`; 180 s (`NO_FRAME_ESCALATE_S`) → restart | restart do container | `:70-76`, `:791-823` |
| — | crash-loop: ≥5 restarts em 900 s **com** corroboração de fonte morta | restart escalado | `:78-90`, `:700-754` |
| 2 | FPS < 30 % do alvo por 120 s | registra degradação (sem restart) | `:95-97`, `_handle_metric:534` |
| 3 | RSS > 15 % acima da baseline (após 300 s de carência) | alerta | `:99-101`, `:824-838` |
| 4 | RAM do sistema > 90 % por 3 confirmações | `docker restart` | `:103-105`, `:824-838` |
| 5 | Erro CUDA fatal nos logs (`CUDA_FATAL_RE`) | reinicia o processo/container | `:110-115`, `:839-851` |
| extra | `container_missing` (404 da API Docker) | registra, **não** escala restart | `:21-24`, `:645-666` |

Acesso ao Docker é feito por socket cru com HTTP em `/var/run/docker.sock` (`DockerAPI`, `:154-255`),
sem SDK. Saída: `data/logs/watchdog.jsonl` (55 KB observados).

### 4.5 Telemetria e eventos

Eventos emitidos por `pipeline_real.py` (grep de `log_event("`): `startup` (`:296`), `preview_branch_ready`
(`:413`), `rtsp_video_pad` (`:451`), `rtsp_link_error` (`:453`), `source_reconnect_budget_reset` (`:462`),
`eos` (`:492`, `:629`), `track_exited` (`:535`), `track_entered` (`:538`), `area_transition` (`:540`),
`shutdown_requested` (`:595`), `pipeline_failed` (`:603`), `pipeline_warning` (`:624`),
`rtsp_reconnect_error` (`:649`), `preview_branch_error` (`:652`), `pipeline_error` (`:655`),
`preview_branch_*` (`:779-857`), `rtsp_reconnect_started` (`:888`, `:897`, `:961`),
`source_reconnect_step_note` (`:921`), `source_reconnect_action` (`:944`, `:949`),
`rtsp_reconnect_resumed` (`:956`), `interval_metrics` (`:1047`), `source_active` (`:1100`),
`test_started` (`:1116`), `pipeline_playing` (`:1129`), `shutdown_before_loop` (`:1135`),
`interrupted_by_user` (`:1140`), `pipeline_stopped` (`:1148`), `pipeline_stop_timeout` (`:1177`),
`startup_failed` (`:1203`), `process_exit` (`:1210`), `shutdown_before_startup` (`:62`).

Distribuição real (`data/logs/live.jsonl`, contagem por evento): `interval_metrics` 5565,
`area_transition` 683, `track_entered` 475, `track_exited` 450, `pipeline_error` 394, `startup` 315,
`preview_branch_ready` 301, `pipeline_playing` 301, `pipeline_stopped` 241, `source_reconnect_action` 208,
`source_active` 157, `rtsp_video_pad` 134, `rtsp_reconnect_started` 121, `shutdown_requested` 71,
`pipeline_failed` 47, `process_exit` 47, `startup_failed` 27, `eos` 19, `preview_branch_failed` 13,
`preview_branch_retry_scheduled` 13, `preview_branch_retry` 11, `preview_branch_error` 8,
`preview_branch_retry_failed` 7, `rtsp_reconnect_resumed` 7.

O dashboard só empurra para a lista os eventos de uma whitelist (`dashboard/backend/app.py:63-79`):
`track_entered`, `track_exited`, `area_transition`, `pipeline_error`, `pipeline_warning`, `eos`,
`interrupted_by_user`, `test_duration_reached`, `pipeline_stopped`, `pipeline_playing`, `source_active`,
`preview_branch_ready`, `rtsp_reconnect_error`, `preview_branch_error`. `error_count` conta apenas
`pipeline_error` da run atual (`:110-118`).

### 4.6 Shutdown gracioso

1. `_boot_stop` instalado **antes** de `gi`/`pyds`/TensorRT, para cobrir a janela de boot
   (`deepstream/pipeline_real.py:48-72`, instalação `:73`).
2. `GracefulShutdown.install()` no início do `__init__` (`:268`), trocando o handler anterior.
   Sinais entram pelo GLib (`GLib.unix_signal_add`), nunca por trabalho de GStreamer no handler
   (`deepstream/graceful_shutdown.py:1-24`).
3. `_request_shutdown` (`:579-598`) emite `shutdown_requested` e sai do loop; `run()` faz
   `finally` → métricas finais → `_teardown()` → `pipeline_stopped exit_code` (`:1130-1152`).
4. `_teardown` roda `set_state(NULL)` numa thread daemon com teto `PIPELINE_STOP_TIMEOUT_S`=1.5 s; se
   estourar, emite `pipeline_stop_timeout` e o processo sai mesmo assim, evitando exit 137 (`:1153-1181`).
5. Exit codes: `0` limpo, `1` erro de pipeline, `2` fonte perdida (`:199-201`).
6. `init: true` no compose garante que o `SIGTERM` chegue ao processo e não seja ignorado pelo kernel
   como PID 1 (`deploy/docker-compose.yml:66-74`).

### 4.7 Como os transversais se conectam

```
switch_source.sh ──grava──▶ data/config/active_source ──lida no boot──▶ sources.resolve_active_source()
        │                                │                                        │
        └─grava marcador switch_in_progress ──▶ watchdog (silencia R1/crash-loop)  ▼
                                                                    VisionPipeline.__init__
                                                                              │
   begin_session(source_id) ◀── source_state.SessionState ◀───────────────────┘
              │                                    │
              ▼                                    ▼
   log_event() ──▶ live.jsonl ──▶ dashboard tailer ──▶ /api/* + /ws ──▶ browser
              │                └─▶ watchdog tailer ──▶ restart / alerta
              ▼
   bus_classifier ──▶ ACTION_RECONNECT ──▶ source_reconnect (máquina) ──▶ _reconnect_step (timer 250 ms)
                 └─▶ ACTION_DROP_PREVIEW ──▶ _retry_preview_branch (backoff 15→300 s)
   SIGTERM ──▶ graceful_shutdown (GLib) ──▶ _request_shutdown ──▶ _teardown (teto 1,5 s)
```

---

## 5. FATOS, HIPÓTESES e LACUNAS

### 5.1 FATOS (com evidência)

1. **O entrypoint de produção é `deepstream/pipeline_real.py`** — `deploy/Dockerfile:29`
   (`CMD ["python3", "deepstream/pipeline_real.py"]`).
2. **`pipeline.py`, `pipeline_rtsp_real.py` e `app.py` são legado** — nenhuma referência em
   Dockerfile/compose/scripts; `app.py` só importa `pipeline.main` (`deepstream/app.py:3`).
3. **Há 5 serviços no compose**: `mediamtx`, `capture-web`, `vision-app`, `watchdog`, `dashboard`
   (`deploy/docker-compose.yml:2, 31, 60, 128, 145`).
4. **Todas as portas do MediaMTX e a 8443 são publicadas amarradas a `${LAN_IP}`**, nunca em `0.0.0.0`
   (`deploy/docker-compose.yml:15-21`, `:42`); só o dashboard publica em `0.0.0.0` (`:153`).
5. **O slot do DeepStream é único e `batch_size=1`** (`deepstream/pipeline_real.py:357`; `infer.txt`
   `batch-size=1`), coerente com D1 do ADR-0006.
6. **O preview é o único caminho de vídeo para o browser** — a análise termina em `fakesink`
   (`deepstream/pipeline_real.py:380`).
7. **O EOS da fonte é interceptado no tee** para não matar o pipeline (`:430-433`, `:471-496`).
8. **Nenhum trabalho de GStreamer ocorre no callback do bus** — apenas classificação e agendamento
   (`:607-660`; `deepstream/source_reconnect.py:1-35`).
9. **A troca de fonte reinicia o processo** e é provada por `session_epoch` vs `StartedAt`
   (`scripts/switch_source.sh:20-27`; `deepstream/source_state.py:1-30`).
10. **O watchdog roda fora do processo**, com acesso ao `docker.sock`, e escalona restart
    (`scripts/watchdog.py:1-24`, `:139-141` do compose, `DockerAPI` `:154`).
11. **Telemetria é append-only em JSONL**, sem banco; o dashboard reconstrói histórico por replay
    (`dashboard/backend/app.py:1-18`, `:180-197`).
12. **A troca de codec é por fonte** via `depay_factory` (`deepstream/sources.py:632-646`), não por env do
    container (o env `RTSP_CODEC` só sobrevive no caminho legado).
13. **Nenhum consumidor de MQTT existe no código** — `requirements.txt:5-6` instala `mqtt`/`paho-mqtt`, e o
    grep por `mqtt`/`paho` em `.py/.js/.html` não retorna nada.
14. **`config/source.yaml`, `config/osd.yml`, `config/tracker_iou.ini`, `config/tracker.yml.legacy` não são
    lidos por nenhum `.py`** (grep de referências vazio); `tracker_iou.ini` se autodeclara "referência
    canônica" não consumida (`deepstream/config/tracker_iou.ini:1-5`).

### 5.2 HIPÓTESES (plausíveis, não confirmadas)

1. **Os containers `vision-mediamtx` e `vision-capture-web` em `Exited (255)` são a causa do
   `preview_branch_failed` observado.** A correlação é forte (erro `Could not open resource` do
   `rtspclientsink0` + MediaMTX parado), mas **não** inspecionei o motivo da saída 255 nem a ordem temporal
   exata dos dois eventos a partir de `docker inspect`.
2. **`vision-stability-app` está `unhealthy` por falta de escrita recente em `live.jsonl`**, dado o
   healthcheck por mtime (`deploy/docker-compose.yml:120-127`). O último registro que li é de
   12:26 UTC e o container foi reiniciado há ~10 min; não medi o intervalo com precisão.
3. **`dashboard` não tem `depends_on`** e por isso pode subir antes do arquivo existir; o tailer parece
   tolerar isso (espera o arquivo, `dashboard/backend/app.py:137-141`), mas não executei o cenário.
4. **`requirements.txt` instalado na imagem DeepStream** inclui `fastapi`, `uvicorn` e `pydantic`
   (`requirements.txt:7-8`, `deploy/Dockerfile:18-19`), que aparentemente não são usados dentro do container
   do pipeline. Não confirmei se algum script importa esses pacotes de dentro do `vision-app`.
5. **SRT/MoQ do MediaMTX** estão ativos internamente (log do container cita `:8890`, `:8892`, `:8893`), mas
   só 8890/udp é publicado; 8892/8893 não têm porta publicada — não verifiquei se isso é intencional.
6. **`PREVIEW_TRANSPORT=udp`** continua funcional (`deepstream/pipeline_real.py:702-707`), mas o MediaMTX
   não ingere RTP/UDP puro (afirmação de `docs/architecture.md`, seção "Observações"); não testei.

### 5.3 LACUNAS (o que não consegui determinar e por quê)

1. **Latência real fim a fim** — não há instrumentação de timestamp por frame no caminho; o único gate
   declarado é `gate.latency_p95_ms` no YAML (`deepstream/config/sources.yaml:34-35`, `:52-54`, `:67-68`), sem código que o
   consuma no pipeline. Não localizei o componente que avalia o gate.
2. **Quem consome `data/logs/switch.jsonl` e `drone-campo-ingest.jsonl`** — os arquivos existem
   (`data/logs/`), mas não encontrei leitor no código além do próprio `switch_source.sh`.
3. **Rede Docker nominal e IPs** — o compose não declara `networks:`; os nomes vistos em `docker ps`
   (`deploy-*`) vêm da bridge default. Não inspecionei `docker network inspect` para confirmar subnets.
4. **Conteúdo de `docs/plans/` e `docs/runbooks/`** — listei os diretórios mas não li os planos;
   referências a "E0…E6" acima vêm de comentários no código, não do plano.
5. **Uso de `tegrastats_reader.py` em produção** — o parser existe e é usado por `scripts/phase5_run.py:109`,
   mas nenhum import em `pipeline_real.py`; não sei se a leitura de `lfb` do `mem_budget.sh` é o caminho
   oficial em campo.
6. **Tamanho/uso do `data/model_b1_gpu0_fp16.engine`** — 43,8 MB presentes; não verifiquei se o engine foi
   reconstruído após o último `.onnx` (não há hash/timestamp comparativo documentado aqui).
7. **Cobertura de testes executada** — há 10+4 módulos de teste, mas não rodei `pytest`; não afirmo que
   passam.
8. **Multi-arch/build reprodutível** — a imagem base é `deepstream-l4t:7.1-triton-multiarch` e o wheel
   `pyds-1.2.0-cp310-linux_aarch64` está versionado em `deploy/wheels/`; não validei em outra máquina.

---

## 6. Divergências entre `docs/architecture.md` e o código

`docs/architecture.md` está datado de **2026-08-23** e se autodeclara "Fase 4 concluída". O código atual
incorpora Fase 5/6 e toda a onda multifonte (ADR-0006, 2026-10-30). Divergências concretas:

| # | `docs/architecture.md` diz | Código atual | Evidência |
|---|---|---|---|
| D1 | `rtspsrc (…, protocols=tcp+udp)` | `protocols` vem da **fonte** (`SourceConfig.protocols`; `transport: tcp` → 4) | `deepstream/pipeline_real.py:352`; `deepstream/sources.py:109-111`; `deepstream/config/sources.yaml:24` |
| D2 | "alternável para h264 via `RTSP_CODEC`" | Codec é **da fonte** (`self.source.codec` + `depay_factory`); `RTSP_CODEC` sobrevive só no fallback legado | `deepstream/pipeline_real.py:287-288`; `deepstream/sources.py:632-646, 276-278` |
| D3 | Lista de componentes: `tracker_state.py`/`area_monitor.py` como par | `tracker_state.py` é **legado** (importado só por `pipeline.py`/`pipeline_rtsp_real.py`); o runtime usa apenas `AreaMonitor` | `deepstream/pipeline_real.py:83`; `deepstream/tracker_state.py` sem importador ativo |
| D4 | "Contêineres: `vision-app`, `mediamtx`, `dashboard`" (3) | São **5** serviços: + `capture-web` (nginx 8443) + `watchdog` | `deploy/docker-compose.yml:2, 31, 60, 128, 145` |
| D5 | Arquitetura não menciona reconexão da fonte, orçamento de tentativas, `exit 2`, nem reconexão do preview | Existem `source_reconnect.py`, `source_budget.py`, `bus_classifier.py`, `graceful_shutdown.py`, com máquina de estados e backoff | `deepstream/source_reconnect.py`, `source_budget.py`, `bus_classifier.py`, `graceful_shutdown.py` |
| D6 | Arquitetura não menciona **troca de fonte**, `sources.yaml`, `active_source` nem `session_epoch` | `sources.py` + `source_state.py` + `scripts/switch_source.sh`; `sources.yaml` com 3 fontes | `deepstream/config/sources.yaml:1-56`; `deepstream/source_state.py:85`; `scripts/switch_source.sh:1-49` |
| D7 | Arquitetura não menciona **watchdog** | `scripts/watchdog.py` (888 linhas, 5 regras + crash-loop) como container próprio | `deploy/docker-compose.yml:128-144` |
| D8 | "`tegrastats_reader.py` — stats nativas do Jetson (uso planejado)" | Continua **não integrado** ao pipeline (só `scripts/phase5_run.py`) | grep sem import em `pipeline_real.py`; `scripts/phase5_run.py:109` |
| D9 | Diagrama de telemetria não cita `run`/`current_run`, nem a whitelist de eventos | Backend particiona por "run" e filtra eventos | `dashboard/backend/app.py:42-84` |
| D10 | Não menciona TLS obrigatório na 8889 nem a página de captura 8443 no diagrama de componentes (só no texto) | `capture-web` é serviço próprio; `webrtcEncryption: yes` | `deploy/docker-compose.yml:31-58`; `mediamtx/mediamtx.yml:31-33` |

O que **está** correto no documento: a cadeia GStreamer do §1.1 (nomes e ordem dos elementos), `batch=1`,
`1280x720`, `nvdsosd process-mode=1`, o `tee` com ramo leaky, o `rtspclientsink` como default de
`PREVIEW_TRANSPORT`, o JSONL em `data/logs/live.jsonl`, os ring buffers 480/500, as rotas REST/WS, as
portas 8081/8888/8889/8443 e a ausência de persistência além do JSONL.

---

## 7. Resumo executivo do discovery

- **Entrypoint real:** `python3 deepstream/pipeline_real.py` (`deploy/Dockerfile:29`); `pipeline.py`,
  `pipeline_rtsp_real.py` e `app.py` são legado.
- **Runtime:** 5 containers (`vision-mediamtx`, `vision-capture-web`, `vision-stability-app`,
  `vision-watchdog`, `vision-dashboard`) numa única bridge; só o dashboard publica em `0.0.0.0:8081`.
- **Fluxo de vídeo:** RTSP → decode HW → `nvstreammux(1)` → `nvinfer(YOLO11m fp16)` → `nvtracker(IOU)` →
  `nvdsosd` → `tee` → {`fakesink` análise | `nvv4l2h264enc` → `rtspclientsink` → MediaMTX `live` → WHEP/HLS}.
- **Fluxo de controle:** JSONL append-only → dashboard (tailer + 480/500 buffers + REST/WS) e watchdog
  externo (restart por ausência de frames/RAM/CUDA).
- **Divergência principal:** `docs/architecture.md` (2026-08-23) ignora inteiramente a onda multifonte
  (sources.yaml, `session_epoch`, reconexão, watchdog, `capture-web`) e ainda descreve `protocols=tcp+udp` e
  codec por env.
