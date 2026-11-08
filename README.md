# Projeto proposto: Vision Stability Lab

A melhor forma de transformar seu objetivo em um projeto de aprendizado é criar um sistema simples de **monitoramento de ocupação de uma área**, em vez de começar com EPI, múltiplos modelos, regras complexas ou reconhecimento de pessoas.

O sistema receberá **uma fonte por vez** — câmera IP por RTSP, drone por RTMP ou celular Android por WHIP (`docs/adr/adr-0006-troca-de-fonte.md`) —, detectará pessoas com um modelo TensorRT `.engine`, acompanhará os objetos com tracker, transmitirá vídeo anotado por H.264 e exibirá, em um dashboard, tanto os resultados da visão quanto a saúde do Jetson. O objetivo principal não será apenas “detectar pessoas”, mas provar que o pipeline permanece estável por várias horas.

## Status de execução (atualizado em 2026-11-01)

| Fase | Escopo | Estado |
|---|---|---|
| 0 — Preparação | Ambiente Jetson + DeepStream 7.1 (container L4T), modelo YOLO11m `.engine` gerado | Concluída |
| 1 — RTSP & inferência | Ingestão H.265 + decode HW + `nvinfer`, probes pyds, logs JSON | Concluída |
| 2 — Tracker & regra temporal | `nvtracker` IOU + TTL + estado EMPTY/OCCUPIED, 30 min validados | Concluída (`docs/phase-2-evaluation.md`) |
| 3 — OSD & vídeo H.264 | `nvdsosd` + tee + ramo de preview descartável (`nvv4l2h264enc`), 30 min validados com player externo | Concluída (`docs/phase-3-evaluation.md`) |
| 4 — MediaMTX & dashboard | Preview publicado via RTSP no MediaMTX; dashboard (FastAPI + vanilla JS) com WebRTC/WHEP, API/WebSocket de métricas, cards, gráficos e eventos | Concluída — validação E2E ≥30 min em 2026-08-23 (vídeo, métricas, eventos); revisão UI aplicada |
| Multi-fonte (ADR-0006) | Câmera fixa, drone e celular como entradas **alternativas** (uma ativa por vez); catálogo declarativo, troca por script e pela tela | Concluída no Orin NX — gate E4 PASS em 2026-11-01 (`docs/baselines/2026-11-01-gate-e4.md`); em validação no Orin Nano |
| 5+ — Estabilidade, watchdog, testes longos | — | Não iniciadas |

Evidências e pendências: `docs/phase-1-evaluation.md` · Incidentes: `docs/incident-log.md`

Nota: a avaliação anterior da Fase 1 declarava sucesso sem inferência executada (pipeline antigo sem `nvinfer` e com depayloader H.264 numa câmera H.265). O documento foi corrigido e o teste será refeito com o novo pipeline.

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
  o endereço do momento (e as URLs de captura). `localhost` não serve (ver
  Fase 4).
- O certificado é **por host**: `deploy/certs/generate-certs.sh` monta o SAN a
  partir dos IPv4 atuais do host + `<hostname>.local` + `vsl.local`. Quando o
  IP muda, `make up` reemite **só o certificado do servidor**, preservando a
  CA: quem já instalou o `ca.crt` continua confiando, sem reinstalar nada.
  A pasta é gitignored: nunca copie o par `server.crt`/`server.key` de um host
  para o outro.
- As duas CAs têm o **mesmo CN** (`Vision Stability Lab Internal CA`), mas
  chaves diferentes. Uma máquina que abre os dois dashboards precisa das duas
  instaladas; regenerar (`--force`) num host invalida **só** aquela CA para os
  clientes. Ver a seção da Fase 4.
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

Esse problema é menor que um sistema de EPI, mas preserva as partes mais importantes:

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

## 2. Escopo mínimo

### Incluído

| Componente | Escopo |
|---|---|
| Fonte | Câmera IP (RTSP), drone (RTMP) ou celular Android (WHIP) — **uma ativa por vez**, catálogo em `deepstream/config/sources.yaml` |
| Inferência | Um detector de pessoas em TensorRT `.engine` |
| Tracker | NvDCF ou tracker equivalente |
| Regra | Contagem, entrada, saída e permanência |
| Vídeo | OSD com bounding boxes e informações básicas |
| Streaming | H.264 por hardware para MediaMTX |
| Dashboard | Vídeo, estado da área e métricas |
| Métricas | FPS, latência, memória, CPU, GPU, temperatura e tracks |
| Persistência | Histórico limitado de eventos e métricas |
| Estabilidade | Testes de 1, 6 e 24 horas |
| Operação | Reconexão RTSP e watchdog simples |

### Não incluído na primeira versão

Deixe estes recursos para etapas posteriores:

- Reconhecimento facial.
- Re-identificação de pessoas.
- Classificação de comportamento.
- Segundo modelo SGIE.
- Detecção de EPI.
- Múltiplas fontes **simultâneas** — as três entradas do ADR-0006 são alternativas, não concorrentes.
- Gravação contínua.
- Envio de todos os frames ao backend.
- Processamento de imagem em OpenCV.
- Kubernetes.
- Distribuição/orquestração em vários Jetsons — o Orin NX e o Orin Nano são dois ambientes de teste independentes, não um cluster.

O escopo deve ter **uma fonte ativa por vez**, uma métrica de ocupação (pessoas — o modelo detecta e desenha as 80 classes COCO, mas a telemetria conta só `person`) e uma regra temporal. Isso permite concentrar o aprendizado em lifecycle, backpressure, observabilidade e recuperação de falhas.

## 3. Arquitetura recomendada

### Pipeline do Jetson

A primeira versão pode seguir este desenho:

```text
RTSP source
  ↓
rtspsrc / nvurisrcbin
  ↓
hardware decode
  ↓
nvstreammux
  ↓
nvinfer
  ↓
nvtracker
  ↓
probe de metadados
  ↓
nvdsosd
  ↓
tee
  ├── queue leaky → fakesink
  └── queue leaky → nvvideoconvert
                         ↓
                    nvv4l2h264enc
                         ↓
                      h264parse
                         ↓
                      rtph264pay
                         ↓
                    MediaMTX
```

O probe deve trabalhar principalmente com:

- `frame_num`;
- timestamp;
- número de objetos;
- `object_id`;
- classe detectada;
- confiança;
- bounding box;
- estado da região;
- tempo de permanência.

A NVIDIA descreve probes como pontos para interceptar buffers e metadados processados, permitindo extrair resultados do `nvinfer` e gerar contagens ou overlays. [docs.nvidia](https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_service_maker_python_into_to_pipeline_api.html)

### Ingestão das três fontes

O pipeline continua com **uma** entrada por execução. Quem materializa a
escolha é o catálogo (`deepstream/config/sources.yaml`) mais o
`data/config/active_source`. O MediaMTX é o ponto comum das duas fontes que
publicam por push:

```text
drone   ── RTMP :1935/drone ──┐
celular ── WHIP :8889/phone ──┴──► MediaMTX ── RTSP :8554/{drone,phone} ──┐
câmera  ── RTSP (direto, RTSP_URI) ───────────────────────────────────────┴──► rtspsrc
```

A câmera não passa pelo MediaMTX na entrada — o pipeline fala RTSP direto com
ela. O preview anotado é que é publicado no path `live`, independentemente da
fonte ativa. Depayloader e parser saem do `codec` da fonte (D5), e a URI de cada
fonte vem do YAML ou de uma variável de ambiente (`uri_env`), nunca hardcoded no
pipeline.

### Separação entre vídeo e eventos

O pipeline não deve enviar imagens ao dashboard por requisição HTTP a cada frame.

Use dois canais separados:

```text
DeepStream → H.264/RTP → MediaMTX → WebRTC ou HLS → navegador

DeepStream → eventos JSON → MQTT ou API → dashboard
DeepStream → métricas JSON → MQTT ou API → dashboard
```

O vídeo permanece comprimido e acelerado. O backend recebe apenas dados pequenos, como:

```json
{
  "camera_id": "lab-camera-01",
  "timestamp": "2026-08-23T00:00:00Z",
  "people": 2,
  "area_state": "occupied",
  "event": null,
  "fps_in": 15.0,
  "fps_infer": 15.0,
  "latency_ms": 82.4
}
```

Para a primeira validação, use esta sequência:

1. Gravar vídeo anotado localmente.
2. Enviar RTP/UDP para outro processo.
3. Integrar MediaMTX.
4. Exibir no navegador.
5. Adicionar histórico e alertas no dashboard.

Essa ordem reduz o número de variáveis durante o diagnóstico.

## 4. Métricas do dashboard

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

## 5. Dashboard sugerido

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

## 6. Contrato de dados

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

## 7. Fases de implementação

### Fase 0 — Preparação

Objetivo: confirmar o ambiente.

Entregáveis:

- JetPack e DeepStream instalados.
- `gst-inspect-1.0` funcionando.
- `nvinfer` disponível.
- `nvtracker` disponível.
- `nvdsosd` disponível.
- `nvv4l2h264enc` disponível.
- `tegrastats` funcionando.
- Modelo `.engine` validado.

Comandos úteis:

```bash
gst-inspect-1.0 nvinfer
gst-inspect-1.0 nvtracker
gst-inspect-1.0 nvdsosd
gst-inspect-1.0 nvv4l2h264enc
tegrastats --interval 1000
```

As propriedades dos elementos podem variar entre versões do JetPack, portanto valide a instalação local com `gst-inspect-1.0` antes de copiar configurações de outro ambiente. A documentação atual do Jetson mantém exemplos e propriedades específicas por versão. [docs.nvidia](https://docs.nvidia.com/jetson/archives/r38.4/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html)

É o que acontece entre os dois ambientes de teste — Orin NX com DeepStream 7.1 e Orin Nano com 9.1 (ver “Ambientes de teste”). Por isso o `nvtracker` resolve a biblioteca por um caminho estável entre as duas bases, o encoder do preview é escolhido por disponibilidade (hardware → software) e `make doctor` compara a L4T do host com a base de imagem do `deploy/.env`, bloqueando só o que impede subir.

### Fase 1 — RTSP e inferência

Objetivo: processar uma câmera sem dashboard.

Entregáveis:

- Entrada RTSP.
- Detecção de pessoas.
- FPS de entrada e processamento.
- Contagem de objetos.
- Logs estruturados.
- Execução por pelo menos 30 minutos.

Nesta fase, ainda não implemente tracker complexo nem streaming externo.

### Fase 2 — Tracker e regra temporal

Objetivo: provar que o sistema consegue manter estado sem crescimento ilimitado.

Entregáveis:

- `track_id`.
- Entrada e saída da região.
- Estado `EMPTY` ou `OCCUPIED`.
- Tempo de permanência.
- TTL para estados antigos.
- Contadores limitados.

Estrutura recomendada:

```python
tracks = {
    track_id: {
        "last_seen": timestamp,
        "first_seen": timestamp,
        "last_bbox": bbox
    }
}
```

Limpeza:

```python
if now - state["last_seen"] > TRACK_TTL_SECONDS:
    del tracks[track_id]
```

Também use limites explícitos para eventos:

```python
recent_events = deque(maxlen=200)
```

### Fase 3 — OSD e vídeo H.264

Objetivo: visualizar o resultado sem extrair frames para Python.

Pipeline conceitual:

```text
nvdsosd
  → tee
  → queue leaky
  → nvvideoconvert
  → video/x-raw(memory:NVMM),format=NV12
  → nvv4l2h264enc
  → h264parse
  → rtph264pay
  → udpsink
```

A NVIDIA documenta o uso de `nvv4l2h264enc` como encoder H.264 acelerado e a combinação com `h264parse`, `rtph264pay` e `udpsink` para transporte RTP/UDP. [docs.nvidia](https://docs.nvidia.com/jetson/archives/r38.4/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html)

Configure o ramo de preview como descartável:

```text
leaky = downstream
max-size-buffers = 1
max-size-bytes = 0
max-size-time = 0
```

O vídeo de preview não pode bloquear a inferência.

### Fase 4 — MediaMTX e dashboard

Objetivo: disponibilizar vídeo e métricas no navegador.

Entregáveis:

- MediaMTX recebendo o stream.
- Reprodução por WebRTC ou HLS.
- API de métricas.
- WebSocket com dados em tempo real.
- Cards de saúde.
- Gráficos históricos.
- Lista de eventos.

Para baixa latência, prefira WebRTC. Para a primeira integração, HLS pode ser mais simples, embora normalmente introduza mais atraso.

> **Acesse o dashboard pelo IP da LAN atual do host que serve o painel** —
> rode `make ip` no host para ver o endereço do momento (ex.:
> `http://192.168.59.102:8081`) —, não por `http://localhost:8081`. O MediaMTX
> é publicado amarrado no `LAN_IP` **detectado no `make up`** (nunca `0.0.0.0`
> — DIV-003 em `docs/incident-log.md`), então `127.0.0.1:8889` não responde; e
> o frontend deriva a URL do WHEP do hostname da página
> (`dashboard/frontend/app.js`: `${location.hostname}:8889`), logo em
> `localhost` o player negocia contra uma porta fechada e o vídeo não aparece.
> Se o roteador trocar o IP, rode `make up` de novo: as portas são republicadas
> no endereço novo e o certificado do servidor é reemitido para ele (a CA é
> preservada, então nada a reinstalar nos clientes).

> **A máquina que abre o dashboard precisa confiar na CA daquele host.** Desde o
> E0-T4 a 8889 é TLS (`webrtcEncryption: yes`) e o certificado é assinado pela CA
> de `deploy/certs/` **do host que serve** — que não está no store do sistema e
> é diferente em cada Jetson (o SAN lista os IPs do host e os nomes locais
> `<hostname>.local`/`vsl.local`). Sem instalar o
> `ca.crt` certo nessa máquina, o `fetch` do WHEP falha na verificação de
> certificado e o player anuncia "certificado do MediaMTX não é confiável neste
> navegador" (antes o sintoma era "sem stream do mediamtx", com tudo `healthy`
> no `docker ps`).
>
> Trocar o IP **não** exige novo `ca.crt`: `make up` reemite o certificado do
> servidor com o SAN novo usando a mesma CA. Só `generate-certs.sh --force`
> (ou uma CA perdida) obriga a reinstalar o arquivo. Em rede com mDNS, prefira
> o nome estável — `https://<hostname>.local:8443/capture.html` ou
> `https://vsl.local:8443/capture.html` — que sobrevive ao DHCP.
>
> Antes de instalar, pegue o arquivo **no host que serve** — o `deploy/certs/` é
> gitignored e diferente em cada Jetson — e confirme que é a CA que assinou
> aquele host, da máquina do navegador:
>
> ```bash
> IP=$(ssh usuario@<host> 'cd ~/Vision_Stability_Lab && make -s ip' | awk '/^IP da LAN:/ {print $4}')
> scp usuario@<host>:'~/Vision_Stability_Lab/deploy/certs/ca.crt' ./ca.crt
> curl --cacert ca.crt -s -o /dev/null -w '%{http_code}\n' "https://$IP:8889/"
> # 404 = TLS validou (404 é a raiz do MediaMTX); erro de certificado = CA errada
> ```
>
> Instalação, na máquina que abre o dashboard:
>
> ```bash
> sudo cp ca.crt /usr/local/share/ca-certificates/vsl-ca.crt
> sudo update-ca-certificates   # Chrome/Edge/Chromium no Linux
> ```
>
> Deve responder `1 added`. Se responder `0 added`, o arquivo já estava
> instalado — normalmente sinal de que é a CA do **outro** host (as duas têm o
> mesmo `CN`; compare o `notBefore` e prove com `curl --cacert` antes de
> instalar — INC-008 em `docs/incident-log.md`).
>
> Firefox tem store próprio: `about:preferences#privacy` → *Ver certificados* →
> *Autoridades* → *Importar* → *Confiar nesta CA para identificar sites*.
> Windows: `certutil -addstore -f Root ca.crt` como administrador (Autoridades
> de Certificação Raiz Confiáveis, máquina local). macOS:
> `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.crt`.
> No **Android**, o caminho é o do runbook
> (`docs/runbooks/captura-celular.md`, seção 3) — Configurações → Segurança →
> Criptografia e credenciais → Instalar um certificado → Certificado de CA. O
> aviso de certificado é só do lado do navegador: o pipeline e o
> `whip_probe.py --cacert` não dependem disso.
>
> O `ca.crt` de um host **não** serve para o outro: NX e Nano têm CAs distintas
> com o mesmo CN. Uma máquina que abre os dois dashboards instala as duas;
> `generate-certs.sh --force` num host invalida só aquela CA para os clientes.

### Fase 5 — Testes de estabilidade

Execute testes controlados:

| Teste | Duração | Condição |
|---|---:|---|
| Baseline | 30 min | câmera e pipeline normais |
| Longa duração | 6 h | operação normal |
| Estresse | 2 h | alta quantidade de pessoas |
| Viewer lento | 1 h | cliente de vídeo atrasado |
| RTSP interrompido | 30 min | desconectar e reconectar câmera |
| Dashboard desligado | 1 h | pipeline sem consumidor |
| Reinício controlado | 3 ciclos | parar e iniciar o pipeline |

Durante cada teste, registre:

- RSS;
- RAM do Jetson;
- GPU;
- CPU;
- FPS;
- frames descartados;
- tracks ativos;
- número de reconexões;
- tamanho das filas;
- temperatura;
- erros GStreamer;
- erros CUDA;
- tempo de recuperação.

## 8. Critérios de sucesso

O projeto estará concluído quando atender a estes critérios:

### Estabilidade

- A memória sobe durante o warm-up e depois entra em platô.
- O RSS não apresenta crescimento linear contínuo.
- Nenhuma fila permanece crescendo.
- O número de tracks ativos retorna a zero quando a cena fica vazia.
- O pipeline continua funcionando sem o dashboard conectado.
- Fechar o cliente de vídeo não interrompe a inferência.

### Desempenho

- FPS processado permanece próximo do FPS configurado.
- Latência permanece dentro de uma faixa previsível.
- Frames antigos são descartados quando necessário.
- O uso de CPU não cresce ao longo do tempo.
- O encoder utiliza o caminho de hardware esperado.

### Recuperação

- A câmera pode ser desconectada e reconectada.
- O pipeline detecta ausência de frames.
- O watchdog registra a causa de uma falha.
- O processo pode ser reiniciado sem deixar containers ou processos órfãos.
- Erros fatais de CUDA resultam em reinício do processo, não em tentativa de continuar.

## 9. Watchdog inicial

Não comece com um watchdog muito complexo. Implemente primeiro estas regras:

```text
Se não houver frame por 30 segundos:
    tentar reconectar o RTSP

Se FPS ficar abaixo de 30% do alvo por 2 minutos:
    registrar degradação

Se RSS crescer mais de 15% em relação à baseline:
    emitir alerta

Se RAM do sistema ultrapassar 90%:
    reiniciar o pipeline

Se ocorrer erro CUDA fatal:
    encerrar o processo
```

O watchdog deve ficar fora do pipeline principal, por exemplo em:

```text
systemd
Docker restart policy
supervisor externo
```

Um `while True` dentro do mesmo processo não é suficiente para proteger contra travamentos nativos, erros de driver ou deadlocks.

## 10. Organização do repositório

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

## Nome e objetivo final

Eu chamaria o projeto de:

> **Vision Stability Lab: sistema de monitoramento contínuo de ocupação com DeepStream e observabilidade embarcada**

O objetivo não seria simplesmente detectar pessoas. Seria:

> Construir e validar um pipeline de visão contínua capaz de processar **uma fonte por vez** (câmera fixa, drone ou celular) por várias horas, mantendo memória, filas, latência e consumo de recursos em comportamento estável, enquanto disponibiliza vídeo anotado e métricas operacionais em um dashboard — com o mesmo stack rodando em dois Jetsons (Orin NX e Orin Nano).

Esse escopo é suficientemente simples para ser concluído, mas ainda exercita as competências mais importantes:

- ingestão RTSP;
- decode acelerado;
- TensorRT;
- DeepStream metadata;
- tracker;
- regras temporais;
- OSD;
- encoder H.264;
- RTP/MediaMTX;
- WebRTC ou HLS;
- troca de fonte entre câmera, drone e celular sem reconfiguração em runtime;
- métricas do Jetson;
- backpressure;
- lifecycle;
- watchdog;
- portabilidade do mesmo stack entre JetPack 6/DeepStream 7.1 e JetPack 7/DeepStream 9.1;
- testes de longa duração.

O arquivo disponível no projeto parece ser um TCC sobre IoT e agricultura e não deve impor a lógica deste novo laboratório; ele pode ser usado apenas como referência de organização acadêmica, não como requisito funcional. 
