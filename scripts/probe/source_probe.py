#!/usr/bin/env python3
"""Pré-check de uma fonte por id, rodando **dentro** do container do pipeline.

O `scripts/switch_source.sh` roda a sonda no host, onde há GStreamer. O
dashboard não tem GStreamer, então executa este invólucro dentro do
`vision-stability-app` (via `docker exec`), onde o ambiente é o mesmo que o
pipeline vai usar depois do restart: `RTSP_URI`, `MTX_HOST` e `SOURCES_CONFIG`
vêm do container, não do `deploy/.env` do host.

Uso (dentro do container):

    python3 /app/scripts/probe/source_probe.py --source drone [--seconds 5]

Contrato de saída (uma linha `chave=valor` por linha em stdout):

    ok=1|0
    reason=<ok|no_frames_on_path|unstable_path|probe_failed|id_invalido|config_invalida>
    masked_uri=rtsp://***@host:porta/path

Exit codes: `0` aprovado, `4` reprovado (mesmo contrato de pré-check do
`switch_source.sh`), `2` uso/configuração inválidos. Credencial nunca é
impressa (DIV-001): `masked_uri` é a única forma de URI na saída.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

PROBE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROBE_DIR.parent.parent

# `sources.py` é puro (sem `gi`): resolver o id aqui não carrega GStreamer.
if str(REPO_ROOT / "deepstream") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "deepstream"))

import sources  # noqa: E402  (depois do sys.path)


def _emit(ok: bool, reason: str, masked_uri: str) -> None:
    print(f"ok={1 if ok else 0}")
    print(f"reason={reason}")
    print(f"masked_uri={masked_uri}", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="source_probe.py",
        description="Pré-check RTSP de uma fonte do sources.yaml, por id.")
    parser.add_argument("--source", required=True,
                        help="id da fonte em deepstream/config/sources.yaml")
    parser.add_argument("--seconds", type=float, default=5.0,
                        help="janela de leitura da sonda (default: 5)")
    args = parser.parse_args(argv)

    try:
        candidates = sources.load_sources(None, env=os.environ)
    except sources.SourceConfigError as exc:
        _emit(False, "config_invalida", "")
        print(str(exc), file=sys.stderr)
        return 2

    by_id = {source.id: source for source in candidates}
    source = by_id.get(args.source)
    if source is None:
        _emit(False, "id_invalido", "")
        print(f"fontes válidas: {', '.join(sorted(by_id)) or '<nenhuma>'}",
              file=sys.stderr)
        return 2

    command = [
        sys.executable,
        str(PROBE_DIR / "rtsp_read_probe.py"),
        "--url", source.uri,
        "--seconds", f"{args.seconds:g}",
        "--json",
    ]
    completed = subprocess.run(command, capture_output=True, text=True)

    report = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue

    ok = completed.returncode == 0
    if ok:
        reason = "ok"
    else:
        reason = str(report.get("reason") or "probe_failed")
        if completed.stderr.strip():
            print(completed.stderr.strip()[:500], file=sys.stderr)
    _emit(ok, reason, source.masked_uri())
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
