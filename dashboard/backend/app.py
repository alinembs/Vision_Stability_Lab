"""Dashboard backend — Vision Stability Lab (Fase 4).

Consome o JSONL de telemetria do pipeline (data/logs/live.jsonl),
mantém histórico em memória (ring buffers limitados) e expõe:

  GET  /api/snapshot   estado atual + saúde derivada
  GET  /api/history    amostras de métricas (últimos N minutos)
  GET  /api/events     lista de eventos recentes
  WS   /ws             push em tempo real (snapshot + métricas + eventos)

Sem persistência além do próprio JSONL (o histórico é reconstruído
replayando o arquivo na partida).
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dashboard")

TELEMETRY_FILE = Path(os.environ.get(
    "TELEMETRY_FILE", "/app/data/logs/live.jsonl"))
METRICS_HISTORY_MAX = int(os.environ.get("METRICS_HISTORY_MAX", "480"))  # 4h @30s
EVENTS_HISTORY_MAX = int(os.environ.get("EVENTS_HISTORY_MAX", "500"))
POLL_INTERVAL_S = float(os.environ.get("TAIL_POLL_S", "0.5"))
STALE_AFTER_S = float(os.environ.get("STALE_AFTER_S", "90"))

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class DashboardState:
    """Estado compartilhado alimentado pelo tailer."""

    def __init__(self):
        self.metrics_history = deque(maxlen=METRICS_HISTORY_MAX)
        self.events = deque(maxlen=EVENTS_HISTORY_MAX)
        self.last_metric_ts = None      # ts do último interval_metrics
        self.pipeline_started_at = None
        self.preview_transport = None
        self.subscribers = set()

    def apply(self, record):
        """Aplica um registro JSONL vindo do pipeline."""
        event = record.get("event")
        if event == "interval_metrics":
            record["recv_at"] = time.time()
            self.metrics_history.append(record)
            self.last_metric_ts = record.get("ts")
        elif event == "startup":
            self.pipeline_started_at = record.get("ts")
        elif event == "preview_branch_ready":
            self.preview_transport = record.get("transport")
            self._push_event(record)
        else:
            # eventos relevantes para a lista (transições, tracks, erros...)
            if event in ("track_entered", "track_exited", "area_transition",
                         "pipeline_error", "pipeline_warning", "eos",
                         "interrupted_by_user", "test_duration_reached",
                         "pipeline_stopped", "pipeline_playing",
                         "preview_branch_ready"):
                self._push_event(record)
        return event

    def _push_event(self, record):
        self.events.append(record)
        return record

    def health(self):
        now = time.time()
        last = self.metrics_history[-1] if self.metrics_history else None
        if last is None or (now - last["recv_at"]) > STALE_AFTER_S * 3:
            return {"status": "down", "reason": "sem telemetria"}
        age = now - last["recv_at"]
        if age > STALE_AFTER_S:
            return {"status": "stale", "reason": f"telemetria há {int(age)}s"}
        fps = last.get("fps_processed") or 0
        rss = last.get("rss_mb")
        if fps <= 0:
            return {"status": "degraded", "reason": "fps_processed = 0"}
        return {"status": "healthy", "reason": None}

    def snapshot(self):
        last = self.metrics_history[-1] if self.metrics_history else None
        errors = [e for e in self.events
                  if e.get("event") == "pipeline_error"]
        return {
            "server_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "health": self.health(),
            "latest_metrics": last,
            "pipeline_started_at": self.pipeline_started_at,
            "preview_transport": self.preview_transport,
            "error_count": len(errors),
            "last_error": errors[-1] if errors else None,
            "events_recent": list(self.events)[-50:],
        }


state = DashboardState()
app = FastAPI(title="Vision Stability Lab")


async def tail_telemetry(start_offset=None):
    """Segue live.jsonl (tolera rotação/truncamento) e difunde novidades.

    start_offset: byte a partir do qual ler; evita perder linhas escritas
    entre o fim do replay inicial e a abertura do arquivo aqui.
    """
    logger.info("tailer aguardando %s", TELEMETRY_FILE)
    while True:
        if not TELEMETRY_FILE.exists():
            await asyncio.sleep(1)
            continue
        try:
            with open(TELEMETRY_FILE) as fh:
                fh.seek(start_offset if start_offset is not None
                        else os.path.getsize(TELEMETRY_FILE))
                start_offset = None
                while True:
                    line = fh.readline()
                    if not line:
                        await asyncio.sleep(POLL_INTERVAL_S)
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    state.apply(record)
                    await broadcast({
                        "type": "record",
                        "data": record,
                    })
        except FileNotFoundError:
            continue
        except Exception:
            logger.exception("tailer falhou; reiniciando em 5s")
            await asyncio.sleep(5)


async def broadcast(message):
    dead = []
    for ws in list(state.subscribers):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.subscribers.discard(ws)


@app.on_event("startup")
async def _start_tailer():
    # Replay do histórico existente antes de seguir ao vivo; o offset final
    # é passado ao tailer para não perder linhas escritas nesse intervalo.
    start_offset = 0
    if TELEMETRY_FILE.exists():
        n = 0
        with open(TELEMETRY_FILE) as fh:
            for line in fh:
                try:
                    state.apply(json.loads(line))
                    n += 1
                except json.JSONDecodeError:
                    continue
            start_offset = fh.tell()
        logger.info("histórico carregado: %d registros", n)
    asyncio.create_task(tail_telemetry(start_offset))


@app.get("/api/snapshot")
async def api_snapshot():
    return state.snapshot()


@app.get("/api/history")
async def api_history(minutes: int = 30):
    cutoff = time.time() - minutes * 60
    return [m for m in state.metrics_history if m["recv_at"] >= cutoff]


@app.get("/api/events")
async def api_events(limit: int = 100):
    return list(state.events)[-limit:]


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state.subscribers.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot",
                                       "data": state.snapshot()}))
        while True:
            await ws.receive_text()  # keepalive/ping do cliente
    except WebSocketDisconnect:
        pass
    finally:
        state.subscribers.discard(ws)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
