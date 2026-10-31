# Plano de implementação — troca de fonte no Vision Stability Lab

- **Branch:** `feature/multifonte` (HEAD `d367088`, 2026-10-30 16:12 −0300)
- **ADR:** [`docs/adr/adr-0006-troca-de-fonte.md`](../adr/adr-0006-troca-de-fonte.md)
- **Escopo:** o pipeline passa a aceitar **três fontes alternativas** (câmera fixa, drone por RTMP, celular Android por WHIP), uma ativa por vez, trocável por comando, com estado isolado por fonte e observabilidade por fonte.
- **Fora de escopo:** multi-fonte simultânea, tiler/grade, segundo `nvinfer`/engine b3, iOS, gravação contínua.
- **Estado:** plano executável. Etapas **E0, E1-T1, E1-T2, E2 e E3 (T1/T2)**
  concluídas e commitadas (E2 e E3-T2 com parecer de gate em
  `docs/baselines/2026-10-31-gate-e2-inc006.md` e
  `docs/baselines/2026-10-31-gate-e3t2.md`); **E4→E7 pendentes**. O E3-T2 tem
  parecer do `deepstream-tester` (6/6 itens, 2026-10-31); o parecer provisório
  de um agente `general`, usado enquanto o tester estava bloqueado por cota,
  foi revisado e corrigido no mesmo documento.

> **Regra de leitura deste documento:** todo número tem fonte explícita (arquivo, comando ou
> medição com data). A branch já teve corpo de commit com número errado —
> `docs/baselines/2026-10-30-e1t2-baseline.md` corrigiu um deles. Se um número aqui não tiver
> fonte, ele é **gate de plano** (alvo a ser provado), não medição.

---

## 1. Objetivo e critério de sucesso do épico

O laboratório nasceu para provar estabilidade de **uma** fonte RTSP por horas
(`README.md:1-20`). O épico de troca de fonte adiciona uma pergunta nova, sem afrouxar a
antiga:

> "O mesmo pipeline, sem mudar `batch_size`, sem tiler e sem engine nova, entrega o mesmo
> nível de estabilidade quando a fonte é uma câmera fixa, um drone por RTMP ou um celular
> Android por WHIP — e a troca entre elas é um comando, não um `vim` no código?"

Critério de sucesso do épico (E7):

1. 12 ciclos de troca consecutivos sem intervenção manual e sem restart espúrio do watchdog.
2. Um teste longo (6 h) por fonte, cada um com baseline própria e gates atendidos.
3. `git log` mostra um commit por tarefa, com evidência (JSONL + relatório em `docs/`).
4. Nenhuma credencial nova no repositório (`deepstream/tests/test_no_hardcoded_secrets.py` verde).

---

## 2. Decisões travadas (não reabrir)

Estas decisões foram tomadas antes desta sessão e o plano **não** as rediscute. O
raciocínio e as alternativas descartadas estão no ADR-0006.

| ID | Decisão | Consequência direta no plano |
|---|---|---|
| **D1** | **Uma fonte ativa por vez** — câmera, drone e celular são alternativas | `batch_size=1` mantido (`deepstream/config/infer.txt`, `pipeline_real.py:190`); sem `nvmultistream` tiler, sem grade no OSD, sem engine b3 |
| **D2** | Estado **por fonte**, com isolamento e reset entre sessões (namespace/descarte) | E3: `source_state.py`, `session_epoch` na telemetria, sem tocar em `useUniqueID: 0` |
| **D3** | Drone **fora** do gate de latência p95 ≤ 300 ms | E6: latência do drone é **reportada**, não gated. Gate do drone = reconexão, estabilidade, integridade |
| **D4** | Troca via `data/config/active_source` + `scripts/switch_source.sh` + recriação do container | E2-T3. Reprocessar = reiniciar processo; sem endpoint de troca em runtime |
| **D5** | Codec **por fonte** (`depay_factory`): câmera H.265, drone/celular H.264 | E2-T1/T2. `RTSP_CODEC` do compose vira fallback legado, não fonte de verdade |
| **D6** | Celular **somente Android**; iOS fora de escopo | Runbook `docs/runbooks/captura-celular.md` já declara isso; E7 não valida Safari |
| **D7** | `switch_source.sh` faz pré-check **obrigatório** com `rtsp_read_probe.py --seconds 5` | Exit `!= 0` do probe ⇒ **não troca**. Sem flag de bypass |
| **D8** | Branch de trabalho `feature/multifonte` | Todos os commits do épico nesta branch |
| **D9** | Imagem DeepStream: **pull anônimo do `nvcr.io` funciona** | E1-T1 já provou (imagem `deploy-vision-app:latest`, 17,8 GB, sem login). Nenhuma tarefa pendente depende de conta NGC |

---

## 3. Fatos de ambiente (verificados em 2026-10-30)

| Item | Valor | Fonte |
|---|---|---|
| Host | Jetson Orin, aarch64, kernel `5.15.148-tegra`, L4T R36.4.7 | `uname -a` (2026-10-30) |
| Memória unificada | `MemTotal` 7.802.160 kB ≈ 7,44 GiB; `MemAvailable` 4.067.412 kB | `/proc/meminfo` (2026-10-30) |
| Carveout CMA | `CmaTotal` 262.144 kB, `CmaFree` 65.332 kB | `/proc/meminfo` (2026-10-30) |
| Imagem do pipeline | `deploy-vision-app:latest`, **17,8 GB** | `docker images` (2026-10-30) |
| Stack ativo | `vision-stability-app` (healthy), `vision-mediamtx`, `vision-dashboard`, `vision-watchdog`, `vision-capture-web` (healthy) | `docker ps -a` (2026-10-30) |
| Stack arquo | parado (`arquo-web`/`arquo-db` `Exited (0)`, `arquo-server` `Exited (137)`) | `docker ps -a` (2026-10-30) |
| MediaMTX | `bluenviron/mediamtx:1.21.3`, paths `live`/`drone`/`phone`, 8889 TLS, ICE em 8189 udp+tcp | `mediamtx/mediamtx.yml`, `deploy/docker-compose.yml` |
| Portas | publicadas em `${LAN_IP:-192.168.59.102}`, nunca `0.0.0.0` (DIV-003) | `deploy/docker-compose.yml` |
| Fonte câmera | `192.168.59.108:5543/live/channel0`, **H.265** | `deploy/.env.example`, `docs/baselines/2026-10-30-e1t2-baseline.md` |
| fps de entrada atual | média **19,90** (min 15,12 / máx 20,03) nas últimas 60 amostras | `data/logs/live.jsonl`, `interval_metrics.fps_in` |
| RSS atual | **913,34 → 946,75 MB** nas últimas amostras | `data/logs/live.jsonl` |
| Alvo de fps do watchdog | **25** (`WATCHDOG_TARGET_FPS`) | `deploy/docker-compose.yml`, `scripts/watchdog.py:84` |
| `fps_degradation` | **11 eventos**; 6 deles com `fps_processed` **7,0–7,03** contra limiar **7,5** (= 25 × 0,3) | `data/logs/watchdog.jsonl` (30/10, 16:59→18:32) |
| `restart_suppressed` | **19 eventos**, todos `reason: no_frames_escalation` | `data/logs/watchdog.jsonl` |
| `no_frames_detected` / `restart_triggered` | 12 / 7 | `data/logs/watchdog.jsonl` |

### 3.1 O que mudou desde o plano original (correções de estabilidade já commitadas)

Estas correções mudam o que se pode medir e **invalidam** baselines antigas. Tudo abaixo
está em `git log` (`feature/multifonte`, `c29cf8b`→`d367088`).

| Correção | Arquivo | Efeito no plano |
|---|---|---|
| Erro do ramo de **preview** não derruba mais a análise; **EOS reconecta** a fonte; exit code propaga (0 limpo / 1 erro / 2 fonte perdida) | `deepstream/bus_classifier.py`, `deepstream/pipeline_real.py`, `deepstream/source_budget.py` | Baseline antiga (E1-T2) inválida: continha queda por EOS e por falso positivo |
| Watchdog: fim do falso positivo de `no_frames_escalation` (high-water de `frames_total_in`); `FRAME_TIMEOUT_S` 30→60; crash-loop só escala com **fonte morta**; carência por relógio monotônico; `container_missing`; `JsonlTail` robusto a truncamento | `scripts/watchdog.py` | E5 pode confiar nos eventos do watchdog; ainda falta o alvo de fps por fonte |
| Orçamento de reconexão de EOS por janela de tempo (fonte oscilante não zera orçamento com 1 frame) | `deepstream/source_budget.py` | Exit 2 passa a ser alcançável ⇒ o conflito com `restart: unless-stopped` (seção 11) fica real |

---

## 4. Arquitetura alvo

### 4.1 Fluxo de fontes (uma ativa por vez)

```text
data/config/active_source  ──── lido no startup ────┐
                                                     ▼
deepstream/config/sources.yaml ──► deepstream/sources.py
        (declarativo)                load_sources()
                                     resolve_active_source()
                                     depay_factory(source)  → ("rtph265depay","h265parse")
                                                                            │
                                                     ┌──────────────────────┘
                                                     ▼
  câmera fixa   192.168.59.108:5543  H.265  ──┐
  drone         rtsp://<lan>:8554/drone H.264 ├─► rtspsrc ─► depay ─► parse ─► nvv4l2decoder
  celular (WHIP) rtsp://<lan>:8554/phone H.264 ┘        ─► nvstreammux(batch=1) ─► nvinfer
                                                        ─► nvtracker ─► nvdsosd ─► tee
                                                            ├─ queue ─► fakesink   (análise)
                                                            └─ queue leaky ─► NVENC ─► MediaMTX `live`
```

Regras estruturais:

1. **O pipeline não conhece nomes de fonte.** Ele conhece `SourceConfig` (dataclass de
   `deepstream/sources.py`): `id`, `name`, `uri`, `codec`, `target_fps`, `gate`,
   `transport`. A escolha do elemento de depayload é `depay_factory(config.codec)`.
2. **Credencial não entra no YAML.** A câmera tem usuário/senha ⇒ `sources.yaml` guarda
   `uri_env: RTSP_URI` e a URI é lida em runtime por `rtsp_env.read_rtsp_uri()`
   (`deepstream/rtsp_env.py`, DIV-001); `uri_env` é ambiente **já resolvido** —
   `${...}` dentro do valor não é expandido. Drone e celular chegam pelo MediaMTX, que **não tem
   autenticação** (DIV-003) ⇒ URI literal `rtsp://<LAN_IP>:8554/<path>` é aceitável, e
   mesmo assim é derivada de `LAN_IP`.
3. **Codec muda só a cadeia de depayload.** `nvv4l2decoder`, `nvstreammux`, `nvinfer`,
   `nvtracker`, `nvdsosd` e o ramo de preview são idênticos nas três fontes.
4. **Troca = restart do processo**, não reconfiguração em runtime (D4). Isso mantém a
   propriedade de que *nenhum estado atravessa fontes* sem passar por `session_epoch`.

### 4.2 Estado e telemetria por fonte

```text
startup ──► session_epoch = STARTED_AT (ISO8601 UTC do início do processo)
             │
             ├─ source_id  (do active_source)
             ├─ reset de: FpsCounter, AreaMonitor (tracks + eventos + dwell), SourceReconnectBudget
             └─ todo registro JSONL (data/logs/live.jsonl) carrega source_id + session_epoch
```

- O JSONL é **append-only**: não se apaga histórico ao trocar de fonte. O isolamento é por
  namespace (`(source_id, session_epoch)`) na leitura (dashboard) e nos gates (watchdog).
- `session_epoch` = timestamp de início do processo: único por execução, comparável com
  `docker inspect .State.StartedAt` (`scripts/mem_budget.sh` já expõe esse campo) e não exige
  persistência nova.
- Contadores cumulativos (`frames_total_in`, `frames_total_processed`) continuam
  por-processo — é o contrato que `scripts/watchdog.py` usa desde o fix de high-water.

---

## 5. Etapas → histórias → tarefas

Legenda de prioridade: **P0** bloqueador · **P1** alta · **P2** média · **P3** baixa.
Complexidade: **XS** <2 h · **S** ½ dia · **M** 1 dia · **L** 2 dias · **XL** >3 dias.

Mapa de dependências (ordem obrigatória, exceto onde indicado):

```text
E1-T2 (baseline)
   │
   ├──► E2-T1 ──► E2-T2 ──► E2-T3 ──► E3-T1 ──► E3-T2 ──► E4-T1 ──► E4-T2
   │                 │                                        │
   │                 └──────────────► E5-T1 ◄────────────────┘
   │                                     │
   │                                     ├──► E5-T2 (healthcheck/heartbeat)
   │                                     └──► E5-T3 (rotação de log)
   │                                                │
   └───────────────────────────────────────────────► E6-T1 ──► E6-T2 ──► E7-T1 ──► E7-T2
```

Paralelizáveis: **E2-T1** com **E1-T2** (E2-T1 é módulo puro, não toca o pipeline);
**E5-T2** e **E5-T3** com **E4** (não compartilham arquivo).

---

### Épico E1 — Baseline limpo de 1 fonte

#### E1-T2 — Refazer o baseline de memória com a fonte real

| Campo | Valor |
|---|---|
| **Objetivo** | Produzir um baseline de 1 fonte **sem restart de falso positivo** e válido como referência de todas as comparações seguintes |
| **Arquivos** | `docs/baselines/2026-10-30-e1t2-baseline-v2.md` (novo), `docs/baselines/2026-10-30-e1t2-baseline-v2.jsonl` (novo), `docs/baselines/2026-10-30-e1t2-baseline.md` (só acrescentar ponteiro para a v2 no topo), `scripts/mem_budget.sh` (comentário de gates) |
| **Depende de** | — (correções de estabilidade já estão em `d367088`) |
| **Prioridade** | **P0** |
| **Risco** | Médio — a fonte pode cair durante a janela e invalidar de novo |
| **Complexidade** | S |
| **Critério de aceite** | Janela ≥ 30 min; `restarts: 0` em **todas** as amostras; nenhum evento `watchdog_fps_degradation`/`watchdog_no_frames_detected`/`watchdog_container_restarted` no intervalo (conferir `data/logs/watchdog.jsonl`); `rss_mb` nunca `null`; relatório declara host, imagem, commit e fps da fonte |
| **Validação** | `APP_CONTAINER=vision-stability-app bash scripts/mem_budget.sh --loop 60 30 | tee docs/baselines/2026-10-30-e1t2-baseline-v2.jsonl`; conferir `grep -c '"restarts":0'` = nº de amostras; `docker inspect --format '{{.RestartCount}}' vision-stability-app` antes/depois; `rtsp_read_probe.py --seconds 10` antes e depois para provar que a fonte estava viva |

Detalhes que o relatório **precisa** conter (aprendidos na v1):

- **Correção do gate de `lfb`**: a v1 previa `lfb ≥ 400 MB` e o valor medido foi **16–60 MB**
  — `lfb` baixo é o normal do carveout nesta Orin mesmo após boot limpo
  (`docs/baselines/2026-10-30-e1t2-baseline.md`, seção "Correção do gate de `lfb`"). O gate
  absoluto está **abandonado**; ver seção 6.
- Baselines de 2026-08 (`data/logs/phase5/*-summary.json`) **não** servem como referência de
  1 fonte sem nota explícita: foram medidas com outro pipeline e outra configuração de OSD.
- Atualizar o cabeçalho de `scripts/mem_budget.sh`, que hoje cita
  `docs/plano-multifonte-vsl.md` (caminho que não existe) — deve apontar para **este**
  arquivo — e que ainda lista `lfb ≥ 400 MB` como gate.

---

### Épico E2 — Fonte ativa por configuração

#### E2-T1 — `deepstream/config/sources.yaml` + `deepstream/sources.py`

| Campo | Valor |
|---|---|
| **Objetivo** | Um descritor declarativo de fontes e um módulo puro (sem `gi`/`pyds`) que resolve a fonte ativa e a cadeia de depayload |
| **Arquivos** | `deepstream/config/sources.yaml` (novo), `deepstream/sources.py` (novo), `deepstream/tests/test_sources.py` (novo), `data/config/.gitkeep` (novo), `data/config/active_source.example` (novo), `.gitignore` (+`data/config/active_source`) |
| **Depende de** | — (pode rodar em paralelo com E1-T2) |
| **Prioridade** | **P0** |
| **Risco** | Baixo — módulo puro, sem GPU; risco concentrado na validação do YAML |
| **Complexidade** | M |
| **Critério de aceite** | `python3 -c "from sources import load_sources"` funciona **sem** `gi`; `resolve_active_source()` respeita `data/config/active_source`, cai para `default_source` do YAML quando o arquivo está ausente e para `RTSP_URI`/`RTSP_CODEC` quando `VSL_SOURCE_SELECTION=off`; `depay_factory("h265") == ("rtph265depay","h265parse")` e idem H.264; fonte desconhecida em `active_source` **não** cai silenciosamente em default (falha com mensagem que lista os ids válidos) |
| **Validação** | `python3 -m pytest deepstream/tests/test_sources.py -q`. Casos mínimos: (a) YAML válido com 3 fontes, (b) `active_source` ausente → default, (c) `active_source` com id inválido → erro com lista de ids, (d) `uri_env` ausente no ambiente → erro de `rtsp_env` (não default embutido), (e) `depay_factory` para os 2 codecs + codec inválido, (f) `VSL_SOURCE_SELECTION=off` → caminho legado. Sem GPU, sem container |

Contrato de `sources.yaml` (forma implementada em `deepstream/sources.py`, E2-T1 —
estas são as chaves que o código lê; o `deepstream/config/sources.yaml` versionado
é a referência):

```yaml
# Sem credencial: DIV-001. A URI da câmera vem de RTSP_URI (deploy/.env).
version: 1
default_source: camera            # id, não rótulo
sources:
  - id: camera
    name: "câmera fixa"           # rótulo humano; não é usado como chave
    uri_env: RTSP_URI             # deepstream/rtsp_env.py: read_rtsp_uri()
    codec: h265
    transport: tcp
    latency_ms: 200
    target_fps: 20                # alvo nominal: ~19,9 fps em 2026-08-23/24;
                                  # mediana 12,0 e regime ~7,0 em 2026-10-30
    enabled: true
    gate:                         # D3: gate de qualidade por fonte
      latency_p95_ms: 300
  - id: drone
    name: "drone"
    # MediaMTX sem auth (DIV-003); `mediamtx` é o DNS do compose no container,
    # `MTX_HOST`/`LAN_IP` é o caminho no host (probe, switch_source.sh).
    uri: "rtsp://${MTX_HOST:-mediamtx}:8554/drone"
    codec: h264
    transport: tcp
    latency_ms: 200
    target_fps: 25
    enabled: true
    gate:
      latency_p95_ms: null
      report_only: true           # D3: latência reportada, não gated
  - id: phone
    name: "celular"
    uri: "rtsp://${MTX_HOST:-mediamtx}:8554/phone"
    codec: h264
    transport: tcp
    latency_ms: 200
    target_fps: 25
    enabled: true
    gate:
      latency_p95_ms: 300
```

> `target_fps` por fonte é **a** correção do maior gerador de alerta espúrio atual: o
> watchdog compara `fps_processed` com `0,3 × TARGET_FPS = 7,5` fixo, enquanto a câmera
> entrega ~20 fps e já foi medida em **7,0 fps** (`data/logs/watchdog.jsonl`, eventos de
> 16:59 a 18:32). Ver E5-T1.

#### E2-T2 — Pipeline consome a fonte ativa (codec por fonte)

| Campo | Valor |
|---|---|
| **Objetivo** | `pipeline_real.py` deixa de ler `RTSP_URI`/`RTSP_CODEC` do ambiente como caminho principal e passa a usar `resolve_active_source()`; codec vem da fonte, não de env |
| **Arquivos** | `deepstream/pipeline_real.py` (construtor: `self.source`, `self.rtsp_uri`, `self.codec`, `self.depay`/`self.parser`; `run()`: evento `source_active`), `deepstream/config/sources.yaml` (só leitura), `deepstream/tests/test_sources.py` (caso de integração leve) |
| **Depende de** | E1-T2 (baseline válido antes da mudança), E2-T1 |
| **Prioridade** | **P0** |
| **Risco** | **Alto** — é a única alteração que toca o caminho de mídia já validado; um erro de pad/link derruba as três fontes |
| **Complexidade** | M |
| **Critério de aceite** | Com `active_source=camera`, o pipeline sobe e a telemetria mostra `fps_in > 0` e `source_id: camera` em `interval_metrics`; com `active_source=drone` (e `rtmp_publisher.sh` publicando), o depay criado é `rtph264depay` (visível em `source_active`); `grep -n "RTSP_CODEC" deepstream/pipeline_real.py` só encontra o caminho de fallback documentado; nenhum `location=` do `rtspsrc` fora de `self.source.uri` |
| **Validação** | Gate obrigatório `deepstream-tester` para `deepstream/*` (seção 7). Além disso: `pytest deepstream/tests -q`; 3 execuções curtas (`TEST_DURATION_S=120`) com cada `active_source`; conferir `docker exec vision-stability-app gst-inspect-1.0 rtph265depay` no log de `source_active` |

Notas de implementação:

- `self.depay`/`self.parser` são criados no `__init__` e linkados na hora
  (`deepstream/pipeline_real.py:186-222`). Como a troca de fonte **reinicia o processo** (D4),
  não é preciso deslinkar nada em runtime — basta construir a cadeia certa no boot.
- `_on_rtspsrc_pad_added` já ignora pads de áudio (`pipeline_real.py:275-282`); não mexer —
  é o que faz a câmera (com áudio PCMA) e o celular funcionarem juntos.
- `RTSP_URI` continua vindo de `deepstream/rtsp_env.py` (DIV-001) e só é lida no caminho
  legado (`sem sources.yaml`, `VSL_SOURCE_SELECTION=off`) e no `uri_env` da câmera. Erro de
  variável ausente é `SourceConfigError` — **nunca** `sys.exit` dentro de `sources.py`:
  `SystemExit` não é capturado por `except Exception` e saía sem `startup_failed` na
  telemetria. O `sys.exit` fica só no `main()` do pipeline.

#### E2-T3 — `scripts/switch_source.sh` com pré-check obrigatório (D7)

| Campo | Valor |
|---|---|
| **Objetivo** | Um comando que troca a fonte ativa **só** quando a fonte pedida está entregando vídeo, e que volta sozinho se a troca não der certo |
| **Arquivos** | `scripts/switch_source.sh` (novo), `scripts/switch_source_lib.py` (novo — as funções puras), `scripts/tests/test_switch_source.py` (novo), `data/config/active_source.example` (referência), `docs/runbooks/troca-de-fonte.md` (novo, curto: uso + o que olhar quando falha) |
| **Depende de** | E2-T1, E2-T2 |
| **Prioridade** | **P0** |
| **Risco** | Médio — o script reinicia o container do pipeline; um bug deixa o stack parado |
| **Complexidade** | M |
| **Critério de aceite** | Sequência fixa e verificável: (1) valida `source_id` contra `sources.yaml` (id inexistente/desabilitado ⇒ exit 2, **sem** fallback para `default_source`); (1b) **guarda de precedência**: `docker exec <app> printenv ACTIVE_SOURCE` preenchido ⇒ aborta com exit 3, sem escrever nada e sem recriar o container (o ambiente vence o arquivo: a troca não pegaria e o pós-check aprovaria a fonte errada); (2) **pré-check** `python3 scripts/probe/rtsp_read_probe.py --url <uri> --seconds 5 --json` — exit `!= 0` aborta com exit 4 **sem** tocar em `active_source` e com o container intacto; (3) grava `data/config/active_source` de forma atômica (temporário no mesmo diretório + `mv`); (4) recria o container único do projeto com `docker compose up -d --no-deps vision-app`; (5) pós-check: espera em `data/logs/live.jsonl`, a partir do tamanho medido antes da recriação, um `interval_metrics` com `source_id` pedido e `fps_in > 0`, dentro de `--timeout` (default 90 s); (6) falha no pós-check ⇒ restaura o valor anterior (ou apaga o arquivo quando não havia controle), recria de novo e sai 1; (7) registra `data/logs/switch.jsonl` (uma linha: `from`, `to`, `probe`, `result`, `reason`, `ts`). `--skip-precheck` (registra `probe: skipped`, só para o teste de ciclo E6), `--timeout`, `--help`, `set -euo pipefail`, `bash -n` limpo |
| **Validação** | `python3 -m pytest scripts/tests/test_switch_source.py -q` sobre a metade pura (`switch_source_lib.py`; nenhum teste chama docker): (a) espera sobre JSONL de fixture: fonte certa com `fps_in > 0` ⇒ PASS, fonte errada/`fps_in = 0`/registro anterior ao offset ⇒ FAIL; (b) decisão de rollback: confirmado ⇒ `keep`, senão `restore_to` = valor anterior (vazio quando não havia arquivo); (c) guarda de precedência: container ou `deploy/.env` preenchido ⇒ bloqueio; (d) id inválido ⇒ exit 2. O script prova a ordem por asserção de texto (`probe` antes de `write_control`). Manualmente: `./scripts/switch_source.sh drone` com `rtmp_publisher.sh` no ar (PASS) e sem ele (FAIL `no_frames_on_path`, `StartedAt` do container e arquivo de controle inalterados) |

Mitigação de ruído obrigatória (senão E6-T2 é impossível): o script grava
`data/config/switch_in_progress` com `{"to": "<id>", "expires_at": "<ISO>"}` e o remove no
fim (inclusive em falha). O watchdog consome esse marcador (E5-T1) para não registrar
`crash_loop`/`no_frames` durante a troca — `up -d --no-deps` em série é, por assinatura,
idêntico a crash-loop (o próprio docstring de `scripts/watchdog.py` registra essa
ambiguidade: "restart em série sozinho também é a assinatura de `docker compose up -d` e de
troca de fonte").

---

### Épico E3 — Estado por fonte e `session_epoch`

#### E3-T1 — `deepstream/source_state.py`

| Campo | Valor |
|---|---|
| **Objetivo** | Encapsular a noção de sessão de fonte: epoch, namespace de telemetria e reset determinístico do estado de visão entre sessões |
| **Arquivos** | `deepstream/source_state.py` (novo), `deepstream/tests/test_source_state.py` (novo) |
| **Depende de** | E2-T2 |
| **Prioridade** | **P1** |
| **Risco** | Baixo — módulo puro |
| **Complexidade** | S |
| **Critério de aceite** | API mínima: `SessionState(source_id, epoch)`, `SessionState.new(source_id)` (epoch = `STARTED_AT`), `namespace()` → `{"source_id":…, "session_epoch":…}`, `reset()` devolve contadores zerados e **não** apaga histórico; `epoch` é estável dentro do processo e distinto entre dois processos consecutivos; nenhuma dependência de `gi`/`pyds`/`psutil` |
| **Validação** | `pytest deepstream/tests/test_source_state.py -q`: estabilidade do epoch intra-processo, unicidade entre dois `new()` no mesmo segundo (resolução de ms), `namespace()` incluído em todo dict de saída, `reset()` preservando `source_id` |

#### E3-T2 — Pipeline publica `source_id`/`session_epoch` e reseta entre sessões

| Campo | Valor |
|---|---|
| **Objetivo** | Todo registro de telemetria identifica fonte e sessão; o estado de visão não atravessa trocas |
| **Arquivos** | `deepstream/pipeline_real.py` (`log_event`, `periodic_metrics`, `main`), `deepstream/sources.py` (se o `SessionState` morar lá; caso contrário só import), `deepstream/tests/test_sources.py` (caso: `log_event` inclui namespace) |
| **Depende de** | E3-T1 |
| **Prioridade** | **P1** |
| **Risco** | Médio — `log_event` é chamado de threads de probe; mudar o formato do registro quebra consumidores (`scripts/watchdog.py`, `dashboard/backend/app.py`) |
| **Complexidade** | S |
| **Critério de aceite** | Todo JSONL emitido tem `source_id` e `session_epoch`; `startup` inclui `source_id`, `codec`, `target_fps`, `session_epoch`; `interval_metrics` inclui `source_id`, `source_name`, `session_epoch`, `source_target_fps` (este é o insumo de E5-T1: é o nome **exato** do campo que `pipeline_real.py` emite desde o E2-T2); campos antigos (`frames_total_in`, `fps_in`, `fps_processed`, `rss_mb`) **inalterados**; `scripts/watchdog.py` continua funcionando sem nenhuma mudança (campo novo é aditivo) |
| **Validação** | Gate `deepstream-tester`; `pytest deepstream/tests scripts/tests -q` (o teste do watchdog usa fixture de telemetria — se quebrar, o campo deixou de ser aditivo); `python3 -c "import json;[json.loads(l) for l in open('data/logs/live.jsonl')]"` após 5 min de execução |

---

### Épico E4 — Telemetria e dashboard multi-fonte

#### E4-T1 — Backend do dashboard por fonte

| Campo | Valor |
|---|---|
| **Objetivo** | O dashboard mostra **qual** fonte está ativa e mantém histórico separado por fonte, sem misturar séries |
| **Arquivos** | `dashboard/backend/app.py` (ring buffers por `(source_id, session_epoch)`; `snapshot()`, `health()`; rotas novas `/api/sources` e `/api/history?source=`), `dashboard/tests/test_backend_sources.py` (novo, se a pasta existir) |
| **Depende de** | E3-T2 |
| **Prioridade** | **P2** |
| **Risco** | Médio — o replay do JSONL na partida (`tail_telemetry`) é o mecanismo de histórico; trocar a chave quebra o gráfico se o frontend não for junto |
| **Complexidade** | M |
| **Critério de aceite** | `/api/snapshot` traz `source: {id, name, session_epoch, since}` (`name` vem de `source_name` na telemetria) e `health` derivado da sessão **ativa** apenas; `/api/history?minutes=30` sem `source` usa a ativa, com `source=<id>` devolve `[]` (não erro) para id sem histórico ou devolve os pontos daquele id; nenhum ponto de uma fonte aparece na série de outra; `healthy/stale/down` continua com os limiares atuais (`healthy` = `interval_metrics` < 90 s e `fps_processed > 0`, `dashboard/backend/app.py:86-99`, `docs/architecture.md:75`) |
| **Validação** | Replay de um JSONL de fixture com 2 fontes intercaladas e 2 epochs da mesma fonte: assertar separação das séries e que `health` só olha a última sessão da fonte ativa; `curl -s localhost:8081/api/sources | jq` com o stack real |

#### E4-T2 — Frontend: fonte ativa e histórico por fonte

| Campo | Valor |
|---|---|
| **Objetivo** | O operador vê, sem abrir `docker ps`, qual fonte está no ar, desde quando, e o histórico daquela fonte |
| **Arquivos** | `dashboard/frontend/app.js`, `dashboard/frontend/index.html`, `dashboard/frontend/style.css` |
| **Depende de** | E4-T1 |
| **Prioridade** | **P2** |
| **Risco** | Baixo — SPA vanilla, sem build |
| **Complexidade** | S |
| **Critério de aceite** | Card "Fonte ativa" com `name`, `id`, `session_epoch` e uptime da sessão; troca de fonte no seletor troca gráfico e eventos; o player WHEP continua apontando para `live` (o preview é sempre o mesmo path, independente da fonte); nada de `localhost` hardcoded (o WHEP deriva de `location.hostname` — `README.md:521-527`) |
| **Validação** | `node --check dashboard/frontend/app.js` (Node 24.19 presente no host); abrir `http://192.168.59.102:8081` com a CA instalada e conferir 2 sessões de fontes diferentes no seletor |

---

### Épico E5 — Watchdog por fonte

#### E5-T1 — Alvo de fps e janela de troca por fonte

| Campo | Valor |
|---|---|
| **Objetivo** | Eliminar a fonte nº 1 de alerta espúrio: `TARGET_FPS=25` fixo contra uma câmera que entrega ~20 fps (e caiu a 7,0) |
| **Arquivos** | `scripts/watchdog.py` (`TARGET_FPS` → alvo por fonte lido de `interval_metrics.source_target_fps`, com fallback env; leitura de `data/config/switch_in_progress`), `scripts/tests/test_watchdog_sources.py` (estender) |
| **Depende de** | E3-T2 (campo `source_target_fps`), E2-T3 (marcador de troca) |
| **Prioridade** | **P1** |
| **Risco** | Médio — mexer na Regra 2 pode **mascarar** degradação real; o watchdog é a segunda linha de defesa |
| **Complexidade** | M |
| **Critério de aceite** | (a) com `source_target_fps: 20`, `fps_processed: 16` **não** gera `fps_degradation` (é 80 % do alvo); com `fps_processed: 7,0` gera; (b) o limiar passa a ser **60 % do alvo da fonte** (12 fps para a câmera), não 30 % de 25 — justificativa: 30 % de 25 = 7,5 é exatamente onde a câmera foi medida, então o gate antigo alternava entre "espúrio" e "quase disparando"; (c) durante `switch_in_progress` válido, `no_frames_detected` e `crash_loop_detected` **não** escalam restart; (d) sem `source_target_fps` na telemetria (JSONL antigo), o comportamento é o de hoje (fallback `TARGET_FPS`); (e) o evento `watchdog_started` registra o alvo resolvido |
| **Validação** | `pytest scripts/tests/test_watchdog_sources.py -q` com fixtures de `interval_metrics` (20/16/12/7 fps; com e sem `source_target_fps`; com e sem marcador de troca). Gate `deepstream-tester` **não** se aplica (não é `deepstream/*`), mas `scripts/watchdog.py` exige revisão por `REVIEWER` antes do merge |

> **Medido no gate do E3-T2 (2026-10-31, `docs/baselines/2026-10-31-gate-e3t2.md`, seção "Causa da queda de fps"):**
> a câmera oscilou para `fps_processed=6,53` e o watchdog emitiu
> `{"event": "watchdog_fps_degradation", "threshold": 7.5, "target_fps": 25.0, "sustained_s": 140.2}`
> — 30 % de 25 = 7,5, exatamente o valor que o critério (b) descreve. A fonte
> declara `source_target_fps=20.0` na mesma telemetria, então o alvo já está
> disponível para ser consumido sem mudança no pipeline (E3-T2 fechado).
>
> **Causa da queda (operador, 2026-10-31): ambiente escuro, com a luz sendo
> ligada/desligada — a própria câmera reduziu o fps.** A queda foi **real**: o
> watchdog não errou em detectá-la, e o E5-T1 **não** pode passar a mascará-la.
> O que está calibrado errado é o limiar (7,5 = 30 % de 25 contra alvo 20).
> Fica registrado também que luminosidade é causa externa legítima de queda de
> fps que a telemetria do pipeline não distingue de degradação própria — toda
> comparação de fps (inclusive a da sessão do drone) precisa anotar a condição
> de luz.

#### E5-T2 — `healthcheck` e heartbeat do próprio watchdog

| Campo | Valor |
|---|---|
| **Objetivo** | O watchdog é hoje um container que pode morrer em silêncio e ninguém percebe; e o `depends_on` não garante que ele esteja vivo |
| **Arquivos** | `deploy/Dockerfile.watchdog`, `deploy/docker-compose.yml` (serviço `watchdog`: `healthcheck`), `scripts/watchdog.py` (heartbeat em `data/logs/watchdog.jsonl` a cada `CHECK_INTERVAL_S`; comando `--healthcheck`) |
| **Depende de** | E5-T1 |
| **Prioridade** | **P2** |
| **Risco** | Baixo |
| **Complexidade** | S |
| **Critério de aceite** | `docker inspect --format '{{.State.Health.Status}}' vision-watchdog` = `healthy`; matar o watchdog com `docker kill` → `unhealthy` em ≤ 3 intervalos; `--healthcheck` sai 0 se o último `watchdog_heartbeat` tem menos de `3 × CHECK_INTERVAL_S`; o heartbeat **não** polui os gates (é evento próprio, não `interval_metrics`) |
| **Validação** | `docker inspect … Health.Status`; `docker kill vision-watchdog && sleep 90 && docker ps` mostra `unhealthy`; o log tem `watchdog_heartbeat` com cadência regular |

#### E5-T3 — Rotação de `data/logs/*.jsonl`

| Campo | Valor |
|---|---|
| **Objetivo** | `live.jsonl` e `watchdog.jsonl` crescem sem teto (o `live.jsonl` já tem **4.176** amostras de `interval_metrics` desde 2026-08-23) |
| **Arquivos** | `scripts/rotate_logs.sh` (novo), `deploy/docker-compose.yml` (volume/mount já existe: `../data:/app/data`), `docs/architecture.md` (nota de retenção) |
| **Depende de** | E5-T2 |
| **Prioridade** | **P2** |
| **Risco** | Médio — `scripts/watchdog.py::JsonlTail` e `dashboard/backend/app.py::tail_telemetry` seguem o arquivo; truncar/renomear no lugar errado faz os dois pararem de ler (o `JsonlTail` já é robusto a truncamento, mas o tailer do dashboard reconstrói o histórico na partida) |
| **Complexidade** | S |
| **Critério de aceite** | Rotação por tamanho (ex.: 32 MB) **ou** tempo (ex.: diária), com os consumidores sobrevivendo: após a rotação, `interval_metrics` continua aparecendo no dashboard e o watchdog continua emitindo `no_frames`/`frames_recovered`; arquivos rotacionados fora de `data/logs/*.jsonl` (o `.gitignore` hoje ignora exatamente `data/logs/*.jsonl`) |
| **Validação** | Rodar a rotação com o stack no ar e conferir 5 min de telemetria contínua nos dois consumidores; `ls -la data/logs/` mostra rotacionados; `git status --short` limpo |

---

### Épico E6 — Latência por fonte e ciclos de troca

> **Pré-requisitos obrigatórios** (seção 11): fonte estabilizada e re-baselinada (E1-T2), alvo
> de fps por fonte (E5-T1), `healthcheck` do watchdog (E5-T2), rotação de log (E5-T3) e a
> autoridade de "fonte perdida" decidida. Sem isso, o E6 mede ruído do próprio laboratório.

#### E6-T1 — Instrumento de latência por fonte

| Campo | Valor |
|---|---|
| **Objetivo** | Medir latência **ponta a ponta por conteúdo de imagem**, por fonte, sem repetir o erro do PTS (que se cancela) |
| **Arquivos** | `scripts/probe/latency_flash.py` (novo: publica alternância de cor e detecta a transição no leitor), `scripts/tests/test_probe_logic.py` (estender com os parsers puros), `docs/baselines/2026-11-XX-latencia-por-fonte.md` (novo, quando houver dados) |
| **Depende de** | E1-T2, E5-T1, E5-T2, E5-T3 |
| **Prioridade** | **P1** |
| **Risco** | Médio — medir latência com dois processos na mesma SoC já produziu número enganoso no projeto irmão (ADR-0005 do arquo, achado 1: "`now − PTS` se cancela porque o `rtspsrc` desloca o PTS junto com o `latency`") |
| **Complexidade** | M |
| **Critério de aceite** | Relatório por fonte com `p50`, `p95`, `máx` e nº de transições; **gate p95 ≤ 300 ms** para `camera` e `phone`; para `drone`, **apenas reportado** (D3); o método está documentado no relatório (alternância de cor + relógio comum no host); nenhum número de latência entra na telemetria do pipeline como se fosse medido por ele |
| **Validação** | Execução com `PREVIEW_TRANSPORT=udp` e com `rtsp` para comparar o custo do hop do MediaMTX; 3 execuções por fonte; declarar a resolução e o fps usados (o p95 depende disso) |

#### E6-T2 — 12 ciclos de troca

| Campo | Valor |
|---|---|
| **Objetivo** | Provar que trocar de fonte é operação, não evento: 12 ciclos `camera → drone → phone → camera` sem intervenção manual |
| **Arquivos** | `scripts/cycles_switch.sh` (novo, orquestra `switch_source.sh` + `rtmp_publisher.sh` + coleta), `data/logs/switch_cycles-summary.json` (gerado), `docs/phase-multifonte-evaluation.md` (novo, relatório) |
| **Depende de** | E6-T1, E2-T3 |
| **Prioridade** | **P1** |
| **Risco** | **Alto** — é onde os bugs de estado entre sessões aparecem (vazamento de `AreaMonitor`, contador de frames atravessando, `crash_loop` do watchdog disparando por troca) |
| **Complexidade** | L |
| **Critério de aceite** | 12/12 ciclos com pós-check verde; `restarts` do container = exatamente 12 (+0 espúrios); **zero** `watchdog_restart_triggered` durante as trocas; RSS no início de cada sessão dentro de 750–1050 MB; `active_tracks` volta a 0 em cena vazia em toda sessão; `session_epoch` distinto em cada ciclo; nenhuma amostra de telemetria com `source_id` divergente do `active_source` vigente |
| **Validação** | `bash scripts/cycles_switch.sh 12`, depois conferência automática sobre `data/logs/switch.jsonl` + `data/logs/watchdog.jsonl` + `data/logs/live.jsonl` (script de conferência incluso na tarefa). Gate `deepstream-tester` em cada mudança de `deepstream/*` feita para corrigir achados |

---

### Épico E7 — Teste longo e documentação

#### E7-T1 — Teste longo (6 h) por fonte

| Campo | Valor |
|---|---|
| **Objetivo** | Fechar o critério original do laboratório (estabilidade por horas) para **cada** fonte, com baseline própria |
| **Arquivos** | `data/logs/multifonte/<fonte>-longrun-summary.json` (gerado), `docs/phase-multifonte-evaluation.md` (completar) |
| **Depende de** | E6-T2 |
| **Prioridade** | **P2** |
| **Risco** | Médio — 18 h de wall clock (3 × 6 h) e uma queda de energia (INC-005) já invalidou sessão neste projeto |
| **Complexidade** | L |
| **Critério de aceite** | Por fonte: platô de RSS após warm-up; crescimento pós-warm-up < 15 %; `restarts: 0`; `OOMKilled: false`; filas `queue_analyze`/`queue_preview` ≤ 1; `active_tracks` → 0 em cena vazia; fonte do drone: reconexão bem-sucedida em ≥ 1 desconexão provocada, com `source_reconnect_resumed` no log |
| **Validação** | `mem_budget.sh --loop 60 360` por sessão, mais `rtsp_read_probe.py` antes/depois; comparar contra a baseline v2 (E1-T2), não contra 2026-08 |

#### E7-T2 — Documentação e fechamento

| Campo | Valor |
|---|---|
| **Objetivo** | O repositório conta a verdade sobre multi-fonte, sem números órfãos |
| **Arquivos** | `docs/architecture.md` (seção de fontes + diagrama), `README.md` (status, como trocar de fonte, tabela de fases), `docs/test-plan.md` (testes de troca e longo), `docs/incident-log.md` (dívidas abertas: autoridade de fonte perdida, DIV-003 ainda aberta), `docs/runbooks/troca-de-fonte.md` |
| **Depende de** | E7-T1 |
| **Prioridade** | **P1** |
| **Risco** | Baixo |
| **Complexidade** | S |
| **Critério de aceite** | Nenhum número sem fonte; `docs/architecture.md` descreve o pipeline real (não o do README de 2026-08-23); `README.md` não promete multi-fonte simultânea; dívidas novas registradas com data e ação requerida; ADR-0006 linkado a partir do plano e do `architecture.md` |
| **Validação** | Revisão por `REVIEWER`; `grep -rn "lfb ≥ 400" docs/ README.md scripts/` não encontra nada (gate corrigido em todo lugar) |

---

## 6. Orçamento de memória (gates corrigidos)

### 6.1 Gates por fonte

| Métrica | Câmera (1 fonte) | Drone | Celular | Fonte do número |
|---|---|---|---|---|
| RSS do pipeline pós-warm-up | **750–1050 MB** | **858–882 MB** (medido no drone real, 2026-10-31) | 750–1050 MB (gate provisório) | medido: 879–894 MB (`docs/baselines/2026-10-30-e1t2-baseline.md`), 913–947 MB (`data/logs/live.jsonl`, 2026-10-30) e 858–882 MB (`docs/baselines/2026-10-31-drone-campo.md`) |
| Crescimento de RSS pós-warm-up | < 15 % | < 15 % | < 15 % | `scripts/watchdog.py` Regra 3 (`RULE3_RSS_RATIO`) |
| RAM do sistema | ≤ 70 % (stack arquo parado) | ≤ 70 % | ≤ 70 % | medido 35,6–45,2 % (`docs/baselines/2026-10-30-e1t2-baseline.md`) |
| `OOMKilled` / exit 137 | `false` / 0 | idem | idem | `scripts/mem_budget.sh` (`oom_killed`, `exit_code`) |
| Containers DeepStream ativos | exatamente 1 | 1 | 1 | `AGENTS.md` global (regra 1) + `mem_budget.sh` (`ds_containers`) |
| Filas do tee | ≤ 1 buffer | ≤ 1 | ≤ 1 | medido `0`/`0` na baseline v1 |
| Idade da telemetria | < 180 s | < 180 s | < 180 s | `deploy/docker-compose.yml` (healthcheck do `vision-app`) |
| `restarts` | 0 | 0 | 0 | `mem_budget.sh` |

Gates do drone/celular são **provisórios** até a primeira medição de cada um (E7-T1). Ficam
escritos para não se inventar número depois: quem medir, atualiza esta tabela com a fonte.

> **Primeira medição (gate E2, 2026-10-31):** com publisher sintético H.264 a 720p/25 fps, o
> RSS ficou em **721–723 MB** em `drone` e `phone` — abaixo do piso de 750 MB, que foi medido
> na câmera H.265. Não é falha do gate: é o piso provisório se mostrando alto para fonte
> H.264. Ajustar com a medição do **drone real** (E7-T1), não com o publisher sintético
> (`docs/baselines/2026-10-31-gate-e2-inc006.md`, divergência 3).
>
> **Drone real (2026-10-31, `docs/baselines/2026-10-31-drone-campo.md`):** DJI Mini 3 em
> 1440x1080/30 fps, 660 s de regime — RSS **858,2–882 MB**, platô de **861,9 MB** na janela,
> dentro do gate 750–1050. O piso de 750 MB **se confirma** para o drone real; os 721–723 MB
> do publisher sintético eram artefato do sintético, não da fonte. O celular continua
> provisório (falta a medição real).

### 6.2 Gate de `lfb` — substituído (não é valor absoluto)

O plano anterior exigia `lfb ≥ 400 MB`. **Está errado para esta máquina:** após boot limpo,
sem carga e com 32 % de RAM, o `lfb` mede **16 MB** (4 blocos × 4 MB), e ao longo da janela
v1 variou entre **16 e 60 MB** (`docs/baselines/2026-10-30-e1t2-baseline.md`, seção
"Correção do gate de `lfb`"). `lfb` baixo é o estado **normal** do carveout da Orin.

Gate substituto — **variação**, não absoluto:

1. `lfb_ref` = mediana das 3 primeiras amostras após o warm-up (10 min) da sessão.
2. **Alerta** se `lfb` cair abaixo de **50 % de `lfb_ref`** em **3 amostras consecutivas**
   *e* o RSS estiver subindo no mesmo intervalo.
3. **Falha** se, junto com (2), houver `OOMKilled: true` ou `exit_code: 137`.
4. Referência de sanidade independente: `CmaFree` de `/proc/meminfo` (medido 65.332 kB de
   262.144 kB em 2026-10-30) — **não** é o mesmo número que `lfb` do `tegrastats`, e não
   deve ser comparado com ele.

Também registrado: os OOMs de `nvinfer` anteriores ao reboot (CUDA runtime OOM ao alocar
40 MB com RAM de sistema em 25 %) **não** eram falta de `lfb`; desapareceram com o reboot, o
que aponta para alocação presa no carveout por processos mortos em crash-loop
(`docs/baselines/2026-10-30-e1t2-baseline.md`). Consequência prática: **antes de cada
baseline, reiniciar o host** (não só o container).

---

## 7. Estratégia de teste

### 7.1 Camadas

| Camada | O que cobre | Comando | Quando |
|---|---|---|---|
| Unidade pura (sem GPU) | `sources.py`, `source_state.py`, `source_budget.py`, `bus_classifier.py`, parsers de probe, lógica de watchdog | `python3 -m pytest deepstream/tests scripts/tests -q` | a cada tarefa |
| Script de shell | ordem das operações, rollback, falha de pré-check | `python3 -m pytest scripts/tests/test_switch_source.py -q` com `docker`/probe falsos no `PATH`; `bash -n scripts/*.sh` | E2-T3, E5-T3 |
| Frontend | sintaxe e fixtures | `node --check dashboard/frontend/app.js` (Node **24.19.0** no host); autoteste de `dashboard/frontend/codec-gate-selftest.html` | E4-T2 |
| Integração em container | pipeline real, mídia real, telemetria | **gate `deepstream-tester`** | toda mudança em `deepstream/*` (`pipeline_real.py`, `area_monitor.py`, `bus_classifier.py`, `source_budget.py`, `sources.py`, `source_state.py`) ou no PGIE `deepstream/config/infer.txt` ou em `scripts/watchdog.py` |
| Operação | 12 ciclos, 6 h por fonte | E6-T2, E7-T1 | fim de etapa |

### 7.2 `deepstream-tester` como gate obrigatório

Nenhuma tarefa que toque `deepstream/*` ou `scripts/watchdog.py` é considerada concluída sem
o parecer do subagente `deepstream-tester` rodando o pipeline no **container único do
projeto** (`vision-stability-app`, imagem `deploy-vision-app:latest`). Isso é também a
aplicação da regra global: **um** container DeepStream por projeto, nunca dois
(`AGENTS.md`). Em particular:

- **proibido** subir um segundo container/imagem DeepStream para testar troca de fonte;
- o `deepstream-tester` valida telemetria e eventos, não só "subiu sem crash";
- para E2-T2, o parecer precisa cobrir as **três** fontes (com `rtmp_publisher.sh` fazendo o
  papel do drone e do celular — `scripts/probe/rtmp_publisher.sh --path drone|phone`).

### 7.3 O que **não** conta como validação

- `docker ps` mostrando `healthy` — os healthchecks validam config/telemetria, não caminho
  de mídia (`docs/runbooks/captura-celular.md`, tabela de diagnóstico).
- `fps_in > 0` numa única amostra após a troca — o pós-check do `switch_source.sh` exige
  `session_epoch` novo **e** `fps_in > 0`, e o E6-T2 exige 12 ciclos.
- Latência medida por `PTS` — se cancela (ADR-0005 do arquo, achado 1).

---

## 8. Riscos e mitigação

| # | Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|---|
| R1 | `docker restart` da troca é indistinguível de crash-loop para o watchdog ⇒ restart espúrio no meio do E6-T2 | Alta | Alto | Marcador `data/config/switch_in_progress` (E2-T3) consumido por E5-T1; carência monotônica já existente em `scripts/watchdog.py::_observe_container_start` |
| R2 | `restart: unless-stopped` **mascara** o exit 2 (fonte perdida): o container volta e o incidente vira "reinício" | Certa | Alto | Decidir a autoridade (seção 11) **antes** do E6; alternativa já desenhada: `restart: on-failure` só para `vision-app`, ou o watchdog lendo `docker inspect .State.ExitCode` (o campo já existe em `scripts/mem_budget.sh`) e emitindo `source_lost` |
| R3 | Trocar `location` do `rtspsrc` em runtime em vez de reiniciar deixaria estado de caps/decoder inconsistente | Média | Alto | **Não fazer**: D4 manda reiniciar o processo (ADR-0006, alternativa descartada) |
| R4 | Limite de sessões RTSP simultâneas na câmera (hipótese para o EOS da v1) | Média | Médio | `switch_source.sh` **só** toca a câmera depois que o pré-check passa; nunca rodar `rtsp_read_probe` e o pipeline contra a câmera ao mesmo tempo por mais de 5 s; se o EOS voltar, medir com `GST_DEBUG=rtspsrc:5` |
| R5 | Vazamento de estado entre fontes (`AreaMonitor`, contadores, `SourceReconnectBudget`) | Média | Alto | E3-T1/T2 + assertiva do E6-T2 ("nenhuma amostra com `source_id` divergente do `active_source`"); reset explícito no boot, não em runtime |
| R6 | Container recriado no meio de uma sessão perde telemetria em voo | Alta | Baixo | JSONL append-only + `session_epoch`: a sessão interrompida fica visível como sessão incompleta, não como continuação |
| R7 | `data/config/active_source` versionado por acidente (estado local no repositório) | Média | Baixo | `.gitignore` + `active_source.example` versionado (E2-T1) |
| R8 | Rotação de log quebra `JsonlTail`/tailer do dashboard | Média | Médio | E5-T3 com o stack no ar; `JsonlTail` já é robusto a truncamento (`scripts/watchdog.py:283-302`) |
| R9 | Dois containers DeepStream no host (regra global) | Baixa | Alto (OOM/exit 137 em 7,4 GB) | Checklist do `AGENTS.md` antes de qualquer teste; o `deepstream-tester` usa o container único |
| R10 | Medição de latência enganosa (SoC compartilhada, encode competindo) | Alta | Médio | E6-T1 exige declarar resolução/fps e comparar `rtsp` vs `udp`; drone só reportado (D3) |
| R11 | Queda de energia no meio de teste longo (INC-005 já ocorreu) | Baixa | Alto | UPS é DIV-002 (`docs/phase5-progress.md:89`), ainda aberta; no mínimo, `switch.jsonl`/`mem_budget` permitem retomar de onde parou |

---

## 9. Rollback por feature flag

Três níveis, do mais barato ao mais caro. O primeiro é o rollback normal.

### Nível 1 — flag de seleção (segundos)

```bash
# volta ao comportamento de 2026-10-30 (RTSP_URI + RTSP_CODEC do compose)
VSL_SOURCE_SELECTION=off
docker compose -f deploy/docker-compose.yml up -d --force-recreate vision-app
```

`deepstream/sources.py` mantém o caminho legado vivo: com a flag `off`, o pipeline usa
`rtsp_env.read_rtsp_uri()` e `RTSP_CODEC` exatamente como hoje. **`RTSP_CODEC` e `RTSP_URI` não
podem ser removidos do compose** enquanto a flag existir — removê-los é o que transforma o
rollback em cirurgia.

### Nível 2 — fonte

```bash
bash scripts/switch_source.sh camera        # volta para a fonte que já foi validada
```

Se a fonte de destino estiver morta, o pré-check (D7) impede a troca — o operador fica na
fonte atual, que é o comportamento desejado.

### Nível 3 — código

Reverter o merge do épico, ou `git revert` das tarefas na ordem inversa (E5 → E4 → E3 → E2).
As tarefas foram desenhadas para serem revertíveis isoladamente: E2-T1 é módulo novo
(removível), E2-T2 é a única que troca o caminho de mídia, E3/E4 só **acrescentam** campos,
E5 muda uma constante com fallback.

---

## 10. O que depende do usuário (hardware e presença física)

Nada de campo acontece sem isto — o plano não pode ser "concluído na bancada".

| # | Dependência | Bloqueia | Como confirmar |
|---|---|---|---|
| U1 | **Câmera fixa ligada** em `192.168.59.108:5543`, H.265, alcançável | E1-T2, E6, E7 | `python3 scripts/probe/rtsp_read_probe.py --url "$RTSP_URI" --seconds 5 --json` → exit 0 |
| U2 | **Drone publicando RTMP** em `rtmp://192.168.59.102:1935/drone`, H.264 | E2-T2 (fonte drone), E6-T2, E7 | `rtmp_publisher.sh --seconds 30` prova a bancada; **o drone real é do usuário** |
| U3 | **Celular Android** com `ca.crt` instalado (`docs/runbooks/captura-celular.md`, seção 3), abrindo `https://192.168.59.102:8443/capture.html` | E2-T2 (fonte phone), E6-T2, E7 | `whip_probe.py --cacert deploy/certs/ca.crt --path phone` → exit 0 **não** prova o device; os passos 4 e 5 do runbook são de campo |
| U4 | **Stack arquo parado** durante medições | E1-T2, E7 | `docker ps --filter name=arquo` vazio |
| U5 | **Reboot do host** antes de cada baseline | E1-T2, E7 | `uptime -p` curto; `lfb` fresco; ver seção 6.2 |
| U6 | Aceite de que **iOS está fora** (D6) | nada — já decidido | — |

---

## 11. Bloqueios conhecidos para E6/E7 (pré-requisitos, não tarefas opcionais)

Estes itens **não** podem ser resolvidos durante o E6: eles fazem o E6 medir a si mesmo.

1. **Fonte estabilizada e re-baselinada.** O baseline de
   `docs/baselines/2026-10-30-e1t2-baseline.md` está marcado **inválido** (contém restart de
   falso positivo do watchdog, às 15:54, mais um EOS real às 15:51). Sem E1-T2, qualquer
   comparação de memória no E6 está contra uma janela suja.
2. **Alvo de fps por fonte (E5-T1).** Com `TARGET_FPS=25` fixo, o watchdog emitiu **11**
   `fps_degradation`, 6 deles com `fps_processed` **7,0–7,03** contra limiar **7,5**
   (`data/logs/watchdog.jsonl`, 16:59→18:32). O E6-T2 teria alertas espúrios em quase toda
   sessão, e o E7-T1 herdaria o mesmo ruído por 6 h.
3. **`healthcheck` + heartbeat do próprio watchdog (E5-T2).** Hoje um watchdog morto é
   invisível: os gates do E7 seriam avaliados por um observador que pode ter parado.
4. **Rotação de `data/logs/*.jsonl` (E5-T3).** `live.jsonl` já tem 4.176 `interval_metrics`
   desde 2026-08-23; 12 ciclos + 3 testes de 6 h somam muito mais, e o dashboard reconstrói
   todo o histórico na partida (`docs/architecture.md:76`).
5. **Autoridade de "fonte perdida" — decisão pendente.** Hoje existem **três** candidatos
   com opiniões diferentes:
   - pipeline: exit **2** (`EXIT_SOURCE_LOST`, `deepstream/pipeline_real.py:100`) após
     esgotar `SOURCE_RECONNECT_MAX_ATTEMPTS = 5`;
   - watchdog: `no_frames_escalation` após 180 s, com restart em
     `scripts/watchdog.py` (Regra 1);
   - Docker: `restart: unless-stopped` em `deploy/docker-compose.yml`, que **reinicia mesmo
     no exit 0 e no exit 2**.
   Enquanto o terceiro existir sem tratamento, o exit 2 é **mascarado**: o container volta e
   o evento aparece como "reinício", exatamente o sintoma que motivou o fix de EOS
   (`c29cf8b`). Decisão necessária antes do E6-T2, e o registro cabe em
   `docs/incident-log.md` como **DIV-004** (o log tem DIV-001, DIV-003 e DIV-002 em
   `docs/phase5-progress.md:89`; o próximo livre é 004).

   Opções (escolher **uma**, no início do E6):

   | Opção | Como | Custo | Efeito |
   |---|---|---|---|
   | A | `restart: on-failure` no `vision-app` | 1 linha no compose | exit 2 deixa de ser mascarado; um exit 0 limpo **para** o stack (aceitável: exit 0 é só encerramento por duração de teste/CTRL) |
   | B | watchdog lê `.State.ExitCode` e emite `source_lost`, mantendo `unless-stopped` | ~30 linhas em `scripts/watchdog.py` | nenhuma mudança de política; o evento fica correto na telemetria |
   | C | `/dev/shm` de estado + `restart: no` + supervisor externo | alto | força uma reescrita da operação; **não recomendado** agora |

   Recomendação do plano: **B** (não muda o comportamento de recuperação, só a
   observabilidade — e o campo já é exposto por `scripts/mem_budget.sh`).

---

## 12. Lições que custaram retrabalho (não repetir)

1. **Gate de `lfb` absoluto estava errado.** `lfb ≥ 400 MB` era impossível nesta Orin;
   o normal é 16–60 MB. Gate tem de ser **variação**, com `lfb_ref` da própria sessão.
   (Fonte: `docs/baselines/2026-10-30-e1t2-baseline.md`.)
2. **Baseline de 2026-08 não é referência de 1 fonte.** `data/logs/phase5/*-summary.json`
   são de outro pipeline; usar sem nota explícita é comparar coisas diferentes.
3. **`deploy/mediamtx/mediamtx.yml` era um diretório `root:root` vazio**, não um arquivo —
   o `docker compose` montava um diretório em `/mediamtx.yml` e o MediaMTX subia com a
   config default. Removido com `sudo rmdir`. Antes de "consertar" config de mídia,
   `ls -ld` no caminho.
4. **Número em commit sem fonte se propaga.** Corrigir a atribuição do restart de 15:54
   virou commit próprio (`99ed24a`). Todo número deste plano tem fonte.
5. **Falso positivo de watchdog invalida medição.** O `no_frames_escalation` por high-water
   derrubou o container a cada ~12 min no dia da v1. Regra que reinicia precisa de
   corroboração (fonte morta), não só de um contador.
6. **`target_fps` fixo contra fonte de fps variável é gerador de ruído.** A câmera alterna
   entre ~20 fps e 7 fps; o alvo tem de ser por fonte, e o limiar relativo a ele.

---

## 13. Fluxo de implementação e estimativa

| Ordem | Tarefa | Prio | Compl. | Estimativa | Bloqueia |
|---:|---|---|---|---:|---|
| 1 | E1-T2 — baseline v2 limpo | P0 | S | 0,5 d | E2-T2, E6, E7 |
| 2 | E2-T1 — `sources.yaml` + `sources.py` | P0 | M | 1 d | E2-T2 (paralelo com 1) |
| 3 | E2-T2 — pipeline por fonte ativa | P0 | M | 1 d | E2-T3, E3-T2 |
| 4 | E2-T3 — `switch_source.sh` + pré-check | P0 | M | 1 d | E6-T2 |
| 5 | E3-T1 — `source_state.py` | P1 | S | 0,5 d | E3-T2 |
| 6 | E3-T2 — epoch na telemetria | P1 | S | 0,5 d | E4-T1, E5-T1 |
| 7 | E4-T1 — backend por fonte | P2 | M | 1 d | E4-T2 |
| 8 | E4-T2 — frontend por fonte | P2 | S | 0,5 d | — |
| 9 | E5-T1 — fps/janela de troca | P1 | M | 1 d | E6 |
| 10 | E5-T2 — healthcheck do watchdog | P2 | S | 0,5 d | E6 |
| 11 | E5-T3 — rotação de logs | P2 | S | 0,5 d | E6 |
| 12 | E6-T1 — instrumento de latência | P1 | M | 1 d | E6-T2 |
| 13 | E6-T2 — 12 ciclos | P1 | L | 2 d | E7 |
| 14 | E7-T1 — 3 × teste longo 6 h | P2 | L | 2 d de trabalho + 18 h de wall clock | E7-T2 |
| 15 | E7-T2 — docs e fechamento | P1 | S | 0,5 d | — |

**Total:** ~15 dias de trabalho (≈ 2,5 semanas de um desenvolvedor), mais ~20 h de wall clock
de teste que rodam sozinhas. Caminho crítico: **E1-T2 → E2-T2 → E3-T2 → E5-T1 → E6-T2 → E7-T1**.

Marcos:

- **M1 (fim de E2):** troca de fonte é um comando e as três fontes sobem. ~3,5 d.
- **M2 (fim de E3+E4):** estado isolado e dashboard mostra a fonte. ~5,5 d.
- **M3 (fim de E5):** watchdog confiável com alvo por fonte. ~7,5 d.
- **M4 (fim de E6):** latência medida e 12 ciclos verdes. ~10,5 d.
- **M5 (fim de E7):** épico fechado com evidência. ~15 d + wall clock.

---

## 14. Anexos

### 14.1 Contrato de `data/config/active_source`

- Arquivo texto, **uma linha**, o `id` da fonte (`camera`, `drone`, `phone`), newline final.
- Ausente ⇒ `default_source` de `deepstream/config/sources.yaml`.
- Id inválido ⇒ **erro explícito** com a lista de ids válidos (nunca fallback silencioso: uma
  troca que "não pegou" é pior que uma troca que falha).
- Escrita **atômica** (tmp no mesmo diretório + `mv`); quem escreve é só
  `scripts/switch_source.sh`.
- Não versionado (`.gitignore`); exemplo em `data/config/active_source.example`.

### 14.2 Contrato de `data/logs/switch.jsonl`

Uma linha JSON por tentativa de troca:

```json
{"ts":"2026-11-02T14:03:11.224+00:00","from":"camera","to":"drone",
 "probe":{"exit":0,"fps":25.1,"frames":126,"first_frame_s":0.28,"max_gap_ms":180},
 "result":"ok","session_epoch":"2026-11-02T14:03:19.882+00:00","post_check_s":42}
```

`result` ∈ `ok` | `probe_failed` | `post_check_failed` | `rolled_back` | `invalid_source`.

### 14.3 Comandos de referência

```bash
# estado do stack
docker ps -a | grep -i vision

# um container DeepStream, no máximo (regra global)
docker ps --format '{{.Names}}' | grep -ciE 'deepstream|vision-stability-app'

# orçamento de memória de uma sessão
APP_CONTAINER=vision-stability-app bash scripts/mem_budget.sh --loop 60 30

# pré-check de uma fonte (mesmo contrato que o switch_source.sh consome)
python3 scripts/probe/rtsp_read_probe.py --url "$RTSP_URI" --seconds 5 --json

# celular: caminho WHIP vivo (não prova o device)
python3 scripts/probe/whip_probe.py --cacert deploy/certs/ca.crt --path phone

# drone/celular de bancada
./scripts/probe/rtmp_publisher.sh --path drone --seconds 60

# testes puros (sem GPU)
python3 -m pytest deepstream/tests scripts/tests -q

# sintaxe de frontend
node --check dashboard/frontend/app.js
```
