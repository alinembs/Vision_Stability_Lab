"""Máquina de estados pura do ramo de preview (derrubada e retomada).

Motivação (2026-11-01, incidente em campo): com o MediaMTX fora do ar o ramo
de preview falha no boot e o pipeline **congela** — não publica mais
`interval_metrics` e não responde a `SIGTERM` (o `docker restart` do watchdog
vira `SIGKILL`, sem `pipeline_stopped`). O thread dump do processo congelado
mostra o thread do loop GLib preso dentro do callback do bus:

    Thread 1 (GLib/Python): g_main_loop_run -> g_main_dispatch ->
      <emit message:error on GstBus> -> gst_element_change_state ->
      libgstrtspclientsink.so -> pthread_mutex_lock   (parado para sempre)

O caminho era `_on_bus_message` -> `_drop_preview_branch` ->
`_detach_preview_branch` -> `set_state(NULL)` no `rtspclientsink` **dentro do
callback**. O sink que falhou em conectar (`Failed to connect`) não solta o
mutex que o `change_state` precisa. Com o loop parado, os timers (métricas,
`_check_rtsp_alive`) e o handler de sinal param junto.

A regra que sobra é a mesma do `source_reconnect.py` (INC-006): **nenhum
trabalho de GStreamer no callback do bus**. Este módulo decide o que fazer em
cada fatia — sem `gi`/`pyds`, testável sem GPU — e o pipeline executa:

* derrubar o ramo (`unlink`, `release_request_pad`, `set_state(NULL)`,
  `remove`) numa thread própria;
* recriar e religar o ramo na mesma thread;
* observar o resultado no timer, com teto (`op_timeout_s`). Operação que não
  retorna marca o preview como indisponível e **a análise segue** — o pior
  caso vira "sem preview", nunca "pipeline congelado".

O `rtspclientsink` que falhou não volta a publicar num ciclo NULL -> PLAYING
(a thread de sessão dele morre junto com o erro), por isso a retomada recria o
ramo inteiro, e a retomada só é dada como boa quando frames de preview de fato
chegam (`verify`).
"""

from __future__ import annotations

from typing import Optional

# Ações devolvidas por `step` (quem executa é o pipeline).
ACTION_NONE = "none"
ACTION_DETACH = "detach"              # derrubar o ramo (thread própria)
ACTION_INSTALL = "install"            # recriar/religar o ramo (thread própria)
ACTION_ARMED = "armed"                # derrubado; retomada agendada
ACTION_RETRIED = "retried"            # religado; aguardando frame de prova
ACTION_RETRY_FAILED = "retry_failed"  # tentativa de religar falhou
ACTION_GIVE_UP = "give_up"            # preview só volta com reinício

# Desfechos de `verify`.
VERIFY_RESTORED = "restored"
VERIFY_FAILED = "verify_failed"

# Operações em voo (para o teto de tempo e para o diagnóstico).
OP_DETACH = "detach"
OP_INSTALL = "install"

# Motivos de desistência (`give_up_reason`).
GIVE_UP_OP_TIMEOUT = "op_timeout"
GIVE_UP_ATTEMPTS = "attempts"

# Tipos de pedido de derrubada devolvidos por `request_drop`.
DROP_FIRST = "failed"
DROP_RETRY = "retry_failed"
DROP_IGNORED = "ignored"


class PreviewBranchController:
    """Decide o próximo passo do ramo de preview, sem tocar em GStreamer."""

    def __init__(self, retry_base_s: float = 15.0,
                 retry_max_s: float = 300.0,
                 verify_s: float = 20.0,
                 verify_min_frames: int = 5,
                 max_attempts: int = 6,
                 op_timeout_s: float = 5.0) -> None:
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.verify_s = verify_s
        self.verify_min_frames = verify_min_frames
        self.max_attempts = max_attempts
        self.op_timeout_s = op_timeout_s

        self._failed = False
        self._pending = False
        self._exhausted = False
        self._attempts = 0
        self._drop_requested = False
        self._retry_at = 0.0
        self._frames_at_retry = 0
        self._op: Optional[str] = None
        self._op_started_at = 0.0
        self.last_error: Optional[str] = None
        self.last_frames_since_retry = 0
        self.last_elapsed_s = 0.0
        self.give_up_reason = ""

    # -- leitura -------------------------------------------------------------
    @property
    def failed(self) -> bool:
        """True enquanto o ramo estiver derrubado (ou em retomada)."""
        return self._failed

    @property
    def pending(self) -> bool:
        """True entre religar e a prova de que frames voltaram."""
        return self._pending

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def op(self) -> Optional[str]:
        """Operação de GStreamer em voo (`None` se não há)."""
        return self._op

    @property
    def frames_at_retry(self) -> int:
        return self._frames_at_retry

    @property
    def verify_elapsed(self) -> float:
        return self._retry_at

    def delay_s(self, now: float) -> float:
        """Espera já agendada, em segundos (para log e diagnóstico)."""
        return max(0.0, self._retry_at - now)

    # -- entradas (thread do loop GLib: não bloqueiam) -----------------------
    def request_drop(self) -> str:
        """Registra falha do ramo; a derrubada em si acontece em `step`.

        Chamado do callback do bus, que roda no thread do loop GLib — por
        isso **não** toca em GStreamer nem espera nada.

        Returns:
            `DROP_FIRST` na primeira falha, `DROP_RETRY` quando o ramo já
            estava derrubado (tentativa de retomada também caiu) ou
            `DROP_IGNORED` quando não há preview habilitado.
        """
        if self._exhausted:
            return DROP_IGNORED
        if self._failed:
            self._attempts += 1
            kind = DROP_RETRY
        else:
            self._failed = True
            kind = DROP_FIRST
        self._pending = False
        self._drop_requested = True
        return kind

    def step(self, now: float, op_done: bool = False,
             frames_total: int = 0,
             op_error: Optional[str] = None) -> str:
        """Executa uma fatia: decide o que o pipeline deve fazer agora.

        Args:
            now: instante (`time.monotonic()`).
            op_done: a operação em voo terminou (o pipeline observa a thread).
            frames_total: contador acumulado de frames de preview; guardado
                como base da prova de retomada.
            op_error: erro da operação que terminou, se houve.
        """
        if self._exhausted:
            return ACTION_NONE

        if self._op is not None:
            if not op_done:
                if now - self._op_started_at >= self.op_timeout_s:
                    # Operação de GStreamer não retornou: o preview está
                    # perdido, mas o loop (métricas, sinais) segue vivo.
                    self.give_up_reason = GIVE_UP_OP_TIMEOUT
                    self._exhausted = True
                    self._op = None
                    self._drop_requested = False
                    self._pending = False
                    return ACTION_GIVE_UP
                return ACTION_NONE
            op, self._op = self._op, None
            if op == OP_DETACH:
                if self._arm(now):
                    return ACTION_ARMED
                return ACTION_GIVE_UP
            # OP_INSTALL
            if op_error is None:
                self._pending = True
                self._frames_at_retry = frames_total
                self._retry_at = now
                return ACTION_RETRIED
            self._attempts += 1
            self.last_error = op_error
            self._drop_requested = True
            return ACTION_RETRY_FAILED

        if self._drop_requested:
            self._drop_requested = False
            self._op = OP_DETACH
            self._op_started_at = now
            return ACTION_DETACH

        if (self._failed and not self._pending
                and now >= self._retry_at):
            self._op = OP_INSTALL
            self._op_started_at = now
            return ACTION_INSTALL
        return ACTION_NONE

    def verify(self, now: float, frames_total: int = 0) -> Optional[str]:
        """Confere se a retomada de fato voltou a produzir frames.

        Religar os elementos e receber dois buffers não prova que o sink
        voltou a publicar: a prova é `frames_total` crescer depois do
        `retry`. Sem frame dentro de `verify_s`, a tentativa conta como falha.
        """
        if not self._pending:
            return None
        frames = frames_total - self._frames_at_retry
        elapsed = now - self._retry_at
        self.last_frames_since_retry = frames
        self.last_elapsed_s = elapsed
        if frames >= self.verify_min_frames:
            self._failed = False
            self._pending = False
            self._attempts = 0
            return VERIFY_RESTORED
        if now - self._retry_at >= self.verify_s:
            self._pending = False
            self._attempts += 1
            self.last_error = "sem frame de preview após religar"
            self._drop_requested = True
            return VERIFY_FAILED
        return None

    # -- interno -------------------------------------------------------------
    def _arm(self, now: float) -> bool:
        """Agenda a próxima tentativa; `False` se o orçamento acabou."""
        self._pending = False
        if self._attempts >= self.max_attempts:
            self.give_up_reason = GIVE_UP_ATTEMPTS
            self._exhausted = True
            self._retry_at = now
            return False
        delay = min(self.retry_base_s * (2 ** min(self._attempts, 5)),
                    self.retry_max_s)
        self._retry_at = now + delay
        return True
