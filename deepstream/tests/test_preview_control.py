"""Testes do ramo de preview (`deepstream/preview_control.py`).

Prendem o incidente de 2026-11-01: com o MediaMTX fora do ar o ramo de preview
falhava no boot e o pipeline **congelava** — o callback do bus chamava
`set_state(NULL)` no `rtspclientsink` (thread dump no cabeçalho do módulo) e o
loop GLib parava de despachar timers e `SIGTERM`, então as métricas sumiam e o
`docker restart` do watchdog virava `SIGKILL`.

O que estes testes garantem:
  1. o callback do bus (`request_drop`) **só marca** estado e devolve o tipo de
     evento — nenhuma decisão bloqueante, nada de GStreamer;
  2. a derrubada e a retomada são ações separadas, dadas por `step` e
     executadas fora do loop pelo pipeline;
  3. operação que não retorna dentro do teto (`op_timeout_s`) vira
     `give_up` com o loop livre — o pior caso é "sem preview", nunca
     "pipeline congelado";
  4. o orçamento de tentativas termina em `give_up` (`attempts`), sem ficar
     reagendando para sempre.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preview_control as pc  # noqa: E402

RETRY_BASE_S = 15.0
RETRY_MAX_S = 300.0
VERIFY_S = 20.0
VERIFY_MIN_FRAMES = 5
MAX_ATTEMPTS = 3
OP_TIMEOUT_S = 5.0


class ControllerTest(unittest.TestCase):
    """Base: controlador limpo e atalhos para dirigir as fatias."""

    def setUp(self) -> None:
        self.ctl = pc.PreviewBranchController(
            retry_base_s=RETRY_BASE_S,
            retry_max_s=RETRY_MAX_S,
            verify_s=VERIFY_S,
            verify_min_frames=VERIFY_MIN_FRAMES,
            max_attempts=MAX_ATTEMPTS,
            op_timeout_s=OP_TIMEOUT_S)

    def step(self, now: float, **kwargs) -> str:
        return self.ctl.step(now, **kwargs)


class RequisicaoDeQuedaTest(ControllerTest):
    def test_primeira_falha_derruba_sem_contar_tentativa(self) -> None:
        self.assertEqual(self.ctl.request_drop(), pc.DROP_FIRST)
        self.assertTrue(self.ctl.failed)
        self.assertEqual(self.ctl.attempts, 0)

    def test_falha_com_ramo_ja_derrubado_conta_tentativa(self) -> None:
        self.ctl.request_drop()
        self.assertEqual(self.ctl.request_drop(), pc.DROP_RETRY)
        self.assertEqual(self.ctl.attempts, 1)

    def test_requisicao_nao_executa_nada(self) -> None:
        # O callback do bus roda no thread do loop: aqui só muda estado.
        self.ctl.request_drop()
        self.assertIsNone(self.ctl.op)

    def test_requisicao_apos_desistencia_e_ignorada(self) -> None:
        self.ctl.request_drop()
        self.step(1.0)                      # detach
        self.step(2.0, op_done=True)        # armed (retomada em 17.0)
        self.step(2.0 + RETRY_BASE_S)       # install em voo (retomada em 17.0)
        # estoura o teto de uma operação em voo -> desiste
        self.step(2.0 + RETRY_BASE_S + OP_TIMEOUT_S + 1)
        self.assertTrue(self.ctl.exhausted)
        self.assertEqual(self.ctl.request_drop(), pc.DROP_IGNORED)


class FatiasTest(ControllerTest):
    def test_derruba_uma_vez_e_agenda(self) -> None:
        self.ctl.request_drop()
        self.assertEqual(self.step(0.0), pc.ACTION_DETACH)
        # a derrubada está em voo: nada novo começa
        self.assertEqual(self.step(0.5), pc.ACTION_NONE)
        self.assertEqual(
            self.step(1.0, op_done=True), pc.ACTION_ARMED)
        self.assertIsNone(self.ctl.op)

    def test_retomada_so_depois_do_atraso(self) -> None:
        self.ctl.request_drop()
        self.step(0.0)
        self.step(1.0, op_done=True)        # armed, espera RETRY_BASE_S
        self.assertEqual(self.step(2.0), pc.ACTION_NONE)
        self.assertEqual(
            self.step(1.0 + RETRY_BASE_S), pc.ACTION_INSTALL)

    def test_religar_espera_prova_por_frame(self) -> None:
        self.ctl.request_drop()
        self.step(0.0)
        self.step(1.0, op_done=True)
        self.step(1.0 + RETRY_BASE_S)

        retried = self.step(1.0 + RETRY_BASE_S + 1.0, op_done=True,
                            frames_total=100)
        self.assertEqual(retried, pc.ACTION_RETRIED)
        self.assertTrue(self.ctl.pending)
        self.assertIsNone(self.ctl.verify(2.0, frames_total=101))
        self.assertEqual(self.ctl.verify(3.0, frames_total=106),
                         pc.VERIFY_RESTORED)
        self.assertFalse(self.ctl.failed)
        self.assertEqual(self.ctl.attempts, 0)

    def test_sem_frame_apos_religar_derruba_de_novo(self) -> None:
        self.ctl.request_drop()
        self.step(0.0)
        self.step(1.0, op_done=True)
        self.step(1.0 + RETRY_BASE_S)
        self.step(1.0 + RETRY_BASE_S + 1.0, op_done=True, frames_total=100)

        t = 1.0 + RETRY_BASE_S + 1.0 + VERIFY_S + 1.0
        self.assertEqual(self.ctl.verify(t, frames_total=100),
                         pc.VERIFY_FAILED)
        self.assertEqual(self.ctl.attempts, 1)
        self.assertEqual(self.step(t + 0.1), pc.ACTION_DETACH)

    def test_falha_ao_recriar_derruba_e_conta_tentativa(self) -> None:
        self.ctl.request_drop()
        self.step(0.0)
        self.step(1.0, op_done=True)
        self.step(1.0 + RETRY_BASE_S)
        action = self.step(1.0 + RETRY_BASE_S + 1.0, op_done=True,
                           op_error="Falha ao linkar tee -> queue preview")
        self.assertEqual(action, pc.ACTION_RETRY_FAILED)
        self.assertEqual(self.ctl.attempts, 1)
        # a derrubada do ramo parcial é pedida na fatia seguinte
        self.assertEqual(
            self.step(1.0 + RETRY_BASE_S + 1.1), pc.ACTION_DETACH)


class TetoDeTempoTest(ControllerTest):
    """O caso do incidente: operação de GStreamer que nunca retorna."""

    def test_operacao_pendurada_nao_pende_o_loop(self) -> None:
        self.ctl.request_drop()
        self.assertEqual(self.step(0.0), pc.ACTION_DETACH)
        # o `set_state(NULL)` no rtspclientsink travou: a fatia continua
        # devolvendo o controle em vez de esperar
        self.assertEqual(self.step(1.0), pc.ACTION_NONE)
        self.assertEqual(self.step(OP_TIMEOUT_S - 0.1), pc.ACTION_NONE)
        self.assertEqual(self.step(OP_TIMEOUT_S + 1.0), pc.ACTION_GIVE_UP)
        self.assertTrue(self.ctl.exhausted)
        self.assertEqual(self.ctl.give_up_reason, pc.GIVE_UP_OP_TIMEOUT)
        # desistiu de vez: não recria o ramo nem tenta de novo
        self.assertEqual(self.step(1000.0), pc.ACTION_NONE)

    def test_finish_apos_teto_e_ignorado(self) -> None:
        self.ctl.request_drop()
        self.step(0.0)
        self.assertEqual(self.step(OP_TIMEOUT_S + 1.0), pc.ACTION_GIVE_UP)
        self.assertEqual(
            self.step(OP_TIMEOUT_S + 2.0, op_done=True), pc.ACTION_NONE)


class OrcamentoTest(ControllerTest):
    def test_desiste_apos_o_maximo_de_tentativas(self) -> None:
        t = 0.0
        self.ctl.request_drop()
        self.assertEqual(self.step(t), pc.ACTION_DETACH)
        t += 1.0
        self.assertEqual(self.step(t, op_done=True), pc.ACTION_ARMED)

        seen = []
        given_up = False
        for _ in range(MAX_ATTEMPTS * 4):
            t += 1000.0     # sempre depois do atraso e da verificação
            action = self.step(t)
            seen.append(action)
            if action == pc.ACTION_INSTALL:
                self.step(t + 1.0, op_done=True)
                self.ctl.verify(t + 2.0 + VERIFY_S)
            elif action == pc.ACTION_DETACH:
                inner = self.step(t + 1.0, op_done=True)
                seen.append(inner)
                if inner == pc.ACTION_GIVE_UP:
                    given_up = True
                    break
        self.assertTrue(given_up, seen)
        self.assertEqual(self.ctl.give_up_reason, pc.GIVE_UP_ATTEMPTS)
        self.assertTrue(self.ctl.exhausted)


if __name__ == "__main__":
    unittest.main()
