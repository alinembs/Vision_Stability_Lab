"""Testes das funções puras da sonda RTSP (`scripts/probe/rtsp_read_probe.py`).

Só lógica: cálculo de gaps, fps medido na janela, decisão PASS/FAIL e parsing
de argumentos. Nada aqui abre socket nem toca em GStreamer — a bancada de
verdade é a validação de E0-T3 (publisher + probe contra o MediaMTX).

Nota: não existe `scripts/tests/__init__.py` de propósito. `deepstream/tests`
já é importado pelo pytest como pacote `tests` (o avô `deepstream/` não tem
`__init__.py`); criar outro `tests` em `scripts/` colidiria o nome do módulo
("import file mismatch"). Por isso o import abaixo usa o caminho do diretório.
"""

import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probe"))

import rtsp_read_probe as probe  # noqa: E402
import whip_probe  # noqa: E402

# Credencial fictícia montada por partes: a guarda da DIV-001
# (`deepstream/tests/test_no_hardcoded_secrets.py`) procura `usuario:senha@`
# literal em arquivo versionado — aqui o par é montado em tempo de execução.
USUARIO_FALSO = "admin"
SENHA_FALSA = "senhaFalsa123"
# Hosts de fixture em 192.0.2.0/24 (RFC 5737, TEST-NET-1) de propósito: fixture
# não pode coincidir com o IP de LAN de um host real, senão um teste que passe a
# resolver `host_ip.sh` passa por coincidência. Vale para todas as URLs abaixo.
URL_SEM_CREDENCIAL = "rtsp://192.0.2.10:8555/drone"


def com_credencial(url: str, usuario: str = USUARIO_FALSO, senha: str = SENHA_FALSA) -> str:
    """Devolve a URL com `usuario:senha@` inserido antes do host."""
    esquema, resto = url.split("://", 1)
    return esquema + "://" + usuario + ":" + senha + "@" + resto


URL_COM_CREDENCIAL = com_credencial(URL_SEM_CREDENCIAL)


class TestComputeGaps(unittest.TestCase):
    def test_sem_frames_suficientes_volta_zerado(self):
        self.assertEqual(
            probe.compute_gaps([]),
            {"gaps_over_500ms": 0, "max_gap_ms": 0.0, "avg_gap_ms": 0.0},
        )
        self.assertEqual(probe.compute_gaps([1.0])["gaps_over_500ms"], 0)

    def test_fps_constante_nao_gera_gap(self):
        # 30 fps por 1 s -> 30 frames, gaps ~33,3 ms
        stamps = [i / 30.0 for i in range(31)]
        gaps = probe.compute_gaps(stamps)
        self.assertEqual(gaps["gaps_over_500ms"], 0)
        self.assertAlmostEqual(gaps["max_gap_ms"], 33.333, places=2)
        self.assertAlmostEqual(gaps["avg_gap_ms"], 33.333, places=2)

    def test_conta_apenas_gaps_acima_do_limiar(self):
        # 3 frames, depois buraco de 2 s, depois 2 frames
        stamps = [0.0, 0.033, 0.066, 2.066, 2.099]
        gaps = probe.compute_gaps(stamps)
        self.assertEqual(gaps["gaps_over_500ms"], 1)
        self.assertAlmostEqual(gaps["max_gap_ms"], 2000.0, places=1)
        esperado_avg = (33 + 33 + 2000 + 33) / 4
        self.assertAlmostEqual(gaps["avg_gap_ms"], esperado_avg, places=1)

    def test_limiar_e_estrito(self):
        # exatamente 500 ms não conta como "over"
        self.assertEqual(probe.compute_gaps([0.0, 0.5])["gaps_over_500ms"], 0)
        self.assertEqual(probe.compute_gaps([0.0, 0.501])["gaps_over_500ms"], 1)

    def test_limiar_configuravel(self):
        gaps = probe.compute_gaps([0.0, 0.2], threshold_ms=100.0)
        self.assertEqual(gaps["gaps_over_500ms"], 1)


class TestMeasureFps(unittest.TestCase):
    def test_menos_de_dois_frames_e_zero(self):
        self.assertEqual(probe.measure_fps([]), 0.0)
        self.assertEqual(probe.measure_fps([1.0]), 0.0)

    def test_janela_de_um_segundo_a_30fps(self):
        stamps = [i / 30.0 for i in range(31)]
        self.assertAlmostEqual(probe.measure_fps(stamps), 30.0, places=1)

    def test_janela_degenerada_nao_divide_por_zero(self):
        self.assertEqual(probe.measure_fps([2.0, 2.0]), 0.0)


class TestDecide(unittest.TestCase):
    def _decide(self, **kwargs):
        base = dict(
            frames=300, fps=30.0, first_frame_s=0.4, min_frames=10,
            min_fps=5.0, seconds=10.0, error=None,
        )
        base.update(kwargs)
        return probe.decide(**base)

    def test_pass(self):
        self.assertEqual(self._decide(), ("PASS", "ok"))

    def test_sem_frame_e_fail_no_frames(self):
        self.assertEqual(
            self._decide(frames=0, fps=0.0, first_frame_s=None),
            ("FAIL", "no_frames"),
        )

    def test_frames_abaixo_do_minimo(self):
        result, reason = self._decide(frames=4, first_frame_s=0.1)
        self.assertEqual(result, "FAIL")
        self.assertIn("frames_below_min", reason)

    def test_fps_abaixo_do_minimo(self):
        result, reason = self._decide(fps=2.0)
        self.assertEqual(result, "FAIL")
        self.assertIn("fps_below_min", reason)

    def test_primeiro_frame_depois_da_janela(self):
        result, reason = self._decide(first_frame_s=11.0)
        self.assertEqual(result, "FAIL")
        self.assertEqual(reason, "first_frame_too_late")

    def test_erro_de_pipeline_tem_prioridade(self):
        result, reason = self._decide(error="Could not read from resource")
        self.assertEqual(result, "FAIL")
        self.assertTrue(reason.startswith("pipeline_error: "))

    def test_erro_de_pipeline_reprova_mesmo_com_frames_suficientes(self):
        self.assertEqual(
            self._decide(error="connection reset")[0], "FAIL"
        )


class TestDecideGaps(unittest.TestCase):
    """I1: gaps > 500 ms gateiam o PASS (critério de estabilidade do drone)."""

    def _decide(self, **kwargs):
        base = dict(
            frames=300, fps=30.0, first_frame_s=0.4, min_frames=10,
            min_fps=5.0, seconds=10.0, error=None,
        )
        base.update(kwargs)
        return probe.decide(**base)

    def test_zero_gaps_default_e_pass(self):
        self.assertEqual(self._decide(gaps_over_500ms=0), ("PASS", "ok"))

    def test_default_reprova_qualquer_gap(self):
        result, reason = self._decide(gaps_over_500ms=1)
        self.assertEqual(result, "FAIL")
        self.assertEqual(reason, "too_many_gaps (1 > 0)")

    def test_no_limiar_e_pass(self):
        self.assertEqual(
            self._decide(gaps_over_500ms=4, max_gaps=4), ("PASS", "ok")
        )

    def test_acima_do_limiar_e_fail(self):
        result, reason = self._decide(gaps_over_500ms=5, max_gaps=4)
        self.assertEqual(result, "FAIL")
        self.assertIn("too_many_gaps", reason)
        self.assertIn("5 > 4", reason)

    def test_gate_desligado_com_menos_um(self):
        self.assertEqual(
            self._decide(gaps_over_500ms=99, max_gaps=-1), ("PASS", "ok")
        )

    def test_gap_do_publisher_a_2fps_reprova_com_gate_ligado(self):
        # 2 fps por 10 s: gaps de 500 ms + buracos -> reprova no gate default.
        stamps = [0.0, 0.5, 1.0, 2.6, 3.1, 4.9]
        gaps = probe.compute_gaps(stamps)
        self.assertEqual(gaps["gaps_over_500ms"], 2)
        self.assertEqual(self._decide(gaps_over_500ms=gaps["gaps_over_500ms"])[0], "FAIL")
        self.assertEqual(
            self._decide(gaps_over_500ms=gaps["gaps_over_500ms"], max_gaps=-1)[0],
            "PASS",
        )

    def test_no_video_pad_tem_motivo_proprio(self):
        result, reason = self._decide(no_video_pad=True)
        self.assertEqual((result, reason), ("FAIL", "no_video_pad"))

    def test_erro_de_pipeline_tem_prioridade_sobre_no_video_pad(self):
        result, reason = self._decide(error="boom", no_video_pad=True)
        self.assertEqual(result, "FAIL")
        self.assertTrue(reason.startswith("pipeline_error: "))


class TestSanitizeUrl(unittest.TestCase):
    """B1: a credencial da câmera não pode aparecer em nenhuma saída."""

    def test_mascara_usuario_e_senha(self):
        saida = probe.sanitize_url(URL_COM_CREDENCIAL)
        self.assertEqual(saida, "rtsp://***@192.0.2.10:8555/drone")
        self.assertNotIn(SENHA_FALSA, saida)
        self.assertNotIn(USUARIO_FALSO, saida)

    def test_preserva_host_porta_e_path(self):
        saida = probe.sanitize_url(URL_COM_CREDENCIAL)
        self.assertIn("192.0.2.10", saida)
        self.assertIn("8555", saida)
        self.assertIn("/drone", saida)

    def test_url_sem_credencial_fica_inalterada(self):
        self.assertEqual(probe.sanitize_url(URL_SEM_CREDENCIAL), URL_SEM_CREDENCIAL)
        self.assertEqual(probe.sanitize_url("rtsps://host/p"), "rtsps://host/p")

    def test_url_sem_path(self):
        self.assertEqual(
            probe.sanitize_url(com_credencial("rtsp://host:8554")),
            "rtsp://***@host:8554",
        )

    def test_arroba_no_path_nao_e_userinfo(self):
        self.assertEqual(
            probe.sanitize_url("rtsp://host:8554/drone@2"),
            "rtsp://host:8554/drone@2",
        )

    def test_senha_com_arroba_usa_o_ultimo(self):
        self.assertEqual(
            probe.sanitize_url(com_credencial("rtsp://host:8554/drone", senha="p@ss")),
            "rtsp://***@host:8554/drone",
        )

    def test_split_userinfo(self):
        self.assertEqual(
            probe.split_userinfo(URL_COM_CREDENCIAL), f"{USUARIO_FALSO}:{SENHA_FALSA}"
        )
        self.assertIsNone(probe.split_userinfo(URL_SEM_CREDENCIAL))
        self.assertIsNone(probe.split_userinfo("sem-esquema/USUARIO:SENHA@host"))


class TestScrubText(unittest.TestCase):
    """B1: erros do GStreamer que ecoam a location também são mascarados."""

    def test_remove_userinfo_de_mensagem(self):
        texto = f"Could not open resource {URL_COM_CREDENCIAL}"
        saida = probe.scrub_text(texto, URL_COM_CREDENCIAL)
        self.assertNotIn(SENHA_FALSA, saida)
        self.assertIn("rtsp://***@192.0.2.10:8555/drone", saida)

    def test_remove_senha_sozinha(self):
        saida = probe.scrub_text(
            f"401 Unauthorized ({SENHA_FALSA})", URL_COM_CREDENCIAL
        )
        self.assertNotIn(SENHA_FALSA, saida)

    def test_sem_credencial_nao_altera(self):
        texto = "connection reset by peer"
        self.assertEqual(probe.scrub_text(texto, URL_SEM_CREDENCIAL), texto)


class TestClassifyPad(unittest.TestCase):
    """B2: pad de áudio não pode reprovar a fonte; codec só vale para vídeo."""

    def test_audio_e_ignorado_sem_erro(self):
        # A câmera do projeto manda PCMA junto do vídeo (pipeline_real.py).
        self.assertEqual(probe.classify_pad("audio", "PCMA"), (probe.PAD_IGNORE, None))
        self.assertEqual(probe.classify_pad("audio", "MPEG4-GENERIC")[0], probe.PAD_IGNORE)

    def test_application_e_ignorado(self):
        self.assertEqual(probe.classify_pad("application", "VND.ONVIF.METADATA")[0], probe.PAD_IGNORE)

    def test_media_ausente_e_ignorado(self):
        self.assertEqual(probe.classify_pad(None, "H264")[0], probe.PAD_IGNORE)
        self.assertEqual(probe.classify_pad("", "H264")[0], probe.PAD_IGNORE)

    def test_video_h264_e_h265_vinculam(self):
        self.assertEqual(probe.classify_pad("video", "H264"), (probe.PAD_LINK, "h264"))
        self.assertEqual(probe.classify_pad("video", "H265"), (probe.PAD_LINK, "h265"))
        self.assertEqual(probe.classify_pad("video", "hevc"), (probe.PAD_LINK, "h265"))

    def test_video_com_codec_desconhecido_e_nao_suportado(self):
        self.assertEqual(
            probe.classify_pad("video", "VP8"), (probe.PAD_UNSUPPORTED, None)
        )
        self.assertEqual(
            probe.classify_pad("video", None), (probe.PAD_UNSUPPORTED, None)
        )

    def test_audio_com_codec_de_video_continua_ignorado(self):
        # `encoding-name` sozinho não basta: o gate é o `media`.
        self.assertEqual(probe.classify_pad("audio", "H264")[0], probe.PAD_IGNORE)


class TestPadAddedDoRtspsrc(unittest.TestCase):
    """B2: o handler `pad-added` não aborta em pad de áudio (sem rede/GStreamer).

    Os objetos abaixo só imitam a forma que `_on_pad_added` consome
    (`query_caps` → `get_size`/`get_structure` → `get_name`/`has_field`/`get_value`).
    """

    class _Estrutura:
        def __init__(self, nome, campos):
            self._nome = nome
            self._campos = campos

        def get_name(self):
            return self._nome

        def has_field(self, campo):
            return campo in self._campos

        def get_value(self, campo):
            return self._campos.get(campo)

    class _Caps:
        def __init__(self, estrutura):
            self._estrutura = estrutura

        def get_size(self):
            return 1

        def get_structure(self, indice):
            return self._estrutura

    class _Pad:
        def __init__(self, caps):
            self._caps = caps

        def query_caps(self, _filtro):
            return self._caps

    def _pad(self, **campos):
        return self._Pad(
            self._Caps(self._Estrutura("application/x-rtp", campos))
        )

    def test_pad_de_audio_nao_aborta(self):
        sonda = probe.RtspReadProbe("rtsp://host:8554/drone")
        sonda._on_pad_added(None, self._pad(media="audio", **{"encoding-name": "PCMA"}))
        self.assertIsNone(sonda.result.error)
        self.assertFalse(sonda._linked)
        self.assertIsNone(sonda.result.codec)

    def test_pad_de_video_com_codec_ruim_aborta(self):
        sonda = probe.RtspReadProbe("rtsp://host:8554/drone")
        sonda._on_pad_added(None, self._pad(media="video", **{"encoding-name": "VP8"}))
        self.assertIn("codec não suportado", sonda.result.error or "")

    def test_pad_que_nao_e_rtp_e_ignorado(self):
        sonda = probe.RtspReadProbe("rtsp://host:8554/drone")
        pad = self._Pad(self._Caps(self._Estrutura("video/x-raw", {"media": "video"})))
        sonda._on_pad_added(None, pad)
        self.assertIsNone(sonda.result.error)
        self.assertFalse(sonda._linked)


class TestArgsEUrl(unittest.TestCase):
    def test_url_valida(self):
        self.assertEqual(
            probe.validate_url("  rtsp://192.0.2.10:8554/drone  "),
            "rtsp://192.0.2.10:8554/drone",
        )

    def test_url_vazia(self):
        with self.assertRaises(ValueError):
            probe.validate_url("")

    def test_url_sem_esquema(self):
        with self.assertRaises(ValueError):
            probe.validate_url("192.0.2.10:8554/drone")

    def test_url_com_esquema_errado(self):
        with self.assertRaises(ValueError):
            probe.validate_url("http://192.0.2.10:8888/live")

    def test_url_sem_host(self):
        with self.assertRaises(ValueError):
            probe.validate_url("rtsp:///drone")

    def test_transporte(self):
        self.assertEqual(probe.transport_mask("tcp"), 4)
        self.assertEqual(probe.transport_mask("UDP"), 1)
        with self.assertRaises(ValueError):
            probe.transport_mask("http")

    def test_codec_factories(self):
        self.assertEqual(probe.codec_factories("H264"), ("rtph264depay", "h264parse"))
        self.assertEqual(probe.codec_factories("H265"), ("rtph265depay", "h265parse"))
        self.assertEqual(probe.codec_factories("hevc"), ("rtph265depay", "h265parse"))
        self.assertIsNone(probe.codec_factories("VP8"))
        self.assertIsNone(probe.codec_factories(None))

    def test_parse_args_defaults(self):
        args = probe.parse_args(["--url", "rtsp://127.0.0.1:8554/drone"])
        self.assertEqual(args.seconds, 10.0)
        self.assertEqual(args.transport, "tcp")
        self.assertEqual(args.min_fps, 5.0)
        self.assertEqual(args.min_frames, 10)
        self.assertEqual(args.max_gaps, 0)
        self.assertFalse(args.json)
        self.assertEqual(args.protocols, 4)

    def test_parse_args_sobrescreve(self):
        args = probe.parse_args(
            [
                "--url", "rtsp://host/drone",
                "--seconds", "5",
                "--transport", "udp",
                "--min-fps", "12.5",
                "--min-frames", "3",
                "--json",
                "--max-gaps", "-1",
            ]
        )
        self.assertEqual(args.seconds, 5.0)
        self.assertEqual(args.protocols, 1)
        self.assertEqual(args.min_fps, 12.5)
        self.assertEqual(args.min_frames, 3)
        self.assertEqual(args.max_gaps, -1)
        self.assertTrue(args.json)

    def test_parse_args_rejeita_max_gaps_invalido(self):
        with self.assertRaises(SystemExit) as ctx:
            probe.parse_args(["--url", "rtsp://h/p", "--max-gaps", "-2"])
        self.assertEqual(ctx.exception.code, 2)  # 2 = uso incorreto (M2)

    def test_parse_args_rejeita_url_invalida(self):
        with self.assertRaises(SystemExit) as ctx:
            probe.parse_args(["--url", "http://x/live"])
        self.assertEqual(ctx.exception.code, 2)

    def test_parse_args_nao_vaza_credencial_no_erro(self):
        # B1: a mensagem de erro de argumento também é saída.
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            with self.assertRaises(SystemExit):
                probe.parse_args(["--url", com_credencial("http://host/live")])
        self.assertNotIn(SENHA_FALSA, buffer.getvalue())
        self.assertIn("***@host/live", buffer.getvalue())

    def test_parse_args_rejeita_seconds_nao_positivo(self):
        with self.assertRaises(SystemExit):
            probe.parse_args(["--url", "rtsp://h/p", "--seconds", "0"])


class TestBuildReport(unittest.TestCase):
    def test_relatorio_de_stream_saudavel(self):
        result = probe.ProbeResult(codec="h264")
        for i in range(120):
            result.add_frame(i / 30.0)
        report = probe.build_report(
            url="rtsp://host:8554/drone",
            result=result,
            seconds=10.0,
            min_frames=10,
            min_fps=5.0,
            elapsed_s=4.0,
        )
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["codec"], "h264")
        self.assertEqual(report["container"], "rtsp")
        self.assertEqual(report["frames"], 120)
        self.assertAlmostEqual(report["fps"], 30.0, places=1)
        self.assertEqual(report["gaps_over_500ms"], 0)
        for chave in (
            "url", "codec", "container", "first_frame_s", "frames", "fps",
            "gaps_over_500ms", "max_gaps", "max_gap_ms", "avg_gap_ms",
            "duration_s", "result", "reason",
        ):
            self.assertIn(chave, report)

    def test_relatorio_nao_expoe_credencial(self):
        # B1: o JSON é consumido e logado pelo switch_source.sh.
        result = probe.ProbeResult(codec="h265")
        for i in range(60):
            result.add_frame(i / 30.0)
        report = probe.build_report(
            url=URL_COM_CREDENCIAL,
            result=result,
            seconds=3.0,
            min_frames=10,
            min_fps=5.0,
            elapsed_s=2.0,
        )
        self.assertEqual(report["url"], "rtsp://***@192.0.2.10:8555/drone")
        self.assertNotIn(SENHA_FALSA, str(report))

    def test_relatorio_reprova_por_gaps(self):
        # I1: 2 fps com buracos > 500 ms passa no fps mínimo e reprova nos gaps.
        result = probe.ProbeResult(codec="h264")
        for stamp in (0.0, 0.5, 1.0, 2.6, 3.1, 4.9):
            result.add_frame(stamp)
        report = probe.build_report(
            url="rtsp://host:8554/drone",
            result=result,
            seconds=5.0,
            min_frames=5,
            min_fps=1.0,
            elapsed_s=5.0,
        )
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("too_many_gaps", report["reason"])
        self.assertEqual(report["gaps_over_500ms"], 2)
        self.assertEqual(report["max_gaps"], 0)

    def test_relatorio_aceita_gaps_com_limite(self):
        result = probe.ProbeResult(codec="h264")
        for stamp in (0.0, 0.5, 1.0, 2.6, 3.1, 4.9):
            result.add_frame(stamp)
        report = probe.build_report(
            url="rtsp://host:8554/drone",
            result=result,
            seconds=5.0,
            min_frames=5,
            min_fps=1.0,
            elapsed_s=5.0,
            max_gaps=-1,
        )
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["max_gaps"], -1)

    def test_relatorio_de_pad_sem_video(self):
        result = probe.ProbeResult(no_video_pad=True)
        report = probe.build_report(
            url="rtsp://host:8554/drone",
            result=result,
            seconds=5.0,
            min_frames=10,
            min_fps=5.0,
            elapsed_s=5.0,
        )
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["reason"], "no_video_pad")

    def test_relatorio_de_path_mudo(self):
        result = probe.ProbeResult()
        report = probe.build_report(
            url="rtsp://host:8554/drone",
            result=result,
            seconds=5.0,
            min_frames=10,
            min_fps=5.0,
            elapsed_s=5.0,
        )
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["reason"], "no_frames")
        self.assertIsNone(report["first_frame_s"])


class TestWhipProbe(unittest.TestCase):
    """M3/M4: `whip_probe` é importável e o SDP de resposta é parseado limpo."""

    ANSWER = (
        "v=0\r\n"
        "o=- 0 0 IN IP4 127.0.0.1\r\n"
        "s=-\r\n"
        "t=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "a=ice-ufrag:ufrag\r\n"
        "a=candidate:1 1 UDP 2130706431 192.0.2.10 8189 typ host\r\n"
        "a=candidate:2 1 TCP 2130706431 192.0.2.10 8189 typ host tcptype passive\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "a=rtpmap:97 H265/90000\r\n"
        "a=fmtp:96 packetization-mode=1\r\n"
        "a=end-of-candidates\r\n"
    )

    def test_offer_e_importavel_e_tem_m_video(self):
        sdp = whip_probe.offer()
        self.assertIn("m=video", sdp)
        self.assertIn("a=fingerprint:sha-256", sdp)

    def test_candidatos_sem_cr_colado(self):
        parsed = whip_probe.parse_answer_sdp(self.ANSWER)
        self.assertEqual(parsed["candidate_count"], 2)
        for candidate in parsed["candidates"]:
            self.assertNotIn("\r", candidate)
            self.assertIn("typ host", candidate)
        self.assertEqual(parsed["candidate_hosts"], ["192.0.2.10"])

    def test_codecs_da_resposta_preenchidos(self):
        parsed = whip_probe.parse_answer_sdp(self.ANSWER)
        self.assertEqual(parsed["answer_codecs"], ["96 H264/90000", "97 H265/90000"])

    def test_mline_da_resposta(self):
        parsed = whip_probe.parse_answer_sdp(self.ANSWER)
        self.assertEqual(parsed["answer_media"], "video")
        self.assertTrue(parsed["answer_mline"].startswith("m=video 9"))

    def test_resposta_vazia_nao_quebra(self):
        parsed = whip_probe.parse_answer_sdp("")
        self.assertEqual(parsed["candidates"], [])
        self.assertEqual(parsed["answer_codecs"], [])
        self.assertIsNone(parsed["answer_mline"])


class TestWhipProbeTls(unittest.TestCase):
    """E0-T4: a sonda fala HTTPS e o default não é confiar em qualquer host."""

    def test_default_e_https_no_ip_da_lan(self):
        # O host do default é o IP atual (LAN_IP ou scripts/host_ip.sh), nunca
        # um IP de bancada fixo no código — ver INC-009.
        with patch.dict("os.environ", {"LAN_IP": "10.0.0.7"}):
            args = whip_probe.parse_args([])
        self.assertEqual(args.base, "https://10.0.0.7:8889")
        self.assertEqual(args.path, "phone")
        self.assertFalse(args.insecure)
        self.assertIsNone(args.cacert)

    def test_default_sem_ip_explicito_nao_e_ip_de_bancada(self):
        with patch.dict("os.environ", {"LAN_IP": "", "VSL_LAN_IP": ""}), \
                patch.object(whip_probe, "_host_ip", return_value="172.20.0.9"):
            self.assertEqual(whip_probe.default_base(), "https://172.20.0.9:8889")

    def test_base_http_nao_monta_contexto(self):
        # Instalação sem `webrtcEncryption`: nada de TLS no caminho.
        self.assertIsNone(whip_probe.build_ssl_context("http://192.0.2.10:8889"))

    def test_cacert_verifica_o_servidor(self):
        contexto = whip_probe.build_ssl_context(
            "https://192.0.2.10:8889", cacert="deploy/certs/ca.crt"
        )
        self.assertIsNotNone(contexto)
        self.assertEqual(contexto.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexto.check_hostname)

    def test_insecure_desliga_verificacao(self):
        contexto = whip_probe.build_ssl_context(
            "https://192.0.2.10:8889", insecure=True
        )
        self.assertEqual(contexto.verify_mode, ssl.CERT_NONE)
        self.assertFalse(contexto.check_hostname)

    def test_sem_cacert_usa_o_store_do_sistema(self):
        # Fail closed: a CA interna do VSL não está no store, então a sonda
        # recusa o servidor em vez de "quase" validar.
        contexto = whip_probe.build_ssl_context("https://192.0.2.10:8889")
        self.assertEqual(contexto.verify_mode, ssl.CERT_REQUIRED)

    def test_cacert_inexistente_levanta_em_vez_de_passar(self):
        with self.assertRaises((OSError, ssl.SSLError)):
            whip_probe.build_ssl_context(
                "https://192.0.2.10:8889", cacert="/caminho/que/nao/existe.crt"
            )

    def test_tls_label_distingue_os_modos(self):
        self.assertEqual(whip_probe.tls_label("http://x:8889"), "off")
        self.assertEqual(whip_probe.tls_label("https://x:8889"), "verify:sistema")
        self.assertEqual(
            whip_probe.tls_label("https://x:8889", cacert="deploy/certs/ca.crt"),
            "verify:deploy/certs/ca.crt",
        )
        self.assertEqual(whip_probe.tls_label("https://x:8889", insecure=True), "insecure")

    def test_cacert_e_insecure_sao_excludentes(self):
        with self.assertRaises(SystemExit):
            whip_probe.parse_args(["--cacert", "x.crt", "--insecure"])

    def test_base_sobrescreve_o_default(self):
        args = whip_probe.parse_args(["--base", "https://10.0.0.5:8889"])
        self.assertEqual(args.base, "https://10.0.0.5:8889")


class TestWhipProbeExitCodes(unittest.TestCase):
    """I6: o exit code tem de casar com o que o `--help` promete.

    O epilog dizia "0 = sessão aceita (2xx **e** DELETE executado)", mas
    `main()` saía 0 com `delete.ok: false` — um path publicado passava por
    sucesso em qualquer script que só olhasse o exit code.
    """

    POST_OK = {
        "ok": True,
        "status": 201,
        "headers": {"Location": "/phone/whip/abc"},
        "body": "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n",
    }
    DELETE_OK = {"ok": True, "status": 200}
    BASE = ["--base", "http://127.0.0.1:8889"]

    def _main(self, argv, post=None, delete=None):
        import contextlib
        import io
        import json

        buffer = io.StringIO()
        with patch.object(whip_probe, "post_offer",
                          post or (lambda *a, **k: self.POST_OK)), \
                patch.object(whip_probe, "delete_session",
                             delete or (lambda *a, **k: self.DELETE_OK)), \
                contextlib.redirect_stdout(buffer):
            code = whip_probe.main(self.BASE + argv)
        return code, (json.loads(buffer.getvalue()) if buffer.getvalue() else None)

    def test_sessao_aceita_e_encerrada_sai_zero(self):
        code, report = self._main(["--path", "phone"])
        self.assertEqual(code, 0)
        self.assertTrue(report["delete"]["ok"])
        self.assertEqual(report["status"], 201)

    def test_delete_falhando_sai_quatro(self):
        code, report = self._main(
            ["--path", "phone"],
            delete=lambda *a, **k: {"ok": False, "status": 500, "error": "boom"})
        self.assertEqual(code, 4)
        self.assertFalse(report["delete"]["ok"])
        # o relatório continua provando a sessão (o POST foi 2xx)
        self.assertEqual(report["status"], 201)

    def test_sem_location_sai_quatro(self):
        """Sessão não identificada = não encerrada: mesmo veredito do DELETE."""
        post = dict(self.POST_OK, headers={})
        code, report = self._main(["--path", "phone"], post=lambda *a, **k: post)
        self.assertEqual(code, 4)
        self.assertFalse(report["delete"]["ok"])
        self.assertIn("Location", report["delete"]["error"])

    def test_keep_session_sai_zero_e_marca_skip(self):
        def _nao_deve_apagar(*a, **k):
            raise AssertionError("DELETE não pode ser enviado com --keep-session")

        code, report = self._main(["--path", "phone", "--keep-session"],
                                  delete=_nao_deve_apagar)
        self.assertEqual(code, 0)
        self.assertIsNone(report["delete"])
        self.assertIn("keep-session", report["delete_skipped"])

    def test_post_recusado_sai_dois(self):
        code, report = self._main(
            ["--path", "phone"],
            post=lambda *a, **k: {"ok": False, "status": 409,
                                  "error": "stream is already available"})
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], 409)
        self.assertEqual(report["tls"], "off")


if __name__ == "__main__":
    unittest.main()
