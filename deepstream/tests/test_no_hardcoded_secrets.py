"""Guarda de regressão da DIV-001: nenhuma credencial em URI versionada.

Varre os arquivos rastreados pelo Git procurando usuário:senha embutidos em
URLs `rtsp://`/`rtsps://`/`rtmp://`. Credenciais vêm do ambiente
(`deploy/.env`, não versionado) — ver `docs/incident-log.md`, DIV-001.
"""

import re
import subprocess
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

# protocolo://algumacoisa:algumacoisa@host  (usuário e senha não vazios,
# descartando placeholders usados em exemplos e documentação)
URL_COM_CREDENCIAL = re.compile(
    r"(?:rtsps?|rtmps?)://(?P<user>[^/\s:@]+):(?P<senha>[^/\s@]+)@"
)
PLACEHOLDERS = {"USUARIO", "SENHA", "USER", "PASS", "user", "password"}

ARQUIVOS_IGNORADOS = {".md"}  # documentação pode citar o formato mascarado


def _e_placeholder(match):
    """Credencial de exemplo: `${VAR}` (vem do ambiente) ou palavra-modelo."""
    usuario, senha = match.group("user"), match.group("senha")
    if "$" in usuario or "$" in senha:
        return True
    return usuario in PLACEHOLDERS and senha in PLACEHOLDERS



def _arquivos_rastreados():
    saida = subprocess.run(
        ["git", "ls-files"],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for linha in saida.splitlines():
        caminho = RAIZ / linha
        if caminho.suffix in ARQUIVOS_IGNORADOS or not caminho.is_file():
            continue
        yield linha, caminho


class TestSemCredencialVersionada(unittest.TestCase):
    def test_nenhuma_url_com_credencial_embutida(self):
        achados = []
        for relativo, caminho in _arquivos_rastreados():
            try:
                conteudo = caminho.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in URL_COM_CREDENCIAL.finditer(conteudo):
                if _e_placeholder(m):
                    continue
                achados.append(f"{relativo}: {m.group('user')}:***@")

        self.assertEqual(
            achados,
            [],
            "Credencial embutida em URL versionada (DIV-001). "
            "Use RTSP_URI do ambiente (deepstream/rtsp_env.py):\n" + "\n".join(achados),
        )


if __name__ == "__main__":
    unittest.main()
