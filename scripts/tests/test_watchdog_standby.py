"""Standby do observador no watchdog (`scripts/watchdog.py`).

Cobre a transição de ida e volta do standby, que é o que evita o Jetson gastar
memória com o engine TensorRT sem fonte (o motivo de o observador existir):

- `STOP_STANDBY` idempotente: um `docker stop` por entrada em standby, nunca um
  por ciclo (`_stop_standby`);
- `START_FROM_STANDBY`: mutex reivindicado, fonte ativa gravada, container
  subido e pós-check armado **sem bloquear** o loop (`_engage`);
- pós-check com dois desfechos: frame novo -> `engaged_ok`/PASS/marcador
  liberado; prazo estourado -> stop, rollback da fonte, FAIL (`_check_wake`);
- pós-check só aprova a métrica do processo novo (`session_epoch` >=
  `StartedAt` - tolerância) e com `fps_in > 0` (`_wake_confirmed`);
- precedência: marcador de outro dono (`yield_marker`) e `ACTIVE_SOURCE` no
  ambiente (`yield_env`) nunca disputam o container;
- falha do docker.sock no `stop` (`standby_stop_failed`): modo mantido e nova
  tentativa no ciclo seguinte;
- `OBSERVER_AUTO_SWITCH=0`: a decisão é registrada, o container fica intocado;
- Regra 1 silenciada em standby: "sem frame" ali é o estado desejado.

Sem docker, sem GStreamer e sem `sleep`: o `DockerAPI` é um duplo, o relógio é
controlado pelo teste e `emit` é capturado em memória (herdado de
`WatchdogHarness`). Os nomes de evento são os do `emit()` interno, sem o
prefixo `watchdog_` do log estruturado.

Nota: `scripts/tests` não tem `__init__.py` de propósito (ver
`test_probe_logic.py`); os imports abaixo usam o caminho dos diretórios.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import observer_policy  # noqa: E402
import watchdog as wd  # noqa: E402
from test_watchdog_sources import FakeDocker, WatchdogHarness  # noqa: E402


class StopFailsDocker(FakeDocker):
    """`FakeDocker` com o knob que ele não tem: `stop` devolvendo False.

    `FakeDocker.stop` sempre devolve True (o caminho normal). Aqui o teste
    alterna `stop_ok` para exercitar a falha do docker.sock sem inventar
    mais um duplo: a tentativa continua sendo registrada em `stops` mesmo
    quando falha — é o que prova a nova tentativa do ciclo seguinte.
    """

    def __init__(self, clock) -> None:
        super().__init__(clock)
        self.stop_ok = True

    def stop(self, container: str, timeout_s: int = 10) -> bool:
        self.stops.append(container)
        return self.stop_ok


class StandbyHarness(WatchdogHarness):
    """`WatchdogHarness` + `SWITCH_LOG` em tmp e estado inicial conhecido."""

    def setUp(self) -> None:
        super().setUp()
        # O harness base não patcheia o histórico: sem isto o PASS/FAIL do
        # pós-check tentaria `data/logs/switch.jsonl` da bancada.
        self.switch_log = Path(self._tmp.name) / "switch.jsonl"
        patcher = patch.object(wd, "SWITCH_LOG", str(self.switch_log))
        patcher.start()
        self._patchers.append(patcher)   # `_stop_patchers` desfaz em ordem
        # A troca automática é knob de ambiente (`OBSERVER_AUTO_SWITCH`); o
        # teste precisa dela ligada para o `_engage` ser chamado.
        auto = patch.object(wd, "OBSERVER_AUTO_SWITCH", True)
        auto.start()
        self._patchers.append(auto)

        # Estado de container conhecido (o `Watchdog` nasce com `None`).
        self.dog._app_running = True
        self.dog._app_exit_code = 0

    # -- utilidades ---------------------------------------------------------
    def enter_standby(self) -> None:
        """Coloca o observador em standby sem passar pelo docker.

        Usado pelo `START_FROM_STANDBY`: assim `docker.stops` começa vazio e o
        teste do rollback consegue contar exatamente o stop do `_fail_wake`.
        """
        self.dog._set_mode(observer_policy.MODE_STANDBY, reason="sem_fonte_viva")
        self.dog._app_running = False
        self.dog._app_exit_code = 0

    def decide(self, action, source_id=None, reason="teste") -> None:
        self.dog._handle_decision(
            observer_policy.Decision(action, source_id=source_id, reason=reason),
            self.clock.now)

    def switch_records(self) -> list[dict]:
        if not self.switch_log.exists():
            return []
        linhas = self.switch_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(linha) for linha in linhas if linha.strip()]

    def marker_payload(self) -> dict:
        return json.loads(self.switch_marker.read_text(encoding="utf-8"))

    def write_marker(self, payload: dict) -> None:
        self.switch_marker.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


class StopStandbyTest(StandbyHarness):
    """`STOP_STANDBY`: um stop por entrada, idempotente depois."""

    def test_stop_standby_para_o_container_uma_vez(self) -> None:
        self.decide(observer_policy.Action.STOP_STANDBY,
                    reason="sem_fonte_viva")

        self.assertEqual(self.dog.docker.stops, [wd.APP_CONTAINER])
        self.assertEqual(self.dog._mode, observer_policy.MODE_STANDBY)
        self.assertEqual(self.count("standby_entered"), 1)
        self.assertFalse(self.dog._app_running)
        self.assertEqual(self.dog._last_action, "stop_standby")

    def test_stop_standby_repetido_nao_para_de_novo(self) -> None:
        self.decide(observer_policy.Action.STOP_STANDBY,
                    reason="sem_fonte_viva")
        self.clock.advance(wd.CHECK_INTERVAL_S)
        # A política devolve `STOP_STANDBY` a cada ciclo enquanto nenhuma fonte
        # viva aparece: repetir o stop de 10 em 10 s só gasta API e log.
        self.decide(observer_policy.Action.STOP_STANDBY,
                    reason="sem_fonte_viva")

        self.assertEqual(len(self.dog.docker.stops), 1)
        self.assertEqual(self.count("standby_entered"), 1)
        self.assertEqual(self.dog._last_action, "stop_standby")

    def test_falha_do_docker_mantem_o_modo_e_tenta_de_novo(self) -> None:
        """`stop` devolve False: modo inalterado, evento e nova tentativa.

        Com o container possivelmente rodando, `MODE_STANDBY` no estado seria
        mentira e esconderia o motivo: `standby_stop_failed` diz o que houve e
        o ciclo seguinte chama `stop` outra vez (não é standby idempotente).
        """
        docker = StopFailsDocker(self.clock)
        self.dog.docker = docker
        docker.stop_ok = False

        self.decide(observer_policy.Action.STOP_STANDBY,
                    reason="sem_fonte_viva")

        self.assertEqual(docker.stops, [wd.APP_CONTAINER])
        self.assertEqual(self.dog._mode, observer_policy.MODE_WATCHING)
        self.assertTrue(self.dog._app_running)
        self.assertNotIn("standby_entered", self.names())
        falha = self.first("standby_stop_failed")
        self.assertEqual(falha["container"], wd.APP_CONTAINER)
        self.assertEqual(falha["reason"], "sem_fonte_viva")
        self.assertEqual(self.dog._last_action, "stop_standby_failed")

        # docker.sock voltou: o ciclo seguinte repete o stop e entra em standby.
        self.clock.advance(wd.CHECK_INTERVAL_S)
        docker.stop_ok = True
        self.decide(observer_policy.Action.STOP_STANDBY,
                    reason="sem_fonte_viva")

        self.assertEqual(docker.stops, [wd.APP_CONTAINER, wd.APP_CONTAINER])
        self.assertEqual(self.dog._mode, observer_policy.MODE_STANDBY)
        self.assertFalse(self.dog._app_running)
        self.assertEqual(self.count("standby_entered"), 1)
        self.assertEqual(self.dog._last_action, "stop_standby")


class StartFromStandbyTest(StandbyHarness):
    """`START_FROM_STANDBY`: mutex, fonte ativa, start e pós-check armado."""

    def test_start_from_standby_reivindica_marcador_e_sobe(self) -> None:
        self.enter_standby()

        self.decide(observer_policy.Action.START_FROM_STANDBY,
                    source_id="drone", reason="fonte_viva")

        payload = self.marker_payload()
        self.assertEqual(payload["owner"], "observer")
        self.assertEqual(payload["to"], "drone")
        self.assertEqual(wd.read_active_source(str(self.active_source_file)),
                         "drone")
        self.assertEqual(self.dog.docker.starts, [wd.APP_CONTAINER])
        self.assertEqual(self.dog.docker.stops, [])
        self.assertEqual(self.dog._mode, observer_policy.MODE_SWITCHING)
        # `_engage` retorna sem esperar o pós-check: o loop não fica cego.
        self.assertIsNotNone(self.dog._wake)
        self.assertEqual(self.dog._wake["source"], "drone")
        self.assertEqual(self.dog._last_action, "start_from_standby")

    def test_start_from_standby_confirmado_com_frame(self) -> None:
        wd.write_active_source(str(self.active_source_file), "camera")
        self.enter_standby()
        # O processo que o `start()` vai criar: `StartedAt`/epoch do **mesmo**
        # relógio falso, senão o boundary do pós-check não tem como fechar.
        self.dog.docker.started_at = wd._iso_ts(self.clock.now)
        self.dog.docker.started_epoch = self.clock.now
        engaged_at = self.clock.now

        self.decide(observer_policy.Action.START_FROM_STANDBY,
                    source_id="drone", reason="fonte_viva")
        # Premissa explícita: `_engage` congela em `_wake` o `StartedAt` lido
        # logo após o `start()`. O relógio (e o duplo) andam em seguida e o
        # pós-check continua comparando com o valor congelado.
        self.assertEqual(self.dog._wake["started_at"], wd._iso_ts(engaged_at))
        self.clock.advance(wd.CHECK_INTERVAL_S)
        self.dog.docker.started_at = wd._iso_ts(self.clock.now)
        self.dog.docker.started_epoch = self.clock.now

        # Uma janela de telemetria depois do start: a amostra é do processo
        # novo (`session_epoch` do relógio falso) e traz `fps_in > 0`.
        self.clock.advance(30.0)
        self.frames_total = 600
        self.fps_in = 20.0
        self.feed(source_id="drone",
                  session_epoch=wd._iso_ts(self.clock.now))

        self.dog._check_wake(self.clock.now)

        self.assertEqual(self.dog._mode, observer_policy.MODE_WATCHING)
        self.assertEqual(self.count("standby_exited"), 1)
        self.assertIsNone(self.dog._wake)
        self.assertFalse(self.switch_marker.exists())
        records = self.switch_records()
        self.assertEqual(len(records), 1)
        record = records[-1]
        self.assertEqual(record["event"], "switch_source")
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(record["probe"], "ok")
        self.assertEqual(record["origin"], "observer")
        self.assertEqual(record["to"], "drone")
        self.assertEqual(record["from"], "camera")
        self.assertEqual(self.dog._last_action, "engage_ok")

    def test_start_from_standby_sem_frame_estoura_prazo_e_volta(self) -> None:
        wd.write_active_source(str(self.active_source_file), "camera")
        self.enter_standby()

        self.decide(observer_policy.Action.START_FROM_STANDBY,
                    source_id="drone", reason="fonte_viva")
        self.assertEqual(self.dog.docker.stops, [])

        self.clock.advance(wd.OBSERVER_WAKE_TIMEOUT_S + 5)
        self.dog._check_wake(self.clock.now)

        self.assertEqual(self.dog.docker.stops, [wd.APP_CONTAINER])
        self.assertEqual(wd.read_active_source(str(self.active_source_file)),
                         "camera")
        self.assertEqual(self.dog._mode, observer_policy.MODE_STANDBY)
        self.assertFalse(self.switch_marker.exists())
        self.assertEqual(self.count("engage_failed"), 1)
        self.assertIsNone(self.dog._wake)
        records = self.switch_records()
        self.assertEqual(len(records), 1)
        record = records[-1]
        # Shape replicado de `switch_record()`: o histórico é uma série só.
        self.assertEqual(record["event"], "switch_source")
        self.assertEqual(record["from"], "camera")
        self.assertEqual(record["to"], "drone")
        self.assertEqual(record["probe"], "ok")
        self.assertEqual(record["result"], "FAIL")
        self.assertEqual(record["reason"], "post_check_timeout")
        self.assertEqual(record["origin"], "observer")
        self.assertEqual(self.dog._last_action, "engage_failed")

    def _arm_wake(self) -> float:
        """Arma o pós-check do `START_FROM_STANDBY` e devolve o instante base.

        O `StartedAt` do processo novo vem do relógio falso antes do `decide`;
        o teste devolve o epoch do `_engage` para montar a `session_epoch` da
        métrica em relação ao processo (antes ou depois dele).
        """
        wd.write_active_source(str(self.active_source_file), "camera")
        self.enter_standby()
        self.dog.docker.started_at = wd._iso_ts(self.clock.now)
        self.dog.docker.started_epoch = self.clock.now
        engaged_at = self.clock.now

        self.decide(observer_policy.Action.START_FROM_STANDBY,
                    source_id="drone", reason="fonte_viva")

        self.assertEqual(self.dog._mode, observer_policy.MODE_SWITCHING)
        self.assertEqual(self.dog._wake["started_at"], wd._iso_ts(engaged_at))
        self.assertTrue(self.switch_marker.exists())
        return engaged_at

    def _assert_rejeitada_ate_o_fail(self) -> None:
        """Nada confirmado dentro do prazo; no estouro, FAIL e rollback."""
        # Ainda dentro do prazo: a amostra viva não pode ter virado PASS.
        self.assertEqual(self.dog._mode, observer_policy.MODE_SWITCHING)
        self.assertIsNotNone(self.dog._wake)
        self.assertTrue(self.switch_marker.exists())
        self.assertEqual(self.switch_records(), [])
        self.assertEqual(self.count("engage_ok"), 0)

        self.clock.advance(wd.OBSERVER_WAKE_TIMEOUT_S)
        self.dog._check_wake(self.clock.now)

        self.assertEqual(self.dog._mode, observer_policy.MODE_STANDBY)
        self.assertEqual(self.dog.docker.stops, [wd.APP_CONTAINER])
        self.assertIsNone(self.dog._wake)
        self.assertFalse(self.switch_marker.exists())
        self.assertEqual([r["result"] for r in self.switch_records()],
                         ["FAIL"])
        self.assertEqual(self.dog._last_action, "engage_failed")

    def test_start_from_standby_recusa_processo_anterior(self) -> None:
        """(d1) `fps_in > 0`, mas `session_epoch` 5 s antes do `StartedAt`.

        Boundary do `_wake_confirmed`: ignorar `epoch >= started -
        WAKE_EPOCH_TOLERANCE_S` aprovaria com a métrica do processo anterior:
        o "PASS na fonte errada" (contrato E3-T2).
        """
        engaged_at = self._arm_wake()
        self.clock.advance(30.0)
        self.frames_total = 600
        self.fps_in = 20.0
        self.feed(source_id="drone",
                  session_epoch=wd._iso_ts(engaged_at - 5.0))

        self.dog._check_wake(self.clock.now)

        self._assert_rejeitada_ate_o_fail()

    def test_start_from_standby_recusa_metrica_sem_frame(self) -> None:
        """(d2) `session_epoch` do processo novo, mas `fps_in == 0`.

        Remover `if self._last_fps_in <= 0: return False` aprovaria uma amostra
        que não prova frame entregue — o pós-check existe para isso.
        """
        self._arm_wake()
        self.clock.advance(30.0)
        self.frames_total = 600
        self.fps_in = 0.0
        self.feed(source_id="drone",
                  session_epoch=wd._iso_ts(self.clock.now))

        self.dog._check_wake(self.clock.now)

        self._assert_rejeitada_ate_o_fail()


class StandbyPrecedenciaTest(StandbyHarness):
    """Guardas do `_engage`: marcador alheio, ambiente, marcador expirado."""

    def test_marcador_de_outro_dono_cede_o_ciclo(self) -> None:
        self.write_marker({"to": "drone",
                           "expires_at": wd._iso_utc_seconds(
                               self.clock.now + 60)})
        self.enter_standby()

        self.decide(observer_policy.Action.START_FROM_STANDBY,
                    source_id="phone", reason="fonte_viva")

        self.assertEqual(self.dog.docker.starts, [])
        self.assertEqual(self.dog.docker.stops, [])
        self.assertEqual(self.dog._last_action, "yield_marker")
        # Marcador alheio nunca é apagado nem sobrescrito.
        self.assertEqual(self.marker_payload()["to"], "drone")
        self.assertEqual(self.dog._mode, observer_policy.MODE_STANDBY)

    def test_active_source_no_ambiente_tem_precedencia(self) -> None:
        self.enter_standby()

        with patch.object(wd, "ACTIVE_SOURCE_ENV", "camera"):
            self.decide(observer_policy.Action.START_FROM_STANDBY,
                        source_id="drone", reason="fonte_viva")

        self.assertEqual(self.dog.docker.starts, [])
        self.assertEqual(self.dog.docker.stops, [])
        self.assertEqual(self.dog._last_action, "yield_env")
        # Escolha do operador: nem marcador nem fonte ativa foram tocados.
        self.assertFalse(self.switch_marker.exists())
        self.assertFalse(self.active_source_file.exists())

    def test_marcador_expirado_e_removido_e_reivindicado(self) -> None:
        self.write_marker({"to": "drone", "owner": "dashboard",
                           "expires_at": wd._iso_utc_seconds(
                               self.clock.now - 10)})
        self.enter_standby()

        self.decide(observer_policy.Action.START_FROM_STANDBY,
                    source_id="phone", reason="fonte_viva")

        self.assertEqual(self.dog.docker.starts, [wd.APP_CONTAINER])
        self.assertEqual(self.dog._last_action, "start_from_standby")
        payload = self.marker_payload()
        self.assertEqual(payload["owner"], "observer")
        self.assertEqual(payload["to"], "phone")


class AutoSwitchDesligadoTest(StandbyHarness):
    """`OBSERVER_AUTO_SWITCH=0`: a política mede, mas não toca no container."""

    def test_knob_desligado_start_from_standby_nao_sobe(self) -> None:
        """Comportamento observado em `_handle_decision`: `_engage` não roda.

        A ação é só registrada (`last_action`) e o modo fica em `standby`: a
        política continua medindo, quem religa o pipeline é o operador.
        """
        self.enter_standby()

        with patch.object(wd, "OBSERVER_AUTO_SWITCH", False):
            self.decide(observer_policy.Action.START_FROM_STANDBY,
                        source_id="drone", reason="fonte_viva")

        self.assertEqual(self.dog.docker.starts, [])
        self.assertEqual(self.dog.docker.restarts, [])
        self.assertFalse(self.active_source_file.exists())
        self.assertFalse(self.switch_marker.exists())
        self.assertIsNone(self.dog._wake)
        self.assertEqual(self.dog._mode, observer_policy.MODE_STANDBY)
        self.assertEqual(self.dog._last_action, "start_from_standby")

    def test_knob_desligado_switch_to_nao_recria_container(self) -> None:
        """`SWITCH_TO` com o knob desligado: modo corrente mantido, sem troca.

        Nem `auto_switch_triggered` sai: o evento é do caminho automático que
        o operador desligou.
        """
        with patch.object(wd, "OBSERVER_AUTO_SWITCH", False):
            self.decide(observer_policy.Action.SWITCH_TO,
                        source_id="drone", reason="fonte_viva")

        self.assertEqual(self.dog.docker.starts, [])
        self.assertEqual(self.dog.docker.restarts, [])
        self.assertFalse(self.active_source_file.exists())
        self.assertFalse(self.switch_marker.exists())
        self.assertIsNone(self.dog._wake)
        self.assertEqual(self.dog._mode, observer_policy.MODE_WATCHING)
        self.assertEqual(self.dog._last_action, "switch_to")
        self.assertNotIn("auto_switch_triggered", self.names())


class StandbyRule1Test(StandbyHarness):
    """Regra 1 em standby: contador congelado não é fonte morta."""

    def test_contador_congelado_em_standby_nao_escala(self) -> None:
        self.frames_total = 500
        self.dog._prev_frames_total = 500

        for mode in (observer_policy.MODE_STANDBY,
                     observer_policy.MODE_SWITCHING):
            with self.subTest(mode=mode):
                self.enter_standby()
                self.dog._mode = mode
                self.run_loop(300, freeze_counter=True)

                self.assertNotIn("no_frames_escalation", self.names())
                self.assertNotIn("no_frames_detected", self.names())
                self.assertEqual(self.dog.docker.restarts, [])
                self.assertEqual(self.dog.docker.stops, [])
