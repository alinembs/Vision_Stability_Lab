#!/usr/bin/env python3
"""Sonda de saúde de um stream RTSP do MediaMTX (pré-check de troca de fonte).

Lê um path do MediaMTX por RTSP e mede o que o plano de troca de fonte precisa
saber antes de mexer em qualquer coisa: chega frame? com que fps? com que
buracos? e há quanto tempo o primeiro frame demorou?

Contrato de saída (é o que `scripts/switch_source.sh` vai consumir, D7):
  - stdout: **uma** linha JSON com as métricas;
  - stderr: resumo legível (suprimido com `--json`);
  - exit code 0 = PASS, 1 = FAIL.

PASS exige, simultaneamente: `frames >= --min-frames`, `fps >= --min-fps` e o
primeiro frame chegou dentro de `--seconds`. Qualquer erro de GStreamer é
capturado no bus e vira `result: FAIL` com `reason` — nunca traceback.

Codec: o `rtspsrc` só entrega os pads quando o DESCRIBE volta, então o codec é
descoberto no `pad-added` pelas caps do RTP (`encoding-name`) e o depayloader
correspondente é criado na hora — H.264 (`rtph264depay`/`h264parse`) ou H.265
(`rtph265depay`/`h265parse`). Isso aceita as três fontes do projeto sem
parametrizar codec na linha de comando.

Não decodifica: `fakesink` com `sync=false` conta frames de graça (sem
`nvv4l2decoder`, então roda no host e dentro de container indistintamente).

Dependências: PyGObject (`python3-gi`) + GStreamer 1.0 do host. Nada além.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402  (exige require_version antes)

# Janela de gap considerada "buraco" na entrega de frames.
GAP_THRESHOLD_MS = 500.0

# Folga acima de --seconds antes do timeout duro (pipeline travado).
HARD_TIMEOUT_SLACK_S = 5

# rtspsrc `protocols` é uma máscara de flags (GstRtsp.RTSPLowerTrans):
# UDP unicast = 1, TCP = 4. Valores literais para não exigir o typelib GstRtsp.
TRANSPORT_MASKS = {"udp": 1, "tcp": 4}

# Codec normalizado -> (depayloader, parser)
CODEC_FACTORIES: Dict[str, Tuple[str, str]] = {
    "h264": ("rtph264depay", "h264parse"),
    "h265": ("rtph265depay", "h265parse"),
}

# `encoding-name` do SDP -> codec normalizado
ENCODING_TO_CODEC: Dict[str, str] = {
    "H264": "h264",
    "H265": "h265",
    "HEVC": "h265",
}


# ---------------------------------------------------------------------------
# Funções puras (testadas em scripts/tests/test_probe_logic.py)
# ---------------------------------------------------------------------------
def validate_url(url: str) -> str:
    """Valida a URL de leitura e a devolve normalizada.

    Só aceita RTSP: a sonda mede paths do MediaMTX. Levanta `ValueError` com
    mensagem acionável para entrada vazia, esquema errado ou host ausente.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("--url vazia: informe rtsp://host:porta/path")
    if "://" not in candidate:
        raise ValueError(
            f"--url inválida ({candidate!r}): falta o esquema rtsp://"
        )
    scheme, rest = candidate.split("://", 1)
    if scheme.lower() not in ("rtsp", "rtsps"):
        raise ValueError(
            f"--url inválida ({candidate!r}): esquema {scheme!r} não é rtsp/rtsps"
        )
    if not rest or rest.startswith("/"):
        raise ValueError(f"--url inválida ({candidate!r}): host ausente")
    return candidate


def transport_mask(name: str) -> int:
    """Converte o nome do transporte no valor da propriedade `protocols`."""
    key = (name or "").strip().lower()
    if key not in TRANSPORT_MASKS:
        raise ValueError(
            f"transporte inválido ({name!r}): use 'tcp' ou 'udp'"
        )
    return TRANSPORT_MASKS[key]


def codec_factories(encoding_name: Optional[str]) -> Optional[Tuple[str, str]]:
    """Devolve (depayloader, parser) para um `encoding-name` de RTP.

    `None` quando o codec não é H.264/H.265 — a sonda falha alto em vez de
    mascarar um stream que o pipeline também não conseguiria consumir.
    """
    if not encoding_name:
        return None
    return CODEC_FACTORIES.get(ENCODING_TO_CODEC.get(encoding_name.strip().upper(), ""))


def compute_gaps(
    timestamps_s: Sequence[float], threshold_ms: float = GAP_THRESHOLD_MS
) -> Dict[str, float]:
    """Estatística dos intervalos entre frames consecutivos (em ms).

    `timestamps_s` são instantes de chegada monotônicos. Sem dois frames não há
    intervalo, então tudo volta zerado (e `decide()` reprova por falta de frame).
    """
    gaps = [
        (later - earlier) * 1000.0
        for earlier, later in zip(timestamps_s, timestamps_s[1:])
    ]
    if not gaps:
        return {"gaps_over_500ms": 0, "max_gap_ms": 0.0, "avg_gap_ms": 0.0}
    return {
        "gaps_over_500ms": sum(1 for gap in gaps if gap > threshold_ms),
        "max_gap_ms": round(max(gaps), 3),
        "avg_gap_ms": round(sum(gaps) / len(gaps), 3),
    }


def measure_fps(timestamps_s: Sequence[float]) -> float:
    """fps medido na janela entre o primeiro e o último frame recebido."""
    if len(timestamps_s) < 2:
        return 0.0
    window = timestamps_s[-1] - timestamps_s[0]
    if window <= 0:
        return 0.0
    return round((len(timestamps_s) - 1) / window, 3)


def decide(
    *,
    frames: int,
    fps: float,
    first_frame_s: Optional[float],
    min_frames: int,
    min_fps: float,
    seconds: float,
    error: Optional[str] = None,
) -> Tuple[str, str]:
    """Decide PASS/FAIL e o motivo, na ordem em que faz diferença para operação.

    Retorna `(result, reason)`. O motivo do primeiro critério violado é o que
    aparece — é ele que o `switch_source.sh` vai imprimir.
    """
    if error:
        return "FAIL", f"pipeline_error: {error}"
    if frames <= 0:
        return "FAIL", "no_frames"
    if first_frame_s is None or first_frame_s > seconds:
        return "FAIL", "first_frame_too_late"
    if frames < min_frames:
        return "FAIL", f"frames_below_min ({frames} < {min_frames})"
    if fps < min_fps:
        return "FAIL", f"fps_below_min ({fps} < {min_fps})"
    return "PASS", "ok"


# ---------------------------------------------------------------------------
# Leitura via GStreamer
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    """Acumulador do que a sonda observou em uma execução."""

    timestamps: List[float] = field(default_factory=list)
    codec: Optional[str] = None
    error: Optional[str] = None
    eos: bool = False
    hard_timeout: bool = False

    def add_frame(self, offset_s: float) -> None:
        self.timestamps.append(offset_s)


class RtspReadProbe:
    """Monta `rtspsrc ! depay ! parse ! fakesink` e mede a entrega de frames."""

    def __init__(self, url: str, transport: str = "tcp", verbose: bool = False) -> None:
        self.url = url
        self.transport = transport
        self.verbose = verbose
        self.result = ProbeResult()
        self.pipeline: Optional[Gst.Pipeline] = None
        self.sink: Optional[Gst.Element] = None
        self.src: Optional[Gst.Element] = None
        self._start_t: float = 0.0
        self._loop: Optional[GLib.MainLoop] = None
        self._linked = False
        # Fontes GLib que já dispararam não podem ser removidas de novo.
        self._soft_fired = False
        self._hard_fired = False

    # -- log ---------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[probe] {message}", file=sys.stderr)

    # -- construção --------------------------------------------------------
    def _build(self) -> None:
        pipeline = Gst.Pipeline.new("rtsp-read-probe")

        src = Gst.ElementFactory.make("rtspsrc", "src")
        if src is None:
            raise RuntimeError("elemento rtspsrc indisponível (plugins RTSP ausentes)")
        src.set_property("location", self.url)
        src.set_property("protocols", transport_mask(self.transport))
        src.set_property("latency", 200)
        pipeline.add(src)
        src.connect("pad-added", self._on_pad_added)

        sink = Gst.ElementFactory.make("fakesink", "sink")
        if sink is None:
            raise RuntimeError("elemento fakesink indisponível")
        # sync=False: não esperamos o relógio do pipeline; medimos a chegada.
        # async=False: não bloqueia o preroll esperando mídia (queremos falhar
        # rápido quando o path está mudo, não travar no PAUSED).
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("enable-last-sample", False)
        pipeline.add(sink)

        sinkpad = sink.get_static_pad("sink")
        sinkpad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)

        self.pipeline = pipeline
        self.src = src
        self.sink = sink

    def _on_pad_added(self, _src: Gst.Element, pad: Gst.Pad) -> None:
        """Religa o pad do rtspsrc ao depayloader do codec anunciado no SDP."""
        if self._linked:
            return
        caps = pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            return
        structure = caps.get_structure(0)
        if structure.get_name() != "application/x-rtp":
            return
        encoding = structure.get_string("encoding-name")
        factories = codec_factories(encoding)
        if factories is None:
            self.result.error = f"codec não suportado: {encoding!r}"
            self._quit()
            return
        depay_name, parse_name = factories
        self.result.codec = ENCODING_TO_CODEC.get((encoding or "").upper())
        self._log(f"codec={self.result.codec} ({depay_name} ! {parse_name})")

        assert self.pipeline is not None and self.sink is not None
        depay = Gst.ElementFactory.make(depay_name, None)
        parse = Gst.ElementFactory.make(parse_name, None)
        if depay is None or parse is None:
            self.result.error = f"elementos ausentes: {depay_name}/{parse_name}"
            self._quit()
            return
        self.pipeline.add(depay)
        self.pipeline.add(parse)

        if not (depay.link(parse) and parse.link(self.sink)):
            self.result.error = f"falha ao ligar {depay_name} ! {parse_name}"
            self._quit()
            return
        depay.sync_state_with_parent()
        parse.sync_state_with_parent()

        if pad.link(depay.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            self.result.error = f"falha ao ligar o pad do rtspsrc em {depay_name}"
            self._quit()
            return
        self._linked = True

    def _on_buffer(self, _pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        self.result.add_frame(time.monotonic() - self._start_t)
        return Gst.PadProbeReturn.OK

    # -- bus ---------------------------------------------------------------
    def _on_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        kind = message.type
        if kind == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            detail = error.message
            if debug:
                detail = f"{detail} ({str(debug).splitlines()[0]})" if self.verbose else detail
            self.result.error = detail
            self._quit()
        elif kind == Gst.MessageType.EOS:
            self.result.eos = True
            self._quit()

    # -- ciclo de vida -----------------------------------------------------
    def _quit(self) -> bool:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        return GLib.SOURCE_REMOVE

    def _on_soft_deadline(self) -> bool:
        # Janela cumprida: o resultado continua sendo decidido pelas métricas.
        self._soft_fired = True
        return self._quit()

    def _on_hard_deadline(self) -> bool:
        self._hard_fired = True
        self.result.hard_timeout = True
        self.result.error = "timeout duro: pipeline não respondeu"
        return self._quit()

    def run(self, seconds: float) -> ProbeResult:
        """Roda a leitura por até `seconds` e devolve o resultado observado."""
        if self.verbose:
            Gst.debug_set_default_threshold(4)

        self._build()
        assert self.pipeline is not None

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

        loop = GLib.MainLoop()
        self._loop = loop

        signal_ids = [
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self._quit),
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self._quit),
        ]
        soft_id = GLib.timeout_add(int(seconds * 1000), self._on_soft_deadline)
        hard_id = GLib.timeout_add(
            int((seconds + HARD_TIMEOUT_SLACK_S) * 1000), self._on_hard_deadline
        )

        self._log(f"abrindo {self.url} transport={self.transport} por {seconds}s")
        self._start_t = time.monotonic()
        self.pipeline.set_state(Gst.State.PLAYING)

        try:
            loop.run()
        except KeyboardInterrupt:  # pragma: no cover - fallback fora do GLib
            self.result.error = self.result.error or "interrompido pelo usuário"
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_state(Gst.CLOCK_TIME_NONE)  # espera soltar os sockets
            for source_id, fired in (
                (soft_id, self._soft_fired),
                (hard_id, self._hard_fired),
            ):
                if source_id and not fired:
                    GLib.source_remove(source_id)
            for source_id in signal_ids:
                if source_id:
                    GLib.source_remove(source_id)
            bus.remove_signal_watch()

        return self.result


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
def build_report(
    *,
    url: str,
    result: ProbeResult,
    seconds: float,
    min_frames: int,
    min_fps: float,
    elapsed_s: float,
    container: str = "rtsp",
) -> Dict[str, Any]:
    """Consolida o observado no JSON de saída (uma linha em stdout)."""
    gaps = compute_gaps(result.timestamps)
    frames = len(result.timestamps)
    fps = measure_fps(result.timestamps)
    first_frame_s = (
        round(result.timestamps[0], 3) if result.timestamps else None
    )
    outcome, reason = decide(
        frames=frames,
        fps=fps,
        first_frame_s=first_frame_s,
        min_frames=min_frames,
        min_fps=min_fps,
        seconds=seconds,
        error=result.error,
    )
    return {
        "url": url,
        "codec": result.codec or "unknown",
        "container": container,
        "first_frame_s": first_frame_s,
        "frames": frames,
        "fps": fps,
        "gaps_over_500ms": gaps["gaps_over_500ms"],
        "max_gap_ms": gaps["max_gap_ms"],
        "avg_gap_ms": gaps["avg_gap_ms"],
        "duration_s": round(elapsed_s, 3),
        "result": outcome,
        "reason": reason,
    }


def format_human(report: Dict[str, Any]) -> str:
    """Resumo de uma linha para quem roda a sonda na mão (vai para stderr)."""
    first = report["first_frame_s"]
    first_txt = f"{first:.2f}s" if first is not None else "—"
    return (
        f"{report['result']} {report['reason']} | codec={report['codec']} "
        f"frames={report['frames']} fps={report['fps']} "
        f"first_frame={first_txt} gaps>500ms={report['gaps_over_500ms']} "
        f"max_gap={report['max_gap_ms']:.0f}ms dur={report['duration_s']:.1f}s"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Interpreta a linha de comando e valida os valores de contrato."""
    parser = argparse.ArgumentParser(
        prog="rtsp_read_probe.py",
        description=(
            "Lê um path RTSP do MediaMTX e mede saúde do stream. "
            "Exit 0 = PASS, exit 1 = FAIL (contrato de pré-check de troca de fonte)."
        ),
        epilog=(
            "Exemplo: rtsp_read_probe.py --url rtsp://192.168.59.102:8554/drone "
            "--seconds 5 --json    # pré-check do switch_source.sh\n\n"
            "Atenção: as portas do MediaMTX são publicadas no IP da LAN "
            "(E0-T2); rtsp://127.0.0.1:8554/... só funciona se o MediaMTX "
            "estiver publicado em loopback.\n\n"
            "O codec (H.264/H.265) é descoberto nas caps do pad do rtspsrc e o "
            "depayloader é criado em tempo de execução; não há flag de codec."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="URL rtsp:// do path a ler")
    parser.add_argument(
        "--seconds", type=float, default=10.0, help="janela de leitura (default: 10)"
    )
    parser.add_argument(
        "--transport",
        default="tcp",
        help="transporte RTSP: tcp (default) ou udp",
    )
    parser.add_argument(
        "--min-fps", type=float, default=5.0, help="fps mínimo para PASS (default: 5.0)"
    )
    parser.add_argument(
        "--min-frames",
        type=int,
        default=10,
        help="frames mínimos recebidos para PASS (default: 10)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="não imprime o resumo legível em stderr (só a linha JSON em stdout)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log de depuração em stderr"
    )
    args = parser.parse_args(argv)

    try:
        args.url = validate_url(args.url)
        args.protocols = transport_mask(args.transport)
    except ValueError as exc:
        parser.error(str(exc))
    if args.seconds <= 0:
        parser.error("--seconds precisa ser maior que zero")
    if args.min_fps < 0:
        parser.error("--min-fps não pode ser negativo")
    if args.min_frames < 0:
        parser.error("--min-frames não pode ser negativo")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    Gst.init([])

    module = RtspReadProbe(args.url, transport=args.transport, verbose=args.verbose)
    report: Dict[str, Any]
    try:
        started = time.monotonic()
        result = module.run(args.seconds)
        report = build_report(
            url=args.url,
            result=result,
            seconds=args.seconds,
            min_frames=args.min_frames,
            min_fps=args.min_fps,
            elapsed_s=time.monotonic() - started,
        )
    except Exception as exc:  # erro de construção/execução do pipeline
        report = {
            "url": args.url,
            "codec": "unknown",
            "container": "rtsp",
            "first_frame_s": None,
            "frames": 0,
            "fps": 0.0,
            "gaps_over_500ms": 0,
            "max_gap_ms": 0.0,
            "avg_gap_ms": 0.0,
            "duration_s": 0.0,
            "result": "FAIL",
            "reason": f"probe_error: {exc}",
        }

    if not args.json:
        print(format_human(report), file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
