"""Resolução da URI da fonte RTSP a partir do ambiente.

As credenciais da câmera não ficam no código (DIV-001, docs/incident-log.md).
`RTSP_URI` vem do ambiente:

- no host: `deploy/.env` (criado a partir de `deploy/.env.example`);
- no container: variável do compose (`deploy/docker-compose.yml`).

Sem valor, o processo encerra com mensagem explícita em vez de usar um
default embutido — foi assim que a credencial vazou para o repositório.
"""

import os
import sys

ENV_VAR = "RTSP_URI"

_MENSAGEM = (
    "RTSP_URI nao definida.\n"
    "  Copie deploy/.env.example para deploy/.env e preencha a URI da fonte,\n"
    "  ou exporte RTSP_URI no ambiente.\n"
    "  Nenhuma credencial e embutida no codigo (DIV-001, docs/incident-log.md)."
)


class RtspUriError(ValueError):
    """`RTSP_URI` ausente ou vazia no ambiente (DIV-001).

    Existe para que quem **não** é o entrypoint do processo (`sources.py`)
    possa converter a ausência da variável no próprio erro de configuração, em
    vez de herdar um `SystemExit` — que `except Exception` não captura e que
    saía sem evento `startup_failed` na telemetria.
    """

    def __init__(self) -> None:
        super().__init__(_MENSAGEM)


def read_rtsp_uri():
    """Retorna `RTSP_URI` do ambiente ou levanta `RtspUriError`."""
    uri = (os.environ.get(ENV_VAR) or "").strip()
    if not uri:
        raise RtspUriError()
    return uri


def require_rtsp_uri():
    """Retorna RTSP_URI do ambiente; encerra o processo se ausente.

    Reservado aos entrypoints de bancada (`deepstream/rtsp_*_test.py`). Quem
    precisa de mensagem tratável usa `read_rtsp_uri()`.
    """
    try:
        return read_rtsp_uri()
    except RtspUriError as exc:
        sys.exit(f"ERRO: {exc}")
