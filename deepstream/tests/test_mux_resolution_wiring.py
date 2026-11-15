"""Guarda da fiação da resolução do `nvstreammux` no pipeline e no compose.

A resolução do mux é a que o nvdsosd desenha e o preview publica. Fixá-la em
1280x720 no código fazia toda fonte 1080p+ sair em 720p no dashboard. A regra
(fonte > default, override por ambiente, teto sem NVENC) mora em
`sources.resolve_mux_resolution` e é testada em `test_sources.py`; aqui só se
garante que o pipeline a usa. Verificado no texto, como em
`test_pipeline_source_wiring.py`: o módulo importa `gi`/`pyds`.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PIPELINE = RAIZ / "deepstream" / "pipeline_real.py"
COMPOSE = RAIZ / "deploy" / "docker-compose.yml"


class MuxResolutionWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codigo = PIPELINE.read_text(encoding="utf-8")
        self.compose = COMPOSE.read_text(encoding="utf-8")
        chamada = re.search(r'_make\("nvstreammux".*?\)', self.codigo, re.S)
        self.assertIsNotNone(chamada)
        self.mux = chamada.group(0)

    def test_pipeline_resolve_pela_fonte(self) -> None:
        self.assertIn("resolve_mux_resolution(", self.codigo)
        self.assertIn("hw_encoder=os.path.exists(NVENC_DEVICE)", self.codigo)

    def test_mux_usa_a_resolucao_resolvida(self) -> None:
        self.assertIn("width=self.mux_width, height=self.mux_height", self.mux)
        self.assertNotRegex(self.mux, r"width=\d|height=\d")

    def test_mux_mantem_a_proporcao(self) -> None:
        self.assertIn("enable_padding=True", self.mux)

    def test_resolucao_vai_para_a_telemetria(self) -> None:
        self.assertIn("mux_origin=self.mux_origin", self.codigo)

    def test_compose_nao_fixa_default(self) -> None:
        # Default no compose venceria o `mux` do YAML: vazio = não definido.
        for var in ("MUX_WIDTH", "MUX_HEIGHT", "MUX_SW_MAX"):
            self.assertIn(f"{var}=${{{var}:-}}", self.compose)


if __name__ == "__main__":
    unittest.main()
