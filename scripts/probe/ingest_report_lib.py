"""Lógica pura do relatório de ingestão (`scripts/probe/ingest_report.py`).

Por que este módulo existe: a ficha de campo do drone
(`docs/runbooks/drone-campo.md`, §6) pede **codec, resolução, fps e bitrate
reais** do app. O `rtsp_read_probe.py` só mede codec e fps — resolução e
bitrate não saem em lugar nenhum, nem na telemetria do pipeline
(`nvstreammux` é fixo em 1280x720 e o `PREVIEW_BITRATE` é do nosso encoder, não
do app). Sem isso, "720p ou 1080p e a que bitrate?" viraria leitura de tela do
app.

`rtsp_read_probe.py` **não** foi estendido de propósito: o JSON dele é o
contrato do pré-check obrigatório do `switch_source.sh` (D7), e resolução/
bitrate não têm gate. Este instrumento é de **observação** — não reprova
fonte.

Como no resto do projeto, a lógica vive aqui (sem `gi`/`pyds`, testada em
`scripts/tests/test_ingest_report.py`) e o script importa este módulo.

Nota de duplicação assumida: `sanitize_url`, `validate_url`, `classify_pad` e
`compute_gaps` existem também em `rtsp_read_probe.py`. Importar de lá obrigaria
a carregar `gi` neste módulo (o import de `gi` é no topo do outro arquivo),
quebrando a testabilidade pura. A duplicação é pequena e coberta por teste.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Sequence, Tuple

# Janela de gap considerada "buraco" na entrega (mesmo limiar da sonda).
GAP_THRESHOLD_MS = 500.0

# `media` das caps de RTP.
VIDEO_MEDIA = "video"
AUDIO_MEDIA = "audio"

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

# Classificação de um pad do rtspsrc (`classify_pad`).
PAD_LINK = "link"                # vídeo H.264/H.265: cria depay ! parse
PAD_AUDIO = "audio"              # áudio: conta e ignora (a câmera tem PCMA)
PAD_IGNORE = "ignore"            # outro (application, metadata): ignora
PAD_UNSUPPORTED = "unsupported"  # vídeo fora de H.264/H.265: falha alto

# Máscara do userinfo nas URLs exibidas.
MASKED_USERINFO = "***"

# Rótulos legíveis por codec (ficha de campo).
CODEC_LABELS = {"h264": "H.264", "h265": "H.265"}

# Transportes aceitos pelo rtspsrc (`protocols` é máscara de flags).
TRANSPORT_MASKS = {"udp": 1, "tcp": 4}

# Localização em C que o GStreamer prefixa no debug (`gstrtspsrc.c(6795): ...`).
# Não ajuda o operador em campo: fica fora do motivo reportado.
C_LOCATION_RE = re.compile(r"\.c\(\d+\)")


# ---------------------------------------------------------------------------
# URL e credencial
# ---------------------------------------------------------------------------
def _split_authority(url: str) -> Optional[Tuple[str, str, str]]:
    """Separa a URL em `(esquema, authority, resto)`; `None` sem `://`."""
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
    """Devolve `usuario:senha` da authority (último `@`), ou `None`."""
    parts = _split_authority(url)
    if parts is None:
        return None
    _scheme, authority, _tail = parts
    if "@" not in authority:
        return None
    return authority.rsplit("@", 1)[0] or None


def sanitize_url(url: str) -> str:
    """Mascara a credencial preservando host, porta e path."""
    candidate = (url or "").strip()
    parts = _split_authority(candidate)
    userinfo = split_userinfo(candidate)
    if parts is None or userinfo is None:
        return candidate
    scheme, authority, tail = parts
    masked = MASKED_USERINFO + "@" + authority.rsplit("@", 1)[1]
    return f"{scheme}://{masked}{tail}"


def scrub_text(text: str, url: str) -> str:
    """Remove userinfo e senha de texto livre (mensagens do GStreamer)."""
    if not text:
        return text
    userinfo = split_userinfo(url)
    if not userinfo:
        return text
    scrubbed = text.replace(f"{userinfo}@", f"{MASKED_USERINFO}@")
    password = userinfo.split(":", 1)[1] if ":" in userinfo else ""
    if len(password) >= 4:
        scrubbed = scrubbed.replace(password, MASKED_USERINFO)
    if len(userinfo) >= 4:
        scrubbed = scrubbed.replace(userinfo, MASKED_USERINFO)
    return scrubbed


def summarize_gst_debug(debug: Optional[str], limit: int = 200) -> str:
    """Extrai o motivo útil do debug de um erro do GStreamer.

    `error.message` do `rtspsrc` costuma ser genérico ("Unhandled error") e o
    motivo real vem nas linhas seguintes do debug ("Bad Request (400)",
    "Could not open resource for reading (5)"). As linhas que são só a
    localização em C (`gstrtspsrc.c(6795): ...`) não ajudam o operador em campo
    e são descartadas. Texto vazio quando não há nada além disso.
    """
    if not debug:
        return ""
    useful = [
        line.strip()
        for line in str(debug).splitlines()
        if line.strip() and not C_LOCATION_RE.search(line)
    ]
    return " | ".join(useful)[:limit]


def validate_url(url: str) -> str:
    """Valida e normaliza a URL; `ValueError` com mensagem acionável."""
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
        raise ValueError(f"transporte inválido ({name!r}): use 'tcp' ou 'udp'")
    return TRANSPORT_MASKS[key]


# ---------------------------------------------------------------------------
# Pads e codec
# ---------------------------------------------------------------------------
def classify_pad(
    media: Optional[str], encoding_name: Optional[str]
) -> Tuple[str, Optional[str]]:
    """Decide o que fazer com um pad do `rtspsrc`.

    Distinto da sonda: áudio tem ação própria (`PAD_AUDIO`), porque a ficha
    registra quantas faixas o app publica — "1 track (H264)" vs "2 tracks" é
    informação de campo, não erro.
    """
    kind = (media or "").strip().lower()
    if kind == AUDIO_MEDIA:
        return PAD_AUDIO, None
    if kind != VIDEO_MEDIA:
        return PAD_IGNORE, None
    codec = ENCODING_TO_CODEC.get((encoding_name or "").strip().upper())
    if codec is None or codec_factories(encoding_name) is None:
        return PAD_UNSUPPORTED, None
    return PAD_LINK, codec


def codec_factories(
    encoding_name: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Devolve (depayloader, parser) do `encoding-name`, ou `None`."""
    if not encoding_name:
        return None
    codec = ENCODING_TO_CODEC.get(encoding_name.strip().upper(), "")
    return CODEC_FACTORIES.get(codec)


def codec_label(codec: Optional[str], encoding_name: Optional[str] = None) -> str:
    """Rótulo legível do codec para a ficha (`H.264 (H264)`)."""
    normalized = (codec or "").strip().lower()
    label = CODEC_LABELS.get(normalized, normalized.upper() or "desconhecido")
    if encoding_name and encoding_name.strip().upper() != normalized.upper():
        return f"{label} ({encoding_name})"
    return label


def format_resolution(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """`1920x1080` quando as duas dimensões são positivas; senão `None`."""
    try:
        w = int(width) if width is not None else 0
        h = int(height) if height is not None else 0
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return f"{w}x{h}"


def resolution_class(resolution: Optional[str]) -> str:
    """Classifica a resolução em `720p`/`1080p`/... quando reconhecível.

    É o que o app do drone oferece (720p e 1080p), então a ficha fala a mesma
    língua do operador. Resolução fora da lista volta como está.
    """
    if not resolution or "x" not in resolution:
        return resolution or "desconhecida"
    known = {"1280x720": "720p", "1920x1080": "1080p", "3840x2160": "4K"}
    return known.get(resolution, resolution)


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------
def measure_fps(timestamps_s: Sequence[float]) -> float:
    """fps medido na janela entre o primeiro e o último frame."""
    if len(timestamps_s) < 2:
        return 0.0
    window = timestamps_s[-1] - timestamps_s[0]
    if window <= 0:
        return 0.0
    return round((len(timestamps_s) - 1) / window, 3)


def compute_gaps(
    timestamps_s: Sequence[float], threshold_ms: float = GAP_THRESHOLD_MS
) -> Dict[str, float]:
    """Estatística dos intervalos entre frames consecutivos (em ms)."""
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


def compute_bitrate_kbps(total_bytes: int, window_s: float) -> float:
    """Bitrate médio da janela medida, em kbps.

    `total_bytes` é o que chegou **no fio** (pacotes RTP inteiros, cabeçalho
    incluso) — é o número comparável com o bitrate configurado no app, que
    também é bitrate de fluxo. Janela inválida (<= 0) devolve 0.0 em vez de
    `ZeroDivisionError`, porque fonte muda é caso previsto.
    """
    if total_bytes <= 0 or not window_s or window_s <= 0:
        return 0.0
    kbps = (total_bytes * 8.0) / window_s / 1000.0
    if not math.isfinite(kbps):
        return 0.0
    return round(kbps, 1)


def verdict(
    *,
    frames: int,
    first_frame_s: Optional[float],
    seconds: float,
    error: Optional[str] = None,
    no_video_pad: bool = False,
) -> Tuple[str, str]:
    """OK quando o stream entrega vídeo; FAIL com o motivo mais útil.

    Não há gate de fps nem de gap: este instrumento **observa** o link de
    rádio do drone, que é dado, não aprovação (D3). Quem gateia é o
    `switch_source.sh`.
    """
    if error:
        return "FAIL", f"pipeline_error: {error}"
    if no_video_pad:
        return "FAIL", "no_video_pad"
    if frames <= 0:
        return "FAIL", "no_frames"
    if first_frame_s is None or first_frame_s > seconds:
        return "FAIL", "first_frame_too_late"
    return "OK", "ok"


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
def build_report(
    *,
    url: str,
    codec: Optional[str],
    encoding_name: Optional[str],
    timestamps_s: Sequence[float],
    rtp_bytes: int,
    audio_tracks: int,
    seconds: float,
    elapsed_s: float,
    transport: str = "tcp",
    width: Optional[int] = None,
    height: Optional[int] = None,
    error: Optional[str] = None,
    no_video_pad: bool = False,
    eos: bool = False,
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Consolida o observado no JSON de uma linha (stdout do script)."""
    timestamps = list(timestamps_s)
    gaps = compute_gaps(timestamps)
    frames = len(timestamps)
    fps = measure_fps(timestamps)
    first_frame_s = round(timestamps[0], 3) if timestamps else None
    window_s = round(timestamps[-1] - timestamps[0], 3) if frames >= 2 else 0.0
    resolution = format_resolution(width, height)
    outcome, reason = verdict(
        frames=frames,
        first_frame_s=first_frame_s,
        seconds=seconds,
        error=error,
        no_video_pad=no_video_pad,
    )
    return {
        "ts": ts,
        "url": sanitize_url(url),
        "transport": transport,
        "seconds": round(seconds, 3),
        "window_s": window_s,
        "codec": codec or "unknown",
        "encoding_name": encoding_name,
        "width": width,
        "height": height,
        "resolution": resolution,
        "resolution_class": resolution_class(resolution),
        "frames": frames,
        "fps": fps,
        "bitrate_kbps": compute_bitrate_kbps(rtp_bytes, window_s),
        "rtp_bytes": rtp_bytes,
        "first_frame_s": first_frame_s,
        "gaps_over_500ms": gaps["gaps_over_500ms"],
        "max_gap_ms": gaps["max_gap_ms"],
        "avg_gap_ms": gaps["avg_gap_ms"],
        "audio_tracks": audio_tracks,
        "eos": eos,
        "elapsed_s": round(elapsed_s, 3),
        "result": outcome,
        "reason": reason,
        "error": error,
    }


def format_human(report: Dict[str, Any]) -> str:
    """Resumo de três linhas para quem roda o instrumento na mão (stderr)."""
    first = report.get("first_frame_s")
    first_txt = f"{first:.2f}s" if first is not None else "—"
    resolution = report.get("resolution") or "—"
    bitrate = report.get("bitrate_kbps") or 0.0
    lines = [
        f"{report['result']} {report['reason']} | {report['url']}",
        (
            f"codec={report['codec']} {resolution} fps={report['fps']} "
            f"bitrate={bitrate:.0f}kbps frames={report['frames']} "
            f"janela={report['window_s']:.1f}s"
        ),
        (
            f"first_frame={first_txt} gaps>500ms={report['gaps_over_500ms']} "
            f"max_gap={report['max_gap_ms']:.0f}ms "
            f"audio_tracks={report['audio_tracks']} "
            f"dur={report['elapsed_s']:.1f}s"
        ),
    ]
    return "\n".join(lines)


def render_ficha(report: Dict[str, Any]) -> str:
    """Linhas da ficha de campo (§6 do runbook) preenchidas com o medido.

    Só o que foi medido vira valor; o resto sai como `não medido` para não
    repetir o erro de número sem fonte (regra do plano).
    """
    resolution = format_resolution(
        report.get("width"), report.get("height")
    )
    resolution_txt = (
        f"{resolution} ({resolution_class(resolution)})"
        if resolution
        else "não medido"
    )
    bitrate = report.get("bitrate_kbps") or 0.0
    fps = report.get("fps") or 0.0
    measured = bool(report.get("frames"))
    codec_txt = (
        codec_label(report.get("codec"), report.get("encoding_name"))
        if measured
        else "não medido"
    )
    fps_txt = f"{fps} (medido)" if measured else "não medido"
    bitrate_txt = (
        f"{bitrate:.0f} kbps (medido no RTP)" if measured else "não medido"
    )
    return "\n".join(
        [
            f"| Codec real | {codec_txt} |",
            f"| Resolução do app | {resolution_txt} |",
            f"| FPS do app | {fps_txt} |",
            f"| Bitrate do app | {bitrate_txt} |",
            f"| Áudio junto | {report.get('audio_tracks', 0)} faixa(s) |",
            (
                f"| Sonda: `fps`, `first_frame_s` | {fps}, "
                f"{report.get('first_frame_s')} |"
            ),
        ]
    )
