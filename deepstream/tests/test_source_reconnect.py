"""Testes da reconexão da fonte (`deepstream/source_reconnect.py`).

Cobrem o bug de E2 (2026-10-30): com a fonte perdida o pipeline congelava
porque o callback do bus fazia trabalho de GStreamer no thread do loop GLib
(thread dump no cabeçalho de `source_reconnect.py`). A correção separou
**decisão** (aqui, puro) de **execução** (fatias do timer em
`pipeline_real.py`).

O que estes testes prendem, e que já regrediu uma vez:
  1. agendamento — `begin`/`fail` só mudam de fase, nada executa na chamada;
  2. tentativas — cada falha gasta uma do orçamento, uma reconexão em curso
     não gasta duas;
  3. desfecho — sempre `STEP_RESUMED` (fonte voltou) ou `STEP_GIVE_UP`
     (orçamento esgotado, `EXIT_SOURCE_LOST`=2), nunca um ciclo pendurado.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import source_reconnect as sr  # noqa: E402
from source_budget import SourceReconnectBudget  # noqa: E402

MAX_ATTEMPTS = 3
RESET_S = 600.0
STOP_TIMEOUT_S = 3.0
FRAME_TIMEOUT_S = 15.0
RETRY_BASE_S = 10.0
RETRY_MAX_S = 45.0


class MachineTest(unittest.TestCase):
    """Base: máquina limpa + atalhos para dirigir as fatias."""

    def setUp(self) -> None:
        self.budget = SourceReconnectBudget(MAX_ATTEMPTS, RESET_S)
        self.machine = sr.SourceReconnectMachine(
            self.budget,
            stop_timeout_s=STOP_TIMEOUT_S,
            frame_timeout_s=FRAME_TIMEOUT_S,
            retry_base_s=RETRY_BASE_S,
            retry_max_s=RETRY_MAX_S)

    def step(self, now: float, stopped: bool = False,
             frames_total: int = 0) -> str:
        return self.machine.step(now, stopped=stopped, frames_total=frames_total)


class AgendamentoTest(MachineTest):
    """O callback do bus só decide: nenhuma transição acontece na chamada."""

    def test_begin_nao_executa_nada(self) -> None:
        attempt = self.machine.begin(100.0, "eos")
        self.assertEqual(attempt, 1)
        self.assertEqual(self.machine.phase, sr.PHASE_STOPPING)
        self.assertEqual(self.budget.attempts, 1)

    def test_primeira_fatia_pede_o_stop_e_so_uma_vez(self) -> None:
        self.machine.begin(100.0, "eos")
        self.assertEqual(self.step(100.1), sr.STEP_STOP)
        # a mesma fase de parada não pede outro `set_state(NULL)`
        self.assertEqual(self.step(100.2), sr.STEP_NONE)
        self.assertEqual(self.step(101.0), sr.STEP_NONE)

    def test_stop_confirmado_pede_play_e_espera_frame(self) -> None:
        self.machine.begin(100.0, "eos")
        self.assertEqual(self.step(100.1), sr.STEP_STOP)
        self.assertEqual(self.step(100.2, stopped=True), sr.STEP_START)
        self.assertEqual(self.machine.phase, sr.PHASE_WAITING)

    def test_stop_sem_confirmacao_estoura_no_teto_e_segue(self) -> None:
        """`set_state` que não confirma NULL não pode prender a reconexão."""
        self.machine.begin(100.0, "eos")          # teto do stop: 100 + 3
        self.assertEqual(self.step(100.1), sr.STEP_STOP)
        self.assertEqual(self.step(102.9), sr.STEP_NONE)
        self.assertEqual(self.step(103.0), sr.STEP_START)
        self.assertEqual(self.machine.note, "stop_timeout")
        # a nota é lida (e zerada) por quem chama, uma vez só
        self.machine.note = ""
        self.assertEqual(self.step(104.0), sr.STEP_NONE)

    def test_reconexao_em_curso_nao_gasta_outra_tentativa(self) -> None:
        self.machine.begin(100.0, "eos")
        self.assertIsNone(self.machine.begin(100.5, "eos"))
        # erro da fonte no meio de um ciclo já agendado não conta de novo
        self.assertIsNone(self.machine.fail(100.6, "bus_error"))
        self.assertEqual(self.budget.attempts, 1)


class DesfechoTest(MachineTest):
    """Todo ciclo termina em RESUMED ou GIVE_UP."""

    def test_frame_voltou_encerra_a_reconexao(self) -> None:
        self.machine.begin(100.0, "eos")
        self.step(100.1)                       # stop
        self.step(100.2, stopped=True)         # start
        self.assertEqual(self.step(101.0, frames_total=50), sr.STEP_RESUMED)
        self.assertEqual(self.machine.phase, sr.PHASE_IDLE)
        self.assertFalse(self.machine.active)

    def test_frame_antigo_nao_conta_como_volta_da_fonte(self) -> None:
        """Só frames **depois** do PLAYING provam que a fonte voltou."""
        self.machine.begin(100.0, "eos")
        self.step(100.1)
        self.step(100.2, stopped=True, frames_total=50)   # base = 50
        self.assertEqual(self.step(101.0, frames_total=50), sr.STEP_NONE)
        self.assertEqual(self.step(102.0, frames_total=51), sr.STEP_RESUMED)

    def test_sem_frame_apos_o_teto_conta_falha_e_reagenda(self) -> None:
        self.machine.begin(100.0, "eos")
        self.step(100.1)
        self.step(100.2, stopped=True, frames_total=7)   # base = 7
        action = self.step(100.2 + FRAME_TIMEOUT_S, frames_total=7)
        self.assertEqual(action, sr.STEP_RETRY)
        self.assertEqual(self.budget.attempts, 2)
        self.assertEqual(self.machine.phase, sr.PHASE_BACKOFF)
        self.assertEqual(self.machine.reason, "no_frames_after_reconnect")

    def test_orcamento_esgotado_da_desfecho(self) -> None:
        now = 100.0
        for _ in range(MAX_ATTEMPTS):
            if self.machine.phase == sr.PHASE_IDLE:
                self.machine.begin(now, "eos")
            self.step(now + 0.1)                       # stop
            self.step(now + 0.2, stopped=True)         # start
            action = self.step(now + 0.2 + FRAME_TIMEOUT_S)
            now += 0.2 + FRAME_TIMEOUT_S + RETRY_MAX_S + 0.1
            self.step(now)                             # backoff -> stopping
        # a próxima falha estoura o teto: GIVE_UP, nunca "pendurado"
        self.step(now + 0.1)                           # stop
        self.step(now + 0.2, stopped=True)             # start
        action = self.step(now + 0.2 + FRAME_TIMEOUT_S)
        self.assertEqual(action, sr.STEP_GIVE_UP)
        self.assertTrue(self.machine.exhausted)
        # e continua em GIVE_UP: o pipeline encerra com exit 2
        self.assertEqual(self.step(now + 100.0), sr.STEP_GIVE_UP)
        self.assertIsNone(self.machine.begin(now + 101.0, "eos"))

    def test_erro_da_fonte_gasta_tentativa_e_nao_mata_o_processo(self) -> None:
        """`rtspsrc` sem EOS (path inexistente) também entra no orçamento."""
        attempt = self.machine.fail(0.0, "bus_error")
        self.assertEqual(attempt, 1)
        self.assertEqual(self.machine.phase, sr.PHASE_STOPPING)
        self.assertEqual(self.machine.reason, "bus_error")
        self.assertEqual(self.budget.attempts, 1)

    def test_erros_seguidos_chegam_ao_orcamento_esgotado(self) -> None:
        now = 0.0
        self.assertEqual(self.machine.fail(now, "bus_error"), 1)   # STOPPING
        for n in range(2, MAX_ATTEMPTS + 1):
            self.assertEqual(self.step(now + 0.1), sr.STEP_STOP)
            self.assertEqual(self.step(now + 0.2, stopped=True),
                             sr.STEP_START)
            now += 0.2
            # erro do `rtspsrc` com a tentativa em andamento: conta a próxima
            self.assertEqual(self.machine.fail(now, "bus_error"), n)
            now += RETRY_MAX_S + 0.1
            self.assertEqual(self.step(now), sr.STEP_NONE)   # backoff -> stop
        self.assertEqual(self.step(now + 0.1), sr.STEP_STOP)
        self.assertEqual(self.step(now + 0.2, stopped=True), sr.STEP_START)
        now += 0.2
        self.assertIsNone(self.machine.fail(now, "bus_error"))
        self.assertTrue(self.machine.exhausted)
        self.assertEqual(self.step(now + 0.3), sr.STEP_GIVE_UP)


class BackoffTest(MachineTest):
    """Espera entre tentativas: cresce, tem teto e não trava o primeiro reset."""

    def test_primeira_tentativa_e_imediata(self) -> None:
        self.machine.begin(0.0, "eos")
        self.assertEqual(self.machine._retry_delay(), 0.0)

    def test_espera_cresce_e_respeita_o_teto(self) -> None:
        esperas = []
        for attempt in range(2, 9):
            self.machine.attempt = attempt
            esperas.append(self.machine._retry_delay())
        self.assertEqual(esperas[:3], [10.0, 20.0, 40.0])
        self.assertEqual(max(esperas), RETRY_MAX_S)

    def test_backoff_segura_a_reconexao_antes_de_nova_tentativa(self) -> None:
        self.machine.begin(100.0, "eos")
        self.step(100.1)                                   # stop
        self.step(100.2, stopped=True)                     # start
        now = 100.2
        for attempt in range(2, MAX_ATTEMPTS + 1):
            self.assertEqual(self.step(now + FRAME_TIMEOUT_S, frames_total=0),
                             sr.STEP_RETRY)
            espera = self.machine._retry_delay()
            self.assertGreater(espera, 0.0)
            peak = now + FRAME_TIMEOUT_S
            # antes do fim do backoff não há novo stop
            self.assertEqual(self.step(peak + espera - 0.1), sr.STEP_NONE)
            self.assertEqual(self.step(peak + espera), sr.STEP_NONE)
            self.assertEqual(self.step(peak + espera + 0.1), sr.STEP_STOP)
            self.assertEqual(self.step(peak + espera + 0.2, stopped=True),
                             sr.STEP_START)
            now = peak + espera + 0.2


class OrcamentoCompartilhadoTest(MachineTest):
    """A máquina usa o mesmo orçamento de antes (devolução por estabilidade)."""

    def test_janela_estavel_devolve_o_orcamento(self) -> None:
        self.machine.begin(100.0, "eos")
        self.assertEqual(self.budget.attempts, 1)
        # fonte volta e fica estável: `_on_input_buffer` devolve o crédito
        self.step(100.1)                                  # stop
        self.step(100.2, stopped=True)                    # start
        self.assertEqual(self.step(101.0, frames_total=1), sr.STEP_RESUMED)
        self.assertTrue(self.budget.on_frame(101.0 + RESET_S))
        self.assertEqual(self.budget.attempts, 0)
        # e a próxima falha volta a ser a tentativa 1
        self.assertEqual(self.machine.begin(200.0 + RESET_S, "eos"), 1)
        self.assertEqual(self.budget.attempts, 1)

    def test_max_attempts_vem_do_orcamento(self) -> None:
        self.assertEqual(self.machine.max_attempts, MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
