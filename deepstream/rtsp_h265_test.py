import sys
import os
import time
import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
from rtsp_env import require_rtsp_uri  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("RTSPH265Test")

def on_pad_added(src, pad, data):
    caps = pad.get_current_caps()
    if not caps:
        caps = pad.query_caps(None)
    name = caps.to_string()
    logger.info(f"Pad dinâmico adicionado em {src.get_name()}: {name}")
    if "video" in name:
        depay, parse, decoder = data
        sink_pad = depay.get_static_pad("sink")
        if not sink_pad.is_linked():
            pad.link(sink_pad)
            logger.info("Pad de vídeo vinculado com sucesso!")

def main():
    Gst.init(None)
    rtsp_uri = require_rtsp_uri()
    logger.info(f"Testando H.265 (HEVC) RTSP: {rtsp_uri}")

    pipeline = Gst.Pipeline.new("rtsp-pipeline")

    rtspsrc = Gst.ElementFactory.make("rtspsrc", "source")
    rtspsrc.set_property("location", rtsp_uri)
    rtspsrc.set_property("latency", 200)
    rtspsrc.set_property("drop-on-latency", True)

    # Usar depay/parser H.265 já que a câmera envia HEVC
    depay = Gst.ElementFactory.make("rtph265depay", "depay")
    parse = Gst.ElementFactory.make("h265parse", "parse")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "decoder")
    conv = Gst.ElementFactory.make("nvvidconv", "conv")
    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", False)

    for el in [rtspsrc, depay, parse, decoder, conv, sink]:
        if not el:
            logger.error("Elemento GStreamer essencial não encontrado!")
            sys.exit(1)
        pipeline.add(el)

    # Linkar estáticos
    depay.link(parse)
    parse.link(decoder)
    decoder.link(conv)
    conv.link(sink)

    # Conectar pad-added com depay/parser/decoder para callback
    rtspsrc.connect("pad-added", on_pad_added, (depay, parse, decoder))

    loop = GLib.MainLoop()

    def bus_call(bus, message, loop):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("EOS alcançado")
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"Erro: {err}: {debug}")
            loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == pipeline:
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.info(f"Estado do pipeline mudou para {new_state.value_nick}")
        return True

    bus = pipeline.get_bus()
    bus.add_watch(GLib.PRIORITY_DEFAULT, bus_call, loop)

    pipeline.set_state(Gst.State.PLAYING)

    GLib.timeout_add_seconds(10, lambda: (pipeline.set_state(Gst.State.NULL), loop.quit(), False))

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        logger.info("Teste finalizado.")

if __name__ == "__main__":
    main()