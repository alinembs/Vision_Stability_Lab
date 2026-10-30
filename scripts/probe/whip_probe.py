#!/usr/bin/env python3
"""Sonda WHIP — lê os candidatos ICE que o MediaMTX anuncia no SDP de resposta.

Porte de `arquo:scripts/spike/whip-probe.py` (branch
`feature/SURVEY-110-m1-phone-ingest-spike`), adaptado ao stack do Vision
Stability Lab: defaults apontam para o MediaMTX do VSL (porta 8889, **HTTPS
desde o E0-T4**) e para o path `phone` (ingestão do celular).

Não publica mídia: envia um offer WHIP sintético e imprime as linhas
`a=candidate` da resposta. É o suficiente para responder "o candidato sai
127.0.0.1 ou 172.x?", que é a causa raiz do sintoma "WHIP conecta e não há
vídeo" — e, no VSL, é o gate do path `phone` (E0-T4) e do publisher WHIP
sintético (E0-T5).

Além dos candidatos, o relatório traz a **m-line** e os `a=rtpmap` da resposta
(`answer_media`, `answer_mline`, `answer_codecs`): é o que prova qual codec o
MediaMTX aceitou negociar como evidência da sonda.

Ciclo de vida da sessão: o WHIP é um POST no recurso `<base>/<path>/whip`; o
servidor devolve o `Location` da sessão. Ao final a sonda faz **DELETE** nesse
`Location` (o mesmo que o navegador faz ao fechar), para não deixar uma sessão
pendurada no path sondado. O efeito aparece em `delete` no JSON e no log do
container (`docker logs vision-mediamtx`).

**TLS (E0-T4).** Com `webrtcEncryption: yes`, a 8889 só fala HTTPS, e o
certificado é assinado pela CA interna de `deploy/certs/` — que **não** está no
store do sistema. O default, portanto, é falhar fechado: sem `--cacert` a
verificação usa as CAs do sistema e a sonda recusa o servidor (é o
comportamento correto, não um bug). `--insecure` existe para depurar um
certificado errado, nunca para validar.

Uso:
    python3 scripts/probe/whip_probe.py --cacert deploy/certs/ca.crt
    python3 scripts/probe/whip_probe.py --cacert deploy/certs/ca.crt --path phone
    python3 scripts/probe/whip_probe.py --base http://192.168.59.102:8889  # instalação sem TLS

Atenção: `--path phone` cria e encerra uma sessão WHIP de verdade no path
informado. Com `urllib` + `ssl` apenas — sem dependência nova.

Exit codes: 0 = resposta WHIP aceita (2xx), 2 = servidor recusou o POST ou está
inacessível, 3 = problema de TLS/CA local (arquivo de CA ausente ou ilegível).
Uso incorreto de argumento também sai 2, pelo `argparse`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence


# Campos da resposta SDP que interessam à sonda.
CANDIDATE_RE = re.compile(r"^a=candidate:(.+?)\r?$", re.MULTILINE)
MLINE_RE = re.compile(r"^(m=\S+.*?)\r?$", re.MULTILINE)
RTPMAP_RE = re.compile(r"^a=rtpmap:(\d+ [^\r\n]+?)\r?$", re.MULTILINE)

# Base default da sonda: o MediaMTX do VSL com TLS (E0-T4). Sobrescreva com
# `--base` para uma instalação sem `webrtcEncryption`, ou para outro host.
DEFAULT_BASE = "https://192.168.59.102:8889"



def offer() -> str:
    """Monta um offer SDP mínimo de sendonly H.264 (o mesmo do spike original)."""
    fingerprint = ":".join(f"{b:02X}" for b in hashlib.sha256(os.urandom(32)).digest())
    return "\r\n".join(
        [
            "v=0",
            "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
            "s=-",
            "t=0 0",
            "a=group:BUNDLE 0",
            "m=video 9 UDP/TLS/RTP/SAVPF 96",
            "c=IN IP4 0.0.0.0",
            "a=ice-ufrag:probeufrag",
            "a=ice-pwd:probeicepwdprobeicepwdprobe",
            f"a=fingerprint:sha-256 {fingerprint}",
            "a=setup:actpass",
            "a=mid:0",
            "a=sendonly",
            "a=rtcp-mux",
            "a=rtpmap:96 H264/90000",
            "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
            "a=ssrc:1234 cname:probe",
            "",
        ]
    )


def build_ssl_context(
    base: str,
    cacert: Optional[str] = None,
    insecure: bool = False,
) -> Optional[ssl.SSLContext]:
    """Contexto TLS para a base informada, ou `None` quando o esquema é HTTP.

    Função pura (testada em `scripts/tests/test_probe_logic.py`) — só monta o
    contexto, não abre conexão. Três modos:

    - `--insecure`: aceita qualquer certificado (`check_hostname=False`,
      `verify_mode=CERT_NONE`). Existe para depurar um certificado errado,
      **nunca** para validar um deployment: sem verificação, qualquer host da
      LAN responde no lugar do Jetson.
    - `--cacert <arquivo>`: verifica contra a CA interna de `deploy/certs/`.
      É o modo da validação do E0-T4.
    - nenhum dos dois: verifica contra o store do sistema. Com a CA interna do
      VSL isso **falha** (`CERTIFICATE_VERIFY_FAILED`) — de propósito: a sonda
      não deve confiar em servidor desconhecido só porque está na LAN.
    """
    if not base.lower().startswith("https"):
        return None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if cacert:
        return ssl.create_default_context(cafile=cacert)
    return ssl.create_default_context()


def tls_label(base: str, cacert: Optional[str] = None, insecure: bool = False) -> str:
    """Rótulo do modo de TLS, para o relatório da sonda não ser ambíguo.

    `off` (HTTP), `insecure` (sem verificação) ou `verify:<arquivo|sistema>`.
    Sem isso, um relatório com `status: 201` não diz se o certificado foi
    verificado — e a verificação é justamente o ponto do E0-T4.
    """
    if not base.lower().startswith("https"):
        return "off"
    if insecure:
        return "insecure"
    return "verify:" + (cacert or "sistema")


def parse_answer_sdp(body: str) -> Dict[str, Any]:
    """Extrai candidatos ICE, m-line e codecs negociados do SDP de resposta.

    Função pura (testada em `scripts/tests/test_probe_logic.py`). O SDP vem com
    CRLF; sem normalizar, o `\\r` fica colado em cada valor (`"typ host\\r"`) e
    os regexes ancorados em fim de linha não casam (era o motivo de
    `answer_codecs` sair sempre vazio).
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    candidates = [c.strip() for c in CANDIDATE_RE.findall(text) if c.strip()]
    hosts: List[str] = []
    for candidate in candidates:
        fields = candidate.split()
        # candidate:<foundation> <component> <transport> <priority> <address> ...
        if len(fields) > 4:
            host = fields[4]
            if host not in hosts:
                hosts.append(host)
    mline_match = MLINE_RE.search(text)
    mline = mline_match.group(1).strip() if mline_match else None
    return {
        "candidate_count": len(candidates),
        "candidates": candidates,
        "candidate_hosts": sorted(hosts),
        "answer_mline": mline,
        "answer_media": mline.split()[0].split("=", 1)[1] if mline else None,
        "answer_codecs": [codec.strip() for codec in RTPMAP_RE.findall(text)],
    }


def post_offer(
    url: str,
    timeout: float,
    context: Optional[ssl.SSLContext] = None,
) -> Dict[str, Any]:
    """Envia o offer WHIP e devolve o resultado cru do POST.

    Nunca levanta: erro HTTP/inacessibilidade/ TLS voltam como `ok: False` com
    `status` e `error`, que é o que o relatório precisa mostrar. `context` nulo
    em URL HTTP é o caso da instalação sem `webrtcEncryption`.
    """
    request = urllib.request.Request(
        url,
        data=offer().encode(),
        headers={"Content-Type": "application/sdp"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return {
                "ok": True,
                "status": response.status,
                "headers": dict(response.headers),
                "body": response.read().decode(errors="replace"),
            }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "error": exc.read().decode(errors="replace")[:400],
        }
    except urllib.error.URLError as exc:
        # Servidor inacessível (porta fechada, TLS sem CA confiável, DNS).
        # `SSLCertVerificationError` chega aqui dentro de `reason`.
        return {
            "ok": False,
            "status": None,
            "error": f"{type(exc.reason).__name__}: {exc.reason}",
        }


def delete_session(
    session_url: str,
    timeout: float,
    context: Optional[ssl.SSLContext] = None,
) -> Dict[str, Any]:
    """Encerra a sessão WHIP no `Location` (o DELETE que o navegador faria)."""
    request = urllib.request.Request(session_url, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return {"ok": True, "status": response.status}
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "error": exc.read().decode(errors="replace")[:400],
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "status": None,
            "error": f"{type(exc.reason).__name__}: {exc.reason}",
        }


def _print(payload: Dict[str, Any], indent: Optional[int] = 2) -> None:
    print(json.dumps(payload, indent=indent, ensure_ascii=False))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Lê os argumentos da linha de comando (testável sem socket)."""
    ap = argparse.ArgumentParser(
        prog="whip_probe.py",
        description=(
            "Envia um offer WHIP sintético, imprime os candidatos ICE, a m-line "
            "e os codecs devolvidos pelo MediaMTX e encerra a sessão com DELETE."
        ),
        epilog=(
            "Defaults do Vision Stability Lab: --base https://192.168.59.102:8889 "
            "--path phone. As portas do MediaMTX são publicadas no IP da LAN "
            "(E0-T2), então 127.0.0.1 não responde. Desde o E0-T4 o WebRTC exige "
            "TLS com a CA interna: passe --cacert deploy/certs/ca.crt.\n\n"
            "Exit codes: 0 = sessão WHIP aceita (2xx e DELETE executado), "
            "2 = servidor recusou o POST ou está inacessível, "
            "3 = CA/certificado local ilegível."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"base HTTP(S) do MediaMTX (default: {DEFAULT_BASE})",
    )
    ap.add_argument(
        "--path",
        default="phone",
        help="nome do path WHIP a sondar (default: phone)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="timeout do POST/DELETE em segundos (default: 10)",
    )
    ap.add_argument(
        "--keep-session",
        action="store_true",
        help="não envia DELETE (deixa a sessão para o MediaMTX expirar)",
    )
    # `--cacert` e `--insecure` são excludentes: aceitar os dois esconderia
    # qual verificação valeu, e o relatório precisa ser lido sem ambiguidade.
    tls = ap.add_mutually_exclusive_group()
    tls.add_argument(
        "--cacert",
        metavar="ARQUIVO",
        help="CA (PEM) para verificar o certificado do servidor — "
        "ex.: deploy/certs/ca.crt",
    )
    tls.add_argument(
        "--insecure",
        action="store_true",
        help="NÃO verifica o certificado do servidor (só para depurar TLS)",
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    url = f"{args.base.rstrip('/')}/{args.path}/whip"

    # CA local ilegível é erro de uso, não de rede: sai 3 com mensagem clara em
    # vez de virar um `CERTIFICATE_VERIFY_FAILED` que parece falha do servidor.
    try:
        context = build_ssl_context(args.base, args.cacert, args.insecure)
    except (OSError, ssl.SSLError) as exc:
        _print(
            {
                "ok": False,
                "status": None,
                "error": f"CA local ilegível ({args.cacert}): {exc}",
                "url": url,
            }
        )
        return 3

    result = post_offer(url, args.timeout, context)
    if not result["ok"]:
        _print(
            {
                "ok": False,
                "status": result.get("status"),
                "error": result.get("error"),
                "url": url,
                "tls": tls_label(args.base, args.cacert, args.insecure),
            }
        )
        return 2

    body = result["body"]
    location = result["headers"].get("Location")
    # O MediaMTX devolve `Location` relativa em algumas versões.
    session_url = urllib.parse.urljoin(url, location) if location else None

    report: Dict[str, Any] = {
        "ok": True,
        "url": url,
        "status": result["status"],
        "tls": tls_label(args.base, args.cacert, args.insecure),
        "location": location,
        "session_url": session_url,
        **parse_answer_sdp(body),
    }

    if session_url and not args.keep_session:
        report["delete"] = delete_session(session_url, args.timeout, context)
    elif args.keep_session:
        report["delete"] = None
    else:
        report["delete"] = {
            "ok": False,
            "status": None,
            "error": "resposta sem header Location: sessão não identificada",
        }

    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
