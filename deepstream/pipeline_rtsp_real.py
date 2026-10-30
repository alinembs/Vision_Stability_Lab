import sys
import os
import time
import logging

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from tracker_state import TrackerState
from health_metrics import HealthMetricsCollector
from tegrastats_reader import TegrastatsReader
from rtsp_env import require_rtsp_uri  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("VisionPipelineRTSP")

def main():
    Gst.init(None)

    tracker_state = TrackerState(ttl_seconds=10.0)
    health_collector = HealthMetricsCollector()
    tegrastats = TegrastatsReader()

    rtsp_uri = require_rtsp_uri()
    logger.info(f"Conectando à câmera RTSP real: {rtsp_uri}")

    # Pipeline GStreamer adaptado para o ambiente sem plugins de SDK DeepStream (nvinfer/nvstreammux),
    # focando em decode acelerado (nvv4l2decoder), conversão NVMM (nvvidconv) e streaming H.264.
    pipeline_str = (
        f"rtspsrc location={rtsp_uri} latency=200 drop-on-latency=true ! "
        f"rtph264depay ! h264parse ! nvv4l2decoder ! "
        f"video/x-raw(memory:NVMM) ! "
        f"nvvidconv ! "
        f"video/x-raw, format=BGRx ! "
        f"videoconvert ! "
        f"video/x-raw, format=RGBA ! "
        f"queue ! "
        f"nvv21h264enc? 2>/dev/null || "
        f"nvv4l2h264enc bitrate=4000000 ! "
        f"h264parse ! "
        f"rtph264pay name=pay0 pt=96 ! "
        f"udpsink host=127.0.0.1 port=5000"
    )

    # Pipeline limpo e compatível com o runtime atual da Jetson:
    pipeline_clean = (
        f"rtspsrc location={rtsp_uri} latency=200 drop-on-latency=true ! "
        f"rtph264depay ! h264parse ! nvv4l2decoder ! "
        f"video/x-raw(memory:NVMM) ! "
        f"tee name=t "
        f"t. ! queue leaky=2 max-size-buffers=1 ! fakesink sync=false "
        f"t. ! queue leaky=2 max-size-buffers=1 ! "
        f"nvv4l2h264enc bitrate=4000000 ! "
        f"h264parse ! "
        f"rtph264pay name=pay0 pt=96 ! "
        f"udpsink host=127.0.0.1 port=5000"
    )

    logger.info(f"Pipeline construído para câmera RTSP: {pipeline_clean}")

    try:
        pipeline = Gst.parse_launch(pipeline_clean)
    except Exception as e:
        logger.error(f"Erro ao instanciar pipeline GStreamer RTSP: {e}")
        sys.exit(1)

    loop = GLib.MainLoop()

    def bus_call(bus, message, loop):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("Fim do stream (EOS)")
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"Erro crítico no pipeline RTSP: {err}: {debug}")
            loop.quit()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"Aviso no pipeline: {warn}: {debug}")
        return True

    bus = pipeline.get_bus()
    bus.add_watch(GLib.PRIORITY_DEFAULT, bus_call, loop)

    def periodic_metrics():
        metrics = health_collector.collect()
        t_stats = tegrastats.read_latest()
        logger.info(f"[Métricas] RSS: {metrics['process']['rss_mb']} MB | Growth: {metrics['process']['memory_growth_mb']} MB | Tegrastats: {t_stats.get('status')}")
        return True

    GLib.timeout_add_seconds(5, periodic_metrics)

    pipeline.set_state(Gst.State.PLAYING)
    logger.info("Pipeline RTSP com câmera real em execução (PLAYING).")

    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Interrupção pelo usuário recebida.")
    finally:
        pipeline.set_state(Gst.State.NULL)
        logger.info("Pipeline encerrado com segurança.")

if __name__ == "__main__":
    main()
