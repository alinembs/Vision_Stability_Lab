"""Orçamento de reconexões da fonte (EOS) devolvido por janela de tempo.

Fica separado de `pipeline_real.py` de propósito — mesmo motivo de
`bus_classifier.py`: aquele módulo exige `gi`, `pyds` e um ambiente DeepStream
completo só para ser importado, e é a **regra** ("quando esta fonte deve ser
declarada perdida") que precisa de teste rápido sem GPU.

Motivação (Menor 8 da revisão): `_on_input_buffer` zerava o contador de
reconexões a **cada frame recebido**, então uma fonte oscilante
(frame + EOS + frame + EOS) devolvia o orçamento antes de esgotá-lo e o
processo nunca chegava ao exit 2 (`EXIT_SOURCE_LOST`) — a fonte morta ficava
só como `eos` repetido no log, e o watchdog era a única linha de defesa que
percebia.
"""

from __future__ import annotations


class SourceReconnectBudget:
    """Conta reconexões por EOS e só devolve o orçamento após estabilidade.

    O orçamento é devolvido quando a fonte fica `reset_after_s` segundos sem
    EOS (o frame continua sendo evidência de vida, mas não basta sozinho: com
    fonte oscilante, um único frame entre dois EOS não pode zerar a contagem).
    """

    def __init__(self, max_attempts: int, reset_after_s: float) -> None:
        self.max_attempts = max_attempts
        self.reset_after_s = reset_after_s
        self.attempts = 0
        self._last_eos_at: float | None = None

    def on_eos(self, now: float) -> int:
        """Anota o EOS e devolve o número da tentativa (1-based).

        Quem chama decide se a tentativa conta (`commit`) — o pipeline não
        conta o EOS que chegou com uma reconexão já em curso.
        """
        self._last_eos_at = now
        return self.attempts + 1

    def commit(self, attempt: int) -> None:
        """Efetiva a tentativa que o pipeline de fato executou."""
        self.attempts = max(self.attempts, attempt)

    def exhausted(self, attempt: int) -> bool:
        """True quando a tentativa estourou o teto (fonte declarada perdida)."""
        return attempt > self.max_attempts

    def on_frame(self, now: float) -> bool:
        """Registra frame recebido; True quando o orçamento foi devolvido.

        Devolver o orçamento exige `reset_after_s` sem EOS. Nunca zera a
        contagem por um único frame.
        """
        if not self.attempts:
            return False
        if self._last_eos_at is not None and \
                now - self._last_eos_at < self.reset_after_s:
            return False
        self.attempts = 0
        self._last_eos_at = None
        return True
