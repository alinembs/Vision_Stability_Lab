"""Vision Stability Lab — Fases 2 e 3.

Pipeline: RTSP (fonte ativa, H.265 ou H.264) -> decode HW (nvv4l2decoder) ->
nvstreammux -> nvinfer (YOLO11 COCO 80 classes) -> nvtracker (IOU) -> probe de metadados
pyds -> nvdsosd -> tee:
  ramo análise:  queue -> fakesink (probes de métricas)
  ramo preview:  queue leaky(downstream, max-size-buffers=1)
                 -> nvvideoconvert -> NV12(NVMM) -> nvv4l2h264enc
                 -> h264parse -> rtph264pay -> udpsink

A fonte (câmera fixa, drone ou celular — uma por vez, D1/ADR-0006) e o codec
vêm de `sources.yaml` + `data/config/active_source`, resolvidos por
`sources.py` no boot (`source_active` na telemetria). Trocar de fonte reinicia
o processo (D4); `batch_size=1` e o ramo único de análise continuam valendo.

O ramo de preview é descartável: o vídeo anotado nunca bloqueia a
inferência (queue leaky com 1 buffer de folga). Falha nele — erro de bus ou
EOS — derruba só o ramo de preview (`preview_branch_failed`) e tenta
religá-lo com backoff; a análise segue. A classificação dessa decisão está
em `bus_classifier.py` (puro, testável sem DeepStream), e a derrubada/retomada
também **não** roda no thread do loop: `preview_control.py` decide e
`_preview_step` despacha o trabalho de GStreamer para uma thread própria, com
teto de tempo (INC-007: `rtspclientsink.set_state(NULL)` no callback do bus
congelava o loop quando o MediaMTX estava fora do ar).
EOS (ou erro) do `rtspsrc` também não encerra o processo: reconecta a fonte.
A reconexão é **agendada**, nunca executada no callback do bus: aquele
callback roda no thread do loop GLib, e uma chamada de GStreamer que espere um
stream lock preso (o `nvstreammux` segura o do pad src enquanto espera batch)
congelava o loop inteiro — métricas, `_check_rtsp_alive` e `SIGTERM`
inclusive. A decisão mora em `source_reconnect.py` (puro) e as fatias são
executadas por `_reconnect_step` (timer de 250 ms, com teto por fase).
Códigos de saída (`main`): 0 = encerramento limpo, 1 = erro de pipeline,
2 = fonte perdida após esgotar as tentativas de reconexão. "Encerramento
limpo" inclui `SIGTERM`/`SIGINT` (`docker stop`, Ctrl+C): o sinal entra pelo
loop GLib (`graceful_shutdown.py`), a telemetria recebe `shutdown_requested` e
`pipeline_stopped exit_code=0` e o processo sai antes da carência do Docker —
sem isso o `docker stop` sempre terminava em `SIGKILL` (exit 143, sem evento).
Métricas: FPS de entrada/processado/preview, contagem de objetos,
estado da área (EMPTY/OCCUPIED) e RSS do processo, em logs JSON.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone


def _boot_stop(signum, _frame):
    """`SIGTERM` durante o boot: sai limpo em vez de ser `SIGKILL`ado.

    O processo é PID 1 no container e **o kernel ignora sinais de ação default
    no init**. Com o handler só no `GracefulShutdown` (que precisa do loop
    GLib), um `SIGTERM` que chega enquanto o TensorRT carrega o engine não
    derruba nem é atendido: o Docker espera a carência e mata com `SIGKILL`
    (exit 137 — medido em 2026-10-30 num restart do watchdog durante o boot).
    Este handler cobre a janela entre o start do processo e o `install()`:
    nada do pipeline existe ainda, então não há o que desmontar.
    """
    try:
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": "shutdown_before_startup",
            "signal": signal.Signals(int(signum)).name,
            "exit_code": 0,
        })
        sys.stderr.write(line + "\n")
    except Exception:  # noqa: BLE001 — sair é o que importa
        pass
    os._exit(0)


# Instalado **antes** de `gi`/`pyds`/TensorRT por construção (o import deles é
# a parte lenta do boot); o `GracefulShutdown` troca este handler depois.
signal.signal(signal.SIGTERM, _boot_stop)

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

import pyds  # noqa: E402

from area_monitor import AreaMonitor  # noqa: E402
from bus_classifier import (  # noqa: E402
    ACTION_DROP_PREVIEW, ACTION_QUIT, ACTION_RECONNECT, MSG_ERROR, MSG_EOS,
    classify_bus_message)
from graceful_shutdown import GracefulShutdown, signal_name  # noqa: E402
from health_metrics import HealthMetricsCollector  # noqa: E402
import preview_control  # noqa: E402  (puro: sem gi/pyds)
from source_budget import SourceReconnectBudget  # noqa: E402
from source_reconnect import (  # noqa: E402  (puro: sem gi/pyds)
    STEP_GIVE_UP, STEP_NONE, STEP_RESUMED, STEP_RETRY, STEP_START, STEP_STOP,
    SourceReconnectMachine)
from source_state import SessionState, with_namespace  # noqa: E402  (puro)
from sources import (  # noqa: E402  (puro: sem gi/pyds)
    depay_factory, endpoint_parts, load_sources, mask_uri,
    resolve_active_source)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("VisionPipelineReal")

# Início do processo (E3-T2). É o relógio da sessão de fonte: no container o
# import deste módulo coincide com o início do processo (`python3
# pipeline_real.py` é o entrypoint), e o valor é comparável com
# `docker inspect .State.StartedAt` — o mesmo campo que `scripts/mem_budget.sh`
# expõe e que o `switch_source.sh` compara com `session_epoch` para provar que
# a troca subiu um processo novo.
STARTED_AT = datetime.now(timezone.utc)

# Sessão ativa do processo (E3-T2). `None` até a fonte ser resolvida: eventos
# anteriores (`shutdown_before_startup`, `startup_failed` de config inválida)
# ficam **sem** namespace de propósito — não se inventa fonte para um processo
# que ainda não tem fonte. Depois de `begin_session()`, todo registro sai com
# `source_id` e `session_epoch` (ver `log_event`).
SESSION = None


def begin_session(source_id):
    """Abre a sessão da fonte ativa e a torna o namespace da telemetria.

    O epoch deriva de `STARTED_AT`, não do instante desta chamada: a sessão é a
    execução inteira do processo, e dois `begin_session` no mesmo processo
    seriam o mesmo epoch se o relógio fosse o de agora. A unicidade entre
    processos consecutivos fica com `source_state.next_epoch` (resolução de ms
    + guarda monotônica).
    """
    global SESSION
    SESSION = SessionState.new(source_id, started_at=STARTED_AT)
    return SESSION


# O modelo está liberado para as 80 classes COCO (`deepstream/config/infer.txt`)
# e o OSD desenha **tudo** o que o nvinfer entrega. A telemetria de ocupação,
# porém, continua sendo de pessoas: `AreaMonitor`/`persons_*` só contam a
# classe 0, que é a métrica do produto (estabilidade de detecção de pessoas).
# Se um dia a ocupação passar a considerar outras classes, este é o ponto.
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
# Fase 6: se nenhum frame chegar por este tempo, reseta só o rtspsrc
# (reconexão de RTSP sem derrubar o processo; o watchdog externo é a
# segunda linha de defesa e reinicia o container se isso não bastar)
RTSP_IDLE_RECONNECT_S = float(os.environ.get("RTSP_IDLE_RECONNECT_S", "30"))
RTSP_RECONNECT_COOLDOWN_S = float(
    os.environ.get("RTSP_RECONNECT_COOLDOWN_S", "60"))
# EOS da fonte: quantas reconexões seguidas sem frame de volta antes de
# desistir e encerrar com código != 0 (o watchdog não pode ser o único a
# perceber uma fonte morta).
SOURCE_RECONNECT_MAX_ATTEMPTS = int(
    os.environ.get("SOURCE_RECONNECT_MAX_ATTEMPTS", "5"))
# Quanto tempo sem EOS para devolver o orçamento de reconexões. Zerar a cada
# frame recebido fazia fonte oscilante (frame + EOS + frame + EOS) nunca
# esgotar as tentativas e nunca chegar ao exit 2 (Menor 8 da revisão).
SOURCE_RECONNECT_RESET_S = float(
    os.environ.get("SOURCE_RECONNECT_RESET_S", "600"))
# Ritmo e tetos do caminho de reconexão (`source_reconnect.py`). O trabalho é
# feito em fatias curtas por um timer GLib: nada de `set_state`/`send_event`
# no callback do bus, que roda no thread do loop (ver cabeçalho do módulo).
SOURCE_RECONNECT_STEP_MS = int(os.environ.get("SOURCE_RECONNECT_STEP_MS", "250"))
SOURCE_STOP_TIMEOUT_S = float(os.environ.get("SOURCE_STOP_TIMEOUT_S", "3"))
SOURCE_FRAME_TIMEOUT_S = float(os.environ.get("SOURCE_FRAME_TIMEOUT_S", "15"))
# Espera antes de cada nova tentativa: exponencial (10, 20, 40, 45 s...), com o
# mesmo padrão do ramo de preview. Com 5 tentativas o processo passa ~2 min
# reconectando **e publicando métricas** antes do exit 2: tempo para uma queda
# de link voltar sem declarar a fonte perdida cedo demais, e ainda abaixo da
# escalada de 180 s do watchdog (`scripts/watchdog.py`).
SOURCE_RETRY_BASE_S = float(os.environ.get("SOURCE_RETRY_S", "10"))
SOURCE_RETRY_MAX_S = float(os.environ.get("SOURCE_RETRY_MAX_S", "45"))
# Teto do `set_state(NULL)` do pipeline no encerramento. O Docker mata por
# `SIGKILL` na carência de 10 s (exit 137) e o contrato é `docker stop` em ≤ 2 s
# mesmo com a fonte morta: se a parada travar num stream lock, o processo sai
# assim mesmo, com o `exit_code` correto, em vez de virar 137.
PIPELINE_STOP_TIMEOUT_S = float(
    os.environ.get("PIPELINE_STOP_TIMEOUT_S", "1.5"))
# Ramo de preview derrubado por erro: quando tentar religá-lo, com backoff.
PREVIEW_RETRY_BASE_S = float(os.environ.get("PREVIEW_RETRY_S", "15"))
PREVIEW_RETRY_MAX_S = float(os.environ.get("PREVIEW_RETRY_MAX_S", "300"))
# Tempo máximo esperando frame de preview depois de recriar o ramo sem que
# nenhum erro novo apareça no bus (sink que não volta a publicar, por ex.).
PREVIEW_VERIFY_S = float(os.environ.get("PREVIEW_VERIFY_S", "20"))
# Frames de preview exigidos para considerar o ramo de volta (religar e
# receber dois buffers não prova que o sink voltou a publicar).
PREVIEW_VERIFY_MIN_FRAMES = int(
    os.environ.get("PREVIEW_VERIFY_MIN_FRAMES", "5"))
# Tentativas antes de desistir. Recriar o ramo instancia NVENC a cada vez —
# churn de encoder no Jetson precisa de teto.
PREVIEW_MAX_ATTEMPTS = int(os.environ.get("PREVIEW_MAX_ATTEMPTS", "6"))
# Encoders do preview, em ordem de preferência: hardware primeiro, software
# depois. Cada entrada carrega o **codec** do fluxo publicado, porque ele muda
# o payloader/parser da cadeia. Medido em 2026-11-01 no Orin Nano:
#   - nenhum `/dev/v4l2-nvenc` (o elemento `nvv4l2h264enc` existe mas falha no
#     PLAYING), então o caminho de hardware não é opção;
#   - a base DeepStream 9.1 (Ubuntu 24.04) **não tem encoder H.264 por
#     software**: `gstreamer1.0-plugins-ugly` não traz mais o `x264enc` e a
#     `libavcodec` de lá expõe só o *decoder* H.264 (`avdec_h264`), sem
#     `avenc_h264`;
#   - o que existe é `vp8enc`/`vp9enc` — depois de restaurar `libvpx9`, que a
#     base remove deixando o dpkg "installed" (ver `deploy/Dockerfile`).
# VP8 é aceito nativamente pelo WebRTC do MediaMTX, então no Nano o preview
# sai em VP8 em vez de derrubar o boot. Efeito colateral consciente: **HLS
# não carrega VP8** (HLS é H.264/H.265), então nessa plataforma o fallback
# HLS do dashboard fica sem preview — WebRTC, que é o caminho preferido, ok.
PREVIEW_ENCODERS = (
    ("nvv4l2h264enc",
     {"bitrate": PREVIEW_BITRATE,
      "idrinterval": PREVIEW_IDR_INTERVAL,
      "insert_sps_pps": True},
     "video/x-raw(memory:NVMM),format=NV12",
     "h264"),
    ("x264enc",
     {"bitrate": max(1, PREVIEW_BITRATE // 1000),      # kbps no x264enc
      "key_int_max": PREVIEW_IDR_INTERVAL,
      "tune": "zerolatency",
      "speed_preset": "ultrafast"},
     "video/x-raw,format=I420",
     "h264"),
    ("vp8enc",
     {"target_bitrate": PREVIEW_BITRATE,       # bps
      "keyframe_max_dist": PREVIEW_IDR_INTERVAL,
      "deadline": 1,                           # 1 = realtime
      "cpu_used": 4},
     "video/x-raw,format=I420",
     "vp8"),
)
# Ritmo do timer que despacha o trabalho do ramo de preview para uma thread
# própria e observa o resultado. Nada de GStreamer do preview no thread do loop
# GLib: medido em 2026-11-01, `rtspclientsink.set_state(NULL)` chamado no
# callback do bus ficou preso no mutex do elemento (sink que falhou em
# conectar, MediaMTX fora do ar) e o loop parou de despachar timers e
# `SIGTERM` — `interval_metrics` sumia e o restart do watchdog virava
# `SIGKILL`. Ver `preview_control.py`.
PREVIEW_STEP_MS = int(os.environ.get("PREVIEW_STEP_MS", "250"))
# Teto de uma operação de GStreamer do ramo (derrubar ou recriar). Estourado
# o teto, o preview é dado como perdido e a análise segue publicando métricas.
PREVIEW_OP_TIMEOUT_S = float(
    os.environ.get("PREVIEW_OP_TIMEOUT_S", "10"))

# Códigos de saída do processo (observabilidade via docker inspect).
EXIT_OK = 0
EXIT_PIPELINE_ERROR = 1
EXIT_SOURCE_LOST = 2


def log_event(event, **fields):
    """Emite um log estruturado em JSON (uma linha por evento).

    Todo registro recebe o namespace da sessão (`source_id`,
    `session_epoch`) enquanto houver fonte resolvida (E3-T2), seja qual for o
    thread que chamou — inclusive os callbacks de pad probe. Campo explícito
    vence (`with_namespace` usa `setdefault`), então quem já declarou
    `source_id` não é sobrescrito. Sem sessão, o registro sai como sempre saiu:
    o formato é aditivo, e `scripts/watchdog.py` e o dashboard continuam
    lendo os campos antigos sem mudança.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    record = with_namespace(record, SESSION)
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
        # Código de saída e loop existem antes de tudo: o handler de sinal é
        # instalado logo abaixo e pode rodar com o resto do objeto ainda em
        # construção (não há mais nada de GStreamer criado nesse ponto).
        self.exit_code = EXIT_OK
        self.loop = None
        self._shutdown_pending = False
        # `SIGTERM`/`SIGINT` o mais cedo possível. O processo é PID 1 no
        # container e **o kernel ignora sinais de ação default no init**: um
        # `SIGTERM` que chega antes deste `install()` (carregamento do engine
        # TensorRT leva segundos) não derruba nem é atendido — o Docker espera
        # a carência e mata com `SIGKILL` (exit 137). Medido em 2026-10-30 num
        # restart do watchdog durante o boot do pipeline.
        self.shutdown = GracefulShutdown(GLib, self._request_shutdown)
        self.shutdown.install()

        # Fonte ativa por configuração (E2-T1/T2): `ACTIVE_SOURCE` (env) >
        # `data/config/active_source` > `default_source` do sources.yaml > 1ª
        # habilitada. Credencial nunca embutida no código (DIV-001): a URI da
        # câmera vem de `RTSP_URI` via `uri_env` no YAML, ou do fallback legado
        # de `sources.py` quando não há YAML (`VSL_SOURCE_SELECTION=off`).
        # Trocar de fonte reinicia o processo (D4): nada de reconfiguração em
        # runtime, nenhum estado atravessa fontes.
        self.source = resolve_active_source(load_sources())
        # Sessão da fonte (E3-T2): daqui em diante todo evento de telemetria
        # carrega `source_id` + `session_epoch` (injetados por `log_event`) e o
        # `startup` já pode identificar a fonte. A sessão nasce **depois** de
        # resolver a fonte porque é ela que dá identidade ao namespace.
        self.session = begin_session(self.source.id)
        self.rtsp_uri = self.source.uri
        # Codec é da fonte, não do container (D5): câmera H.265, drone/celular
        # H.264. `RTSP_CODEC` continua existindo só no fallback legado.
        self.codec = self.source.codec
        self.depay_factory, self.parser_factory = depay_factory(self.codec)
        # `startup` sai aqui — fonte e codec resolvidos, antes de qualquer outro
        # evento desta execução (o `preview_branch_ready` é emitido no fim
        # deste `__init__`). O dashboard abre uma "run" a cada `startup`
        # (`dashboard/backend/app.py`, `DashboardState.apply`): um evento que
        # saísse antes cairia na run anterior. O aceite do E3-T2 exige
        # `source_id`, `codec`, `target_fps` e `session_epoch` no startup, e
        # esses só existem depois de resolver a fonte.
        log_event("startup",
                  gst_version=Gst.version_string(),
                  pyds_version=getattr(pyds, "__version__", "unknown"),
                  preview_enabled=PREVIEW_ENABLED,
                  source_id=self.source.id,
                  source_name=self.source.name,
                  codec=self.codec,
                  target_fps=self.source.target_fps)
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
        self._last_input_frame_at = time.monotonic()
        self._last_reconnect_at = 0.0
        self.source_budget = SourceReconnectBudget(
            SOURCE_RECONNECT_MAX_ATTEMPTS, SOURCE_RECONNECT_RESET_S)
        # Caminho da reconexão da fonte: a máquina decide (puro), o timer
        # `_reconnect_step` executa. Substituiu o `set_state(NULL)` +
        # `send_event(flush_stop)` feitos dentro do callback do bus, que
        # travavam o loop GLib (thread dump em `source_reconnect.py`).
        self.reconnect = SourceReconnectMachine(
            self.source_budget,
            stop_timeout_s=SOURCE_STOP_TIMEOUT_S,
            frame_timeout_s=SOURCE_FRAME_TIMEOUT_S,
            retry_base_s=SOURCE_RETRY_BASE_S,
            retry_max_s=SOURCE_RETRY_MAX_S)
        # Uma exceção na fatia não pode inundar o log a cada 250 ms.
        self._reconnect_error_logged = False

        # Ramo de preview (descartável): estado da queda e da retomada.
        # A decisão é pura (`preview_control.py`); as mutações de GStreamer
        # rodam numa thread própria, despachadas por `_preview_step`.
        self.preview = preview_control.PreviewBranchController(
            retry_base_s=PREVIEW_RETRY_BASE_S,
            retry_max_s=PREVIEW_RETRY_MAX_S,
            verify_s=PREVIEW_VERIFY_S,
            verify_min_frames=PREVIEW_VERIFY_MIN_FRAMES,
            max_attempts=PREVIEW_MAX_ATTEMPTS,
            op_timeout_s=PREVIEW_OP_TIMEOUT_S)
        self._preview_chain = ()
        self._preview_element_names = frozenset()
        self._preview_tee_pad = None
        # Operação em voo: nome, evento de conclusão e erro (se houve).
        self._preview_op = None
        self._preview_op_done = threading.Event()
        self._preview_op_done.set()
        self._preview_op_error = None
        self._preview_op_started_at = 0.0

        self.pipeline = Gst.Pipeline.new("vision-phase1")

        # `location`/`latency`/`protocols` vêm da fonte ativa: a câmera usa
        # 200 ms e TCP (4), como antes; o valor fica no YAML por fonte.
        self.rtspsrc = self._make("rtspsrc", location=self.rtsp_uri,
                                  latency=self.source.latency_ms,
                                  drop_on_latency=True,
                                  protocols=self.source.protocols)
        self.depay = self._make(self.depay_factory)
        self.parser = self._make(self.parser_factory)
        self.decoder = self._make("nvv4l2decoder")

        self.mux = self._make("nvstreammux", batch_size=1, width=1280, height=720,
                              batched_push_timeout=40000, live_source=True)

        infer_config = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "config", "infer.txt")
        self.infer = self._make("nvinfer", config_file_path=infer_config)

        tracker_config = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "config", "config_tracker_IOU.yml")
        # Lib de baixo nível do tracker: o caminho **sem versão**
        # (`/opt/nvidia/deepstream/deepstream/lib`) é symlink estável nas duas
        # bases — com `deepstream-7.1` fixo o `dlopen` falha no DeepStream 9.1
        # e o `nvtracker` derruba o `set_state(PLAYING)` do pipeline inteiro
        # (medido no Orin Nano em 2026-11-01). Sobrescreva com
        # `NVTRACKER_LL_LIB` se a base mudar de layout.
        ll_lib = os.environ.get(
            "NVTRACKER_LL_LIB",
            "/opt/nvidia/deepstream/deepstream/lib/"
            "libnvds_nvmultiobjecttracker.so")
        self.tracker = self._make("nvtracker",
                                  ll_lib_file=ll_lib,
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

        for element in (self.depay, self.parser, self.decoder, self.mux,
                        self.infer, self.tracker, self.osd, self.tee,
                        self.queue_analyze, self.sink):
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
            self._install_preview_branch(self._make_preview_elements())
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
        # EOS da fonte morre no tee (ver `_on_source_eos_event`): sem isso os
        # dois ramos ficam presos no estado de fim de stream e o pipeline
        # continua "rodando" com `fps_processed = 0` depois de reconectar.
        self.tee.get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_source_eos_event, None)
        self._source_eos_pending = False

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
        self._last_input_frame_at = time.monotonic()
        # Frame voltou, mas o orçamento de reconexões só é devolvido depois de
        # SOURCE_RECONNECT_RESET_S sem EOS: zerar a cada frame fazia fonte
        # oscilante nunca esgotar as tentativas (exit 2 nunca acontecia).
        if self.source_budget.on_frame(self._last_input_frame_at):
            log_event("source_reconnect_budget_reset",
                      attempts_cleared=True,
                      stable_s=SOURCE_RECONNECT_RESET_S)
        return Gst.PadProbeReturn.OK

    def _on_preview_buffer(self, _pad, info, _user_data):
        self.fps_preview.tick()
        return Gst.PadProbeReturn.OK

    def _on_source_eos_event(self, _pad, info, _user_data):
        """EOS da fonte **morre no tee**: os ramos não entram em fim de stream.

        Por que o EOS não pode passar: um EOS deixa `queue`/`fakesink` (e o
        ramo de preview) presos no estado de fim de stream — depois de
        reconectar a fonte o pipeline continuava "rodando" com
        `fps_processed = 0` / `fps_preview = 0` (medido em 2026-10-30: 30 fps
        na entrada, zero processados). A limpeza usada antes era um
        `flush_stop` no pad do tee, e era exatamente ela que travava o loop
        GLib: evento serializado esperando stream lock de pad com a thread de
        streaming do `nvstreammux` parada (thread dump em `source_reconnect.py`).

        Contendo o EOS aqui o problema não existe: os ramos seguem vivos e
        voltam a receber buffers quando o `rtspsrc` reconecta. Quem reconecta
        é o timer `_reconnect_step` — este probe roda na thread de streaming e
        não chama GStreamer.
        """
        event = info.get_event()
        if event is None or event.type != Gst.EventType.EOS:
            return Gst.PadProbeReturn.OK
        self._source_eos_pending = True
        log_event("eos", src="tee",
                  action="EOS contido no tee: análise e preview seguem vivos; "
                         "reconexão agendada")
        return Gst.PadProbeReturn.DROP

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

    def _preview_branch_owner(self, element):
        """Nome do elemento do ramo de preview que é `element` ou seu ancestral.

        O `rtspclientsink` é um bin: erros dos filhos internos chegam ao bus
        com o nome do filho, não com o do sink. Subir a árvore resolve isso
        sem depender do texto da mensagem.
        """
        node = element
        for _ in range(8):
            if node is None:
                return None
            try:
                name = node.get_name()
            except AttributeError:
                return None
            if name in self._preview_element_names:
                return name
            node = node.get_parent()
        return None

    def _quit_loop(self):
        if self.loop is not None:
            self.loop.quit()
        else:
            # Sinal/falha antes de o loop existir: `run()` respeita a bandeira
            # em vez de rodar para sempre (o `loop.quit()` não teria efeito).
            self._shutdown_pending = True

    def _request_shutdown(self, signum=None):
        """Parada limpa pedida por `SIGTERM`/`SIGINT` (roda no thread do loop).

        Roda no thread do `GLib.MainLoop` (a callback do sinal é uma fonte do
        loop), então `loop.quit()` é seguro; o `set_state(NULL)` e a linha
        `pipeline_stopped` ficam no `finally` de `run()` — nada de GStreamer
        dentro do handler de sinal.

        `exit_code` não é sobrescrito: se o pipeline já falhou (`_fail`), o
        processo continua saindo != 0 mesmo que o sinal chegasse depois.

        Sem `source_id` explícito de propósito (E3-T2): quem injeta o namespace
        é `log_event`, e este handler pode rodar **antes** de a fonte ser
        resolvida (`self.source` ainda não existe → `AttributeError` dentro do
        handler de sinal). Nesse caso o evento sai sem fonte, que é a verdade.
        """
        log_event("shutdown_requested",
                  signal=signal_name(signum),
                  exit_code=self.exit_code)
        self._quit_loop()

    def _fail(self, reason, exit_code, **fields):
        """Encerra o processo por erro real, com código de saída != 0."""
        self.exit_code = exit_code
        log_event("pipeline_failed", reason=reason, exit_code=exit_code,
                  **fields)
        self._quit_loop()

    def _on_bus_message(self, _bus, message):
        t = message.type
        if t not in (Gst.MessageType.EOS, Gst.MessageType.ERROR,
                     Gst.MessageType.WARNING):
            return True

        src = message.src
        try:
            src_name = src.get_name() if src is not None else ""
        except AttributeError:
            src_name = ""
        owner = self._preview_branch_owner(src)
        if owner:
            src_name = owner

        if t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            log_event("pipeline_warning", src=src_name, warning=str(warn),
                      debug=debug)
            return True

        if t == Gst.MessageType.EOS:
            log_event("eos", src=src_name)
            action = classify_bus_message(MSG_EOS, src_name,
                                         preview_elements=self._preview_element_names)
            detail = "eos"
            reason = "eos"
        else:
            err, debug = message.parse_error()
            detail = str(err)
            reason = "bus_error"
            action = classify_bus_message(MSG_ERROR, src_name, detail,
                                          self._preview_element_names)
            # Nem todo ERROR do bus é falha do pipeline. Erro do `rtspsrc` com a
            # fonte fora do ar é o caminho **esperado** da reconexão (o MediaMTX
            # recusa sessão para path sem publisher) e erro do ramo de preview é
            # o ramo descartável caindo. Os dois saíam como `pipeline_error`,
            # que é o evento que o dashboard conta em `error_count`: medido na
            # sessão de campo do drone (2026-10-31), um corte de 23 s que o
            # pipeline reconectou sozinho gerou 4 "erros" na tela. Só
            # `ACTION_QUIT` é falha de verdade e mantém o nome `pipeline_error`.
            if action == ACTION_RECONNECT:
                log_event("rtsp_reconnect_error", src=src_name,
                          error=detail, debug=debug)
            elif action == ACTION_DROP_PREVIEW:
                log_event("preview_branch_error", src=src_name,
                          error=detail, debug=debug)
            else:
                log_event("pipeline_error", src=src_name, error=detail,
                          debug=debug)

        if action == ACTION_DROP_PREVIEW:
            self._drop_preview_branch(element=src_name, error=detail)
        elif action == ACTION_RECONNECT:
            # Fonte caiu: só agenda (o callback do bus roda no thread do loop
            # GLib e não pode esperar GStreamer — ver `source_reconnect.py`).
            self._on_source_failure(src=src_name, reason=reason, error=detail)
        elif action == ACTION_QUIT:
            self._fail("bus_error", EXIT_PIPELINE_ERROR, src=src_name,
                       error=detail)
        return True

    # -- ramo de preview: construção, queda e retomada -----------------------
    def _make_preview_encoder(self):
        """Escolhe o encoder (e o codec) do preview que existe nesta base.

        Hardware primeiro (`nvv4l2h264enc`); depois software — `x264enc` quando
        a base tiver, senão `vp8enc`. O Orin Nano não expõe
        `/dev/v4l2-nvenc` e a base DS 9.1 não tem encoder H.264 por software,
        então lá o preview sai em **VP8** (WebRTC do MediaMTX aceita; HLS não).
        Uma base sem encoder nenhum vira erro explícito, não crash de boot.

        Returns:
            `(elemento, caps, codec)`: o encoder pronto, a caps que combina com
            ele (NVMM/NV12 no hardware; memória de sistema no software) e o
            codec do fluxo (`h264` ou `vp8`), que define o payloader/parser.
        """
        for factory, props, caps, codec in PREVIEW_ENCODERS:
            if factory == "nvv4l2h264enc" and not os.path.exists(
                    "/dev/v4l2-nvenc"):
                # Sem o device de NVENC (Orin Nano) o elemento até é criado,
                # mas falha no PLAYING e queima tentativa de retomada à toa.
                continue
            element = Gst.ElementFactory.make(factory, None)
            if element is None:
                continue
            try:
                for key, value in props.items():
                    element.set_property(key.replace("_", "-"), value)
            except Exception:  # noqa: BLE001 — fábrica de outra versão
                logger.exception("encoder %s recusou as propriedades", factory)
                continue
            log_event("preview_encoder_selected", factory=factory, caps=caps,
                      codec=codec, bitrate_preview=PREVIEW_BITRATE)
            return element, caps, codec
        raise RuntimeError(
            "nenhum encoder de preview disponível "
            f"(tentados: {[f for f, _p, _c, _k in PREVIEW_ENCODERS]}); "
            "use PREVIEW_ENABLED=0 nesta plataforma")

    def _make_preview_elements(self):
        """Cria os elementos do ramo de preview; devolve a cadeia em ordem.

        Chamado no startup e de novo na retomada. Recriar em vez de reciclar
        é deliberado: um `rtspclientsink` que publicou e recebeu erro não
        volta a publicar num ciclo NULL -> PLAYING — o laço de sessão dele
        morre junto com o erro e a conexão nunca é reaberta.
        """
        # Ramo de preview: leaky downstream com 1 buffer de folga —
        # se o encoder atrasar, frames são descartados antes de
        # propagar backpressure para a inferência.
        self.queue_preview = self._make(
            "queue", **{"leaky": 2, "max_size_buffers": 1,
                        "max_size_bytes": 0, "max_size_time": 0})
        # `nvvideoconvert` serve os dois caminhos: NVMM/NV12 para o encoder de
        # hardware e memória de sistema para o de software.
        self.videoconv = self._make("nvvideoconvert")
        self.encoder, caps, self.preview_codec = self._make_preview_encoder()
        # gst_element_set_property não parseia a string de caps;
        # é preciso um objeto Gst.Caps.
        self.enc_caps = self._make("capsfilter")
        self.enc_caps.set_property("caps", Gst.Caps.from_string(caps))
        # O elemento depois do encoder depende do codec. H.264 entra num
        # `h264parse` (o `rtspclientsink` faz o payload sozinho; no transporte
        # UDP o `rtph264pay` é explícito). VP8 vai **elementar** para o
        # `rtspclientsink`, que escolhe o `rtpvp8pay` via propriedade
        # `payloader` — passar RTP pronto ali não linka (medido em 2026-11-01:
        # `rtpvp8pay0 -> rtspclientsink0` falhava).
        self.media_parse = None
        media_chain = ()
        if self.preview_codec == "h264":
            self.media_parse = self._make("h264parse")
            media_chain = (self.media_parse,)
        else:
            # VP8: sem fixar as caps de saída, o payloader automático do
            # `rtspclientsink` empurra `profile=5` para o encoder, que morre
            # com "Invalid vpx profile (5)" (medido em 2026-11-01). Um
            # capsfilter com `video/x-vp8` fecha a negociação.
            self.codec_caps = self._make("capsfilter")
            self.codec_caps.set_property(
                "caps", Gst.Caps.from_string("video/x-vp8"))
            media_chain = (self.codec_caps,)
        if PREVIEW_TRANSPORT == "rtsp":
            # Fase 4 — publica no MediaMTX via RTSP RECORD
            self.rtsp_client = self._make(
                "rtspclientsink", location=PREVIEW_MTX_URI,
                protocols=4)  # 4 = TCP
            tail = (self.rtsp_client,)
        else:
            # Fase 3 — RTP/UDP direto (testes de viewer lento)
            if self.preview_codec == "h264":
                self.payloader = self._make(
                    "rtph264pay", pt=96, **{"config-interval": -1})
            else:
                self.payloader = self._make("rtpvp8pay", pt=96)
            self.udp_sink = self._make(
                "udpsink", host=PREVIEW_HOST, port=PREVIEW_PORT,
                sync=False, **{"async": False}, qos=False)
            tail = (self.payloader, self.udp_sink)
        return ((self.queue_preview, self.videoconv, self.enc_caps,
                 self.encoder) + media_chain + tail)

    def _install_preview_branch(self, chain, start=False):
        """Adiciona a cadeia ao pipeline, linka internamente e no tee.

        `start=True` (retomada) leva a cadeia a PLAYING à mão: adicionar um
        elemento a um pipeline já PLAYING não o coloca em PLAYING.
        """
        self._preview_chain = chain
        for element in chain:
            self.pipeline.add(element)
        for src, dst in zip(chain, chain[1:]):
            if not src.link(dst):
                raise RuntimeError(
                    "Falha ao linkar ramo de preview: "
                    f"{src.get_name()} -> {dst.get_name()}")
        self.encoder.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_preview_buffer, None)
        # Nomes do ramo descartável: a classificação de erro do bus precisa
        # deles porque `queue` (e `h264parse`, com codec H.264) também existem
        # do lado da análise. Acumula em vez de substituir: um erro atrasado do
        # ramo antigo não pode virar "erro da análise".
        self._preview_element_names = self._preview_element_names | frozenset(
            el.get_name() for el in chain)
        tee_pad = self.tee.get_request_pad("src_%u")
        if tee_pad is None or \
                tee_pad.link(self.queue_preview.get_static_pad("sink")) \
                != Gst.PadLinkReturn.OK:
            raise RuntimeError("Falha ao linkar tee -> queue preview")
        self._preview_tee_pad = tee_pad
        if start:
            # do sink para a fonte: downstream pronto antes de receber buffer
            for element in reversed(chain):
                element.set_state(Gst.State.PLAYING)

    def _detach_preview_branch(self):
        """Corta o tee e descarta a cadeia; a retomada recria do zero."""
        pad, self._preview_tee_pad = self._preview_tee_pad, None
        try:
            if pad is not None:
                peer = pad.get_peer()
                if peer is not None:
                    pad.unlink(peer)
                self.tee.release_request_pad(pad)
        except Exception:  # noqa: BLE001 — nunca derrubar a análise por isso
            logger.exception("falha ao desligar o tee do ramo de preview")
        chain, self._preview_chain = self._preview_chain, ()
        for el in reversed(chain):
            try:
                el.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                logger.exception("falha ao parar elemento do preview")
        for el in reversed(chain):
            try:
                self.pipeline.remove(el)
            except Exception:  # noqa: BLE001
                logger.exception("falha ao remover elemento do preview")
        for name in ("queue_preview", "videoconv", "enc_caps", "encoder",
                     "media_parse", "codec_caps", "rtsp_client", "payloader",
                     "udp_sink"):
            if hasattr(self, name):
                setattr(self, name, None)

    def _drop_preview_branch(self, element, error):
        """Pede a derrubada do ramo de preview — **só marca estado e loga**.

        Roda no thread do loop GLib (callback do bus). A derrubada em si
        (`unlink`, `release_request_pad`, `set_state(NULL)`, `remove`) é
        trabalho de GStreamer e é executada por `_preview_step` numa thread
        própria. Motivo medido em 2026-11-01: `rtspclientsink.set_state(NULL)`
        aqui ficou preso no mutex do elemento enquanto o caminho de erro do
        sink (MediaMTX fora do ar) não liberava, e o loop parou de despachar
        timers e `SIGTERM` — `interval_metrics` sumia e o `docker restart` do
        watchdog virava `SIGKILL`. Mesma classe do INC-006 (`source_reconnect`).
        """
        if not PREVIEW_ENABLED:
            return
        kind = self.preview.request_drop()
        if kind == preview_control.DROP_IGNORED:
            return
        if kind == preview_control.DROP_RETRY:
            # tentativa de retomada falhou de novo: registra e reagenda com
            # backoff (a falha original não é repetida em preview_branch_failed)
            log_event("preview_branch_retry_failed", element=element,
                      error=error, attempt=self.preview.attempts)
        else:
            log_event("preview_branch_failed", element=element, error=error,
                      action="ramo de preview derrubado; análise segue",
                      fps_processed=self.fps_proc.fps)

    def _retry_preview_branch(self):
        """Recria o ramo de preview inteiro e o religa ao tee.

        Alvo do worker de `_preview_step` (nunca o thread do loop): levanta
        exceção em falha, que o timer registra como tentativa gasta.
        """
        self._install_preview_branch(self._make_preview_elements(),
                                     start=True)

    def _start_preview_op(self, name, target):
        """Executa uma mutação de GStreamer do preview fora do thread do loop.

        O loop só observa `_preview_op_done` e o teto de tempo: operação que
        não retorna vira `preview_branch_unavailable`, nunca loop parado.
        """
        done = threading.Event()
        self._preview_op = name
        self._preview_op_done = done
        self._preview_op_error = None
        self._preview_op_started_at = time.monotonic()

        def worker():
            error = None
            try:
                target()
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("operação do preview falhou (%s)", name)
            finally:
                self._preview_op_error = error
                done.set()

        threading.Thread(target=worker, name=f"preview-{name}",
                         daemon=True).start()

    def _preview_step(self):
        """Timer: despacha e observa o trabalho do ramo de preview.

        O thread do loop **não** executa GStreamer do preview — só decide
        (`preview_control.PreviewBranchController`) e, quando a operação
        termina, registra e agenda. Retorno True mantém o timer ativo.
        """
        if not PREVIEW_ENABLED:
            return False
        if self.preview.exhausted:
            # Preview dado como perdido nesta sessão: desliga o timer (a
            # análise, as métricas e o `SIGTERM` já não dependem dele).
            return False
        now = time.monotonic()
        action = self.preview.step(
            now,
            op_done=self._preview_op_done.is_set(),
            frames_total=self.fps_preview.total,
            op_error=self._preview_op_error)
        if action == preview_control.ACTION_DETACH:
            self._start_preview_op("detach", self._detach_preview_branch)
        elif action == preview_control.ACTION_INSTALL:
            self._start_preview_op("install", self._retry_preview_branch)
        elif action == preview_control.ACTION_ARMED:
            log_event("preview_branch_retry_scheduled",
                      delay_s=int(self.preview.delay_s(now)),
                      attempt=self.preview.attempts)
        elif action == preview_control.ACTION_RETRIED:
            log_event("preview_branch_retry",
                      attempt=self.preview.attempts + 1)
        elif action == preview_control.ACTION_RETRY_FAILED:
            log_event("preview_branch_retry_failed",
                      error=self.preview.last_error,
                      attempt=self.preview.attempts)
        elif action == preview_control.ACTION_GIVE_UP:
            log_event("preview_branch_unavailable",
                      attempts=self.preview.attempts,
                      reason=self.preview.give_up_reason,
                      action="preview só volta com reinício do pipeline "
                             "(docker restart vision-stability-app); "
                             "a análise segue sem ele")
        return True

    def _check_preview_alive(self):
        """Timer 5 s: confere se a retomada do preview voltou a dar frames.

        A retomada só é dada como boa quando frames de preview de fato
        chegam: religar os elementos e receber dois buffers não significa que
        o `rtspclientsink` voltou a publicar. Quem derruba/recria é o worker de
        `_preview_step`; aqui só se verifica o resultado.
        Retorno True mantém o timer GLib ativo.
        """
        if not PREVIEW_ENABLED:
            return True
        outcome = self.preview.verify(time.monotonic(), self.fps_preview.total)
        if outcome == preview_control.VERIFY_RESTORED:
            log_event("preview_branch_restored",
                      frames_since_retry=self.preview.last_frames_since_retry,
                      elapsed_s=round(self.preview.last_elapsed_s, 1))
        elif outcome == preview_control.VERIFY_FAILED:
            log_event("preview_branch_retry_failed",
                      error=self.preview.last_error,
                      frames_since_retry=self.preview.last_frames_since_retry,
                      attempt=self.preview.attempts)
        return True

    # -- fonte (rtspsrc) -----------------------------------------------------
    def _on_source_failure(self, src="", reason="eos", error=""):
        """Fonte caiu (EOS) ou o `rtspsrc` errou: agenda a reconexão.

        Roda no thread do loop GLib (callback do bus): **nenhuma** chamada de
        GStreamer aqui. A causa raiz do congelamento de E2 estava exatamente
        nisso — `set_state`/`send_event` no callback esperavam um stream lock
        preso na thread do `nvstreammux` e o loop parava de despachar timers e
        `SIGTERM` (thread dump em `source_reconnect.py`).

        Antes, EOS chamava `loop.quit()`: o pipeline morria, o
        `restart: unless-stopped` recriava o container e o incidente ficava
        mascarado como "reinício".
        """
        attempt = self.reconnect.fail(time.monotonic(), reason)
        if attempt is None:
            # já havia reconexão em curso (ou orçamento esgotado: o timer dá
            # o desfecho na próxima fatia)
            return
        log_event("rtsp_reconnect_started", reason=reason, src=src,
                  error=error or None, attempt=attempt,
                  limit=self.reconnect.max_attempts)

    def _request_source_reconnect(self, reason, **fields):
        """Pede a primeira tentativa de reconexão (só agenda e loga)."""
        attempt = self.reconnect.begin(time.monotonic(), reason)
        if attempt is None:
            return False
        log_event("rtsp_reconnect_started", reason=reason, attempt=attempt,
                  limit=self.reconnect.max_attempts, **fields)
        return True

    def _reconnect_step(self):
        """Fatia curta do caminho de reconexão — única que toca GStreamer.

        Timer GLib: o retorno `True` mantém o ciclo vivo. É de propósito um
        timer **persistente** e não um `timeout_add` reagendado a cada passo:
        um agendamento perdido no meio de uma exceção deixava a reconexão
        pendurada para sempre (suspeita explícita da investigação de E2).

        Cada fatia faz no máximo uma transição de estado, com teto de tempo
        (`SOURCE_STOP_TIMEOUT_S`/`SOURCE_FRAME_TIMEOUT_S`). `get_state` sempre
        com timeout zero: a fatia nunca espera.
        """
        try:
            if self._source_eos_pending:
                # EOS contido no tee (thread de streaming) -> agenda aqui.
                self._source_eos_pending = False
                self._on_source_failure(src="tee", reason="eos")
            note = self.reconnect.note
            if note:
                self.reconnect.note = ""
                log_event("source_reconnect_step_note", note=note,
                          attempt=self.reconnect.attempt,
                          phase=self.reconnect.phase)
            action = self.reconnect.step(
                time.monotonic(),
                stopped=self._source_is_stopped(),
                frames_total=self.fps_in.total)
            self._run_reconnect_action(action)
        except Exception:  # noqa: BLE001 — timer nunca morre calado
            # Uma vez por sequência de falhas: logar a cada 250 ms esconderia
            # o resto da telemetria.
            if not self._reconnect_error_logged:
                self._reconnect_error_logged = True
                logger.exception("_reconnect_step falhou")
        else:
            self._reconnect_error_logged = False
        return True

    def _run_reconnect_action(self, action):
        """Executa a decisão da máquina (fatia curta, uma transição)."""
        if action == STEP_NONE:
            return
        if action == STEP_STOP:
            log_event("source_reconnect_action", action=STEP_STOP,
                      attempt=self.reconnect.attempt)
            self._change_source_state(Gst.State.NULL)
            return
        if action == STEP_START:
            log_event("source_reconnect_action", action=STEP_START,
                      attempt=self.reconnect.attempt)
            self._change_source_state(Gst.State.PLAYING)
            # Carência de um ciclo antes de considerar travado de novo.
            self._last_reconnect_at = time.monotonic()
            return
        if action == STEP_RESUMED:
            log_event("rtsp_reconnect_resumed",
                      attempt=self.reconnect.attempt,
                      reason=self.reconnect.reason)
            return
        if action == STEP_RETRY:
            log_event("rtsp_reconnect_started",
                      reason=self.reconnect.reason,
                      attempt=self.reconnect.attempt,
                      limit=self.reconnect.max_attempts)
            return
        if action == STEP_GIVE_UP:
            self._fail("source_reconnect_exhausted", EXIT_SOURCE_LOST,
                       last_reason=self.reconnect.reason,
                       attempts=self.reconnect.attempts,
                       limit=self.reconnect.max_attempts)

    def _change_source_state(self, state):
        """Muda o estado do `rtspsrc` **sem prender o loop GLib**.

        `gst_element_set_state` espera (`GST_CLOCK_TIME_NONE`) a transição
        assíncrona terminar; com a fonte morta essa espera pode ficar presa no
        stream lock do `nvstreammux`. A chamada roda numa thread própria e o
        timer só *consulta* o estado (`get_state(0)`): se ela travar, métricas
        e `SIGTERM` continuam sendo atendidos.
        """
        def worker():
            try:
                self.rtspsrc.set_state(state)
            except Exception:  # noqa: BLE001
                logger.exception("falha ao mudar o estado do rtspsrc")

        threading.Thread(target=worker, name="source-state",
                         daemon=True).start()

    def _source_is_stopped(self):
        """`rtspsrc` já está em NULL? Consulta sem espera (`get_state(0)`)."""
        try:
            state, _pending, _timeout = self.rtspsrc.get_state(0)
        except Exception:  # noqa: BLE001
            logger.exception("falha ao consultar o estado do rtspsrc")
            return False
        return state == Gst.State.NULL

    def _check_rtsp_alive(self):
        """Regra 1 do watchdog: sem frame por 30s -> reconectar o RTSP.

        Só pede a reconexão (`_request_source_reconnect`): o reset do rtspsrc
        acontece nas fatias do timer `_reconnect_step`, nunca aqui. O
        pad-added religa o fluxo quando a câmera responder. Timer GLib:
        retorno True mantém o ciclo ativo.
        """
        now = time.monotonic()
        idle = now - self._last_input_frame_at
        if idle < RTSP_IDLE_RECONNECT_S or self.reconnect.active:
            return True
        if now - self._last_reconnect_at < RTSP_RECONNECT_COOLDOWN_S:
            return True
        self._request_source_reconnect("no_frames", idle_s=round(idle, 1))
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
                  # Namespace da sessão (E3-T2): qual fonte produziu esta
                  # amostra e desde quando. `log_event` injeta os mesmos dois
                  # campos em todo registro; aqui eles são explícitos porque
                  # `interval_metrics` é o insumo do watchdog (E5-T1) e da
                  # série do dashboard (E4-T1).
                  source_id=self.source.id,
                  source_name=self.source.name,
                  source_target_fps=self.source.target_fps,
                  session_epoch=self.session.epoch,
                  last_frame_age_s=round(
                      time.monotonic() - self._last_input_frame_at, 2),
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
        """Roda o pipeline até o fim. Retorna o código de saída do processo."""
        self.loop = GLib.MainLoop()
        # O handler de `SIGTERM`/`SIGINT` já foi instalado no `__init__` (antes
        # de qualquer elemento): aqui só falta o loop existir. A fonte do sinal
        # é despachada na primeira iteração de `loop.run()`, no thread certo.

        # Evento de startup da fonte (E2-T2): identifica a fonte e o caminho de
        # mídia escolhido sem expor credencial — `mask_uri` troca o userinfo da
        # câmera por `***` e mantém host/porta/path para diagnóstico (DIV-001).
        endpoint = endpoint_parts(self.rtsp_uri)
        log_event("source_active",
                  source_id=self.source.id,
                  source_name=self.source.name,
                  codec=self.codec,
                  depay=self.depay_factory,
                  parser=self.parser_factory,
                  transport=self.source.transport,
                  target_fps=self.source.target_fps,
                  session_epoch=self.session.epoch,
                  uri=mask_uri(self.rtsp_uri),
                  host=endpoint["host"],
                  port=endpoint["port"],
                  path=endpoint["path"])

        if TEST_DURATION_SECONDS > 0:
            GLib.timeout_add_seconds(TEST_DURATION_SECONDS, self._stop)
            log_event("test_started", duration_s=TEST_DURATION_SECONDS,
                      codec=self.codec, source_id=self.source.id,
                      uri=mask_uri(self.rtsp_uri))
        GLib.timeout_add_seconds(METRICS_INTERVAL_SECONDS, self.periodic_metrics)
        GLib.timeout_add_seconds(5, self._check_rtsp_alive)
        # Fatias da reconexão da fonte. Timer persistente de propósito: é o
        # único agendamento do caminho, e cada fatia tem teto de tempo (nada
        # de trabalho de GStreamer no callback do bus ou no `_check_rtsp_alive`).
        GLib.timeout_add(SOURCE_RECONNECT_STEP_MS, self._reconnect_step)
        if PREVIEW_ENABLED:
            GLib.timeout_add_seconds(5, self._check_preview_alive)
            # Trabalho do ramo de preview em thread própria: o loop só
            # despacha e observa (ver `preview_control.py`).
            GLib.timeout_add(PREVIEW_STEP_MS, self._preview_step)

        # O retorno do `set_state` importa: FAILURE significa que um elemento
        # não passou a PLAYING (ex.: `nvtracker` com `ll-lib-file` inválido na
        # base de imagem) e o pipeline fica "rodando" sem nunca receber frame.
        # Medido em 2026-11-01 no Orin Nano: sem esta checagem o log dizia
        # `pipeline_playing` e a telemetria ficava em `fps_in=0` para sempre,
        # sem nenhum erro visível.
        state_ret = self.pipeline.set_state(Gst.State.PLAYING)
        if state_ret == Gst.StateChangeReturn.FAILURE:
            # O elemento culpado publica ERROR no bus; o callback do bus já
            # registra (`pipeline_error`), mas o processo não pode seguir.
            log_event("pipeline_state_failed", codec=self.codec,
                      source_id=self.source.id,
                      action="pipeline não chegou a PLAYING; encerrando para "
                             "o compose reiniciar com a causa no log")
            self._teardown()
            return EXIT_PIPELINE_ERROR
        log_event("pipeline_playing", codec=self.codec,
                  source_id=self.source.id,
                  state_return=getattr(state_ret, "value_nick", str(state_ret)))

        try:
            if self._shutdown_pending:
                # Sinal chegou durante o boot (antes de o loop existir).
                log_event("shutdown_before_loop",
                          source_id=self.source.id)
            else:
                self.loop.run()
        except KeyboardInterrupt:
            log_event("interrupted_by_user")
        finally:
            try:
                self.periodic_metrics()
            except Exception:
                logger.exception("metrics de shutdown falharam")
            finally:
                self._teardown()
                log_event("pipeline_stopped", exit_code=self.exit_code,
                          signal=(self.shutdown.received_signal
                                  if self.shutdown is not None else None))
        return self.exit_code

    def _teardown(self):
        """`set_state(NULL)` do pipeline com teto de tempo.

        O `docker stop` precisa voltar em ≤ 2 s mesmo com a fonte morta; se a
        parada ficar presa num stream lock (a mesma classe de espera que
        congelava o loop), o processo sai com o `exit_code` certo em vez de
        esperar a carência do Docker e morrer por `SIGKILL` (exit 137).

        A parada roda numa thread daemon: se o teto estourar, o `finally` do
        `run()` continua e o interpretador encerra sem juntar a thread.
        """
        done = threading.Event()

        def worker():
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                logger.exception("falha ao parar o pipeline")
            finally:
                done.set()

        threading.Thread(target=worker, name="pipeline-teardown",
                         daemon=True).start()
        if not done.wait(PIPELINE_STOP_TIMEOUT_S):
            log_event("pipeline_stop_timeout",
                      timeout_s=PIPELINE_STOP_TIMEOUT_S,
                      action="set_state(NULL) não confirmou; processo sai "
                             "assim mesmo para não virar exit 137")

    def _stop(self):
        log_event("test_duration_reached")
        self._quit_loop()
        return False


def main():
    Gst.init(None)
    # O `startup` é emitido pelo `VisionPipeline.__init__`, logo depois de a
    # fonte ser resolvida (E3-T2): é a primeira oportunidade de o evento
    # carregar `source_id`/`session_epoch`/`codec`/`target_fps` e continua
    # sendo o primeiro evento da execução (o dashboard abre a run nele).
    try:
        app = VisionPipeline()
    except Exception as exc:  # noqa: BLE001
        # Erro de configuração de fonte (id inexistente em `ACTIVE_SOURCE`,
        # `uri_env` ausente, codec inválido) precisa ser legível no
        # `docker logs`: uma linha "ERRO: ...", nunca um traceback cru. Se a
        # falha for antes de a fonte existir, `startup_failed` sai sem
        # namespace — e essa é a informação correta: não houve sessão.
        logger.error("ERRO: %s", exc)
        log_event("startup_failed", error=str(exc))
        return EXIT_PIPELINE_ERROR
    exit_code = app.run()
    # Exit 0 só para encerramento limpo (CTRL/duração de teste). Erro de
    # pipeline e fonte perdida precisam sair != 0 para o compose e a
    # observabilidade distinguirem de saída normal.
    if exit_code:
        log_event("process_exit", exit_code=exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
