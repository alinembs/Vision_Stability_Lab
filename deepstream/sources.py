"""Descritor declarativo das fontes do Vision Stability Lab (E2-T1).

Uma fonte ativa por vez (D1, ADR-0006): câmera fixa (H.265), drone (H.264,
RTMP -> MediaMTX) e celular Android (H.264, WHIP -> MediaMTX) são alternativas,
não simultâneas. Este módulo é **puro** (sem `gi`/`pyds`) para rodar no host
sem GPU, dentro do container e sob `pytest`.

O que ele resolve, e para quem:

- `load_sources()`  -> lista validada de `SourceConfig`, com o `default_source`
  do arquivo anexado (a lista é uma `SourceList`, que é um `list` normal com o
  atributo `default_source`);
- `resolve_active_source(sources)` -> qual fonte está ativa, por precedência
  `ACTIVE_SOURCE` (env) > `data/config/active_source` (arquivo) >
  `default_source` (YAML) > primeiro `enabled`;
- `depay_factory(codec)` -> `("rtph265depay", "h265parse")` e o par H.264
  equivalente (D5: codec é por fonte, não global do container).

Caminho do YAML: `SOURCES_CONFIG` (env) > `deepstream/config/sources.yaml`
resolvido **a partir deste arquivo** — o mesmo caminho funciona no host e no
container, onde o cwd é `/app` e `../deepstream` está montado em
`/app/deepstream`; se o caminho do módulo não existir, tenta relativo ao cwd.

Fallback legado: sem `sources.yaml` (arquivo ausente ou vazio) ou com
`VSL_SOURCE_SELECTION=off`, o módulo devolve **uma** fonte derivada de
`RTSP_URI`/`RTSP_CODEC` — o comportamento de antes do E2. É o que mantém um
clone antigo subindo e o que torna o rollback do plano (seção 9, nível 1) uma
variável de ambiente.

Credencial nunca sai daqui: `mask_uri()` troca o userinfo por `***`
(DIV-001). A implementação é local, e não importada de
`scripts/probe/rtsp_read_probe.py`, porque aquela sonda importa `gi` — importá-la
traria GStreamer para dentro de um módulo que precisa rodar sem ele.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from rtsp_env import require_rtsp_uri

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(MODULE_DIR)
DEFAULT_CONFIG_PATH = os.path.join(MODULE_DIR, "config", "sources.yaml")
DEFAULT_CONTROL_PATH = os.path.join(REPO_ROOT, "data", "config", "active_source")

ENV_CONFIG = "SOURCES_CONFIG"          # caminho alternativo do YAML
ENV_ACTIVE_SOURCE = "ACTIVE_SOURCE"    # fonte ativa; vence o arquivo
ENV_SELECTION = "VSL_SOURCE_SELECTION"  # "off" => caminho legado (rollback)
ENV_URI = "RTSP_URI"                   # URI da fonte legada (DIV-001)
ENV_CODEC = "RTSP_CODEC"               # codec da fonte legada
ENV_LEGACY_TARGET_FPS = "RTSP_TARGET_FPS"

# D1: câmera, drone e celular são alternativas — nunca mais que três.
MAX_SOURCES = 3

VALID_CODECS = ("h264", "h265")
VALID_TRANSPORTS = {"tcp": 4, "udp": 1}  # máscara de GstRtsp.RTSPLowerTrans
VALID_SCHEMES = ("rtsp", "rtsps")
DEFAULT_CODEC = "h265"
DEFAULT_TRANSPORT = "tcp"
DEFAULT_LATENCY_MS = 200     # mesmo valor que o rtspsrc já usava
DEFAULT_TARGET_FPS = 20.0    # câmera medida em 19,90 fps (data/logs/live.jsonl)
LEGACY_SOURCE_ID = "camera"
MASKED_USERINFO = "***"

# `id` vai virar nome de arquivo/argumento de script: alfabeto restrito.
SOURCE_ID_RE = re.compile(r"^[a-z0-9_-]+$")
# `${VAR}` e `${VAR:-default}` — o mesmo formato que o compose usa.
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class SourceConfigError(RuntimeError):
    """Configuração de fonte inválida — sempre com mensagem acionável.

    Quem consome é o `pipeline_real.py` no startup (vira `startup_failed` +
    exit 1) e o `scripts/switch_source.sh` (E2-T3, validação sem subir nada).
    Por isso a mensagem carrega os ids válidos: uma troca que "não pegou" é
    pior que uma troca que falha (seção 14.1 do plano).
    """


@dataclass(frozen=True)
class SourceConfig:
    """Uma fonte declarada em `sources.yaml`, com a URI já resolvida."""

    id: str
    name: str
    uri: str
    codec: str = DEFAULT_CODEC
    transport: str = DEFAULT_TRANSPORT
    latency_ms: int = DEFAULT_LATENCY_MS
    target_fps: float = DEFAULT_TARGET_FPS
    enabled: bool = True
    # Gate de qualidade por fonte (D3): a latência do drone é reportada, não
    # gated. Forma livre: `{"latency_p95_ms": 300}`, `{"report_only": true}`.
    gate: Optional[Dict[str, Any]] = None

    @property
    def protocols(self) -> int:
        """Máscara de `rtspsrc.protocols` (4 = TCP, 1 = UDP unicast)."""
        return VALID_TRANSPORTS[self.transport]

    @property
    def depay_factory(self) -> str:
        return depay_factory(self.codec)[0]

    @property
    def parser_factory(self) -> str:
        return depay_factory(self.codec)[1]

    def masked_uri(self) -> str:
        return mask_uri(self.uri)


class SourceList(list):
    """Lista de `SourceConfig` que carrega também o `default_source` do YAML.

    `resolve_active_source()` recebe apenas a lista (contrato do plano), então
    o default viaja junto com ela em vez de virar um terceiro parâmetro nem um
    estado global do módulo. Continua sendo um `list`: indexar, iterar e
    `len()` funcionam como em qualquer outra lista.
    """

    def __init__(self, sources: List[SourceConfig],
                 default_source: Optional[str] = None) -> None:
        super().__init__(sources)
        self.default_source = default_source


# -- URI: mascaramento e decomposição ---------------------------------------
def mask_uri(uri: str) -> str:
    """Mascara a credencial da URI preservando host, porta e path.

    `rtsp://USUARIO:SENHA@host:8554/drone` -> `rtsp://***@host:8554/drone`.
    É a única forma de URI que pode ir para log/telemetria (DIV-001); o que o
    operador precisa para diagnosticar (host, porta, path) continua visível.
    URI sem userinfo volta inalterada.
    """
    candidate = (uri or "").strip()
    scheme, sep, rest = candidate.partition("://")
    if not sep:
        return candidate
    authority_end = len(rest)
    for marker in "/?#":
        index = rest.find(marker)
        if index != -1:
            authority_end = min(authority_end, index)
    authority = rest[:authority_end]
    if "@" not in authority:
        return candidate
    host_part = authority.rsplit("@", 1)[1]
    return f"{scheme}://{MASKED_USERINFO}@{host_part}{rest[authority_end:]}"


def endpoint_parts(uri: str) -> Dict[str, Any]:
    """Decompõe a URI em `host`, `port` e `path` — nunca em credencial.

    Usado no evento `source_active`: o operador precisa ver *de onde* vem o
    vídeo, sem que a senha da câmera apareça no `docker logs`.
    """
    parts = urlsplit((uri or "").strip())
    try:
        port = parts.port
    except ValueError:  # porta não numérica: não é motivo para derrubar nada
        port = None
    if port is None:
        port = 322 if parts.scheme == "rtsps" else 554
    return {"host": parts.hostname or "", "port": port,
            "path": parts.path or "/"}


# -- expansão de variáveis ---------------------------------------------------
def expand_vars(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Expande `${VAR}` e `${VAR:-default}` no texto (formato do compose).

    Sem default e sem valor no ambiente é **erro**, não string vazia: um
    `rtsp://${IP_DA_CAMERA}:554/...` sem `IP_DA_CAMERA` produziria uma URI
    inválida que só falharia depois, no `rtspsrc`, com mensagem opaca.
    """
    environ = os.environ if env is None else env
    missing: List[str] = []

    def _substitute(match: "re.Match[str]") -> str:
        name, default = match.group(1), match.group(2)
        value = environ.get(name)
        if value:
            return value
        if default is not None:
            return default
        missing.append(name)
        return match.group(0)

    expanded = VAR_RE.sub(_substitute, text or "")
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise SourceConfigError(
            f"variável de ambiente ausente na uri ({mask_uri(text)}): {names}\n"
            f"  defina {names} no ambiente ou use ${{{names}:-<default>}} "
            "no sources.yaml")
    return expanded


# -- carga do YAML -----------------------------------------------------------
def resolve_config_path(path: Optional[str] = None,
                        env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Caminho do YAML de fontes, ou `None` quando o fallback legado vale.

    Ordem: argumento explícito > `SOURCES_CONFIG` > caminho do módulo >
    caminho relativo ao cwd. `None` = arquivo não encontrado **ou** vazio
    (`/dev/null`), que é o gatilho do fallback legado.
    """
    environ = os.environ if env is None else env
    if path is None:
        path = (environ.get(ENV_CONFIG) or "").strip() or None
    candidates = []
    if path:
        candidates.append(path)
    else:
        candidates.append(DEFAULT_CONFIG_PATH)
        candidates.append(os.path.join(os.getcwd(), "deepstream", "config",
                                       "sources.yaml"))
        candidates.append(os.path.join(os.getcwd(), "config", "sources.yaml"))
    for candidate in candidates:
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
        except OSError:
            continue
    return None


def selection_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """`False` quando `VSL_SOURCE_SELECTION=off` (rollback nível 1)."""
    environ = os.environ if env is None else env
    return (environ.get(ENV_SELECTION) or "").strip().lower() != "off"


def _read_document(path: str) -> Optional[Dict[str, Any]]:
    """Lê o YAML; devolve `None` para arquivo vazio/sem mapeamento."""
    import yaml  # import local: quem só usa o fallback legado não precisa dele

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except OSError as exc:
        raise SourceConfigError(
            f"não foi possível ler {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SourceConfigError(
            f"{path} não é YAML válido: {exc}") from exc
    if document is None:
        return None
    if not isinstance(document, Mapping):
        raise SourceConfigError(
            f"{path}: o documento precisa ser um mapeamento com `sources:` "
            f"(recebido {type(document).__name__})")
    return dict(document)


def _as_float(value: Any, source_id: str, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceConfigError(
            f"fonte {source_id!r}: `{field_name}` precisa ser número "
            f"(recebido {value!r})") from exc
    return number


def _build_source(entry: Mapping[str, Any], index: int,
                  env: Mapping[str, str]) -> SourceConfig:
    """Valida uma entrada do YAML e devolve o `SourceConfig` correspondente."""
    where = f"sources[{index}]"
    raw_id = str(entry.get("id") or "").strip()
    if not raw_id:
        raise SourceConfigError(f"{where}: `id` é obrigatório")
    if not SOURCE_ID_RE.match(raw_id):
        raise SourceConfigError(
            f"{where}: id {raw_id!r} inválido — use apenas [a-z0-9_-] "
            "(o id vira argumento de script e nome de arquivo de estado)")
    where = f"fonte {raw_id!r}"

    uri_field = entry.get("uri")
    uri_env = entry.get("uri_env")
    if uri_field and uri_env:
        raise SourceConfigError(
            f"{where}: use `uri` **ou** `uri_env`, não os dois "
            "(a credencial da câmera não entra no repositório, DIV-001)")
    if uri_env:
        var = str(uri_env).strip()
        raw_uri = (env.get(var) or "").strip()
        if not raw_uri:
            raise SourceConfigError(
                f"{where}: variável {var} (uri_env) não definida no ambiente.\n"
                f"  copie deploy/.env.example para deploy/.env e preencha, ou "
                f"exporte {var}.\n"
                "  Nenhuma credencial é embutida no código nem no "
                "sources.yaml (DIV-001)")
    elif uri_field:
        raw_uri = str(uri_field).strip()
    else:
        raise SourceConfigError(
            f"{where}: declare `uri` ou `uri_env`")
    uri = expand_vars(raw_uri, env)

    parts = urlsplit(uri)
    if parts.scheme.lower() not in VALID_SCHEMES:
        raise SourceConfigError(
            f"{where}: uri precisa de esquema rtsp:// (recebido "
            f"{mask_uri(uri)!r})")
    if not parts.hostname:
        raise SourceConfigError(
            f"{where}: uri sem host (recebido {mask_uri(uri)!r})")

    codec = str(entry.get("codec") or DEFAULT_CODEC).strip().lower()
    if codec not in VALID_CODECS:
        raise SourceConfigError(
            f"{where}: codec {codec!r} inválido — use um de "
            f"{', '.join(VALID_CODECS)}")

    transport = str(entry.get("transport") or DEFAULT_TRANSPORT).strip().lower()
    if transport not in VALID_TRANSPORTS:
        raise SourceConfigError(
            f"{where}: transport {transport!r} inválido — use "
            f"{', '.join(sorted(VALID_TRANSPORTS))}")

    latency_ms = int(_as_float(entry.get("latency_ms", DEFAULT_LATENCY_MS),
                               raw_id, "latency_ms"))
    if latency_ms < 0:
        raise SourceConfigError(f"{where}: `latency_ms` não pode ser negativo")

    target_fps = _as_float(entry.get("target_fps", DEFAULT_TARGET_FPS),
                           raw_id, "target_fps")
    if target_fps <= 0:
        raise SourceConfigError(
            f"{where}: `target_fps` precisa ser > 0 (recebido {target_fps}) — "
            "é o alvo de fps da fonte, insumo do watchdog por fonte (E5-T1)")

    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SourceConfigError(
            f"{where}: `enabled` precisa ser true/false (recebido {enabled!r})")

    gate = entry.get("gate")
    if gate is not None and not isinstance(gate, Mapping):
        raise SourceConfigError(
            f"{where}: `gate` precisa ser um mapeamento (recebido "
            f"{type(gate).__name__})")

    name = str(entry.get("name") or raw_id).strip()
    return SourceConfig(id=raw_id, name=name, uri=uri, codec=codec,
                        transport=transport, latency_ms=latency_ms,
                        target_fps=target_fps, enabled=enabled,
                        gate=dict(gate) if gate is not None else None)


def _legacy_source(env: Mapping[str, str],
                   live_env: bool = False) -> SourceConfig:
    """A fonte de antes do E2: `RTSP_URI` + `RTSP_CODEC` do ambiente.

    Mantém vivos o rollback (`VSL_SOURCE_SELECTION=off`) e o clone antigo, onde
    não existe `sources.yaml`. O id é `camera` porque hoje a única URI do
    projeto é a da câmera fixa — e porque id estável preserva o consumo de
    `source_id` pela telemetria/watchdog (E5).

    Com o ambiente real (`live_env`), a URI continua vindo de
    `rtsp_env.require_rtsp_uri()` — o mesmo contrato da DIV-001 de antes do E2,
    com a mesma mensagem e o mesmo `sys.exit` (exit code 1). Ambiente injetado
    (testes) levanta `SourceConfigError` em vez de derrubar o processo.
    """
    if live_env:
        uri = require_rtsp_uri()
    else:
        uri = (env.get(ENV_URI) or "").strip()
    if not uri:
        raise SourceConfigError(
            "RTSP_URI nao definida e nenhuma fonte declarada em "
            f"{DEFAULT_CONFIG_PATH}.\n"
            "  Copie deploy/.env.example para deploy/.env e preencha a URI da "
            "fonte, ou exporte RTSP_URI no ambiente.\n"
            "  Nenhuma credencial e embutida no codigo (DIV-001, "
            "docs/incident-log.md).")
    codec = (env.get(ENV_CODEC) or DEFAULT_CODEC).strip().lower()
    if codec not in VALID_CODECS:
        raise SourceConfigError(
            f"{ENV_CODEC}={codec!r} inválido — use um de "
            f"{', '.join(VALID_CODECS)}")
    target_fps = _as_float(env.get(ENV_LEGACY_TARGET_FPS, DEFAULT_TARGET_FPS),
                           LEGACY_SOURCE_ID, "target_fps")
    return SourceConfig(id=LEGACY_SOURCE_ID,
                        name="fonte legada (RTSP_URI)",
                        uri=uri, codec=codec,
                        target_fps=target_fps if target_fps > 0
                        else DEFAULT_TARGET_FPS)


def load_sources(path: Optional[str] = None,
                 env: Optional[Mapping[str, str]] = None) -> SourceList:
    """Carrega e valida `sources.yaml`; cai no legado quando não há arquivo.

    Erros de configuração levantam `SourceConfigError` com mensagem legível
    (sem traceback no `docker logs`) — nunca um default silencioso.
    """
    environ = os.environ if env is None else env
    # `live_env`: o ambiente do processo de verdade (não um dict de teste) —
    # é o caso em que o caminho legado usa `require_rtsp_uri()`.
    live_env = env is None or env is os.environ

    def _legacy() -> SourceList:
        return SourceList([_legacy_source(environ, live_env)],
                          default_source=LEGACY_SOURCE_ID)

    if not selection_enabled(environ):
        return _legacy()

    config_path = resolve_config_path(path, environ)
    if config_path is None:
        return _legacy()

    document = _read_document(config_path)
    if document is None:
        # arquivo só com comentários: mesmo tratamento de "sem arquivo"
        return _legacy()

    entries = document.get("sources")
    if not isinstance(entries, list) or not entries:
        raise SourceConfigError(
            f"{config_path}: `sources:` precisa ser uma lista não vazia")
    if len(entries) > MAX_SOURCES:
        raise SourceConfigError(
            f"{config_path}: {len(entries)} fontes declaradas, máximo "
            f"{MAX_SOURCES} — o laboratório usa UMA fonte ativa por vez (D1, "
            "ADR-0006); multi-fonte simultânea está fora de escopo")

    sources: List[SourceConfig] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise SourceConfigError(
                f"{config_path}: sources[{index}] precisa ser um mapeamento")
        source = _build_source(dict(entry), index, environ)
        if source.id in seen:
            raise SourceConfigError(
                f"{config_path}: id {source.id!r} duplicado "
                f"(sources[{seen[source.id]}] e sources[{index}])")
        seen[source.id] = index
        sources.append(source)

    default_source = document.get("default_source")
    if default_source is not None:
        default_source = str(default_source).strip()
        if default_source not in seen:
            raise SourceConfigError(
                f"{config_path}: default_source {default_source!r} não existe "
                f"— fontes válidas: {', '.join(sorted(seen))}")
    return SourceList(sources, default_source=default_source)


# -- seleção da fonte ativa --------------------------------------------------
def read_active_source(path: Optional[str] = None) -> str:
    """Id gravado em `data/config/active_source`; `""` se ausente/vazio.

    Arquivo de uma linha (seção 14.1 do plano). Ausente ou vazio **não** é
    erro: cai para o `default_source` do YAML. Id inválido, sim — quem decide
    isso é `resolve_active_source()`.
    """
    candidate = path or DEFAULT_CONTROL_PATH
    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return ""
    return content.strip()


def resolve_active_source(sources: List[SourceConfig],
                          env: Optional[Mapping[str, str]] = None,
                          control_path: Optional[str] = None) -> SourceConfig:
    """Fonte ativa, por precedência: env > arquivo > default_source > 1º enabled.

    Id inexistente ou desabilitado é **erro** com a lista de ids válidos: um
    `active_source` que "não pegou" e caiu no default seria pior que uma falha,
    porque a troca pareceria feita (seção 14.1 do plano).
    """
    environ = os.environ if env is None else env
    by_id: Dict[str, SourceConfig] = {source.id: source for source in sources}
    if not by_id:
        raise SourceConfigError("nenhuma fonte declarada — não há o que ativar")
    valid_ids = ", ".join(sorted(by_id))
    enabled_ids = ", ".join(sorted(s.id for s in sources if s.enabled)) or "—"

    requested = ""
    origin = ""
    env_id = (environ.get(ENV_ACTIVE_SOURCE) or "").strip()
    if env_id:
        requested, origin = env_id, f"variável {ENV_ACTIVE_SOURCE}"
    else:
        file_path = control_path or DEFAULT_CONTROL_PATH
        file_id = read_active_source(file_path)
        if file_id:
            requested, origin = file_id, f"arquivo {file_path}"
        else:
            default_id = getattr(sources, "default_source", None)
            if default_id:
                requested, origin = str(default_id).strip(), "default_source"

    if not requested:
        for source in sources:
            if source.enabled:
                return source
        raise SourceConfigError(
            f"nenhuma fonte habilitada em sources.yaml (ids: {valid_ids})")

    source = by_id.get(requested)
    if source is None:
        raise SourceConfigError(
            f"fonte ativa {requested!r} ({origin}) não existe.\n"
            f"  fontes válidas: {valid_ids}")
    if not source.enabled:
        raise SourceConfigError(
            f"fonte ativa {requested!r} ({origin}) está com enabled: false em "
            "sources.yaml.\n"
            f"  fontes habilitadas: {enabled_ids}")
    return source


# -- codec -> cadeia de depayload/parse --------------------------------------
_DEPAY_FACTORIES: Dict[str, Tuple[str, str]] = {
    "h264": ("rtph264depay", "h264parse"),
    "h265": ("rtph265depay", "h265parse"),
}


def depay_factory(codec: str) -> Tuple[str, str]:
    """`(depayloader, parser)` para o codec da fonte (D5).

    Codec é propriedade da **fonte**, não do container: a câmera publica H.265
    e drone/celular publicam H.264 pelo MediaMTX. Um valor fora de
    `h264`/`h265` é erro de configuração, não um default.
    """
    key = (codec or "").strip().lower()
    factories = _DEPAY_FACTORIES.get(key)
    if factories is None:
        raise SourceConfigError(
            f"codec {codec!r} não suportado — use um de "
            f"{', '.join(sorted(_DEPAY_FACTORIES))} "
            "(o pipeline só decodifica H.264/H.265)")
    return factories
