"""Testes da troca de fonte pedida pela tela (E4-T3).

Duas camadas:

- **`Switcher`** (orquestração) — roda no **host** também: nada aqui toca
  Docker. O `SwitchRuntime` é um dublê que registra chamadas; o
  `wait_for_source` é injetado, então não há espera de verdade. As decisões
  vêm do `scripts/switch_source_lib.py`, o mesmo módulo do
  `switch_source.sh` — por isso um PASS/rollback aqui prova o mesmo contrato
  do script.
- **rotas** (`/api/source/switch`) — só no container, onde `fastapi` existe:
  token ausente/errado, feature desligada, conflito de troca em andamento.

    # host (só a camada do Switcher)
    python3 -m pytest dashboard/tests -q
    # container (as duas camadas)
    docker exec -w /app/dashboard/backend vision-dashboard \
        python -m unittest discover -s /app/dashboard/tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import switch_control  # noqa: E402  (precisa do sys.path acima)

switch_control_lib = sys.modules.get("switch_source_lib")
if switch_control_lib is None:
    # O dashboard só tem `scripts/` montado no overlay de troca
    # (`docker-compose.switch.yml`); sem ele as decisões da troca não existem
    # aqui e os testes de `Switcher` são pulados.
    try:
        import switch_source_lib as switch_control_lib  # type: ignore
    except ModuleNotFoundError:
        switch_control_lib = None


def _load_backend():
    """Carrega `dashboard/backend/app.py` **pelo caminho**.

    `import app` não serve: com `deepstream/` no `sys.path` (o
    `switch_control` insere para achar `sources.py`), `deepstream/app.py`
    vem primeiro e o teste exercitaria o módulo errado.
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
  - id: phone
    name: "celular"
    uri: "rtsp://${MTX_HOST:-mediamtx}:8554/phone"
    codec: h264
    enabled: false
"""


class FakeRuntime:
    """`SwitchRuntime` de teste: registra chamadas, nunca fala com o Docker."""

    def __init__(self, env: dict | None = None,
                 probe: "switch_control.ProbeResult | None" = None) -> None:
        self.env = dict(env or {})
        self.probe_result = probe or switch_control.ProbeResult(True, "ok")
        self.restarts: list[str] = []
        self.probes: list[tuple[str, str]] = []
        self.started = "2026-11-01T14:00:00.000000000Z"

    def container_env(self, name: str) -> dict:
        return dict(self.env)

    def started_at(self, name: str):
        return self.started

    def restart(self, name: str) -> None:
        self.restarts.append(name)

    def probe(self, name: str, source_id: str):
        self.probes.append((name, source_id))
        return self.probe_result


def outcome(ok: bool, source_id: str = "drone", reason: str = "ok",
            fps_in: float | None = 25.0) -> "switch_control_lib.WaitOutcome":
    """`WaitOutcome` no formato que o `wait_for_source` real devolve."""
    return switch_control_lib.WaitOutcome(
        ok=ok, elapsed_s=12.0, first_frame_in=3.0 if ok else None,
        source_id=source_id if ok else None, fps_in=fps_in,
        last_error=None, reason=reason, session_epoch="2026-11-01T14:00:02Z")


class SwitcherTestBase(unittest.TestCase):
    def setUp(self) -> None:
        if switch_control_lib is None:
            self.skipTest("scripts/ não montado no dashboard (overlay de troca)")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.config_file = self.root / "sources.yaml"
        self.config_file.write_text(YAML_TEXT, encoding="utf-8")
        self.telemetry = self.root / "live.jsonl"
        self.telemetry.write_text("", encoding="utf-8")
        self.switch_log = self.root / "switch.jsonl"

    def make_env(self, **extra: str) -> dict:
        env = {
            "SOURCES_CONFIG": str(self.config_file),
            "RTSP_URI": "rtsp://USUARIO:SENHA@10.0.0.1:554/live",
            "MTX_HOST": "mediamtx",
        }
        env.update(extra)
        return env

    def make_switcher(self, runtime: FakeRuntime, waiter=None,
                      **kwargs) -> "switch_control.Switcher":
        if waiter is None:
            def waiter(path, source_id, timeout_s, **wait_kwargs):
                return outcome(True, source_id=source_id)
        return switch_control.Switcher(
            runtime,
            config_dir=self.config_dir,
            telemetry_file=self.telemetry,
            switch_log=self.switch_log,
            timeout_s=1.0,
            wait_for_source=waiter,
            **kwargs,
        )

    def control_value(self) -> str | None:
        path = self.config_dir / "active_source"
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def audit(self) -> list[dict]:
        if not self.switch_log.exists():
            return []
        return [json.loads(line) for line in
                self.switch_log.read_text(encoding="utf-8").splitlines() if line]

    def marker_exists(self) -> bool:
        return (self.config_dir / "switch_in_progress").exists()


class SwitcherValidationTest(SwitcherTestBase):
    def test_invalid_id_is_rejected_before_any_thread(self) -> None:
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        for bad in ("", "  ", "Drone", "../etc", "a" * 40, "drone;rm -rf /"):
            with self.subTest(bad=bad):
                with self.assertRaises(switch_control.SwitchRejected):
                    switcher.start(bad)

        self.assertEqual(switcher.status.state, "idle")
        self.assertEqual(self.audit(), [])

    def test_id_outside_yaml_fails_without_touching_control_file(self) -> None:
        runtime = FakeRuntime(self.make_env())
        switcher = self.make_switcher(runtime)

        switcher.start("nao-existe")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "id_invalido")
        self.assertEqual(runtime.restarts, [])
        self.assertEqual(runtime.probes, [])
        self.assertIsNone(self.control_value())
        self.assertFalse(self.marker_exists())
        self.assertEqual(self.audit()[0]["result"], "FAIL")
        self.assertEqual(self.audit()[0]["probe"], "blocked")

    def test_disabled_source_in_yaml_is_rejected(self) -> None:
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        switcher.start("phone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.reason, "id_invalido")
        self.assertIsNone(self.control_value())

    def test_second_request_while_running_is_busy(self) -> None:
        started = []

        def blocking_waiter(path, source_id, timeout_s, **kwargs):
            started.append(source_id)
            return outcome(True, source_id=source_id)

        switcher = self.make_switcher(FakeRuntime(self.make_env()),
                                      waiter=blocking_waiter)
        # segura o lock como se a thread estivesse no meio da troca
        self.assertTrue(switcher._lock.acquire(blocking=False))
        try:
            with self.assertRaises(switch_control.SwitchBusy):
                switcher.start("drone")
        finally:
            switcher._lock.release()

        self.assertEqual(started, [])

    def test_active_source_in_env_blocks_before_probe(self) -> None:
        runtime = FakeRuntime(self.make_env(ACTIVE_SOURCE="camera"))
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "active_source_no_ambiente")
        # guard de precedência: nenhuma sonda, nenhum restart, nada escrito
        self.assertEqual(runtime.probes, [])
        self.assertEqual(runtime.restarts, [])
        self.assertIsNone(self.control_value())
        self.assertEqual(self.audit()[0]["probe"], "blocked")


class SwitcherPrecheckTest(SwitcherTestBase):
    def test_failed_precheck_writes_nothing_and_does_not_restart(self) -> None:
        runtime = FakeRuntime(
            self.make_env(),
            probe=switch_control.ProbeResult(False, "no_frames_on_path",
                                             "rtsp://***@10.0.0.1:554/live"))
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "precheck:no_frames_on_path")
        self.assertEqual(runtime.probes, [("vision-stability-app", "drone")])
        self.assertEqual(runtime.restarts, [])
        self.assertIsNone(self.control_value())
        self.assertFalse(self.marker_exists())
        record = self.audit()[0]
        self.assertEqual((record["result"], record["probe"]), ("FAIL", "failed"))

    def test_motivo_cru_da_sonda_e_normalizado(self) -> None:
        # H2/M2: o motivo cru (`pipeline_error: ...`) vira o vocabulário do
        # switch, igual ao script — senão consulta/alertar por
        # `precheck:no_frames_on_path` não vê trocas vindas da tela.
        runtime = FakeRuntime(
            self.make_env(),
            probe=switch_control.ProbeResult(
                False, "pipeline_error: Could not open resource (7)"))
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.reason, "precheck:no_frames_on_path")
        self.assertEqual(self.audit()[0]["reason"], "precheck:no_frames_on_path")
        self.assertIn("Could not open resource",
                      switcher.status.message)

    def test_sonda_que_estoura_o_teto_nao_pende_a_troca(self) -> None:
        # H4: sonda pendurada não pode deixar o `Switcher` travado (409 eterno).
        runtime = FakeRuntime(
            self.make_env(), probe=switch_control.ProbeResult(False, "timeout"))
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "precheck:timeout")
        self.assertEqual(runtime.restarts, [])
        self.assertFalse(switcher._lock.locked(), "lock preso após timeout")
        # e a próxima troca é aceita
        switcher.start("drone")
        switcher.wait(timeout=10)

    def test_probe_uses_container_exec_not_host(self) -> None:
        runtime = FakeRuntime(self.make_env())
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(runtime.probes, [("vision-stability-app", "drone")])


class ProbeExecTest(unittest.TestCase):
    """`DockerRuntime.probe`: teto de tempo via `timeout` dentro do container."""

    class _Result:
        def __init__(self, exit_code: int, output: bytes = b"") -> None:
            self.exit_code = exit_code
            self.output = output

    class _Container:
        def __init__(self, results) -> None:
            self.results = list(results)
            self.calls: list[list[str]] = []

        def exec_run(self, argv, demux=False):
            self.calls.append(list(argv))
            return self.results.pop(0)

    class _Client:
        def __init__(self, container) -> None:
            self._container = container
            self.containers = self
            self.attrs_env = {}

        def get(self, name):
            return self._container

    def _runtime(self, results, timeout_s: float = 20.0):
        container = self._Container(results)
        runtime = switch_control.DockerRuntime(
            client=self._Client(container), probe_timeout_s=timeout_s)
        return runtime, container

    def test_sonda_roda_sob_timeout_com_sigkill_de_reserva(self) -> None:
        runtime, container = self._runtime(
            [self._Result(0, b"ok=1\nreason=ok\n")])
        result = runtime.probe("vision-stability-app", "drone")

        self.assertTrue(result.ok)
        self.assertEqual(container.calls[0][:4], ["timeout", "-k", "5", "20"])

    def test_exit_124_e_137_sao_reportados_como_timeout(self) -> None:
        for code in (124, 137):
            with self.subTest(code=code):
                runtime, _ = self._runtime([self._Result(code)])
                result = runtime.probe("vision-stability-app", "drone")

                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "timeout")

    def test_sem_timeout_na_imagem_repete_sem_o_teto(self) -> None:
        runtime, container = self._runtime(
            [self._Result(127), self._Result(0, b"reason=ok\n")])
        result = runtime.probe("vision-stability-app", "drone")

        self.assertTrue(result.ok)
        self.assertEqual(len(container.calls), 2)
        self.assertNotIn("timeout", container.calls[1])


class MarcadorEntreProcessosTest(SwitcherTestBase):
    """H2: `switch_in_progress` é mutex entre a tela e o `switch_source.sh`."""

    def _marcador_externo(self, expires_at: str, owner: str = "script:1:1") -> None:
        (self.config_dir / "switch_in_progress").write_text(json.dumps({
            "to": "drone", "expires_at": expires_at,
            "owner": owner, "origin": "script"}) + "\n", encoding="utf-8")

    def test_troca_do_script_em_curso_bloqueia_a_tela(self) -> None:
        self._marcador_externo("2099-01-01T00:00:00Z")
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        with self.assertRaises(switch_control.SwitchBusy) as ctx:
            switcher.start("drone")
        self.assertIn("script", str(ctx.exception))
        # nada rodou: sem restart, sem escrita
        self.assertEqual(switcher.status.state, "idle")

    def test_marcador_expirado_nao_bloqueia(self) -> None:
        self._marcador_externo("2020-01-01T00:00:00Z")
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "done")
        self.assertEqual(self.control_value(), "drone")
        self.assertFalse(self.marker_exists())

    def test_marcador_expirado_e_descartado_na_reivindicacao(self) -> None:
        self._marcador_externo("2020-01-01T00:00:00Z")
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        switcher._claim_marker("drone")

        data = json.loads((self.config_dir / "switch_in_progress")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["owner"][:9], "dashboard")
        # o arquivo descartado não fica para trás
        self.assertEqual(
            list(self.config_dir.glob("switch_in_progress.stale.*")), [])

    def test_marcador_sem_validade_expira_por_mtime(self) -> None:
        marker = self.config_dir / "switch_in_progress"
        marker.write_text('{"to": "drone"}\n', encoding="utf-8")
        os.utime(marker, (0, 0))          # mtime muito antigo
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "done")

    def test_marcador_ilegivel_recente_ainda_bloqueia(self) -> None:
        (self.config_dir / "switch_in_progress").write_text(
            "lixo", encoding="utf-8")
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        with self.assertRaises(switch_control.SwitchBusy):
            switcher.start("drone")

    def test_reivindicacao_atomica_recusa_marcador_de_terceiro(self) -> None:
        switcher = self.make_switcher(FakeRuntime(self.make_env()))
        self._marcador_externo("2099-01-01T00:00:00Z")
        # simula a corrida: o marcador aparece entre o `start` e a escrita
        with self.assertRaises(switch_control.SwitchBusy):
            switcher._claim_marker("drone")

    def test_saida_nao_apaga_marcador_de_outro_dono(self) -> None:
        switcher = self.make_switcher(FakeRuntime(self.make_env()))

        def waiter(path, source_id, timeout_s, **kwargs):
            # outro processo assume o marcador no meio da espera
            self._marcador_externo("2099-01-01T00:00:00Z", owner="script:9:9")
            return outcome(True, source_id=source_id)

        switcher = self.make_switcher(FakeRuntime(self.make_env()), waiter=waiter)
        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertTrue(self.marker_exists(),
                        "o marcador de outro dono foi apagado")

    def test_corrida_na_reivindicacao_vira_troca_em_andamento(self) -> None:
        # Outro processo reivindica **durante** o pré-check: a checagem do
        # `start` já passou, e quem perde a corrida na escrita precisa reportar
        # `troca_em_andamento` (bloqueio), não `erro_interno`.
        escrever = lambda: self._marcador_externo("2099-01-01T00:00:00Z")

        class RuntimeQueCriaMarcador(FakeRuntime):
            def probe(self, name: str, source_id: str):
                escrever()
                return super().probe(name, source_id)

        switcher = self.make_switcher(RuntimeQueCriaMarcador(self.make_env()))
        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "troca_em_andamento")
        self.assertIsNone(self.control_value(), "escreveu com outra troca em curso")
        self.assertEqual(self.audit()[0]["reason"], "troca_em_andamento")
        self.assertEqual(self.audit()[0]["probe"], "blocked")

    def test_get_informa_troca_de_terceiro(self) -> None:
        switcher = self.make_switcher(FakeRuntime(self.make_env()))
        self._marcador_externo("2099-01-01T00:00:00Z")

        foreign = switcher.foreign_marker()
        self.assertIsNotNone(foreign)
        self.assertEqual(foreign["origin"], "script")
        self.assertEqual(foreign["to"], "drone")


class RestartFalhouTest(SwitcherTestBase):
    """H3: restart quebrado depois da escrita não deixa a fonte trocada."""

    class RuntimeQueFalhaNoRestart(FakeRuntime):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.tentativas = 0

        def restart(self, name: str) -> None:
            self.tentativas += 1
            if self.tentativas == 1:
                raise RuntimeError("daemon do Docker caiu")
            super().restart(name)

    def test_falha_restaura_a_fonte_anterior_e_audita(self) -> None:
        (self.config_dir / "active_source").write_text("camera\n",
                                                       encoding="utf-8")
        runtime = self.RuntimeQueFalhaNoRestart(self.make_env())
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "restart_falhou")
        self.assertEqual(self.control_value(), "camera",
                         "active_source ficou apontando para a fonte nova")
        self.assertEqual(runtime.tentativas, 2, "faltou o restart de compensação")
        record = self.audit()[0]
        self.assertEqual((record["result"], record["reason"]),
                         ("FAIL", "restart_falhou"))
        self.assertFalse(self.marker_exists())

    def test_restart_de_compensacao_tambem_falhando_ainda_audita(self) -> None:
        (self.config_dir / "active_source").write_text("camera\n",
                                                       encoding="utf-8")

        class RuntimeSempreQuebrado(FakeRuntime):
            def restart(self, name: str) -> None:
                raise RuntimeError("sem daemon")

        runtime = RuntimeSempreQuebrado(self.make_env())
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.reason, "restart_falhou")
        self.assertEqual(self.control_value(), "camera")
        self.assertEqual(self.audit()[0]["reason"], "restart_falhou")
        self.assertFalse(self.marker_exists())


class SwitcherHappyPathTest(SwitcherTestBase):
    def test_pass_writes_control_restarts_and_audits(self) -> None:
        runtime = FakeRuntime(self.make_env())
        seen = {}

        def waiter(path, source_id, timeout_s, **kwargs):
            seen.update(kwargs)
            seen["path"] = str(path)
            seen["source_id"] = source_id
            seen["timeout_s"] = timeout_s
            return outcome(True, source_id=source_id, fps_in=24.5)

        (self.config_dir / "active_source").write_text("camera\n",
                                                       encoding="utf-8")
        switcher = self.make_switcher(runtime, waiter=waiter)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "done")
        self.assertEqual(switcher.status.previous, "camera")
        self.assertEqual(switcher.status.reason, None)
        self.assertIn("drone", switcher.status.message)
        self.assertEqual(self.control_value(), "drone")
        self.assertEqual(runtime.restarts, ["vision-stability-app"])
        self.assertFalse(self.marker_exists())
        # o pós-check recebeu o boundary do processo novo e o offset do JSONL
        self.assertEqual(seen["started_at"], runtime.started)
        self.assertIsNotNone(seen["recreated_at"])
        self.assertEqual(seen["offset"], 0)
        record = self.audit()[0]
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(record["to"], "drone")
        self.assertEqual(record["from"], "camera")
        self.assertEqual(record["origin"], "dashboard")

    def test_marker_is_written_before_restart(self) -> None:
        """O watchdog usa o marcador para não escalar restart durante a troca."""
        runtime = FakeRuntime(self.make_env())
        markers: list[bool] = []
        original_restart = runtime.restart

        def restart(name: str) -> None:
            markers.append(self.marker_exists())
            original_restart(name)

        runtime.restart = restart  # type: ignore[assignment]
        switcher = self.make_switcher(runtime)

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(markers, [True])

    def test_offset_is_jsonl_size_before_restart(self) -> None:
        self.telemetry.write_text('{"event": "startup"}\n', encoding="utf-8")
        runtime = FakeRuntime(self.make_env())
        seen = {}

        def waiter(path, source_id, timeout_s, **kwargs):
            seen.update(kwargs)
            return outcome(True, source_id=source_id)

        switcher = self.make_switcher(runtime, waiter=waiter)
        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(seen["offset"],
                         len('{"event": "startup"}\n'))


class SwitcherRollbackTest(SwitcherTestBase):
    def test_failed_postcheck_restores_previous_source(self) -> None:
        runtime = FakeRuntime(self.make_env())
        (self.config_dir / "active_source").write_text("camera\n",
                                                       encoding="utf-8")
        switcher = self.make_switcher(
            runtime,
            waiter=lambda *a, **k: outcome(False, reason="timeout_sem_frames"))

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "timeout_sem_frames")
        self.assertEqual(self.control_value(), "camera")
        # um restart para trocar, outro para voltar
        self.assertEqual(runtime.restarts,
                         ["vision-stability-app", "vision-stability-app"])
        self.assertFalse(self.marker_exists())
        record = self.audit()[0]
        self.assertEqual((record["result"], record["to"]), ("FAIL", "drone"))
        self.assertEqual(record["from"], "camera")

    def test_rollback_without_previous_removes_control_file(self) -> None:
        runtime = FakeRuntime(self.make_env())
        switcher = self.make_switcher(
            runtime, waiter=lambda *a, **k: outcome(False, reason="fps_zero"))

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertIsNone(self.control_value())
        self.assertEqual(len(runtime.restarts), 2)

    def test_wrong_source_in_postcheck_is_not_a_pass(self) -> None:
        """`source_id` diferente do pedido não confirma a troca."""
        runtime = FakeRuntime(self.make_env())
        switcher = self.make_switcher(
            runtime,
            waiter=lambda *a, **k: outcome(True, source_id="camera"))

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertIsNone(self.control_value())

    def test_rollback_restart_failure_still_reports_the_previous_source(self) -> None:
        """Se o restart do rollback falhar, o operador precisa saber que só vale no próximo."""
        runtime = FakeRuntime(self.make_env())
        (self.config_dir / "active_source").write_text("camera\n",
                                                       encoding="utf-8")
        calls = {"n": 0}
        original_restart = runtime.restart

        def restart(name: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("socket ocupado")
            original_restart(name)

        runtime.restart = restart  # type: ignore[assignment]
        switcher = self.make_switcher(
            runtime, waiter=lambda *a, **k: outcome(False, reason="fps_zero"))

        switcher.start("drone")
        switcher.wait(timeout=10)

        self.assertEqual(switcher.status.state, "failed")
        self.assertEqual(switcher.status.reason, "fps_zero")
        self.assertEqual(self.control_value(), "camera")
        self.assertIn("restart falhou", switcher.status.message)
        self.assertEqual(self.audit()[0]["result"], "FAIL")


@unittest.skipIf(backend is None,
                 "fastapi não instalado no host; rode no container vision-dashboard")
class SwitchRouteTest(unittest.TestCase):
    """Token, feature desligada e conflito — o HTTP do `POST`."""

    def setUp(self) -> None:
        self._saved = {
            "token": backend.SWITCH_TOKEN,
            "enabled": backend.SWITCH_ENABLED,
            "switcher": backend.switch_state.get("switcher"),
            "lib": switch_control.lib_available,
        }
        self.addCleanup(self._restore)
        switch_control.lib_available = lambda: True  # type: ignore[assignment]

    def _restore(self) -> None:
        backend.SWITCH_TOKEN = self._saved["token"]
        backend.SWITCH_ENABLED = self._saved["enabled"]
        switch_control.lib_available = self._saved["lib"]  # type: ignore[assignment]
        backend.switch_state["switcher"] = self._saved["switcher"]

    def _use(self, token: str, switcher=None) -> None:
        backend.SWITCH_TOKEN = token
        backend.switch_state["switcher"] = switcher

    def test_empty_token_disables_the_feature(self) -> None:
        self._use("")
        self.assertEqual(backend.switch_feature_reason(), "switch_token_ausente")
        with self.assertRaises(backend.HTTPException) as ctx:
            import asyncio
            asyncio.run(backend.api_switch_source({"source_id": "drone"},
                                                 x_switch_token="x"))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_switch_enabled_off_disables_even_with_token(self) -> None:
        self._use("segredo")
        backend.SWITCH_ENABLED = False
        self.assertEqual(backend.switch_feature_reason(), "switch_enabled_off")

        import asyncio
        with self.assertRaises(backend.HTTPException) as ctx:
            asyncio.run(backend.api_switch_source({"source_id": "drone"},
                                                 x_switch_token="segredo"))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_repo_mount_disables_the_feature(self) -> None:
        self._use("segredo")
        switch_control.lib_available = lambda: False  # type: ignore[assignment]
        self.assertEqual(backend.switch_feature_reason(),
                         "switch_sem_repo_montado")

    def test_wrong_or_missing_token_is_401(self) -> None:
        import asyncio
        self._use("segredo")
        for header in (None, "", "errado", "segredo "[:-1] + "x"):
            with self.subTest(header=header):
                with self.assertRaises(backend.HTTPException) as ctx:
                    asyncio.run(backend.api_switch_source(
                        {"source_id": "drone"}, x_switch_token=header))
                self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_source_id_is_400(self) -> None:
        import asyncio
        self._use("segredo")
        with self.assertRaises(backend.HTTPException) as ctx:
            asyncio.run(backend.api_switch_source({}, x_switch_token="segredo"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail["error"], "source_id_ausente")

    def test_accepted_returns_202_payload_and_status_is_public(self) -> None:
        import asyncio
        self._use("segredo", switcher=_FakeSwitcher())

        payload = asyncio.run(backend.api_switch_source(
            {"source_id": "drone"}, x_switch_token="segredo"))

        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"]["source_id"], "drone")
        status = asyncio.run(backend.api_switch_status())
        self.assertTrue(status["enabled"])
        self.assertEqual(status["status"]["state"], "running")

    def test_busy_switch_is_409(self) -> None:
        import asyncio
        self._use("segredo", switcher=_FakeSwitcher(
            error=switch_control.SwitchBusy("já existe uma troca")))
        with self.assertRaises(backend.HTTPException) as ctx:
            asyncio.run(backend.api_switch_source({"source_id": "drone"},
                                                 x_switch_token="segredo"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_invalid_id_is_400(self) -> None:
        import asyncio
        self._use("segredo", switcher=_FakeSwitcher(
            error=switch_control.SwitchRejected("id inválido")))
        with self.assertRaises(backend.HTTPException) as ctx:
            asyncio.run(backend.api_switch_source({"source_id": "Drone"},
                                                 x_switch_token="segredo"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_token_never_appears_in_status(self) -> None:
        import asyncio
        self._use("segredo-de-teste", switcher=_FakeSwitcher())
        payload = asyncio.run(backend.api_switch_status())
        self.assertNotIn("segredo-de-teste", json.dumps(payload))


class _FakeSwitcher:
    """`Switcher` mínimo para exercitar as rotas sem thread nem Docker."""

    def __init__(self, error: Exception | None = None,
                 foreign: dict | None = None) -> None:
        self.error = error
        self.foreign = foreign
        self.status = switch_control.SwitchStatus(
            state="running", step="precheck", source_id="drone")

    def start(self, source_id: str) -> "switch_control.SwitchStatus":
        if self.error is not None:
            raise self.error
        self.status.source_id = source_id
        return self.status

    def foreign_marker(self):
        return self.foreign


@unittest.skipIf(backend is None,
                 "fastapi ausente no host (rotas só no container)")
class SwitchStatusRouteTest(unittest.TestCase):
    """`GET /api/source/switch`: informa troca em curso de outro processo."""

    def setUp(self) -> None:
        self._saved = {
            "token": backend.SWITCH_TOKEN,
            "switcher": backend.switch_state.get("switcher"),
        }
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        backend.SWITCH_TOKEN = self._saved["token"]
        backend.switch_state["switcher"] = self._saved["switcher"]

    def test_get_mostra_troca_do_script(self) -> None:
        import asyncio

        backend.SWITCH_TOKEN = "segredo"
        backend.switch_state["switcher"] = _FakeSwitcher(foreign={
            "origin": "script", "to": "phone",
            "expires_at": "2099-01-01T00:00:00Z"})
        payload = asyncio.run(backend.api_switch_status())

        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["in_progress_by"],
                         {"origin": "script", "to": "phone",
                          "expires_at": "2099-01-01T00:00:00Z"})

    def test_get_sem_troca_em_curso(self) -> None:
        import asyncio

        backend.SWITCH_TOKEN = "segredo"
        backend.switch_state["switcher"] = _FakeSwitcher()
        payload = asyncio.run(backend.api_switch_status())

        self.assertIsNone(payload["in_progress_by"])


if __name__ == "__main__":
    unittest.main()
