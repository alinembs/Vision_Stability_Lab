"""Testes do descritor declarativo de fontes (`deepstream/sources.py`, E2-T1).

Tudo aqui é lógica pura: nenhum caso abre socket, container ou toca em
GStreamer/`pyds` — a bancada de mídia é o `deepstream-tester` (E2-T2). O
objetivo é provar as regras que, se quebrarem silenciosamente, fariam a troca
de fonte "não pegar" (seção 14.1 do plano): precedência da fonte ativa,
validação do YAML, expansão de variável, fallback legado e depay por codec.

Nenhuma URI aqui tem credencial literal: a guarda da DIV-001
(`test_no_hardcoded_secrets.py`) varre o repositório atrás de `usuario:senha@`.
As credenciais falsas são montadas em tempo de execução com `_com_credencial()`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rtsp_env  # noqa: E402
import sources  # noqa: E402

USUARIO_FALSO = "admin"
SENHA_FALSA = "senhaFalsa123"
URI_CAMERA = "rtsp://192.168.59.108:5543/live/channel0"
URI_DRONE = "rtsp://192.168.59.102:8554/drone"
URI_PHONE = "rtsp://192.168.59.102:8554/phone"


def _com_credencial(uri: str) -> str:
    esquema, resto = uri.split("://", 1)
    return f"{esquema}://{USUARIO_FALSO}:{SENHA_FALSA}@{resto}"


YAML_BASE = """
version: 1
default_source: {default}
sources:
  - id: camera
    name: "câmera fixa"
    uri_env: RTSP_URI
    codec: h265
    transport: tcp
    latency_ms: 200
    target_fps: 20
    enabled: true
    gate:
      latency_p95_ms: 300
  - id: drone
    name: "drone"
    uri: "rtsp://${{MTX_HOST:-mediamtx}}:8554/drone"
    codec: h264
    latency_ms: 150
    target_fps: 25
    enabled: true
    gate:
      latency_p95_ms: null
      report_only: true
  - id: phone
    name: "celular"
    uri: "{phone_uri}"
    codec: h264
    target_fps: 25
    enabled: {phone_enabled}
"""


class SourcesTestCase(unittest.TestCase):
    """Base com um YAML temporário e ambiente isolado (nunca o do processo)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        # Arquivo de controle inexistente por default: quem lê a fonte ativa
        # não pode enxergar o `data/config/active_source` real do host.
        self.control_ausente = str(self.dir / "active_source")

    def escrever(self, conteudo: str, nome: str = "sources.yaml") -> str:
        caminho = self.dir / nome
        caminho.write_text(conteudo, encoding="utf-8")
        return str(caminho)

    def yaml_base(self, default: str = "camera", phone_uri: str = URI_PHONE,
                  phone_enabled: str = "true") -> str:
        return YAML_BASE.format(default=default, phone_uri=phone_uri,
                                phone_enabled=phone_enabled)

    def carregar(self, caminho: str | None = None, **env) -> sources.SourceList:
        """`load_sources` com ambiente explícito (default: só RTSP_URI)."""
        ambiente = {"RTSP_URI": URI_CAMERA}
        ambiente.update(env)
        return sources.load_sources(caminho or self.escrever(self.yaml_base()),
                                    env=ambiente)


class ParseFelizTest(SourcesTestCase):
    def test_tres_fontes_parseadas(self) -> None:
        fontes = self.carregar(MTX_HOST="192.168.59.102")
        self.assertIsInstance(fontes, list)
        self.assertEqual([f.id for f in fontes], ["camera", "drone", "phone"])
        self.assertEqual(fontes.default_source, "camera")

        camera, drone, phone = fontes
        self.assertEqual(camera.uri, URI_CAMERA)  # veio de RTSP_URI (DIV-001)
        self.assertEqual(camera.codec, "h265")
        self.assertEqual(camera.latency_ms, 200)
        self.assertEqual(camera.target_fps, 20)
        self.assertEqual(camera.gate, {"latency_p95_ms": 300})
        self.assertTrue(camera.enabled)

        self.assertEqual(drone.uri, URI_DRONE)  # ${MTX_HOST:-...} expandido
        self.assertEqual(drone.codec, "h264")
        self.assertEqual(drone.gate, {"latency_p95_ms": None,
                                      "report_only": True})
        self.assertEqual(phone.transport, "tcp")  # default do módulo
        self.assertEqual(phone.name, "celular")

    def test_protocols_do_rtspsrc_por_transport(self) -> None:
        fontes = self.carregar()
        self.assertEqual(fontes[0].protocols, 4)  # tcp
        caminho = self.escrever(
            "sources:\n  - id: camera\n    uri: " + URI_CAMERA +
            "\n    codec: h265\n    transport: udp\n")
        self.assertEqual(sources.load_sources(caminho, env={})[0].protocols, 1)


class ValidacaoTest(SourcesTestCase):
    def test_id_duplicado(self) -> None:
        caminho = self.escrever(
            "sources:\n"
            f"  - id: camera\n    uri: {URI_CAMERA}\n    codec: h265\n"
            f"  - id: camera\n    uri: {URI_DRONE}\n    codec: h264\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        self.assertIn("duplicado", str(ctx.exception))

    def test_id_invalido(self) -> None:
        for invalido in ("Camera", "drone 1", "drone/1", ""):
            caminho = self.escrever(
                f"sources:\n  - id: {invalido!r}\n    uri: {URI_DRONE}\n")
            with self.assertRaises(sources.SourceConfigError) as ctx:
                sources.load_sources(caminho, env={})
            self.assertIn("id", str(ctx.exception))

    def test_codec_invalido(self) -> None:
        caminho = self.escrever(
            f"sources:\n  - id: drone\n    uri: {URI_DRONE}\n    codec: vp9\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        self.assertIn("codec", str(ctx.exception))
        self.assertIn("h264", str(ctx.exception))

    def test_target_fps_nao_positivo(self) -> None:
        for valor in ("0", "-5"):
            caminho = self.escrever(
                f"sources:\n  - id: drone\n    uri: {URI_DRONE}\n"
                f"    codec: h264\n    target_fps: {valor}\n")
            with self.assertRaises(sources.SourceConfigError) as ctx:
                sources.load_sources(caminho, env={})
            self.assertIn("target_fps", str(ctx.exception))

    def test_uri_sem_rtsp(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: drone\n    uri: http://192.168.59.102:8554/drone\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        self.assertIn("rtsp://", str(ctx.exception))

    def test_transport_invalido(self) -> None:
        caminho = self.escrever(
            f"sources:\n  - id: drone\n    uri: {URI_DRONE}\n"
            "    codec: h264\n    transport: sctp\n")
        with self.assertRaises(sources.SourceConfigError):
            sources.load_sources(caminho, env={})

    def test_enabled_precisa_ser_booleano(self) -> None:
        caminho = self.escrever(
            f"sources:\n  - id: drone\n    uri: {URI_DRONE}\n"
            "    codec: h264\n    enabled: 'sim'\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        self.assertIn("enabled", str(ctx.exception))

    def test_default_source_inexistente(self) -> None:
        caminho = self.escrever(self.yaml_base(default="tablet"))
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={"RTSP_URI": URI_CAMERA})
        self.assertIn("tablet", str(ctx.exception))
        self.assertIn("camera, drone, phone", str(ctx.exception))

    def _yaml_com_n_fontes(self, quantidade: int, nome: str) -> str:
        entradas = "".join(
            f"  - id: fonte{i}\n    uri: rtsp://host:8554/p{i}\n    codec: h264\n"
            for i in range(quantidade))
        return self.escrever("sources:\n" + entradas, nome=nome)

    def test_max_sources_e_o_limite_de_uma_fonte_ativa_por_vez(self) -> None:
        excedente = self._yaml_com_n_fontes(sources.MAX_SOURCES + 1, "quatro.yaml")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(excedente, env={})
        mensagem = str(ctx.exception)
        self.assertIn("máximo", mensagem)
        self.assertIn("D1", mensagem)

        no_limite = self._yaml_com_n_fontes(sources.MAX_SOURCES, "tres.yaml")
        self.assertEqual(len(sources.load_sources(no_limite, env={})),
                         sources.MAX_SOURCES)


class ExpansaoDeVariavelTest(SourcesTestCase):
    def test_variavel_simples(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: drone\n    uri: \"rtsp://${MTX_HOST}:8554/drone\"\n"
            "    codec: h264\n")
        fonte = sources.load_sources(caminho, env={"MTX_HOST": URI_DRONE.split("//")[1].split(":")[0]})[0]
        self.assertEqual(fonte.uri, URI_DRONE)

    def test_variavel_com_default(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: drone\n"
            "    uri: \"rtsp://${MTX_HOST:-mediamtx}:8554/drone\"\n    codec: h264\n")
        self.assertEqual(sources.load_sources(caminho, env={})[0].uri,
                         "rtsp://mediamtx:8554/drone")
        self.assertEqual(
            sources.load_sources(caminho, env={"MTX_HOST": "192.168.59.102"})[0].uri,
            URI_DRONE)
        # `${VAR:-}` com variável vazia também cai no default (semântica bash)
        self.assertEqual(sources.load_sources(caminho, env={"MTX_HOST": ""})[0].uri,
                         "rtsp://mediamtx:8554/drone")

    def test_variavel_sem_default_ausente_e_erro(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: drone\n    uri: \"rtsp://${IP_DO_DRONE}:8554/drone\"\n"
            "    codec: h264\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        self.assertIn("IP_DO_DRONE", str(ctx.exception))

    def test_uri_env_nao_e_expandida(self) -> None:
        """m10: `uri_env` é o ambiente já resolvido — `${...}` não se expande.

        O valor vem de `deploy/.env`/compose, que já fizeram a expansão do
        compose. Expandir de novo abriria a porta para `$` dentro de senha e
        mascararia um `.env` mal preenchido.
        """
        caminho = self.escrever(
            "sources:\n  - id: camera\n    uri_env: RTSP_URI\n    codec: h265\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(
                caminho, env={"RTSP_URI": "rtsp://${CAM_HOST}:5543/live/channel0",
                              "CAM_HOST": "192.168.59.108"})
        mensagem = str(ctx.exception)
        self.assertIn("RTSP_URI", mensagem)
        self.assertIn("não expandido", mensagem)

    def test_uri_env_resolvida_entra_literal(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: camera\n    uri_env: RTSP_URI\n    codec: h265\n")
        fonte = sources.load_sources(
            caminho, env={"RTSP_URI": _com_credencial(URI_CAMERA)})[0]
        self.assertEqual(fonte.uri, _com_credencial(URI_CAMERA))

    def test_host_so_com_espaco_e_erro(self) -> None:
        """m4: `H=" "` produzia `rtsp:// /x`, aceito e opaco no `rtspsrc`."""
        caminho = self.escrever(
            "sources:\n  - id: drone\n    uri: \"rtsp://${H}:8554/drone\"\n"
            "    codec: h264\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={"H": " "})
        mensagem = str(ctx.exception)
        self.assertIn("vazia", mensagem)
        self.assertIn("H", mensagem)

    def test_host_vazio_pela_expansao_e_erro_de_host(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: drone\n    uri: \"rtsp://${H:-}:8554/drone\"\n"
            "    codec: h264\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        self.assertIn("host", str(ctx.exception))

    def test_valor_expandido_e_aparado(self) -> None:
        """m4: `strip()` no valor expandido (espaço no fim da URI)."""
        caminho = self.escrever(
            "sources:\n  - id: drone\n    uri: \"  rtsp://${HOST}:8554/drone  \"\n"
            "    codec: h264\n")
        fonte = sources.load_sources(caminho, env={"HOST": "192.168.59.102 "})[0]
        self.assertEqual(fonte.uri, URI_DRONE)


class UriEnvTest(SourcesTestCase):
    def test_uri_env_ausente_e_erro_claro_sem_default(self) -> None:
        caminho = self.escrever(
            "sources:\n  - id: camera\n    uri_env: RTSP_URI\n    codec: h265\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={})
        mensagem = str(ctx.exception)
        self.assertIn("RTSP_URI", mensagem)
        self.assertIn("DIV-001", mensagem)
        self.assertIn("deploy/.env.example", mensagem)

    def test_uri_e_uri_env_juntos_e_erro(self) -> None:
        caminho = self.escrever(
            f"sources:\n  - id: camera\n    uri: {URI_CAMERA}\n"
            "    uri_env: RTSP_URI\n    codec: h265\n")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(caminho, env={"RTSP_URI": URI_CAMERA})
        self.assertIn("não os dois", str(ctx.exception))


class PrecedenciaDaFonteAtivaTest(SourcesTestCase):
    """env `ACTIVE_SOURCE` > arquivo `active_source` > default > 1º enabled."""

    def setUp(self) -> None:
        super().setUp()
        self.fontes = self.carregar()
        self.arquivo = str(self.dir / "active_source")

    def resolver(self, **env) -> sources.SourceConfig:
        return sources.resolve_active_source(self.fontes, env=env,
                                             control_path=self.arquivo)

    def test_env_vence_arquivo_e_default(self) -> None:
        Path(self.arquivo).write_text("drone\n", encoding="utf-8")
        self.assertEqual(self.resolver(ACTIVE_SOURCE="phone").id, "phone")

    def test_arquivo_vence_default(self) -> None:
        Path(self.arquivo).write_text("drone\n", encoding="utf-8")
        self.assertEqual(self.resolver().id, "drone")

    def test_arquivo_ausente_cai_no_default(self) -> None:
        self.assertEqual(self.resolver().id, "camera")

    def test_sem_default_cai_na_primeira_habilitada(self) -> None:
        caminho = self.escrever(
            "sources:\n"
            "  - id: camera\n    uri: rtsp://host:554/c\n    codec: h265\n"
            "    enabled: false\n"
            "  - id: drone\n    uri: rtsp://host:8554/drone\n    codec: h264\n")
        fontes = sources.load_sources(caminho, env={})
        self.assertIsNone(fontes.default_source)
        self.assertEqual(
            sources.resolve_active_source(fontes, env={},
                                          control_path=self.control_ausente).id,
            "drone")

    def test_arquivo_vazio_cai_no_default(self) -> None:
        Path(self.arquivo).write_text("\n", encoding="utf-8")
        self.assertEqual(self.resolver().id, "camera")

    def test_arquivo_com_espacos_e_newline(self) -> None:
        Path(self.arquivo).write_text("  phone  \n", encoding="utf-8")
        self.assertEqual(self.resolver().id, "phone")

    def test_env_com_id_inexistente_lista_os_validos(self) -> None:
        with self.assertRaises(sources.SourceConfigError) as ctx:
            self.resolver(ACTIVE_SOURCE="inexistente")
        mensagem = str(ctx.exception)
        self.assertIn("inexistente", mensagem)
        self.assertIn("camera, drone, phone", mensagem)
        self.assertIn("fontes válidas", mensagem)

    def test_arquivo_com_id_inexistente_nao_cai_no_default(self) -> None:
        Path(self.arquivo).write_text("tablet\n", encoding="utf-8")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            self.resolver()
        self.assertIn("camera, drone, phone", str(ctx.exception))

    def test_fonte_desabilitada_por_env_e_erro(self) -> None:
        caminho = self.escrever(
            f"sources:\n  - id: camera\n    uri: {URI_CAMERA}\n    codec: h265\n"
            f"  - id: drone\n    uri: {URI_DRONE}\n    codec: h264\n"
            "    enabled: false\n")
        fontes = sources.load_sources(caminho, env={"RTSP_URI": URI_CAMERA})
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.resolve_active_source(fontes, env={"ACTIVE_SOURCE": "drone"},
                                          control_path=self.control_ausente)
        mensagem = str(ctx.exception)
        self.assertIn("enabled: false", mensagem)
        self.assertIn("camera", mensagem)

    def test_sem_nenhuma_fonte_habilitada(self) -> None:
        caminho = self.escrever(
            f"sources:\n  - id: camera\n    uri: {URI_CAMERA}\n"
            "    codec: h265\n    enabled: false\n")
        fontes = sources.load_sources(caminho, env={"RTSP_URI": URI_CAMERA})
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.resolve_active_source(fontes, env={},
                                          control_path=self.control_ausente)
        self.assertIn("habilitada", str(ctx.exception))


class FallbackLegadoTest(SourcesTestCase):
    """Sem YAML (ou com a seleção desligada) = comportamento de antes do E2."""

    def test_sem_arquivo_usa_rtsp_uri_e_rtsp_codec(self) -> None:
        # Arquivo **vazio** (não caminho inexistente: caminho explícito que não
        # existe é erro, ver `test_caminho_explicito_inexistente_nao_cai_no_legado`).
        vazio = self.escrever("", nome="vazio.yaml")
        fontes = sources.load_sources(vazio,
                                      env={"RTSP_URI": _com_credencial(URI_CAMERA),
                                           "RTSP_CODEC": "h265"})
        self.assertEqual(len(fontes), 1)
        self.assertEqual(fontes[0].id, sources.LEGACY_SOURCE_ID)
        self.assertEqual(fontes[0].codec, "h265")
        self.assertEqual(fontes.default_source, sources.LEGACY_SOURCE_ID)
        self.assertEqual(
            sources.resolve_active_source(fontes, env={},
                                          control_path=self.control_ausente).id,
            sources.LEGACY_SOURCE_ID)

    def test_caminho_explicito_inexistente_nao_cai_no_legado(self) -> None:
        """m1: typo em `SOURCES_CONFIG`/argumento é erro, não legado."""
        for env_extra in ({sources.ENV_CONFIG: str(self.dir / "typo.yaml")}, {}):
            with self.assertRaises(sources.SourceConfigError) as ctx:
                sources.load_sources(
                    self.dir / "nao_existe.yaml",
                    env={"RTSP_URI": URI_CAMERA, **env_extra})
            mensagem = str(ctx.exception)
            self.assertIn("não existe", mensagem)
            self.assertNotIn(SENHA_FALSA, mensagem)

    def test_caminho_explicito_que_e_diretorio_e_erro(self) -> None:
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(self.dir, env={"RTSP_URI": URI_CAMERA})
        self.assertIn("arquivo regular", str(ctx.exception))

    def test_sources_config_apontando_para_arquivo_vazio(self) -> None:
        fontes = sources.load_sources(
            env={sources.ENV_CONFIG: "/dev/null",
                 "RTSP_URI": URI_CAMERA, "RTSP_CODEC": "h264"})
        self.assertEqual(len(fontes), 1)
        self.assertEqual(fontes[0].codec, "h264")

    def test_sources_config_vazio_cai_no_versionado(self) -> None:
        # SOURCES_CONFIG vazio não sobrescreve o caminho do módulo: o clone
        # normal (com sources.yaml versionado) sobe com as três fontes.
        fontes = sources.load_sources(env={sources.ENV_CONFIG: "",
                                           "RTSP_URI": URI_CAMERA,
                                           "MTX_HOST": "mediamtx"})
        self.assertEqual([f.id for f in fontes], ["camera", "drone", "phone"])

    def test_sem_rtsp_uri_e_sem_arquivo_e_erro_claro(self) -> None:
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.load_sources(env={sources.ENV_CONFIG: "/dev/null"})
        mensagem = str(ctx.exception)
        self.assertIn("RTSP_URI", mensagem)
        self.assertIn("DIV-001", mensagem)

    def test_flag_de_selecao_desligada_usa_o_caminho_legado(self) -> None:
        fontes = sources.load_sources(
            env={sources.ENV_SELECTION: "off", "RTSP_URI": URI_CAMERA,
                 "RTSP_CODEC": "h265"})
        self.assertEqual(len(fontes), 1)
        self.assertEqual(fontes[0].name, "fonte legada (RTSP_URI)")
        self.assertFalse(sources.selection_enabled(
            {sources.ENV_SELECTION: "OFF"}))
        self.assertTrue(sources.selection_enabled({"VSL_SOURCE_SELECTION": "on"}))


class ArquivoDeControleTest(SourcesTestCase):
    """`data/config/active_source`: leitura robusta e *fail-closed* (m2)."""

    def setUp(self) -> None:
        super().setUp()
        self.fontes = self.carregar()
        self.arquivo = self.dir / "active_source"

    def resolver(self, **env) -> sources.SourceConfig:
        return sources.resolve_active_source(self.fontes, env=env,
                                             control_path=str(self.arquivo))

    def test_bom_nao_vira_parte_do_id(self) -> None:
        self.arquivo.write_bytes(b"\xef\xbb\xbfdrone\n")
        self.assertEqual(sources.read_active_source(str(self.arquivo)), "drone")
        self.assertEqual(self.resolver().id, "drone")

    def test_crlf_continua_funcionando(self) -> None:
        self.arquivo.write_bytes(b"drone\r\n")
        self.assertEqual(self.resolver().id, "drone")

    def test_comentarios_e_linhas_em_branco_sao_ignorados(self) -> None:
        self.arquivo.write_text(
            "# fonte ativa da bancada\n\n   # drone hoje\ndrone\nphone\n",
            encoding="utf-8")
        self.assertEqual(self.resolver().id, "drone")

    def test_so_a_primeira_linha_valida_vale(self) -> None:
        self.arquivo.write_text("drone\nphone\n", encoding="utf-8")
        self.assertEqual(sources.read_active_source(str(self.arquivo)), "drone")

    def test_arquivo_so_com_comentarios_cai_no_default(self) -> None:
        self.arquivo.write_text("# nada escolhido ainda\n\n", encoding="utf-8")
        self.assertEqual(self.resolver().id, "camera")

    def test_arquivo_ilegivel_nao_cai_no_default(self) -> None:
        """m3 original: `except OSError: return ""` fazia `chmod 000` virar câmera."""
        self.arquivo.write_text("drone\n", encoding="utf-8")
        self.arquivo.chmod(0o000)
        self.addCleanup(self.arquivo.chmod, 0o644)
        if os.geteuid() == 0:  # pragma: no cover - root lê qualquer coisa
            self.skipTest("root ignora as permissões do arquivo")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            self.resolver()
        mensagem = str(ctx.exception)
        self.assertIn("não pôde ser lido", mensagem)
        self.assertIn("NÃO cai em default", mensagem)

    def test_controle_que_e_diretorio_e_erro(self) -> None:
        """EISDIR: arquivo existe e não é legível como arquivo (sem chmod)."""
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.read_active_source(str(self.dir))
        self.assertIn("não pôde ser lido", str(ctx.exception))

    def test_arquivo_binario_e_erro_de_codificacao(self) -> None:
        self.arquivo.write_bytes(b"\xff\xfe\x00drone")
        with self.assertRaises(sources.SourceConfigError) as ctx:
            sources.read_active_source(str(self.arquivo))
        self.assertIn("UTF-8", str(ctx.exception))

    def test_ausente_devolve_string_vazia(self) -> None:
        self.assertEqual(sources.read_active_source(self.control_ausente), "")


class CaminhoLegadoSemSystemExitTest(unittest.TestCase):
    """m3: `sources.py` levanta `SourceConfigError`; o `sys.exit` fica no main."""

    def test_rtsp_uri_ausente_no_ambiente_real_nao_encerra_o_processo(self) -> None:
        ambiente = {sources.ENV_CONFIG: "/dev/null"}
        with mock.patch.dict(os.environ, ambiente, clear=True):
            with self.assertRaises(sources.SourceConfigError) as ctx:
                sources.load_sources()  # env=None => ambiente do processo
        mensagem = str(ctx.exception)
        self.assertIn("RTSP_URI", mensagem)
        self.assertIn("DIV-001", mensagem)

    def test_ambiente_real_com_rtsp_uri_usa_a_fonte_legada(self) -> None:
        ambiente = {sources.ENV_CONFIG: "/dev/null",
                    "RTSP_URI": _com_credencial(URI_CAMERA),
                    "RTSP_CODEC": "h264"}
        with mock.patch.dict(os.environ, ambiente, clear=True):
            fontes = sources.load_sources()
        self.assertEqual(fontes[0].codec, "h264")
        self.assertEqual(fontes[0].uri, _com_credencial(URI_CAMERA))

    def test_require_rtsp_uri_continua_saindo_para_os_scripts_de_bancada(self) -> None:
        self.assertTrue(callable(rtsp_env.require_rtsp_uri))
        self.assertTrue(issubclass(rtsp_env.RtspUriError, ValueError))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                rtsp_env.require_rtsp_uri()


class DepayFactoryTest(unittest.TestCase):
    def test_h265(self) -> None:
        self.assertEqual(sources.depay_factory("h265"),
                         ("rtph265depay", "h265parse"))
        self.assertEqual(sources.depay_factory("H265"),
                         ("rtph265depay", "h265parse"))

    def test_h264(self) -> None:
        self.assertEqual(sources.depay_factory("h264"),
                         ("rtph264depay", "h264parse"))

    def test_codec_invalido(self) -> None:
        for invalido in ("vp9", "h266", ""):
            with self.assertRaises(sources.SourceConfigError) as ctx:
                sources.depay_factory(invalido)
            self.assertIn("h264, h265", str(ctx.exception))

    def test_source_config_expoe_a_cadeia(self) -> None:
        fonte = sources.SourceConfig(id="drone", name="drone", uri=URI_DRONE,
                                     codec="h264")
        self.assertEqual(fonte.depay_factory, "rtph264depay")
        self.assertEqual(fonte.parser_factory, "h264parse")


class MascaramentoTest(unittest.TestCase):
    def test_credencial_mascarada(self) -> None:
        self.assertEqual(sources.mask_uri(_com_credencial(URI_CAMERA)),
                         "rtsp://***@192.168.59.108:5543/live/channel0")
        self.assertNotIn(SENHA_FALSA, sources.mask_uri(_com_credencial(URI_CAMERA)))

    def test_uri_sem_credencial_inalterada(self) -> None:
        self.assertEqual(sources.mask_uri(URI_DRONE), URI_DRONE)
        self.assertEqual(sources.mask_uri("rtsp://mediamtx:8554/phone"),
                         "rtsp://mediamtx:8554/phone")

    def test_arroba_no_path_nao_confunde(self) -> None:
        self.assertEqual(
            sources.mask_uri(_com_credencial("rtsp://host:8554/drone@2")),
            "rtsp://***@host:8554/drone@2")

    def test_endpoint_sem_credencial(self) -> None:
        self.assertEqual(
            sources.endpoint_parts(_com_credencial(URI_CAMERA)),
            {"host": "192.168.59.108", "port": 5543, "path": "/live/channel0"})
        self.assertEqual(sources.endpoint_parts("rtsp://host/x")["port"], 554)


class IntegracaoLeveTest(unittest.TestCase):
    """Guardas de contrato entre o YAML versionado e o pipeline (E2-T2)."""

    RAIZ = Path(__file__).resolve().parents[2]

    def test_sources_yaml_do_repositorio_carrega(self) -> None:
        fontes = sources.load_sources(
            sources.DEFAULT_CONFIG_PATH,
            env={"RTSP_URI": URI_CAMERA, "MTX_HOST": "mediamtx"})
        self.assertEqual([f.id for f in fontes], ["camera", "drone", "phone"])
        self.assertEqual(fontes.default_source, "camera")
        codecs = {f.id: f.codec for f in fontes}
        self.assertEqual(codecs, {"camera": "h265", "drone": "h264",
                                  "phone": "h264"})
        # D3: drone não entra no gate de latência.
        self.assertTrue(fontes[1].gate["report_only"])
        self.assertEqual(fontes[0].gate["latency_p95_ms"], 300)

    def test_pipeline_source_wiring_fica_no_caso_de_integracao(self) -> None:
        """A guarda do pipeline mora em `test_pipeline_source_wiring.py`.

        Separada de propósito: este arquivo é de E2-T1 (módulo puro) e a guarda
        de fiação é de E2-T2, que é o commit que toca `pipeline_real.py`.
        """
        vizinho = Path(__file__).with_name("test_pipeline_source_wiring.py")
        self.assertTrue(vizinho.is_file())

    def test_sources_py_e_puro_sem_gstreamer(self) -> None:
        codigo = (self.RAIZ / "deepstream" / "sources.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import gi", codigo)
        self.assertNotIn("import pyds", codigo)


if __name__ == "__main__":
    unittest.main()
