"""Máquina de estados pura da reconexão da fonte (`rtspsrc`).

Motivação (E2, 2026-10-30): com a fonte perdida o pipeline **congelava**. O
callback do bus chamava `rtspsrc.set_state(NULL)` e
`pad.send_event(flush_stop)` no **thread do loop GLib**; o `flush_stop` é um
evento serializado e espera o stream lock do pad que a thread de streaming do
`nvstreammux` mantém enquanto espera um batch. O thread dump do processo
congelado mostra exatamente isso:

    Thread 1 (GLib/Python): gst_pad_send_event -> gst_pad_event_default ->
      gst_pad_forward -> gst_pad_push_event (x6) -> pthread_mutex_lock
    dono do mutex: LWP `nvstreammux0:sr`, parado em `g_cond_wait` dentro de
      libnvdsgst_multistream.so

Consequência: o loop parava de despachar **tudo** — timer de métricas (30 s),
`_check_rtsp_alive` (5 s) e o próprio handler de `SIGTERM` —, então o
`docker stop` só terminava por `SIGKILL` (10 s, exit 137) e a reconexão
anunciada nunca acontecia.

A regra que sobrou: **nenhum trabalho de GStreamer no callback do bus**. Ele só
decide e registra a falha; o trabalho é feito em fatias curtas por um timer
GLib (`pipeline_real._reconnect_step`), cada fatia com teto de tempo. Esta
máquina decide o que cada fatia faz, sem `gi`/`pyds` — por isso dá para
exercitá-la em `pytest` (agendamento, tentativas e desfecho) sem GPU.

Desfecho garantido: a reconexão termina numa de duas saídas — fonte de volta
(`STEP_RESUMED`) ou orçamento esgotado (`STEP_GIVE_UP`, que o pipeline traduz
em `EXIT_SOURCE_LOST`=2). Nunca fica pendurada: cada fase tem `deadline`.
"""

from __future__ import annotations

from typing import Optional

# Fases. `PHASE_EXHAUSTED` é terminal: só resta dar o desfecho (exit 2).
PHASE_IDLE = "idle"
PHASE_STOPPING = "stopping"      # rtspsrc -> NULL pedido, aguardando NULL
PHASE_WAITING = "waiting"        # rtspsrc -> PLAYING pedido, aguardando frame
PHASE_BACKOFF = "backoff"        # tentativa falhou; nova tentativa agendada
PHASE_EXHAUSTED = "exhausted"

# Fatias. Quem executa é o pipeline; aqui só se decide.
STEP_NONE = "none"
STEP_STOP = "stop_source"        # set_state(NULL) no rtspsrc
STEP_START = "start_source"      # set_state(PLAYING) no rtspsrc
STEP_RESUMED = "resumed"         # frame voltou: reconexão bem-sucedida
STEP_RETRY = "retry"             # tentativa falhou, próxima agendada
STEP_GIVE_UP = "give_up"         # orçamento esgotado: fonte perdida (exit 2)


class SourceReconnectMachine:
    """Decide o próximo passo da reconexão da fonte, sem tocar em GStreamer.

    O orçamento (`SourceReconnectBudget`) continua sendo quem conta tentativas
    e devolve o crédito depois de uma janela estável; esta máquina só o
    consulta. "Falha da fonte" aqui é EOS, erro do `rtspsrc` **ou** ausência de
    frame depois de mandar PLAYING — os três gastam tentativa, porque os três
    significam a mesma coisa na prática: a fonte não está entregando vídeo.
    """

    def __init__(self, budget, stop_timeout_s: float = 3.0,
                 frame_timeout_s: float = 15.0,
                 retry_base_s: float = 10.0,
                 retry_max_s: float = 45.0) -> None:
        self._budget = budget
        self.stop_timeout_s = stop_timeout_s
        self.frame_timeout_s = frame_timeout_s
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.phase = PHASE_IDLE
        self.attempt = 0
        self.reason = ""
        # Motivo de um caminho excepcional da última fatia (ex.:
        # `stop_timeout`); quem chama loga e zera. Mantém esta classe sem log.
        self.note = ""
        self._deadline = 0.0
        self._frames_at_start = 0
        self._stop_issued = False

    # -- leitura -------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True enquanto há reconexão em curso (para cooldown e idle check)."""
        return self.phase in (PHASE_STOPPING, PHASE_WAITING, PHASE_BACKOFF)

    @property
    def attempts(self) -> int:
        return self._budget.attempts

    @property
    def max_attempts(self) -> int:
        return self._budget.max_attempts

    @property
    def exhausted(self) -> bool:
        return self.phase == PHASE_EXHAUSTED

    # -- entradas (thread do loop GLib: não bloqueiam) -----------------------
    def begin(self, now: float, reason: str) -> Optional[int]:
        """Pede a primeira tentativa de reconexão.

        Returns:
            Número da tentativa (1-based) se o ciclo começou; `None` se já
            havia uma reconexão em curso ou se o orçamento esgotou (nesse caso
            `phase` já é `PHASE_EXHAUSTED` e o desfecho é `STEP_GIVE_UP`).
        """
        if self.phase == PHASE_EXHAUSTED:
            return None
        if self.phase != PHASE_IDLE:
            return None
        return self._next_attempt(now, reason, PHASE_STOPPING)

    def fail(self, now: float, reason: str) -> Optional[int]:
        """Registra falha da fonte (erro do `rtspsrc`) e agenda a próxima.

        Só conta tentativa quando há uma de fato **em andamento** (aguardando
        frame). Erro que chega durante a parada/backoff é resíduo do ciclo em
        curso: o pipeline já está reconectando e não pode queimar orçamento
        por um erro atrasado do `rtspsrc`.

        Returns:
            Número da nova tentativa, ou `None` se nada foi contado (ciclo já
            em curso) ou se o orçamento esgotou.
        """
        if self.phase in (PHASE_EXHAUSTED, PHASE_STOPPING, PHASE_BACKOFF):
            return None
        if self.phase == PHASE_IDLE:
            return self.begin(now, reason)
        return self._next_attempt(now, reason, PHASE_BACKOFF)

    def step(self, now: float, stopped: bool, frames_total: int) -> str:
        """Executa uma fatia do ciclo e devolve o que o pipeline deve fazer.

        Args:
            now: instante (`time.monotonic()`).
            stopped: `rtspsrc.get_state(0)` já confirmou `NULL` (consulta sem
                espera: é o pipeline quem sonda, aqui só se usa o resultado).
            frames_total: contador acumulado de frames de entrada; crescer
                depois do `PLAYING` é a prova de que a fonte voltou.

        Returns:
            Uma das constantes `STEP_*`.
        """
        if self.phase == PHASE_EXHAUSTED:
            return STEP_GIVE_UP

        if self.phase == PHASE_STOPPING:
            if not self._stop_issued:
                self._stop_issued = True
                return STEP_STOP
            if stopped:
                return self._start(now, frames_total)
            if now >= self._deadline:
                # NULL não confirmou no teto: segue para PLAYING de qualquer
                # forma (o GStreamer serializa a transição) — o que não pode é
                # a fatia esperar para sempre.
                self.note = "stop_timeout"
                return self._start(now, frames_total)
            return STEP_NONE

        if self.phase == PHASE_WAITING:
            if frames_total > self._frames_at_start:
                self.phase = PHASE_IDLE
                return STEP_RESUMED
            if now < self._deadline:
                return STEP_NONE
            if self.fail(now, "no_frames_after_reconnect") is None:
                return STEP_GIVE_UP
            return STEP_RETRY

        if self.phase == PHASE_BACKOFF:
            if now < self._deadline:
                return STEP_NONE
            self.phase = PHASE_STOPPING
            self._stop_issued = False
            self._deadline = now + self.stop_timeout_s
            return STEP_NONE

        return STEP_NONE

    # -- interno -------------------------------------------------------------
    def _retry_delay(self) -> float:
        """Espera antes da próxima tentativa: exponencial, com teto.

        Mesmo padrão do ramo de preview (`PREVIEW_RETRY_S`/`PREVIEW_RETRY_MAX_S`).
        A primeira tentativa é imediata; a espera cresce com o número da
        tentativa (10, 20, 40, 45 s com o default) para a fonte ter tempo de
        voltar sem o pipeline declarar perda cedo demais — com 5 tentativas o
        processo fica ~2 min reconectando e **publicando métricas** antes do
        `exit 2` (é o caminho que antes congelava o loop e não publicava nada).
        """
        if self.attempt <= 1:
            return 0.0
        return min(self.retry_base_s * (2 ** (self.attempt - 2)),
                   self.retry_max_s)

    def _start(self, now: float, frames_total: int) -> str:
        self.phase = PHASE_WAITING
        self._deadline = now + self.frame_timeout_s
        self._frames_at_start = frames_total
        return STEP_START

    def _next_attempt(self, now: float, reason: str,
                      phase_after: str) -> Optional[int]:
        attempt = self._budget.on_eos(now)
        self.attempt = attempt
        self.reason = reason
        if self._budget.exhausted(attempt):
            self.phase = PHASE_EXHAUSTED
            return None
        self._budget.commit(attempt)
        self.phase = phase_after
        self._stop_issued = False
        self._deadline = now + (self._retry_delay()
                               if phase_after == PHASE_BACKOFF
                               else self.stop_timeout_s)
        return attempt
