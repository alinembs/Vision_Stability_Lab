"""Guarda do download da CA pela 8443 (`deploy/nginx/capture-web.conf`).

A página de captura publica `/ca.crt` para o onboarding do celular sem cabo.
O risco é a rota virar porta de saída de material de chave: só o certificado
**público** da CA pode sair, por location exata, e nada de `/certs` genérico.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

CONF = Path(__file__).resolve().parents[2] / "deploy" / "nginx" / "capture-web.conf"


class CaDownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        linhas = CONF.read_text(encoding="utf-8").splitlines()
        # Ignora comentários: só a configuração efetiva conta.
        self.conf = "\n".join(l.split("#", 1)[0] for l in linhas)

    def test_ca_crt_por_location_exata(self) -> None:
        bloco = re.search(r"location = /ca\.crt \{(.*?)\n    \}", self.conf, re.S)
        self.assertIsNotNone(bloco, "location = /ca.crt ausente")
        self.assertIn("alias /ca/ca.crt;", bloco.group(1))
        self.assertIn("application/x-x509-ca-cert", bloco.group(1))

    def test_nenhuma_chave_e_servida(self) -> None:
        # `ssl_certificate_key` é o TLS do próprio nginx, não uma rota.
        servido = re.sub(r"ssl_certificate_key\s+\S+;", "", self.conf)
        self.assertNotRegex(servido, r"\.key\b")
        aliases = re.findall(r"alias\s+(\S+);", self.conf)
        self.assertEqual(aliases, ["/ca/ca.crt"])
        self.assertNotRegex(self.conf, r"location\s+(\^~\s+)?/certs")

    def test_compose_monta_so_o_ca_crt(self) -> None:
        compose = (CONF.parents[1] / "docker-compose.yml").read_text(
            encoding="utf-8")
        self.assertIn("../deploy/certs/ca.crt:/ca/ca.crt:ro", compose)
        self.assertNotRegex(compose, r"certs/\S*\.key:")


if __name__ == "__main__":
    unittest.main()
