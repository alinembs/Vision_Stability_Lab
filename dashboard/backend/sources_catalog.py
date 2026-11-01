"""Catálogo de fontes do dashboard (E4-T1).

O dashboard precisa mostrar as três fontes de `deepstream/config/sources.yaml`
mesmo antes de qualquer telemetria da fonte existir (câmera, drone, celular).
Este módulo lê **somente** `id`, `name`, `codec`, `target_fps`, `enabled` e o
caminho de exibição — o caminho é **somente leitura** (decisão do operador,
2026-11-01): editar path pela tela fica para uma etapa posterior.

Duas regras que vêm de decisões já travadas:

- **DIV-001** — a URI da câmera tem usuário e senha e mora em `RTSP_URI`
  (`deploy/.env`). `sources.yaml` referencia a variável (`uri_env`) e este
  módulo **não lê** o valor: devolve `path: null` + `path_env: "RTSP_URI"` e o
  frontend mostra de onde vem. Nenhuma credencial entra em resposta HTTP.
- **Path literal** (drone/celular) é expandido com `os.environ` no mesmo
  formato do YAML (`${MTX_HOST:-mediamtx}`) e mascarado por segurança, mesmo
  sem credencial hoje.

O arquivo é opcional: ausente (host sem o repo montado), ilegível ou inválido
devolve `[]` — o dashboard continua servindo o que existe na telemetria, sem
500. Isso é o oposto do `deepstream/sources.py`, onde caminho explícito errado é
erro: lá o erro muda o pipeline, aqui só a lista de cartões.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import yaml

ENV_CONFIG = "SOURCES_CONFIG"

DASHBOARD_DIR = Path(__file__).resolve().parent.parent          # .../dashboard
REPO_ROOT = DASHBOARD_DIR.parent                                # .../Vision_Stability_Lab

# Ordem de procura quando `SOURCES_CONFIG` não está definido: caminho do
# container (mount `ro` no compose) e caminho do host (pytest / uvicorn local).
DEFAULT_CANDIDATES = (
    Path("/app/deepstream/config/sources.yaml"),
    REPO_ROOT / "deepstream" / "config" / "sources.yaml",
)

# `${VAR}` e `${VAR:-default}` — o mesmo dialeto que o `sources.yaml` usa.
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str) -> str:
    """Expande `${VAR}`/`${VAR:-default}` usando o ambiente do dashboard."""

    def repl(match: "re.Match[str]") -> str:
        name, default = match.group(1), match.group(2)
        from_env = os.environ.get(name)
        if from_env:
            return from_env
        return default if default is not None else ""

    return _VAR_PATTERN.sub(repl, value)


def mask_uri(uri: str) -> str:
    """Troca o userinfo por `***` (DIV-001), preservando host, porta e path."""
    parts = urlsplit(uri)
    if parts.username is None and parts.password is None:
        return uri
    netloc = f"***@{parts.hostname or ''}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def _catalog_path() -> Optional[Path]:
    """Caminho do YAML: `SOURCES_CONFIG` vence; senão o primeiro candidato vivo."""
    explicit = os.environ.get(ENV_CONFIG)
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    for candidate in DEFAULT_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def load_catalog() -> List[Dict[str, Any]]:
    """Lista de fontes para a UI, sem credencial e sem erro fatal.

    Cada item: `id`, `name`, `codec`, `target_fps`, `enabled`, `path`,
    `path_origin` (`"config"` quando o caminho está no YAML, `"env"` quando vem
    de variável) e `path_env` (nome da variável, quando for o caso).
    """
    path = _catalog_path()
    if path is None:
        return []

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(raw, dict):
        return []

    entries: List[Dict[str, Any]] = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        uri_env = item.get("uri_env")
        if uri_env:
            display: Optional[str] = None
            origin = "env"
        else:
            expanded = _expand(str(item.get("uri") or ""))
            display = mask_uri(expanded) if expanded else None
            origin = "config"
        entries.append({
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "codec": item.get("codec"),
            "target_fps": item.get("target_fps"),
            "enabled": bool(item.get("enabled", True)),
            "path": display,
            "path_origin": origin,
            "path_env": str(uri_env) if uri_env else None,
        })
    return entries
