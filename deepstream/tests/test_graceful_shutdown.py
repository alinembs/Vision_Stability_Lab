"""Testes do encerramento limpo por sinal (`deepstream/graceful_shutdown.py`).

Contexto (F3 do E2-T3): `docker events` mostrava `kill` -> `kill` 10 s depois ->
`stop/die` em toda recriação do container. Não havia handler de `SIGTERM` e,
mesmo com `signal.signal`, o handler do CPython não roda com o thread principal
bloqueado dentro de `GMainLoop.run()` — o Docker chegava ao `SIGKILL` na
carência (exit 143, sem `pipeline_stopped` na telemetria).

O que estes testes protegem:

1. os dois sinais que importam (`SIGTERM` do `docker stop`, `SIGINT` do Ctrl+C)
   são registrados, e pelo caminho que funciona com o loop bloqueado
   (`GLib.unix_signal_add`, cuja callback roda no thread do loop);
2. a parada é pedida **uma vez** — repetir `loop.quit()`/telemetria no meio do
   desligamento seria reentrância;
3. nada de GStreamer no handler: o fallback (`signal.signal`) só enfileira a
   parada com `GLib.idle_add`.

Sem `gi`/`pyds`: o GLib é um duplo (`FakeGLib`) e o handler do CPython é
capturado com `mock.patch` em `graceful_shutdown.signal.signal`.
"""

from __future__ import annotations

import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graceful_shutdown as gs  # noqa: E402


class FakeGLib:
    """Duplo mínimo do módulo `GLib` (só o que o handler usa)."""

    PRIORITY_HIGH = -100
    SOURCE_CONTINUE = True
    SOURCE_REMOVE = False

    def __init__(self, with_unix_signal_add: bool = True) -> None:
        self.sources: list[tuple[int, int]] = []   # (source_id, signum)
        self.idle: list[tuple] = []
        self._next_id = 1
        if not with_unix_signal_add:
            # Simula PyGObject sem integração de sinal: o atributo não existe.
            self.unix_signal_add = None

    def unix_signal_add(self, priority, signum, handler, *user_data):  # noqa: D401
        source_id = self._next_id
        self._next_id += 1
        self.sources.append((source_id, signum))
        self.handlers = getattr(self, "handlers", {})
        self.handlers[signum] = (handler, user_data)
        return source_id

    def idle_add(self, callback, *user_data):
        self.idle.append((callback, user_data))
        return len(self.idle)

    def fire(self, signum: int) -> bool:
        """Entrega o sinal como o GLib entregaria (no thread do loop)."""
        handler, user_data = self.handlers[signum]
        return handler(*user_data)


class UnixSignalAddTest(unittest.TestCase):
    def setUp(self) -> None:
        self.glib = FakeGLib()
        self.requests: list = []
        self.shutdown = gs.GracefulShutdown(
            self.glib, self.requests.append)

    def test_registra_sigterm_e_sigint_no_glib(self) -> None:
        """`SIGTERM` é o `docker stop`; `SIGINT` é o Ctrl+C do operador."""
        sources = self.shutdown.install()
        self.assertEqual(self.shutdown.mode, "glib")
        self.assertEqual(len(sources), 2)
        self.assertEqual([signum for _id, signum in self.glib.sources],
                         [signal.SIGTERM, signal.SIGINT])

    def test_sigterm_pede_parada_uma_vez(self) -> None:
        self.shutdown.install()
        self.assertTrue(self.glib.fire(signal.SIGTERM))
        self.assertEqual(self.requests, [signal.SIGTERM])
        self.assertEqual(self.shutdown.received_signal, "SIGTERM")
        self.assertTrue(self.shutdown.handled)

    def test_segundo_sinal_nao_repete_a_parada(self) -> None:
        """A fonte do sinal fica viva, mas a parada é idempotente."""
        self.shutdown.install()
        self.glib.fire(signal.SIGTERM)
        self.glib.fire(signal.SIGTERM)
        self.glib.fire(signal.SIGINT)
        self.assertEqual(self.requests, [signal.SIGTERM])
        self.assertEqual(self.shutdown.received_signal, "SIGTERM")

    def test_callback_do_sinal_nao_chama_gstreamer(self) -> None:
        """A parada é `loop.quit()`; `set_state(NULL)` fica no `finally` do run."""
        import ast

        arvore = ast.parse(Path(gs.__file__).read_text(encoding="utf-8"))
        funcoes = {no.name: no for no in ast.walk(arvore)
                   if isinstance(no, ast.FunctionDef)}
        usados: set[str] = set()
        for nome in ("_on_signal", "_defer_shutdown", "_deferred"):
            for no in ast.walk(funcoes[nome]):
                if isinstance(no, ast.Attribute):
                    usados.add(no.attr)
                elif isinstance(no, ast.Name):
                    usados.add(no.id)
        for proibido in ("set_state", "set_state_recursive", "Gst",
                         "pipeline", "send_event"):
            self.assertNotIn(
                proibido, usados,
                f"o handler de sinal usa {proibido} — não é seguro fora do "
                "thread/contexto do loop")


class FallbackIdleTest(unittest.TestCase):
    """Sem `unix_signal_add`, o handler do CPython só enfileira a parada."""

    def setUp(self) -> None:
        self.glib = FakeGLib(with_unix_signal_add=False)
        self.requests: list = []
        self.shutdown = gs.GracefulShutdown(self.glib, self.requests.append)
        patcher = mock.patch.object(gs.signal, "signal")
        self.signal_signal = patcher.start()
        self.addCleanup(patcher.stop)

    def installed_handlers(self) -> dict:
        return {chamada.args[0]: chamada.args[1]
                for chamada in self.signal_signal.call_args_list}

    def test_instala_signal_signal_para_os_dois_sinais(self) -> None:
        self.shutdown.install()
        self.assertEqual(self.shutdown.mode, "idle")
        self.assertEqual(
            sorted(chamada.args[0]
                   for chamada in self.signal_signal.call_args_list),
            sorted([signal.SIGTERM, signal.SIGINT]))

    def test_handler_apenas_enfileira_a_parada(self) -> None:
        self.shutdown.install()
        self.installed_handlers()[signal.SIGTERM](signal.SIGTERM, None)
        # Nada pedido ainda: só a fonte idle foi agendada.
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.glib.idle), 1)

        callback, user_data = self.glib.idle[0]
        self.assertFalse(callback(*user_data),
                         "idle_add com retorno True viraria loop de CPU")
        self.assertEqual(self.requests, [signal.SIGTERM])


class SignalNameTest(unittest.TestCase):
    def test_nomes(self) -> None:
        self.assertEqual(gs.signal_name(signal.SIGTERM), "SIGTERM")
        self.assertEqual(gs.signal_name(signal.SIGINT), "SIGINT")

    def test_numero_desconhecido_nao_levanta(self) -> None:
        self.assertEqual(gs.signal_name(9999), "9999")
        self.assertEqual(gs.signal_name(None), "None")


if __name__ == "__main__":
    unittest.main()
