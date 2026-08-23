# Fase 2 — Tracker e Regra Temporal: Plano de Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar `nvtracker` (IOU) ao pipeline real e substituir o `TrackerState` por um `AreaMonitor` com track_ids estáveis, eventos de entrada/saída, tempo de permanência e limites explícitos de memória, validado por ≥30 min na câmera real.

**Architecture:** Um novo módulo puro-Python (`deepstream/area_monitor.py`) concentra toda a regra temporal (TTL, eventos com `deque(maxlen)`, estado da área) e é testável no host sem pyds/GStreamer. O pipeline GStreamer ganha um único elemento (`nvtracker`) entre `nvinfer` e `fakesink`; o probe existente passa a consumir `obj_meta.object_id`.

**Tech Stack:** Python 3.10 stdlib (unittest, collections.deque), GStreamer/DeepStream 7.1 (nvtracker), Docker `vision-stability-app`, Jetson (aarch64, JetPack R36).

## Global Constraints

- Spec de referência: `docs/superpowers/specs/2026-08-23-phase2-tracker-design.md`.
- Região monitorada: **frame inteiro** (qualquer pessoa detectada ocupa a área).
- Tracker: **nvtracker modo IOU** (`tracker-type=2`), sem modelo visual.
- Saída: **apenas logs JSON de uma linha** + buffer em memória; sem dashboard/streaming/banco.
- Limites explícitos: eventos em `deque(maxlen=200)` (env var `MAX_EVENTS`, padrão 200); TTL de tracks por `TRACK_TTL_S` (padrão 10).
- Estrutura de tracks obrigatória: `{track_id: {"first_seen", "last_seen", "last_bbox"}}`.
- **Este diretório NÃO é um repositório git — ignore/pule qualquer passo de commit.**
- Testes unitários rodam NO HOST (sem pyds): `cd deepstream && python3 -m unittest discover -s tests -p "test_*.py"` — devem passar também dentro do container.
- Não alterar `deepstream/tracker_state.py` nem `deepstream/pipeline.py` (legados da Fase 1); apenas parar de usá-los em `pipeline_real.py`.

---

### Task 1: Módulo `AreaMonitor` com TDD (regra temporal completa)

**Files:**
- Create: `deepstream/tests/test_area_monitor.py`
- Create: `deepstream/tests/__init__.py` (arquivo vazio)
- Create: `deepstream/area_monitor.py`

**Interfaces:**
- Consumes: nada (módulo standalone, só stdlib).
- Produces: `class AreaMonitor(ttl_seconds: float = 10.0, max_events: int = 200)` com:
  - `update(current_tracks: dict[str, tuple], timestamp: float | None = None) -> dict` retornando `{"area_state": str, "transition": str | None, "entered": list, "exited": list[dict]}` onde cada item de `exited` é `{"track_id": ..., "dwell_seconds": float}`;
  - atributos: `tracks` (dict na estrutura do spec), `recent_events` (`deque(maxlen=max_events)` de dicts `{"type", "ts", ...}`), `area_state`, `total_entries`, `total_exits`;
  - `active_dwell_seconds(now=None) -> dict[str, float]`.

- [ ] **Step 1: Criar diretório de testes e teste de entrada**

Criar `deepstream/tests/__init__.py` vazio e `deepstream/tests/test_area_monitor.py`:

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from area_monitor import AreaMonitor


class TestEntryExit(unittest.TestCase):
    def test_first_track_generates_enter_and_occupied(self):
        m = AreaMonitor(ttl_seconds=10.0)
        info = m.update({"t1": (0, 0, 100, 200)}, timestamp=100.0)
        self.assertEqual(m.area_state, "OCCUPIED")
        self.assertEqual(info["transition"], "AREA_OCCUPIED")
        self.assertEqual(info["entered"], ["t1"])
        self.assertEqual(m.total_entries, 1)
        self.assertIn("t1", m.tracks)
        self.assertEqual(m.tracks["t1"]["first_seen"], 100.0)
        self.assertEqual(m.tracks["t1"]["last_bbox"], (0, 0, 100, 200))
        kinds = [e["type"] for e in m.recent_events]
        self.assertIn("track_entered", kinds)

    def test_exit_after_ttl_with_dwell(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"t1": (0, 0, 100, 200)}, timestamp=100.0)
        m.update({"t1": (0, 0, 100, 200)}, timestamp=105.0)
        info = m.update({}, timestamp=116.0)  # 11 s sem aparição > TTL
        self.assertEqual(m.area_state, "EMPTY")
        self.assertEqual(info["transition"], "AREA_EMPTY")
        self.assertNotIn("t1", m.tracks)
        self.assertEqual(m.total_exits, 1)
        exited_event = next(e for e in m.recent_events if e["type"] == "track_exited")
        self.assertEqual(exited_event["track_id"], "t1")
        self.assertAlmostEqual(exited_event["dwell_seconds"], 5.0)
        self.assertAlmostEqual(info["exited"][0]["dwell_seconds"], 5.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Rodar o teste para confirmar falha**

```bash
cd deepstream && python3 -m unittest discover -s tests -p "test_*.py" -v
```

Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'area_monitor'`

- [ ] **Step 3: Implementação mínima do `AreaMonitor`**

Criar `deepstream/area_monitor.py`:

```python
"""Fase 2 — monitor da área com tracks, TTL e eventos limitados.

Regra temporal pura (sem pyds/GStreamer): frame inteiro como região,
estado EMPTY/OCCUPIED, tempo de permanência e limpeza por TTL.
"""

from collections import deque
import time


class AreaMonitor:
    def __init__(self, ttl_seconds=10.0, max_events=200):
        self.ttl_seconds = ttl_seconds
        self.tracks = {}
        self.recent_events = deque(maxlen=max_events)
        self.area_state = "EMPTY"
        self.total_entries = 0
        self.total_exits = 0

    def _record(self, kind, ts, **fields):
        self.recent_events.append({"type": kind, "ts": ts, **fields})

    def update(self, current_tracks, timestamp=None):
        now = timestamp if timestamp is not None else time.time()

        entered = []
        for track_id, bbox in current_tracks.items():
            if track_id not in self.tracks:
                self.tracks[track_id] = {
                    "first_seen": now,
                    "last_seen": now,
                    "last_bbox": bbox,
                }
                entered.append(track_id)
                self.total_entries += 1
            else:
                track = self.tracks[track_id]
                track["last_seen"] = now
                track["last_bbox"] = bbox

        exited = []
        expired = [
            tid for tid, track in self.tracks.items()
            if now - track["last_seen"] > self.ttl_seconds
        ]
        for tid in expired:
            track = self.tracks.pop(tid)
            dwell = round(track["last_seen"] - track["first_seen"], 3)
            self.total_exits += 1
            exited.append({"track_id": tid, "dwell_seconds": dwell})

        new_state = "OCCUPIED" if self.tracks else "EMPTY"
        transition = None
        if self.area_state == "EMPTY" and new_state == "OCCUPIED":
            transition = "AREA_OCCUPIED"
        elif self.area_state == "OCCUPIED" and new_state == "EMPTY":
            transition = "AREA_EMPTY"

        for tid in entered:
            self._record("track_entered", now, track_id=tid)
        for ev in exited:
            self._record("track_exited", now, track_id=ev["track_id"],
                         dwell_seconds=ev["dwell_seconds"])
        if transition:
            self._record("area_transition", now, transition=transition)

        self.area_state = new_state
        return {
            "area_state": new_state,
            "transition": transition,
            "entered": entered,
            "exited": exited,
        }

    def active_dwell_seconds(self, now=None):
        current = now if now is not None else time.time()
        return {
            tid: round(current - track["first_seen"], 3)
            for tid, track in self.tracks.items()
        }
```

- [ ] **Step 4: Completar os testes restantes**

Acrescentar ao `deepstream/tests/test_area_monitor.py` (antes do bloco `if __name__`):

```python
class TestLimitsAndTransitions(unittest.TestCase):
    def test_repeated_frames_do_not_duplicate_entry(self):
        m = AreaMonitor()
        m.update({"t1": (0, 0, 1, 1)}, timestamp=1.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=2.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=3.0)
        self.assertEqual(m.total_entries, 1)
        self.assertEqual(
            sum(1 for e in m.recent_events if e["type"] == "track_entered"), 1)

    def test_multiple_tracks_independent(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"a": (0, 0, 1, 1), "b": (2, 2, 1, 1)}, timestamp=10.0)
        self.assertEqual(len(m.tracks), 2)
        m.update({"b": (2, 2, 1, 1)}, timestamp=25.0)  # 'a' expira (15 s > TTL)
        self.assertEqual(list(m.tracks.keys()), ["b"])
        self.assertEqual(m.total_exits, 1)
        self.assertEqual(m.area_state, "OCCUPIED")

    def test_event_deque_is_bounded(self):
        m = AreaMonitor(ttl_seconds=0.9, max_events=200)
        ts = 0.0
        for i in range(400):
            m.update({f"t{i}": (0, 0, 1, 1)}, timestamp=ts)
            ts += 1.0  # cada track entra e expira -> 2 eventos/iteração
        self.assertLessEqual(len(m.recent_events), 200)
        self.assertEqual(len(m.recent_events), 200)

    def test_transition_sequence_occupied_empty(self):
        m = AreaMonitor(ttl_seconds=10.0)
        self.assertIsNone(m.update({}, timestamp=0.0)["transition"])
        self.assertEqual(
            m.update({"x": (0, 0, 1, 1)}, timestamp=1.0)["transition"],
            "AREA_OCCUPIED")
        self.assertIsNone(
            m.update({"x": (0, 0, 1, 1)}, timestamp=2.0)["transition"])
        self.assertEqual(
            m.update({}, timestamp=20.0)["transition"], "AREA_EMPTY")

    def test_active_dwell_seconds(self):
        m = AreaMonitor(ttl_seconds=10.0)
        m.update({"t1": (0, 0, 1, 1)}, timestamp=100.0)
        self.assertEqual(m.active_dwell_seconds(now=103.5), {"t1": 3.5})
```

- [ ] **Step 5: Rodar todos os testes até verde (host)**

```bash
cd deepstream && python3 -m unittest discover -s tests -p "test_*.py" -v
```

Expected: PASS (7 testes).

- [ ] **Step 6: Repetir a suíte dentro do container**

```bash
docker cp deepstream vision-stability-app:/tmp/deepstream_check && \
docker exec vision-stability-app bash -lc \
  "cd /tmp/deepstream_check && python3 -m unittest discover -s tests -p 'test_*.py'"
```

Expected: PASS (mesmos 7 testes; confirma que o módulo não depende de pyds).

---

### Task 2: Config do nvtracker IOU

**Files:**
- Create: `deepstream/config/tracker_iou.ini`

**Interfaces:**
- Consumes: biblioteca nativa do DS 7.1 no container (`libnvds_tracker.so`).
- Produces: caminho `deepstream/config/tracker_iou.ini` usado pela propriedade `config-file-path` do elemento `nvtracker` na Task 3.

- [ ] **Step 1: Localizar o template oficial dentro do container**

```bash
docker exec vision-stability-app bash -lc \
  "ls /opt/nvidia/deepstream/deepstream-7.1/samples/configs/deepstream-app/ | grep -i tracker; \
   ls /opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_tracker.so"
```

Expected: existe `config_tracker_IOU.yml` (ou equivalente) e `libnvds_tracker.so`.

- [ ] **Step 2: Copiar e adaptar para `deepstream/config/tracker_iou.ini`**

Baseado no conteúdo real do template do Step 1, gravar em `deepstream/config/tracker_iou.ini` algo equivalente a (ajustar paths conforme o Step 1):

```ini
[tracker]
tracker-width=640
tracker-height=384
ll-lib-file=/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_tracker.so
ll-config-file=
tracker-type=2
gpu-id=0
enable-batch-process=1
enable-past-frame=0
display-tracking-id=0
logging-status=0
output-terse-logfile=
output-extensive-logfile=
```

Nota: `tracker-type=2` seleciona o algoritmo IOU (o mais leve). Se o template do DS 7.1 usar chaves diferentes, prevalece o template.

- [ ] **Step 3: Validar que o elemento carrega a config**

```bash
docker exec -w /app/deepstream vision-stability-app bash -lc \
  "GST_DEBUG=2 timeout 15 gst-launch-1.0 nvtracker config-file-path=/app/deepstream/config/tracker_iou.ini num-surfaces-per-frame=1 ! fakesink" 2>&1 | tail -5
```

Expected: pipeline transiciona para PLAYING sem erro fatal (EOF/timeout limpo); nenhum `Failed to load config file`.

---

### Task 3: Integração no `pipeline_real.py`

**Files:**
- Modify: `deepstream/pipeline_real.py`

**Interfaces:**
- Consumes: `AreaMonitor` (Task 1), `deepstream/config/tracker_iou.ini` (Task 2).
- Produces: logs JSON novos `track_entered` / `track_exited`; `interval_metrics` estendido com `entries_interval`, `exits_interval`, `avg_dwell_s`, `events_buffered`, `total_entries`, `total_exits`.

- [ ] **Step 1: Trocar imports e constantes**

Em `deepstream/pipeline_real.py`:

Substituir:
```python
from tracker_state import TrackerState  # noqa: E402
```
por:
```python
from area_monitor import AreaMonitor  # noqa: E402
```

Adicionar junto às constantes existentes:
```python
TRACK_TTL_SECONDS = float(os.environ.get("TRACK_TTL_S", "10"))
MAX_EVENTS_BUFFERED = int(os.environ.get("MAX_EVENTS", "200"))
```

- [ ] **Step 2: Inserir o elemento nvtracker no pipeline**

No `__init__`, substituir `self.tracker_state = TrackerState(ttl_seconds=10.0)` por:
```python
self.monitor = AreaMonitor(ttl_seconds=TRACK_TTL_SECONDS,
                           max_events=MAX_EVENTS_BUFFERED)
self.entries_interval = 0
self.exits_interval = 0
```

Após a criação de `self.infer`, adicionar:
```python
tracker_config = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config", "tracker_iou.ini")
self.tracker = self._make("nvtracker", config_file_path=tracker_config)
```

Incluir `self.tracker` nas duas tuplas: a do loop `for element in (...)` (entre `self.infer` e `self.sink`) e ajustar os links — substituir:
```python
if not self.mux.link(self.infer) or not self.infer.link(self.sink):
    raise RuntimeError("Falha ao linkar mux -> nvinfer -> fakesink")
```
por:
```python
if not self.mux.link(self.infer) or not self.infer.link(self.tracker) or \
        not self.tracker.link(self.sink):
    raise RuntimeError("Falha ao linkar mux -> nvinfer -> nvtracker -> fakesink")
```

- [ ] **Step 3: Adaptar o probe de processamento para o AreaMonitor**

Substituir o corpo de `_on_processed_buffer` entre a criação de `current_tracks`/`person_count` e o fim do laço de frames:

```python
l_obj = frame_meta.obj_meta_list
while l_obj is not None:
    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
    if obj_meta.class_id == PERSON_CLASS_ID:
        rect = obj_meta.rect_params
        # object_id agora vem do nvtracker (estável entre frames)
        current_tracks[str(obj_meta.object_id)] = (
            rect.left, rect.top, rect.width, rect.height)
        person_count += 1
    try:
        l_obj = l_obj.next
    except StopIteration:
        break

state_info = self.monitor.update(current_tracks)
self.persons_detected_interval += person_count
self.persons_max_concurrent = max(self.persons_max_concurrent, person_count)
self.entries_interval += len(state_info["entered"])
self.exits_interval += len(state_info["exited"])

for ev in state_info["exited"]:
    log_event("track_exited", track_id=ev["track_id"],
              dwell_seconds=ev["dwell_seconds"])
for tid in state_info["entered"]:
    log_event("track_entered", track_id=tid)
if state_info["transition"]:
    log_event("area_transition",
              transition=state_info["transition"],
              active_tracks=len(self.monitor.tracks),
              frame_num=frame_meta.frame_num)
```

(Manter intactos os `try/except StopIteration` do laço de frames e o retorno do probe.)

- [ ] **Step 4: Estender `periodic_metrics`**

Dentro de `periodic_metrics`, após `fps_proc = self.fps_proc.update()`, calcular:
```python
dwell_values = list(self.monitor.active_dwell_seconds().values())
avg_dwell = round(sum(dwell_values) / len(dwell_values), 2) if dwell_values else 0.0
```

E acrescentar aos campos do `log_event("interval_metrics", ...)`:
```python
entries_interval=self.entries_interval,
exits_interval=self.exits_interval,
avg_dwell_s=avg_dwell,
events_buffered=len(self.monitor.recent_events),
total_entries=self.monitor.total_entries,
total_exits=self.monitor.total_exits,
```
substituindo as referências antigas `active_tracks=len(self.tracker_state.tracks)` e `area_state=self.tracker_state.area_state` por `self.monitor.*`. Zerar `self.entries_interval`/`self.exits_interval` junto com os demais contadores de intervalo.

- [ ] **Step 5: Compilar e smoke test de 60 s**

```bash
docker exec vision-stability-app bash -lc \
  "python3 -m py_compile /app/deepstream/pipeline_real.py" && \
docker exec -w /app/deepstream -e TEST_DURATION_S=60 -e METRICS_INTERVAL_S=15 \
  vision-stability-app python3 pipeline_real.py 2>&1 | grep '"event"' | tail -12
```

Expected: exit 0; eventos `track_entered`/`area_transition` presentes quando houver pessoas; `interval_metrics` com os campos novos; zero tracebacks.

---

### Task 4: Validação ≥30 min e documentação da fase

**Files:**
- Create: `data/logs/phase2-30min-YYYY-MM-DD.log` (evidência)
- Create: `docs/phase-2-evaluation.md`
- Modify: `docs/incident-log.md` (apenas se surgirem incidentes)

**Interfaces:**
- Consumes: pipeline integrado (Task 3), suíte de testes (Task 1).

- [ ] **Step 1: Suíte unitária verde antes do teste longo**

Host e container (comandos das Tasks 1 Step 5/6). Expected: PASS.

- [ ] **Step 2: Executar 30 minutos contra a câmera real**

```bash
docker exec -w /app/deepstream -e TEST_DURATION_S=1800 -e METRICS_INTERVAL_S=30 \
  vision-stability-app python3 pipeline_real.py > data/logs/phase2-30min-$(date +%F).log 2>&1
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 3: Analisar evidências**

Verificar no log:
- FPS entrada/processamento ~20 estável (mínimo aceitável > 15);
- RSS estável (crescimento < 20 MB ao longo dos 30 min);
- zero `pipeline_error`/tracebacks;
- presença de `track_entered` E pelo menos uma troca real de track (idealmente `track_exited` com `dwell_seconds`; se a cena nunca esvaziar, registrar observação);
- `events_buffered <= 200` em todos os intervalos;
- `total_entries == total_exits + tracks_ativas_no_final` (conservação).

Comando de apoio:

```bash
grep '"event": "interval_metrics"' data/logs/phase2-30min-*.log | python3 - <<'EOF'
import json, sys
rows = [json.loads(l) for l in sys.stdin]
assert all(r["events_buffered"] <= 200 for r in rows), "deque estourou!"
last = rows[-1]
print("fps_in min/max:", min(r["fps_in"] for r in rows), max(r["fps_in"] for r in rows))
print("rss primeiro/ultimo:", rows[0]["rss_mb"], last["rss_mb"])
print("entries/exits totais:", last["total_entries"], last["total_exits"])
print("events_buffered max:", max(r["events_buffered"] for r in rows))
EOF
```

- [ ] **Step 4: Escrever `docs/phase-2-evaluation.md`**

Estrutura espelhada em `docs/phase-1-evaluation.md`: ambiente, correções, tabela de resultados (FPS/RSS/entradas/saídas/dwell), checklist dos 6 entregáveis com estado validado, observações para a Fase 3. Marcar Status: CONCLUÍDA somente se todos os critérios do Step 3 passarem; caso contrário, documentar o desvio e abrir incidente.

---

## Self-Review

- **Cobertura do spec:** track_id (Tasks 2–3), entrada/saída (Tasks 1/3), EMPTY/OCCUPIED (Task 1/3), dwell (Tasks 1/3), TTL (Task 1, env `TRACK_TTL_S`), contadores limitados (Task 1, `deque(maxlen)` + teste), validação 30 min (Task 4). ✔
- **Placeholders:** nenhum TBD/TODO; Task 2 depende do template real do container com fallback explícito escrito. ✔
- **Consistência de tipos:** `AreaMonitor.update()` retorna `entered: list[str]` e `exited: list[dict]` usados exatamente assim na Task 3; nomes `entries_interval`/`exits_interval` idênticos entre Steps 2–4 da Task 3. ✔
