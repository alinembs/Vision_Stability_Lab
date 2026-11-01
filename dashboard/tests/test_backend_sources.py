"""Testes do backend multi-fonte do dashboard (E4-T1).

No container (onde `fastapi` existe — é o caminho usado na validação):

    docker exec -w /app/dashboard/backend vision-dashboard \
        python -m unittest discover -s /app/dashboard/tests -v

No host (`pytest dashboard/tests -q`) roda só a parte do catálogo: os testes de
estado e de rota são pulados porque o host não tem `fastapi` instalado.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import sources_catalog  # noqa: E402  (precisa do sys.path acima)


def _load_backend():
    """Carrega `dashboard/backend/app.py` pelo caminho, não por `import app`.

    O `dashboard/tests/test_switch_control.py` coloca `deepstream/` no
    `sys.path` (para achar `sources.py`), e existe um `deepstream/app.py` — um
    `import app` dependeria da ordem de descoberta dos testes.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dashboard_backend_app", BACKEND_DIR / "app.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_backend_app"] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError:  # host sem fastapi
        sys.modules.pop("dashboard_backend_app", None)
        return None
    return module


backend = _load_backend()


YAML_TEXT = """\
version: 1
default_source: camera
sources:
  - id: camera
    name: "câmera fixa"
    uri_env: RTSP_URI
    codec: h265
    enabled: true
  - id: drone
    name: "drone"
    uri: "rtsp://${MTX_HOST:-mediamtx}:8554/drone"
    codec: h264
    enabled: true
"""


def metric(source_id: str | None, epoch: str | None, ts: str,
           fps: float = 10.0, rss: float = 100.0) -> dict:
    """Registro `interval_metrics` no formato que o pipeline publica (E3-T2)."""
    record = {
        "ts": ts,
        "event": "interval_metrics",
        "session_epoch": epoch,
        "fps_processed": fps,
        "rss_mb": rss,
    }
    if source_id is not None:
        record["source_id"] = source_id
        record["source_name"] = f"nome-{source_id}"
    return record


class CatalogTest(unittest.TestCase):
    """`sources.yaml` -> cartões; credencial nunca sai no JSON."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "sources.yaml"
        self.config.write_text(YAML_TEXT, encoding="utf-8")

        self._env = {key: os.environ.get(key)
                     for key in ("SOURCES_CONFIG", "MTX_HOST", "RTSP_URI")}
        self.addCleanup(self._restore_env)
        os.environ["SOURCES_CONFIG"] = str(self.config)

    def _restore_env(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_env_source_never_leaks_credential(self) -> None:
        os.environ["RTSP_URI"] = "rtsp://USUARIO:SENHA@10.0.0.1:554/live"
        os.environ.pop("MTX_HOST", None)

        catalog = sources_catalog.load_catalog()
        camera = next(entry for entry in catalog if entry["id"] == "camera")

        self.assertIsNone(camera["path"])
        self.assertEqual(camera["path_origin"], "env")
        self.assertEqual(camera["path_env"], "RTSP_URI")
        self.assertNotIn("senha", str(catalog))

    def test_literal_path_is_expanded_and_masked(self) -> None:
        os.environ["MTX_HOST"] = "192.168.59.102"

        catalog = sources_catalog.load_catalog()
        drone = next(entry for entry in catalog if entry["id"] == "drone")

        self.assertEqual(drone["path"], "rtsp://192.168.59.102:8554/drone")
        self.assertEqual(drone["path_origin"], "config")
        self.assertIsNone(drone["path_env"])

    def test_missing_explicit_path_returns_empty(self) -> None:
        os.environ["SOURCES_CONFIG"] = str(Path(self.tmp.name) / "nao-existe.yaml")
        self.assertEqual(sources_catalog.load_catalog(), [])

    def test_mask_uri_hides_userinfo(self) -> None:
        masked = sources_catalog.mask_uri(
            "rtsp://USUARIO:SENHA@192.168.59.108:5543/live/channel0")
        self.assertEqual(masked, "rtsp://***@192.168.59.108:5543/live/channel0")


@unittest.skipIf(backend is None,
                 "fastapi não instalado no host; rode no container vision-dashboard")
class StateTest(unittest.TestCase):
    """Séries, sessão ativa e saúde derivada por fonte."""

    def setUp(self) -> None:
        self.state = backend.DashboardState()

    def _apply(self, records: list[dict]) -> None:
        for record in records:
            self.state.apply(dict(record))

    @contextlib.contextmanager
    def _env(self, **values: str | None):
        """Troca variáveis de ambiente e restaura no fim (catálogo/MTX_HOST)."""
        previous = {key: os.environ.get(key) for key in values}
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @contextlib.contextmanager
    def _routes(self):
        """Aponta as rotas para este estado (o módulo usa `state` global)."""
        previous = backend.state
        backend.state = self.state
        try:
            yield
        finally:
            backend.state = previous

    def test_history_is_separated_by_source(self) -> None:
        self._apply([
            metric("camera", "E1", "2026-11-01T10:00:00+00:00"),
            metric("camera", "E1", "2026-11-01T10:00:30+00:00"),
            metric("drone", "E2", "2026-11-01T10:01:00+00:00"),
        ])

        camera = self.state.history(source_id="camera")
        drone = self.state.history(source_id="drone")

        self.assertEqual([m["source_id"] for m in camera], ["camera", "camera"])
        self.assertEqual([m["source_id"] for m in drone], ["drone"])
        # sem `source`, o histórico é o da fonte ativa (drone, a última vista)
        self.assertEqual([m["source_id"] for m in self.state.history()], ["drone"])

    def test_new_epoch_resets_series(self) -> None:
        self._apply([
            metric("camera", "E1", "2026-11-01T10:00:00+00:00"),
            metric("camera", "E1", "2026-11-01T10:00:30+00:00"),
            metric("camera", "E2", "2026-11-01T11:00:00+00:00"),
        ])

        history = self.state.history(source_id="camera")
        self.assertEqual(len(history), 1)
        self.assertEqual(self.state.sessions["camera"].epoch, "E2")

    def test_health_uses_active_session_only(self) -> None:
        self._apply([
            metric("camera", "E1", "2026-11-01T10:00:00+00:00"),
            metric("drone", "E2", "2026-11-01T10:01:00+00:00"),
        ])
        # a câmera continua "fresca" (recv_at do replay), o drone não:
        # saúde tem de olhar só a sessão ativa (drone)
        self.state.sessions["drone"].metrics[-1]["recv_at"] = time.time() - 1000

        self.assertEqual(self.state.health()["status"], "down")

    def test_snapshot_exposes_active_source(self) -> None:
        self._apply([metric("drone", "E2", "2026-11-01T10:01:00+00:00")])

        snapshot = self.state.snapshot()

        self.assertEqual(snapshot["source"], {
            "id": "drone",
            "name": "nome-drone",
            "session_epoch": "E2",
            "since": "2026-11-01T10:01:00+00:00",
        })
        self.assertEqual(snapshot["latest_metrics"]["source_id"], "drone")

    def test_legacy_telemetry_uses_legacy_key(self) -> None:
        self._apply([metric(None, None, "2026-11-01T10:00:00+00:00")])

        self.assertEqual(self.state.snapshot()["source"]["id"], "legacy")
        self.assertEqual(len(self.state.history(source_id="legacy")), 1)

    def test_routes_filter_by_source(self) -> None:
        self._apply([
            metric("camera", "E1", "2026-11-01T10:00:00+00:00"),
            {"ts": "2026-11-01T10:00:01+00:00", "event": "pipeline_playing",
             "source_id": "camera", "session_epoch": "E1"},
            metric("drone", "E2", "2026-11-01T10:01:00+00:00"),
            {"ts": "2026-11-01T10:01:01+00:00", "event": "pipeline_playing",
             "source_id": "drone", "session_epoch": "E2"},
        ])

        with self._routes():
            camera = asyncio.run(backend.api_history(minutes=30, source="camera"))
            self.assertEqual([m["source_id"] for m in camera], ["camera"])
            self.assertEqual(
                asyncio.run(backend.api_history(minutes=30, source="nao-existe")), [])

            events = asyncio.run(backend.api_events(limit=50, source="drone"))
            self.assertTrue(events)
            self.assertEqual({r["source_id"] for r in events}, {"drone"})

    def test_api_sources_marks_active_and_exposes_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.yaml"
            config.write_text(YAML_TEXT, encoding="utf-8")
            with self._env(SOURCES_CONFIG=str(config),
                           MTX_HOST="192.168.59.102"), self._routes():
                self._apply([metric("drone", "E2", "2026-11-01T10:01:00+00:00")])
                payload = asyncio.run(backend.api_sources())

        by_id = {entry["id"]: entry for entry in payload}
        self.assertEqual(set(by_id), {"camera", "drone"})
        self.assertTrue(by_id["drone"]["active"])
        self.assertFalse(by_id["camera"]["active"])
        self.assertEqual(by_id["drone"]["path"],
                         "rtsp://192.168.59.102:8554/drone")
        self.assertIsNone(by_id["camera"]["path"])
        self.assertEqual(by_id["camera"]["path_env"], "RTSP_URI")
        self.assertIsNone(by_id["camera"]["session"])
        self.assertEqual(by_id["drone"]["session"]["points"], 1)

    def test_runtime_only_source_is_hidden_unless_active_or_no_catalog(self) -> None:
        self._apply([
            metric(None, None, "2026-11-01T10:00:00+00:00"),   # JSONL legado
            metric("camera", "E1", "2026-11-01T10:01:00+00:00"),
        ])

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.yaml"
            config.write_text(YAML_TEXT, encoding="utf-8")
            with self._env(SOURCES_CONFIG=str(config)), self._routes():
                ids = {entry["id"] for entry in asyncio.run(backend.api_sources())}
                self.assertEqual(ids, {"camera", "drone"})

            # catálogo indisponível: a telemetria é a única lista possível
            missing = str(Path(tmp) / "nao-existe.yaml")
            with self._env(SOURCES_CONFIG=missing), self._routes():
                ids = {entry["id"] for entry in asyncio.run(backend.api_sources())}
                self.assertEqual(ids, {"camera", "legacy"})


if __name__ == "__main__":
    unittest.main()
