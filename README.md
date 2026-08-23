# Projeto proposto: Vision Stability Lab

A melhor forma de transformar seu objetivo em um projeto de aprendizado é criar um sistema simples de **monitoramento de ocupação de uma área**, em vez de começar com EPI, múltiplos modelos, regras complexas ou reconhecimento de pessoas.

O sistema receberá uma câmera RTSP, detectará pessoas com um modelo TensorRT `.engine`, acompanhará os objetos com tracker, transmitirá vídeo anotado por H.264 e exibirá, em um dashboard, tanto os resultados da visão quanto a saúde do Jetson. O objetivo principal não será apenas “detectar pessoas”, mas provar que o pipeline permanece estável por várias horas.

## Status de execução (2026-08-23)

| Fase | Escopo | Estado |
|---|---|---|
| 0 — Preparação | Ambiente Jetson + DeepStream 7.1 (container L4T), modelo YOLO11m `.engine` gerado | Concluída |
| 1 — RTSP & inferência | Ingestão H.265 + decode HW + `nvinfer`, probes pyds, logs JSON | Concluída |
| 2 — Tracker & regra temporal | `nvtracker` IOU + TTL + estado EMPTY/OCCUPIED, 30 min validados | Concluída (`docs/phase-2-evaluation.md`) |
| 3 — OSD & vídeo H.264 | `nvdsosd` + tee + ramo de preview descartável (`nvv4l2h264enc`), 30 min validados com player externo | Concluída (`docs/phase-3-evaluation.md`) |
| 4 — MediaMTX & dashboard | Preview publicado via RTSP no MediaMTX; dashboard (FastAPI + vanilla JS) com WebRTC/WHEP, API/WebSocket de métricas, cards, gráficos e eventos | Concluída — validação E2E ≥30 min em 2026-08-23 (vídeo, métricas, eventos); revisão UI aplicada |
| 5+ — Estabilidade, watchdog, testes longos | — | Não iniciadas |

Evidências e pendências: `docs/phase-1-evaluation.md` · Incidentes: `docs/incident-log.md`

Nota: a avaliação anterior da Fase 1 declarava sucesso sem inferência executada (pipeline antigo sem `nvinfer` e com depayloader H.264 numa câmera H.265). O documento foi corrigido e o teste será refeito com o novo pipeline.

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
| Fonte | Uma câmera ou arquivo publicado como RTSP |
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
- Múltiplas câmeras.
- Gravação contínua.
- Envio de todos os frames ao backend.
- Processamento de imagem em OpenCV.
- Kubernetes.
- Distribuição em vários Jetsons.

O escopo deve ter somente uma câmera, uma classe de objeto e uma regra temporal. Isso permite concentrar o aprendizado em lifecycle, backpressure, observabilidade e recuperação de falhas.

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

Uma estrutura adequada seria:

```text
vision-stability-lab/
├── deepstream/
│   ├── app.py
│   ├── pipeline.py
│   ├── probes.py
│   ├── tracker_state.py
│   ├── health_metrics.py
│   ├── tegrastats_reader.py
│   └── config/
│       ├── source.yaml
│       ├── infer.txt
│       ├── tracker.yml.legacy
│       └── osd.yml
├── dashboard/
│   ├── backend/
│   └── frontend/
├── mediamtx/
│   └── mediamtx.yml
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── systemd/
├── scripts/
│   ├── run_test.sh
│   ├── collect_metrics.sh
│   └── analyze_memory.py
├── data/
│   └── .gitkeep
├── docs/
│   ├── architecture.md
│   ├── test-plan.md
│   └── incident-log.md
└── README.md
```

## Nome e objetivo final

Eu chamaria o projeto de:

> **Vision Stability Lab: sistema de monitoramento contínuo de ocupação com DeepStream e observabilidade embarcada**

O objetivo não seria simplesmente detectar pessoas. Seria:

> Construir e validar um pipeline de visão contínua capaz de processar uma fonte RTSP por várias horas, mantendo memória, filas, latência e consumo de recursos em comportamento estável, enquanto disponibiliza vídeo anotado e métricas operacionais em um dashboard.

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
- métricas do Jetson;
- backpressure;
- lifecycle;
- watchdog;
- testes de longa duração.

O arquivo disponível no projeto parece ser um TCC sobre IoT e agricultura e não deve impor a lógica deste novo laboratório; ele pode ser usado apenas como referência de organização acadêmica, não como requisito funcional. 
