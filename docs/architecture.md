# Documentação de Arquitetura do Vision Stability Lab

## Visão Geral
O **Vision Stability Lab** é um sistema embarcado para monitoramento contínuo de ocupação em Jetson, combinando ingestão RTSP, aceleração por hardware (NVDEC/NVENC), inferência com TensorRT (`nvinfer`), tracking (`nvtracker`), regras temporais de permanência, OSD e streaming H.264 para MediaMTX.

## Estado atual (2026-08-23 — Fase 3)

### Pipeline ativo (`deepstream/pipeline_real.py`)
```text
rtspsrc (latency=200, drop-on-latency, protocols=tcp+udp)
  → rtph265depay / h265parse      [alternável para h264 via RTSP_CODEC]
  → nvv4l2decoder                 [saída NVMM]
  → nvstreammux (batch=1, 1280x720, live-source)
  → nvinfer (config/infer.txt — YOLO11m fp16, classe única "person")
  → nvtracker (IOU, config_tracker_IOU.yml)
  → nvdsosd (bbox + labels; DS 7.1 expõe apenas display-bbox/display-text/process-mode)
  → tee
      ├── queue → fakesink                          [ramo análise — probes]
      └── queue leaky=downstream, max-size-buffers=1 [ramo preview — descartável]
              → nvvideoconvert
              → capsfilter video/x-raw(memory:NVMM),format=NV12
              → nvv4l2h264enc (bitrate PREVIEW_BITRATE, IDR a cada GOP, SPS/PPS inline)
              → h264parse
              → rtph264pay (pt=96, config-interval=-1)
              → udpsink (PREVIEW_HOST:PREVIEW_PORT, sync/async off)
```

- **Probe de entrada** (`nvinfer.sink`): contador de FPS de entrada.
- **Probe de saída** (`fakesink.sink`): FPS processado + leitura de metadados via pyds (`NvDsFrameMeta`/`NvDsObjectMeta`), contagem da classe `person` (class_id=0) e alimentação do `AreaMonitor`.
- **Probe do preview** (`nvv4l2h264enc.sink`): FPS codificado; `preview_dropped_fps = fps_in − fps_preview` nos logs periódicos.
- **Ramo de preview descartável:** a fila com `leaky=downstream` e `max-size-buffers=1` descarta frames antigos se o encoder/consumidor atrasar — o vídeo nunca bloqueia a inferência. Ambos os sinks têm `sync=false, async=false` (obrigatório em splits de tee).
- **Config do preview (env):** `PREVIEW_ENABLED` (padrão 1), `PREVIEW_HOST`/`PREVIEW_PORT` (padrão `127.0.0.1:5600`), `PREVIEW_BITRATE` (4 Mbps), `PREVIEW_IDR_GOP` (30).
- **Pad-added do rtspsrc** filtra apenas pads `media=video` (a câmera também publica áudio PCMA).
- **Logs JSON** de uma linha por evento; métricas periódicas a cada `METRICS_INTERVAL_S` (30 s padrão); duração de teste via `TEST_DURATION_S` (0 = executa até interromper).

### Componentes
1. **`deepstream/pipeline_real.py`** — pipeline instrumentado (cadeia acima com OSD e preview H.264, probes, logs JSON).
2. **`deepstream/tracker_state.py`** — tracks com TTL (10 s) e transições `EMPTY`/`OCCUPIED`.
3. **`deepstream/health_metrics.py`** — coleta psutil (RSS, crescimento de memória, threads, fds) + RAM/CPU do sistema.
4. **`deepstream/tegrastats_reader.py`** — estatísticas nativas do Jetson (uso planejado para as fases seguintes).
5. **`data/`** — modelo YOLO11m (`yolo11m.onnx`, engine `model_b1_gpu0_fp16.engine`) e parser customizado (`libnvdsinfer_custom_impl_Yolo.so`).
6. **`deploy/docker-compose.yml`** — serviço `vision-app` montando `deepstream/` e `data/` no container L4T 7.1 com runtime NVIDIA.
7. **`mediamtx/`** — servidor de mídia para as Fases 3–4 (ainda não integrado).

### Observações e dependências
- Scripts legados (`pipeline_rtsp_real.py`, `rtsp_*.py`, `phase1_*.py`) permanecem no repositório como histórico de diagnóstico; o caminho suportado da Fase 1 é `pipeline_real.py`.
- `pyds` (bindings Python do DeepStream) precisa ser compilado dentro do container — ver `docs/incident-log.md` INC-003.
