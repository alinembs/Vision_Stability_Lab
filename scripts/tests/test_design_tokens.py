"""Guarda de paridade entre `DESIGN.md` e o frontend.

O `DESIGN.md` só vale algo se o código o obedecer. Este teste fecha o circuito:
ele lê o frontmatter do documento, os blocos de token de `style.css` (claro e
escuro) e as cores que o `app.js` desenha no canvas, e falha quando os dois
lados divergem.

Cobre as classes de deriva que já aconteceram neste projeto:

1. cor documentada sem variável correspondente (ou o inverso), em cada tema;
2. hex literal no meio do CSS, furando a camada de tokens;
3. cor hardcoded no `app.js`, que deixava o gráfico numa cor e o painel em
   outra quando um token mudava.

Não abre browser nem container: é leitura de texto. A verificação visual do
render continua sendo na Jetson, com o stack no ar.
"""

import json
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
FRONTEND = RAIZ / "dashboard" / "frontend"
DESIGN = RAIZ / "DESIGN.md"
SIDECAR = RAIZ / ".impeccable" / "design.json"

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# valor de token que é uma cor inteira (não um atalho como `var(--x)` nem uma
# sombra composta, que também começa com `rgba(` mas não é cor de paleta)
COR_INTEIRA = re.compile(
    r"^(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))$")
BLOCO_TOKEN = re.compile(r":root(\[data-theme=\"dark\"\])?\s*\{([^}]*)\}")
COR_LITERAL = re.compile(r"(#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d)")


def normaliza_hex(valor: str) -> str:
    """`#abc` -> `#aabbcc`; resto volta em minúsculas para comparar."""
    valor = valor.strip().strip('"').lower()
    if re.fullmatch(r"#[0-9a-f]{3}", valor):
        return "#" + "".join(c * 2 for c in valor[1:])
    return valor


def frontmatter(texto: str) -> dict:
    """Lê o subconjunto de YAML que o DESIGN.md usa: seção e chave de 2 espaços.

    Sem dependência de PyYAML de propósito — o `make test-host` não instala
    nada além do que já está no requirements. Chave aninhada mais fundo
    (propriedade de componente ou de tipografia) é ignorada: aqui só importam
    nomes de token e valores de cor.
    """
    _, bloco, _ = texto.split("---", 2)
    secoes, atual = {}, None
    for linha in bloco.splitlines():
        if not linha.strip():
            continue
        recuo = len(linha) - len(linha.lstrip())
        chave, _, resto = linha.strip().partition(":")
        if recuo == 0:
            atual = chave.strip()
            secoes[atual] = {}
        elif recuo == 2 and atual:
            secoes[atual][chave.strip()] = resto.strip().strip('"')
    return secoes


def blocos_de_token(css: str) -> dict:
    """Tokens declarados em `:root` (claro) e `:root[data-theme="dark"]`."""
    blocos = {}
    for casamento in BLOCO_TOKEN.finditer(css):
        tema = "dark" if casamento.group(1) else "light"
        blocos[tema] = dict(
            re.findall(r"--([a-z0-9-]+):\s*([^;]+);", casamento.group(2)))
    return blocos


def valores(secao: dict) -> set:
    return {normaliza_hex(v) for v in secao.values()}


def tokens_de_cor(bloco: dict) -> set:
    """Nomes cujo valor é uma cor inteira.

    Fonte, raio, largura da sidebar e sombra não pertencem a tema nenhum: eles
    moram só em `:root` e o bloco escuro os herda. Exigir que apareçam nos dois
    blocos obrigaria a duplicar valor sem motivo e criaria o drift que o teste
    existe para evitar.
    """
    return {nome for nome, valor in bloco.items()
            if COR_INTEIRA.match(valor.strip())}


class TestDesignTokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.camadas = frontmatter(DESIGN.read_text(encoding="utf-8"))
        cls.css = (FRONTEND / "style.css").read_text(encoding="utf-8")
        cls.js = (FRONTEND / "app.js").read_text(encoding="utf-8")
        cls.blocos = blocos_de_token(cls.css)
        cls.claro = cls.blocos["light"]
        cls.escuro = cls.blocos["dark"]

    def test_o_parser_do_frontmatter_encontrou_os_tokens(self):
        """Se a formatação do DESIGN.md mudar, o teste falha alto em vez de passar vazio."""
        self.assertGreaterEqual(len(self.camadas["colors"]), 20)
        self.assertGreaterEqual(len(self.camadas["colors-dark"]), 20)
        self.assertGreaterEqual(len(self.camadas["typography"]), 8)

    def test_todo_tema_declara_as_mesmas_cores(self):
        """Cor que só existe num tema quebra `The Same Page at Night Rule`."""
        claro, escuro = tokens_de_cor(self.claro), tokens_de_cor(self.escuro)
        self.assertEqual(claro, escuro,
                         "cores diferentes entre os temas claro e escuro")

    def test_tema_escuro_nao_inventa_token(self):
        """O escuro sobrescreve `:root`; token que só existe nele é inalcançável."""
        orfaos = set(self.escuro) - set(self.claro)
        self.assertFalse(
            orfaos,
            f"token declarado só no tema escuro: {sorted(orfaos)}",
        )

    def test_toda_cor_documentada_existe_como_variavel(self):
        for tema, secao, declaradas in (
                ("claro", self.camadas["colors"], valores(self.claro)),
                ("escuro", self.camadas["colors-dark"], valores(self.escuro))):
            faltando = valores(secao) - declaradas
            self.assertFalse(
                faltando,
                f"cor no DESIGN.md ({tema}) sem variável no CSS: {sorted(faltando)}",
            )

    def test_toda_variavel_de_cor_esta_documentada(self):
        for tema, secao, vars_tema in (
                ("claro", self.camadas["colors"], self.claro),
                ("escuro", self.camadas["colors-dark"], self.escuro)):
            documentadas = valores(secao)
            orfas = {
                nome for nome, valor in vars_tema.items()
                if COR_INTEIRA.match(valor.strip())
                and normaliza_hex(valor) not in documentadas
            }
            self.assertFalse(
                orfas,
                f"variável de cor fora da paleta do DESIGN.md ({tema}): {sorted(orfas)}",
            )

    def test_nenhum_hex_literal_fora_dos_blocos_de_token(self):
        """Cor nova tem de virar token: senão a paleta volta a se espalhar."""
        corpo = BLOCO_TOKEN.sub("", self.css)
        sobras = sorted({m for m in HEX.findall(corpo)})
        self.assertFalse(sobras, f"hex literal fora dos blocos de token: {sobras}")

    def test_nenhum_var_sem_declaracao(self):
        usadas = set(re.findall(r"var\(--([a-z0-9-]+)\)", self.css))
        self.assertFalse(
            usadas - set(self.claro),
            f"var() sem declaração: {sorted(usadas - set(self.claro))}",
        )

    def test_app_js_le_os_tokens_do_css(self):
        """O canvas não pode ter paleta própria — era o bug de cor que não acompanha."""
        lidos = set(re.findall(r'token\("(--[a-z0-9-]+)"\)', self.js))
        self.assertGreaterEqual(len(lidos), 4, f"app.js lê só: {sorted(lidos)}")
        for tema, declaradas in (("claro", self.claro), ("escuro", self.escuro)):
            sem_declaracao = {t for t in lidos if t.lstrip("-") not in declaradas}
            self.assertFalse(
                sem_declaracao,
                f"app.js lê var inexistente no tema {tema}: {sorted(sem_declaracao)}",
            )

    def test_app_js_nao_tem_cor_literal(self):
        linhas = [
            (n, l) for n, l in enumerate(self.js.splitlines(), 1)
            if COR_LITERAL.search(l) and not l.lstrip().startswith(("//", "*"))
        ]
        self.assertFalse(linhas, f"cor literal no app.js: {linhas}")

    def test_componentes_compartilhados_nao_sao_redefinidos(self):
        """`.note` e `.link` moram em style.css; página que redefine volta a divergir."""
        for pagina in ("capture.html", "codec-gate-selftest.html"):
            texto = (FRONTEND / pagina).read_text(encoding="utf-8")
            for seletor in (".note", ".link"):
                # só redefinição no começo da linha conta: a regra escopada
                # (`.video-panel .note {`) é o uso esperado do componente
                redefine = re.search(rf"(?m)^\s*{re.escape(seletor)}\s*\{{", texto)
                self.assertIsNone(
                    redefine, f"{seletor} redefinido em {pagina}")

    def test_nenhum_hex_literal_nos_blocos_de_pagina(self):
        """Página satélite também declara cor em token — `capture` fazia o contrário."""
        for pagina in ("capture.html", "codec-gate-selftest.html", "index.html"):
            texto = (FRONTEND / pagina).read_text(encoding="utf-8")
            for bloco in re.findall(r"<style>(.*?)</style>", texto, re.S):
                sobras = sorted({m for m in HEX.findall(bloco)})
                self.assertFalse(sobras, f"hex literal em <style> de {pagina}: {sobras}")

    def test_sidecar_espelha_o_frontmatter(self):
        sidecar = json.loads(SIDECAR.read_text(encoding="utf-8"))
        meta_cores = sidecar["extensions"]["colorMeta"]
        self.assertEqual(
            set(meta_cores), set(self.camadas["colors"]),
            "colorMeta e colors divergiram",
        )
        self.assertEqual(
            set(meta_cores), set(self.camadas["colors-dark"]),
            "colorMeta e colors-dark divergiram",
        )
        for nome, meta in meta_cores.items():
            self.assertEqual(
                normaliza_hex(meta["canonical"]),
                normaliza_hex(self.camadas["colors"][nome]),
                f"canonical de {nome} divergiu do tema claro",
            )
            self.assertEqual(
                normaliza_hex(meta["darkCanonical"]),
                normaliza_hex(self.camadas["colors-dark"][nome]),
                f"darkCanonical de {nome} divergiu do tema escuro",
            )
        tipos = set(sidecar["extensions"]["typographyMeta"])
        self.assertEqual(
            tipos, set(self.camadas["typography"]),
            "typographyMeta e typography divergiram",
        )


if __name__ == "__main__":
    unittest.main()
