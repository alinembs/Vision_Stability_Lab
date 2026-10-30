import sys
import time
import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
from rtsp_env import require_rtsp_uri  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("RTSPSigTest")

def main():
    Gst.init(None)
    rtsp_uri = require_rtsp_uri()
    logger.info(f"Testando conexão RTSP com rtspsrc e decodebin: {rtsp_uri}")

    pipeline_str = f"rtspsrc location={rtsp_uri} latency=200 ! decodebin ! fakesink sync=false"

    pipeline = Gst.parse_launch(pipeline_str)
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

    GLib.timeout_add_seconds(8, lambda: (pipeline.set_state(Gst.State.NULL), loop.quit(), False))

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        logger.info("Teste finalizado.")

if __name__ == "__main__":
    main()
