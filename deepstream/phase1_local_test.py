import sys
import os
import time
import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("Phase1TestFile")

def main():
    Gst.init(None)

    # Gerar um arquivo de vídeo de teste simples em H.264 ou usar videotestsrc se preferir,
    # mas para testar o decodificador NVMM e pipeline, vamos usar videotestsrc convertido para NVMM via nvvconv
    pipeline_str = (
        "videotestsrc num-buffers=300 ! "
        "video/x-raw, width=1280, height=720, framerate=30/1 ! "
        "nvvidconv ! "
        "video/x-raw(memory:NVMM), format=NV12 ! "
        "nvv4l2h264enc bitrate=4000000 ! "
        "h264parse ! "
        "rtph264pay name=pay0 pt=96 ! "
        "fakesink sync=false"
    )

    logger.info(f"Pipeline GStreamer (Gerador local): {pipeline_str}")

    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except Exception as e:
        logger.error(f"Erro ao criar pipeline GStreamer: {e}")
        sys.exit(1)

    loop = GLib.MainLoop()

    def bus_call(bus, message, loop):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("Fim do stream (EOS)")
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"Erro no pipeline: {err}: {debug}")
            loop.quit()
        return True

    bus = pipeline.get_bus()
    bus.add_watch(GLib.PRIORITY_DEFAULT, bus_call, loop)

    logger.info("Executando pipeline local por 5 segundos...")
    pipeline.set_state(Gst.State.PLAYING)

    def stop_pipeline():
        logger.info("Encerrando teste com sucesso.")
        pipeline.set_state(Gst.State.NULL)
        loop.quit()
        return False

    GLib.timeout_add_seconds(5, stop_pipeline)

    try:
        loop.run()
    except Exception as e:
        logger.error(f"Exceção no loop principal: {e}")
    finally:
        pipeline.set_state(Gst.State.NULL)
        logger.info("Teste concluído.")

if __name__ == "__main__":
    main()
