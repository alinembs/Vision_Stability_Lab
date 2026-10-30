"""Testes do orçamento de reconexões da fonte (`deepstream/source_budget.py`).

Motivação (Menor 8 da revisão): `pipeline_real.py` zerava
`_source_reconnect_attempts` a cada frame recebido. Fonte oscilante — frame,
EOS, frame, EOS — devolvia o orçamento antes de esgotá-lo, o `exit 2`
(`EXIT_SOURCE_LOST`) nunca acontecia e o log só mostrava `eos` repetido.

A regra mora em módulo puro justamente para poder ser testada sem `gi`/`pyds`.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_budget import SourceReconnectBudget  # noqa: E402

MAX_ATTEMPTS = 5
RESET_S = 600.0


class SourceReconnectBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = SourceReconnectBudget(MAX_ATTEMPTS, RESET_S)

    def _eos(self, now: float) -> int:
        attempt = self.budget.on_eos(now)
        if not self.budget.exhausted(attempt):
            self.budget.commit(attempt)
        return attempt

    def test_eos_seguidos_esgotam_o_orcamento(self) -> None:
        for n in range(MAX_ATTEMPTS):
            self.assertEqual(self._eos(float(n)), n + 1)
        self.assertFalse(self.budget.exhausted(self.budget.attempts))
        # a 6ª estoura o teto: é o caminho de `source_reconnect_exhausted`
        self.assertTrue(self.budget.exhausted(self.budget.attempts + 1))

    def test_frame_isolado_nao_devolve_o_orcamento(self) -> None:
        """O defeito raiz: um único frame entre dois EOS zerava a contagem."""
        self._eos(0.0)
        self._eos(30.0)
        self.assertEqual(self.budget.attempts, 2)

        # frames logo depois do EOS não devolvem nada
        self.assertFalse(self.budget.on_frame(31.0))
        self.assertFalse(self.budget.on_frame(60.0))
        self.assertEqual(self.budget.attempts, 2)

    def test_fonte_oscilante_chega_ao_exit_2(self) -> None:
        """frame + EOS em ciclo: o orçamento esgota e o pipeline desiste."""
        now = 0.0
        esgotado = False
        for _ in range(MAX_ATTEMPTS + 1):
            now += 30.0                     # EOS
            attempt = self.budget.on_eos(now)
            self.budget.commit(attempt)
            esgotado = self.budget.exhausted(attempt)
            now += 1.0                      # frame (fonte "volta" por 1 s)
            self.budget.on_frame(now)
        self.assertTrue(esgotado)

    def test_janela_sem_eos_devolve_o_orcamento(self) -> None:
        self._eos(0.0)
        self._eos(10.0)
        self.assertEqual(self.budget.attempts, 2)

        # passou RESET_S desde o último EOS: a fonte ficou estável
        self.assertTrue(self.budget.on_frame(10.0 + RESET_S))
        self.assertEqual(self.budget.attempts, 0)
        # e o orçamento volta inteiro
        self.assertEqual(self._eos(10.0 + RESET_S + 1), 1)

    def test_limiar_da_janela_e_inclusivo_no_reset(self) -> None:
        self._eos(0.0)
        self.assertFalse(self.budget.on_frame(RESET_S - 0.1))
        self.assertTrue(self.budget.on_frame(RESET_S))

    def test_sem_eos_nao_ha_o_que_devolver(self) -> None:
        self.assertFalse(self.budget.on_frame(0.0))
        self.assertEqual(self.budget.attempts, 0)

    def test_commit_nao_recua_a_contagem(self) -> None:
        self.budget.on_eos(0.0)
        self.budget.commit(3)
        self.budget.commit(1)
        self.assertEqual(self.budget.attempts, 3)

    def test_eos_com_reconexao_em_curso_nao_conta(self) -> None:
        """Reconexão recusada (`_reconnect_source` False) não gasta tentativa."""
        attempt = self.budget.on_eos(0.0)
        self.assertEqual(attempt, 1)        # sem `commit`
        self.assertEqual(self.budget.attempts, 0)
        self.assertEqual(self.budget.on_eos(5.0), 1)


if __name__ == "__main__":
    unittest.main()
