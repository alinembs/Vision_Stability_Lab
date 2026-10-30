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


def require_rtsp_uri():
    """Retorna RTSP_URI do ambiente; encerra o processo se ausente."""
    uri = (os.environ.get(ENV_VAR) or "").strip()
    if not uri:
        sys.exit(f"ERRO: {_MENSAGEM}")
    return uri
