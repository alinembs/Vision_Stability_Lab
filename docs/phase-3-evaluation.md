# Avaliação — Fase 3 (OSD & Vídeo H.264)

> **Status: CONCLUÍDA.**
> Validação executada em 2026-08-23 com pipeline integrado (`nvdsosd` + ramo de preview `nvv4l2h264enc → RTP/UDP`) por 30 minutos contra a câmera real, incluindo validação visual por player externo e teste de fechar/reabrir o consumidor. Evidência bruta: `data/logs/phase3-30min-2026-08-23.log`.

## 1. Condições do ambiente
- **Fonte RTSP:** câmera IP em `rtsp://admin:***@192.168.59.108:5543/live/channel0` (H.265).
- **Ambiente:** container `phase3-test` (imagem reconstruída com pyds 1.2.0 embutido — ver correções), JetPack R36, GStreamer 1.20.3.
- **Pipeline:** `rtspsrc → rtph265depay → h265parse → nvv4l2decoder → nvstreammux → nvinfer (YOLO11m) → nvtracker (IOU) → nvdsosd → tee`
  - *ramo análise:* `queue → fakesink` (probes);
  - *ramo preview:* `queue leaky=downstream, max-size-buffers=1 → nvvideoconvert → capsfilter NV12(NVMM) → nvv4l2h264enc (4 Mbps, IDR a cada 30) → h264parse → rtph264pay (pt=96, config-interval=-1) → udpsink`.
- **Parâmetros:** `PREVIEW_HOST=192.168.59.110` (PC do operador), `TEST_DURATION_S=1800`, `METRICS_INTERVAL_S=30`, `TRACK_TTL_S=10`.
- **Validação visual:** ffplay 6.1.1 no PC via SDP (`rtp://@:5600`), com OSD visível; teste manual de fechar (Ctrl+C) e reabrir o player durante a execução.

## 2. Correções aplicadas nesta fase
1. **Imagem desatualizada / pyds ausente:** containers novos criados via `docker compose run` não tinham `pyds` (a instalação do INC-003 tinha sido feita em runtime no container antigo). A imagem foi reconstruída com o Dockerfile já corrigido: o `COPY ... /tmp/pyds.whl` quebrava o `pip install` ("not a valid wheel filename") — agora copia o wheel com o nome original (`deploy/Dockerfile`). Correção definitiva; INC-003 não depende mais de passo manual.
2. **`nvdsosd` no DS 7.1:** as propriedades `border-width`, `font-size` e `font` não existem mais no elemento (estilo fino agora é via `NvDsDisplayMeta`). O pipeline usa apenas `display-bbox`, `display-text` e `process-mode=1` (GPU). `config/osd.yml` permanece como referência histórica obsoleta.
3. **Capsfilter:** `gst_element_set_property` não parseia strings de caps com parênteses; o capsfilter NV12(NVMM) passou a receber um objeto `Gst.Caps.from_string(...)`.
4. **Fragmentação de memória do host (INC-004):** a primeira tentativa falhou com CUDA OOM ao deserializar o engine (40 MB) apesar de ~5 GB de RAM livre — memória física fragmentada após dias de uptime (`lfb 10x4MB`). Resolvido com `drop_caches` via sudo (`lfb` → 196x4MB); registrado no `docs/incident-log.md`.

## 3. Resultado do teste de longa duração (30 min, com OSD + encoder H.264 ativo)
Execução: exit **0**, encerramento limpo por `test_duration_reached` → `pipeline_stopped`. Únicos warnings: banner do driver NVIDIA (pré-existente desde a Fase 1) e plugins de áudio ausentes no container (irrelevantes — fonte só tem vídeo).

| Métrica | Valor |
|---|---|
| Duração | 1800 s (60 intervalos plenos + residual de shutdown) |
| FPS entrada (mín/média/máx) | 16,60 / 19,84 / 20,67 |
| FPS processado (mín/média/máx) | 16,60 / 19,84 / 20,67 |
| FPS preview (mín/média/máx) | 16,60 / 19,84 / 20,67 — idêntico aos anteriores |
| `preview_dropped_fps` máximo | 0,10 (leaky queue praticamente não atuou) |
| Frames totais | 35.722 in / 35.721 processados |
| RSS | 783,8 → 891,3 MB (warm-up do encoder até ~min 14, depois platô em 891,02–891,47) |
| Threads / FDs | 32 / 109–111 (constantes) |
| Entradas / Saídas | 27 / 27 (conservação ✓, cena esvaziou no final) |
| Transições de área | Coerentes com a presença real do operador na cena |
| Dwell máximo observado | 26,5 s (operador parado diante da câmera) |
| Pico de pessoas simultâneas | 1 |
| `events_buffered` máximo | 72 (≤ 200 ✓) |

## 4. Validação visual (operador externo)
| Item | Resultado |
|---|---|
| Stream RTP recebido no PC (`192.168.59.110:5600`) | OK (via SDP; payload dinâmico 96 exige arquivo SDP no ffplay) |
| OSD com bounding boxes das pessoas | Confirmado pelo operador |
| Latência percebida | Sub-second |
| Fechar o player (Ctrl+C) não interrompeu a inferência | Validado — `fps_processed` seguiu ~20 e nenhum `pipeline_error` foi emitido |
| Reabrir o player no meio do stream | Validado (`config-interval=-1` reenvia SPS/PPS a cada IDR) |

## 5. Checklist da Fase 3
| Entregável | Estado |
|---|---|
| `nvdsosd` anotando detecções sem extração de frames para Python | Validado (metadados desenhados em GPU, `process-mode=1`) |
| Tee com ramo de análise isolado | Validado (probes e métricas intactas durante todo o teste) |
| Ramo de preview descartável (`leaky=downstream`, 1 buffer) | Validado (nunca bloqueou a inferência; descartes ≤ 0,1 fps) |
| Encode HW H.264 (`nvv4l2h264enc`) no caminho NVMM | Validado (NV12/NVMM direto, sem cópia para CPU) |
| Transporte RTP/UDP para consumidor externo | Validado (ffplay via SDP no PC do operador) |
| Preview não bloqueia inferência (fechar/reabrir player) | Validado E2E |
| Execução ≥ 30 min com encoder ativo | Validado (exit 0, 35.721 frames, RSS em platô, zero erros) |

## 6. Observações para as próximas fases
- **Baseline de RSS subiu** para ~891 MB com o encoder ativo (vs. 723 MB da Fase 2). Platô confirmado após warm-up de ~14 min; usar este baseline nos limiares de memória da Fase 5 (+15% ≈ 1025 MB).
- **SDP necessário para consumidores diretos**: com payload dinâmico, qualquer player precisa do arquivo SDP (disponibilizado nesta avaliação). Na Fase 4 o MediaMTX elimina esse atrito (RTSP/RTMP/WebRTC têm sinalização própria).
- **UDP puro não gera backpressure** de cliente lento; o cenário crítico "viewer lento" (Fase 5) deve ser exercitado via MediaMTX/consumidor RTSP real.
- **Monitorar `lfb` do tegrastats** antes dos testes longos: INC-004 mostra que fragmentação física derruba o TRT mesmo com RAM livre. Candidato a check automático no script de teste (Fase 5).
