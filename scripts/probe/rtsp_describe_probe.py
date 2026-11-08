#!/usr/bin/env python3
"""Sonda de nível 1 de um path RTSP: `OPTIONS` + `DESCRIBE` sem GStreamer.

Pergunta barata que o observador de fontes faz a cada ciclo: "este path ainda
existe e declara vídeo?". Fala RTSP 1.0 na mão sobre `socket` TCP da stdlib —
nada de `gi`, nada de pipeline —, então roda no container do watchdog (que só
tem `python3-gi` para a sonda de nível 2) e em qualquer máquina sem GPU.

A request line leva a URL **absoluta** (`OPTIONS rtsp://host:porta/path?query
RTSP/1.0`), como a RFC 2326 exige: o MediaMTX fecha a conexão com
`invalid URL (/path)` quando o alvo é relativo. A URL absoluta nunca carrega
userinfo — host e porta vêm de `parts.hostname`/`parts.port`.

Contrato de saída (`DescribeResult.status`):

  - `"200"`      DESCRIBE aceito e o SDP anuncia `m=video`;
  - `"404"`      o path não existe / não tem publisher (MediaMTX responde 404);
  - `"timeout"`  connect ou leitura estourou o `timeout` (default 2,0 s);
  - `"sem_video"` DESCRIBE aceito, mas o SDP não tem `m=video` — o servidor
                 responde, o stream não entrega vídeo (o caso que o nível 1
                 **precisa** reprovar: `alive=False`);
  - `"error"`    URI inválida ou resposta ilegível (nunca levanta).

Nível 1 não prova mídia: prova só que o caminho responde. Quem confirma quadro
de verdade é a sonda de nível 2 (`rtsp_read_probe.py`), chamada pelo watchdog
quando a política pede.

Credencial: a URI da câmera tem usuário e senha (DIV-001). Toda saída — o
`reason`, o JSON do CLI, a linha legível — passa por `mask_uri()`, que troca o
userinfo por `***` e preserva host, porta e path. A credencial só é usada para
montar `Authorization: Basic` na requisição.

CLI (diagnóstico manual): `rtsp_describe_probe.py --url rtsp://host:8554/path`
imprime uma linha JSON e sai com 0 quando o path está vivo, 1 quando não.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

# Default curto: a sonda roda a cada CHECK_INTERVAL_S (10 s) do watchdog e não
# pode atrasar as regras de segurança do container (RAM/CUDA/Regra 1).
DEFAULT_TIMEOUT_S = 2.0

RTSP_DEFAULT_PORT = 554
RTSP_SCHEMES = ("rtsp",)          # rtsps exigiria TLS: não é o caso do lab
HEADER_END = b"\r\n\r\n"
MAX_RESPONSE_BYTES = 256 * 1024
USER_AGENT = "vsl-describe-probe"

STATUS_OK = "200"
STATUS_NOT_FOUND = "404"
STATUS_TIMEOUT = "timeout"
STATUS_NO_VIDEO = "sem_video"
STATUS_ERROR = "error"

MASKED_USERINFO = "***"


# ---------------------------------------------------------------------------
# URI: mascaramento e decomposição (espelha os nomes do `rtsp_read_probe.py`)
# ---------------------------------------------------------------------------
def _split_authority(uri: str) -> Optional[Tuple[str, str, str]]:
    """`(esquema, authority, resto)` — `resto` começa em `/`, `?` ou `#`."""
    candidate = (uri or "").strip()
    if "://" not in candidate:
        return None
    scheme, rest = candidate.split("://", 1)
    authority_end = len(rest)
    for separator in ("/", "?", "#"):
        index = rest.find(separator)
        if index != -1:
            authority_end = min(authority_end, index)
    return scheme, rest[:authority_end], rest[authority_end:]


def split_userinfo(uri: str) -> Optional[str]:
    """Trecho `usuario:senha` da authority, ou `None` (último `@` da authority)."""
    parts = _split_authority(uri)
    if parts is None:
        return None
    _scheme, authority, _tail = parts
    if "@" not in authority:
        return None
    return authority.rsplit("@", 1)[0] or None


def mask_uri(uri: str) -> str:
    """Mascara a credencial preservando host, porta e path (DIV-001).

    `rtsp://USUARIO:SENHA@host:8554/camera` -> `rtsp://***@host:8554/camera`.
    É a única forma de URI que sai desta sonda. Sem userinfo, volta inalterada.
    """
    candidate = (uri or "").strip()
    parts = _split_authority(candidate)
    if parts is None or split_userinfo(candidate) is None:
        return candidate
    scheme, authority, tail = parts
    masked = MASKED_USERINFO + "@" + authority.rsplit("@", 1)[1]
    return f"{scheme}://{masked}{tail}"


def _credentials(uri: str) -> Optional[str]:
    """Valor do header `Authorization` (Basic), ou `None` sem userinfo.

    A senha da câmera pode ter caracteres escapados (`%40`): desfaz o escape
    antes do base64, senão o servidor recusa. Nunca devolve a credencial em
    texto — só o header já codificado.
    """
    userinfo = split_userinfo(uri)
    if not userinfo:
        return None
    user, _sep, password = userinfo.partition(":")
    token = f"{unquote(user)}:{unquote(password)}".encode()
    return "Basic " + base64.b64encode(token).decode()


def _request_url(host: str, port: int, target: str) -> str:
    """URL **absoluta** da request line: `rtsp://host:porta/path?query`.

    A RFC 2326 (RTSP 1.0) exige URL absoluta na request line; o MediaMTX recusa
    o alvo relativo fechando a conexão com `invalid URL (/path)`. Por isso a
    montagem é explícita aqui em vez de reaproveitar `parts.netloc`:

      - o host vem de `parts.hostname`, nunca de `parts.netloc`, então a URL
        não carrega userinfo (DIV-001: a credencial vai **só** no header
        `Authorization: Basic` montado por `_credentials()`);
      - a porta aparece sempre, a mesma usada no `socket.create_connection`;
      - host IPv6 sai entre colchetes (`rtsp://[::1]:8554/live`), senão a URL
        fica inválida.
    """
    literal = f"[{host}]" if ":" in host else host
    return f"rtsp://{literal}:{port}{target}"


def _request_target(uri: str) -> Tuple[str, int, str]:
    """`(host, porta, URL absoluta da request line)` de uma URI RTSP.

    `host`/`porta` são o alvo do `socket.create_connection`; a URL absoluta é
    o alvo da request line (`OPTIONS <url> RTSP/1.0`, idem DESCRIBE), já sem
    userinfo e com porta explícita — ver `_request_url`.

    Levanta `ValueError` (mensagem sem credencial) para esquema errado, host
    ausente ou porta ilegível — o chamador transforma em `STATUS_ERROR`.
    """
    masked = mask_uri(uri)
    try:
        parts = urlsplit((uri or "").strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"URI inválida ({masked!r}): {exc}") from exc
    if parts.scheme.lower() not in RTSP_SCHEMES:
        raise ValueError(
            f"URI inválida ({masked!r}): esquema {parts.scheme or '—'!r} "
            "não é rtsp")
    if not parts.hostname:
        raise ValueError(f"URI inválida ({masked!r}): host ausente")
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    resolved_port = port or RTSP_DEFAULT_PORT
    return parts.hostname, resolved_port, _request_url(parts.hostname,
                                                       resolved_port, target)


# ---------------------------------------------------------------------------
# RTSP sobre socket TCP
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DescribeResult:
    """Resultado tipado da sonda de nível 1.

    `status` é um dos valores do contrato (`"200"`, `"404"`, `"timeout"`,
    `"sem_video"`, `"error"`); `reason` é texto curto para o log estruturado e
    nunca contém credencial (`uri` já vem mascarada).
    """

    status: str = STATUS_ERROR
    reason: str = ""
    uri: str = ""
    has_video: bool = False
    code: Optional[int] = None
    elapsed_s: float = 0.0
    sdp: str = ""

    @property
    def alive(self) -> bool:
        """O path responde **e** declara vídeo (o que o observador chama vivo)."""
        return self.status == STATUS_OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "status": self.status,
            "alive": self.alive,
            "has_video": self.has_video,
            "code": self.code,
            "reason": self.reason,
            "elapsed_s": self.elapsed_s,
        }


def _read_response(sock: socket.socket) -> Tuple[int, str, str]:
    """Lê uma resposta RTSP completa: `(código, reason_phrase, corpo)`.

    Framing mínimo do protocolo: cabeçalhos até `\\r\\n\\r\\n` e corpo de
    `Content-Length` bytes (o SDP do DESCRIBE). Resposta truncada ou conexão
    fechada devolve código 0.
    """
    raw = b""
    while HEADER_END not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            return 0, "", ""
        raw += chunk
        if len(raw) > MAX_RESPONSE_BYTES:
            return 0, "", ""
    head, _, body = raw.partition(HEADER_END)
    lines = head.decode(errors="replace").split("\r\n")
    fields = lines[0].split() if lines and lines[0] else []
    if len(fields) < 2:
        return 0, "", ""
    try:
        code = int(fields[1])
    except ValueError:
        return 0, "", ""
    phrase = " ".join(fields[2:])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = headers.get("content-length", "")
    if length.isdigit():
        wanted = int(length)
        while len(body) < wanted:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        body = body[:wanted]
    return code, phrase, body.decode(errors="replace")


def _exchange(sock, method, request_url, cseq, authorization):
    """Envia uma requisição RTSP e devolve a resposta lida.

    `request_url` é absoluta (`rtsp://host:porta/path?query`): a RFC 2326 não
    aceita alvo relativo na request line e o MediaMTX recusa com `invalid URL`.
    """
    lines = [
        f"{method} {request_url} RTSP/1.0",
        f"CSeq: {cseq}",
        f"User-Agent: {USER_AGENT}",
    ]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    if authorization:
        lines.append(f"Authorization: {authorization}")
    request = "\r\n".join(lines) + "\r\n\r\n"
    sock.sendall(request.encode())
    return _read_response(sock)


def has_video_track(sdp: str) -> bool:
    """O SDP anuncia uma faixa de vídeo (`m=video ...`)?"""
    return any(line.strip().lower().startswith("m=video")
               for line in (sdp or "").splitlines())


def describe(uri: str, timeout: float = DEFAULT_TIMEOUT_S) -> DescribeResult:
    """`OPTIONS` + `DESCRIBE` em `uri`. Nunca levanta; devolve sempre resultado.

    Uma conexão só (o `CSeq` do OPTIONS é 1, o do DESCRIBE é 2), como um
    cliente RTSP de verdade. Timeout do `connect` e da leitura é o mesmo —
    orçamento total fica em até ~2x `timeout` no pior caso de servidor mudo,
    mas a primeira resposta já decide o caso `timeout`.
    """
    started = time.monotonic()
    masked = mask_uri(uri)
    try:
        host, port, request_url = _request_target(uri)
    except ValueError as exc:
        return DescribeResult(status=STATUS_ERROR, reason=str(exc),
                              uri=masked,
                              elapsed_s=round(time.monotonic() - started, 3))

    def _elapsed() -> float:
        return round(time.monotonic() - started, 3)

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        # `TimeoutError` é subclasse de `OSError`; separar só para o status.
        if isinstance(exc, TimeoutError):
            return DescribeResult(status=STATUS_TIMEOUT,
                                  reason="connect_timeout", uri=masked,
                                  elapsed_s=_elapsed())
        return DescribeResult(status=STATUS_ERROR,
                              reason=f"connect_error: {exc}", uri=masked,
                              elapsed_s=_elapsed())

    authorization = _credentials(uri)
    with sock:
        sock.settimeout(timeout)
        try:
            code, _phrase, _body = _exchange(sock, "OPTIONS", request_url, 1,
                                             authorization)
            if code == 0:
                return DescribeResult(status=STATUS_TIMEOUT,
                                      reason="timeout_no_options", uri=masked,
                                      elapsed_s=_elapsed())
            code, phrase, body = _exchange(sock, "DESCRIBE", request_url, 2,
                                           authorization)
        except TimeoutError:
            return DescribeResult(status=STATUS_TIMEOUT,
                                  reason="timeout_no_describe", uri=masked,
                                  elapsed_s=_elapsed())
        except OSError as exc:
            return DescribeResult(status=STATUS_ERROR,
                                  reason=f"rtsp_error: {exc}", uri=masked,
                                  elapsed_s=_elapsed())

    if code == 0:
        return DescribeResult(status=STATUS_ERROR, reason="resposta_invalida",
                              uri=masked, elapsed_s=_elapsed())
    if code == 404:
        return DescribeResult(status=STATUS_NOT_FOUND, reason="rtsp_404",
                              uri=masked, code=code, elapsed_s=_elapsed())
    if code != 200:
        return DescribeResult(status=str(code),
                              reason=f"rtsp_{code}"
                              + (f" {phrase}" if phrase else ""),
                              uri=masked, code=code, elapsed_s=_elapsed())
    video = has_video_track(body)
    if not video:
        # Responder sem `m=video` é o server vivo entregando nada de vídeo:
        # reprovar aqui evita gastar uma sonda de nível 2 à toa.
        return DescribeResult(status=STATUS_NO_VIDEO, reason="sdp_sem_video",
                              uri=masked, code=code, elapsed_s=_elapsed(),
                              sdp=body)
    return DescribeResult(status=STATUS_OK, reason="ok", uri=masked, code=code,
                          has_video=True, elapsed_s=_elapsed(), sdp=body)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rtsp_describe_probe.py",
        description=(
            "Sonda de nível 1: OPTIONS + DESCRIBE do path RTSP e o status "
            "200/404/timeout/sem_video. Exit 0 = path com vídeo, 1 = não."),
        epilog=(
            "Credencial: a URI é mascarada em toda saída (usuário e senha viram "
            "'***'); host, porta e path continuam visíveis.\n\n"
            "Exemplo: rtsp_describe_probe.py --url rtsp://mediamtx:8554/drone"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="URL rtsp:// do path")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S,
        help=f"timeout de connect/leitura em segundos (default: {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--json", action="store_true",
                        help="só a linha JSON em stdout (sem resumo em stderr)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout precisa ser maior que zero")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    result = describe(args.url, timeout=args.timeout)
    if not args.json:
        print(f"[probe] {result.status} {result.reason} "
              f"{mask_uri(args.url)} ({result.elapsed_s:.2f}s)", file=sys.stderr)
    print(json.dumps(result.as_dict(), ensure_ascii=False))
    return 0 if result.alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
