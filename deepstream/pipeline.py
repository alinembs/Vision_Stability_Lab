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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("VisionPipeline")

def pyds_probe_cb(pad, info, u_data):
    # Probe callback para interceptar metadados do DeepStream
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        return Gst.PadProbeReturn.OK

    # Aqui extrairíamos os metadados do DeepStream com pyds se instalado/carregado
    # Exemplo de lógica temporal com TrackerState
    tracker_state, health_collector, tegrastats = u_data

    # Simulação de contagem para validação do fluxo do probe
    current_tracks = {} # Em produção, preenchido via pyds.gst_buffer_get_nvds_batch_meta

    state_info = tracker_state.update(current_tracks)
    if state_info["transition"]:
        logger.info(f"Transição de Estado da Área: {state_info['transition']} (Pessoas/Tracks: {state_info['active_tracks']})")

    return Gst.PadProbeReturn.OK

def main():
    Gst.init(None)

    tracker_state = TrackerState(ttl_seconds=10.0)
    health_collector = HealthMetricsCollector()
    tegrastats = TegrastatsReader()

    rtsp_uri = os.environ.get("RTSP_URI", "rtsp://localhost:8554/live")
    logger.info(f"Iniciando pipeline principal do Vision Stability Lab com URI: {rtsp_uri}")

    # Pipeline GStreamer estruturado conforme o README.md
    # Para testes iniciais robustos na Jetson sem modelo .engine obrigatório pré-compilado,
    # montamos a infraestrutura com conversão e encodagem H.264 para MediaMTX.
    pipeline_str = (
        f"rtspsrc location={rtsp_uri} latency=200 drop-on-latency=true ! "
        f"rtph265depay ! h265parse ! nvv4l2decoder ! "
        f"video/x-raw(memory:NVMM) ! "
        f"tee name=t "
        f"t. ! queue leaky=2 max-size-buffers=1 ! fakesink sync=false "
        f"t. ! queue leaky=2 max-size-buffers=1 ! "
        f"nvvidconv ! "
        f"video/x-raw(memory:NVMM), format=NV12 ! "
        f"nvv4l2h264enc bitrate=4000000 ! "
        f"h264parse ! "
        f"rtph264pay name=pay0 pt=96 ! "
        f"udpsink host=127.0.0.1 port=5000"
    )

    logger.info(f"Pipeline construído: {pipeline_str}")

    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except Exception as e:
        logger.error(f"Erro ao instanciar pipeline GStreamer: {e}")
        sys.exit(1)

    loop = GLib.MainLoop()

    def bus_call(bus, message, loop):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.info("Fim do stream (EOS)")
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"Erro crítico no pipeline: {err}: {debug}")
            loop.quit()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"Aviso no pipeline: {warn}: {debug}")
        return True

    bus = pipeline.get_bus()
    bus.add_watch(GLib.PRIORITY_DEFAULT, bus_call, loop)

    # Timer periódico para métricas de saúde e tegrastats
    def periodic_metrics():
        metrics = health_collector.collect()
        t_stats = tegrastats.read_latest()
        logger.info(f"[Métricas] RSS: {metrics['process']['rss_mb']} MB | Growth: {metrics['process']['memory_growth_mb']} MB | Tegrastats: {t_stats.get('status')}")
        return True

    GLib.timeout_add_seconds(5, periodic_metrics)

    pipeline.set_state(Gst.State.PLAYING)
    logger.info("Pipeline em execução (PLAYING). Pressione Ctrl+C para encerrar.")

    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Interrupção pelo usuário recebida.")
    finally:
        pipeline.set_state(Gst.State.NULL)
        logger.info("Pipeline encerrado com segurança.")

if __name__ == "__main__":
    main()
