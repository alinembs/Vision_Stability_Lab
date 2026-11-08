"""Encerramento limpo do pipeline por `SIGTERM`/`SIGINT` (E2-T3, F3).

`docker stop` manda `SIGTERM` e só depois da carência (10 s por default) manda
`SIGKILL`. O pipeline respondia a **nenhum** dos dois: `docker events` mostrava
`kill` -> `kill` 10 s depois -> `stop/die` em toda recriação, `ExitCode` 143 e
nenhum `pipeline_stopped` na telemetria — o docstring do módulo prometia
"0 = encerramento limpo" e o `docker stop` nunca chegava lá.

Por que um handler de `signal.signal` não resolve: o thread principal do
pipeline fica bloqueado dentro de `GMainLoop.run()` (código C, esperando
evento). O handler do CPython só executa quando o thread principal volta a
rodar bytecode, o que não acontece enquanto o loop está parado — o handler fica
enfileirado e o processo morre por SIGKILL na carência.

Solução: os sinais entram pelo próprio GLib. `GLib.unix_signal_add` transforma
o sinal em fonte do contexto padrão, e a callback roda **no thread do loop**:
aí `loop.quit()` e, no bloco de saída do pipeline, `pipeline.set_state(NULL)`
são seguros — nada de GStreamer é chamado do handler de sinal.

Sem `unix_signal_add` (PyGObject antigo), o fallback instala `signal.signal` e
apenas **enfileira** a parada com `GLib.idle_add`: o efeito depende de o loop
voltar a iterar, o que é o melhor possível sem a integração do GLib.
"""

from __future__ import annotations

import signal
from typing import Any, Callable, List, Optional, Sequence, Tuple

# Sinais que o Docker usa (`docker stop`) e que o operador usa (Ctrl+C).
DEFAULT_SIGNALS: Tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)


def signal_name(signum: Optional[int]) -> str:
    """Nome legível do sinal (`SIGTERM`); número cru se não for conhecido."""
    try:
        return signal.Signals(int(signum)).name
    except (TypeError, ValueError):
        return str(signum)


class GracefulShutdown:
    """Pede parada limpa ao loop GLib quando o processo recebe `SIGTERM`/`SIGINT`.

    `request_stop(signum)` é chamado **uma vez**, no thread do loop; o chamador
    (o pipeline) registra o evento e chama `loop.quit()`. O `exit_code` do
    processo continua sendo o do pipeline: um encerramento pedido por sinal não
    sobrescreve um erro já registrado (`EXIT_PIPELINE_ERROR`/`EXIT_SOURCE_LOST`).
    """

    def __init__(self, glib: Any, request_stop: Callable[[Optional[int]], None],
                 signals: Sequence[int] = DEFAULT_SIGNALS) -> None:
        self._glib = glib
        self._request_stop = request_stop
        self._signals: Tuple[int, ...] = tuple(signals)
        self._mode = "none"
        self._sources: List[int] = []
        self._handled = False
        self.received_signal: Optional[str] = None

    # -- instalação ---------------------------------------------------------
    def install(self) -> List[int]:
        """Registra os sinais; devolve os source ids do GLib (vazio no fallback)."""
        unix_signal_add = getattr(self._glib, "unix_signal_add", None)
        if callable(unix_signal_add):
            self._mode = "glib"
            for signum in self._signals:
                source = unix_signal_add(self._glib.PRIORITY_HIGH, signum,
                                         self._on_signal, signum)
                if source:
                    self._sources.append(source)
            return self.sources

        self._mode = "idle"
        for signum in self._signals:
            signal.signal(signum, self._defer_shutdown)
        return self.sources

    @property
    def mode(self) -> str:
        """`glib` (`unix_signal_add`), `idle` (fallback) ou `none`."""
        return self._mode

    @property
    def sources(self) -> List[int]:
        return list(self._sources)

    @property
    def handled(self) -> bool:
        return self._handled

    # -- callbacks ----------------------------------------------------------
    def _on_signal(self, signum: Optional[int] = None) -> bool:
        """Callback do sinal no thread do loop; devolve `True` (fonte viva).

        A fonte fica viva de propósito: se o encerramento travar (por exemplo
        `set_state(NULL)` esperando a thread de streaming), um segundo
        `SIGTERM` não é engolido por um handler já destruído — ele volta a
        acordar o loop para que o Docker não precise chegar ao `SIGKILL` sem
        ninguém ter tentado nada.

        Só o **primeiro** sinal pede parada: repetir `request_stop` reentraria
        em `loop.quit()`/telemetria no meio do desligamento.
        """
        if self._handled:
            return True
        self._handled = True
        self.received_signal = signal_name(signum)
        self._request_stop(signum)
        return True

    def _deferred(self, signum: Optional[int] = None) -> bool:
        """Ponte `SIGTERM` do CPython -> loop: `idle_add` exige retorno False."""
        self._on_signal(signum)
        return False

    def _defer_shutdown(self, signum: Optional[int] = None,
                        frame: Any = None) -> None:
        """Handler do CPython no fallback: apenas enfileira a parada.

        Nada de GStreamer nem de `loop.quit()` aqui: o handler roda em contexto
        de sinal. `idle_add` agenda o trabalho para o loop.
        """
        self._glib.idle_add(self._deferred, signum)
