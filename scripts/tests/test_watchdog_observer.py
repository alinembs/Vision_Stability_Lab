"""Testes de infraestrutura do observador no watchdog (E7).

Cobre o que a integração do `scripts/watchdog.py` acrescenta:

  - `DockerAPI.stop()`/`start()` batem nos endpoints certos (com `t=` no stop);
  - `DockerAPI.container_state()` devolve o `State.ExitCode` (inclusive `2`,
    que é `source_reconnect_exhausted` no pipeline);
  - `data/config/observer_state.json` é gravado de forma atômica (`.tmp` +
    `os.replace`) e tem todos os campos que o dashboard consome;
  - os eventos novos (`source_probe_changed`, `standby_entered`,
    `standby_exited`, `auto_switch_triggered`) saem com `event` e `ts` pelo
    `emit()` existente — nenhum `print()` solto.

Sem docker.sock, sem GStreamer e sem rede: o transporte HTTP é um duplo e a
sonda DESCRIBE é substituída por uma função. Os nomes de evento asseridos são
os do `emit()` interno, **sem** o prefixo `watchdog_` que o log adiciona.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import observer_policy  # noqa: E402
import watchdog as wd  # noqa: E402
from test_watchdog_sources import FakeDocker  # noqa: E402


class FakeClock:
    def __init__(self, start: float = 1_793_400_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DockerClientTest(unittest.TestCase):
    """`DockerAPI`: endpoints de stop/start e o `ExitCode` do `/json`."""

    def _api(self, status: int = 204):
        api = wd.DockerAPI("/tmp/watchdog-test-docker.sock")
        calls: list[tuple[str, str]] = []

        def request(method, path, timeout=20):
            calls.append((method, path))
            return status, b""

        api.request = request
        return api, calls

    def _silent_emit(self):
        return patch.object(wd, "emit", lambda *args, **kwargs: None)

    def test_stop_bate_no_endpoint_com_timeout(self) -> None:
        api, calls = self._api()
        with self._silent_emit():
            ok = api.stop("vision-stability-app", timeout_s=7)
        self.assertTrue(ok)
        self.assertEqual(calls, [("POST", "/containers/vision-stability-app/stop?t=7")])

    def test_stop_default_usa_t_10(self) -> None:
        api, calls = self._api()
        with self._silent_emit():
            api.stop("app")
        self.assertEqual(calls, [("POST", "/containers/app/stop?t=10")])

    def test_start_bate_no_endpoint_de_start(self) -> None:
        api, calls = self._api()
        with self._silent_emit():
            ok = api.start("vision-stability-app")
        self.assertTrue(ok)
        self.assertEqual(calls, [("POST", "/containers/vision-stability-app/start")])

    def test_stop_ja_parado_e_sucesso(self) -> None:
        """304 = container já parado/rodando: idempotente, não é falha."""
        api, _ = self._api(status=304)
        with self._silent_emit():
            self.assertTrue(api.stop("app"))
            self.assertTrue(api.start("app"))

    def test_falha_da_api_devolve_false(self) -> None:
        api, _ = self._api(status=500)
        with self._silent_emit():
            self.assertFalse(api.stop("app"))
            self.assertFalse(api.start("app"))

    def test_stop_e_start_logam_resultado_estruturado(self) -> None:
        api, _ = self._api()
        eventos: list[tuple[str, dict]] = []
        with patch.object(wd, "emit",
                          lambda event, **fields: eventos.append((event, fields))):
            api.stop("app", timeout_s=3)
            api.start("app")
        self.assertEqual([name for name, _ in eventos],
                         ["docker_stop", "docker_start"])
        self.assertEqual(eventos[0][1]["container"], "app")
        self.assertEqual(eventos[0][1]["timeout_s"], 3)
        self.assertTrue(eventos[0][1]["ok"])

    def test_container_state_devolve_exit_code(self) -> None:
        api = wd.DockerAPI("/var/run/docker.sock")
        api.request = lambda *args, **kwargs: (200, json.dumps({"State": {
            "Running": False,
            "StartedAt": "2026-11-06T10:00:00.000000000Z",
            "ExitCode": 2,
        }}).encode())
        running, started, uptime, exit_code = api.container_state("c")
        self.assertFalse(running)
        self.assertEqual(started, "2026-11-06T10:00:00.000000000Z")
        self.assertIsNotNone(uptime)
        self.assertEqual(exit_code, 2)

    def test_container_state_com_exit_code_ausente_devolve_none(self) -> None:
        api = wd.DockerAPI("/var/run/docker.sock")
        api.request = lambda *args, **kwargs: (200, json.dumps({"State": {
            "Running": True,
            "StartedAt": "2026-11-06T10:00:00.000000000Z",
        }}).encode())
        self.assertIsNone(api.container_state("c")[3])

    def test_container_state_com_exit_code_invalido_devolve_none(self) -> None:
        """Nada em `container_state` pode levantar para o loop do watchdog."""
        api = wd.DockerAPI("/var/run/docker.sock")
        api.request = lambda *args, **kwargs: (200, json.dumps({"State": {
            "Running": True, "StartedAt": "nada", "ExitCode": "dois",
        }}).encode())
        self.assertEqual(api.container_state("c"), (True, "nada", None, None))

    def test_api_fora_do_ar_devolve_quatro_nones(self) -> None:
        api = wd.DockerAPI("/var/run/docker.sock")
        api.request = lambda *args, **kwargs: (404, b"")
        self.assertEqual(api.container_state("c"), (None, None, None, None))


class ObserverHarness(unittest.TestCase):
    """Watchdog com relógio, log, estado e sondas controlados pelo teste."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.events: list[tuple[str, dict]] = []
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "observer_state.json"
        self.switch_marker = Path(self._tmp.name) / "switch_in_progress"
        self.switch_log = Path(self._tmp.name) / "switch.jsonl"
        self.camera_uri = "rtsp://admin:segredo@cam.local:554/live"
        self.drone_uri = "rtsp://mediamtx:8554/drone"
        self.phone_uri = "rtsp://mediamtx:8554/phone"
        # URI -> `DescribeResult` da sonda de nível 1 (default: path sem
        # publisher). Nenhum teste fala com socket de verdade.
        self.describe_results: dict[str, object] = {}
        self.describe_calls: list[str] = []
        # `emit` real, guardado **antes** do dublê: o teste do contrato de log
        # precisa escrever no arquivo de verdade, não no coletor de eventos.
        self.real_emit = wd.emit
        self._patchers = [
            patch.object(wd, "time", SimpleNamespace(
                monotonic=self.clock.monotonic, time=self.clock.time)),
            patch.object(wd, "emit", self._record),
            patch.object(wd, "OBSERVER_STATE", str(self.state_path)),
            # Mutex de troca e histórico: em tmp, como o `WatchdogHarness`. Sem
            # isso o `_engage` de um `SWITCH_TO` tentaria escrever em
            # `/app/data/config/switch_in_progress` e cederia o ciclo
            # (`yield_marker`) por um arquivo que não existe no host.
            patch.object(wd, "SWITCH_MARKER", str(self.switch_marker)),
            patch.object(wd, "SWITCH_LOG", str(self.switch_log)),
            patch.object(wd, "ACTIVE_SOURCE_FILE",
                         str(Path(self._tmp.name) / "active_source")),
            patch.object(wd, "ACTIVE_SOURCE_ENV", ""),
            patch.object(wd, "RTSP_URI", self.camera_uri),
            patch.object(wd, "MTX_HOST", "mediamtx"),
            patch.object(wd, "OBSERVER_SOURCE_PRIORITY", "camera,drone,phone"),
            patch.object(wd.rtsp_describe_probe, "describe", self._describe),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)
        self.dog = wd.Watchdog()
        # Cliente Docker com duplo: `_engage` chama `start`/`restart`/`stop` e
        # lê o `StartedAt` do processo novo. Sem ele o `Watchdog()` aberto usa o
        # socket de verdade (ausente no host) e toda transição falharia.
        self.dog.docker = FakeDocker(self.clock)
        self.dog._app_running = True
        self.dog._app_exit_code = 0

    def _describe(self, uri, timeout=None):
        self.describe_calls.append(uri)
        result = self.describe_results.get(uri)
        if result is not None:
            return result
        return wd.rtsp_describe_probe.DescribeResult(
            status=wd.rtsp_describe_probe.STATUS_NOT_FOUND, reason="rtsp_404",
            uri=wd.rtsp_describe_probe.mask_uri(uri), code=404)

    def _alive(self, uri) -> None:
        self.describe_results[uri] = wd.rtsp_describe_probe.DescribeResult(
            status=wd.rtsp_describe_probe.STATUS_OK, reason="ok",
            uri=wd.rtsp_describe_probe.mask_uri(uri), has_video=True, code=200)

    def _stop_patchers(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()

    def _record(self, event: str, **fields) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def first(self, event: str) -> dict:
        for name, fields in self.events:
            if name == event:
                return fields
        self.fail(f"evento {event!r} não foi emitido: {self.names()}")

    def count(self, event: str) -> int:
        return self.names().count(event)


class ObserverStateTest(ObserverHarness):
    """`data/config/observer_state.json`: escrito todo ciclo, sem meio-termo."""

    def test_estado_tem_todos_os_campos_e_sem_tmp(self) -> None:
        self.dog._observer_cycle()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(state),
            {"ts", "mode", "mode_reason", "app_container_running",
             "app_exit_code", "active_source", "last_action", "last_action_ts",
             "sources_probe"})
        self.assertTrue(state["ts"].endswith("+00:00"))
        self.assertEqual(state["mode"], observer_policy.MODE_WATCHING)
        self.assertTrue(state["app_container_running"])
        self.assertEqual(state["app_exit_code"], 0)
        self.assertEqual(state["active_source"], "camera")
        self.assertEqual(state["last_action"], "none")
        self.assertIsNone(state["last_action_ts"])
        self.assertEqual(set(state["sources_probe"]),
                         {"camera", "drone", "phone"})
        for probe in state["sources_probe"].values():
            self.assertEqual(set(probe),
                             {"alive", "level", "reason", "checked_at"})
            self.assertFalse(probe["alive"])
            self.assertEqual(probe["reason"], "rtsp_404")
        # Escrita atômica: o `.tmp` não sobrevive ao `os.replace`.
        self.assertFalse(Path(f"{self.state_path}.tmp").exists())

    def test_estado_e_reescrito_a_cada_ciclo(self) -> None:
        self.dog._observer_cycle()
        primeiro = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.clock.advance(wd.CHECK_INTERVAL_S)
        self.dog._observer_cycle()
        segundo = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(primeiro["ts"], segundo["ts"])
        self.assertGreater(
            wd._timestamp_epoch(segundo["ts"]),
            wd._timestamp_epoch(primeiro["ts"]))

    def test_estado_registra_motivo_da_transicao_de_modo(self) -> None:
        self.dog._set_mode(observer_policy.MODE_STANDBY,
                           reason="standby_no_sources")

        self.assertTrue(self.dog._write_observer_state(self.clock.now))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["mode"], observer_policy.MODE_STANDBY)
        self.assertEqual(state["mode_reason"], "standby_no_sources")

    def test_estado_registra_ultima_acao_decidida(self) -> None:
        self.dog._observer_cycle()
        decision = observer_policy.Decision(
            observer_policy.Action.SWITCH_TO, source_id="drone",
            reason="fonte_ativa_morta")
        self.dog._handle_decision(decision, self.clock.now)

        self.dog._observer_cycle()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_action"], "switch_to")
        self.assertIsNotNone(state["last_action_ts"])

    def test_estado_e_escrito_mesmo_sem_api_do_docker(self) -> None:
        """Sem retrato confiável não há decisão — mas o arquivo é reescrito."""
        self.dog._app_running = None
        self.dog._observer_cycle()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["app_container_running"])
        self.assertEqual(state["sources_probe"], {})

    def test_falha_de_escrita_nao_derruba_o_ciclo(self) -> None:
        self.dog._app_running = True
        with patch.object(wd, "OBSERVER_STATE", "/proc/impossivel/estado.json"):
            self.assertFalse(self.dog._write_observer_state())
        self.assertIn("observer_state_write_failed", self.names())

    def test_escrita_atomica_usa_replace(self) -> None:
        """`os.replace` troca o arquivo inteiro: leitor nunca vê JSON truncado."""
        self.state_path.write_text('{"antigo": true}', encoding="utf-8")
        original = os.replace
        with patch.object(wd.os, "replace", side_effect=original) as replace:
            self.assertTrue(wd.write_json_atomic(
                str(self.state_path), {"novo": True}))
        replace.assert_called_once()
        self.assertEqual(json.loads(self.state_path.read_text()), {"novo": True})


class Level2ProbeTest(ObserverHarness):
    """Nível 2: subprocesso efêmero com o contrato do plano, PASS promove."""

    def _run_with(self, returncode: int, stdout: str):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["timeout"] = kwargs.get("timeout")
            return SimpleNamespace(returncode=returncode, stdout=stdout,
                                   stderr="")

        with patch.object(wd.subprocess, "run", fake_run):
            self.dog._run_level2_probe("camera", self.clock.now)
        return captured

    def test_comando_usa_o_orcamento_de_midia_do_plano(self) -> None:
        self.dog._record_probe("camera", True, reason="ok",
                               now_epoch=self.clock.now)

        captured = self._run_with(0, '{"result": "PASS", "reason": "ok"}')

        command = captured["command"]
        self.assertIn("rtsp_read_probe.py", command[1])
        self.assertIn("--seconds", command)
        self.assertIn("5", command)
        self.assertIn("--min-frames", command)
        self.assertIn("15", command)
        self.assertIn("--min-fps", command)
        self.assertIn("3.0", command)
        self.assertIn("--max-gaps", command)
        self.assertIn("2", command)
        self.assertIn(self.camera_uri, command)
        self.assertEqual(captured["timeout"], wd.LEVEL2_PROBE_TIMEOUT_S)

    def test_pass_promove_o_probe_para_nivel_2(self) -> None:
        self.dog._record_probe("camera", True, reason="ok",
                               now_epoch=self.clock.now)

        self._run_with(0, '{"result": "PASS", "reason": "ok"}')

        probe = self.dog._probes["camera"]
        self.assertTrue(probe.alive)
        self.assertEqual(probe.level, observer_policy.LEVEL_MEDIA)
        self.assertGreaterEqual(probe.ok_streak, wd.observer_policy.MIN_OK_STREAK)

    def test_fail_mantem_a_fonte_viva_no_nivel_1(self) -> None:
        self.dog._record_probe("camera", True, reason="ok",
                               now_epoch=self.clock.now)

        self._run_with(1, '{"result": "FAIL", "reason": "no_frames"}')

        probe = self.dog._probes["camera"]
        self.assertTrue(probe.alive)          # DESCRIBE continua respondendo
        self.assertEqual(probe.level, observer_policy.LEVEL_DESCRIBE)
        self.assertTrue(probe.reason.startswith("nivel2_fail"))

    def test_timeout_do_subprocesso_nao_derruba_o_ciclo(self) -> None:
        import subprocess

        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

        with patch.object(wd.subprocess, "run", fake_run):
            self.dog._run_level2_probe("camera", self.clock.now)

        probe = self.dog._probes["camera"]
        self.assertEqual(probe.level, observer_policy.LEVEL_DESCRIBE)
        self.assertIn("timeout", probe.reason)


class ObserverEventTest(ObserverHarness):
    """Eventos novos do observador: transição, não poll; sempre com ts."""

    def test_standby_entered_e_exited_uma_vez_por_transicao(self) -> None:
        self.dog._set_mode(observer_policy.MODE_STANDBY, reason="sem_fonte_viva")
        self.dog._set_mode(observer_policy.MODE_STANDBY, reason="sem_fonte_viva")
        self.assertEqual(self.count("standby_entered"), 1)
        self.assertEqual(self.first("standby_entered")["reason"], "sem_fonte_viva")

        self.dog._set_mode(observer_policy.MODE_WATCHING, reason="fonte_voltou")
        self.dog._set_mode(observer_policy.MODE_WATCHING, reason="fonte_voltou")
        self.assertEqual(self.count("standby_exited"), 1)
        self.assertEqual(self.first("standby_exited")["mode"],
                         observer_policy.MODE_WATCHING)

    def test_auto_switch_triggered_quando_a_politica_decide_trocar(self) -> None:
        decision = observer_policy.Decision(
            observer_policy.Action.SWITCH_TO, source_id="drone",
            reason="fonte_ativa_morta")
        self.dog._handle_decision(decision, self.clock.now)

        self.assertEqual(self.count("auto_switch_triggered"), 1)
        evento = self.first("auto_switch_triggered")
        self.assertEqual(evento["source"], "drone")
        self.assertEqual(evento["previous_source"], "camera")
        self.assertEqual(self.dog._last_action, "switch_to")

    def test_auto_switch_desligado_nao_emite(self) -> None:
        decision = observer_policy.Decision(
            observer_policy.Action.SWITCH_TO, source_id="drone",
            reason="fonte_ativa_morta")
        with patch.object(wd, "OBSERVER_AUTO_SWITCH", False):
            self.dog._handle_decision(decision, self.clock.now)
        self.assertNotIn("auto_switch_triggered", self.names())

    def test_decisao_vazia_nao_altera_ultima_acao(self) -> None:
        self.dog._handle_decision(
            observer_policy.Decision(observer_policy.Action.NONE,
                                     reason="fonte_ativa_saudavel"),
            self.clock.now)
        self.assertEqual(self.dog._last_action, "none")
        self.assertIsNone(self.dog._last_action_ts)

    def test_eventos_do_log_tem_event_e_ts(self) -> None:
        """O log é o contrato do `emit()`: `event` prefixado e `ts` ISO."""
        log = Path(self._tmp.name) / "watchdog.jsonl"
        with patch.object(wd, "WATCHDOG_LOG", str(log)), \
                patch.object(wd, "emit", self.real_emit), \
                contextlib.redirect_stdout(io.StringIO()):
            wd.emit("source_probe_changed", source="camera", alive=False)
            wd.emit("standby_entered", reason="sem_fonte_viva")
            wd.emit("standby_exited", mode="watching")
            wd.emit("auto_switch_triggered", source="drone")

        registros = [json.loads(line) for line in
                     log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["event"] for r in registros], [
            "watchdog_source_probe_changed", "watchdog_standby_entered",
            "watchdog_standby_exited", "watchdog_auto_switch_triggered"])
        for registro in registros:
            self.assertIsNotNone(wd._timestamp_epoch(registro["ts"]), registro)


if __name__ == "__main__":
    unittest.main()
