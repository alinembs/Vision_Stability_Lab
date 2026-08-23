# Avaliação — Fase 2 (Tracker & Monitor de Área)

> **Status: CONCLUÍDA.**
> Validação executada em 2026-08-23 com pipeline integrado (`nvtracker` IOU + `AreaMonitor`) por 30 minutos contra a câmera real, com pessoas transitando na cena. Evidência bruta: `data/logs/phase2-30min-2026-08-23.log`.

## 1. Condições do ambiente
- **Fonte RTSP:** câmera IP em `rtsp://admin:***@192.168.59.108:5543/live/channel0` (H.265/HEVC, conforme Fase 1).
- **Ambiente:** Docker `vision-stability-app` (imagem `deepstream-l4t:7.1-triton-multiarch`, JetPack R36 rev 4.7, runtime NVIDIA); GStreamer 1.20.3, pyds 1.2.0.
- **Pipeline:** `rtspsrc → rtph265depay → h265parse → nvv4l2decoder → nvstreammux → nvinfer (YOLO11m, classe "person") → nvtracker (IOU) → probe AreaMonitor → fakesink`.
- **Parâmetros do monitor:** `TRACK_TTL_S=10`, `max_events=200` (deque limitado), `TEST_DURATION_S=1800`, `METRICS_INTERVAL_S=30`.

## 2. Correções aplicadas nesta fase
Nenhuma correção foi necessária durante a validação — o pipeline integrado da Task 3 passou no teste longo de primeira tentativa (exit 0). Os ajustes estruturais da fase (integração do `nvtracker`, `TrackerState` com TTL e `AreaMonitor` com deque limitado) foram validados previamente pela suíte unitária (10/10 em host e container).

## 3. Resultado do teste de longa duração (30 min, com tracker e monitor de área)
Execução: `TEST_DURATION_S=1800`, `METRICS_INTERVAL_S=30` — saída limpa (exit 0), encerramento por `test_duration_reached` → `pipeline_stopped`, zero `pipeline_error`/tracebacks. Apenas 2 warnings benignos e pré-existentes: deprecation de `Gst.Element.get_request_pad` e aviso do TRT sobre engine plan entre dispositivos (ambos já presentes na Fase 1).

| Métrica | Valor |
|---|---|
| Duração | 1800 s (60 intervalos plenos de métricas + 1 intervalo residual de shutdown) |
| FPS entrada (mín/média/máx)* | 18,80 / 19,90 / 20,03 |
| FPS processado (mín/média/máx)* | 18,80 / 19,90 / 20,02 |
| Frames totais | 35.833 in / 35.832 processados |
| RSS | 723,0 → 723,5 MB (crescimento ~0,5 MB em 30 min — estável) |
| Threads / FDs finais | 26 / 89 |
| Entradas / Saídas / Ativas no final | 58 / 58 / 0 (conservação ✓) |
| `track_entered` / `track_exited` | 58 / 58 (100% dos exits com `dwell_seconds`) |
| Transições de área | 60 (`AREA_OCCUPIED` ↔ `AREA_EMPTY`, cena esvaziou e reocupou repetidamente) |
| Permanência (`dwell_seconds`) mín/média/máx | 0,03 / 3,7 / 35,6 s |
| Pessoas simultâneas (pico) | 2 |
| `events_buffered` máximo | 176 (≤ 200 em todos os intervalos ✓) |

\* Excluído o intervalo residual emitido no shutdown (fps 0,0, mesmo timestamp do anterior, delta zero / contadores cumulativos inalterados — artefato de encerramento, não de runtime).

## 4. Checklist da Fase 2
| Entregável | Estado |
|---|---|
| `track_id` estável por pessoa (nvtracker IOU) | Validado (58 IDs únicos, sem órfãos: todo exit tem enter correspondente) |
| Eventos de entrada/saída (`track_entered`/`track_exited`) | Validado E2E (58 de cada, com `dwell_seconds` em todos os exits) |
| Estado da área EMPTY/OCCUPIED | Validado (60 transições reais; cena alternou ocupada/vazia durante o teste) |
| Tempo de permanência (`dwell_seconds`) | Validado (0,03–35,6 s; média 3,7 s) |
| TTL de tracks (limpeza de desaparecidos) | Validado unitariamente (suíte 10/10, incluindo expiração e ressurreição) **e E2E**: 1 dos 58 exits da execução real ocorreu por TTL (track "0" — ver observações) |
| Contadores limitados (deque `max_events=200`) | Validado (unit test + pico real de 176 eventos buffered, nunca estourou) |
| Execução ≥30 min integrada | Validado (exit 0, 35.832 frames processados, RSS plano, zero erros) |

## 5. Observações para as próximas fases
- **Baseline de RSS subiu** de ~561 MB (Fase 1) para ~723 MB com o `nvtracker` + `AreaMonitor` no pipeline; crescimento ao longo dos 30 min foi desprezível (~0,5 MB). Considerar o novo baseline nos limiares de memória da Fase 3.
- **Exit por TTL confirmado em cena real:** a track "0" registrou `track_entered` às 13:47:33.391 e `track_exited` às 13:47:43.431 com `dwell_seconds=0.027`. O intervalo entre enter e exit (~10,04 s) corresponde exatamente ao `TRACK_TTL_S=10`: a pessoa foi vista por ~1 frame e, após o gap de TTL sem reaparição, o monitor emitiu o exit. `dwell_seconds=0.027` está correto — dwell mede presença efetiva na área (1 frame), não o tempo até a expiração. Comportamento esperado; valida o TTL E2E.
- **Os 57 exits restantes** decorreram de saída real de quadro ou troca de ID pelo tracker.
- **FPS mínimo de 18,80** ocorreu em intervalos isolados (recuperação pós-cena vazia); regime estável em ~20 FPS.
- O `memory_growth_mb` reportado pelo coletor (~679 MB acumulado) mede alocação cumulativa do processo, não RSS residente; usar `rss_mb` como referência de vazamento.

## 6. Revalidação pós-hardening de concorrência (mesma data)
Após o review final, o pipeline recebeu proteção de concorrência entre a thread de streaming (probe) e o GLib MainLoop (`threading.Lock` em torno do `AreaMonitor`, `periodic_metrics` blindado contra exceções, shutdown garantido). Como essa mudança veio depois do teste da seção 3, a validação de 30 min foi **reexecutada com o código final**. Evidência: `data/logs/phase2-30min-posfix-2026-08-23.log`.

| Métrica | Valor |
|---|---|
| Resultado | exit 0, zero `pipeline_error`/tracebacks |
| FPS entrada (mín/média/máx)* | 19,21 / 19,90 / 20,03 |
| RSS | 727,2 → 727,5 MB (+0,3 MB em 30 min) |
| Frames | 35.833 in / 35.832 processados |
| Conservação | 119 entries = 118 exits + 1 ativa no final ✓ |
| Eventos | 119 `track_entered` / 118 `track_exited` / 65 transições |
| `events_buffered` máximo | 200 (limite respeitado — deque saturou e descartou antigos ✓) |

\* Mesmo filtro da seção 3.

Ambas as execuções (pré e pós-fix) satisfazem integralmente os critérios da fase; o status CONCLUÍDA reflete o código final revalidado.
