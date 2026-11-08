# Dependency Map — Vision Stability Lab

> **Natureza:** mapa de dependências e acoplamento do estado ATUAL.
> **Data:** 2026-11-01 · **Commit:** `cd5bbfb` · **Branch:** `feature/multifonte`
> **Método:** Graphify (1734 nós / 3096 arestas / 0 ciclos) → confirmação de imports reais →
> leitura pontual. Nenhuma dependência inferida por nome de arquivo; inferências marcadas **[H]**.

---

## 1. Dependências internas (imports reais)

Direção `A → B` significa "A importa B". Todas as arestas abaixo foram confirmadas por leitura.

### 1.1 Camada de aplicação — `deepstream/`

```
pipeline_real.py:83-94  ──▶ area_monitor, bus_classifier, graceful_shutdown, health_metrics,
                            source_budget, source_reconnect, source_state, sources   [PRODUÇÃO]
sources.py:47           ──▶ rtsp_env
app.py:4                ──▶ pipeline (main)                                          [LEGADO]
pipeline.py:10-12       ──▶ tracker_state, health_metrics, tegrastats_reader          [LEGADO]
pipeline_rtsp_real.py:10-13 ──▶ tracker_state, health_metrics, tegrastats_reader, rtsp_env [LEGADO]
rtsp_*_test.py (6) :8   ──▶ rtsp_env                                                [EXPERIMENTO]
```

### 1.2 Camada operacional — `scripts/`

```
switch_source_lib.py:50-54 ──▶ (sys.path.insert em deepstream/) ──▶ deepstream/sources.py
probe/ingest_report.py:54-56 ──▶ (sys.path.insert em probe/) ──▶ ingest_report_lib.py
watchdog.py                  ──▶ (nenhum import interno; arquivo único de 888 linhas)
probe/rtsp_read_probe.py, probe/whip_probe.py ──▶ (nenhum import interno)
```

### 1.3 Camada de apresentação — `dashboard/`

```
dashboard/backend/app.py:15-26 ──▶ fastapi, fastapi.responses, fastapi.staticfiles, stdlib
                                    NENHUM import de deepstream/ ou scripts/
```

### 1.4 Testes

**13 arquivos** de teste, 420 funções `def test_`. **11 dos 13** injetam
`sys.path.insert(0, <dir de produção>)`: `test_sources.py:23-26`, `test_source_state.py:26-29`,
`test_area_monitor.py:5-7`, `test_bus_classifier.py:16-18`, `test_source_reconnect.py:23-26`,
`test_source_budget.py:17-19`, `test_graceful_shutdown.py:31-33`,
`scripts/tests/test_switch_source.py:35-37`, `test_watchdog_sources.py:28-30`,
`test_ingest_report.py:16-18`, `test_probe_logic.py:19-22`.

Os **2 que não usam** são justamente os mais acoplados: `test_pipeline_source_wiring.py` (lê
código como texto) e `test_no_hardcoded_secrets.py` (depende de `git ls-files`). Ou seja, os dois
arquivos que escapam do `sys.path` são os que não testam comportamento.

Não há `pyproject.toml`, `setup.py` ou `conftest.py`: **o layout de diretórios é o contrato de
importação**.

### 1.5 Profundidade e ciclos

- Profundidade máxima: **3** (`pipeline_real → sources → rtsp_env`;
  `switch_source_lib → sources → rtsp_env`).
- **Ciclos: 0.** Todos os módulos "folha" (`source_state`, `source_budget`, `source_reconnect`,
  `area_monitor`, `bus_classifier`, `graceful_shutdown`, `health_metrics`, `tegrastats_reader`,
  `tracker_state`, `rtsp_env`) importam apenas stdlib.
- **Salvaguarda de pureza:** módulos puros não tocam `gi`/`pyds`. `sources.py:284` faz
  `import yaml` local justamente para o fallback legado não exigir pyyaml. A pureza é garantida
  por **teste textual** (`test_sources.py:639-642` — `assertNotIn("import gi", codigo)`), não por
  fronteira de módulo.

---

## 2. Hubs de acoplamento

### 2.1 Fan-in (quem é importado por muitos)

| Módulo | Importado por | Papel |
|---|---|---|
| `deepstream/rtsp_env.py` | `sources.py:47` (produção) + 6 `rtsp_*_test.py` + `pipeline_rtsp_real.py:13` (legado) | **Hub de borda** — único leitor de `RTSP_URI` |
| `deepstream/sources.py` | `pipeline_real.py:94`, `switch_source_lib.py:54`, 3 arquivos de teste | **Hub de contrato de configuração** |
| `deepstream/health_metrics.py` | `pipeline_real.py:88`, `pipeline.py:11`, `pipeline_rtsp_real.py:11` | Compartilhado entre produção e 2 legados |
| `deepstream/source_state.py` | `pipeline_real.py:93`, `test_source_state.py` | **Hub de contrato de telemetria** |
| `area_monitor`, `bus_classifier`, `source_budget`, `source_reconnect`, `graceful_shutdown` | apenas `pipeline_real.py` (+ teste próprio) | Puros, fan-in 1 — baixo risco de mudança |

### 2.2 Fan-out (quem depende de muitos) — o problema central

**`deepstream/pipeline_real.py`**: 1215 linhas, 8 imports internos, **uma classe
`VisionPipeline` com 31 métodos** (`:255-1182`) concentrando: construção da cadeia GStreamer,
link dinâmico de pad (`:446`), 4 probes (`:455`, `:467`, `:471`, `:497`), classificação de bus
(`:607`), ciclo de vida do preview (9 métodos, `:670-869`), máquina de reconexão (`:870-971`),
liveness RTSP (`:999`), métricas (`:1016`) e shutdown/teardown (`:1089-1215`).

É o único ponto onde os 8 módulos puros convergem — e é o produtor de 100% dos eventos que três
consumidores independentes parseiam.

---

## 3. Contratos entre componentes

### C1 — Telemetria JSONL (`data/logs/live.jsonl`)

| | |
|---|---|
| **Produtor** | `pipeline_real.log_event()` (`:204-227`) |
| **Consumidores** | (a) dashboard `tail_telemetry` (`app.py:132-166`); (b) watchdog `JsonlTail` (`watchdog.py:259`); (c) `switch_source_lib.read_new_records` (`:344`); (d) healthcheck do compose — **só mtime** (`docker-compose.yml:122`) |
| **Acoplamento** | **forte por dado, fraco por código** (nenhum import entre produtor e consumidores) |

**Formato efetivo** (uma linha JSON, plano):
```
{"ts": "<ISO-8601 UTC, ms>", "event": "<nome>", "source_id": "<id>", "session_epoch": "<ISO>", ...campos}
```
- Base: `ts` + `event` (`pipeline_real.py:215-219`).
- Namespace injetado por `with_namespace()` com `setdefault` (`source_state.py:188-215`); sem
  sessão, o registro sai sem namespace (`pipeline_real.py:114`).
- `interval_metrics`: ~29 campos (`pipeline_real.py:1047-1074`).
- Escrita **best-effort**: `except OSError: pass` (`:226-227`), sem contador de perda.

**Riscos de contrato (fatos):**
1. **Sem schema, sem `schema_version`, sem validação** em nenhum consumidor.
2. **Vocabulário de evento duplicado em 3 lugares**: emissor (`pipeline_real.py`), whitelist
   fechada de **14 nomes** (`dashboard/backend/app.py:77-86`), prefixos de falha
   (`switch_source_lib.py:78-84`).
3. **Documentação divergente**: `README.md:329-390` descreve um contrato **aninhado**
   (`timestamp`, `device_id`, `pipeline_id`, `source{}`, `vision{}`, `process{}`, `jetson{}`,
   `health{}`) que **ninguém emite** — nenhum `.py` produz `jetson{}` (os pipelines legados
   logam texto puro; `phase5_run.py:208` usa a chave plana `jetson_ram_used_mb`). O formato real
   é plano.
4. Compatibilidade é **aditiva por disciplina**, não por contrato (`pipeline_real.py:205-213`;
   `source_state.py:194-205`; `switch_source_lib.py:299-310`).

### C2 — Auditoria de troca (`data/logs/switch.jsonl`)

- **Produtor:** `switch_source_lib.switch_record()` (`:525-556`), gravado por
  `switch_source.sh:64`.
- **Consumidores em código: nenhum** — auditoria para humano.
- Formato: `{ts, event:"switch_source", from, to, probe, result, reason}`; `probe ∈
  {ok,skipped,blocked,failed}` é enum de docstring, **não validado** (`:529-534`).
- **Acoplamento fraco**; risco: formato diverge sem ninguém notar.

### C3 — Marcadores de controle

| Arquivo | Formato | Produtor | Consumidor | Acoplamento |
|---|---|---|---|---|
| `data/config/active_source` | 1 linha (id); aceita BOM/CRLF/`#` | `switch_source.sh:61` (escrita atômica); operador pode editar | `sources.read_active_source()` (`:537-572`) | **Forte, unidirecional, fail-closed** (arquivo ilegível = erro, `:557-570`) |
| `data/config/switch_in_progress` | `{"to","expires_at"}` | `switch_source.sh:62` (removido por trap) | `watchdog.read_switch_marker()` (`:369-421`) | **Fraco e tolerante por construção**: ausente/expirado/corrupto → `(False, motivo)`; marcador malformado **nunca** suprime alerta |
| `session_epoch` (namespace, não arquivo) | ISO de `STARTED_AT` | `SessionState.new()` (`source_state.py:118-131`, sobre `next_epoch` `:63-83`) | `switch_source_lib.process_boundary`/`sample_is_from_process` (`:278`, `:295`) | **Forte** — impede aprovar troca com amostra do processo anterior; tolerância de relógio 2,0 s (`:65`) |

### C4 — HTTP/WS do dashboard

| Rota | Produtor | Contrato |
|---|---|---|
| `GET /api/snapshot` (`app.py:199`) | `DashboardState.snapshot()` (`:108-125`) | Sem schema; payload ad hoc (`health`, `latest_metrics`, `error_count`, `events_recent`) |
| `GET /api/history?minutes=N` (`:204`) | ring buffer 480 (`:34`) | Sem schema; filtra por `recv_at` |
| `GET /api/events?limit=N` (`:210`) | deque 500 (`:35`) | Sem schema |
| `WS /ws` (`:215`) | `broadcast()` (`:169`) | Envelope `{"type":"snapshot"\|"record","data":...}` — **único contrato explícito**, só em código |
| `GET /healthz` (`:230`) | constante `{"ok": true}` | **Não reflete saúde real**; não usado pelo compose |
| `GET /`, `/static` (`:235`, `:240`) | bind mount `../dashboard` | Acoplado ao layout de diretório |

**Deriva de limiar (duplicação de conhecimento):** "telemetria velha" tem **3 definições**:
90 s no dashboard (`app.py:37`), 180 s no healthcheck do compose (`docker-compose.yml:122`),
60 s no watchdog (`watchdog.py:73-75`). E `down` usa `STALE_AFTER_S * 3` = 270 s (`app.py:96-98`).

**[F] Superfície de exposição:** o dashboard é o **único** serviço publicado em `0.0.0.0`
(`compose:153`) e **não tem autenticação nem TLS** (nenhum middleware; apenas GET + WS em
`app.py:199-240`). As portas de mídia são amarradas a `${LAN_IP}`, mas o dashboard não. Não há
ADR tratando disso (ver `architecture-risks.md` R22).

**[F] Sem rotação de telemetria:** `live.jsonl` cresce indefinidamente (2026-08-23 → 2026-11-01,
≈4,2 MB) e o backend replaya o arquivo inteiro no boot (`app.py:132-166`); nenhum script faz
rotação ou retenção (ver `architecture-risks.md` R23).

### C5 — Paths do MediaMTX

| Path | Produtor | Consumidor | Evidência |
|---|---|---|---|
| `live` | `pipeline_real.py:696-699` (`rtspclientsink`) | dashboard via WHEP `https://<host>:8889/live/whep` | `mediamtx.yml:39`; `pipeline_real.py:139`; **hardcoded** em `app.js:100` |
| `drone` | externo (RTMP push) | `sources.yaml:41` → `rtsp://${MTX_HOST:-mediamtx}:8554/drone` | `mediamtx.yml:41` |
| `phone` | `capture.js` (`DEFAULT_PATH`, `:50`; `whipUrl()`, `:152-167`) | `sources.yaml:57` | `mediamtx.yml:43`; `whip_probe.py:279` |

**Acoplamento forte sem constante compartilhada:** o par `(path, codec)` precisa casar entre
`sources.yaml` e `mediamtx.yml`; `codec` é declarado só no YAML de fontes (`camera: h265`,
`drone: h264`, `phone: h264`). Rename de path quebra **em runtime**, sem erro de build.
`PREVIEW_TRANSPORT` muda o tipo de sink (`pipeline_real.py:695-708`) — o path `live` só existe
no modo `rtsp`.

### C6 — Variáveis de ambiente

| Grupo | Quantidade | Onde |
|---|---|---|
| Pipeline (`vision-app`) | **33** (27 em `pipeline_real.py:132-199` + 6 constantes em `sources.py:54-59`) | `MTX_HOST` entra por expansão `${VAR}` no YAML (`sources.py:184`), não por `os.environ` |
| Watchdog | **22** (`watchdog.py:56-110`) | Inclui 16 limiares de regra |
| Dashboard | **5** (`app.py:32-37`) | **Nenhuma é repassada pelo compose** exceto `TELEMETRY_FILE` (`:155`) |
| Infra | `LAN_IP`, `DASHBOARD_PORT`, `COMPOSE_FILE`, `ENV_FILE`, `VSL_LAN_IP` | compose / certs |

**Risco de deriva:** defaults duplicados em compose `${VAR:-x}` (`:96-113`) e Python
`os.environ.get("VAR","x")` (`pipeline_real.py:132-186`). Ex.: `SOURCE_RECONNECT_MAX_ATTEMPTS`
= 5 nos dois lugares (`compose:107` / `pipeline_real.py:158-159`) — se um mudar, o comportamento
diverge conforme o container suba pelo compose ou direto.

**Sem `.env` versionado** (correto por segurança), mas então `deploy/.env.example` é a única
lista de variáveis obrigatórias; erro só aparece em runtime (`rtsp_env.py:47`).

---

## 4. Direções de dependência invertidas ou proibidas

1. **Script operacional → aplicação.** `scripts/switch_source_lib.py:50-54` faz
   `sys.path.insert(0, REPO_ROOT/"deepstream")` + `import sources`. A camada de operação depende
   do código da aplicação. **Consequência:** `sources.py` não pode ganhar `gi`/`pyds` sem quebrar
   a CLI no host — e essa restrição é garantida apenas por teste textual
   (`test_sources.py:639-642`).
2. **Teste → produção por caminho mágico.** 11 de 13 arquivos de teste manipulam `sys.path`;
   mover um arquivo quebra a suíte. Não há pacote instalável.
3. **Teste da aplicação → infraestrutura.** `test_pipeline_source_wiring.py` lê
   `deploy/docker-compose.yml` e `scripts/mem_budget.sh` como texto a partir de `deepstream/`.
4. **Duas camadas lendo o mesmo arquivo sem contrato comum** (dashboard e watchdog): acoplamento
   por dado, invisível ao grafo.

---

## 5. Dependências externas

### 5.1 Pip

`requirements.txt:1-6` (imagem principal, Python 3.10 do container DeepStream):

| Dependência | Usada de fato? | Evidência |
|---|---|---|
| `pydantic>=2.0` | **NÃO** | nenhum import |
| `pyyaml>=6.0` | sim (lazy) | `sources.py:284` |
| `requests>=2.28.0` | **NÃO** | `requests` no repo é nome de variável (`test_graceful_shutdown.py:72`) |
| `psutil>=5.9.0` | sim | `health_metrics.py:2` |
| `mqtt>=0.0.1` | **NÃO** | nenhum import |
| `paho-mqtt>=1.6.1` | **NÃO** | nenhum import |

`dashboard/backend/requirements.txt:1-2`: `fastapi>=0.111`, `uvicorn[standard]>=0.30` — ambos
usados (`app.py:24-26`).

**[F] 4 de 6 dependências pip são fantasma** — e, mais grave que o custo de build, sugerem uma
**arquitetura MQTT que não existe** (nenhum broker no compose).

### 5.2 Plataforma (não-instalável via pip)

| Dependência | Versão | Onde fixada | Qualidade da fixação |
|---|---|---|---|
| NVIDIA DeepStream SDK | 7.1 | `deploy/Dockerfile:1` | **Tag sem digest** |
| `pyds` | 1.2.0 (cp310/aarch64) | wheel vendorizado `deploy/wheels/`, instalado em `Dockerfile:23-25` | **Boa** (binário no repo) |
| TensorRT | herdada da imagem | **nenhum arquivo** | **NÃO fixada** |
| CUDA | herdada da imagem | **nenhum arquivo** | **NÃO fixada** |
| GStreamer | 1.20.3 (medido) | só em `docs/phase-2-evaluation.md:8`; plugins via `apt-get` sem versão (`Dockerfile:5-13`) | **NÃO fixada** |
| JetPack / L4T | R36.4.7 | só em prosa (`adr-0006:6`, `phase-1-evaluation.md:9`) | **Só documentada** |
| Driver NVIDIA + Container Toolkit | não declarada | `deploy/docker-compose.yml:73` (`runtime: nvidia`) | **NÃO fixada** (dependência do host) |
| MediaMTX | 1.21.3 | `docker-compose.yml:6` (pin justificado por ICE) | **Boa** |
| nginx | 1.27-alpine | `docker-compose.yml:36` | Tag, sem digest |
| Python (dashboard/watchdog) | 3.11-slim | `dashboard/Dockerfile:1`, `Dockerfile.watchdog:1` | Tag, sem digest |
| Modelo YOLO11m | — | `infer.txt:4` → `.onnx`; `:5` → engine | **Engine não versionado** (`.gitignore`: `data/*.engine`) |
| Parser custom | — | `data/parser/libnvdsinfer_custom_impl_Yolo.so` (`infer.txt:15`) | **Binário não versionado** |

**Assimetria central [F]:** o que é Python puro está razoavelmente fixado; o que é **plataforma
de GPU** (TensorRT, CUDA, GStreamer, driver, L4T) **não é fixado em nenhum artefato executável** —
só em prosa. Um host com outro L4T constrói e sobe, e falha em runtime.

---

## 6. Dependências de deploy / infraestrutura

### 6.1 Serviços

| Serviço | Imagem | Onde é exposto | depends_on | Healthcheck |
|---|---|---|---|---|
| `mediamtx` | `bluenviron/mediamtx:1.21.3` | `${LAN_IP}`: 8554/1935/8888/8889/8890udp/8189 | — | **nenhum** |
| `capture-web` | `nginx:1.27-alpine` | `${LAN_IP}:8443` | — | HTTP real em 8443 |
| `vision-app` | build `deploy/Dockerfile` | nenhuma (interna) | `mediamtx` — **sem `condition`** | mtime de JSONL < 180 s |
| `watchdog` | build `deploy/Dockerfile.watchdog` | — | `vision-app` — sem `condition` | **nenhum** |
| `dashboard` | build `dashboard/Dockerfile` | **`0.0.0.0:8081`** | **nenhum** | **nenhum** |

`depends_on` sem `condition` = **ordem de start, não readiness**.

### 6.2 Bind mounts — contrato implícito de operação

```
vision-app : ../deepstream → /app/deepstream, ../data → /app/data, ../scripts → /app/scripts
dashboard  : ../data → /app/data:ro, ../dashboard → /app/dashboard
mediamtx   : ../mediamtx/mediamtx.yml → /mediamtx.yml, ../deploy/certs → /certs:ro
capture-web: ../deploy/nginx/capture-web.conf:ro, ../dashboard/frontend:ro, ../deploy/certs:ro
watchdog   : ../data → /app/data, /var/run/docker.sock
```

**[F]** Os bind mounts **sobrescrevem** `COPY . /app` (`deploy/Dockerfile:27`) e
`COPY dashboard/ /app/dashboard/` (`dashboard/Dockerfile:9`). Consequência operacional: editar
`.py` não exige rebuild; editar `requirements.txt` **exige**. Não está documentado no compose.

**[F] Consequência de segurança:** `../deepstream` e `../scripts` são montados **RW** (`:115-117`)
dentro de um container `privileged: true` (`:72`). Quem comprometer o pipeline escreve no código
do host que o próximo `docker compose up` executa. Não há revisão de privilégio registrada (ver
`architecture-risks.md` R22).

### 6.3 TLS / CA interna

**[F] O que quebra sem `deploy/certs`:**
1. `mediamtx` — `webrtcEncryption: yes` (`mediamtx.yml:31-33`): sobe e cai na primeira conexão
   WebRTC.
2. `capture-web` — nginx falha ao carregar o certificado: **o container não sobe** (por isso o
   healthcheck bate em 8443 de verdade, `compose:50-54`).
3. Página de captura no celular (`getUserMedia` exige contexto seguro) e WHIP HTTPS.
4. Dashboard: WHEP em `https://<host>:8889/live/whep` — sem CA confiável no **navegador**, o
   preview não toca (`app.js:138-149`).

**[F]** O IP da LAN entra no SAN do certificado (`generate-certs.sh:188`) e no `server_name` do
nginx (`capture-web.conf:22`): **trocar de rede invalida a CA já instalada nos celulares**.
`deploy/certs/` é gitignored por inteiro → clone limpo não sobe `capture-web`. Não há script de
bootstrap documentado no compose.

### 6.4 Recursos externos ao repositório

| Recurso | Exigido por | Situação |
|---|---|---|
| `data/model_b1_gpu0_fp16.engine` (43,8 MB) | `infer.txt:5` | gitignored (`data/*.engine`); sem ele `nvinfer` falha em runtime |
| `data/parser/libnvdsinfer_custom_impl_Yolo.so` | `infer.txt:15` | não versionado |
| `deploy/.env` com `RTSP_URI` | `sources.yaml` / `rtsp_env` | gitignored, obrigatório na prática |
| `deploy/certs/*` | MediaMTX, nginx, browser | gitignored |

---

## 7. Análise de impacto (blast radius)

| Mudança em | Afetados diretos | Afetados transitivos |
|---|---|---|
| `load_sources()` / `SourceConfig` / `sources.yaml` | boot do pipeline; `switch_source_lib.resolve_source`/`precheck_url`; 61 testes de `test_sources.py`; 69 de `test_switch_source.py` | **Toda** troca de fonte; telemetria `source_active`; dashboard; watchdog |
| `VisionPipeline` (31 métodos) | 4 probes, bus, preview (9 métodos), reconexão (7), métricas, shutdown | **Todos** os consumidores de telemetria (nomes de evento e campos nascem aqui) |
| `SessionState` / `with_namespace` | `log_event` (todo registro); `process_boundary`/`sample_is_from_process` | Provas de troca de fonte; histórico do dashboard (campo `run`) |
| `log_event()` | 100% dos eventos; healthcheck depende do mtime | dashboard (todas as rotas), watchdog (todas as regras), switch (pós-check) |
| `AreaMonitor` | `_on_processed_buffer` | `area_state`/entries/exits/dwell na telemetria → dashboard + watchdog |
| Paths MediaMTX | `rtspclientsink`, `app.js`, `sources.yaml`, `capture.js`, `whip_probe.py` | Preview e ingestão de drone/celular — **falha só em runtime** |

---

## 8. Duplicação de conhecimento (4 ocorrências confirmadas)

1. **IP da LAN `192.168.59.102` em 6 lugares:** `mediamtx.yml:18`, `compose:15-21,42`,
   `capture-web.conf:22`, `switch_source_lib.py:60`, `generate-certs.sh:63`, `.env.example`.
2. **Ids de fonte (`camera`/`drone`/`phone`) em:** `sources.yaml`, `mediamtx.yml:39-43`,
   `capture.js:50`, `whip_probe.py:279`, `active_source`.
3. **Vocabulário de eventos em 3 camadas:** emissor, whitelist de 14 no dashboard, prefixos de
   falha no switch.
4. **Limiar de "telemetria velha" em 4 valores:** 90 s (`app.py:37`), 270 s (`app.py:96-98`),
   180 s (`compose:122`), 60 s (`watchdog.py:73-75`).

---

## 9. Fatos, hipóteses e lacunas

### Fatos
1. `pipeline_real.py:83-94` → 8 módulos internos; maior fan-out do repo. `app.py:4` importa o
   **legado** `pipeline`, não `pipeline_real`.
2. `deploy/Dockerfile:29` é o entrypoint real.
3. `switch_source_lib.py:50-54` → `deepstream/sources.py` (direção script → aplicação).
4. `dashboard/backend/app.py:15-26` não importa nada de `deepstream/` nem `scripts/`.
5. `pipeline_real.py:254-1182` — classe única, 31 métodos, 1215 linhas.
6. Zero ciclos de import.
7. `deepstream/config/source.yaml` sem leitor (só `README.md:654`).
8. `tegrastats_reader.py` importado só por `pipeline.py:12` e `pipeline_rtsp_real.py:12`.
9. 4 de 6 deps pip não são importadas.
10. DeepStream 7.1 só por tag; TensorRT/CUDA/GStreamer/driver/L4T sem fixação executável.
11. JSONL sem schema/versão; whitelist de 14 eventos; `switch.jsonl` sem consumidor.
12. `active_source` fail-closed; marcador de troca fail-open (nunca suprime alerta).
13. Portas de mídia em `${LAN_IP}`; dashboard em `0.0.0.0`.
14. `depends_on` sem `condition`; `mediamtx` e `dashboard` sem healthcheck.
15. Healthcheck do `vision-app` depende de **mtime** < 180 s.
16. `watchdog` monta `docker.sock`; `vision-app` roda `privileged`.
17. Bind mounts sobrescrevem o conteúdo copiado na imagem.
18. `deploy/certs` gitignored; SAN amarrado ao IP da LAN.
19. `test_pipeline_source_wiring.py` (39 testes) assere **strings literais** de
    `pipeline_real.py` e `docker-compose.yml`.
20. `test_sources.py:631-633` verifica a existência de **outro arquivo de teste**;
    `:639-642` verifica ausência textual de `import gi`.

### Hipóteses
- **[H]** Os 4 `pipeline*`/legados e 8 scripts de bancada são resíduos sem uso operacional.
- **[H]** O contrato aninhado do `README.md` é a spec original pré-implementação, substituída
  informalmente pelo formato plano.
- **[H]** `source.yaml` é template herdado do `deepstream-app`.
- **[H]** O watchdog não valida `session_epoch` por decisão consciente (decide por métricas e
  recriação), sem comentário explicitando.
- **[H]** O acoplamento de teste por texto foi escolha deliberada para testar sem GPU, sem ADR.

### Lacunas
1. Sem JSON Schema, dataclass de evento ou validação — regressão de campo só é detectável
   inspecionando 3 consumidores.
2. Sem `schema_version`; compatibilidade inferida pela ausência de `session_epoch`
   (`switch_source_lib.py:299-310`).
3. `/api/snapshot` e `/ws` sem contrato publicado; frontend é o único consumidor e não há teste
   de contrato.
4. Nenhum teste de contrato produtor→consumidor.
5. `/healthz` do dashboard é constante e não é usado pelo compose.
6. Nenhum teste de integração da stack (compose up → telemetria → dashboard).
7. Versões de plataforma não fixadas e não registradas em artefato versionado.
8. Nenhuma imagem por digest; `apt-get` sem versão.
9. Dependências fantasma sem justificativa registrada (não há ADR/plano de MQTT).
10. Não verificado o uso real de `data/parser/*.so` fora do Git.
11. Não verificado se `PREVIEW_TRANSPORT=udp` ainda é usado operacionalmente (sem teste).
