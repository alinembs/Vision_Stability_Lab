#!/usr/bin/env python3
"""Relatório de ingestão de um path RTSP: codec, resolução, fps e bitrate.

Existe para a sessão de campo do drone (`docs/runbooks/drone-campo.md`): a
ficha §6 pede **codec, resolução, fps e bitrate reais** do app, e o
`rtsp_read_probe.py` só mede codec e fps. Com o push do drone no ar, um comando
responde tudo:

    python3 scripts/probe/ingest_report.py \
        --url rtsp://192.168.59.102:8554/drone --seconds 20 --ficha

Contrato de saída:
  - stdout: **uma** linha JSON (mesmo estilo de `rtsp_read_probe.py`);
  - stderr: resumo legível (suprimido com `--json`); com `--ficha`, também as
    linhas prontas da ficha de campo;
  - exit code **0 = OK**, **1 = FAIL** (sem vídeo / erro de pipeline),
    **2 = uso incorreto** (argparse).

Diferença deliberada em relação à sonda: **não há gate**. Este instrumento
mede o link de rádio do drone, que por D3 é reportado, não reprovado. O gate de
pré-check continua sendo o `rtsp_read_probe.py` (D7), cujo contrato não foi
tocado.

Como mede:
  - codec: `encoding-name` das caps de RTP no `pad-added` (H.264/H.265);
  - resolução: caps do parser (`h264parse`/`h265parse`), que traz `width`/
    `height` do SPS — sem decodificar (nada de `nvv4l2decoder` aqui);
  - fps e gaps: buffers de access unit no src do parser;
  - bitrate: soma do tamanho dos buffers RTP no src do `rtspsrc`, ou seja o
    bitrate **no fio** (cabeçalho RTP incluso, ~1 % acima do configurado no
    encoder — medido: 6.053 kbps para 6 Mbps).

Roda no host e dentro de container: só precisa de PyGObject + GStreamer 1.0,
como a sonda.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402  (exige require_version antes)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ingest_report_lib as lib  # noqa: E402

# Folga acima de --seconds antes do timeout duro (pipeline travado).
HARD_TIMEOUT_SLACK_S = 5

# Limite do teardown (`set_state(NULL)`): acima disso seguimos com o resultado
# já calculado, como a sonda.
TEARDOWN_TIMEOUT_S = 5
TEARDOWN_TIMEOUT_NS = TEARDOWN_TIMEOUT_S * 1_000_000_000


@dataclass
class Observation:
    """O que a execução observou. Preenchido pelos callbacks do GStreamer."""

    timestamps: List[float] = field(default_factory=list)
    rtp_bytes: int = 0
    codec: Optional[str] = None
    encoding_name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    audio_tracks: int = 0
    error: Optional[str] = None
    eos: bool = False
    no_video_pad: bool = False


def _caps_string(structure: Any, name: str) -> Optional[str]:
    """Lê um campo textual das caps como `str` (`media`, `encoding-name`)."""
    if structure is None or not structure.has_field(name):
        return None
    value = structure.get_value(name)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _caps_int(structure: Any, name: str) -> Optional[int]:
    """Lê um campo inteiro das caps (`width`, `height`)."""
    if structure is None or not structure.has_field(name):
        return None
    try:
        return int(structure.get_value(name))
    except (TypeError, ValueError):
        return None


class IngestReport:
    """Monta `rtspsrc ! depay ! parse ! fakesink` e mede sem decodificar."""

    def __init__(self, url: str, transport: str = "tcp", verbose: bool = False):
        self.url = url
        self.transport = transport
        self.verbose = verbose
        self.obs = Observation()
        self.pipeline: Optional[Gst.Pipeline] = None
        self.sink: Optional[Gst.Element] = None
        self.src: Optional[Gst.Element] = None
        self._start_t: float = 0.0
        self._loop: Optional[GLib.MainLoop] = None
        self._linked = False
        self._soft_fired = False
        self._hard_fired = False

    # -- log ---------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[ingest] {lib.scrub_text(message, self.url)}", file=sys.stderr)

    def _warn(self, message: str) -> None:
        print(
            f"[ingest] aviso: {lib.scrub_text(message, self.url)}", file=sys.stderr
        )

    # -- construção --------------------------------------------------------
    def _build(self) -> None:
        pipeline = Gst.Pipeline.new("ingest-report")

        src = Gst.ElementFactory.make("rtspsrc", "src")
        if src is None:
            raise RuntimeError("elemento rtspsrc indisponível (plugins RTSP ausentes)")
        src.set_property("location", self.url)
        src.set_property("protocols", lib.transport_mask(self.transport))
        src.set_property("latency", 200)
        pipeline.add(src)
        src.connect("pad-added", self._on_pad_added)

        sink = Gst.ElementFactory.make("fakesink", "sink")
        if sink is None:
            raise RuntimeError("elemento fakesink indisponível")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        sink.set_property("enable-last-sample", False)
        pipeline.add(sink)

        self.pipeline = pipeline
        self.src = src
        self.sink = sink

    def _on_pad_added(self, _src: Gst.Element, pad: Gst.Pad) -> None:
        caps = pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            return
        structure = caps.get_structure(0)
        if structure.get_name() != "application/x-rtp":
            return

        media = _caps_string(structure, "media")
        encoding = _caps_string(structure, "encoding-name")
        action, codec = lib.classify_pad(media, encoding)

        if action == lib.PAD_AUDIO:
            self.obs.audio_tracks += 1
            self._log(f"pad de áudio ignorado (encoding={encoding!r})")
            return
        if action == lib.PAD_IGNORE:
            self._log(f"pad ignorado: media={media!r} encoding={encoding!r}")
            return
        if action == lib.PAD_UNSUPPORTED:
            self.obs.error = f"codec não suportado: {encoding!r}"
            self._quit()
            return
        if self._linked:
            return

        assert codec is not None
        factories = lib.codec_factories(encoding)
        assert factories is not None
        depay_name, parse_name = factories
        self.obs.codec = codec
        self.obs.encoding_name = encoding
        self._log(f"codec={codec} ({depay_name} ! {parse_name})")

        assert self.pipeline is not None and self.sink is not None
        depay = Gst.ElementFactory.make(depay_name, None)
        parse = Gst.ElementFactory.make(parse_name, None)
        if depay is None or parse is None:
            self.obs.error = f"elementos ausentes: {depay_name}/{parse_name}"
            self._quit()
            return
        self.pipeline.add(depay)
        self.pipeline.add(parse)

        if not (depay.link(parse) and parse.link(self.sink)):
            self.obs.error = f"falha ao ligar {depay_name} ! {parse_name}"
            self._quit()
            return
        depay.sync_state_with_parent()
        parse.sync_state_with_parent()

        if pad.link(depay.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            self.obs.error = f"falha ao ligar o pad do rtspsrc em {depay_name}"
            self._quit()
            return

        # Bitrate: pacotes RTP crus, antes do depayloader.
        pad.add_probe(Gst.PadProbeType.BUFFER, self._on_rtp_buffer)
        # fps, gaps e resolução: access units na saída do parser.
        parse_src = parse.get_static_pad("src")
        if parse_src is not None:
            parse_src.add_probe(Gst.PadProbeType.BUFFER, self._on_video_buffer)
        self._linked = True

    def _on_rtp_buffer(
        self, _pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        if buf is not None:
            self.obs.rtp_bytes += buf.get_size()
        return Gst.PadProbeReturn.OK

    def _on_video_buffer(
        self, pad: Gst.Pad, _info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        self.obs.timestamps.append(time.monotonic() - self._start_t)
        if self.obs.width is None or self.obs.height is None:
            self._read_video_caps(pad)
        return Gst.PadProbeReturn.OK

    def _read_video_caps(self, pad: Gst.Pad) -> None:
        """Lê `width`/`height` do SPS expostos pelo parser (sem decodificar).

        Nunca levanta: isto roda em callback de pad probe, e exceção aqui
        interromperia o fluxo de buffers (o `framerate` das caps já derrubou
        esta função com `TypeError: unknown type GstFraction` na primeira
        versão — daí `get_fraction`, que devolve `(num, den)`).
        """
        try:
            caps = pad.get_current_caps()
            if caps is None or caps.get_size() == 0:
                caps = pad.query_caps(None)
            if caps is None or caps.get_size() == 0:
                return
            structure = caps.get_structure(0)
            width = _caps_int(structure, "width")
            height = _caps_int(structure, "height")
            if width is not None and height is not None:
                self.obs.width = width
                self.obs.height = height
                self._log(f"resolução das caps: {width}x{height}")
        except Exception as exc:  # noqa: BLE001 - probe não pode derrubar o fluxo
            self._warn(f"não foi possível ler as caps do parser: {exc}")

    # -- bus ---------------------------------------------------------------
    def _on_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        kind = message.type
        if kind == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            # `error.message` sozinho costuma ser inútil ("Unhandled error") em
            # erro do rtspsrc: o motivo real (ex.: `Could not open resource for
            # reading (5)`) vem na primeira linha do debug. Sempre incluído e
            # sempre sanitizado, porque o debug ecoa a `location`.
            detail = error.message
            extra = lib.summarize_gst_debug(debug)
            if self.verbose:
                detail = f"{detail} | {str(debug).strip()}"
            elif extra:
                detail = f"{detail}: {extra}"
            self.obs.error = lib.scrub_text(detail, self.url)
            self._quit()
        elif kind == Gst.MessageType.EOS:
            self.obs.eos = True
            self._quit()

    # -- ciclo de vida -----------------------------------------------------
    def _quit(self) -> bool:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        return GLib.SOURCE_REMOVE

    def _on_soft_deadline(self) -> bool:
        self._soft_fired = True
        return self._quit()

    def _on_hard_deadline(self) -> bool:
        self._hard_fired = True
        self.obs.error = "timeout duro: pipeline não respondeu"
        return self._quit()

    def run(self, seconds: float) -> Observation:
        """Lê por até `seconds` e devolve o que foi observado."""
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

        self._log(
            f"abrindo {lib.sanitize_url(self.url)} "
            f"transport={self.transport} por {seconds}s"
        )
        self._start_t = time.monotonic()
        self.pipeline.set_state(Gst.State.PLAYING)

        try:
            loop.run()
        except KeyboardInterrupt:  # pragma: no cover - fallback fora do GLib
            self.obs.error = self.obs.error or "interrompido pelo usuário"
        finally:
            self._teardown()
            expirados = (
                (soft_id, self._soft_fired),
                (hard_id, self._hard_fired),
            )
            for source_id, fired in expirados:
                if source_id and not fired:
                    GLib.source_remove(source_id)
            for source_id in signal_ids:
                if source_id:
                    GLib.source_remove(source_id)
            bus.remove_signal_watch()

        if not self._linked and self.obs.error is None:
            self.obs.no_video_pad = True
        return self.obs

    def _teardown(self) -> None:
        """Solta o pipeline com espera limitada; não mascara o resultado."""
        assert self.pipeline is not None
        ret = self.pipeline.set_state(Gst.State.NULL)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._warn("teardown: pipeline recusou a transição para NULL")
            return
        _ret, state, _pending = self.pipeline.get_state(TEARDOWN_TIMEOUT_NS)
        if state != Gst.State.NULL:
            self._warn(
                "teardown: pipeline não chegou a NULL em "
                f"{TEARDOWN_TIMEOUT_S}s (estado={state.value_nick}); "
                "seguindo com o resultado já calculado"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ingest_report.py",
        description=(
            "Mede o que a fonte entrega de verdade: codec, resolução, fps e "
            "bitrate. Instrumento de observação (sem gate) para a ficha de "
            "campo do drone. Exit 0 = OK, 1 = FAIL, 2 = uso incorreto."
        ),
        epilog=(
            "Exemplo (drone publicando):\n"
            "  python3 scripts/probe/ingest_report.py \\\n"
            "      --url rtsp://192.168.59.102:8554/drone --seconds 20 --ficha\n\n"
            "O bitrate é medido no fio (RTP, cabeçalho incluso); a resolução "
            "vem do SPS, lida pelo parser, sem decodificar. Nada aqui abre "
            "gate: quem reprova fonte é o rtsp_read_probe.py (D7)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="URL rtsp:// do path a ler")
    parser.add_argument(
        "--seconds",
        type=float,
        default=20.0,
        help="janela de leitura (default: 20; use >= 20 para bitrate estável)",
    )
    parser.add_argument(
        "--transport",
        default="tcp",
        choices=sorted(lib.TRANSPORT_MASKS),
        help="transporte RTSP (default: tcp)",
    )
    parser.add_argument(
        "--ficha",
        action="store_true",
        help="imprime em stderr as linhas prontas da ficha de campo (§6)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="não imprime o resumo legível em stderr (só a linha JSON)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log de depuração em stderr"
    )
    args = parser.parse_args(argv)

    try:
        args.url = lib.validate_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.seconds <= 0:
        parser.error("--seconds precisa ser maior que zero")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    Gst.init([])
    started = time.monotonic()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    module = IngestReport(args.url, transport=args.transport, verbose=args.verbose)
    try:
        obs = module.run(args.seconds)
        report = lib.build_report(
            url=args.url,
            codec=obs.codec,
            encoding_name=obs.encoding_name,
            timestamps_s=obs.timestamps,
            rtp_bytes=obs.rtp_bytes,
            audio_tracks=obs.audio_tracks,
            seconds=args.seconds,
            elapsed_s=time.monotonic() - started,
            transport=args.transport,
            width=obs.width,
            height=obs.height,
            error=obs.error,
            no_video_pad=obs.no_video_pad,
            eos=obs.eos,
            ts=ts,
        )
    except Exception as exc:  # erro de construção/execução do pipeline
        # Mesmo formato do caminho feliz: `format_human` e `render_ficha`
        # leem as mesmas chaves, e o `.jsonl` da ficha fica homogêneo.
        report = lib.build_report(
            url=args.url,
            codec=None,
            encoding_name=None,
            timestamps_s=[],
            rtp_bytes=0,
            audio_tracks=0,
            seconds=args.seconds,
            elapsed_s=time.monotonic() - started,
            transport=args.transport,
            error=f"probe_error: {lib.scrub_text(str(exc), args.url)}",
            ts=ts,
        )

    if not args.json:
        print(lib.format_human(report), file=sys.stderr)
        if args.ficha:
            print(lib.render_ficha(report), file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["result"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
