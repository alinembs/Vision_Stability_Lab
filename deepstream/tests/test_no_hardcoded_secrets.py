"""Guarda de regressão da DIV-001: nenhuma credencial em URI versionada.

Varre o working tree — arquivos rastreados mais os novos ainda não
rastreados, respeitando o `.gitignore` — procurando usuário:senha embutidos
em URLs `rtsp://`/`rtsps://`/`rtmp://`. Credenciais vêm do ambiente
(`deploy/.env`, não versionado) — ver `docs/incident-log.md`, DIV-001.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def _arquivos_do_working_tree():
    """Rastreados + novos não ignorados, respeitando o `.gitignore`."""
    saida = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=RAIZ,
        capture_output=True,
        check=True,
    ).stdout
    # `-z` separa por NUL: não quebra em nome de arquivo com espaço/newline.
    for bruto in saida.split(b"\0"):
        if not bruto:
            continue
        relativo = os.fsdecode(bruto)
        caminho = RAIZ / relativo
        if caminho.suffix in ARQUIVOS_IGNORADOS or not caminho.is_file():
            continue
        yield relativo, caminho


class TestSemCredencialVersionada(unittest.TestCase):
    def test_nenhuma_url_com_credencial_embutida(self):
        achados = []
        for relativo, caminho in _arquivos_do_working_tree():
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


@unittest.skipUnless(shutil.which("git"), "git não disponível no ambiente")
class TestEnumeracaoDoWorkingTree(unittest.TestCase):
    """Prova que a guarda enxerga arquivo novo, ainda não rastreado.

    Antes de `git ls-files --others`, um teste com credencial embutida passava
    verde e só era pego no commit (foi o caso dos testes do observador).
    """

    def test_inclui_untracked_e_respeita_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / ".gitignore").write_text("ignorado.py\n", encoding="utf-8")
            (raiz / "rastreado.py").write_text("ok\n", encoding="utf-8")
            (raiz / "novo.py").write_text("ok\n", encoding="utf-8")
            (raiz / "ignorado.py").write_text("ok\n", encoding="utf-8")

            subprocess.run(
                ["git", "init", "-q"], cwd=raiz, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "add", "rastreado.py"],
                cwd=raiz,
                check=True,
                capture_output=True,
            )

            with mock.patch.object(sys.modules[__name__], "RAIZ", raiz):
                nomes = {relativo for relativo, _ in _arquivos_do_working_tree()}

        self.assertIn("rastreado.py", nomes, "arquivo rastreado deveria entrar")
        self.assertIn("novo.py", nomes, "arquivo untracked deveria entrar")
        self.assertNotIn("ignorado.py", nomes, ".gitignore deveria ser respeitado")


if __name__ == "__main__":
    unittest.main()
