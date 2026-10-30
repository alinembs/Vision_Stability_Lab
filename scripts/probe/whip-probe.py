#!/usr/bin/env python3
"""Sonda WHIP — lê os candidatos ICE que o MediaMTX anuncia no SDP de resposta.

Porte de `arquo:scripts/spike/whip-probe.py` (branch
`feature/SURVEY-110-m1-phone-ingest-spike`), adaptado ao stack do Vision
Stability Lab: defaults apontam para o MediaMTX do VSL (porta 8889) e para o
path `phone` (ingestão do celular).

Não publica mídia: envia um offer WHIP sintético e imprime as linhas
`a=candidate` da resposta. É o suficiente para responder "o candidato sai
127.0.0.1 ou 172.x?", que é a causa raiz do sintoma "WHIP conecta e não há
vídeo" — e, no VSL, é o gate do path `phone` (E0-T4) e do publisher WHIP
sintético (E0-T5).

Uso:
    python3 scripts/probe/whip-probe.py
    python3 scripts/probe/whip-probe.py --base https://192.168.59.102:8889 --path phone

Atenção: `--path phone` cria/derruba uma sessão WHIP de verdade no path
informado. Com `requests`/`urllib` apenas — sem dependência nova.

Exit codes: 0 = resposta WHIP aceita (2xx), 2 = servidor recusou o POST.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.request


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


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="whip-probe.py",
        description=(
            "Envia um offer WHIP sintético e imprime os candidatos ICE, os "
            "codecs e o Location devolvidos pelo MediaMTX."
        ),
        epilog=(
            "Defaults do Vision Stability Lab: --base http://192.168.59.102:8889 "
            "--path phone. As portas do MediaMTX são publicadas no IP da LAN "
            "(E0-T2), então 127.0.0.1 não responde."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--base",
        default="http://192.168.59.102:8889",
        help="base HTTP(S) do MediaMTX (default: http://192.168.59.102:8889)",
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
        help="timeout do POST em segundos (default: 10)",
    )
    args = ap.parse_args()

    url = f"{args.base.rstrip('/')}/{args.path}/whip"
    req = urllib.request.Request(
        url,
        data=offer().encode(),
        headers={"Content-Type": "application/sdp"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = resp.read().decode()
            status = resp.status
            headers = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": exc.code,
                    "error": exc.read().decode(errors="replace")[:400],
                    "url": url,
                }
            )
        )
        return 2
    except urllib.error.URLError as exc:
        # Servidor inacessível (porta fechada, TLS sem CA confiável, DNS).
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": None,
                    "error": f"{type(exc.reason).__name__}: {exc.reason}",
                    "url": url,
                }
            )
        )
        return 2

    candidates = re.findall(r"^a=candidate:(.+)$", body, re.MULTILINE)
    hosts = sorted({c.split()[4] for c in candidates if len(c.split()) > 4})
    print(
        json.dumps(
            {
                "ok": True,
                "url": url,
                "status": status,
                "location": headers.get("Location"),
                "candidate_count": len(candidates),
                "candidate_hosts": hosts,
                "candidates": candidates,
                "answer_codecs": re.findall(r"^a=rtpmap:(\d+ [^\r\n]+)$", body, re.MULTILINE),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
