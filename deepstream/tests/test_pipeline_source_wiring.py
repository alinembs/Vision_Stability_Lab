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

    def test_rtspsrc_usa_uri_e_parametros_da_fonte(self) -> None:
        self.assertTrue("location=self.rtsp_uri" in self.codigo)
        self.assertTrue("latency=self.source.latency_ms" in self.codigo)
        self.assertTrue("protocols=self.source.protocols" in self.codigo)

    def test_pipeline_nao_le_mais_rtsp_codec(self) -> None:
        self.assertIsNone(
            re.search(r'environ\.get\(\s*"RTSP_CODEC"', self.codigo),
            "pipeline_real.py voltou a ler RTSP_CODEC: o codec é da fonte (D5)")

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


if __name__ == "__main__":
    unittest.main()
