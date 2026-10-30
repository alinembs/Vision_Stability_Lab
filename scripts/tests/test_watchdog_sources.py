"""Testes das fontes de "tem frame?" do watchdog (`scripts/watchdog.py`).

Cobre o falso positivo do E-B (`docs/baselines/2026-10-30-e1t2-baseline.md`):
`frames_total_in` é cumulativo **por processo** do pipeline, e o watchdog
guardava um high-water eterno. Depois de um restart o contador novo (~2.900)
nunca alcançava o recorde do processo anterior (1.375.018); a ~20 fps isso
levaria ~19 h, e nesse intervalo o watchdog emitia `no_frames_detected` a cada
30 s e `docker restart` a cada ~600 s indefinidamente.

Não há GStreamer nem docker aqui: o `DockerAPI` é um duplo, o relógio é
controlado pelo teste (sem `sleep`) e `emit` é capturado em memória. Os nomes
de evento asseridos são os do `emit()` interno, **sem** o prefixo
`watchdog_` que o log estruturado adiciona.

Nota: `scripts/tests` não tem `__init__.py` de propósito (ver
`test_probe_logic.py`); o import abaixo usa o caminho do diretório.
"""

import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watchdog as wd  # noqa: E402

# Intervalo em que o pipeline publica `interval_metrics` (METRICS_INTERVAL_S
# do pipeline_real.py). Fixo de propósito: é o contrato que o watchdog assume.
METRICS_INTERVAL_S = 30.0
FPS_NOMINAL = 20.0


class FakeClock:
    """Relógio monotônico controlado pelo teste."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDocker:
    """Duplo do DockerAPI: registra restarts e não fala com o docker.sock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.restarts: list[str] = []
        # `StartedAt` do processo observado e o instante em que ele subiu.
        self.started_at: str | None = "2026-10-30T00:00:00.000000000Z"
        self.started_epoch = clock.now
        self.api_down = False

    def simulate_restart(self, started_at: str) -> None:
        self.started_at = started_at
        self.started_epoch = self.clock.now

    def container_state(self, container: str):
        if self.api_down or self.started_at is None:
            return None, None, None
        return True, self.started_at, round(
            self.clock.now - self.started_epoch, 1)

    def restart(self, container: str, timeout_s: int = 10) -> bool:
        self.restarts.append(container)
        return True

    def logs_since(self, container: str, since_epoch: float) -> str:
        return ""


class WatchdogSourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.events: list[tuple[str, dict]] = []
        self._patchers = [
            patch.object(wd, "time", SimpleNamespace(
                monotonic=self.clock.monotonic, time=self.clock.time)),
            patch.object(wd, "emit", self._record),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)

        self.dog = wd.Watchdog()
        self.dog.docker = FakeDocker(self.clock)

        # Estado do "processo do pipeline" simulado pela telemetria.
        self.frames_total = 0
        self.fps_in = FPS_NOMINAL
        self.fps_processed = FPS_NOMINAL

    def _stop_patchers(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()

    # -- utilidades ---------------------------------------------------------
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

    def feed(self) -> None:
        """Entrega uma amostra de `interval_metrics` com o estado atual."""
        self.dog._handle_metric({
            "frames_total_in": self.frames_total,
            "fps_in": self.fps_in,
            "fps_processed": self.fps_processed,
            "rss_mb": None,
        })

    def run_loop(self, seconds: float, freeze_counter: bool = False) -> None:
        """Reproduz o loop: clock em passos de CHECK_INTERVAL_S, uma métrica
        por METRICS_INTERVAL_S e `_rule1_idle()` a cada passo.

        `freeze_counter=True` mantém `frames_total_in` parado (processo vivo
        que parou de entregar frame sem reiniciar o contador).
        """
        step = wd.CHECK_INTERVAL_S
        remaining = float(seconds)
        since_metric = 0.0
        while remaining > 1e-9:
            advance = min(step, remaining)
            self.clock.advance(advance)
            remaining -= advance
            since_metric += advance
            if since_metric >= METRICS_INTERVAL_S:
                since_metric -= METRICS_INTERVAL_S
                if not freeze_counter:
                    self.frames_total += int(self.fps_in * METRICS_INTERVAL_S)
                self.feed()
            self.dog._rule1_idle()

    # -- (a) operação normal -------------------------------------------------
    def test_sequencia_normal_de_contadores_crescentes(self) -> None:
        self.run_loop(300)

        self.assertEqual(self.dog._prev_frames_total, self.frames_total)
        self.assertNotIn("no_frames_detected", self.names())
        self.assertNotIn("restart_triggered", self.names())
        self.assertNotIn("frames_counter_reset", self.names())
        # frames_total_in cresceu de verdade durante a janela
        self.assertGreater(self.frames_total, 0)

    def test_frames_recovered_quando_o_fluxo_volta(self) -> None:
        self.run_loop(150)                # passa a carência de startup
        self.events.clear()

        # fonte para de verdade: contador e fps_in parados
        self.fps_in = 0.0
        self.run_loop(wd.RULE1_FRAME_TIMEOUT_S + 20, freeze_counter=True)
        self.assertIn("no_frames_detected", self.names())

        # ... e volta
        self.events.clear()
        self.fps_in = FPS_NOMINAL
        self.run_loop(METRICS_INTERVAL_S)
        self.assertIn("frames_recovered", self.names())

    # -- (b) restart com o contador voltando para trás -----------------------
    def test_restart_zera_a_base_do_contador(self) -> None:
        """O defeito raiz: `_restart()` mantinha o high-water do processo morto."""
        self.frames_total = 1_375_018
        self.feed()

        self.dog._restart("no_frames_escalation")

        self.assertIsNone(self.dog._prev_frames_total)
        self.assertFalse(self.dog.no_frame_reported)
        self.assertEqual(self.dog.docker.restarts, ["vision-stability-app"])

    def test_restart_com_contador_reiniciando_menor_nao_escala(self) -> None:
        """Cenário do E-B: high-water 1.375.018, processo novo em ~2.900."""
        self.frames_total = 1_375_018
        self.run_loop(180)
        self.events.clear()

        self.dog._restart("no_frames_escalation")
        self.events.clear()              # isola o efeito do processo novo
        self.frames_total = 0            # processo novo: cumulativo recomeça
        self.clock.advance(3)
        self.frames_total = 2_900
        self.run_loop(180)               # ~5 min de processo novo

        self.assertNotIn("no_frames_detected", self.names())
        self.assertNotIn("restart_triggered", self.names())

    def test_rebaseline_quando_o_contador_anda_para_tras(self) -> None:
        """Restart sem o watchdog participar: rebaseline, sem esperar 19 h."""
        self.run_loop(150)               # carência de startup vencida
        self.frames_total = 1_375_018
        self.feed()                      # base do processo antigo
        self.events.clear()

        # `docker restart` manual / crash-loop: o watchdog não foi o autor,
        # então a base antiga continua na memória dele.
        self.frames_total = 2_900
        self.run_loop(180)

        self.assertEqual(self.count("frames_counter_reset"), 1)
        reset = self.first("frames_counter_reset")
        self.assertEqual(reset["previous"], 1_375_018)
        self.assertEqual(reset["current"], 2_900 + int(FPS_NOMINAL * METRICS_INTERVAL_S))
        self.assertNotIn("no_frames_detected", self.names())
        # e a base passou a ser a leitura do processo novo, não um high-water
        self.assertEqual(self.dog._prev_frames_total, self.frames_total)

    # -- (c) fps_in > 0 sozinho ---------------------------------------------
    def test_fps_in_sozinho_conta_como_frame(self) -> None:
        """`frames_total_in` estagnado, mas `fps_in > 0`: há frame na janela."""
        self.dog._prev_frames_total = 5_000
        self.frames_total = 5_000
        self.fps_in = FPS_NOMINAL

        self.run_loop(300, freeze_counter=True)

        self.assertNotIn("no_frames_detected", self.names())
        self.assertNotIn("restart_triggered", self.names())

    # -- (d) fonte realmente parada -----------------------------------------
    def test_fonte_parada_ainda_detecta_e_escala(self) -> None:
        self.run_loop(150)               # carência de startup vencida
        self.events.clear()

        self.fps_in = 0.0
        self.fps_processed = 0.0
        self.run_loop(wd.RULE1_ESCALATE_S + 120, freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())
        idle = self.first("no_frames_detected")["idle_s"]
        self.assertGreaterEqual(idle, wd.RULE1_FRAME_TIMEOUT_S)

        triggers = [f for name, f in self.events if name == "restart_triggered"]
        self.assertTrue(triggers, "a escalada precisa continuar reiniciando")
        self.assertEqual(triggers[0]["reason"], "no_frames_escalation")
        self.assertGreaterEqual(triggers[0]["idle_s"], wd.RULE1_ESCALATE_S)

    def test_escalada_respeita_o_cooldown(self) -> None:
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0
        self.run_loop(wd.RULE1_ESCALATE_S + 60, freeze_counter=True)

        self.assertEqual(self.count("restart_triggered"), 1)
        # segunda escalada dentro do cooldown -> suprimida, não repetida
        self.run_loop(wd.RULE1_ESCALATE_S + 60, freeze_counter=True)
        self.assertEqual(self.count("restart_triggered"), 1)
        self.assertGreaterEqual(self.count("restart_suppressed"), 1)

    # -- (e) restarts em série ----------------------------------------------
    def test_restarts_seguidos_nao_reintroduzem_o_falso_positivo(self) -> None:
        self.frames_total = 1_375_018
        self.run_loop(90)

        for ciclo in range(5):
            self.events.clear()
            # metade dos ciclos cai no cooldown do restart: nos dois caminhos
            # (base zerada pelo `_restart` e base antiga com rebaseline) não
            # pode aparecer no_frames_detected
            self.dog._restart("no_frames_escalation")
            self.frames_total = 0
            self.clock.advance(5)
            self.run_loop(300)
            with self.subTest(ciclo=ciclo):
                self.assertNotIn("no_frames_detected", self.names())
                self.assertEqual(self.dog._prev_frames_total, self.frames_total)

    # -- restart externo do container ---------------------------------------
    def test_restart_externo_do_container_nao_vira_no_frames(self) -> None:
        """`docker restart` externo: processo subindo não é fonte morta."""
        self.run_loop(150)
        self.events.clear()

        # container reiniciado por fora: telemetria silenciosa enquanto sobe
        self.frames_total = 0
        self.dog.docker.simulate_restart("2026-10-30T17:00:00.000000000Z")
        self.run_loop(60, freeze_counter=True)

        self.assertNotIn("no_frames_detected", self.names())
        self.assertEqual(self.count("container_restarted"), 1)

    def test_carencia_do_restart_nao_cega_o_watchdog(self) -> None:
        """A carência tem fim: fonte que não volta depois dela ainda escala."""
        self.run_loop(150)
        self.events.clear()

        self.frames_total = 0
        self.fps_in = 0.0
        self.dog.docker.simulate_restart("2026-10-30T17:00:00.000000000Z")
        self.run_loop(wd.STARTUP_GRACE_S + wd.RULE1_ESCALATE_S + 60,
                      freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())
        triggers = [f for name, f in self.events if name == "restart_triggered"]
        self.assertTrue(triggers)

    def test_crash_loop_fica_visivel(self) -> None:
        """Startups repetidos aparecem no log (ação é do Docker, não nossa)."""
        self.run_loop(150)
        self.events.clear()

        for n in range(3):
            self.dog.docker.simulate_restart(f"2026-10-30T17:0{n}:00.000000000Z")
            self.run_loop(20, freeze_counter=True)

        self.assertEqual(self.count("container_restarted"), 3)

    def test_api_do_docker_fora_volta_ao_comportamento_antigo(self) -> None:
        self.run_loop(150)
        self.events.clear()

        self.dog.docker.api_down = True
        self.fps_in = 0.0
        self.run_loop(wd.RULE1_ESCALATE_S + 120, freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())


class AgeSecondsTest(unittest.TestCase):
    def test_le_nanossegundos_do_docker(self) -> None:
        # StartedAt do Docker tem 9 casas decimais; strptime só aceita 6
        base = datetime.datetime(
            2026, 10, 30, 16, 59, 23, 123456,
            tzinfo=datetime.timezone.utc).timestamp()
        ts = "2026-10-30T16:59:23.123456789Z"
        self.assertAlmostEqual(wd._age_seconds(ts, now_epoch=base),
                               0.0, places=3)
        self.assertAlmostEqual(wd._age_seconds(ts, now_epoch=base + 30),
                               30.0, places=3)

    def test_timestamp_invalido_devolve_none(self) -> None:
        self.assertIsNone(wd._age_seconds(""))
        self.assertIsNone(wd._age_seconds("nada"))
        self.assertIsNone(wd._age_seconds(None))


class JsonlTailTest(unittest.TestCase):
    """`JsonlTail` é a fonte da regra 1: precisa continuar lendo após
    rotação (inode novo) e após truncamento no lugar (mesmo inode)."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "live.jsonl"
        self.path.write_text("historico antigo\n")
        self.tail = wd.JsonlTail(str(self.path))

    def _append(self, linha: str) -> None:
        with open(self.path, "a") as fh:
            fh.write(linha + "\n")

    def test_ignora_historico_e_le_linhas_novas(self) -> None:
        self.assertEqual(self.tail.poll(), [])
        self._append("nova 1")
        self.assertEqual(self.tail.poll(), ["nova 1"])

    def test_truncamento_no_lugar_nao_cega_o_tail(self) -> None:
        self.assertEqual(self.tail.poll(), [])       # firma a posição
        for n in range(50):
            self._append(f"linha bem comprida numero {n} " + "x" * 100)
        self.assertEqual(len(self.tail.poll()), 50)

        # truncado no lugar (mesmo inode) + linha nova
        self.path.write_text("depois do truncamento\n")

        self.assertEqual(self.tail.poll(), ["depois do truncamento"])

    def test_rotacao_por_inode_novo_reabre(self) -> None:
        self.assertEqual(self.tail.poll(), [])
        self._append("antes da rotacao")
        self.assertEqual(self.tail.poll(), ["antes da rotacao"])

        rotacionado = self.path.with_suffix(".1")
        self.path.rename(rotacionado)
        self.path.write_text("arquivo novo\n")

        self.assertEqual(self.tail.poll(), ["arquivo novo"])

    def test_arquivo_ausente_nao_quebra(self) -> None:
        self.assertEqual(self.tail.poll(), [])
        self.path.unlink()
        self.assertEqual(self.tail.poll(), [])
        self.path.write_text("voltou\n")
        self.assertEqual(self.tail.poll(), ["voltou"])


if __name__ == "__main__":
    unittest.main()
