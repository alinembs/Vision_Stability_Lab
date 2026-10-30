"""Testes das funções puras da sonda RTSP (`scripts/probe/rtsp_read_probe.py`).

Só lógica: cálculo de gaps, fps medido na janela, decisão PASS/FAIL e parsing
de argumentos. Nada aqui abre socket nem toca em GStreamer — a bancada de
verdade é a validação de E0-T3 (publisher + probe contra o MediaMTX).

Nota: não existe `scripts/tests/__init__.py` de propósito. `deepstream/tests`
já é importado pelo pytest como pacote `tests` (o avô `deepstream/` não tem
`__init__.py`); criar outro `tests` em `scripts/` colidiria o nome do módulo
("import file mismatch"). Por isso o import abaixo usa o caminho do diretório.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probe"))

import rtsp_read_probe as probe  # noqa: E402


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


class TestArgsEUrl(unittest.TestCase):
    def test_url_valida(self):
        self.assertEqual(
            probe.validate_url("  rtsp://192.168.59.102:8554/drone  "),
            "rtsp://192.168.59.102:8554/drone",
        )

    def test_url_vazia(self):
        with self.assertRaises(ValueError):
            probe.validate_url("")

    def test_url_sem_esquema(self):
        with self.assertRaises(ValueError):
            probe.validate_url("192.168.59.102:8554/drone")

    def test_url_com_esquema_errado(self):
        with self.assertRaises(ValueError):
            probe.validate_url("http://192.168.59.102:8888/live")

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
            ]
        )
        self.assertEqual(args.seconds, 5.0)
        self.assertEqual(args.protocols, 1)
        self.assertEqual(args.min_fps, 12.5)
        self.assertEqual(args.min_frames, 3)
        self.assertTrue(args.json)

    def test_parse_args_rejeita_url_invalida(self):
        with self.assertRaises(SystemExit) as ctx:
            probe.parse_args(["--url", "http://x/live"])
        self.assertEqual(ctx.exception.code, 2)

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
            "gaps_over_500ms", "max_gap_ms", "avg_gap_ms", "duration_s",
            "result", "reason",
        ):
            self.assertIn(chave, report)

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


if __name__ == "__main__":
    unittest.main()
