"""Vision Stability Lab — Fases 2 e 3.

Pipeline: RTSP (H.265) -> decode HW (nvv4l2decoder) -> nvstreammux ->
nvinfer (YOLO11 person) -> nvtracker (IOU) -> probe de metadados pyds
-> nvdsosd -> tee:
  ramo análise:  queue -> fakesink (probes de métricas)
  ramo preview:  queue leaky(downstream, max-size-buffers=1)
                 -> nvvideoconvert -> NV12(NVMM) -> nvv4l2h264enc
                 -> h264parse -> rtph264pay -> udpsink

O ramo de preview é descartável: o vídeo anotado nunca bloqueia a
inferência (queue leaky com 1 buffer de folga).
Métricas: FPS de entrada/processado/preview, contagem de objetos,
estado da área (EMPTY/OCCUPIED) e RSS do processo, em logs JSON.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

import pyds  # noqa: E402

from area_monitor import AreaMonitor  # noqa: E402
from health_metrics import HealthMetricsCollector  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("VisionPipelineReal")

PERSON_CLASS_ID = 0
METRICS_INTERVAL_SECONDS = int(os.environ.get("METRICS_INTERVAL_S", "30"))
TRACK_TTL_SECONDS = float(os.environ.get("TRACK_TTL_S", "10"))
MAX_EVENTS_BUFFERED = int(os.environ.get("MAX_EVENTS", "200"))
TEST_DURATION_SECONDS = int(os.environ.get("TEST_DURATION_S", "0"))  # 0 = até interromper
PREVIEW_ENABLED = os.environ.get("PREVIEW_ENABLED", "1") == "1"
# Fase 4: rtsp = publica no MediaMTX (rtspclientsink); udp = RTP direto (Fase 3)
PREVIEW_TRANSPORT = os.environ.get("PREVIEW_TRANSPORT", "rtsp").lower()
PREVIEW_MTX_URI = os.environ.get("PREVIEW_MTX_URI", "rtsp://mediamtx:8554/live")
PREVIEW_HOST = os.environ.get("PREVIEW_HOST", "127.0.0.1")
PREVIEW_PORT = int(os.environ.get("PREVIEW_PORT", "5600"))
PREVIEW_BITRATE = int(os.environ.get("PREVIEW_BITRATE", "4000000"))  # bps
PREVIEW_IDR_INTERVAL = int(os.environ.get("PREVIEW_IDR_GOP", "30"))
# Telemetria: além do stdout, grava JSONL estável consumido pelo dashboard
TELEMETRY_FILE = os.environ.get(
    "TELEMETRY_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "logs", "live.jsonl"))


def log_event(event, **fields):
    """Emite um log estruturado em JSON (uma linha por evento)."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    logger.info(line)
    try:
        with open(TELEMETRY_FILE, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # telemetria em arquivo é best-effort; stdout é a fonte primária


class FpsCounter:
    """Conta buffers e calcula FPS por intervalo."""

    def __init__(self, label):
        self.label = label
        self.count = 0
        self.total = 0
        self.window_start = time.monotonic()
        self.fps = 0.0

    def tick(self):
        self.count += 1
        self.total += 1

    def update(self):
        now = time.monotonic()
        elapsed = now - self.window_start
        if elapsed > 0:
            self.fps = self.count / elapsed
        self.count = 0
        self.window_start = now
        return round(self.fps, 2)


class VisionPipeline:
    def __init__(self):
        self.rtsp_uri = os.environ.get(
            "RTSP_URI",
            "rtsp://admin:cameraepi1@192.168.59.108:5543/live/channel0",
        )
        self.codec = os.environ.get("RTSP_CODEC", "h265").lower()  # câmera confirmada H.265
        self.monitor = AreaMonitor(ttl_seconds=TRACK_TTL_SECONDS,
                                   max_events=MAX_EVENTS_BUFFERED)
        self.entries_interval = 0
        self.exits_interval = 0
        self.health = HealthMetricsCollector()
        # Protege o AreaMonitor contra data race probe (streaming thread)
        # <-> periodic_metrics (GLib MainLoop).
        self._monitor_lock = threading.Lock()
        self.fps_in = FpsCounter("input")
        self.fps_proc = FpsCounter("processed")
        self.fps_preview = FpsCounter("preview")
        self.persons_detected_interval = 0
        self.persons_max_concurrent = 0

        self.pipeline = Gst.Pipeline.new("vision-phase1")

        self.rtspsrc = self._make("rtspsrc", location=self.rtsp_uri, latency=200,
                                  drop_on_latency=True, protocols=4)  # 4 = TCP
        self.depay = self._make("rtph265depay" if self.codec == "h265" else "rtph264depay")
        self.parser = self._make("h265parse" if self.codec == "h265" else "h264parse")
        self.decoder = self._make("nvv4l2decoder")

        self.mux = self._make("nvstreammux", batch_size=1, width=1280, height=720,
                              batched_push_timeout=40000, live_source=True)

        infer_config = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "config", "infer.txt")
        self.infer = self._make("nvinfer", config_file_path=infer_config)

        tracker_config = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "config", "config_tracker_IOU.yml")
        self.tracker = self._make("nvtracker",
                                  ll_lib_file="/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_nvmultiobjecttracker.so",
                                  ll_config_file=tracker_config,
                                  tracker_width=640, tracker_height=384,
                                  gpu_id=0, display_tracking_id=0)

        # Fase 3 — OSD + preview H.264 descartável
        # (no DS 7.1 o nvdsosd só expõe flags de exibição; estilo fino
        # exigiria NvDsDisplayMeta via probe)
        self.osd = self._make("nvdsosd", display_bbox=True, display_text=True,
                              process_mode=1)  # 1 = GPU_MODE
        self.tee = self._make("tee")
        # Ramo de análise: consome tudo (probes dependem dele).
        self.queue_analyze = self._make("queue")
        self.sink = self._make("fakesink", sync=False, **{"async": False}, qos=False)
        if PREVIEW_ENABLED:
            # Ramo de preview: leaky downstream com 1 buffer de folga —
            # se o encoder atrasar, frames são descartados antes de
            # propagar backpressure para a inferência.
            self.queue_preview = self._make(
                "queue", **{"leaky": 2, "max_size_buffers": 1,
                            "max_size_bytes": 0, "max_size_time": 0})
            self.videoconv = self._make("nvvideoconvert")
            # gst_element_set_property não parseia a string de caps;
            # é preciso um objeto Gst.Caps.
            self.enc_caps = self._make("capsfilter")
            self.enc_caps.set_property(
                "caps",
                Gst.Caps.from_string("video/x-raw(memory:NVMM),format=NV12"))
            self.encoder = self._make(
                "nvv4l2h264enc", bitrate=PREVIEW_BITRATE,
                idrinterval=PREVIEW_IDR_INTERVAL, insert_sps_pps=True)
            self.h264parse = self._make("h264parse")
            if PREVIEW_TRANSPORT == "rtsp":
                # Fase 4 — publica no MediaMTX via RTSP RECORD
                self.rtsp_client = self._make(
                    "rtspclientsink", location=PREVIEW_MTX_URI,
                    protocols=4)  # 4 = TCP
            else:
                # Fase 3 — RTP/UDP direto (testes de viewer lento)
                self.payloader = self._make(
                    "rtph264pay", pt=96, **{"config-interval": -1})
                self.udp_sink = self._make(
                    "udpsink", host=PREVIEW_HOST, port=PREVIEW_PORT,
                    sync=False, **{"async": False}, qos=False)

        for element in (self.depay, self.parser, self.decoder, self.mux,
                        self.infer, self.tracker, self.osd, self.tee,
                        self.queue_analyze, self.sink):
            self.pipeline.add(element)
        if PREVIEW_ENABLED:
            if PREVIEW_TRANSPORT == "rtsp":
                preview_tail = (self.rtsp_client,)
            else:
                preview_tail = (self.payloader, self.udp_sink)
            for element in (self.queue_preview, self.videoconv, self.enc_caps,
                            self.encoder, self.h264parse) + preview_tail:
                self.pipeline.add(element)
        self.pipeline.add(self.rtspsrc)

        if not self.depay.link(self.parser) or not self.parser.link(self.decoder):
            raise RuntimeError("Falha ao linkar depay -> parse -> decoder")

        mux_sink_pad = self.mux.get_request_pad("sink_0")
        dec_src_pad = self.decoder.get_static_pad("src")
        if not mux_sink_pad or not dec_src_pad or \
                dec_src_pad.link(mux_sink_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("Falha ao linkar decoder -> nvstreammux")

        if not self.mux.link(self.infer) or not self.infer.link(self.tracker):
            raise RuntimeError("Falha ao linkar mux -> nvinfer -> nvtracker")

        if not self.tracker.link(self.osd) or not self.osd.link(self.tee):
            raise RuntimeError("Falha ao linkar nvtracker -> nvdsosd -> tee")

        tee_src_analyze = self.tee.get_request_pad("src_%u")
        if not tee_src_analyze or \
                tee_src_analyze.link(self.queue_analyze.get_static_pad("sink")) \
                != Gst.PadLinkReturn.OK:
            raise RuntimeError("Falha ao linkar tee -> queue análise")
        if not self.queue_analyze.link(self.sink):
            raise RuntimeError("Falha ao linkar queue análise -> fakesink")

        if PREVIEW_ENABLED:
            tee_src_preview = self.tee.get_request_pad("src_%u")
            if not tee_src_preview or \
                    tee_src_preview.link(self.queue_preview.get_static_pad("sink")) \
                    != Gst.PadLinkReturn.OK:
                raise RuntimeError("Falha ao linkar tee -> queue preview")
            chain = (self.queue_preview, self.videoconv, self.enc_caps,
                     self.encoder, self.h264parse) + \
                    ((self.rtsp_client,) if PREVIEW_TRANSPORT == "rtsp"
                     else (self.payloader, self.udp_sink))
            for src, dst in zip(chain, chain[1:]):
                if not src.link(dst):
                    raise RuntimeError(
                        "Falha ao linkar ramo de preview: "
                        f"{src.get_name()} -> {dst.get_name()}")
            log_event("preview_branch_ready",
                      transport=PREVIEW_TRANSPORT,
                      target=PREVIEW_MTX_URI if PREVIEW_TRANSPORT == "rtsp"
                      else f"{PREVIEW_HOST}:{PREVIEW_PORT}",
                      bitrate_bps=PREVIEW_BITRATE,
                      idr_interval=PREVIEW_IDR_INTERVAL)

        self.rtspsrc.connect("pad-added", self._on_rtspsrc_pad_added,
                             self.depay.get_static_pad("sink"))

        self.infer.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_input_buffer, None)
        self.sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_processed_buffer, None)
        if PREVIEW_ENABLED:
            self.encoder.get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER, self._on_preview_buffer, None)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _make(self, factory_name, **props):
        element = Gst.ElementFactory.make(factory_name, None)
        if element is None:
            raise RuntimeError(f"Falha ao criar elemento GStreamer: {factory_name}")
        for key, value in props.items():
            element.set_property(key.replace("_", "-"), value)
        return element

    def _on_rtspsrc_pad_added(self, _src, pad, depay_sink_pad):
        """Linka somente o pad de vídeo do rtspsrc (ignora áudio PCMA)."""
        caps = pad.get_current_caps() or pad.query_caps(None)
        media = caps.get_structure(0).get_value("media") if caps and caps.get_size() else None
        if media == "video":
            log_event("rtsp_video_pad", pad=pad.get_name())
            if pad.link(depay_sink_pad) != Gst.PadLinkReturn.OK:
                log_event("rtsp_link_error", pad=pad.get_name())

    def _on_input_buffer(self, _pad, info, _user_data):
        self.fps_in.tick()
        return Gst.PadProbeReturn.OK

    def _on_preview_buffer(self, _pad, info, _user_data):
        self.fps_preview.tick()
        return Gst.PadProbeReturn.OK

    def _on_processed_buffer(self, _pad, info, _user_data):
        self.fps_proc.tick()
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        if not batch_meta:
            return Gst.PadProbeReturn.OK

        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            current_tracks = {}
            person_count = 0

            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                if obj_meta.class_id == PERSON_CLASS_ID:
                    rect = obj_meta.rect_params
                    # object_id agora vem do nvtracker (estável entre frames)
                    current_tracks[str(obj_meta.object_id)] = (
                        rect.left, rect.top, rect.width, rect.height)
                    person_count += 1
                try:
                    l_obj = l_obj.next
                except StopIteration:
                    break

            with self._monitor_lock:
                state_info = self.monitor.update(current_tracks)
            self.persons_detected_interval += person_count
            self.persons_max_concurrent = max(self.persons_max_concurrent, person_count)
            self.entries_interval += len(state_info["entered"])
            self.exits_interval += len(state_info["exited"])

            for ev in state_info["exited"]:
                log_event("track_exited", track_id=ev["track_id"],
                          dwell_seconds=ev["dwell_seconds"])
            for tid in state_info["entered"]:
                log_event("track_entered", track_id=tid)
            if state_info["transition"]:
                log_event("area_transition",
                          transition=state_info["transition"],
                          active_tracks=len(self.monitor.tracks),
                          frame_num=frame_meta.frame_num)

            try:
                l_frame = l_frame.next
            except StopIteration:
                break
        return Gst.PadProbeReturn.OK

    def _on_bus_message(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            log_event("eos")
            self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log_event("pipeline_error", error=str(err), debug=debug)
            self.loop.quit()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            log_event("pipeline_warning", warning=str(warn), debug=debug)
        return True

    def periodic_metrics(self):
        # Lê o monitor sob lock e blinda exceções para o timer GLib
        # não morrer silenciosamente (retorno True mantém o timeout ativo).
        try:
            with self._monitor_lock:
                dwell_values = list(self.monitor.active_dwell_seconds(
                    now=time.time()).values())
                n_events = len(self.monitor.recent_events)
                total_entries = self.monitor.total_entries
                total_exits = self.monitor.total_exits
                n_tracks = len(self.monitor.tracks)
                area_state = self.monitor.area_state
        except Exception:
            logger.exception("periodic_metrics falhou")
            return True
        fps_in = self.fps_in.update()
        fps_proc = self.fps_proc.update()
        fps_preview = self.fps_preview.update() if PREVIEW_ENABLED else None
        metrics = self.health.collect() or {}
        process = metrics.get("process", {})

        # Fase 5: ocupação das filas do tee (buffers em cada ramo)
        queue_levels = {}
        for name in ("queue_analyze", "queue_preview"):
            q = getattr(self, name, None)
            if q is not None:
                try:
                    queue_levels[name] = q.get_property("current-level-buffers")
                except Exception:
                    queue_levels[name] = None

        log_event("interval_metrics",
                  interval_s=METRICS_INTERVAL_SECONDS,
                  fps_in=fps_in,
                  fps_processed=fps_proc,
                  fps_preview=fps_preview,
                  preview_enabled=PREVIEW_ENABLED,
                  preview_dropped_fps=round(fps_in - fps_preview, 2)
                  if PREVIEW_ENABLED else None,
                  frames_total_in=self.fps_in.total,
                  frames_total_processed=self.fps_proc.total,
                  persons_detected=self.persons_detected_interval,
                  persons_max_concurrent=self.persons_max_concurrent,
                  active_tracks=n_tracks,
                  area_state=area_state,
                  entries_interval=self.entries_interval,
                  exits_interval=self.exits_interval,
                  avg_dwell_s=round(sum(dwell_values) / len(dwell_values), 2) if dwell_values else 0.0,
                  events_buffered=n_events,
                  total_entries=total_entries,
                  total_exits=total_exits,
                  queue_buffers=queue_levels,
                  rss_mb=process.get("rss_mb"),
                  memory_growth_mb=process.get("memory_growth_mb"),
                  threads=process.get("threads"),
                  open_fds=process.get("open_fds"))
        self.persons_detected_interval = 0
        self.persons_max_concurrent = 0
        self.entries_interval = 0
        self.exits_interval = 0
        return True

    def run(self):
        self.loop = GLib.MainLoop()

        if TEST_DURATION_SECONDS > 0:
            GLib.timeout_add_seconds(TEST_DURATION_SECONDS, self._stop)
            log_event("test_started", duration_s=TEST_DURATION_SECONDS, codec=self.codec,
                      uri=self.rtsp_uri.split("@")[-1])
        GLib.timeout_add_seconds(METRICS_INTERVAL_SECONDS, self.periodic_metrics)

        self.pipeline.set_state(Gst.State.PLAYING)
        log_event("pipeline_playing", codec=self.codec)

        try:
            self.loop.run()
        except KeyboardInterrupt:
            log_event("interrupted_by_user")
        finally:
            try:
                self.periodic_metrics()
            except Exception:
                logger.exception("metrics de shutdown falharam")
            finally:
                self.pipeline.set_state(Gst.State.NULL)
                log_event("pipeline_stopped")

    def _stop(self):
        log_event("test_duration_reached")
        self.loop.quit()
        return False


def main():
    Gst.init(None)
    log_event("startup",
              gst_version=Gst.version_string(),
              pyds_version=getattr(pyds, "__version__", "unknown"),
              preview_enabled=PREVIEW_ENABLED)
    try:
        app = VisionPipeline()
    except Exception as exc:  # noqa: BLE001
        log_event("startup_failed", error=str(exc))
        sys.exit(1)
    app.run()


if __name__ == "__main__":
    main()
