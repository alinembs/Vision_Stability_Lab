"""Testes da lógica pura do relatório de ingestão.

Só lógica: resolução a partir das caps, bitrate, fps, gaps, veredito e
renderização da ficha. Nada aqui abre socket nem toca em GStreamer — a bancada
de verdade é rodar `ingest_report.py` contra o path do publisher sintético.

Mesma nota de `test_probe_logic.py`: não existe `scripts/tests/__init__.py`; o
import usa o caminho do diretório para não colidir com `deepstream/tests`.
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probe"))

import ingest_report_lib as lib  # noqa: E402

# Credencial fictícia montada por partes: a guarda da DIV-001
# (`deepstream/tests/test_no_hardcoded_secrets.py`) procura `usuario:senha@`
# literal em arquivo versionado.
USUARIO_FALSO = "admin"
SENHA_FALSA = "senhaFalsa123"
URL_LIMPA = "rtsp://192.168.59.102:8554/drone"
URL_COM_CREDENCIAL = (
    "rtsp://" + USUARIO_FALSO + ":" + SENHA_FALSA + "@192.168.59.108:5543/live/channel0"
)


class TestUrl(unittest.TestCase):
    def test_sanitiza_credencial_preservando_host_porta_e_path(self):
        self.assertEqual(
            lib.sanitize_url(URL_COM_CREDENCIAL),
            "rtsp://***@192.168.59.108:5543/live/channel0",
        )

    def test_url_sem_credencial_volta_inalterada(self):
        self.assertEqual(lib.sanitize_url(URL_LIMPA), URL_LIMPA)

    def test_scrub_remove_a_senha_de_texto_livre(self):
        texto = f"falha em rtsp://{USUARIO_FALSO}:{SENHA_FALSA}@host/path"
        limpo = lib.scrub_text(texto, URL_COM_CREDENCIAL)
        self.assertNotIn(SENHA_FALSA, limpo)
        self.assertIn("***", limpo)

    def test_valida_url_aceita_rtsp(self):
        self.assertEqual(lib.validate_url("  " + URL_LIMPA + "  "), URL_LIMPA)
        self.assertEqual(
            lib.validate_url("rtsps://host:8554/path"), "rtsps://host:8554/path"
        )

    def test_valida_url_reprova_entradas_invalidas(self):
        for ruim in ("", "http://host:8554/path", "rtsp:///path", "host:8554/drone"):
            with self.subTest(url=ruim):
                with self.assertRaises(ValueError):
                    lib.validate_url(ruim)

    def test_transporte(self):
        self.assertEqual(lib.transport_mask("tcp"), 4)
        self.assertEqual(lib.transport_mask("UDP"), 1)
        with self.assertRaises(ValueError):
            lib.transport_mask("http")


class TestPadsECodec(unittest.TestCase):
    def test_video_h264_e_h265_linkam(self):
        self.assertEqual(lib.classify_pad("video", "H264"), (lib.PAD_LINK, "h264"))
        self.assertEqual(lib.classify_pad("video", "H265"), (lib.PAD_LINK, "h265"))
        self.assertEqual(lib.classify_pad("video", "HEVC"), (lib.PAD_LINK, "h265"))

    def test_audio_tem_acao_propria(self):
        self.assertEqual(lib.classify_pad("audio", "PCMA"), (lib.PAD_AUDIO, None))

    def test_outros_pads_sao_ignorados(self):
        self.assertEqual(
            lib.classify_pad("application", "foo"), (lib.PAD_IGNORE, None)
        )

    def test_codec_de_video_fora_do_contrato_reprova(self):
        self.assertEqual(
            lib.classify_pad("video", "H266"), (lib.PAD_UNSUPPORTED, None)
        )

    def test_codec_factories(self):
        self.assertEqual(
            lib.codec_factories("H264"), ("rtph264depay", "h264parse")
        )
        self.assertEqual(
            lib.codec_factories("hevc"), ("rtph265depay", "h265parse")
        )
        self.assertIsNone(lib.codec_factories("AV1"))
        self.assertIsNone(lib.codec_factories(None))

    def test_rotulo_do_codec_para_a_ficha(self):
        self.assertEqual(lib.codec_label("h264", "H264"), "H.264")
        self.assertEqual(lib.codec_label("h265", "HEVC"), "H.265 (HEVC)")
        self.assertEqual(lib.codec_label(None, None), "desconhecido")


class TestMensagemDeErro(unittest.TestCase):
    def test_extrai_o_motivo_real_do_debug(self):
        debug = (
            "../gst/rtsp/gstrtspsrc.c(6795): gst_rtspsrc_send (): "
            "/GstPipeline:p/GstRTSPSrc:src:\nBad Request (400)"
        )
        self.assertEqual(lib.summarize_gst_debug(debug), "Bad Request (400)")

    def test_debug_so_com_localizacao_nao_rende_motivo(self):
        debug = "../gst/rtsp/gstrtspsrc.c(6795): gst_rtspsrc_send (): /p/src:"
        self.assertEqual(lib.summarize_gst_debug(debug), "")

    def test_debug_ausente(self):
        self.assertEqual(lib.summarize_gst_debug(None), "")
        self.assertEqual(lib.summarize_gst_debug(""), "")

    def test_motivo_longo_e_truncado(self):
        motivo = lib.summarize_gst_debug("x" * 500, limit=20)
        self.assertEqual(len(motivo), 20)


class TestMetricas(unittest.TestCase):
    def test_fps_por_timestamps(self):
        timestamps = [0.0, 0.04, 0.08, 0.12]
        self.assertAlmostEqual(lib.measure_fps(timestamps), 25.0, places=3)

    def test_fps_sem_dois_frames_e_zero(self):
        self.assertEqual(lib.measure_fps([]), 0.0)
        self.assertEqual(lib.measure_fps([1.0]), 0.0)

    def test_gaps_detectam_o_buraco(self):
        gaps = lib.compute_gaps([0.0, 0.04, 1.04])
        self.assertEqual(gaps["gaps_over_500ms"], 1)
        self.assertAlmostEqual(gaps["max_gap_ms"], 1000.0, places=3)

    def test_gaps_sem_dois_frames_zeram(self):
        self.assertEqual(
            lib.compute_gaps([]),
            {"gaps_over_500ms": 0, "max_gap_ms": 0.0, "avg_gap_ms": 0.0},
        )

    def test_bitrate_medio(self):
        # 1 MB em 10 s = 800 kbps
        self.assertAlmostEqual(
            lib.compute_bitrate_kbps(1_000_000, 10.0), 800.0, places=1
        )

    def test_bitrate_com_janela_invalida_nao_divide_por_zero(self):
        self.assertEqual(lib.compute_bitrate_kbps(1_000_000, 0.0), 0.0)
        self.assertEqual(lib.compute_bitrate_kbps(1_000_000, -1.0), 0.0)
        self.assertEqual(lib.compute_bitrate_kbps(0, 10.0), 0.0)


class TestResolucao(unittest.TestCase):
    def test_formata_resolucao(self):
        self.assertEqual(lib.format_resolution(1920, 1080), "1920x1080")

    def test_resolucao_incompleta_vira_none(self):
        self.assertIsNone(lib.format_resolution(None, 1080))
        self.assertIsNone(lib.format_resolution(0, 720))
        self.assertIsNone(lib.format_resolution("x", 720))

    def test_classifica_o_que_o_app_oferece(self):
        self.assertEqual(lib.resolution_class("1280x720"), "720p")
        self.assertEqual(lib.resolution_class("1920x1080"), "1080p")
        self.assertEqual(lib.resolution_class("640x480"), "640x480")
        self.assertEqual(lib.resolution_class(None), "desconhecida")


class TestVereditoERelatorio(unittest.TestCase):
    def test_veredito_ok_com_video(self):
        self.assertEqual(
            lib.verdict(frames=10, first_frame_s=0.3, seconds=20.0), ("OK", "ok")
        )

    def test_veredito_sem_frame(self):
        self.assertEqual(
            lib.verdict(frames=0, first_frame_s=None, seconds=20.0)[0], "FAIL"
        )

    def test_veredito_sem_pad_de_video(self):
        self.assertEqual(
            lib.verdict(
                frames=0, first_frame_s=None, seconds=5.0, no_video_pad=True
            ),
            ("FAIL", "no_video_pad"),
        )

    def test_erro_tem_precedencia(self):
        outcome, reason = lib.verdict(
            frames=10, first_frame_s=0.1, seconds=5.0, error="boom"
        )
        self.assertEqual(outcome, "FAIL")
        self.assertIn("boom", reason)

    def test_relatorio_completo(self):
        timestamps = [0.0, 0.04, 0.08]
        report = lib.build_report(
            url=URL_COM_CREDENCIAL,
            codec="h264",
            encoding_name="H264",
            timestamps_s=timestamps,
            rtp_bytes=500_000,
            audio_tracks=0,
            seconds=20.0,
            elapsed_s=20.1,
            width=1920,
            height=1080,
        )
        self.assertEqual(report["result"], "OK")
        self.assertEqual(report["resolution"], "1920x1080")
        self.assertEqual(report["resolution_class"], "1080p")
        self.assertEqual(report["frames"], 3)
        self.assertAlmostEqual(report["fps"], 25.0, places=3)
        self.assertAlmostEqual(report["window_s"], 0.08, places=3)
        self.assertAlmostEqual(
            report["bitrate_kbps"], 500_000 * 8 / 0.08 / 1000, places=1
        )
        self.assertNotIn(SENHA_FALSA, report["url"])
        self.assertIn("***", report["url"])

    def test_relatorio_sem_video_e_fail(self):
        report = lib.build_report(
            url=URL_LIMPA,
            codec=None,
            encoding_name=None,
            timestamps_s=[],
            rtp_bytes=0,
            audio_tracks=1,
            seconds=5.0,
            elapsed_s=5.0,
        )
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["codec"], "unknown")
        self.assertEqual(report["bitrate_kbps"], 0.0)
        self.assertIsNone(report["resolution"])

    def test_ts_do_relatorio_e_repassado(self):
        report = lib.build_report(
            url=URL_LIMPA,
            codec="h264",
            encoding_name="H264",
            timestamps_s=[0.0],
            rtp_bytes=0,
            audio_tracks=0,
            seconds=5.0,
            elapsed_s=5.0,
            ts="2026-10-31T21:00:00+00:00",
        )
        self.assertEqual(report["ts"], "2026-10-31T21:00:00+00:00")

    def test_resumo_legivel_funciona_em_relatorio_sem_video(self):
        """O caminho de erro do script monta o mesmo formato: nada de KeyError."""
        report = lib.build_report(
            url=URL_LIMPA,
            codec=None,
            encoding_name=None,
            timestamps_s=[],
            rtp_bytes=0,
            audio_tracks=0,
            seconds=5.0,
            elapsed_s=5.0,
            error="probe_error: boom",
        )
        texto = lib.format_human(report)
        self.assertIn("FAIL", texto)
        self.assertIn("boom", texto)

    def test_ficha_preenche_so_o_medido(self):
        report = lib.build_report(
            url=URL_LIMPA,
            codec="h264",
            encoding_name="H264",
            timestamps_s=[0.0, 0.04, 0.08],
            rtp_bytes=500_000,
            audio_tracks=0,
            seconds=20.0,
            elapsed_s=20.1,
            width=1280,
            height=720,
        )
        ficha = lib.render_ficha(report)
        self.assertIn("H.264", ficha)
        self.assertIn("1280x720 (720p)", ficha)
        self.assertIn("kbps", ficha)
        self.assertNotIn("não medido", ficha)

    def test_ficha_sem_resolucao_declara_nao_medido(self):
        report = lib.build_report(
            url=URL_LIMPA,
            codec=None,
            encoding_name=None,
            timestamps_s=[0.0, 0.04],
            rtp_bytes=10,
            audio_tracks=0,
            seconds=5.0,
            elapsed_s=5.0,
        )
        ficha = lib.render_ficha(report)
        self.assertIn("| Resolução do app | não medido |", ficha)

    def test_ficha_sem_frames_nao_inventa_medicao(self):
        report = lib.build_report(
            url=URL_LIMPA,
            codec=None,
            encoding_name=None,
            timestamps_s=[],
            rtp_bytes=0,
            audio_tracks=0,
            seconds=5.0,
            elapsed_s=5.0,
            error="probe_error: boom",
        )
        ficha = lib.render_ficha(report)
        self.assertIn("| Codec real | não medido |", ficha)
        self.assertIn("| FPS do app | não medido |", ficha)
        self.assertIn("| Bitrate do app | não medido |", ficha)


class TestModuloPuro(unittest.TestCase):
    def test_importa_sem_gi(self):
        """O módulo puro não pode puxar `gi` (requisito do plano §7.1)."""
        code = (
            "import sys; sys.path.insert(0, "
            f"{str(Path(__file__).resolve().parents[1] / 'probe')!r}); "
            "import ingest_report_lib; "
            "sys.exit(0 if 'gi' not in sys.modules else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
