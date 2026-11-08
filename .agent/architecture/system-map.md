# System Map — Vision Stability Lab

> **Natureza:** documento de descoberta (arquitetura ATUAL), não de arquitetura-alvo.
> **Data:** 2026-11-01 · **Branch:** `feature/multifonte` · **Commit de referência:** `cd5bbfb`
> **Método:** Graphify (1734 nós / 3096 arestas / 106 comunidades) como primeira camada,
> seguido de leitura direcionada. Toda afirmação factual traz `arquivo:linha`.
> **Estado do Git:** em `cd5bbfb` o worktree estava limpo (4 untracked não-produção:
> `.opencode/`, `AGENTS.md`, `graphify-out/`, `opencode.json`). **Durante a análise** surgiram
> alterações de E4 no worktree (`dashboard/backend/app.py`, `app.js`, `index.html`, `style.css`,
> `requirements.txt`, `deploy/docker-compose.yml`, novo `dashboard/backend/sources_catalog.py` e
> `dashboard/tests/`). Todo este documento descreve **`cd5bbfb`**, não o worktree corrente —
> a única mudança material já observada no compose é o dashboard receber
> `SOURCES_CONFIG` + bind ro de `deepstream/config` (E4-T1), que não altera nenhuma conclusão
> estrutural abaixo. Nenhum arquivo de código foi alterado por esta análise.

Legenda de confiança: **[F]** fato verificado · **[H]** hipótese · **[L]** lacuna.

---

## 1. Propósito e forma do sistema

Sistema embarcado (Jetson Orin) de monitoramento contínuo de ocupação por vídeo. Ingesta RTSP,
decodifica por hardware (NVDEC), infere com TensorRT (`nvinfer`, YOLO11m FP16, classe única
`person`), rastreia (`nvtracker` IOU), aplica regra temporal de permanência, desenha OSD,
publica um preview H.264 e expõe telemetria para um dashboard web. **[F]**

Características estruturais que definem a arquitetura:

| Característica | Evidência |
|---|---|
| **Uma fonte ativa por vez**, `batch_size=1` | `deepstream/pipeline_real.py:357`; `deepstream/config/infer.txt`; ADR-0006 D1 |
| **Troca de fonte = restart de processo**, nunca reconfiguração em runtime | `scripts/switch_source.sh`; ADR-0006 D4 |
| **Única persistência é um JSONL append-only** — sem banco, sem fila | `deepstream/pipeline_real.py:204-228`; `dashboard/backend/app.py:1-18` |
| **Vídeo e telemetria são canais separados** — o navegador busca vídeo direto no MediaMTX, não pelo backend | `dashboard/frontend/app.js:93-100`; `README.md:159-196` |
| **Segurança de processo em camadas**: supervisor externo (watchdog) + shutdown gracioso interno + reconexão com orçamento | `scripts/watchdog.py`; `deepstream/graceful_shutdown.py`; `deepstream/source_budget.py` |

**[F] Ponto de entrada real:** `python3 deepstream/pipeline_real.py` — `deploy/Dockerfile:29`.
`deepstream/pipeline.py`, `pipeline_rtsp_real.py` e `app.py` **não** são referenciados por
Dockerfile, compose ou scripts. Corroboração independente: os eventos de `live.jsonl`
(`interval_metrics` 5565, `source_reconnect_action` 208, `preview_branch_ready` 301) só existem
em `pipeline_real.py`.

---

## 2. Fluxo de vídeo (fonte → browser)

Todos os elementos são criados no `__init__` de `VisionPipeline` (`pipeline_real.py:254`) e
linkados estaticamente; apenas o `rtspsrc` usa link dinâmico por `pad-added`.

| # | Estágio | Evidência |
|---|---|---|
| 1 | Resolução da fonte ativa — `load_sources()` + `resolve_active_source()` | `pipeline_real.py:278`; `sources.py:458`, `:574` |
| 2 | Abertura de sessão — `begin_session(source.id)` → `session_epoch` | `pipeline_real.py:283`; `source_state.py:85` |
| 3 | Codec da fonte — `depay_factory(codec)` | `pipeline_real.py:288`; `sources.py:632-646` |
| 4 | `rtspsrc` (`latency`, `drop-on-latency=True`, `protocols` da fonte) | `pipeline_real.py:349-352` |
| 5 | Link dinâmico do pad de vídeo (`rtsp_video_pad`) | `pipeline_real.py:446-453` |
| 6 | `rtph265depay`/`h265parse` **ou** `rtph264depay`/`h264parse` | `pipeline_real.py:353-354`, `:388` |
| 7 | `nvv4l2decoder` (saída NVMM) | `pipeline_real.py:355` |
| 8 | `nvstreammux(batch=1, 1280x720, live-source)` | `pipeline_real.py:357-358` |
| 9 | `nvinfer` (YOLO11m FP16, `num-detected-classes=1`, threshold 0.5) | `pipeline_real.py:361-362`; `config/infer.txt` |
| 10 | `nvtracker` IOU (640x384) | `pipeline_real.py:364-373` |
| 11 | `nvdsosd` (bbox + texto, `process-mode=1`) | `pipeline_real.py:375-376` |
| 12 | `tee` (2 request pads) | `pipeline_real.py:377`, `:400-408` |
| 13a | **Ramo de análise:** `queue` → `fakesink` | `pipeline_real.py:379-380` |
| 14a | Probe de entrada (FPS + reset de budget) no sink pad do `nvinfer` | `pipeline_real.py:423`, `:455-465` |
| 15a | Probe de processados (pyds → `AreaMonitor`, `class_id == 0`) | `pipeline_real.py:425`, `:497-549` |
| 16a | `AreaMonitor`: EMPTY/OCCUPIED, TTL 10 s, dwell, transições | `area_monitor.py:11`, `:24` |
| 13b | **Ramo de preview:** `queue(leaky=downstream, max-size-buffers=1)` | `pipeline_real.py:681-683` |
| 14b | `nvvideoconvert` → `capsfilter NV12/NVMM` | `pipeline_real.py:684-690` |
| 15b | `nvv4l2h264enc` (4 Mbps, IDR 30, SPS/PPS inline) → `h264parse` | `pipeline_real.py:691-694` |
| 16b | `rtspclientsink` → MediaMTX `live` (TCP); alternativa `udp` | `pipeline_real.py:696-699`, `:702-707` |
| 17 | MediaMTX: RTSP 8554, HLS 8888, WebRTC 8889 (TLS) | `mediamtx/mediamtx.yml:2-5`, `:31-33` |
| 18 | Browser: player **WHEP**; 8889/8888 usados como **diagnóstico**, não como fallback de vídeo | `dashboard/frontend/app.js:68-80`, `:93-100` |

```
rtspsrc ─▶ [rtph26xdepay/h26xparse] ─▶ nvv4l2decoder ─▶ nvstreammux(1, 1280x720)
        ─▶ nvinfer(YOLO11m fp16) ─▶ nvtracker(IOU) ─▶ nvdsosd(GPU)
        ─▶ tee ─┬─▶ queue ─▶ fakesink                        [análise + probes]
                └─▶ queue(leaky,1) ─▶ nvvideoconvert ─▶ NV12
                    ─▶ nvv4l2h264enc ─▶ h264parse ─▶ rtspclientsink ─▶ MediaMTX /live
                                                                  ─▶ WHEP 8889 / HLS 8888 ─▶ browser
```

**[F] A análise termina em `fakesink`** (`pipeline_real.py:380`): o browser nunca vê o ramo
analítico, só o preview — que é descartável por desenho (`pipeline_real.py:16-21`).

**[F] Não existe fallback de vídeo no browser.** `app.js` não carrega `hls.js`, não referencia
`.m3u8` e não instancia player HLS: as portas 8889 (WHEP) e 8888 (HLS) são comparadas apenas
como **sonda de diagnóstico** de certificado/host (`app.js:68-80`). Se o WHEP não negocia, o
vídeo não aparece — a tela explica o motivo, mas não há segundo caminho. Isso é relevante para
avaliar R6: hoje, com `vision-mediamtx` fora, o dashboard fica sem vídeo e sem alternativa.

---

## 3. Fluxo de telemetria e controle (pipeline → JSONL → consumidores)

O JSONL é o **barramento de estado de fato** do sistema: três consumidores independentes o leem
sem qualquer import compartilhado.

```
pipeline_real.log_event() ──▶ stdout (primário) + data/logs/live.jsonl (best-effort)
                                        │
                    ┌───────────────────┼────────────────────┐
                    ▼                   ▼                    ▼
        dashboard/backend        scripts/watchdog.py    switch_source_lib
        tailer + 480/500         5 regras + crash-loop  pós-check da troca
        ├ GET /api/snapshot      └ docker.sock restart └ session_epoch
        ├ GET /api/history
        ├ GET /api/events
        └ WS /ws ──▶ browser
```

| Elemento | Evidência |
|---|---|
| Registro = `{"ts","event","source_id","session_epoch",...campos}` (plano) | `pipeline_real.py:215-220`; `source_state.py:188-215` |
| Namespace injetado por `with_namespace()` com `setdefault` (campo explícito vence) | `source_state.py:188` |
| Escrita **best-effort**: `except OSError: pass`, sem contador | `pipeline_real.py:226-227` |
| `interval_metrics` a cada 30 s, ~29 campos | `pipeline_real.py:132`, `:1047-1085` |
| Volumes: `../data:/app/data` RW no pipeline, `:ro` no dashboard | `deploy/docker-compose.yml:114-117`, `:156-157` |
| Replay do arquivo no startup + tailer tolerante a rotação (poll 0.5 s) | `dashboard/backend/app.py:132-166`, `:180-197` |
| Ring buffers: 480 métricas / 500 eventos | `dashboard/backend/app.py:34-35` |
| Saúde derivada: `down` (>270 s ou vazio), `stale` (>90 s), `degraded` (`fps_processed<=0`) | `dashboard/backend/app.py:94-106` |
| WS envia envelope `{"type":"snapshot"\|"record","data":...}` | `dashboard/backend/app.py:9`, `:169-178`, `:215-228` |

**[F]** O dashboard filtra a lista de eventos por whitelist **fechada de 14 nomes**
(`dashboard/backend/app.py:77-86`): `track_entered`, `track_exited`, `area_transition`,
`pipeline_error`, `pipeline_warning`, `eos`, `interrupted_by_user`, `test_duration_reached`,
`pipeline_stopped`, `pipeline_playing`, `source_active`, `preview_branch_ready`,
`rtsp_reconnect_error`, `preview_branch_error`. **[F]** O `live.jsonl` contém **29 tipos de evento
distintos** (contagem direta, 0 linhas corrompidas). Evento novo fora da lista é **descartado em
silêncio** na UI — os 15 tipos fora da whitelist incluem `preview_branch_failed`,
`preview_branch_retry*`, `preview_branch_restored`, `source_reconnect_action`,
`rtsp_reconnect_started`/`resumed`, `startup_failed`, `source_reconnect_budget_reset`.

---

## 4. Topologia de runtime

Um único `deploy/docker-compose.yml`, sem `networks:` explícita (bridge default), sem `version:`,
sem profiles. **[F]** 5 serviços:

| Serviço (linha) | Imagem | Container | Portas | Volumes | depends_on | Healthcheck |
|---|---|---|---|---|---|---|
| `mediamtx` (`:2`) | `bluenviron/mediamtx:1.21.3` | `vision-mediamtx` | `${LAN_IP}`: 8554, 1935, 8888, 8889, 8890/udp, 8189/udp+tcp | `mediamtx.yml`, `certs:ro` | — | **nenhum** |
| `capture-web` (`:31`) | `nginx:1.27-alpine` | `vision-capture-web` | `${LAN_IP}:8443` | `capture-web.conf`, `frontend:ro`, `certs:ro` | — | HTTP real em 8443 |
| `vision-app` (`:60`) | build `deploy/Dockerfile` (base `deepstream-l4t:7.1-triton-multiarch`) | `vision-stability-app` | **nenhuma** | `deepstream/`, `data/`, `scripts/` | `mediamtx` (sem `condition`) | mtime de `live.jsonl` < 180 s |
| `watchdog` (`:128`) | build `deploy/Dockerfile.watchdog` | `vision-watchdog` | — | `data/`, `/var/run/docker.sock` | `vision-app` | **nenhum** |
| `dashboard` (`:145`) | build `dashboard/Dockerfile` | `vision-dashboard` | `0.0.0.0:8081→8000` | `data:ro`, `dashboard/` | **nenhum** | **nenhum** |

**Privilégios** (`vision-app`): `init: true` (tini como PID 1, `:71`), `privileged: true`
(`:72`), `runtime: nvidia` (`:73`). **[F]** `privileged` é a única ocorrência no repo e **não há
comentário nem ADR justificando** — as demais linhas não triviais do compose são comentadas.
Tratado como exigência não verificada (ver `architecture-risks.md` R16).

**[F] Não há limite de memória em nenhum serviço** (`mem_limit`/`deploy.resources`/`shm_size`
ausentes do compose) — restrição relevante em Orin com memória unificada (ver
`architecture-risks.md` R21).

**[F] Publicação de portas é assimétrica:** toda a mídia (MediaMTX e 8443) está amarrada a
`${LAN_IP:-192.168.59.102}`; só o dashboard publica em `0.0.0.0` (`:15-21`, `:42` vs `:153`).
Consequência: `127.0.0.1:8889` não responde — o dashboard deve ser aberto pelo IP da LAN.

**MediaMTX** (`mediamtx/mediamtx.yml`): `rtspTransports: [tcp]` (`:22`) casado com
`protocols=4` do `rtspclientsink`; `webrtcEncryption: yes` com cert em `/certs` (`:31-33`);
`webrtcAdditionalHosts: [192.168.59.102]` (`:18`); `api: no` (`:35`); paths `live`, `drone`,
`phone` (`:37-43`).

### 4.1 Estado observado no host (2026-11-01, `docker ps -a`)

| Container | Status |
|---|---|
| `vision-stability-app` | `Up 4 minutes (healthy)` |
| `vision-watchdog` | `Up 2 hours` |
| `vision-dashboard` | `Up 2 hours` (`0.0.0.0:8081->8000`) |
| `vision-mediamtx` | **`Exited (255) 2 hours ago`** |
| `vision-capture-web` | **`Exited (255) 2 hours ago`** |

**[F]** Enquanto o MediaMTX esteve fora, o preview falhou e o sistema **continuou analisando** —
`live.jsonl` registra `preview_branch_failed` / `rtspclientsink0` / `Could not open resource`,
com `action: "ramo de preview derrubado; análise segue"`. Esse é exatamente o caminho
`ACTION_DROP_PREVIEW` (`bus_classifier.py`; `pipeline_real.py:648-653`, `:789-831`). O núcleo do
sistema degrada de forma controlada e **não** cai junto com o preview.

**[H]** A saída 255 de `mediamtx`/`capture-web` está correlacionada com essa falha, mas a causa
raiz não foi determinada (`docker inspect` não executado).

> Nota de contexto: os containers `arquo-*`/`financas-*`/`financial_*` no mesmo host são de outro
> projeto; `arquo-web` ocupa a porta 8080 (por isso o dashboard usa 8081).

---

## 5. Subsistemas transversais

### 5.1 Multifonte e troca de fonte (ADR-0006)

- **[F] Declaração:** `deepstream/config/sources.yaml` (68 linhas) — `camera` (H.265,
  `uri_env: RTSP_URI`, gate p95 300 ms), `drone` (H.264,
  `rtsp://${MTX_HOST:-mediamtx}:8554/drone`, gate `report_only`), `phone` (H.264, `.../phone`);
  `default_source: camera` (`:13-68`). `MAX_SOURCES = 3` (`sources.py:62`), validado em
  `sources.py:490-493`.
- **[F] Precedência de seleção:** `ACTIVE_SOURCE` (env) > `data/config/active_source` >
  `default_source` > 1ª `enabled` (`sources.py:572-600`). Id inválido/desabilitado e arquivo
  ilegível são **erro explícito**, nunca fallback silencioso (`sources.py:537-571`).
- **[F] Rollback nível 1:** `VSL_SOURCE_SELECTION=off` força o caminho legado
  `RTSP_URI`+`RTSP_CODEC` (`sources.py:276-278`, `:471-481`).
- **[F] Troca = restart** (`scripts/switch_source.sh`), com prova por `session_epoch` vs
  `StartedAt` (`source_state.py:1-30`). Estado de visão não atravessa fontes.
- **[F] Silenciamento do supervisor:** marcador `data/config/switch_in_progress` desliga Regra 1
  e crash-loop durante a recriação (`scripts/watchdog.py:12-19`, `:755-790`).

### 5.2 Reconexão da fonte (intra-processo)

- **[F]** O bus **classifica** e, para `ACTION_RECONNECT`, apenas **agenda** (`pipeline_real.py:646-651`);
  a execução da reconexão acontece em timer (`:901-971`; racional em `source_reconnect.py:1-35`).
  **Exceção [F]:** `ACTION_DROP_PREVIEW` dispara `_drop_preview_branch()` **inline no callback**
  (`:653-655` → `:745-769`: `pad.unlink`, `release_request_pad`, `set_state(NULL)`,
  `pipeline.remove`). O teardown do preview é rápido e protegido por `try/except`, mas é trabalho
  de GStreamer no thread do bus — possibilidade de bloqueio do loop não está medida.
- **[F]** Execução em fatias de 250 ms por timer GLib (`pipeline_real.py:901-971`), com backoff
  exponencial 10→45 s e orçamento de tentativas (`source_reconnect.py:36-50`;
  `source_budget.py`).
- **[F]** EOS da fonte é **contido no tee** (`pipeline_real.py:430-433`, `:471-496`) para não
  matar o pipeline.
- **[F]** Watchdog de liveness: `_check_rtsp_alive` a cada 5 s cobre "nenhum frame por 30 s"
  (`pipeline_real.py:999-1015`, `:1120`, `:152`).
- **[F]** Esgotar o orçamento → `EXIT_SOURCE_LOST=2` (`pipeline_real.py:201`, `:966-967`).

### 5.3 Reconexão do preview

- **[F]** Elementos são **recriados do zero** a cada tentativa, por decisão explícita
  (`pipeline_real.py:669-678`): `rtspclientsink` que errou não volta a publicar em NULL→PLAYING.
- **[F]** Backoff 15→300 s, teto 6 tentativas (`:185-196`); restauração exige ≥5 frames
  (`:192-194`); sete eventos `preview_branch_*` (`:779-857`).

### 5.4 Watchdog (supervisor externo)

**[F]** Processo separado, 888 linhas, acessa `/var/run/docker.sock` por HTTP cru (sem SDK)
(`watchdog.py:154-255`).

| Regra | Gatilho | Ação |
|---|---|---|
| 1 | 60 s sem frame → 180 s → restart | restart do container (`:70-76`, `:791-823`) |
| crash-loop | ≥5 restarts em 900 s **com** corroboração de fonte morta | restart escalado (`:78-90`, `:700-754`) |
| 2 | FPS < 30 % do alvo por 120 s | registra degradação, **sem** restart (`:95-97`) |
| 3 | RSS > 15 % acima da baseline (após 300 s) | alerta (`:99-101`) |
| 4 | RAM do sistema > 90 % por 3 confirmações | `docker restart` (`:103-105`) |
| 5 | Erro CUDA fatal nos logs | restart (`:110-115`, `:839-851`) |
| extra | `container_missing` (404) | registra, **não** escala (`:21-24`) |

Saída: `data/logs/watchdog.jsonl`.

### 5.5 Shutdown gracioso

**[F]** `_boot_stop` é instalado **antes** de `gi`/`pyds`/TensorRT para cobrir a janela de boot
(`pipeline_real.py:48-73`); `GracefulShutdown.install()` usa `GLib.unix_signal_add`
(`graceful_shutdown.py:1-24`); `_teardown` roda `set_state(NULL)` em thread daemon com teto de
1,5 s e emite `pipeline_stop_timeout` em vez de arriscar exit 137
(`pipeline_real.py:1153-1181`). Exit codes: `0` limpo, `1` erro de pipeline, `2` fonte perdida.
`init: true` no compose garante entrega do `SIGTERM`.

---

## 6. Divergências entre `docs/architecture.md` e o código

`docs/architecture.md` (90 linhas, **2026-08-23**, "Fase 4 concluída") antecede toda a onda
multifonte (ADR-0006, 2026-10-30) e a Fase 5/6.

| # | Documento diz | Código faz hoje | Evidência |
|---|---|---|---|
| D1 | `protocols=tcp+udp` | `protocols` vem da fonte (`transport: tcp` → 4) | `pipeline_real.py:352`; `sources.py:109-111`; `sources.yaml:24` |
| D2 | Codec alternável via env `RTSP_CODEC` | Codec é **da fonte**; env só no fallback legado | `pipeline_real.py:287-288`; `sources.py:632-646`, `:276-278` |
| D3 | `tracker_state.py` como componente ativo | **Legado**; runtime usa só `AreaMonitor` | `pipeline_real.py:83` |
| D4 | 3 containers | **5** (+`capture-web`, +`watchdog`) | `deploy/docker-compose.yml:2,31,60,128,145` |
| D5 | Sem reconexão/orçamento/`exit 2`/preview retry | `source_reconnect.py`, `source_budget.py`, `bus_classifier.py` | módulos citados |
| D6 | Sem troca de fonte / `sources.yaml` / `session_epoch` | Multifonte completo | `sources.yaml`; `source_state.py:85`; `switch_source.sh` |
| D7 | Sem watchdog | `watchdog.py` como container próprio | `deploy/docker-compose.yml:128-144` |
| D8 | `tegrastats_reader` "uso planejado" | Continua **não integrado** ao pipeline; é importado apenas pelos **legados** (`pipeline.py:12`, `pipeline_rtsp_real.py:12`) | `scripts/phase5_run.py` tem parser local próprio, não importa o módulo |
| D9 | Telemetria sem `run`/whitelist | Backend particiona por `run` e filtra eventos | `dashboard/backend/app.py:42-84`, `:77-86` |
| D10 | TLS/captura só em prosa | `capture-web` é serviço próprio; `webrtcEncryption: yes` | `deploy/docker-compose.yml:31-58`; `mediamtx.yml:31-33` |

**O que o documento ainda acerta:** a cadeia GStreamer (§2), `batch=1`, 1280x720, `nvdsosd
process-mode=1`, o `tee` com ramo leaky, `rtspclientsink` como default, o path do JSONL, os ring
buffers 480/500, as rotas REST/WS, as portas 8081/8888/8889/8443 e a ausência de persistência
além do JSONL.

---

## 7. Fatos, hipóteses e lacunas

### Fatos (resumo verificado)
1. Entrypoint real = `deepstream/pipeline_real.py` (`deploy/Dockerfile:29`).
2. 5 serviços no compose; só o dashboard publica em `0.0.0.0`.
3. `batch_size=1` em runtime e na config de inferência.
4. O único caminho de vídeo ao browser é o ramo de preview (leaky); **não há fallback HLS de
   vídeo**, só sonda de diagnóstico.
5. EOS da fonte contido no tee; `ACTION_RECONNECT` é apenas agendado — **mas o teardown do
   preview roda inline no callback do bus**.
6. JSONL append-only, plano, sem schema, é a única persistência; **29 tipos de evento distintos**.
7. O dashboard mantém whitelist fechada de **14** eventos — 15 tipos nunca chegam à UI.
8. O watchdog roda fora do processo com poder de restart via `docker.sock`.
9. Multifonte: precedência determinística e fail-closed.
10. `docs/architecture.md` está desatualizado em 10 pontos (tabela §6).
11. **Nenhum serviço tem limite de memória** no compose, apesar de memória unificada no Orin.
12. **13 arquivos de teste / 420 funções**; 11 usam `sys.path.insert`, 2 não.

### Hipóteses
- **[H]** Saída 255 de `mediamtx`/`capture-web` é causa do `preview_branch_failed` observado;
  causa raiz não investigada.
- **[H]** `dashboard` sem `depends_on` pode iniciar antes do JSONL existir; o tailer parece
  tolerar (`app.py:137-141`), mas o cenário não foi executado.
- **[H]** `requirements.txt` instala `fastapi`/`uvicorn`/`pydantic` na imagem do pipeline sem uso
  aparente dentro do `vision-app`.
- **[H]** SRT/MoQ internos do MediaMTX (8892/8893) não publicados é intencional.
- **[H]** `PREVIEW_TRANSPORT=udp` segue funcional, mas o MediaMTX não ingere RTP/UDP puro.

### Lacunas
1. **Latência fim a fim não instrumentada** — `gate.latency_p95_ms` existe no YAML
   (`sources.yaml:34-35`, `:52-54`, `:67-68`) **sem consumidor no código**.
2. **Sem consumidor de `switch.jsonl`** e de `drone-campo-ingest.jsonl` além do próprio shell.
3. **Rede Docker nominal/subnets** não inspecionadas.
4. **`source.yaml`/`osd.yml`/`tracker_iou.ini`/`tracker.yml.legacy`** sem leitor em código.
5. **`pytest` não executado** nesta análise — não se afirma que a suíte passa.
6. **Engine FP16 e parser `.so`** sem verificação de correspondência com o `.onnx` atual.
7. **`tegrastats` em produção** — não se sabe se `mem_budget.sh` é o caminho oficial de campo.
8. **Crescimento do `live.jsonl`** — o arquivo cobre 2026-08-23 → 2026-11-01 (≈4,2 MB) sem
   rotação em nenhum script; o dashboard replaya o arquivo **inteiro** no boot
   (`dashboard/backend/app.py:132-166`). Não há política de retenção nem medição de tempo de boot
   com arquivo grande.
9. **Causa raiz da saída 255** de `mediamtx`/`capture-web` não investigada (`docker inspect`).
