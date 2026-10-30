"""Guarda da fiação do pipeline com a fonte ativa (`pipeline_real.py`, E2-T2).

`pipeline_real.py` não pode ser importado aqui: ele importa `gi`/`pyds` e
instancia TensorRT. O contrato que importa — "o pipeline consome a fonte
resolvida por `sources.py` e não lê mais `RTSP_CODEC` do ambiente" — é
verificável no texto do arquivo, e é o que impede a regressão silenciosa de D5
(codec por fonte) quando alguém "consertar" o depay por `os.environ`.

Isso é o caso de integração leve previsto no plano: o caminho de mídia real
(frames, pad, decoder) é validado pelo gate `deepstream-tester` no container
único do projeto, não por `pytest`.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PIPELINE = RAIZ / "deepstream" / "pipeline_real.py"


class PipelineConsomeFonteAtivaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codigo = PIPELINE.read_text(encoding="utf-8")

    def test_fonte_ativa_e_resolvida_no_boot(self) -> None:
        self.assertTrue(
            "resolve_active_source(load_sources())" in self.codigo,
            "pipeline_real.py não resolve a fonte ativa no __init__")

    def test_uri_e_codec_vem_da_fonte(self) -> None:
        self.assertTrue("self.rtsp_uri = self.source.uri" in self.codigo)
        self.assertTrue("self.codec = self.source.codec" in self.codigo)

    def test_depay_e_escolhido_pelo_codec_da_fonte(self) -> None:
        self.assertTrue("depay_factory(self.codec)" in self.codigo,
                        "depay/parser não vêm de depay_factory()")
        self.assertIsNone(
            re.search(r'"rtph26[45]depay"', self.codigo),
            "factory de depay hardcoded voltou ao pipeline")

    def test_a_cadeia_de_midia_vem_das_fabricas_da_fonte(self) -> None:
        """I5 (revisão): `self._make(...)` com literal passava no teste antigo.

        As duas linhas são comparadas **literais**: `self.depay = self._make(
        "rtph264depay")` (ou qualquer fábrica fixa) é mídia quebrada para
        metade das fontes e o teste anterior ficava verde — a asserção de
        `depay_factory(self.codec)` só provava que a chamada existia em algum
        lugar do arquivo.
        """
        self.assertIn("self.depay = self._make(self.depay_factory)",
                      self.codigo,
                      "depay não é criado a partir da fábrica da fonte")
        self.assertIn("self.parser = self._make(self.parser_factory)",
                      self.codigo,
                      "parser não é criado a partir da fábrica da fonte")

    def test_nenhuma_leitura_de_rtsp_codec_do_ambiente(self) -> None:
        """D5 (I5): codec é da fonte; o ambiente não pode reintroduzir o global.

        Cobre as três formas de leitura por item/chave — a mutação do revisor
        (`self.codec = os.environ['RTSP_CODEC']`) passava no teste anterior, que
        só olhava `environ.get("RTSP_CODEC")`.
        """
        leituras = re.findall(
            r"(?:os\.)?(?:environ(?:\.get)?\s*[\(\[]|getenv\s*\()"
            r"[^)\]\n]*RTSP_CODEC", self.codigo)
        self.assertEqual(
            leituras, [],
            f"pipeline_real.py voltou a ler RTSP_CODEC do ambiente: {leituras}")
        self.assertIn("self.codec = self.source.codec", self.codigo)

    def test_rtspsrc_usa_uri_e_parametros_da_fonte(self) -> None:
        self.assertTrue("location=self.rtsp_uri" in self.codigo)
        self.assertTrue("latency=self.source.latency_ms" in self.codigo)
        self.assertTrue("protocols=self.source.protocols" in self.codigo)

    def test_pipeline_nao_le_mais_rtsp_codec(self) -> None:
        self.assertIsNone(
            re.search(r'environ\.get\(\s*"RTSP_CODEC"', self.codigo),
            "pipeline_real.py voltou a ler RTSP_CODEC: o codec é da fonte (D5)")
        # Nenhum item access/`getenv` do codec legado (assert acima depende da
        # forma exata da chamada; aqui a busca é pela leitura em si).
        self.assertIsNone(
            re.search(r'RTSP_CODEC["\']\s*\]', self.codigo),
            "pipeline_real.py lê RTSP_CODEC por item access: codec é da fonte (D5)")

    def test_telemetria_identifica_a_fonte_e_a_sessao(self) -> None:
        for campo in ("source_id=", "source_name=", "source_target_fps=",
                      "session_epoch=", "last_frame_age_s="):
            self.assertIn(campo, self.codigo,
                          f"interval_metrics sem {campo.rstrip('=')}")

    def test_evento_source_active_mascara_a_credencial(self) -> None:
        self.assertIn('log_event("source_active"', self.codigo)
        self.assertIn("uri=mask_uri(self.rtsp_uri)", self.codigo)
        # Nenhuma URI pode ir para o log sem passar por mask_uri():
        self.assertIsNone(
            re.search(r'log_event\([^)]*(?<!mask_uri\()self\.rtsp_uri',
                      self.codigo),
            "rtsp_uri logado sem mask_uri() — risco de vazar credencial")

    def test_batch_size_continua_um(self) -> None:
        """D1: uma fonte ativa por vez, sem tiler."""
        self.assertIn("batch_size=1", self.codigo)
        self.assertIn('"sink_0"', self.codigo)


class ReconexaoDaFonteTest(unittest.TestCase):
    """E2: a reconexão não pode segurar o loop GLib (bug do congelamento).

    Medido em 2026-10-30 com a fonte perdida: o callback do bus chamava
    `set_state(NULL)` + `pad.send_event(flush_stop)` e o loop parava de
    despachar **tudo** por ≥ 49 s (thread dump: `gst_pad_send_event` ->
    `gst_pad_push_event` -> `pthread_mutex_lock` num stream lock preso na
    thread do `nvstreammux`). Resultado: `docker stop` em 10,5 s -> exit 137,
    zero `pipeline_stopped`, zero reconexão. Os testes abaixo prendem a regra:
    decisão no callback, execução em fatias de timer, desfecho obrigatório.
    """

    def setUp(self) -> None:
        self.codigo = PIPELINE.read_text(encoding="utf-8")

    def _corpo(self, nome: str) -> str:
        arvore = ast.parse(self.codigo)
        for no in ast.walk(arvore):
            if isinstance(no, ast.FunctionDef) and no.name == nome:
                corpo = no.body
                if (corpo and isinstance(corpo[0], ast.Expr)
                        and isinstance(corpo[0].value, ast.Constant)
                        and isinstance(corpo[0].value.value, str)):
                    corpo = corpo[1:]
                return "\n".join(
                    ast.get_source_segment(self.codigo, stmt) or ""
                    for stmt in corpo)
        raise AssertionError(f"método {nome} não encontrado em pipeline_real.py")

    def test_callback_do_bus_nao_chama_gstreamer(self) -> None:
        """Só decide e loga; nada de `set_state`/`send_event` no callback."""
        corpo = self._corpo("_on_source_failure")
        for proibido in ("set_state", "send_event", "get_state", "Gst."):
            self.assertNotIn(proibido, corpo,
                             f"`_on_source_failure` chama {proibido} — é o "
                             "caminho que congelava o loop GLib")

    def test_nenhum_evento_de_flush_no_pipeline(self) -> None:
        """O flush que travava: `send_event` no callback esperava stream lock.

        Um `flush_start`/`flush_stop` enviado de fora da thread de streaming
        espera o stream lock do pad; o `nvstreammux` o mantém enquanto espera
        batch, com a fonte morta. Não volta. (Checagem no AST: comentário e
        docstring citam o defeito de propósito.)
        """
        chamadas = set()
        for no in ast.walk(ast.parse(self.codigo)):
            if isinstance(no, ast.Call):
                if isinstance(no.func, ast.Attribute):
                    chamadas.add(no.func.attr)
                elif isinstance(no.func, ast.Name):
                    chamadas.add(no.func.id)
        for proibida in ("send_event", "new_flush_start", "new_flush_stop"):
            self.assertNotIn(proibida, chamadas)

    def test_reconexao_e_agendada_por_timer_persistente(self) -> None:
        """Um `timeout_add` reagendado a cada passo podia se perder."""
        self.assertIn("GLib.timeout_add(SOURCE_RECONNECT_STEP_MS, "
                      "self._reconnect_step)", self.codigo)
        self.assertIn("SOURCE_RECONNECT_STEP_MS", self.codigo)
        # a fatia sempre devolve True: o timer não morre
        self.assertIn("return True", self._corpo("_reconnect_step"))

    def test_get_state_sempre_com_timeout(self) -> None:
        """`get_state` sem timeout é a mesma espera sem teto que o bug."""
        self.assertIn("get_state(0)", self._corpo("_source_is_stopped"))
        self.assertIsNone(
            re.search(r"get_state\(\s*Gst\.CLOCK_TIME_NONE", self.codigo),
            "get_state com CLOCK_TIME_NONE (espera infinita) de volta")

    def test_set_state_do_rtspsrc_roda_fora_do_loop(self) -> None:
        """`set_state` espera a transição (CLOCK_TIME_NONE) por dentro."""
        corpo = self._corpo("_change_source_state")
        self.assertIn("Thread(", corpo)
        self.assertIn("set_state", corpo)

    def test_desfecho_orcamento_esgotado_e_exit_2(self) -> None:
        corpo = self._corpo("_run_reconnect_action")
        self.assertIn("STEP_GIVE_UP", corpo)
        self.assertIn("EXIT_SOURCE_LOST", corpo)
        self.assertIn('"source_reconnect_exhausted"', corpo)
        self.assertIn("STEP_RESUMED", corpo)
        self.assertIn('"rtsp_reconnect_resumed"', corpo)

    def test_parada_do_pipeline_tem_teto(self) -> None:
        """`docker stop` ≤ 2 s mesmo com a fonte morta."""
        corpo = self._corpo("_teardown")
        self.assertIn("set_state(Gst.State.NULL)", corpo)
        self.assertIn("PIPELINE_STOP_TIMEOUT_S", corpo)
        self.assertIn("pipeline_stop_timeout", corpo)

    def test_fail_nao_recebe_kwarg_reason(self) -> None:
        """`_fail(reason, exit_code, **fields)`: `reason=` como campo estoura.

        Aconteceu na primeira versão desta correção: o desfecho do orçamento
        (`_fail(..., reason=self.reconnect.reason)`) levantava `TypeError`
        dentro do timer a cada 250 ms — o `exit 2` nunca acontecia.
        """
        for no in ast.walk(ast.parse(self.codigo)):
            if not isinstance(no, ast.Call):
                continue
            nome = getattr(no.func, "attr", getattr(no.func, "id", ""))
            if nome != "_fail":
                continue
            for kw in no.keywords:
                self.assertNotIn(
                    kw.arg, ("reason", "exit_code"),
                    "_fail chamado com keyword que colide com seus parâmetros")

    def test_eos_da_fonte_morre_no_tee(self) -> None:
        """Sem conter o EOS os ramos ficam em fim de stream e a análise morre.

        Medido em 2026-10-30: depois de reconectar a fonte o pipeline seguia
        com `fps_in = 30` e `fps_processed = 0` / `fps_preview = 0` — estava
        rodando, mas cego.
        """
        self.assertIn('Gst.PadProbeType.EVENT_DOWNSTREAM, '
                      'self._on_source_eos_event', self.codigo)
        corpo = self._corpo("_on_source_eos_event")
        self.assertIn("Gst.PadProbeReturn.DROP", corpo)
        self.assertIn("_source_eos_pending = True", corpo)
        # o probe roda na thread de streaming: nada de set_state/send_event
        for proibido in ("set_state", "send_event", "get_state"):
            self.assertNotIn(proibido, corpo)

    def test_eos_pendente_e_consumido_pelo_timer(self) -> None:
        """O EOS detectado no probe vira agendamento no thread do loop."""
        self.assertIn("_source_eos_pending", self._corpo("_reconnect_step"))


class EncerramentoLimpoTest(unittest.TestCase):
    """F3 do E2-T3: `SIGTERM`/`SIGINT` precisam encerrar limpo e rápido.

    Sem handler, `docker stop` levava a carência inteira (10 s) e terminava em
    `SIGKILL`: exit 143 e nenhum `pipeline_stopped` na telemetria — o
    `docker stop vision-stability-app` era observável em `docker events` como
    `kill` -> `kill` 10 s depois -> `stop/die`.
    """

    def setUp(self) -> None:
        self.codigo = PIPELINE.read_text(encoding="utf-8")

    def test_usa_o_handler_do_glib(self) -> None:
        """`signal.signal` não roda com o thread principal dentro do loop."""
        self.assertIn("from graceful_shutdown import GracefulShutdown", self.codigo)
        self.assertIn("self.shutdown = GracefulShutdown(GLib, "
                      "self._request_shutdown)", self.codigo)
        self.assertIn("self.shutdown.install()", self.codigo)

    def test_instala_antes_de_rodar_o_loop(self) -> None:
        self.assertLess(self.codigo.index("self.shutdown.install()"),
                        self.codigo.index("self.loop.run()"),
                        "sinal instalado depois de loop.run() nunca é entregue")

    def test_parada_limpa_sai_com_zero_e_registra_pipeline_stopped(self) -> None:
        self.assertIn("self._quit_loop()", self._corpo("_request_shutdown"))
        self.assertIn('log_event("shutdown_requested"',
                      self._corpo("_request_shutdown"))
        # O `set_state(NULL)` + `pipeline_stopped` continuam no finally do run().
        self.assertIn("self.pipeline.set_state(Gst.State.NULL)", self.codigo)
        self.assertIn("log_event(\"pipeline_stopped\", exit_code=self.exit_code",
                      self.codigo)

    def test_handler_nao_chama_gstreamer(self) -> None:
        """Só `loop.quit()` no handler; o resto é o `finally` do `run()`."""
        corpo = self._corpo("_request_shutdown")
        for proibido in ("set_state", "send_event", "Gst."):
            self.assertNotIn(proibido, corpo,
                             f"`_request_shutdown` chama {proibido}")

    def test_sinal_nao_sobrescreve_codigo_de_erro(self) -> None:
        corpo = self._corpo("_request_shutdown")
        self.assertNotIn("self.exit_code =", corpo,
                         "o sinal não pode transformar erro em exit 0")

    def _corpo(self, nome: str) -> str:
        """Código do método, **sem** docstring (o texto dela não é o handler)."""
        arvore = ast.parse(self.codigo)
        for no in ast.walk(arvore):
            if isinstance(no, ast.FunctionDef) and no.name == nome:
                corpo = no.body
                if (corpo and isinstance(corpo[0], ast.Expr)
                        and isinstance(corpo[0].value, ast.Constant)
                        and isinstance(corpo[0].value.value, str)):
                    corpo = corpo[1:]
                return "\n".join(
                    ast.get_source_segment(self.codigo, stmt) or ""
                    for stmt in corpo)
        raise AssertionError(f"método {nome} não encontrado em pipeline_real.py")


if __name__ == "__main__":
    unittest.main()
