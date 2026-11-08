"""Testes da decisão pura sobre o bus do GStreamer (`bus_classifier.py`).

Cobre o E-A: erro do ramo de preview derrubava a análise inteira, e EOS da
fonte encerrava o processo com `ExitCode = 0`
(`docs/baselines/2026-10-30-e1t2-baseline.md`).

Nada aqui importa `gi`/`pyds`: é exatamente por isso que a decisão saiu de
`pipeline_real.py`. Os nomes de elemento usados são os que o GStreamer gera
(factory + índice), como apareceram no `live.jsonl` do incidente.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bus_classifier as bc  # noqa: E402


class TestErroDoPreview(unittest.TestCase):
    """Elementos do ramo descartável: derrubam só o preview."""

    def test_rtspclientsink_do_incidente(self):
        # "rtspclientsink0: Could not connect to server." (E-A, 16:15:40)
        self.assertEqual(
            bc.classify_bus_error("rtspclientsink0",
                                  "Could not connect to server."),
            bc.ACTION_DROP_PREVIEW)

    def test_todo_o_ramo_de_preview(self):
        # nomes reais do ramo no container (o `queue` do preview é `queue1`)
        for elemento in ("nvvideoconvert0", "capsfilter0", "nvv4l2h264enc0",
                         "h264parse0", "rtph264pay0", "udpsink0"):
            with self.subTest(elemento=elemento):
                self.assertEqual(bc.classify_bus_error(elemento, "erro"),
                                 bc.ACTION_DROP_PREVIEW)

    def test_conjunto_exato_do_pipeline_tem_precedencia(self):
        # o pipeline passa os nomes reais; `queue1` só é preview se vier no
        # conjunto explícito
        self.assertEqual(
            bc.classify_bus_error("queue1", "erro", {"queue1"}),
            bc.ACTION_DROP_PREVIEW)
        self.assertEqual(bc.classify_bus_error("queue1", "erro"),
                         bc.ACTION_QUIT)


class TestErroDaAnalise(unittest.TestCase):
    """Ramo de análise mantém o comportamento: encerra com código != 0."""

    def test_elementos_da_analise(self):
        # `rtspsrc` NÃO está aqui: erro da fonte reconecta (ver
        # TestErroDaFonte). Depay/parser/decoder/mux são mídia quebrada de
        # verdade — reconectar o `rtspsrc` não conserta nenhum deles.
        for elemento in ("rtph265depay0", "h265parse0",
                         "nvv4l2decoder0", "nvstreammux0", "nvinfer0",
                         "nvtracker0", "nvdsosd0", "tee0", "queue0",
                         "fakesink0"):
            with self.subTest(elemento=elemento):
                self.assertEqual(bc.classify_bus_error(elemento, "erro"),
                                 bc.ACTION_QUIT)

    def test_h264parse_e_analise_no_codec_h264(self):
        # com RTSP_CODEC=h264 o parser da análise também é h264parse; o
        # conjunto explícito do pipeline evita classificar o errado
        self.assertEqual(bc.classify_bus_error("h264parse0", "erro"),
                         bc.ACTION_DROP_PREVIEW)
        self.assertEqual(
            bc.classify_bus_error("h264parse0", "erro",
                                  {"rtspclientsink0", "nvv4l2h264enc0"}),
            bc.ACTION_QUIT)


class TestEos(unittest.TestCase):
    def test_eos_da_fonte_reconecta(self):
        self.assertEqual(bc.classify_bus_message(bc.MSG_EOS, "rtspsrc0"),
                         bc.ACTION_RECONNECT)

    def test_eos_do_sink_de_analise_reconecta(self):
        # EOS propaga até o sink, que publica a mensagem: também reconecta
        self.assertEqual(bc.classify_bus_message(bc.MSG_EOS, "fakesink0"),
                         bc.ACTION_RECONNECT)

    def test_eos_sem_origem_reconecta(self):
        self.assertEqual(bc.classify_bus_message(bc.MSG_EOS, ""),
                         bc.ACTION_RECONNECT)

    def test_eos_do_preview_derruba_so_o_preview(self):
        self.assertEqual(bc.classify_bus_message(bc.MSG_EOS, "rtspclientsink0"),
                         bc.ACTION_DROP_PREVIEW)


class TestErroDaFonte(unittest.TestCase):
    """Erro do `rtspsrc` reconecta — e é o orçamento quem desiste (E2).

    Com a fonte fora do ar o `rtspsrc` publica
    `Could not open resource for reading (5)` (o MediaMTX recusa sessão para
    path sem publisher), não EOS. Antes isso era "erro da análise": exit 1 e o
    `restart: unless-stopped` escondia o ciclo num crash loop, sem chegar ao
    exit 2 documentado no runbook.
    """

    def test_erro_do_rtspsrc_reconecta(self):
        self.assertEqual(
            bc.classify_bus_error("rtspsrc0",
                                  "Could not open resource for reading. (5)"),
            bc.ACTION_RECONNECT)

    def test_outros_nomes_do_rtspsrc(self):
        for elemento in ("rtspsrc", "rtspsrc1", "rtspsrc12"):
            with self.subTest(elemento=elemento):
                self.assertEqual(bc.classify_bus_error(elemento, "erro"),
                                 bc.ACTION_RECONNECT)

    def test_is_source_element(self):
        self.assertTrue(bc.is_source_element("rtspsrc0"))
        self.assertFalse(bc.is_source_element("rtph265depay0"))
        self.assertFalse(bc.is_source_element(""))


class TestErroDesconhecido(unittest.TestCase):
    def test_elemento_desconhecido_encerra(self):
        # não dá para provar que é descartável: conservador, encerra
        self.assertEqual(bc.classify_bus_error("pluginNovo0", "erro"),
                         bc.ACTION_QUIT)

    def test_sem_origem_e_sem_pista_no_texto_encerra(self):
        self.assertEqual(bc.classify_bus_error("", "erro genérico"),
                         bc.ACTION_QUIT)

    def test_sem_origem_mas_texto_nomeia_o_preview(self):
        self.assertEqual(
            bc.classify_bus_error("", "rtspclientsink0: Could not connect to server."),
            bc.ACTION_DROP_PREVIEW)

    def test_tipo_de_mensagem_nao_tratado_e_ignorado(self):
        for tipo in ("warning", "info", "state-changed", ""):
            with self.subTest(tipo=tipo):
                self.assertEqual(bc.classify_bus_message(tipo, "rtspsrc0"),
                                 bc.ACTION_IGNORE)


class TestBaseElementName(unittest.TestCase):
    def test_remove_o_indice_do_gstreamer(self):
        self.assertEqual(bc.base_element_name("rtspclientsink0"),
                         "rtspclientsink")
        self.assertEqual(bc.base_element_name("nvv4l2h264enc12"),
                         "nvv4l2h264enc")
        self.assertEqual(bc.base_element_name("queue_preview"), "queue_preview")
        self.assertEqual(bc.base_element_name(""), "")


if __name__ == "__main__":
    unittest.main()
