#!/usr/bin/env python3
"""Sonda de saúde de um stream RTSP do MediaMTX (pré-check de troca de fonte).

Lê um path do MediaMTX por RTSP e mede o que o plano de troca de fonte precisa
saber antes de mexer em qualquer coisa: chega frame? com que fps? com que
buracos? e há quanto tempo o primeiro frame demorou?

Contrato de saída (é o que `scripts/switch_source.sh` vai consumir, D7):
  - stdout: **uma** linha JSON com as métricas;
  - stderr: resumo legível (suprimido com `--json`) e avisos de teardown;
  - exit code **0 = PASS**, **1 = FAIL**, **2 = uso incorreto** (erro de
    argumento, emitido pelo `argparse`). O `switch_source.sh` trata qualquer
    código `!= 0` como falha do pré-check.

PASS exige, simultaneamente: `frames >= --min-frames`, `fps >= --min-fps`, o
primeiro frame chegou dentro de `--seconds` e a contagem de gaps > 500 ms não
passa de `--max-gaps` (default 0; `-1` desliga o gate). Qualquer erro de
GStreamer é capturado no bus e vira `result: FAIL` com `reason` — nunca
traceback.

Credencial: a URI da câmera tem usuário e senha, e esta sonda é chamada pelo
`switch_source.sh` com essa URI. Toda saída (JSON, resumo, `--verbose`, texto de
erro do GStreamer) passa por `sanitize_url()`, que troca o userinfo por `***` e
preserva host, porta e path — o necessário para diagnosticar.

Codec: o `rtspsrc` só entrega os pads quando o DESCRIBE volta, então o codec é
descoberto no `pad-added` pelas caps do RTP (`encoding-name`) e o depayloader
correspondente é criado na hora — H.264 (`rtph264depay`/`h264parse`) ou H.265
(`rtph265depay`/`h265parse`). Isso aceita as três fontes do projeto sem
parametrizar codec na linha de comando.

Pads que **não** são vídeo são ignorados (a câmera do projeto publica áudio
PCMA junto do vídeo, como em `deepstream/pipeline_real.py`): só o primeiro pad
de vídeo é vinculado; codec fora de H.264/H.265 reprova apenas se estiver em um
pad de vídeo; se nenhum pad de vídeo aparecer, o FAIL sai com
`reason: no_video_pad`.

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

# Limite de espera do teardown (`set_state(NULL)`); acima disso a sonda segue
# com o resultado já calculado em vez de travar além de --seconds + 5.
TEARDOWN_TIMEOUT_S = 5

# O mesmo limite em nanossegundos, unidade de `get_state()`. Literal porque o
# `gi` deste host não expõe `Gst.SECOND`/`Gst.SECONDS`.
TEARDOWN_TIMEOUT_NS = TEARDOWN_TIMEOUT_S * 1_000_000_000

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

# `media` das caps de RTP que interessam à sonda.
VIDEO_MEDIA = "video"

# Classificação de um pad do rtspsrc (`classify_pad`).
PAD_IGNORE = "ignore"          # não é vídeo: ignora sem erro (áudio, app...)
PAD_LINK = "link"              # vídeo H.264/H.265: cria a cadeia depay ! parse
PAD_UNSUPPORTED = "unsupported"  # vídeo com codec fora de H.264/H.265: falha

# Máscara do userinfo nas URLs exibidas.
MASKED_USERINFO = "***"


# ---------------------------------------------------------------------------
# Funções puras (testadas em scripts/tests/test_probe_logic.py)
# ---------------------------------------------------------------------------
def _split_authority(url: str) -> Optional[Tuple[str, str, str]]:
    """Separa a URL em `(esquema, authority, resto)`.

    `resto` começa no primeiro `/`, `?` ou `#` (vazio quando a URL acaba na
    authority). `None` quando não há `esquema://`.
    """
    candidate = (url or "").strip()
    if "://" not in candidate:
        return None
    scheme, rest = candidate.split("://", 1)
    authority_end = len(rest)
    for separator in ("/", "?", "#"):
        index = rest.find(separator)
        if index != -1:
            authority_end = min(authority_end, index)
    return scheme, rest[:authority_end], rest[authority_end:]


def split_userinfo(url: str) -> Optional[str]:
    """Devolve o trecho `usuario:senha` da URL, ou `None` quando não há.

    Considera só a authority (antes do primeiro `/`, `?` ou `#`): o `@` de um
    path não é userinfo. Usa o **último** `@` da authority porque a senha pode
    conter `@` sem escape.
    """
    parts = _split_authority(url)
    if parts is None:
        return None
    _scheme, authority, _tail = parts
    if "@" not in authority:
        return None
    userinfo = authority.rsplit("@", 1)[0]
    return userinfo or None


def sanitize_url(url: str) -> str:
    """Mascara a credencial da URL, preservando host, porta e path.

    `rtsp://USUARIO:SENHA@host:8554/drone` -> `rtsp://***@host:8554/drone`.
    É a forma usada em **toda** saída da sonda (JSON, resumo, `--verbose`):
    host/porta/path continuam visíveis para diagnóstico; usuário e senha não.
    URL sem userinfo volta inalterada.
    """
    candidate = (url or "").strip()
    parts = _split_authority(candidate)
    userinfo = split_userinfo(candidate)
    if parts is None or userinfo is None:
        return candidate
    scheme, authority, tail = parts
    masked = MASKED_USERINFO + "@" + authority.rsplit("@", 1)[1]
    return f"{scheme}://{masked}{tail}"


def scrub_text(text: str, url: str) -> str:
    """Remove userinfo e senha de um texto livre (mensagens do GStreamer).

    O `rtspsrc` ecoa a `location` em algumas falhas; sem isso a senha voltaria
    pela porta dos fundos do `reason`. Sem credencial na URL, o texto não muda.
    """
    if not text:
        return text
    userinfo = split_userinfo(url)
    if not userinfo:
        return text
    scrubbed = text.replace(f"{userinfo}@", f"{MASKED_USERINFO}@")
    password = userinfo.split(":", 1)[1] if ":" in userinfo else ""
    # A senha sozinha também é segredo: aparece em mensagens de autenticação.
    if len(password) >= 4:
        scrubbed = scrubbed.replace(password, MASKED_USERINFO)
    if len(userinfo) >= 4:
        scrubbed = scrubbed.replace(userinfo, MASKED_USERINFO)
    return scrubbed


def classify_pad(
    media: Optional[str], encoding_name: Optional[str]
) -> Tuple[str, Optional[str]]:
    """Decide o que fazer com um pad do `rtspsrc`, a partir das caps de RTP.

    Retorna `(ação, codec)`:
      - `PAD_IGNORE` para pad que não é de vídeo (áudio PCMA da câmera, por
        exemplo) — ignorar sem erro é o que evita reprovar uma fonte saudável
        só porque o áudio veio primeiro;
      - `PAD_UNSUPPORTED` para pad **de vídeo** com codec fora de H.264/H.265
        (o pipeline também não consumiria);
      - `PAD_LINK` + codec normalizado para vídeo H.264/H.265.
    """
    if (media or "").strip().lower() != VIDEO_MEDIA:
        return PAD_IGNORE, None
    codec = ENCODING_TO_CODEC.get((encoding_name or "").strip().upper())
    if codec is None or codec_factories(encoding_name) is None:
        return PAD_UNSUPPORTED, None
    return PAD_LINK, codec


def validate_url(url: str) -> str:
    """Valida a URL de leitura e a devolve normalizada.

    Só aceita RTSP: a sonda mede paths do MediaMTX. Levanta `ValueError` com
    mensagem acionável para entrada vazia, esquema errado ou host ausente. As
    mensagens usam a URL sanitizada — o erro vai para stderr e não pode vazar
    a credencial.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("--url vazia: informe rtsp://host:porta/path")
    if "://" not in candidate:
        raise ValueError(
            f"--url inválida ({sanitize_url(candidate)!r}): falta o esquema rtsp://"
        )
    scheme, rest = candidate.split("://", 1)
    if scheme.lower() not in ("rtsp", "rtsps"):
        raise ValueError(
            f"--url inválida ({sanitize_url(candidate)!r}): esquema {scheme!r} "
            "não é rtsp/rtsps"
        )
    if not rest or rest.startswith("/"):
        raise ValueError(
            f"--url inválida ({sanitize_url(candidate)!r}): host ausente"
        )
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
    gaps_over_500ms: int = 0,
    max_gaps: int = 0,
    no_video_pad: bool = False,
) -> Tuple[str, str]:
    """Decide PASS/FAIL e o motivo, na ordem em que faz diferença para operação.

    Retorna `(result, reason)`. O motivo do primeiro critério violado é o que
    aparece — é ele que o `switch_source.sh` vai imprimir.

    `max_gaps` é o gate de estabilidade (D3): default 0 exige zero gaps acima de
    `GAP_THRESHOLD_MS`; `-1` desliga o gate. `no_video_pad` cobre o caso em que
    o `rtspsrc` entregou pads, mas nenhum de vídeo — distinto de codec não
    suportado (que só vale para pad de vídeo).
    """
    if error:
        return "FAIL", f"pipeline_error: {error}"
    if no_video_pad:
        return "FAIL", "no_video_pad"
    if frames <= 0:
        return "FAIL", "no_frames"
    if first_frame_s is None or first_frame_s > seconds:
        return "FAIL", "first_frame_too_late"
    if frames < min_frames:
        return "FAIL", f"frames_below_min ({frames} < {min_frames})"
    if fps < min_fps:
        return "FAIL", f"fps_below_min ({fps} < {min_fps})"
    if max_gaps >= 0 and gaps_over_500ms > max_gaps:
        return "FAIL", f"too_many_gaps ({gaps_over_500ms} > {max_gaps})"
    return "PASS", "ok"


# ---------------------------------------------------------------------------
# Leitura via GStreamer
# ---------------------------------------------------------------------------
def _caps_string(structure: Any, field: str) -> Optional[str]:
    """Lê um campo textual das caps (`media`, `encoding-name`) como `str`."""
    if structure is None or not structure.has_field(field):
        return None
    value = structure.get_value(field)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


@dataclass
class ProbeResult:
    """Acumulador do que a sonda observou em uma execução."""

    timestamps: List[float] = field(default_factory=list)
    codec: Optional[str] = None
    error: Optional[str] = None
    eos: bool = False
    hard_timeout: bool = False
    # True quando o rtspsrc só entregou pads que não são de vídeo (ou nenhum).
    no_video_pad: bool = False

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
            print(f"[probe] {scrub_text(message, self.url)}", file=sys.stderr)

    def _warn(self, message: str) -> None:
        """Aviso operacional: sai sempre em stderr, mesmo com `--json`.

        Não vai para stdout (o contrato é uma linha JSON) e não altera o
        resultado já calculado.
        """
        print(f"[probe] aviso: {scrub_text(message, self.url)}", file=sys.stderr)

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
        """Religa o pad **de vídeo** do rtspsrc ao depayloader do codec do SDP.

        Pads que não são de vídeo (áudio PCMA da câmera, por exemplo) são
        ignorados: o pipeline (`deepstream/pipeline_real.py`) também filtra por
        `media == "video"`, e reprovar a fonte por causa do áudio derrubaria o
        pré-check de uma câmera saudável.
        """
        if self._linked:
            return
        caps = pad.query_caps(None)
        if caps is None or caps.get_size() == 0:
            return
        structure = caps.get_structure(0)
        if structure.get_name() != "application/x-rtp":
            return

        media = _caps_string(structure, "media")
        encoding = _caps_string(structure, "encoding-name")
        action, codec = classify_pad(media, encoding)
        if action == PAD_IGNORE:
            self._log(
                f"pad ignorado: media={media!r} encoding={encoding!r} "
                "(não é vídeo; só o primeiro pad de vídeo é vinculado)"
            )
            return
        if action == PAD_UNSUPPORTED:
            self.result.error = f"codec não suportado: {encoding!r}"
            self._quit()
            return

        assert codec is not None  # PAD_LINK sempre traz o codec normalizado
        factories = codec_factories(encoding)
        assert factories is not None
        depay_name, parse_name = factories
        self.result.codec = codec
        self._log(f"codec={codec} ({depay_name} ! {parse_name})")

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
            # Alguns erros do rtspsrc ecoam a `location`: não pode vazar senha.
            self.result.error = scrub_text(detail, self.url)
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

        self._log(f"abrindo {sanitize_url(self.url)} transport={self.transport} por {seconds}s")
        self._start_t = time.monotonic()
        self.pipeline.set_state(Gst.State.PLAYING)

        try:
            loop.run()
        except KeyboardInterrupt:  # pragma: no cover - fallback fora do GLib
            self.result.error = self.result.error or "interrompido pelo usuário"
        finally:
            self._teardown()
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

        # Nenhum pad de vídeo apareceu: motivo próprio, distinto de "codec não
        # suportado" (que só vale para pad de vídeo) e de "sem frames".
        if not self._linked and self.result.error is None:
            self.result.no_video_pad = True

        return self.result

    def _teardown(self) -> None:
        """Solta o pipeline com espera limitada, sem mascarar o resultado.

        `set_state(NULL)` + `get_state(CLOCK_TIME_NONE)` esperaria para sempre um
        pipeline travado. Aqui a espera é de `TEARDOWN_TIMEOUT_S`; se estourar,
        sai um aviso em stderr e a sonda continua com as métricas já coletadas.
        """
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
    max_gaps: int = 0,
    container: str = "rtsp",
) -> Dict[str, Any]:
    """Consolida o observado no JSON de saída (uma linha em stdout).

    A URL sai sanitizada: o JSON é consumido e logado pelo `switch_source.sh`,
    e a URI da câmera tem usuário e senha.
    """
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
        gaps_over_500ms=gaps["gaps_over_500ms"],
        max_gaps=max_gaps,
        no_video_pad=result.no_video_pad,
    )
    return {
        "url": sanitize_url(url),
        "codec": result.codec or "unknown",
        "container": container,
        "first_frame_s": first_frame_s,
        "frames": frames,
        "fps": fps,
        "gaps_over_500ms": gaps["gaps_over_500ms"],
        "max_gaps": max_gaps,
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
        f"first_frame={first_txt} gaps>500ms={report['gaps_over_500ms']}"
        f"/{report['max_gaps']} "
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
            "Exit 0 = PASS, exit 1 = FAIL, exit 2 = uso incorreto "
            "(contrato de pré-check de troca de fonte)."
        ),
        epilog=(
            "Exit codes: 0 = PASS, 1 = FAIL (stream reprovado ou erro de "
            "pipeline), 2 = uso incorreto (argumento inválido, emitido pelo "
            "argparse). O switch_source.sh trata qualquer código != 0 como falha "
            "do pré-check.\n\n"
            "Estabilidade: por padrão PASS exige **zero** gaps acima de 500 ms "
            "(--max-gaps 0), o critério do drone (D3). --max-gaps -1 desliga o "
            "gate; --max-gaps N tolera até N gaps.\n\n"
            "Exemplo: rtsp_read_probe.py --url rtsp://192.168.59.102:8554/drone "
            "--seconds 5 --json    # pré-check do switch_source.sh\n\n"
            "Credencial: a URL é mascarada em toda saída (usuário e senha viram "
            "'***'); host, porta e path continuam visíveis.\n\n"
            "Atenção: as portas do MediaMTX são publicadas no IP da LAN "
            "(E0-T2); rtsp://127.0.0.1:8554/... só funciona se o MediaMTX "
            "estiver publicado em loopback.\n\n"
            "O codec (H.264/H.265) é descoberto nas caps do pad de vídeo do "
            "rtspsrc e o depayloader é criado em tempo de execução; não há flag "
            "de codec. Pads de áudio são ignorados."
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
        "--max-gaps",
        type=int,
        default=0,
        help=(
            "gaps acima de 500 ms tolerados para PASS (default: 0 = nenhum); "
            "-1 desliga o gate"
        ),
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
    if args.max_gaps < -1:
        parser.error("--max-gaps aceita -1 (desliga o gate) ou inteiro >= 0")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Roda a sonda e devolve o exit code do contrato (0 = PASS, 1 = FAIL)."""
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
            max_gaps=args.max_gaps,
        )
    except Exception as exc:  # erro de construção/execução do pipeline
        report = {
            "url": sanitize_url(args.url),
            "codec": "unknown",
            "container": "rtsp",
            "first_frame_s": None,
            "frames": 0,
            "fps": 0.0,
            "gaps_over_500ms": 0,
            "max_gaps": args.max_gaps,
            "max_gap_ms": 0.0,
            "avg_gap_ms": 0.0,
            "duration_s": 0.0,
            "result": "FAIL",
            "reason": f"probe_error: {scrub_text(str(exc), args.url)}",
        }

    if not args.json:
        print(format_human(report), file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
