> ⚠️ **RASCUNHO SUPERADO.** Este arquivo é insumo de discovery da sessão de 2026-11-01 e
> **não é normativo**. A versão consolidada e revisada está em `.agent/architecture/*.md`.
> Contém pelo menos dois erros conhecidos e corrigidos no consolidado: whitelist de eventos do
> dashboard (aqui: 18 nomes; real: **14**) e `tegrastats_reader` atribuído a `phase5_run.py:109`
> (real: **LEGADO**, importado por `pipeline.py:12` e `pipeline_rtsp_real.py:12`).

# Mapa de Dependências, Acoplamento e Impacto — Vision Stability Lab

- **Data:** 2026-11-01
- **Branch:** `feature/multifonte` (worktree limpo; 4 untracked: `.opencode/`, `AGENTS.md`, `graphify-out/`, `opencode.json`)
- **Método:** `graphify query` (grafo 1734 nós / 3096 arestas / 106 comunidades) → confirmação de imports reais por `grep` de arestas + leitura pontual dos arquivos no caminho crítico. Nenhuma dependência foi inferida por nome de arquivo.
- **Natureza:** diagnóstico. Não propõe arquitetura-alvo, não altera código.

---

## 1. Mapa de dependências INTERNAS

### 1.1 Grafo real de imports (arestas confirmadas, direção `A → B` = "A importa B")

**Camada de aplicação — `deepstream/`**

```
deepstream/app.py:4            → pipeline.py                      (main)
deepstream/pipeline.py:10-12   → tracker_state, health_metrics, tegrastats_reader            [LEGADO]
deepstream/pipeline_rtsp_real.py:10-13 → tracker_state, health_metrics, tegrastats_reader, rtsp_env  [LEGADO]
deepstream/pipeline_real.py:83-94      → area_monitor, bus_classifier, graceful_shutdown,
                                          health_metrics, source_budget, source_reconnect,
                                          source_state, sources                              [PRODUÇÃO]
deepstream/sources.py:47       → rtsp_env
deepstream/rtsp_*_test.py (6 arquivos, linha 8)  → rtsp_env            [experimentos]
```

**Camada operacional — `scripts/`**

```
scripts/switch_source_lib.py:50-54  → (sys.path.insert em deepstream/) → deepstream/sources.py
scripts/probe/ingest_report.py:54-56 → (sys.path.insert em probe/)     → ingest_report_lib.py
scripts/watchdog.py                  → nenhum import interno (arquivo único, 888 linhas)
scripts/probe/rtsp_read_probe.py, whip_probe.py → nenhum import interno
```

**Camada de apresentação — `dashboard/backend/`**

```
dashboard/backend/app.py:15-26 → fastapi, stdlib. NENHUM import de deepstream/ ou scripts/.
```

**Testes (12 arquivos, 420 testes `def test_`)** — todos injetam `sys.path.insert(0, <dir de produção>)` e importam o módulo de produção diretamente: `test_sources.py:23-26`, `test_source_state.py:26-29`, `test_area_monitor.py:5-7`, `test_bus_classifier.py:16-18`, `test_source_reconnect.py:23-26`, `test_source_budget.py:17-19`, `test_graceful_shutdown.py:31-33`, `scripts/tests/test_switch_source.py:35-37`, `test_watchdog_sources.py:28-30`, `test_ingest_report.py:16-18`, `test_probe_logic.py:19-22`.

### 1.2 Profundidade e ciclos

- Profundidade máxima real: **3** (`pipeline_real → sources → rtsp_env`; `switch_source_lib → sources → rtsp_env`).
- **Ciclos: 0.** Confirmado por inspeção de todas as arestas — todos os módulos "folha" (`source_state`, `source_budget`, `source_reconnect`, `area_monitor`, `bus_classifier`, `graceful_shutdown`, `health_metrics`, `tegrastats_reader`, `tracker_state`, `rtsp_env`) não importam nada interno. Bate com o grafo (`0 ciclos de import`).
- **Isolamento por pureza:** o desenho separa deliberadamente módulos puros (sem `gi`/`pyds`) de módulos de mídia. `sources.py:284` faz `import yaml` **local** justamente para o fallback legado não exigir pyyaml. `pipeline_real.py:90-94` anota em comentário "(puro: sem gi/pyds)". Isso é a principal salvaguarda de testabilidade do repo.

### 1.3 Hubs de acoplamento (grau de entrada / fan-in)

| Módulo | Importado por | Papel |
|---|---|---|
| `deepstream/rtsp_env.py` | `sources.py:47` (produção) + 6 scripts `rtsp_*_test.py` (`:8`) + `pipeline_rtsp_real.py:13` (legado) | **Hub de borda.** Único lugar que lê `RTSP_URI` (`require_rtsp_uri`, `rtsp_env.py:47`). Usado por produção **e** por scripts experimentais. |
| `deepstream/sources.py` | `pipeline_real.py:94`, `switch_source_lib.py:54`, `test_sources.py`, `test_switch_source.py`, `test_pipeline_source_wiring.py` | **Hub de contrato de configuração.** `load_sources()` (`:458`), `read_active_source()` (`:537`), `resolve_active_source()` (`:574`), `depay_factory()` (`:632`). |
| `deepstream/health_metrics.py` | `pipeline_real.py:88`, `pipeline.py:11`, `pipeline_rtsp_real.py:11` | Hub compartilhado entre produção e 2 legados. |
| `deepstream/source_state.py` | `pipeline_real.py:93`, `test_source_state.py` | Hub de contrato de telemetria (`SessionState:85`, `with_namespace:188`). |
| `deepstream/area_monitor.py`, `bus_classifier.py`, `source_budget.py`, `source_reconnect.py`, `graceful_shutdown.py` | apenas `pipeline_real.py` (+ seu teste) | Módulos puros, fan-in 1. Baixo risco de mudança. |

### 1.4 Hubs de acoplamento (grau de saída / fan-out) — o problema real

**`deepstream/pipeline_real.py`** é o nó mais acoplado do sistema por fan-out: **1215 linhas**, 8 imports internos (`:83-94`) e **uma única classe `VisionPipeline` com 31 métodos** (`:255-1182`) cobrindo: construção da cadeia GStreamer, link dinâmico de pad (`:446`), 4 probes (`:455`, `:467`, `:471`, `:497`), classificação de bus (`:607`), ciclo de vida do ramo de preview (9 métodos, `:670-869`), máquina de reconexão da fonte (`:870-971`), liveness RTSP (`:999`), métricas (`:1016`), shutdown/teardown (`:1089-1215`). É o único ponto onde 8 módulos puros convergem — deixá-lo errado contamina todos.

### 1.5 Direções invertidas / proibidas observadas

1. **Script operacional importando módulo de aplicação (direção invertida).**
   `scripts/switch_source_lib.py:50-54` faz `sys.path.insert(0, REPO_ROOT/"deepstream")` e `import sources`. A camada de operação (host, CLI de troca de fonte) depende do código da aplicação. Consequência: `sources.py` não pode ganhar dependência de `gi`/`pyds` sem quebrar a CLI no host — restrição hoje garantida **apenas por teste de texto** (`test_sources.py:639-642` faz `assertNotIn("import gi", codigo)`).
2. **Teste de unidade importando produção por caminho mágico.** Todos os 12 arquivos de teste manipulam `sys.path` — não há pacote instalável nem `pyproject.toml`/`conftest.py`. O layout de diretórios **é** o contrato de importação. Mover um arquivo quebra a suíte.
3. **Teste acoplado ao texto da implementação** (detalhado em §5.3): `test_pipeline_source_wiring.py` lê `pipeline_real.py` e `deploy/docker-compose.yml` como texto e assere strings literais. É a fronteira mais frágil do repo: renomear uma variável local pode reprovar 39 testes.
4. **Duas camadas lendo o mesmo arquivo sem contrato comum** (dashboard e watchdog): não há import entre elas, mas ambas conhecem campos do JSONL. Acoplamento por dado, não por código — não detectável pelo grafo.

---

## 2. Dependências EXTERNAS

### 2.1 Declaradas em pip

`requirements.txt:1-6` (imagem principal, Python 3.10 do container DS):

| Dependência | Declarada | Usada de fato? | Evidência |
|---|---|---|---|
| `pydantic>=2.0` | sim | **NÃO** — nenhum import no repo | grep vazio (só o próprio requirements) |
| `pyyaml>=6.0` | sim | sim (lazy) | `sources.py:284`, `test_pipeline_source_wiring.py:463` |
| `requests>=2.28.0` | sim | **NÃO** — `requests` no repo é só nome de variável local | `test_graceful_shutdown.py:72` |
| `psutil>=5.9.0` | sim | sim | `health_metrics.py:2` |
| `mqtt>=0.0.1` | sim | **NÃO** | grep vazio |
| `paho-mqtt>=1.6.1` | sim | **NÃO** | grep vazio |

`dashboard/backend/requirements.txt:1-2`: `fastapi>=0.111`, `uvicorn[standard]>=0.30` — ambos usados (`app.py:24-26`).

**Constatado:** 4 de 6 dependências pip da imagem principal nunca são importadas (pydantic, requests, mqtt, paho-mqtt). Aumentam superfície de instalação e tempo de build, e — mais grave — **sugerem uma arquitetura MQTT que não existe** (nenhum broker no compose).

### 2.2 Plataforma (não declarável em pip) e onde cada versão é fixada

| Dependência | Versão | Onde está fixada | Fixação |
|---|---|---|---|
| NVIDIA DeepStream SDK | **7.1** | `deploy/Dockerfile:1` (`nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch`) | **Tag apenas — sem digest.** `README.md:11`, `docs/phase-1-evaluation.md:9` repetem 7.1 |
| `pyds` (bindings Python) | **1.2.0 (cp310/aarch64)** | Wheel **vendorizado**: `deploy/wheels/pyds-1.2.0-cp310-cp310-linux_aarch64.whl`, instalado em `deploy/Dockerfile:27-30` | **Bem fixado** (binário no repo + comentário do INC-003) |
| TensorRT | herdada da imagem DS 7.1 | Não há versão explícita em nenhum arquivo | **NÃO fixada** |
| CUDA | herdada da imagem DS 7.1 | Idem | **NÃO fixada** |
| GStreamer | 1.20.3 (medido) | `docs/phase-2-evaluation.md:8` (documentação, não enforcement); plugins via `apt-get` sem versão em `deploy/Dockerfile:5-13` | **NÃO fixada** (apt sem pin) |
| JetPack / L4T | R36.4.7 | `docs/adr/adr-0006-troca-de-fonte.md:6`, `docs/plans/2026-10-30-troca-de-fonte-vsl.md:64`, `docs/phase-1-evaluation.md:9` | **Só documentado** — nada no build falha se o host for outro L4T |
| Driver NVIDIA + Container Toolkit (`runtime: nvidia`) | não declarada | `deploy/docker-compose.yml:73` (`runtime: nvidia`) | **NÃO fixada** — dependência externa do host, invisível ao repo |
| MediaMTX | **1.21.3** | `deploy/docker-compose.yml:6` (comentário explica o pin de ICE) | **Bem fixado** |
| nginx | 1.27-alpine | `deploy/docker-compose.yml:36` | Tag, sem digest |
| Python do dashboard/watchdog | 3.11-slim | `dashboard/Dockerfile:1`, `deploy/Dockerfile.watchdog:1` | Tag, sem digest |
| Modelo YOLO11m | — | `deepstream/config/infer.txt:4` aponta `../../data/yolo11m.onnx` e `:5` aponta `model_b1_gpu0_fp16.engine` | **Engine não versionado**: `.gitignore` exclui `data/*.engine`; o arquivo existe só no host (43.8 MB, 2026-08-22) |
| Parser custom | — | `data/parser/libnvdsinfer_custom_impl_Yolo.so` | **Binário não versionado** (diretório `data/` não é coberto por regra de ignore; presente no host) |

**Assimetria central:** o que é Python puro está razoavelmente fixado (wheel pyds, MediaMTX 1.21.3); o que é plataforma de GPU (TensorRT, CUDA, GStreamer, driver, L4T) **não é fixado em lugar nenhum executável** — só citado em ADR/avaliações. Um host com outro L4T compila e sobe, e falha em runtime.

---

## 3. Contratos entre componentes

### 3.1 C1 — Telemetria JSONL (`data/logs/live.jsonl`)

| Produtor | Consumidores | Acoplamento |
|---|---|---|
| `pipeline_real.log_event()` (`:204-227`) | (a) dashboard `tail_telemetry` (`app.py:132-166`); (b) watchdog `JsonlTail` (`watchdog.py:259`); (c) `switch_source_lib.read_new_records` (`:344`); (d) healthcheck do compose (`docker-compose.yml:122` — só mtime) | **Forte por dado, fraco por código** (nenhum import entre produtor e consumidores) |

**Formato efetivo** (uma linha JSON por registro):
```
{"ts": "<ISO-8601 UTC, ms>", "event": "<nome>", "source_id": "<id>", "session_epoch": "<ISO>", ...campos do evento}
```
- Base: `ts` + `event` (`pipeline_real.py:215-219`).
- Namespace injetado por `with_namespace()` (`source_state.py:188-215`): `source_id` + `session_epoch`, com `setdefault` — campo explícito vence. Sem sessão (`SESSION is None`, `pipeline_real.py:114`), o registro sai sem namespace (casos `startup_failed`, `shutdown_before_startup`).
- `interval_metrics` (`:1047-1074`) — 29 campos: `interval_s, source_id, source_name, source_target_fps, session_epoch, last_frame_age_s, fps_in, fps_processed, fps_preview, preview_enabled, preview_dropped_fps, frames_total_in, frames_total_processed, persons_detected, persons_max_concurrent, active_tracks, area_state, entries_interval, exits_interval, avg_dwell_s, events_buffered, total_entries, total_exits, queue_buffers, rss_mb, memory_growth_mb, threads, open_fds`.
- Escrita é **best-effort**: `except OSError: pass` (`:226-227`) — perda de telemetria é silenciosa e não contabilizada. `stdout` é a fonte primária (docstring `:227`).

**Riscos de contrato (constatados):**
- **Sem schema versionado, sem version field, sem validação.** Nenhum arquivo `.json`/`.schema`; nenhum consumidor valida tipo. Dashboard faz `record.get(...)` livre.
- **Vocabulário de eventos duplicado em 3 lugares, sem constante compartilhada:**
  - produtor: `pipeline_real.py` (strings em `log_event(...)`);
  - dashboard: **whitelist fechada** em `app.py:77-86` (18 nomes) — evento novo que não esteja na lista **não entra** na lista de eventos da UI, sem erro;
  - watchdog/switch: `switch_source_lib.py:78` (`FAILURE_EVENTS`), `:82-84` (`NO_MEDIA_PREFIXES`, `UNSTABLE_PREFIXES` por **prefixo de string**).
- **Divergência documentação × implementação:** `README.md:329-390` define um contrato aninhado (`timestamp`, `device_id`, `pipeline_id`, `source{uri,connected,last_frame_age_ms}`, `vision{...}`, `process{...}`, `jetson{...}`, `health{...}`) que **não é implementado por ninguém**: o produtor emite plano (`ts`, `fps_processed`, `rss_mb`…). Não há `jetson{}` na telemetria de produção — `tegrastats_reader.py` só é importado pelos pipelines **legados** (`pipeline.py:12`, `pipeline_rtsp_real.py:12`), nunca por `pipeline_real.py`.
- **Compatibilidade declarada como aditiva** (`pipeline_real.py:205-213`, `source_state.py:194-205`: "campo explícito vence", "JSONL antigo sem epoch"). `switch_source_lib.py:299-310` degrada quando não há `session_epoch` — a compatibilidade retroativa é real, mas é garantida por disciplina, não por contrato.

### 3.2 C2 — Telemetria de auditoria de troca (`data/logs/switch.jsonl`)

- **Produtor:** `switch_source_lib.switch_record()` (`:525-556`), gravado por `scripts/switch_source.sh` (`:64`).
- **Consumidores:** **nenhum em código** — é auditoria para humano (justificativa em `:530-537`: "responder 'quando o drone entrou e por quê'").
- Formato: `{ts, event:"switch_source", from, to, probe, result, reason}`. `probe ∈ {ok, skipped, blocked, failed}` (enum na docstring `:529-534`, não validado).
- Acoplamento: **fraco** (append-only, sem leitor). Risco: formato pode divergir sem ninguém notar.

### 3.3 C3 — Marcadores de controle

| Arquivo | Formato | Produtor | Consumidor | Acoplamento |
|---|---|---|---|---|
| `data/config/active_source` | 1 linha, id de fonte; aceita BOM/CRLF/`#` | `scripts/switch_source.sh:61` (escrita atômica, `:223`); operador pode editar à mão | `sources.read_active_source()` (`sources.py:537-572`) | **Forte e unidirecional.** Precedência: `ACTIVE_SOURCE` env > arquivo > `default_source` > 1ª `enabled` (`sources.py:574-...`). Arquivo ilegível = **erro**, não default (`:557-570`, fail-closed deliberado) |
| `data/config/switch_in_progress` | `{"to": "<id>", "expires_at": "<ISO>"}` | `scripts/switch_source.sh:62` (removido por trap) | `watchdog.read_switch_marker()` (`watchdog.py:369-421`) | **Fraco, tolerante por construção**: ausente/`expired`/`corrupt`/`unreadable` → `(False, motivo)`; marcador malformado **nunca** suprime alerta |
| `session_epoch` (não é arquivo, é o namespace do JSONL) | ISO do `STARTED_AT` | `SessionState.new()` sobre `STARTED_AT` (`pipeline_real.py:107`); `source_state.py:63-83` | `switch_source_lib.process_boundary/sample_is_from_process` (`:278`, `:295`) — prova qual processo escreveu a amostra | **Forte.** É o que impede o switch aprovar a troca com amostra do processo anterior (docstring `:16-20`). Tolerância de relógio: `DEFAULT_SESSION_TOLERANCE_S = 2.0` (`:65`) |

### 3.4 C4 — HTTP/WS do dashboard

| Rota | Produtor | Consumidor | Contrato |
|---|---|---|---|
| `GET /api/snapshot` (`app.py:199`) | `DashboardState.snapshot()` (`:108-125`) | `app.js` | Sem schema; payload montado ad hoc (`health`, `latest_metrics`, `error_count`, `events_recent` [-50:]) |
| `GET /api/history?minutes=30` (`:204`) | ring buffer `metrics_history` (max 480, `:34`) | `app.js:540` | Sem schema; filtra por `recv_at` |
| `GET /api/events?limit=100` (`:210`) | deque (max 500, `:35`) | UI | Sem schema |
| `WS /ws` (`:215`) | `broadcast()` (`:169`) | `app.js:217` (`new WebSocket(.../ws)`) | Envelope `{"type": "snapshot"\|"record", "data": ...}` (docstring `:9`, `:158-161`, `:220-221`) — **único contrato de envelope, não documentado fora do código** |
| `GET /healthz` (`:230`) | constante `{"ok": true}` | — | **Não reflete saúde real** (não consulta `state.health()`); não usado por healthcheck do compose |
| `GET /` (`:235`), `/static` (`:240-241`) | `FileResponse` / `StaticFiles` do bind mount `../dashboard` | navegador | Acoplamento ao layout de diretório |

- Acoplamento dashboard ↔ pipeline: **fraco por código, forte por dado** (só o arquivo). É o melhor isolamento do repo.
- **Deriva de limiar (duplicação de conhecimento):** dashboard considera `stale` após `STALE_AFTER_S=90` (`app.py:37`); healthcheck do compose tolera até **180 s** de mtime (`docker-compose.yml:122`); watchdog usa intervalo de 60 s justificado em comentário (`watchdog.py:73-75`). Três respostas diferentes para "a telemetria está velha".

### 3.5 C5 — Paths do MediaMTX

| Path | Uso | Produtor | Consumidor | Evidência |
|---|---|---|---|---|
| `live` | preview anotado (RTSP RECORD) | `pipeline_real.py:696-698` (`rtspclientsink`, `location=PREVIEW_MTX_URI`) | dashboard frontend via WHEP `https://<host>:8889/live/whep` (`app.js:93-100`) | nome do path aparece em `mediamtx.yml:39`, `pipeline_real.py:139` (default `rtsp://mediamtx:8554/live`), compose `:98`, e **hardcoded** em `app.js:100` |
| `drone` | push RTMP → republicado RTSP | externo (drone) | `sources.py` via `sources.yaml:41` (`rtsp://${MTX_HOST:-mediamtx}:8554/drone`) | `mediamtx.yml:41` |
| `phone` | publish WHIP → republicado RTSP | `dashboard/frontend/capture.js` (`DEFAULT_PATH="phone"`, `:50`; `whipUrl()`, `:152-167`) | `sources.yaml:57` | `mediamtx.yml:43`, `whip_probe.py:279` (default `phone`) |

- **Acoplamento forte e sem constante compartilhada:** o par `(path, codec)` precisa casar entre o YAML de fontes e configuração do MediaMTX; um rename de path quebra em runtime sem erro de build. `codec` por fonte é declarado só em `sources.yaml` (`camera: h265`, `drone: h264`, `phone: h264`).
- `PREVIEW_TRANSPORT` (`rtsp`|outro) muda o **tipo de sink** (`rtspclientsink` vs `udpsink`, `pipeline_real.py:695-708`) — o path `live` só existe no modo `rtsp`.

### 3.6 C6 — Variáveis de ambiente

- **Container do pipeline — 33 nomes distintos:** 27 lidos em `pipeline_real.py` (`MAX_EVENTS`, `METRICS_INTERVAL_S`, `PIPELINE_STOP_TIMEOUT_S`, 12 `PREVIEW_*`, `RTSP_IDLE_RECONNECT_S`, `RTSP_RECONNECT_COOLDOWN_S`, 7 `SOURCE_*`, `TELEMETRY_FILE`, `TEST_DURATION_S`, `TRACK_TTL_S`) + 6 definidos como constantes em `sources.py:54-59` (`SOURCES_CONFIG`, `ACTIVE_SOURCE`, `VSL_SOURCE_SELECTION`, `RTSP_URI`, `RTSP_CODEC`, `RTSP_TARGET_FPS`). `MTX_HOST` entra por expansão de `${VAR}` dentro do YAML (`sources.py:184`), não por `os.environ`.
- **Defaults duplicados em dois lugares** (risco de deriva silenciosa): compose `${VAR:-x}` (`docker-compose.yml:96-113`) e Python `os.environ.get("VAR", "x")` (`pipeline_real.py:132-186`). Ex.: `SOURCE_RECONNECT_MAX_ATTEMPTS` default 5 nos dois (`compose:107` / `pipeline_real.py:158-159`) — se um mudar, o comportamento difere conforme o container subir pelo compose ou direto.
- **Watchdog:** **22** variáveis (`watchdog.py:56-110`): `APP_CONTAINER`, `TELEMETRY_FILE`, `WATCHDOG_LOG`, `SWITCH_MARKER`, `TARGET_FPS`, `DOCKER_SOCK` + 16 limiares de regra (crash-loop, RSS, RAM, fps, cooldowns).
- **Dashboard:** 5 variáveis — `TELEMETRY_FILE` (`app.py:32`), `METRICS_HISTORY_MAX`, `EVENTS_HISTORY_MAX`, `TAIL_POLL_S`, `STALE_AFTER_S` (`:34-37`) — **nenhuma** delas é passada pelo compose (só `TELEMETRY_FILE`, em `compose:155`).
- **Infra:** `LAN_IP`, `DASHBOARD_PORT`, `COMPOSE_FILE`, `ENV_FILE`, `VSL_LAN_IP` (certs).
- **Nenhum `.env` é versionado** (`deploy/.env` gitignored; `.gitignore`), então a lista de variáveis obrigatórias só existe em `deploy/.env.example` — correto do ponto de vista de segredo, mas significa que o compose referencia `${RTSP_URI:-}` vazio e o erro só aparece em runtime (`RTSP_URI` ausente → `SourceConfigError` legível via `rtsp_env.py:47`).

---

## 4. Dependências de deploy / infra

### 4.1 Serviços, imagens e ordem

| Serviço | Imagem/build | Portas publicadas | depends_on | healthcheck |
|---|---|---|---|---|
| `mediamtx` | `bluenviron/mediamtx:1.21.3` (`compose:6`) | 8554/tcp, 1935, 8888, 8889, 8890/udp, 8189/udp+tcp — **todas amarradas a `${LAN_IP:-192.168.59.102}`** (`:15-21`) | — | **nenhum** |
| `capture-web` | `nginx:1.27-alpine` (`:36`) | 8443 → **`${LAN_IP}:8443`** (`:42`) | — | **sim**, HTTP real em 8443 (`:54`) |
| `vision-app` | build `deploy/Dockerfile` (`:61-63`) | nenhuma (interna) | `mediamtx` (`:118-119`) — **sem `condition`**, só ordem de start | **sim**, mtime de `live.jsonl` < 180 s (`:122`) |
| `watchdog` | build `deploy/Dockerfile.watchdog` (`:129-131`) | nenhuma | `vision-app` (`:142-143`) | **nenhum** |
| `dashboard` | build `dashboard/Dockerfile` (`:146-148`) | `${DASHBOARD_PORT:-8081}:8000` (`:153`, **em `0.0.0.0`**, diferente das demais) | **nenhum** | **nenhum** |

- `vision-app` roda `privileged: true` + `runtime: nvidia` + `init: true` (`:71-73`). `privileged` é exigência real de DeepStream/L4T, mas amplia o blast radius de qualquer comprometimento do código do pipeline.
- `watchdog` monta `/var/run/docker.sock` (`:141`) — plano de controle com poder de recriar o container da aplicação. É a dependência de infra mais perigosa do stack.

### 4.2 Bind mounts (o que é código vs. o que está na imagem)

```
vision-app : ../deepstream → /app/deepstream   (compose:115)
             ../data       → /app/data         (compose:116)
             ../scripts    → /app/scripts      (compose:117)
dashboard  : ../data → /app/data:ro            (:157)
             ../dashboard → /app/dashboard     (:158)
mediamtx   : ../mediamtx/mediamtx.yml → /mediamtx.yml  (:23)
             ../deploy/certs → /certs:ro       (:29)
capture-web: ../deploy/nginx/capture-web.conf → /etc/nginx/conf.d/default.conf:ro (:44)
             ../dashboard/frontend → /usr/share/nginx/html:ro (:46)
             ../deploy/certs → /certs:ro       (:48)
watchdog   : ../data → /app/data, /var/run/docker.sock (:140-141)
```

**Consequência arquitetural (fato):** `deploy/Dockerfile:31` (`COPY . /app`) e `dashboard/Dockerfile:9` (`COPY dashboard/ /app/dashboard/`) são **sobrescritos** pelos bind mounts em runtime. Editar `deepstream/*.py` não exige rebuild; editar `requirements.txt` **exige** (pip está só na imagem). Isso é um contrato implícito de operação — não documentado no compose.

### 4.3 TLS / CA interna (`deploy/certs`)

- Conteúdo real no host: `ca.crt`, `ca.key`, `server.crt`, `server.key`, `generate-certs.sh`. **Versionado:** apenas `deploy/certs/.gitignore` e `generate-certs.sh`.
- **O que quebra sem os certificados:**
  1. `mediamtx` — `webrtcEncryption: yes` com `webrtcServerCert/Key` em `/certs` (`mediamtx.yml:31-33`): sobe e cai na primeira conexão WebRTC (comentário `compose:26-28`);
  2. `capture-web` — nginx falha com "cannot load certificate": **o container não sobe**, por isso o healthcheck bate em 8443 de verdade e não em `nginx -t` (`compose:50-54`);
  3. página de captura no celular (`getUserMedia` exige contexto seguro, `capture-web.conf:3-8`) e WHIP HTTPS (`capture.js:9`, `:161`);
  4. dashboard: WHEP `https://<host>:8889/live/whep` (`app.js:96-100`) — preview não toca sem CA confiável no navegador (`app.js:138-149`).
- **Acoplamento ao IP da LAN:** o IP entra no SAN (`generate-certs.sh:188`, `subjectAltName=DNS:$LOCAL_DOMAIN,DNS:localhost,IP:$LAN_IP,IP:127.0.0.1`) e em `nginx server_name` (`capture-web.conf:22`). Mudar `LAN_IP` **invalida a CA já instalada nos celulares** (`generate-certs.sh:25,41`) — troca de rede = re-onboarding de todos os dispositivos.
- `deploy/certs` é gitignored **por inteiro** → clone limpo não sobe `capture-web` nem WHIP. Não há `make bootstrap`/script de primeiro setup no compose.

### 4.4 Recursos externos ao repo

- `data/model_b1_gpu0_fp16.engine` (43.8 MB) — exigido por `infer.txt:5`, **ignorado pelo Git** (`.gitignore`: `data/*.engine`). Sem ele, `nvinfer` falha em runtime; nada no build avisa.
- `data/parser/libnvdsinfer_custom_impl_Yolo.so` — exigido por `infer.txt:15`.
- `deploy/wheels/pyds-*.whl` — vendorizado (bom).
- `deploy/.env` com `RTSP_URI` (credencial de câmera) — gitignored, obrigatório na prática.

---

## 5. Análise de impacto

### 5.1 Blast radius dos hubs

| Mudança em | Afetados diretos | Afetados transitivos | Evidência |
|---|---|---|---|
| `load_sources()` / `SourceConfig` / `sources.yaml` (`sources.py:458`) | `pipeline_real.py` boot; `switch_source_lib.resolve_source` (`:136`), `precheck_url` (`:160`); 61 testes de `test_sources.py`; 69 de `test_switch_source.py` | Toda troca de fonte (CLI + operador), telemetria `source_active`, dashboard (card de fonte), watchdog (regras por fonte) | `test_pipeline_source_wiring.py:33` assere literalmente `"resolve_active_source(load_sources())"` no texto do pipeline |
| `VisionPipeline` (`pipeline_real.py:255`, 31 métodos) | 4 probes, bus, preview (9 métodos), reconexão (7), métricas, shutdown | **Todos os consumidores de telemetria** indiretamente: nomes de evento e campos são produzidos aqui | Alterar `fps_processed`, `last_frame_age_s`, `area_state` quebra: watchdog (`:576`), dashboard (`health()` `app.py:102-106`), switch (`read_new_records`) |
| `SessionState` / `with_namespace` (`source_state.py:85`, `:188`) | `log_event` (todo registro), `switch_source_lib.process_boundary` (`:278`), `sample_is_from_process` (`:295`) | Provas de troca de fonte; histórico do dashboard (campo `run` é derivado de `startup`, `app.py:60-64`) | Renomear `session_epoch` reprova 22 testes de `test_source_state.py` e quebra a validação de troca em produção |
| `log_event()` (`pipeline_real.py:204`) | 100% dos eventos; healthcheck do compose depende do mtime do arquivo | dashboard (todas as rotas), watchdog (todas as regras), switch (pós-check) | Mudar o nome de `ts` quebra `switch_source_lib.parse_iso_epoch` (usa `ts`, `:307`); mudar o caminho quebra 3 consumidores |
| `AreaMonitor` (`area_monitor.py`, 19 arestas) | `pipeline_real._on_processed_buffer` | `area_state`/`entries`/`exits`/`dwell` na telemetria → dashboard + watchdog | 10 testes; módulo puro, impacto contido desde que a assinatura de saída não mude |

### 5.2 Sinais de risco de acoplamento

1. **Duplicação de conhecimento (4 ocorrências confirmadas):**
   - **IP da LAN** `192.168.59.102` em 6 lugares independentes: `mediamtx.yml:18`, `compose:15-21,42` (7 binds), `capture-web.conf:22`, `switch_source_lib.py:60` (`DEFAULT_LAN_IP`), `generate-certs.sh:63`, `.env.example`.
   - **Ids de fonte** (`camera`/`drone`/`phone`) em: `sources.yaml`, `mediamtx.yml:39-43`, `capture.js:50`, `whip_probe.py:279`, `active_source*`.
   - **Vocabulário de eventos** em: `pipeline_real.py` (emissão), `app.py:77-86` (whitelist), `switch_source_lib.py:78-84` (prefixos de falha).
   - **Limiar de "telemetria velha"** em 3 valores: 90 s (`app.py:37`), 180 s (`compose:122`), 60 s (`watchdog.py:73-75`).
2. **Módulo com múltiplas responsabilidades:** `pipeline_real.py` (1215 linhas, 31 métodos, 8 deps internas) concentra mídia + estado + métricas + shutdown + reconexão + preview. Nenhuma outra unidade do repo tem mais de 900 linhas.
3. **Quatro "pipelines" coexistentes, sem marcação de obsolescência no código:**
   - produção: `pipeline_real.py` (`Dockerfile:32` CMD);
   - legados: `pipeline.py`, `pipeline_rtsp_real.py` (importados só por `app.py:4` e por ninguém, respectivamente);
   - `app.py` é **entrypoint enganoso**: importa `pipeline` (legado), não `pipeline_real`, e não é usado pelo Dockerfile. Quem editar `app.py` "achando que é o boot" não altera o runtime.
   - experimentos: 6 `rtsp_*_test.py` (acoplam a `gi` **e** `rtsp_env`) + `phase1_test.py`/`phase1_local_test.py` (só `gi`) — nenhum tem teste próprio; nenhum está no build.
4. **Duplicação de configuração de fonte:** `deepstream/config/source.yaml` (formato INI `[source0]`, uri `rtsp://localhost:8554/live`) **não é lido por ninguém** (grep: só aparece em `README.md:654`), enquanto `sources.yaml` é o descritivo real. Risco de edição no arquivo errado.
5. **Testes acoplados à implementação** (ver 5.3) — o principal amortecedor de refatoração está comprometido.

### 5.3 Testes acoplados a implementação (fato, não hipótese)

`deepstream/tests/test_pipeline_source_wiring.py` (**39 testes, 483 linhas**) não testa comportamento: lê `pipeline_real.py`, `deploy/docker-compose.yml` e `scripts/mem_budget.sh` como **texto** e assere substrings literais (`:33` `"resolve_active_source(load_sources())"`, `:39-47` `"self.rtsp_uri = self.source.uri"`, `:57-61` `"self.depay = self._make(self.depay_factory)"`). Justificativa declarada na docstring (`:1-16`): o pipeline não pode ser importado (puxa `gi`/`pyds`/TensorRT). Consequências:
- renomear variável local, reformatar expressão ou reordenar argumentos em `pipeline_real.py` **reprova a suíte sem regressão funcional**;
- o teste valida a *forma do código*, não o comportamento — passa com o pipeline quebrado se as strings estiverem presentes (a própria docstring do teste em `:50-56` documenta um caso em que exatamente isso ocorreu: `self._make("rtph264depay")` passava no teste antigo);
- acopla a suíte a `deploy/` (camada de infra) a partir de um teste de `deepstream/`.

Outros acoplamentos a implementação: `test_sources.py:631-633` (`assertTrue(vizinho.is_file())` — testa a **existência de outro arquivo de teste**) e `:639-642` (`assertNotIn("import gi", codigo)` — regra de pureza por texto). `test_no_hardcoded_secrets.py:33-40` roda `git ls-files` — teste depende do estado do índice do Git.

---

## 6. FATOS / HIPÓTESES / LACUNAS

### FATOS (arquivo:linha)

**Internos**
1. `deepstream/pipeline_real.py:83-94` importa 8 módulos internos; é o maior fan-out do repo. `app.py:4` importa `pipeline` (legado), não `pipeline_real`.
2. `deploy/Dockerfile:32` (`CMD ["python3", "deepstream/pipeline_real.py"]`) é o entrypoint real — `deepstream/app.py` não é usado em runtime.
3. `deepstream/sources.py:47` → `rtsp_env`; `scripts/switch_source_lib.py:50-54` (`sys.path.insert` em `deepstream/`) → `sources`. Direção script-operacional → aplicação.
4. `dashboard/backend/app.py:15-26` não importa nada de `deepstream/` nem de `scripts/`. Isolamento total por código.
5. `deepstream/pipeline_real.py:254-1182` — classe única `VisionPipeline` com 31 métodos; arquivo com 1215 linhas.
6. Nenhum ciclo: todos os módulos folha (`source_state.py:29-33`, `source_budget.py:16`, `source_reconnect.py:31-33`, `area_monitor.py:7-8`, `bus_classifier.py:23-25`, `graceful_shutdown.py:25-28`) só importam stdlib.
7. `deepstream/config/source.yaml` (INI, 277 bytes) não é referenciado por nenhum código; só por `README.md:654`.
8. `deepstream/tegrastats_reader.py` é importado apenas por `pipeline.py:12` e `pipeline_rtsp_real.py:12` — **nenhum** uso no pipeline de produção.

**Externos**
9. `requirements.txt:1-6` declara `pydantic`, `pyyaml`, `requests`, `psutil`, `mqtt`, `paho-mqtt`; só `psutil` (`health_metrics.py:2`) e `pyyaml` (`sources.py:284`, lazy) são importados. `requests` no repo é nome de variável (`test_graceful_shutdown.py:72`).
10. DeepStream 7.1 fixado apenas por tag em `deploy/Dockerfile:1`; sem digest. TensorRT/CUDA/GStreamer/driver/L4T **sem versão executável** — só em `docs/` (`adr-0006:6`, `phase-1-evaluation.md:9`, `phase-2-evaluation.md:8`).
11. pyds 1.2.0 fixado por wheel vendorizado (`deploy/Dockerfile:27-30`, `deploy/wheels/`). MediaMTX 1.21.3 fixado com justificativa (`docker-compose.yml:3-6`).
12. `deepstream/config/infer.txt:5` exige `data/model_b1_gpu0_fp16.engine`, excluído por `.gitignore` (`data/*.engine`) — existe apenas no host.

**Contratos**
13. Formato real do JSONL: `{"ts","event","source_id","session_epoch",...}` (`pipeline_real.py:215-220`, `source_state.py:188-215`). **Não há schema, validação nem campo de versão.**
14. `README.md:329-390` documenta contrato aninhado (`timestamp/device_id/pipeline_id/source{}/vision{}/process{}/jetson{}/health{}`) que não é emitido por nenhum produtor.
15. `dashboard/backend/app.py:77-86` mantém whitelist fechada de 18 nomes de evento; evento novo é descartado silenciosamente na lista.
16. `switch_source_lib.py:78-84` classifica falha por **prefixo de string** (`"no_frames"`, `"fps_below_min"`, …) sobre campos do JSONL.
17. `data/logs/switch.jsonl` (`switch_record`, `switch_source_lib.py:525`) não tem nenhum consumidor em código.
18. `sources.py:537-572`: `active_source` ilegível levanta `SourceConfigError` (fail-closed); ausente cai em `default_source`.
19. `watchdog.py:369-421`: marcador de troca é tolerante a ausência/expiração/corrupção, sempre retornando `(ativo=False)` nesses casos.
20. `pipeline_real.py:226-227`: falha de escrita da telemetria é engolida (`except OSError: pass`), sem contador.

**Deploy**
21. Todas as portas de mídia são amarradas a `${LAN_IP:-192.168.59.102}` (`compose:15-21`), exceto o dashboard, em `0.0.0.0` (`compose:153`).
22. `depends_on` sem `condition` (`compose:118-119`, `:142-143`) — ordem de start, não readiness. `mediamtx` e `dashboard` não têm healthcheck.
23. Healthcheck do `vision-app` depende do **mtime** de `/app/data/logs/live.jsonl` < 180 s (`compose:122`).
24. `watchdog` monta `/var/run/docker.sock` (`compose:141`); `vision-app` roda `privileged: true` (`compose:72`).
25. Bind mounts sobrescrevem `COPY . /app` de `deploy/Dockerfile:31` e `dashboard/Dockerfile:9` (`compose:114-117`, `:156-158`).
26. `deploy/certs` é gitignored por inteiro (só `.gitignore` e `generate-certs.sh` versionados); sem `server.crt` o `capture-web` **não sobe** (`compose:49-54`) e o MediaMTX cai na primeira conexão WebRTC (`compose:24-29`).
27. SAN do certificado contém o `LAN_IP` (`generate-certs.sh:188`); trocar de rede invalida a CA instalada nos celulares (`generate-certs.sh:25,41`).

**Testes**
28. 12 arquivos, 420 funções `def test_`; todos usam `sys.path.insert` para importar produção. Não há `conftest.py`, `pyproject.toml` ou pacote instalável.
29. `test_pipeline_source_wiring.py` (39 testes) assere strings literais de `pipeline_real.py` e `deploy/docker-compose.yml` (`:21-22`, `:33`, `:39-61`).
30. `test_sources.py:631-633` verifica a existência de outro arquivo de teste; `:639-642` verifica ausência textual de `import gi`.

### HIPÓTESES (não confirmadas por leitura completa)

- **H1.** `pipeline.py`, `pipeline_rtsp_real.py`, `app.py`, `phase1_*` e os 6 `rtsp_*_test.py` são resíduos de fases anteriores e não têm uso operacional. Evidência: só `pipeline_real.py` está no `Dockerfile:32` e nenhum outro arquivo é referenciado por compose/scripts. Não verifiquei `docs/runbooks` nem histórico completo para confirmar ausência de uso manual.
- **H2.** O contrato aninhado do `README.md:329-390` é a especificação original de projeto (pré-implementação) e foi substituído informalmente pelo formato plano. `README.md:159-196` cita "separação entre canal de vídeo e canal de eventos/métricas" — coerente com a implementação, mas não prova a intenção.
- **H3.** `source.yaml` (INI) seja template herdado de exemplo do DeepStream (`deepstream-app`), mantido por inércia. O conteúdo (`[source0]`, `camera-name=lab-camera-01`, `uri=rtsp://localhost:8554/live`) é compatível com a hipótese, mas não há comentário que o declare obsoleto.
- **H4.** O watchdog não valida `session_epoch` porque decide por `fps_processed`/`last_frame_age_s` e recriação de container; a ausência de `session_epoch` em `watchdog.py` (grep) sugere decisão consciente, mas não há comentário explicitando o motivo.
- **H5.** O acoplamento de teste por texto (`test_pipeline_source_wiring.py`) foi escolha deliberada para permitir testar o pipeline sem GPU (docstring `:1-16`), e o custo de fragilidade é aceito — não há ADR registrando essa decisão.

### LACUNAS

1. **Sem schema de telemetria**: nem JSON Schema, nem dataclass de evento, nem validação no consumidor. Não é possível detectar regressão de campo sem inspecionar 3 consumidores.
2. **Sem versão de contrato**: o JSONL não tem `schema_version`; `switch_source_lib.py:299-310` infere compatibilidade pela ausência de `session_epoch`. Como lidar com 3+ gerações de formato não está definido.
3. **`/api/snapshot` e `/ws` sem contrato publicado**: o envelope `{"type","data"}` só existe em código e na docstring do módulo. O frontend (`app.js`) é o único consumidor e não há teste de contrato.
4. **Sem teste de contrato produtor→consumidor**: nada garante que um campo emitido por `log_event` chegue ao dashboard. `test_*` cobre os dois lados isoladamente.
5. **`/healthz` do dashboard não reflete saúde** (`app.py:230-232` devolve constante) e não é usado pelo compose — não há sonda de saúde do dashboard.
6. **Nenhum teste de integração da stack** (compose up → telemetria → dashboard). Toda validação de mídia é manual no container único (conforme `test_pipeline_source_wiring.py:11-13`).
7. **Versões de plataforma não fixadas** (TensorRT, CUDA, GStreamer, driver, L4T) — não há `docker inspect`/`--version` registrado em artefato versionado, só prosa em docs.
8. **Sem pin por digest** em nenhuma imagem; `apt-get install` sem versão (`deploy/Dockerfile:5-13`).
9. **Dependências fantasma** (pydantic, requests, mqtt, paho-mqtt) sem justificativa registrada — não há ADR/plano declarando intenção de MQTT.
10. **Não verificado**: uso real de `mqtt`/`requests` em artefatos fora do Git, e se `data/parser/*.so` está coberto por alguma política de versionamento (o `.gitignore` só exclui `data/*.onnx` e `data/*.engine`, mas o `.so` não aparece em `git ls-files`).
11. **Não verificado**: se `PREVIEW_TRANSPORT=udp` (Fase 3) ainda é usado operacionalmente — a configuração existe (`pipeline_real.py:700-708`) e não tem teste.
