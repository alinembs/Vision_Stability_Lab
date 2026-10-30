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
import sys
import time
from dataclasses import dataclass
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
    return {
        "id": source.id,
        "codec": source.codec,
        "target_fps": f"{source.target_fps:g}",
        # Nunca a URI crua: `masked_uri` é a única forma que pode ir para log.
        "masked_uri": source.masked_uri(),
        "precheck_uri": precheck_url(source.uri, env),
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
                    now=time.time, sleep=time.sleep) -> WaitOutcome:
    """Espera um `interval_metrics` com `source_id` pedido e `fps_in > 0`.

    É o pós-check do D7: só PASS se a fonte **pedida** está entregando frames de
    verdade. `offset` é o tamanho do JSONL antes da recriação — sem ele o
    primeiro registro lido seria o da fonte anterior, e a troca que não pegou
    passaria (o caso que o revisor apontou). `since`/`now` são injetáveis para
    testar o timeout sem dormir.
    """
    started = now() if since is None else since
    position = offset
    last_source: Optional[str] = None
    last_error: Optional[str] = None
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
            elapsed = round(now() - started, 1)
            return WaitOutcome(True, elapsed, elapsed, source_id, fps_in,
                               last_error, "ok")
        elapsed = now() - started
        if elapsed >= timeout_s:
            break
        sleep(poll_s)
    elapsed = round(now() - started, 1)
    if last_source and last_source != source_id:
        reason = f"source_id_inesperado ({last_source})"
    elif last_error:
        reason = f"startup_sem_metricas ({last_error})"
    elif last_source == source_id:
        reason = "sem_frames (fps_in = 0)"
    else:
        reason = f"sem_metricas em {timeout_s:g}s"
    return WaitOutcome(False, elapsed, None, last_source, None, last_error,
                       reason)


# ---------------------------------------------------------------------------
# Pré-check e rollback
# ---------------------------------------------------------------------------
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

    `probe` é `ok`, `skipped` (`--skip-precheck`, usado no teste de ciclo E6) ou
    `failed`. O histórico de trocas é o que permite responder "quando o drone
    entrou e por quê" sem vasculhar `live.jsonl`.
    """
    from datetime import datetime, timezone

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
            "3 = ACTIVE_SOURCE no ambiente vence o arquivo (nada foi escrito), "
            "4 = pré-check reprovado (nada foi escrito, container intacto).\n\n"
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
                        help=f"segundos esperando o pós-check (default: {DEFAULT_TIMEOUT_S:g})")
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
                                  since=namespace.since)
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
