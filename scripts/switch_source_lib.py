#!/usr/bin/env python3
"""Parte pura do `scripts/switch_source.sh` (E2-T3) — testável sem docker/rede.

O script de troca de fonte tem duas metades:

- **impura** (o `.sh`): fala com `docker`, `docker compose` e a sonda RTSP;
- **pura** (este módulo): decide o que fazer e com o que, a partir de texto —
  argumentos, `deploy/.env`, `deepstream/config/sources.yaml`, o JSONL de
  telemetria.

`scripts/tests/test_switch_source.py` cobre só a metade pura: validação do id
contra o YAML, montagem da URI do pré-check (o host `mediamtx` do compose não
resolve no host), espera da linha `interval_metrics` sobre um JSONL de fixture e
a decisão de rollback. Nenhum teste chama `docker`.

**Prova de qual processo escreveu a métrica.** `interval_metrics` é
append-only: numa troca para a **mesma** fonte (câmera -> câmera) a métrica do
processo anterior tem o `source_id` pedido e aprovaria a troca antes de o
container novo existir. Por isso a espera exige que a amostra seja do processo
novo (`process_boundary` + `sample_is_from_process`): `session_epoch` da
telemetria >= `StartedAt` do container recriado, com tolerância de 2 s. O
`--timeout` também conta a partir daí, não do início do script — o custo fixo
(pré-check + parada do container) é justamente o que não diz nada sobre o
pipeline novo.

Contrato de saída: cada subcomando imprime linhas `chave=valor` em stdout (é o
que o bash lê) e mensagens legíveis em stderr; o exit code diz o que aconteceu:

- `0` ok / `1` falha de operação / `2` uso incorreto / `3` conflito de
  precedência (`ACTIVE_SOURCE` no ambiente do container vence o arquivo).

Credencial: a URI usada pelo pré-check nunca é impressa por este módulo — a
única forma de URI na saída é `masked_uri` (`***@host:porta/path`, DIV-001).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
# `deepstream/sources.py` é puro (sem `gi`/`pyds`): pode ser importado no host.
sys.path.insert(0, str(REPO_ROOT / "deepstream"))

import sources  # noqa: E402  (depois do sys.path)

DEFAULT_ENV_FILE = REPO_ROOT / "deploy" / ".env"
DEFAULT_TELEMETRY = REPO_ROOT / "data" / "logs" / "live.jsonl"
DEFAULT_TIMEOUT_S = 90.0
DEFAULT_POLL_S = 0.5
DEFAULT_LAN_IP = "192.168.59.102"
# Folga entre o `StartedAt` do Docker e o `session_epoch` do pipeline (que é o
# instante do import do módulo, ~3 s depois do start do container). 2 s cobre
# jitter de relógio sem deixar passar amostra de um processo encerrado há
# segundos — os processos do pipeline vivem minutos.
DEFAULT_SESSION_TOLERANCE_S = 2.0

# Exit codes (documentados no `--help` do switch_source.sh).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_PRECEDENCE = 3
EXIT_PRECHECK = 4

# Hosts que só existem dentro da rede do compose (DNS do serviço).
COMPOSE_ONLY_HOSTS = frozenset({"mediamtx"})

# Eventos que explicam uma falha de subida do pipeline.
FAILURE_EVENTS = frozenset({"pipeline_error", "startup_failed"})

# Razões de reprovação da sonda (`scripts/probe/rtsp_read_probe.py`) agrupadas
# no vocabulário do switch. O detalhe cru vai junto, em `detail=`.
NO_MEDIA_PREFIXES = ("no_frames", "no_video_pad", "first_frame_too_late",
                     "pipeline_error", "probe_error")
UNSTABLE_PREFIXES = ("fps_below_min", "frames_below_min", "too_many_gaps")


# ---------------------------------------------------------------------------
# `deploy/.env`
# ---------------------------------------------------------------------------
def parse_env_file(text: str) -> Dict[str, str]:
    """Interpreta um `.env` no formato do compose (subconjunto).

    Aceita `K=V`, `K="V"`, `K='V'`, `export K=V`, comentários (`#`) e linhas em
    branco. Valor sem aspas tem `\\r` removido (arquivo salvo no Windows).
    Linha sem `=` é ignorada — não é papel do switch validar o `.env`.
    """
    values: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def read_env_file(path: os.PathLike | str) -> Dict[str, str]:
    """Lê o `.env`; ausente é `{}` (o switch avisa antes, no pré-requisito)."""
    try:
        return parse_env_file(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def merged_env(env_file: os.PathLike | str,
               environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """`.env` como base, ambiente do shell por cima (como o compose faz)."""
    merged = read_env_file(env_file)
    merged.update(dict(os.environ if environ is None else environ))
    return merged


# ---------------------------------------------------------------------------
# Validação do id e URI do pré-check
# ---------------------------------------------------------------------------
def resolve_source(source_id: str, env: Mapping[str, str],
                   config_path: Optional[str] = None) -> "sources.SourceConfig":
    """`SourceConfig` do id pedido, ou erro com a lista de ids válidos.

    Um id que não existe **não** pode cair em `default_source`: a troca
    pareceria feita e o script daria PASS na fonte errada (seção 14.1 do plano).
    """
    candidates = sources.load_sources(config_path, env=env)
    by_id = {source.id: source for source in candidates}
    if not by_id:
        raise sources.SourceConfigError("nenhuma fonte declarada")
    if source_id not in by_id:
        raise sources.SourceConfigError(
            f"fonte {source_id!r} não existe em "
            f"{config_path or sources.DEFAULT_CONFIG_PATH}.\n"
            f"  fontes válidas: {', '.join(sorted(by_id))}")
    source = by_id[source_id]
    if not source.enabled:
        raise sources.SourceConfigError(
            f"fonte {source_id!r} está com enabled: false em "
            f"{config_path or sources.DEFAULT_CONFIG_PATH}")
    return source


def precheck_url(uri: str, env: Mapping[str, str]) -> str:
    """URI da fonte trocando o host do compose pelo host alcançável do host.

    No YAML, drone e celular usam `mediamtx` (DNS do serviço dentro do compose).
    No host esse nome não resolve, então o pré-check usa `MTX_HOST`, `LAN_IP` ou
    o IP da LAN do laboratório — as portas do MediaMTX são publicadas nele
    (E0-T2). A credencial, se houver, é preservada; nada aqui imprime a URI.
    """
    parts = urlsplit((uri or "").strip())
    host = (parts.hostname or "").strip()
    if host not in COMPOSE_ONLY_HOSTS:
        return uri
    reachable = (env.get("MTX_HOST") or env.get("LAN_IP")
                 or DEFAULT_LAN_IP).strip()
    if not reachable:
        reachable = DEFAULT_LAN_IP
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
    port = parts.port
    authority = f"{userinfo}{reachable}" + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, authority, parts.path, parts.query,
                       parts.fragment))


def source_report(source: "sources.SourceConfig",
                  env: Mapping[str, str]) -> Dict[str, str]:
    """Linhas `chave=valor` que o bash consome em `resolve`."""
    precheck = precheck_url(source.uri, env)
    return {
        "id": source.id,
        "codec": source.codec,
        "target_fps": f"{source.target_fps:g}",
        # Nunca a URI crua: `masked_uri` é a única forma que pode ir para log.
        "masked_uri": source.masked_uri(),
        "precheck_uri": precheck,
        # O log do pré-check citava a URI do YAML (host `mediamtx`, DNS do
        # compose) enquanto a sonda usava o host da LAN: URI diferente da
        # sondada é diagnóstico errado quando o path está mudo. Esta é a URI
        # que a sonda realmente abriu, mascarada (DIV-001).
        "precheck_masked_uri": sources.mask_uri(precheck),
    }


# ---------------------------------------------------------------------------
# Guarda de precedência (`ACTIVE_SOURCE` vence o arquivo)
# ---------------------------------------------------------------------------
def active_source_conflict(container_value: str,
                           env_file_value: str) -> Optional[str]:
    """Mensagem de bloqueio quando `ACTIVE_SOURCE` do ambiente não está vazio.

    `resolve_active_source()` dá precedência ao ambiente sobre
    `data/config/active_source`; escrever o arquivo com `ACTIVE_SOURCE=drone` no
    container seria uma troca que não pega — e o pós-check veria `drone` na
    telemetria, dando PASS na fonte errada. Devolve `None` quando pode seguir.
    """
    container = (container_value or "").strip()
    env_file = (env_file_value or "").strip()
    if container:
        origem = "no container"
        valor = container
    elif env_file:
        origem = f"em {DEFAULT_ENV_FILE}"
        valor = env_file
    else:
        return None
    return (
        f"ACTIVE_SOURCE={valor} está definida {origem} e vence "
        "data/config/active_source.\n"
        "  A troca não pegaria: a fonte continuaria sendo "
        f"{valor!r}, e o pós-check aprovaria a fonte errada.\n"
        "  Remova ACTIVE_SOURCE de deploy/.env (deixe a linha vazia), recrie o "
        "container e repita:\n"
        "    ACTIVE_SOURCE= docker compose --env-file deploy/.env "
        "-f deploy/docker-compose.yml up -d --no-deps vision-app\n"
        "  Ou troque de fonte pela própria variável "
        "(ACTIVE_SOURCE=<id> no deploy/.env), sem este script.")


# ---------------------------------------------------------------------------
# Espera da telemetria
# ---------------------------------------------------------------------------
# Timestamp ISO-8601 truncado até a fração: o pipeline escreve
# `2026-10-30T21:07:23.703+00:00` e o Docker devolve
# `2026-10-30T21:07:20.664208041Z` (nanossegundos, que `strptime` não aceita).
_ISO_TS_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?")
_ISO_OFFSET_RE = re.compile(r"([+-])(\d\d):?(\d\d)(?::?\d\d)?$")


def parse_iso_epoch(value: Any) -> Optional[float]:
    """Epoch (UTC) de um timestamp ISO-8601, ou `None` se não for parseável.

    Cobre os dois formatos que o switch compara: `session_epoch`/`ts` da
    telemetria (UTC com `+00:00`) e `StartedAt` do Docker (UTC com `Z` e
    nanossegundos). Offset numérico (`-03:00`) é respeitado; timestamp sem
    fuso é tratado como UTC, como os dois produtores fazem.
    """
    text = str(value or "").strip()
    match = _ISO_TS_RE.match(text)
    if not match:
        return None
    try:
        base = datetime(*(int(match.group(index)) for index in range(1, 7)),
                        tzinfo=timezone.utc)
        fraction = float("0." + (match.group(7) or "0")[:6])
    except (ValueError, OverflowError):
        return None
    offset_s = 0.0
    offset = _ISO_OFFSET_RE.search(text[match.end():])
    if offset is not None:
        sinal = -1.0 if offset.group(1) == "-" else 1.0
        offset_s = sinal * (int(offset.group(2)) * 3600 + int(offset.group(3)) * 60)
    try:
        return base.timestamp() + fraction - offset_s
    except (ValueError, OverflowError, OSError):
        return None


def process_boundary(started_at: Optional[str] = None,
                     recreated_at: Optional[float] = None) -> Optional[float]:
    """Instante a partir do qual uma amostra é do processo **novo**.

    Preferência ao `StartedAt` do container recriado: é o dado do Docker, não
    depende do relógio de quem chamou. `recreated_at` (epoch medido pelo script
    logo depois do `up -d`) é o fallback quando o `docker inspect` falha.

    `None` significa "não há como provar qual processo escreveu": a espera cai
    no comportamento antigo (só `offset`), nunca aprova por ausência de prova.
    """
    boundary = parse_iso_epoch(started_at)
    if boundary is not None:
        return boundary
    return recreated_at


def sample_is_from_process(record: Mapping[str, Any], boundary_epoch: Optional[float],
                           tolerance_s: float = DEFAULT_SESSION_TOLERANCE_S) -> bool:
    """True só quando a amostra foi escrita pelo processo que nasceu em `boundary_epoch`.

    O critério é o `session_epoch` (E2-T2: único por processo do pipeline e
    igual em todas as linhas dele). Sem `session_epoch` — JSONL antigo, ou
    linha de outra ferramenta — o `ts` da própria amostra serve de prova; sem
    nenhum dos dois a amostra é recusada, porque aprovar sem prova é
    exatamente o "PASS na fonte errada" que este módulo existe para impedir.
    """
    if boundary_epoch is None:
        return True
    for field in ("session_epoch", "ts"):
        epoch = parse_iso_epoch(record.get(field))
        if epoch is None:
            continue
        return epoch >= boundary_epoch - tolerance_s
    return False


@dataclass(frozen=True)
class WaitOutcome:
    """Resultado da espera por uma linha `interval_metrics` da fonte pedida."""

    ok: bool
    elapsed_s: float
    first_frame_in: Optional[float]
    source_id: Optional[str]
    fps_in: Optional[float]
    last_error: Optional[str]
    reason: str
    # `session_epoch` da amostra aceita: é o que prova, na saída do switch,
    # que a métrica é do processo novo (comparável com o `StartedAt`).
    session_epoch: Optional[str] = None

    def as_lines(self) -> Dict[str, str]:
        return {
            "ok": "1" if self.ok else "0",
            "elapsed_s": f"{self.elapsed_s:.1f}",
            "first_frame_in": "" if self.first_frame_in is None
            else f"{self.first_frame_in:.1f}",
            "source_id": self.source_id or "",
            "fps_in": "" if self.fps_in is None else f"{self.fps_in:g}",
            "reason": self.reason,
            "last_error": (self.last_error or "").replace("\n", " "),
            "session_epoch": self.session_epoch or "",
        }


def read_new_records(path: os.PathLike | str,
                     offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """Registros JSONL a partir de `offset`; devolve também o novo offset.

    Arquivo ausente devolve `([], offset)`. Offset maior que o arquivo (log
    rotacionado/truncado entre a medição e a leitura) recomeça do zero — perder
    histórico antigo é melhor que esperar 90 s por uma linha que já passou.
    Linha parcial (o pipeline ainda está escrevendo) é ignorada e relida na
    próxima volta: só o que termina em `\\n` é consumido.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if offset > size:
                offset = 0
            handle.seek(offset)
            chunk = handle.read()
    except FileNotFoundError:
        return [], offset
    except OSError:
        return [], offset
    consumed = chunk.rfind(b"\n")
    if consumed == -1:
        return [], offset
    payload = chunk[: consumed + 1]
    records: List[Dict[str, Any]] = []
    for line in payload.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, offset + len(payload)


def _failure_detail(record: Mapping[str, Any]) -> str:
    """Texto curto de um evento de falha, sem despejar a linha inteira."""
    for field in ("error", "message", "detail", "reason"):
        value = record.get(field)
        if value:
            return str(value).replace("\n", " ")[:300]
    return json.dumps(record, ensure_ascii=False)[:300]


def wait_for_source(telemetry_path: os.PathLike | str, source_id: str,
                    timeout_s: float = DEFAULT_TIMEOUT_S, *,
                    offset: int = 0, poll_s: float = DEFAULT_POLL_S,
                    since: Optional[float] = None,
                    started_at: Optional[str] = None,
                    recreated_at: Optional[float] = None,
                    tolerance_s: float = DEFAULT_SESSION_TOLERANCE_S,
                    now=time.time, sleep=time.sleep) -> WaitOutcome:
    """Espera um `interval_metrics` do processo novo com a fonte pedida e fps > 0.

    É o pós-check do D7: só PASS se a fonte **pedida** está entregando frames de
    verdade, **escritos pelo processo que a recriação subiu**. Três guardas, cada
    uma cobrindo um PASS falso distinto:

    - `offset` (tamanho do JSONL antes da recriação): a linha da fonte anterior
      não vale, mesmo com `source_id` igual (troca câmera -> câmera);
    - `started_at`/`recreated_at` (`process_boundary`): a linha tem de ser do
      processo novo — o `offset` sozinho não basta se o processo antigo ainda
      escreveu depois dele, durante o `docker stop`;
    - `fps_in > 0`: `source_id` certo com path mudo não é troca bem-sucedida.

    `since` (epoch do início da contagem) é sobreposto pelo boundary: o
    orçamento do `--timeout` conta da recriação do container, não do início do
    script (pré-check + parada não dizem nada sobre o pipeline novo).
    `now`/`sleep` são injetáveis para testar o timeout sem dormir.
    """
    boundary = process_boundary(started_at, recreated_at)
    if since is None:
        started = now() if boundary is None else boundary
    elif boundary is None:
        started = since
    else:
        started = max(since, boundary)
    position = offset
    last_source: Optional[str] = None
    last_error: Optional[str] = None
    # Amostra da fonte certa recusada por ser do processo anterior: é o
    # diagnóstico do "PASS na fonte errada" que não aconteceu.
    stale_epoch: Optional[str] = None
    while True:
        records, position = read_new_records(telemetry_path, position)
        for record in records:
            event = record.get("event")
            if event in FAILURE_EVENTS:
                last_error = f"{event}: {_failure_detail(record)}"
                continue
            if event != "interval_metrics":
                continue
            last_source = record.get("source_id") or last_source
            if record.get("source_id") != source_id:
                continue
            try:
                fps_in = float(record.get("fps_in") or 0)
            except (TypeError, ValueError):
                continue
            if fps_in <= 0:
                continue
            if not sample_is_from_process(record, boundary, tolerance_s):
                stale_epoch = str(record.get("session_epoch")
                                  or record.get("ts") or "?")
                continue
            elapsed = round(max(0.0, now() - started), 1)
            return WaitOutcome(True, elapsed, elapsed, source_id, fps_in,
                               last_error, "ok",
                               record.get("session_epoch"))
        elapsed = now() - started
        if elapsed >= timeout_s:
            break
        sleep(poll_s)
    elapsed = round(max(0.0, now() - started), 1)
    if last_source and last_source != source_id:
        reason = f"source_id_inesperado ({last_source})"
    elif last_error:
        reason = f"startup_sem_metricas ({last_error})"
    elif stale_epoch is not None:
        reason = f"sem_amostra_do_processo_novo (session_epoch={stale_epoch})"
    elif last_source == source_id:
        reason = "sem_frames (fps_in = 0)"
    else:
        reason = f"sem_metricas em {timeout_s:g}s"
    return WaitOutcome(False, elapsed, None, last_source, None, last_error,
                       reason)


# ---------------------------------------------------------------------------
# Pré-check e rollback
# ---------------------------------------------------------------------------
def marker_expired(text: Optional[str], now: float,
                   mtime: Optional[float] = None,
                   ttl_s: float = 150.0) -> bool:
    """`True` se o marcador `switch_in_progress` pode ser descartado.

    O marcador é o mutex entre os dois caminhos de troca (script e botão do
    dashboard). Ele vale enquanto `expires_at` estiver no futuro; sem validade
    legível (vazio, corrompido, ferramenta antiga) vale por `mtime + ttl_s`.
    Marcador **nunca** vale para sempre: um `kill -9` no meio da troca deixava
    o arquivo e nenhuma execução futura trocava fonte até alguém apagar à mão.

    Args:
        text: conteúdo do arquivo (`None` se não existe).
        now: instante atual (`time.time()`).
        mtime: mtime do arquivo, usado quando não há `expires_at` legível.
        ttl_s: validade do marcador sem data (`timeout + 60` por convenção).
    """
    if text is None:
        return True                       # nada a descartar
    raw = text.strip()
    expires = None
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            expires = data.get("expires_at")
    if expires:
        try:
            deadline = datetime.strptime(
                str(expires), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return deadline.timestamp() <= now
        except ValueError:
            pass                          # data inválida: cai no mtime
    if mtime is None:
        return False                      # sem data e sem mtime: conservador
    return (now - mtime) > ttl_s


def probe_failure_reason(report: Mapping[str, Any]) -> str:
    """Traduz a reprovação da sonda no vocabulário do switch.

    `no_frames_on_path` = a fonte não está entregando vídeo agora (path mudo,
    RTSP 404, codec não suportado, primeiro frame atrasado). `unstable_path` =
    entrega, mas fora do gate de estabilidade. O motivo cru da sonda vai no
    `detail` da mensagem — sem ele não se diagnostica nada.
    """
    raw = str(report.get("reason") or "probe_failed")
    if raw.startswith(NO_MEDIA_PREFIXES):
        return "no_frames_on_path"
    if raw.startswith(UNSTABLE_PREFIXES):
        return "unstable_path"
    return "probe_failed"


@dataclass(frozen=True)
class RollbackDecision:
    """O que fazer quando o pós-check não confirma a troca."""

    action: str                 # "keep" | "rollback"
    restore_to: Optional[str]   # valor anterior; None = manter o atual
    reason: str

    def as_lines(self) -> Dict[str, str]:
        return {"action": self.action,
                "restore_to": self.restore_to or "",
                "reason": self.reason}


def decide_rollback(outcome: WaitOutcome, expected_id: str,
                    previous_value: Optional[str]) -> RollbackDecision:
    """Mantém a troca só com pós-check confirmado; senão volta ao valor anterior.

    `previous_value` vazio/`None` significa "não havia arquivo de controle":
    o rollback apaga o arquivo, e a fonte volta a ser o `default_source` do
    `sources.yaml` — escrever uma linha vazia teria o mesmo efeito, mas apagar
    é o estado exato de antes.
    """
    if outcome.ok and (outcome.source_id or "") == expected_id:
        return RollbackDecision("keep", None, "ok")
    return RollbackDecision("rollback", previous_value or "",
                            outcome.reason or "pos_check_falhou")


def switch_record(*, source_id: str, previous: str, result: str, reason: str,
                  probe: str, timestamp: Optional[str] = None,
                  extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Linha de `data/logs/switch.jsonl` (uma por execução, PASS ou FAIL).

    `probe` é `ok`, `skipped` (`--skip-precheck`, usado no teste de ciclo E6),
    `blocked` (o guard de precedência abortou antes de qualquer sonda) ou
    `failed`. O histórico de trocas é o que permite responder "quando o drone
    entrou e por quê" sem vasculhar `live.jsonl`. Uma linha por **execução**:
    PASS, FAIL do pós-check e também a tentativa bloqueada no guard de
    precedência (`reason=active_source_no_ambiente`), que antes abortava sem
    deixar rastro em `switch.jsonl`.
    """
    record: Dict[str, Any] = {
        "ts": timestamp or datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"),
        "event": "switch_source",
        "from": previous.strip() if previous else "",
        "to": source_id,
        "probe": probe,
        "result": result,
        "reason": reason,
    }
    if extra:
        record.update(extra)
    return record


# ---------------------------------------------------------------------------
# Argumentos do switch
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SwitchArgs:
    """`switch_source.sh <id> [--skip-precheck] [--timeout S]`."""

    source_id: str
    skip_precheck: bool
    timeout_s: float
    help_requested: bool = False

    def as_lines(self) -> Dict[str, str]:
        if self.help_requested:
            return {"help": "1"}
        return {"id": self.source_id,
                "skip_precheck": "1" if self.skip_precheck else "0",
                "timeout": f"{self.timeout_s:g}"}


def build_arg_parser(prog: str = "switch_source.sh") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Troca a fonte ativa do Vision Stability Lab (E2-T3): valida o id, "
            "faz o pré-check RTSP (D7), escreve data/config/active_source, "
            "recria o container do pipeline e confere a telemetria. Se o "
            "pós-check não confirmar, volta a fonte anterior."),
        epilog=(
            "Exemplo:\n"
            "  ./scripts/switch_source.sh drone            # exige o publisher no ar\n"
            "  ./scripts/switch_source.sh camera --timeout 120\n"
            "  ./scripts/switch_source.sh drone --skip-precheck   # só no teste de ciclo E6\n\n"
            "Exit codes: 0 = PASS (pós-check confirmou a fonte), "
            "1 = FAIL (pós-check não confirmou; rollback feito), "
            "2 = uso incorreto ou id inválido, "
            "3 = bloqueado sem escrever: ACTIVE_SOURCE no ambiente vence o arquivo "
            "ou outra troca está em andamento (marcador switch_in_progress), "
            "4 = pré-check reprovado (nada foi escrito, container intacto).\n\n"
            "Orçamento do --timeout: conta a partir da recriação do container "
            "(StartedAt do processo novo), não do início do script. Ficam FORA "
            "da conta o pré-check (~5 s de sonda) e a parada/recriação do "
            "container (~1 s). O primeiro interval_metrics chega 33-35 s depois "
            "da recriação (import do DeepStream + METRICS_INTERVAL_S=30 s): "
            "--timeout 40 cobre o caminho feliz, e abaixo disso a troca falha "
            "mesmo funcionando.\n\n"
            "Pós-check: a métrica aprovada tem de vir do processo NOVO "
            "(session_epoch >= StartedAt do container recriado). Sem isso uma "
            "troca para a mesma fonte já ativa passaria com a métrica do "
            "processo anterior.\n\n"
            "Pré-requisito: a fonte precisa estar publicando. Drone/celular usam "
            "'mediamtx' no sources.yaml (DNS do compose); no host o pré-check "
            "troca por MTX_HOST/LAN_IP.\n\n"
            "Credencial: nenhuma URI sai na saída — host, porta e path sim "
            "(`***@host:porta/path`, DIV-001)."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("source_id", nargs="?", help="id da fonte (camera, drone, phone)")
    parser.add_argument("--skip-precheck", action="store_true",
                        help="não roda a sonda RTSP antes de trocar (registra precheck_skipped)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="segundos esperando o pós-check, contados a partir "
                             "da recriação do container — não do início do "
                             f"script (default: {DEFAULT_TIMEOUT_S:g}; o "
                             "caminho feliz precisa de ~35 s)")
    parser.add_argument("-h", "--help", action="store_true",
                        help="esta ajuda")
    return parser


def parse_args(argv: Sequence[str]) -> SwitchArgs:
    """Interpreta os argumentos do switch (puro, sem tocar no sistema)."""
    parser = build_arg_parser()
    if any(arg in ("-h", "--help") for arg in argv):
        return SwitchArgs("", False, DEFAULT_TIMEOUT_S, help_requested=True)
    if not argv:
        parser.error("informe o id da fonte (ex.: camera, drone, phone) "
                     "ou --help")
    namespace = parser.parse_args(list(argv))
    source_id = (namespace.source_id or "").strip()
    if not source_id:
        parser.error("informe o id da fonte (ex.: camera, drone, phone)")
    if not sources.SOURCE_ID_RE.match(source_id):
        parser.error(
            f"id {source_id!r} inválido: use apenas [a-z0-9_-] "
            "(o id vira argumento de script e nome de arquivo de estado)")
    if namespace.timeout <= 0:
        parser.error("--timeout precisa ser maior que zero")
    return SwitchArgs(source_id, namespace.skip_precheck, float(namespace.timeout))


# ---------------------------------------------------------------------------
# CLI (lida pelo switch_source.sh)
# ---------------------------------------------------------------------------
def _emit(lines: Mapping[str, str]) -> None:
    for key, value in lines.items():
        print(f"{key}={value}")


def _read_stdin_json() -> Mapping[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switch_source_lib.py",
        description="Lógica pura do scripts/switch_source.sh (E2-T3).")
    sub = parser.add_subparsers(dest="command", required=True)

    args_cmd = sub.add_parser("args", add_help=False,
                              help="valida os argumentos do switch (via main())")

    resolve_cmd = sub.add_parser("resolve", help="id -> URI do pré-check")
    resolve_cmd.add_argument("--id", required=True)
    resolve_cmd.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    resolve_cmd.add_argument("--config", default=None)

    guard_cmd = sub.add_parser("guard", help="checa precedência de ACTIVE_SOURCE")
    guard_cmd.add_argument("--container-value", default="")
    guard_cmd.add_argument("--env-value", default="")

    wait_cmd = sub.add_parser("wait", help="espera o interval_metrics da fonte")
    wait_cmd.add_argument("--source-id", required=True)
    wait_cmd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    wait_cmd.add_argument("--offset", type=int, default=0)
    wait_cmd.add_argument("--since", type=float, default=None)
    wait_cmd.add_argument("--started-at", default=None,
                          help="StartedAt do container recriado (prova de "
                               "processo novo e início do orçamento)")
    wait_cmd.add_argument("--recreated-at", type=float, default=None,
                          help="epoch do script logo após o up -d (fallback)")
    wait_cmd.add_argument("--telemetry", default=str(DEFAULT_TELEMETRY))

    probe_cmd = sub.add_parser("probe-reason",
                               help="motivo da reprovação da sonda (JSON no stdin)")
    probe_cmd.add_argument("--report", default=None,
                           help="JSON inline (default: stdin)")

    rollback_cmd = sub.add_parser("rollback", help="decisão de rollback")
    rollback_cmd.add_argument("--ok", default="0")
    rollback_cmd.add_argument("--reason", default="")
    rollback_cmd.add_argument("--previous", default="")
    rollback_cmd.add_argument("--expected", default="",
                              help="id pedido na troca")
    rollback_cmd.add_argument("--observed", default="",
                              help="source_id que apareceu na telemetria")

    env_cmd = sub.add_parser("active-source-env",
                             help="ACTIVE_SOURCE de deploy/.env")
    env_cmd.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))

    marker_cmd = sub.add_parser(
        "marker-expired",
        help="o marcador switch_in_progress pode ser descartado? (exit 0 = sim)")
    marker_cmd.add_argument("--file", required=True)
    marker_cmd.add_argument("--ttl", default="150")

    record_cmd = sub.add_parser("switch-record",
                                help="linha JSON de data/logs/switch.jsonl")
    record_cmd.add_argument("--to", required=True)
    record_cmd.add_argument("--previous", default="")
    record_cmd.add_argument("--result", required=True)
    record_cmd.add_argument("--reason", default="")
    record_cmd.add_argument("--probe", default="ok")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Despacha o subcomando e devolve o exit code do contrato."""
    arguments = list(sys.argv[1:] if argv is None else argv)

    # `args` é tratado antes do argparse: os argumentos do switch (`--timeout`,
    # `--skip-precheck`, `--help`) não são opções deste módulo, e um `--help`
    # aqui é o do `switch_source.sh` (usage diferente).
    if arguments and arguments[0] == "args":
        switch_args = parse_args([a for a in arguments[1:] if a != "--"])
        if switch_args.help_requested:
            build_arg_parser().print_help()
            print()
        _emit(switch_args.as_lines())
        return EXIT_OK

    parser = build_cli_parser()
    namespace = parser.parse_args(arguments)

    if namespace.command == "resolve":
        env = merged_env(namespace.env_file)
        try:
            source = resolve_source(namespace.id, env, namespace.config)
        except sources.SourceConfigError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return EXIT_USAGE
        _emit(source_report(source, env))
        return EXIT_OK

    if namespace.command == "guard":
        conflict = active_source_conflict(namespace.container_value,
                                          namespace.env_value)
        if conflict:
            print(f"ERRO: {conflict}", file=sys.stderr)
            _emit({"blocked": "1"})
            return EXIT_PRECEDENCE
        _emit({"blocked": "0"})
        return EXIT_OK

    if namespace.command == "wait":
        outcome = wait_for_source(namespace.telemetry, namespace.source_id,
                                  namespace.timeout, offset=namespace.offset,
                                  since=namespace.since,
                                  started_at=namespace.started_at,
                                  recreated_at=namespace.recreated_at)
        _emit(outcome.as_lines())
        return EXIT_OK if outcome.ok else EXIT_FAIL

    if namespace.command == "probe-reason":
        report = (json.loads(namespace.report) if namespace.report
                  else _read_stdin_json())
        if not isinstance(report, dict):
            report = {}
        _emit({"result": str(report.get("result") or "FAIL"),
               "reason": probe_failure_reason(report),
               "detail": str(report.get("reason") or "")[:300],
               "frames": str(report.get("frames") or 0),
               "fps": str(report.get("fps") or 0)})
        return EXIT_OK

    if namespace.command == "rollback":
        ok = (namespace.ok or "").strip() in ("1", "true", "ok")
        outcome = WaitOutcome(
            ok=ok, elapsed_s=0.0, first_frame_in=None,
            source_id=(namespace.observed or namespace.expected or None),
            fps_in=None, last_error=None,
            reason=namespace.reason or "pos_check_falhou")
        _emit(decide_rollback(outcome, namespace.expected,
                              namespace.previous).as_lines())
        return EXIT_OK

    if namespace.command == "active-source-env":
        _emit({"value": read_env_file(namespace.env_file).get("ACTIVE_SOURCE", "")})
        return EXIT_OK

    if namespace.command == "marker-expired":
        import time as _time
        path = pathlib.Path(namespace.file)
        try:
            text = path.read_text(encoding="utf-8")
            mtime = path.stat().st_mtime
        except OSError:
            text, mtime = None, None
        # exit 0 = pode descartar (órfão); exit 1 = em curso, não invadir
        return EXIT_OK if marker_expired(text, _time.time(), mtime,
                                         float(namespace.ttl)) else 1

    if namespace.command == "switch-record":
        print(json.dumps(
            switch_record(source_id=namespace.to, previous=namespace.previous,
                          result=namespace.result, reason=namespace.reason,
                          probe=namespace.probe),
            ensure_ascii=False))
        return EXIT_OK

    parser.error(f"subcomando desconhecido: {namespace.command}")
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
