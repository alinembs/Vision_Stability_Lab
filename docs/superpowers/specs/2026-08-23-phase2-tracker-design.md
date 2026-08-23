# Design — Fase 2: Tracker e Regra Temporal

**Data:** 2026-08-23
**Status:** Aprovado pelo usuário
**Objetivo:** provar que o sistema mantém estado sem crescimento ilimitado, com track_ids estáveis, eventos de entrada/saída, tempo de permanência e limites explícitos de memória.

## Decisões (definidas com o usuário)

1. **Região monitorada:** frame inteiro — qualquer pessoa detectada ocupa a área.
2. **Origem do track_id:** `nvtracker` em modo IOU (plugin nativo DeepStream, sem modelo, custo mínimo).
3. **Entrega de eventos:** apenas logs JSON estruturados (uma linha por evento) + buffer em memória. Sem dashboard, streaming ou persistência (Fases 3+).

## Arquitetura

```
rtspsrc → rtph265depay → h265parse → nvv4l2decoder → nvstreammux
        → nvinfer (YOLO11m) → nvtracker (IOU) → probe de tracking → fakesink
```

Mudança única no pipeline GStreamer (`deepstream/pipeline_real.py`): inserir o elemento
`nvtracker` entre `nvinfer` e `fakesink`, configurado por
`deepstream/config/tracker_iou.ini` (adaptado do template do DS 7.1: tracker 640x384,
`tracker-type=2`/IOU, `ll-lib-file` apontando para `libnvds_tracker_iou.so` do DS).

O `NvDsObjectMeta.object_id` passa a ser estável entre frames, atribuído nativamente
pelo tracker — o probe não faz casamento heurístico de bboxes.

## Componente central: `AreaMonitor`

Novo módulo puro-Python `deepstream/area_monitor.py`, sem dependência de pyds/GStreamer
(testável no host). Substitui o `TrackerState` da Fase 1.

### Estado

```python
tracks = {
    track_id: {
        "first_seen": timestamp,
        "last_seen": timestamp,
        "last_bbox": (left, top, width, height),
    }
}
recent_events = deque(maxlen=200)
```

### Regras

- **Entrada:** primeiro frame em que um `track_id` aparece → evento `track_entered`.
- **Saída:** track removida quando `now - last_seen > TRACK_TTL_SECONDS`
  (env var `TRACK_TTL_S`, padrão 10) → evento `track_exited` com
  `dwell_seconds = last_seen - first_seen`.
- **Dwell time ativo:** consultável a qualquer momento como
  `now - first_seen` para tracks vivas.
- **Estado da área:** `EMPTY` ↔ `OCCUPIED`; mudança gera evento
  `area_transition` (valores `AREA_OCCUPIED` / `AREA_EMPTY`).
- **Contadores limitados:** além do `deque(maxlen=200)`, apenas agregados
  numéricos (`total_entries`, `total_exits`) — nenhum estado cresce sem bound.

### Interface

```python
class AreaMonitor:
    def __init__(self, ttl_seconds=10.0, max_events=200): ...
    def update(self, current_tracks: dict[str, tuple], timestamp=None) -> dict:
        """Retorna {'area_state', 'transition', 'entered', 'exited', 'dwell_updates'}."""
```

## Integração no pipeline

- `pipeline_real.py`: troca `TrackerState` por `AreaMonitor`; o probe itera
  `NvDsObjectMeta` usando `obj_meta.object_id` e alimenta `monitor.update()`.
- Eventos novos emitidos como logs JSON de uma linha: `track_entered`,
  `track_exited` (com `dwell_seconds`), além dos já existentes.
- `interval_metrics` estendido com: `entries_interval`, `exits_interval`,
  `avg_dwell_s` (tracks ativas), `events_buffered`, `tracks_total_seen`.

## Validação da fase

1. **Testes unitários no host** (`python3 -m unittest discover deepstream/tests`,
   unittest stdlib, sem deps externas):
   - entrada e saída de track geram eventos corretos;
   - dwell time calculado corretamente (ativo e finalizado);
   - expiração por TTL remove a track;
   - deque respeita `maxlen=200` (200+ eventos não cresce);
   - transições EMPTY/OCCUPIED corretas.
2. **Execução ≥30 min** contra a câmera real (`TEST_DURATION_S=1800`) com tracker
   ativo, mesmos critérios da Fase 1 (FPS ~20 estável, RSS plano, zero erros)
   + evidência real de `track_entered`/`track_exited` nos logs.

## Critérios de aceite

| Entregável | Critério |
|---|---|
| track_id | IDs estáveis entre frames via nvtracker IOU |
| Entrada/saída da região | Eventos `track_entered`/`track_exited` nos logs |
| Estado EMPTY/OCCUPIED | Transições registradas e coerentes |
| Tempo de permanência | `dwell_seconds` presente no evento de saída |
| TTL para estados antigos | Track removida após `TRACK_TTL_S` sem aparição |
| Contadores limitados | Deque com maxlen; teste unitário prova o bound |
| Estabilidade ≥30 min | Sem crescimento ilimitado de RSS/tracks/eventos |

## Fora de escopo

ROI/polígono configurável, dashboard, streaming externo, persistência em banco,
tracker NvDCF (reservado se o IOU mostrar instância de ID insuficiente na prática).

## Adenda (2026-08-23, pós-implementação)

Divergências registradas entre este design e o código efetivamente implementado:

1. **`update()` não retorna `'dwell_updates'`.** O dict de retorno de
   `AreaMonitor.update()` contém `{'area_state', 'transition', 'entered', 'exited'}`;
   dwell time ativo é obtido via `AreaMonitor.active_dwell_seconds()`, não por
   atualizações retornadas a cada frame.
2. **Métricas usam `total_entries`/`total_exits`** (contadores agregados do monitor)
   no lugar do campo `tracks_total_seen` previsto na seção "Integração no pipeline".
3. **Tracker configurado via props `ll-lib-file`/`ll-config-file`** no elemento
   `nvtracker` (`libnvds_nvmultiobjecttracker.so` + YAML
   `deepstream/config/config_tracker_IOU.yml`). O arquivo
   `deepstream/config/tracker_iou.ini` mencionado acima permanece apenas como
   referência inerte — não é consumido pelo pipeline.
