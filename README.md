# Vision Stability Lab

Laboratório de estabilidade de visão contínua sobre DeepStream, rodando em Jetson. O pipeline processa **uma fonte por vez** — câmera IP por RTSP, drone por RTMP ou celular Android por WHIP (ADR-0006, `docs/adr/adr-0006-troca-de-fonte.md`) —, detecta pessoas com um modelo TensorRT `.engine`, acompanha os objetos com tracker, publica o vídeo anotado e expõe métricas operacionais em um dashboard.

O alvo é **estabilidade por horas**: memória que entra em platô, filas que não crescem, latência previsível, recuperação automática quando a fonte cai e encerramento limpo. Detectar pessoas é o caminho para exercitar isso, não o objetivo em si. O mesmo stack roda em dois Jetsons — Orin NX e Orin Nano —, cada host com seu `deploy/.env` e sua base de imagem.

## Ambientes de teste: Orin NX e Orin Nano

A mesma árvore do repositório serve **dois** Jetsons. Não há fork nem branch de
portabilidade: o que muda entre eles é o `deploy/.env` de cada host e a base de
imagem, que o `make doctor` valida contra a plataforma.

| Ambiente | IP na LAN | Papel | Plataforma | Base do stack |
|---|---|---|---|---|
| **Orin NX** | DHCP — `make ip` | produção e referência dos gates | L4T R36.4.7 · JetPack 6 · DeepStream **7.1** · Python 3.10 no container | `deploy/Dockerfile` padrão (DS 7.1, wheel `pyds` cp310) |
| **Orin Nano** (`pr3via-jetson`) | DHCP — `make ip` | validação de portabilidade | L4T R39.2.1 · JetPack 7.2.1 · DeepStream **9.1** · CUDA 13 · TRT 10.16.2.10 · Ubuntu 24.04 · Python 3.12.3 · GStreamer 1.24.2 | `DS_BASE_IMAGE=nvcr.io/nvidia/deepstream:9.1-triton-multiarch` + wheel `pyds` cp312 |

Estado em 2026-11-01: o **NX** é o ambiente de produção do VSL e onde os gates
são fechados (E4 PASS). O **Nano** sobe o stack completo com `make up` e serve
dashboard, telemetria e preview — medido no `/api/snapshot` às 20:12 UTC:
`healthy`, `fps_in=20`, `fps_preview=20`, sessão `camera` iniciada às 19:44 UTC.
O gate de portabilidade (P4) e as pendências de plataforma estão em
[`docs/plans/2026-11-01-portabilidade-orin-nano.md`](docs/plans/2026-11-01-portabilidade-orin-nano.md).

Cada host é um **stack independente** — não é cluster nem distribuição:

- **Um** container DeepStream por host (regra global do projeto). Os dois
  Jetsons podem rodar ao mesmo tempo porque são hosts distintos; dois
  containers do projeto no **mesmo** host continuam proibidos.
- `deploy/.env` é por host: `RTSP_URI`, `DASHBOARD_PORT` e, opcionalmente,
  `LAN_IP`. O IP **não** é mais fixo no repositório nem obrigatório no `.env`:
  `make up` detecta o endereço atual (`scripts/host_ip.sh`: rota default e,
  depois, `hostname -I`) e exporta `LAN_IP` para o compose — é ele que amarra a
  publicação das portas do MediaMTX e alimenta o `MTX_WEBRTCADDITIONALHOSTS` (o
  IP que o SDP anuncia ao navegador). Se o roteador trocar o IP, basta
  `make up`; nada de editar arquivo.
- O dashboard é sempre `http://<IP atual do host>:8081`. Use `make ip` para ver
  o endereço do momento (e as URLs de captura). `localhost` não serve: as portas
  do MediaMTX são publicadas no IP da LAN e o frontend negocia o WHEP contra
  `${location.hostname}:8889`, então em `127.0.0.1` o player bate numa porta
  fechada (DIV-003 em `docs/incident-log.md`).
- O certificado é **por host**: `deploy/certs/generate-certs.sh` monta o SAN a
  partir dos IPv4 atuais do host + `<hostname>.local` + `vsl.local`. Quando o
  IP muda, `make up` reemite **só o certificado do servidor**, preservando a
  CA: quem já instalou o `ca.crt` continua confiando, sem reinstalar nada.
  A pasta é gitignored: nunca copie o par `server.crt`/`server.key` de um host
  para o outro.
- As duas CAs têm o **mesmo CN** (`Vision Stability Lab Internal CA`), mas
  chaves diferentes. Uma máquina que abre os dois dashboards precisa das duas
  instaladas; regenerar (`--force`) num host invalida **só** aquela CA para os
  clientes. O SAN tem de conter o IP usado pelo navegador, senão o WHEP falha na
  verificação — ver `deploy/certs/generate-certs.sh` e
  [`docs/plans/2026-11-01-portabilidade-orin-nano.md`](docs/plans/2026-11-01-portabilidade-orin-nano.md).
- As portas são as mesmas nos dois hosts (`8081`, `8554`, `1935`, `8888`,
  `8889`, `8189`, `8443`, além de `8890/udp` do SRT), cada uma publicada no IP
  do seu host — por isso os dois coexistem na mesma LAN sem conflito.

## Fontes disponíveis: uma ativa por vez

O laboratório aceita **três entradas alternativas**, nunca simultâneas
(ADR-0006). O catálogo é declarativo, em `deepstream/config/sources.yaml`:

| id | nome | entrada | codec | gate |
|---|---|---|---|---|
| `camera` | câmera fixa | RTSP direto no pipeline (`RTSP_URI`, `deploy/.env`) | H.265 | latência p95 ≤ 300 ms |
| `drone` | drone | RTMP `rtmp://<LAN_IP>:1935/drone` → MediaMTX path `drone` → RTSP `:8554/drone` | H.264 | reportado, não gated (link de rádio) |
| `phone` | celular (Android) | WHIP `https://<LAN_IP>:8889/phone/whip` → path `phone` → RTSP `:8554/phone` | H.264 | latência p95 ≤ 300 ms |

```text
câmera fixa ── RTSP (H.265) ──────────────────────────────┐
drone       ── RTMP :1935/drone ──┐                       │
celular     ── WHIP :8889/phone ──┴──► MediaMTX ──────────┤ RTSP :8554/{drone,phone}
                                                          ▼
                                        pipeline (batch_size=1, uma fonte)
                                                          │
                                                          └──► preview H.264 no path `live` ──► dashboard (WHEP)
```

Regras que valem para qualquer fonte:

- **Uma ativa por vez** (D1): `batch_size=1`, sem tiler, sem grade no OSD e sem
  engine `b3`. A memória unificada do Jetson é o orçamento que decide isso.
- **Estado isolado por sessão** (D2): todo registro de telemetria carrega
  `source_id` e `session_epoch`; trocar de fonte reinicia tracks, eventos,
  dwell e contadores. Nada atravessa fontes.
- **Codec por fonte** (D5): o depayloader/parser vem do catálogo, não de uma
  variável global do container.
- **Sem reconfiguração em runtime** (D4): trocar de fonte é escrever
  `data/config/active_source` e reiniciar o processo do pipeline.
- **Pré-check obrigatório** (D7): a fonte pedida tem de estar publicando (sonda
  RTSP de 5 s); se não estiver, nada é escrito e o container fica intacto.

Precedência da fonte ativa — a primeira que existir vence:

```text
ACTIVE_SOURCE (ambiente) > data/config/active_source > default_source > primeira com enabled: true
```

Trocar:

```bash
make source                    # fonte ativa no disco
make sources                   # catálogo visto pelo dashboard (paths mascarados)
make switch SOURCE=drone       # pré-check → active_source → restart → pós-check
./scripts/switch_source.sh phone --timeout 120
```

O script não deixa a troca "meio feita": se o pós-check não confirmar
`interval_metrics` com `fps_in > 0` na fonte nova, ele **restaura** a anterior e
sai `1`. Cada execução deixa uma linha em `data/logs/switch.jsonl`; contratos de
exit code e diagnóstico estão em
[`docs/runbooks/troca-de-fonte.md`](docs/runbooks/troca-de-fonte.md).

Adicionar uma fonte nova é editar o catálogo (id, `uri`/`uri_env`, `codec`,
`target_fps`, `enabled`, `gate`) e reiniciar — não é mexer no Python. URI com
credencial fica em `deploy/.env` via `uri_env`, nunca no YAML (DIV-001).

## 1. Problema do projeto

### Monitoramento contínuo de uma área

Imagine uma câmera apontada para uma pequena sala, garagem, corredor ou bancada de trabalho.

O sistema deverá:

- Detectar pessoas continuamente.
- Acompanhar cada pessoa com um tracker.
- Contar quantas pessoas estão presentes.
- Identificar entrada e saída da área.
- Medir o tempo de permanência.
- Gerar um evento quando a área estiver ocupada.
- Exibir o vídeo anotado no dashboard.
- Coletar métricas de desempenho e estabilidade do Jetson.

Exemplo de regra:

```text
Se uma pessoa permanecer na região monitorada por mais de 10 segundos:
    gerar evento AREA_OCCUPIED

Quando a última pessoa sair:
    gerar evento AREA_EMPTY
```

O escopo é menor que o de um sistema industrial de conformidade ou de monitoramento, mas preserva as partes mais importantes do caminho:

```text
RTSP
  ↓
Decode acelerado
  ↓
DeepStream
  ↓
TensorRT .engine
  ↓
Tracker
  ↓
Regras temporais
  ↓
OSD
  ├── vídeo H.264
  └── eventos e métricas
          ↓
      Dashboard
```

A arquitetura deve privilegiar o caminho acelerado da Jetson e evitar a conversão contínua para NumPy, BGR, JPEG e Base64. As bindings Python do DeepStream permitem acessar metadados de inferência e analytics anexados aos buffers sem necessariamente extrair o frame para a CPU. [docs.nvidia](https://docs.nvidia.com/metropolis/deepstream/5.0/python-api/gettingstarted.html)

## 2. Métricas do dashboard

O dashboard deve ter três grupos: visão, pipeline e hardware.

### Métricas de visão

- FPS de entrada.
- FPS processado.
- Frames descartados.
- Número de pessoas detectadas.
- Número de tracks ativos.
- Eventos de entrada.
- Eventos de saída.
- Tempo médio de permanência.
- Latência de captura até inferência.
- Latência de inferência até publicação.
- Estado da câmera RTSP.

### Métricas do processo

- RSS do processo.
- Memória virtual.
- Threads ativas.
- File descriptors abertos.
- Profundidade das filas.
- Tempo desde o último frame.
- Tempo desde a última publicação.
- Estado do pipeline GStreamer.
- Quantidade de reconexões RTSP.
- Quantidade de reinícios do pipeline.

### Métricas do Jetson

- RAM usada e disponível.
- Swap usada.
- CPU total.
- CPU por núcleo.
- Utilização do `GR3D`.
- Utilização do NVDEC.
- Utilização do NVENC.
- Temperatura.
- Frequência da GPU.
- Frequência da CPU.
- Potência, quando disponível.

O `tegrastats` é apropriado para essa camada porque informa uso de memória, processadores, GPU e motores de vídeo do Jetson. Ele pode ser executado periodicamente e gravar os dados em arquivo para processamento posterior. [docs.nvidia](https://docs.nvidia.com/jetson/archives/r34.1/DeveloperGuide/text/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html)

Uma métrica especialmente importante será:

```text
memory_growth_mb =
    rss_atual_mb - rss_depois_do_warmup_mb
```

No dashboard, mostre:

```text
Baseline após warm-up: 1.240 MB
RSS atual:             1.268 MB
Crescimento:           +28 MB
Tempo de execução:     06:42:18
Status:                estável
```

## 3. Dashboard sugerido

Você pode implementar o dashboard em Go, FastAPI ou Node.js. Para esse projeto, eu usaria:

```text
Backend:
  Go ou FastAPI

Frontend:
  React ou HTML + JavaScript

Transporte:
  WebSocket para dados ao vivo

Vídeo:
  WebRTC ou HLS via MediaMTX

Persistência:
  PostgreSQL ou SQLite inicialmente

Mensageria:
  MQTT, opcional na primeira versão
```

A tela principal poderia ter:

```text
┌──────────────────────┬────────────────────────┐
│ Vídeo anotado        │ Estado da área         │
│                      │ OCCUPIED               │
│                      │ Pessoas: 2             │
│                      │ Tracks: 2              │
├──────────────────────┼────────────────────────┤
│ Saúde do sistema     │ Gráficos               │
│ RAM: 38%             │ RAM / RSS / GPU / FPS   │
│ GPU: 62%             │ últimas 30 minutos     │
│ CPU: 44%             │                        │
│ Temperatura: 57 °C   │                        │
└──────────────────────┴────────────────────────┘
```

### Fontes no dashboard

O painel implementado tem uma faixa **“fontes · uma ativa por vez”** com o
catálogo (`GET /api/sources`), o path de cada fonte em modo leitura, o selo da
ativa e um botão *ativar* por fonte. Gráficos e eventos podem ser filtrados por
fonte (`/api/history?source=`, `/api/events?source=`), e o cabeçalho mostra a
fonte ativa com `session_epoch` e uptime.

O botão *ativar* é **opt-in**: no deploy padrão o endpoint
`POST /api/source/switch` responde `503` e o dashboard é somente leitura. Para
habilitar, `make up-switch SWITCH_TOKEN=…` — o painel passa a escutar em
`127.0.0.1` (acesso por túnel SSH), porque o overlay monta o socket do Docker
(DIV-004 em `docs/incident-log.md`). O caminho executado é o mesmo do
`switch_source.sh`: pré-check, `active_source`, restart e pós-check com
rollback.

### Indicadores de estabilidade

Use estados simples:

- Verde: dentro do comportamento esperado.
- Amarelo: degradação ou tendência de crescimento.
- Vermelho: pipeline sem frames, memória crítica ou processo reiniciado.

Exemplos:

```text
RAM: estável
RSS: crescimento anormal
RTSP: reconectando
FPS: abaixo do esperado
Fila: acumulada
Pipeline: saudável
```

Não mostre apenas o valor atual. Mostre também a tendência:

```text
RSS atual: 1.268 MB
Crescimento nos últimos 30 min: +4 MB
Tendência: estável
```

## 4. Contrato de dados

Defina desde o começo um formato único para métricas:

```json
{
  "timestamp": "2026-08-23T00:00:00Z",
  "device_id": "jetson-orin-nx",
  "pipeline_id": "occupancy-v1",
  "source": {
    "uri": "camera-01",
    "connected": true,
    "last_frame_age_ms": 42
  },
  "vision": {
    "input_fps": 15.0,
    "processed_fps": 14.9,
    "dropped_frames": 12,
    "active_tracks": 2,
    "objects": 2,
    "area_state": "occupied"
  },
  "process": {
    "rss_mb": 1268,
    "threads": 18,
    "open_fds": 42,
    "queue_depth": 0
  },
  "jetson": {
    "ram_used_mb": 3120,
    "ram_available_mb": 4870,
    "swap_used_mb": 0,
    "cpu_percent": 44.0,
    "gpu_percent": 62.0,
    "temperature_c": 57.0,
    "nvdec_percent": 18.0,
    "nvenc_percent": 11.0
  },
  "health": {
    "status": "healthy",
    "uptime_seconds": 24138,
    "restart_count": 0,
    "last_error": null
  }
}
```

Publique métricas a cada 1 ou 5 segundos para o dashboard, mas persista somente uma amostra a cada 15 ou 30 segundos. Assim, você evita transformar o banco em outra fonte de crescimento ilimitado.

> **Contrato implementado (multi-fonte).** Todo registro carrega `source_id`,
> `source_name`, `session_epoch` e `run`, e os endpoints aceitam `?source=<id>`
> (`/api/history?source=drone`, `/api/events?source=phone`). O `/api/snapshot`
> traz `source: {id, name, session_epoch, since}` e `health` derivado **apenas**
> da sessão ativa: `healthy` exige `interval_metrics` fresco (<90 s) com
> `fps_processed > 0`; `stale` com telemetria velha; `down` sem telemetria. Um
> id sem histórico devolve lista vazia (HTTP 200), não erro — e nenhum ponto de
> uma fonte aparece na série de outra.

Para eventos, publique somente transições:

```json
{
  "event_id": "evt-01",
  "type": "AREA_OCCUPIED",
  "camera_id": "camera-01",
  "track_count": 1,
  "timestamp": "2026-08-23T00:01:22Z"
}
```

Não publique um evento de “área ocupada” em todos os frames.

## 5. Critérios de sucesso

O projeto é considerado estável quando atende aos critérios abaixo. Os números vêm de `data/logs/phase5/*-report.md` e dos baselines de campo em `docs/baselines/`.

### Estabilidade

- A memória sobe durante o warm-up e depois entra em platô — baseline de 30 min com RSS avg **856,6 MB** e p95 **891,1 MB**; longrun de 6 h com avg **935,8 MB** e **0 reconexões**.
- Nenhuma fila permanece crescendo: `queue_analyze` e `queue_preview` com máximo **0**.
- O número de tracks ativos retorna a **0** quando a cena fica vazia.
- O pipeline continua funcionando sem o dashboard conectado: `dashboard_off` rodou 1 h com FPS ~**19,84**.
- Fechar o cliente de vídeo não interrompe a inferência — o ramo de preview é descartável (`queue leaky` com 1 buffer).

### Desempenho

- `fps_processed ≥ 0,95 × fps_in` na janela medida (gate E4: 17,77 / 17,81 ≈ **0,998**; no campo com o drone, **0,999–1,001**).
- `fps_preview` acompanha o processado; descarte só acontece sob backpressure (`viewer_slow`: avg **14,91** fps com fila de preview **0**).
- O encoder usa o caminho de hardware quando existe; sem `/dev/v4l2-nvenc` a base cai para software (no Orin Nano, VP8).

### Recuperação

- Fonte derrubada e religada volta ao primeiro frame em **≤ 20 s** (medido **11,56 s** no baseline de campo do drone) **sem reiniciar o processo**.
- Sem frame por 30 s o `rtspsrc` é reiniciado; o watchdog externo escala depois (ver “Observabilidade e watchdog”).
- `docker stop` encerra em **≤ 2 s** com `exit_code=0` e nenhum `exit 137`.
- Fonte perdida depois de 5 tentativas encerra o processo com **exit 2** — o processo não fica "saudável" mentindo.
- Zero `OOMKilled`; **um** container DeepStream por host.

## 6. Observabilidade e watchdog

**Telemetria.** O pipeline grava JSONL em `data/logs/live.jsonl`. São dois tipos de linha: `interval_metrics` (fps_in, fps_processed, fps_preview, `queue_buffers`, `rss_mb`, tracks, `area_state`, eventos de entrada/saída) e eventos pontuais (`rtsp_reconnect_started`, `rtsp_reconnect_resumed`, `source_reconnect_exhausted`, `preview_branch_failed`, `pipeline_stopped` com `exit_code`, `shutdown_requested`). Cada linha carrega o namespace da sessão (`source_id` + `session_epoch`), então métricas de sessões diferentes não se misturam.

**Dashboard.** Backend FastAPI com frontend vanilla JS: `/api/snapshot`, `/api/history`, `/api/events` e `/ws`; vídeo anotado por WebRTC/WHEP com fallback HLS. A tela mostra lâmpadas de saúde, gráficos de FPS e RSS, estado da área e o painel do observador de fontes.

**Watchdog externo.** Roda em container próprio, com acesso ao `docker.sock` — um `while True` dentro do processo do pipeline não protege contra travamento nativo, erro de driver ou deadlock. Regras implementadas em `scripts/watchdog.py`:

```text
Sem frame por 60 s              → evento e escalada
FPS < 30% do alvo por 2 min     → degradação
RSS > 15% acima da baseline     → alerta
RAM do sistema > 90%            → docker restart
Erro CUDA fatal                 → reinício
```

Além disso o watchdog detecta crash-loop e `container_missing`, respeita o marcador `data/config/switch_in_progress` (troca de fonte não pode virar alarme) e inclui o observador de fontes (E7), que grava `data/config/observer_state.json` e emite eventos em `watchdog.jsonl`.

**Contrato.** O watchdog mede e decide o restart; a política do observador (`scripts/observer_policy.py`) só decide a ação (`SWITCH_TO`, `STOP_STANDBY`, `RUN_LEVEL2_PROBE`, `START_FROM_STANDBY`), que o watchdog executa.

## 7. Organização do repositório

A árvore do repositório hoje (subconjunto representativo):

```text
vision-stability-lab/
├── deepstream/
│   ├── pipeline_real.py          # pipeline instrumentado (OSD, preview, probes)
│   ├── sources.py                # catálogo e fonte ativa (ADR-0006)
│   ├── source_state.py           # isolamento por sessão (source_id/session_epoch)
│   ├── source_reconnect.py       # máquina de reconexão da fonte
│   ├── tracker_state.py
│   ├── health_metrics.py
│   ├── tegrastats_reader.py
│   └── config/
│       ├── sources.yaml          # câmera | drone | celular (uma ativa por vez)
│       ├── infer.txt
│       ├── tracker_iou.ini
│       └── osd.yml
├── dashboard/
│   ├── backend/                  # FastAPI: /api/snapshot, /api/history, /api/events, /ws
│   ├── frontend/                 # vanilla JS: player WHEP, cards, gráficos, fontes
│   └── tests/
├── mediamtx/
│   └── mediamtx.yml              # paths live/drone/phone, TLS na 8889
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── docker-compose.switch.yml # overlay opt-in da troca pela tela
│   ├── nginx/capture-web.conf    # página de captura do celular (8443)
│   ├── certs/                    # gitignored: ca.crt/server.crt por host
│   └── .env                      # por host: RTSP_URI, DASHBOARD_PORT (LAN_IP opcional: auto)
├── scripts/
│   ├── switch_source.sh          # troca com pré-check, pós-check e rollback
│   ├── switch_source_lib.py
│   ├── watchdog.py
│   └── probe/                    # rtsp_read_probe, whip_probe, rtmp_publisher, source_probe
├── data/
│   ├── config/active_source      # fonte ativa (uma linha)
│   ├── parser/
│   └── logs/                     # live.jsonl (telemetria), switch.jsonl (auditoria)
├── docs/
│   ├── architecture.md
│   ├── adr/                      # ADR-0006 (multi-fonte)
│   ├── plans/
│   ├── baselines/
│   ├── runbooks/                 # captura-celular, troca-de-fonte, drone-campo
│   └── incident-log.md
├── Makefile                      # make up / doctor / status / sources / switch / test
└── README.md
```
