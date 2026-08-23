import sys
import os
import time
import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("Phase1Test")

def main():
    Gst.init(None)

    rtsp_uri = os.environ.get("RTSP_URI", "rtsp://localhost:8554/live")
    logger.info(f"Iniciando teste da Fase 1 com RTSP URI: {rtsp_uri}")

    # Construir pipeline simples de teste com decodificação e fakesink para validação de estabilidade
    pipeline_str = (
        f"rtspsrc location={rtsp_uri} latency=200 ! "
        f"rtph264depay ! h264parse ! nvv4l2decoder ! "
        f"video/x-raw(memory:NVMM) ! "
        f"fakesink sync=false"
    )

    logger.info(f"Pipeline GStreamer: {pipeline_str}")

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
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"Aviso no pipeline: {warn}: {debug}")
        return True

    bus = pipeline.get_bus()
    bus.add_watch(GLib.PRIORITY_DEFAULT, bus_call, loop)

    logger.info("Executando pipeline por 10 segundos para validação da Fase 1...")
    pipeline.set_state(Gst.State.PLAYING)

    # Rodar por 10 segundos e encerrar com segurança
    def stop_pipeline():
        logger.info("Encerrando teste da Fase 1 com sucesso.")
        pipeline.set_state(Gst.State.NULL)
        loop.quit()
        return False

    GLib.timeout_add_seconds(10, stop_pipeline)

    try:
        loop.run()
    except Exception as e:
        logger.error(f"Exceção no loop principal: {e}")
    finally:
        pipeline.set_state(Gst.State.NULL)
        logger.info("Teste da Fase 1 concluído.")

if __name__ == "__main__":
    main()
