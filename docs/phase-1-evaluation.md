# Avaliação — Fase 1 (RTSP & Inferência)

> **Status: CONCLUÍDA.**
> Revalidação executada em 2026-08-23 com pipeline completo (`nvstreammux`/`nvinfer`), pyds 1.2.0 instalado e inferência ativa por 30 minutos contra a câmera real. Evidência bruta: `data/logs/phase1-30min-2026-08-23.log`.

## 1. Condições do ambiente
- **Fonte RTSP:** câmera IP em `rtsp://admin:***@192.168.59.108:5543/live/channel0`.
- **Codec real:** H.265/HEVC — confirmado por inspeção de caps ao vivo (`encoding-name=(string)H265`). A câmera também publica áudio PCMA, filtrado fora do pipeline (pad-added aceita apenas `media=video`).
- **Ambiente:** Docker `vision-stability-app` (imagem `deepstream-l4t:7.1-triton-multiarch`, JetPack R36 rev 4.7, runtime NVIDIA).

## 2. Correções aplicadas (2026-08-23)
1. **Codec corrigido e registrado (INC-002):** `rtspsrc → rtph265depay → h265parse`, com alternância via `RTSP_CODEC=h264|h265`.
2. **Pipeline completo:** `nvv4l2decoder → nvstreammux (1280x720, live) → nvinfer (config/infer.txt — YOLO11m, classe "person") → fakesink`.
3. **pyds desbloqueado (INC-003):** o release **v1.2.0** do `deepstream_python_apps` publica o wheel `pyds-1.2.0-cp310-cp310-linux_aarch64.whl`, compatível com DeepStream 7.1 + Python 3.10 (o v1.2.2 só publica cp312, que causava o 404 original). Wheel persistido em `deploy/wheels/` e integrado ao `deploy/Dockerfile`. O build manual do pyds 1.1.8 foi abandonado.
4. **Parser customizado:** a `libnvdsinfer_custom_impl_Yolo.so` exporta `NvDsInferParseYolo` (não `NvDsInferParseCustomYolo`) — corrigido no `infer.txt`; chaves inválidas no DS 7.1 removidas (`visualize-input-bounding-boxes`, `net-stride`, `confidence-threshold`, `io-iou-threshold` → `pre-cluster-threshold`/`nms-iou-threshold`).
5. **Correções de código:** link explícito decoder→mux via request pad (`sink_0`); propriedade `protocols` do rtspsrc como valor numérico (TCP+UDP=4); `hash(gst_buffer)` exigido pelo pyds 1.2.0.

## 3. Resultado do teste de longa duração (30 min, com inferência)
Execução: `TEST_DURATION_S=1800`, `METRICS_INTERVAL_S=30` — saída limpa (exit 0), sem erros, warnings ou tracebacks no log.

| Métrica | Valor |
|---|---|
| Duração | 1800 s (60 intervalos de métricas) |
| FPS entrada (mín/média/máx) | 19,38 / 19,97 / 20,03 |
| FPS processado (mín/média/máx) | 19,35 / 19,97 / 20,03 |
| Frames totais | 35.959 in / 35.958 processados |
| RSS | 560,9 → 561,4 MB (crescimento ~0,5 MB em 30 min — estável) |
| Threads / FDs finais | 23 / 88 |
| Detecções | contínuas na cena; pico de 2 pessoas simultâneas; transição `AREA_OCCUPIED` registrada |

## 4. Checklist da Fase 1
| Entregável | Estado |
|---|---|
| Entrada RTSP | Validado (>30 min com inferência, H.265) |
| Depayloader conforme codec real (H.265) | Validado |
| Detecção de pessoas (`nvinfer`) | Validado (YOLO11m, class_id=0, detecções contínuas) |
| FPS de entrada e processamento | Validado (~20 FPS estável em ambos) |
| Contagem de objetos | Validado (probe pyds, máx. 2 simultâneas) |
| Logs estruturados (JSON) | Validado (`interval_metrics`, `area_transition`, …) |
| Execução ≥30 min com inferência | Validado (35.958 frames, RSS plano, zero erros) |

## 5. Observações para as próximas fases
- O RSS de regime (~561 MB) inclui o motor TensorRT carregado; o crescimento marginal em 30 min foi desprezível.
- A cena da câmera permaneceu ocupada durante todo o teste; validar transições `AREA_EMPTY` em cenário sem pessoas antes da Fase 2.
- Sem tracker NvDCF (escopo Fase 2): os IDs usados no `TrackerState` vêm do `object_id` do nvinfer + TTL próprio; revisar estabilidade de IDs quando o tracker for integrado.
