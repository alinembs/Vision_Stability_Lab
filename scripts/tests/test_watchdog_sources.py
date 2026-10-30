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
import json
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
        # Uptime fixo devolvido pela API: reproduz o relógio de parede atrás do
        # `StartedAt` (passo de NTP para trás), em que a idade é sempre 0.0.
        self.uptime_override: float | None = None
        # Chamado a cada restart simulado: um processo novo tem contador
        # cumulativo próprio. Sem isso o duplo fingia frame na primeira amostra
        # de todo processo novo (`prev is None` + contador herdado > 0).
        self.on_process_reset = None

    def simulate_restart(self, started_at: str) -> None:
        self.started_at = started_at
        self.started_epoch = self.clock.now
        if self.on_process_reset is not None:
            self.on_process_reset()

    def container_state(self, container: str):
        if self.api_down or self.started_at is None:
            return None, None, None
        uptime = (self.uptime_override if self.uptime_override is not None
                  else round(self.clock.now - self.started_epoch, 1))
        return True, self.started_at, uptime

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
        # `frames_total_in` é cumulativo por processo: um restart externo faz o
        # contador do pipeline recomeçar em zero (pipeline_real.py).
        self.dog.docker.on_process_reset = self._reset_frames_counter

    def _reset_frames_counter(self) -> None:
        self.frames_total = 0

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

    def test_restart_isolado_aparece_no_log(self) -> None:
        """3 restarts isolados: o log mostra o restart (1 linha por janela)."""
        self.run_loop(150)
        self.events.clear()

        for n in range(3):
            self.dog.docker.simulate_restart(f"2026-10-30T17:0{n}:00.000000000Z")
            self.run_loop(20, freeze_counter=True)

        self.assertEqual(self.count("container_restarted"), 1)

    # -- crash-loop (I1) -----------------------------------------------------
    def _crash_loop(self, reinicios: int, intervalo_s: float = 60.0) -> None:
        """Reproduz o backoff do Docker: um restart a cada `intervalo_s`."""
        for n in range(reinicios):
            self.dog.docker.simulate_restart(
                f"2026-10-30T17:{n:02d}:00.000000000Z")
            self.run_loop(intervalo_s, freeze_counter=True)

    def test_crash_loop_escala_e_emite_evento(self) -> None:
        """I1: 5 restarts em 15 min não podem manter o watchdog cego.

        Antes do fix, com fonte morta e restart a cada ~60 s, o uptime era
        sempre < STARTUP_GRACE_S: `_rule1_idle` retornava antes de
        `no_frames_detected` e de `_restart()` para sempre.
        """
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0
        self.fps_processed = 0.0

        self._crash_loop(wd.CRASH_LOOP_MIN_RESTARTS + 1)

        detectado = self.first("crash_loop_detected")
        self.assertGreaterEqual(detectado["restarts"],
                                wd.CRASH_LOOP_MIN_RESTARTS)
        self.assertEqual(detectado["window_s"], wd.CRASH_LOOP_WINDOW_S)

        triggers = [f for name, f in self.events if name == "restart_triggered"]
        self.assertTrue(triggers, "crash-loop precisa escalar restart")
        self.assertEqual(triggers[0]["reason"], "crash_loop")
        self.assertGreaterEqual(triggers[0]["restarts"],
                               wd.CRASH_LOOP_MIN_RESTARTS)

    def test_crash_loop_abaixo_do_limiar_nao_escala(self) -> None:
        """4 restarts em 15 min ainda são restart isolado: carência vale."""
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0
        self.fps_processed = 0.0

        self._crash_loop(wd.CRASH_LOOP_MIN_RESTARTS - 1)

        self.assertNotIn("crash_loop_detected", self.names())
        self.assertNotIn("restart_triggered", self.names())

    def test_crash_loop_detectado_uma_vez_por_janela(self) -> None:
        """A escalada é uma por janela: nada de `crash_loop_detected` a cada 10 s."""
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0

        self._crash_loop(wd.CRASH_LOOP_MIN_RESTARTS + 4)

        self.assertEqual(self.count("crash_loop_detected"), 1)

    def test_crash_loop_que_estabiliza_volta_a_vigiar(self) -> None:
        """Parou de reiniciar: a Regra 1 volta a cobrar frames."""
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0
        self.fps_processed = 0.0

        self._crash_loop(wd.CRASH_LOOP_MIN_RESTARTS)
        self.assertIn("crash_loop_detected", self.names())
        self.events.clear()

        # container estável (sem restart novo) e fonte morta
        self.run_loop(wd.CRASH_LOOP_ACTIVE_S + wd.STARTUP_GRACE_S
                      + wd.RULE1_ESCALATE_S + 60, freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())
        self.assertNotIn("crash_loop_detected", self.names())

    def test_container_restarted_nao_vira_spam(self) -> None:
        """10 restarts em ~3 min: no máximo uma linha `container_restarted`."""
        self.run_loop(150)
        self.events.clear()

        for n in range(10):
            self.dog.docker.simulate_restart(
                f"2026-10-30T17:{n:02d}:00.000000000Z")
            self.run_loop(20, freeze_counter=True)

        self.assertEqual(self.count("container_restarted"), 1)

    def test_uptime_desconhecido_nao_impede_a_regra_1(self) -> None:
        """I2: `uptime` None (parse ruim) não pode suspender a Regra 1."""
        self.run_loop(150)
        self.events.clear()

        started = self.dog.docker.started_at
        # mesmo StartedAt (sem restart) e uptime desconhecida: o caminho que
        # um `StartedAt` malformado produz no DockerAPI real.
        self.dog.docker.container_state = lambda _c: (True, started, None)
        self.fps_in = 0.0
        self.run_loop(wd.RULE1_ESCALATE_S + 120, freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())
        # Regra 1 inteira: detecta E escala (comportamento antigo preservado)
        triggers = [f for name, f in self.events if name == "restart_triggered"]
        self.assertTrue(triggers)
        self.assertEqual(triggers[0]["reason"], "no_frames_escalation")
        self.assertNotIn("watchdog_error", self.names())

    def test_api_do_docker_fora_volta_ao_comportamento_antigo(self) -> None:
        self.run_loop(150)
        self.events.clear()

        self.dog.docker.api_down = True
        self.fps_in = 0.0
        self.run_loop(wd.RULE1_ESCALATE_S + 120, freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())

    # -- crash-loop: corroboração por frames (D1) ----------------------------
    def _crash_loop_cadence(self, reinicios: int, intervalo_s: float,
                            fonte_viva: bool = True) -> None:
        """Restarts externos em série, com a fonte viva ou morta entre eles."""
        for n in range(reinicios):
            self.dog.docker.simulate_restart(
                f"2026-10-30T19:{n:02d}:00.000000000Z")
            self.run_loop(intervalo_s, freeze_counter=not fonte_viva)

    def _restart_reasons(self) -> list[str]:
        return [f["reason"] for name, f in self.events
                if name == "restart_triggered"]

    def _docker_api(self, payload: dict | None, status: int = 200):
        """`DockerAPI` real com transporte falso (parse de `StartedAt` incluso).

        Permite exercitar o caminho de verdade de `container_state` — parse do
        timestamp e código HTTP — sem docker.sock. Devolve a API e a lista de
        paths de restart efetivamente pedidos.
        """
        api = wd.DockerAPI("/tmp/watchdog-test-docker.sock")
        restarts: list[str] = []

        def request(method, path, timeout=20):
            if path.endswith("/json"):
                body = b"{}" if payload is None else json.dumps(payload).encode()
                return status, body
            if "restart" in path:
                restarts.append(path)
                return 204, b""
            return 200, b""

        api.request = request
        return api, restarts

    def test_restarts_em_serie_com_frames_fluindo_nao_reiniciam_o_pipeline(
            self) -> None:
        """D1: 5 restarts em 14 min com a fonte a 20 fps o tempo todo.

        É a assinatura de `StartedAt` de um `docker compose up -d` ou de um
        ciclo de `switch_source.sh`. Sem corroboração de frames o watchdog
        reiniciava um pipeline saudável (evidência do revisor: `restart_result`
        com a fonte entregando 20 fps) — ~35 s sem vídeo e 5 min de vigilância
        de RSS cega (RSS_BASELINE_GRACE_S) dentro da janela do E6/E7.
        """
        self.run_loop(150)
        self.events.clear()

        self._crash_loop_cadence(wd.CRASH_LOOP_MIN_RESTARTS, 840.0 / 5)

        # o pipeline está entregando frame: nenhum restart pode ser acionado
        self.assertEqual(self._restart_reasons(), [])
        self.assertEqual(self.dog.docker.restarts, [])
        self.assertNotIn("restart_result", self.names())

        detectado = self.first("crash_loop_detected")
        self.assertEqual(detectado["restarts"], wd.CRASH_LOOP_MIN_RESTARTS)
        self.assertTrue(detectado["frames_flowing"])
        self.assertLess(detectado["source_idle_s"], wd.RULE1_FRAME_TIMEOUT_S)
        # avisa uma vez (é restart em série de verdade) e não toca no container
        self.assertEqual(self.count("crash_loop_detected"), 1)

    def test_restarts_em_serie_com_fonte_morta_continuam_escalando(self) -> None:
        """I1 preservado: mesma assinatura de restarts, agora sem frames."""
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0
        self.fps_processed = 0.0

        self._crash_loop_cadence(wd.CRASH_LOOP_MIN_RESTARTS, 840.0 / 5,
                                 fonte_viva=False)

        detectado = self.first("crash_loop_detected")
        self.assertEqual(self._restart_reasons(), ["crash_loop"])
        self.assertEqual(self.dog.docker.restarts, ["vision-stability-app"])
        self.assertFalse(detectado["frames_flowing"])
        self.assertGreaterEqual(detectado["source_idle_s"],
                                wd.RULE1_FRAME_TIMEOUT_S)

    def test_re_reporte_nao_reinicia_container_ja_estabilizado(self) -> None:
        """D1 (achado secundário): loop encerrado logo antes do re-reporte.

        O gate "já reportado" vence em reportado+900 s, quando a janela ainda
        guarda os restarts do loop que já acabou — o watchdog reiniciava um
        container estabilizado 60 s antes (provado pelo revisor: loop de 25 min
        com estabilização em reportado+840 s).
        """
        self.run_loop(150)
        self.events.clear()

        # restart a cada 60 s por 20 min: o reporte cai no 5º ciclo
        self._crash_loop_cadence(20, 60.0)
        self.assertEqual(self.count("crash_loop_detected"), 1)
        self.events.clear()

        # estabilizou em reportado+840 s; o gate de 900 s vence depois disso
        self.run_loop(130)

        self.assertEqual(self.count("crash_loop_detected"), 0)
        self.assertEqual(self._restart_reasons(), [])
        self.assertEqual(self.dog.docker.restarts, [])

    def test_loop_longo_com_fonte_morta_nao_reinsiste_no_restart(self) -> None:
        """Escalada é uma por tempestade: o container estabilizado não é alvo."""
        self.run_loop(150)
        self.events.clear()
        self.fps_in = 0.0
        self.fps_processed = 0.0

        self._crash_loop_cadence(20, 60.0, fonte_viva=False)
        self.assertEqual(self._restart_reasons(), ["crash_loop"])
        self.events.clear()

        self.run_loop(130)

        self.assertEqual(self._restart_reasons(), [])
        self.assertEqual(len(self.dog.docker.restarts), 1)

    # -- carência: monotônico observado (D2) ---------------------------------
    def test_uptime_zero_repetido_nao_muta_a_regra_1(self) -> None:
        """D2: `StartedAt` no futuro (NTP para trás) -> uptime 0.0 em todo poll.

        A carência de `uptime < STARTUP_GRACE_S` virava portão absoluto e a
        Regra 1 ficava muda para sempre (30 min de fonte morta -> só
        `fps_degradation`). A carência precisa vir do monotônico observado.
        """
        self.run_loop(150)
        self.events.clear()

        self.dog.docker.uptime_override = 0.0
        self.fps_in = 0.0
        self.fps_processed = 0.0
        self.run_loop(30 * 60, freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())
        self.assertGreaterEqual(self.count("no_frames_detected"), 1)
        self.assertEqual(set(self._restart_reasons()), {"no_frames_escalation"})
        # escalou de verdade (não ficou só olhando): o cooldown limita o resto
        self.assertGreaterEqual(len(self.dog.docker.restarts), 1)

    def test_started_at_invalido_estavel_nao_cega_a_regra_1(self) -> None:
        """I2 (regressão): `StartedAt` impossível em todos os polls."""
        self.run_loop(150)
        self.events.clear()

        api, restarts = self._docker_api({"State": {
            "Running": True, "StartedAt": "2026-13-45T99:99:99.123456789Z"}})
        self.dog.docker = api
        self.fps_in = 0.0
        self.fps_processed = 0.0
        self.run_loop(wd.STARTUP_GRACE_S + wd.RULE1_ESCALATE_S + 60,
                      freeze_counter=True)

        self.assertIn("no_frames_detected", self.names())
        self.assertIn("no_frames_escalation", self._restart_reasons())
        self.assertEqual(len(restarts), 1)
        self.assertNotIn("watchdog_error", self.names())


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

    def test_timestamp_com_data_impossivel_devolve_none(self) -> None:
        """I2: o regex casa `13-45T99:99:99`; o `strptime` levanta ValueError."""
        self.assertIsNone(wd._age_seconds("2026-13-45T99:99:99.123456789Z"))
        self.assertIsNone(wd._age_seconds("2026-02-30T10:00:00.000000000Z"))

    def test_uptime_negativo_vira_zero(self) -> None:
        """Relógio do host atrás do StartedAt: idade negativa é descartada."""
        base = datetime.datetime(
            2026, 10, 30, 17, 0, 0,
            tzinfo=datetime.timezone.utc).timestamp()
        ts = "2026-10-30T17:00:00.000000000Z"
        self.assertEqual(wd._age_seconds(ts, now_epoch=base - 60), 0.0)


class ContainerStateTest(unittest.TestCase):
    """I2: nada na leitura de estado pode levantar para o loop do watchdog."""

    @staticmethod
    def _api(payload: dict) -> wd.DockerAPI:
        api = wd.DockerAPI("/var/run/docker.sock")
        api.request = lambda *a, **k: (200, json.dumps(payload).encode())
        return api

    def test_started_at_impossivel_devolve_uptime_none(self) -> None:
        started = "2026-13-45T99:99:99.123456789Z"
        api = self._api({"State": {"Running": True, "StartedAt": started}})
        # não levanta — é o que mantém `_rule4_ram`/`_rule5_cuda_logs` no ciclo
        self.assertEqual(api.container_state("c"), (True, started, None))

    def test_started_at_texto_livre_devolve_uptime_none(self) -> None:
        api = self._api({"State": {"Running": True, "StartedAt": "nada"}})
        self.assertEqual(api.container_state("c"), (True, "nada", None))

    def test_started_at_valido_continua_calculando(self) -> None:
        api = self._api({"State": {
            "Running": True,
            "StartedAt": "2026-10-30T16:59:23.123456789Z"}})
        running, started, uptime = api.container_state("c")
        self.assertTrue(running)
        self.assertEqual(started, "2026-10-30T16:59:23.123456789Z")
        self.assertIsNotNone(uptime)


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
