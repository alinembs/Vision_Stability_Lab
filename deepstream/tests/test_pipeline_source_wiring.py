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
