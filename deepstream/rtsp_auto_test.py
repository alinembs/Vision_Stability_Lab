import sys
import os
import time
import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("RTSPAutoTest")

def main():
    Gst.init(None)
    rtsp_uri = os.environ.get("RTSP_URI", "rtsp://admin:cameraepi1@192.168.59.108:5543/live/channel0")
    logger.info(f"Testando pipeline completo com autovideoconvert/nvv4l2h264enc: {rtsp_uri}")

    pipeline_str = (
        f"rtspsrc location={rtsp_uri} latency=200 ! "
        f"rtph264depay ! h264parse ! nvv4l2decoder ! "
        f"video/x-raw(memory:NVMM) ! nvvidconv ! "
        f"video/x-raw, format=BGRx ! videoconvert ! "
        f"video/x-raw, format=I420 ! "
        f"nvv4l2h264enc bitrate=4000000 ! "
        f"h264parse ! rtph264pay name=pay0 pt=96 ! "
        f"fakesink sync=false"
    )

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
