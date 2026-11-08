"""Troca da fonte ativa pedida pelo dashboard (E4-T3).

A troca continua sendo a de D4 (ADR-0006): escreve `data/config/active_source`
e **reinicia o processo** do pipeline — nada de reconfiguração em runtime (A3
segue descartada). O que muda é quem aperta o botão: o operador, pela tela.

Para não criar um segundo caminho de troca, **todas as decisões** vêm do
`scripts/switch_source_lib.py`, o mesmo módulo puro que o
`scripts/switch_source.sh` usa: validação do id contra o `sources.yaml`, guarda
de precedência (`ACTIVE_SOURCE` vence o arquivo), espera da `interval_metrics`
do processo novo (`process_boundary`) e decisão de rollback. A diferença está
só na metade "impura":

- `switch_source.sh`: `docker compose --force-recreate` + sonda no **host**;
- aqui: `restart` do container (equivalente para reler o arquivo, que chega por
  volume) + sonda **dentro do `vision-app`** (`scripts/probe/source_probe.py`
  via `docker exec`), onde o ambiente é o do pipeline e não o `deploy/.env`.

Contrato de saída é o mesmo do script: uma linha em `data/logs/switch.jsonl`
por execução (PASS, FAIL ou bloqueio), marcador `switch_in_progress` enquanto
roda, e `active_source` só é tocado depois do pré-check aprovado (D7).

Este módulo não fala HTTP: quem expõe rota é o `app.py`, que também decide se a
feature está habilitada (token). Testável sem Docker: `Switcher` recebe um
`DockerRuntime` ou um dublê.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

REPO_ROOT = Path(os.environ.get(
    "VSL_REPO_ROOT", str(Path(__file__).resolve().parents[2])))

# `switch_source_lib` e `sources.py` são puros (sem gi/pyds): o dashboard pode
# importá-los no host e no container, desde que estejam montados.
for _extra in (REPO_ROOT / "scripts", REPO_ROOT / "deepstream"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

APP_CONTAINER = os.environ.get("APP_CONTAINER", "vision-stability-app")
PROBE_PATH = os.environ.get("SWITCH_PROBE", "/app/scripts/probe/source_probe.py")
CONFIG_DIR = Path(os.environ.get("SWITCH_CONFIG_DIR", REPO_ROOT / "data" / "config"))
TELEMETRY_FILE = Path(os.environ.get(
    "TELEMETRY_FILE", REPO_ROOT / "data" / "logs" / "live.jsonl"))
SWITCH_LOG = Path(os.environ.get(
    "SWITCH_LOG", REPO_ROOT / "data" / "logs" / "switch.jsonl"))
TIMEOUT_S = float(os.environ.get("SWITCH_TIMEOUT_S", "90"))
PRECHECK_SECONDS = os.environ.get("PRECHECK_SECONDS", "5")
# Teto da sonda de pré-check. Sem ele, uma sonda pendurada (a classe de falha
# deste projeto: `rtspclientsink`/teardown preso) travava a thread da troca e
# todo `POST` passava a responder 409 até reiniciar o dashboard.
PRECHECK_TIMEOUT_S = float(os.environ.get("PRECHECK_TIMEOUT_S", "20"))
MARKER_NAME = "switch_in_progress"

_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

logger = logging.getLogger("dashboard.switch")


class SwitchError(Exception):
    """Erro de uso da API de troca (vira HTTP no `app.py`)."""

    status_code = 400


class SwitchDisabled(SwitchError):
    status_code = 503


class SwitchBusy(SwitchError):
    status_code = 409


class SwitchRejected(SwitchError):
    status_code = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _lib():
    """`switch_source_lib` importado sob demanda (o dashboard pode não tê-lo)."""
    import switch_source_lib  # noqa: WPS433 (import tardio de propósito)
    return switch_source_lib


def lib_available() -> bool:
    """True quando `scripts/` e `deepstream/` estão montados no dashboard."""
    try:
        _lib().resolve_source  # atributo usado como prova de import real
    except Exception:
        return False
    return (REPO_ROOT / "scripts" / "switch_source_lib.py").is_file()


@dataclass
class ProbeResult:
    ok: bool
    reason: str
    masked_uri: str = ""


class SwitchRuntime(Protocol):
    """O que o `Switcher` precisa do mundo (Docker, nos testes um dublê)."""

    def container_env(self, name: str) -> Dict[str, str]: ...
    def started_at(self, name: str) -> Optional[str]: ...
    def restart(self, name: str) -> None: ...
    def probe(self, name: str, source_id: str) -> ProbeResult: ...


class DockerRuntime:
    """Adaptador mínimo do Docker SDK, sem CLI nem `docker compose`."""

    def __init__(self, client: Any = None, probe_path: str = PROBE_PATH,
                 probe_seconds: float = 5.0,
                 probe_timeout_s: float = PRECHECK_TIMEOUT_S) -> None:
        if client is None:
            import docker  # import tardio: só quem troca de fonte precisa do SDK
            client = docker.from_env()
        self.client = client
        self.probe_path = probe_path
        self.probe_seconds = probe_seconds
        self.probe_timeout_s = probe_timeout_s

    def _container(self, name: str) -> Any:
        return self.client.containers.get(name)

    def container_env(self, name: str) -> Dict[str, str]:
        raw = self._container(name).attrs.get("Config", {}).get("Env") or []
        env: Dict[str, str] = {}
        for item in raw:
            key, _, value = str(item).partition("=")
            env[key] = value
        return env

    def started_at(self, name: str) -> Optional[str]:
        return self._container(name).attrs.get("State", {}).get("StartedAt")

    def restart(self, name: str) -> None:
        self._container(name).restart(timeout=10)

    def probe(self, name: str, source_id: str) -> ProbeResult:
        """Roda a sonda com teto de tempo (`timeout` dentro do container).

        `timeout` é do coreutils da imagem; se não existir (exit 127), repete
        sem ele em vez de falhar a troca por falta de um utilitário.
        """
        base = ["python3", self.probe_path, "--source", source_id,
                "--seconds", f"{self.probe_seconds:g}"]
        container = self._container(name)
        # `-k 5`: depois do SIGTERM, SIGKILL em 5 s. Sem isso, um processo
        # preso dentro de uma chamada C que não despacha o handler (o caso do
        # INC-007: `change_state` bloqueado) faz o `timeout` esperar para
        # sempre e o teto não vale nada.
        result = container.exec_run(
            ["timeout", "-k", "5", f"{self.probe_timeout_s:g}"] + base,
            demux=False)
        if result.exit_code == 127:      # sem `timeout` na imagem
            logger.warning(
                "`timeout` ausente no container %s: sonda rodando SEM teto "
                "(instale coreutils ou ajuste a imagem)", name)
            result = container.exec_run(base, demux=False)
        output = result.output or b""
        text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
        if result.exit_code in (124, 137):   # 124 = teto; 137 = SIGKILL do -k
            return ProbeResult(False, "timeout", _field(text, "masked_uri") or "")
        reason = _field(text, "reason") or ("ok" if result.exit_code == 0 else "probe_failed")
        return ProbeResult(result.exit_code == 0, reason,
                           _field(text, "masked_uri") or "")


def _field(text: str, key: str) -> Optional[str]:
    match = re.search(rf"(?:^|[\r\n]){re.escape(key)}=([^\r\n]*)", text)
    return match.group(1).strip() if match else None


@dataclass
class SwitchStatus:
    """Estado visível da última (ou atual) troca pedida pela tela."""

    state: str = "idle"          # idle | running | done | failed
    step: str = ""
    source_id: Optional[str] = None
    previous: Optional[str] = None
    reason: Optional[str] = None
    message: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Switcher:
    """Executa uma troca por vez, em thread própria, com o mesmo contrato do .sh."""

    def __init__(self, runtime: SwitchRuntime, *,
                 app_container: str = APP_CONTAINER,
                 config_dir: Path = CONFIG_DIR,
                 telemetry_file: Path = TELEMETRY_FILE,
                 switch_log: Path = SWITCH_LOG,
                 timeout_s: float = TIMEOUT_S,
                 precheck_seconds: str = PRECHECK_SECONDS,
                 precheck_timeout_s: float = PRECHECK_TIMEOUT_S,
                 wait_for_source=None) -> None:
        self.runtime = runtime
        self.app_container = app_container
        self.config_dir = Path(config_dir)
        self.telemetry_file = Path(telemetry_file)
        self.switch_log = Path(switch_log)
        self.timeout_s = timeout_s
        self.precheck_seconds = precheck_seconds
        self.precheck_timeout_s = precheck_timeout_s
        self._wait_for_source = wait_for_source
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._marker_owner: str = ""
        self.status = SwitchStatus()

    # -- API ---------------------------------------------------------------
    def start(self, source_id: str) -> SwitchStatus:
        """Dispara a troca e devolve imediatamente o estado inicial.

        Recusa com `SwitchBusy` (HTTP 409) quando **outro processo** (o
        `switch_source.sh`) já está trocando: o marcador
        `data/config/switch_in_progress` é o mutex entre os dois caminhos,
        não só o lock em memória.
        """
        source_id = (source_id or "").strip()
        if not _SOURCE_ID_RE.match(source_id):
            raise SwitchRejected(
                f"id inválido: {source_id!r} (use o id de sources.yaml)")
        foreign = self._marker_active()
        if foreign is not None:
            raise SwitchBusy(self._busy_message(foreign))
        if not self._lock.acquire(blocking=False):
            raise SwitchBusy("já existe uma troca de fonte em andamento")
        foreign = self._marker_active()   # confere de novo já com o lock
        if foreign is not None:
            self._lock.release()
            raise SwitchBusy(self._busy_message(foreign))
        self.status = SwitchStatus(
            state="running", step="validando", source_id=source_id,
            started_at=_now_iso(), message="validando a fonte")
        self._thread = threading.Thread(
            target=self._run, args=(source_id,), name="switch-source", daemon=True)
        self._thread.start()
        return self.status

    @staticmethod
    def _busy_message(marker: Dict[str, Any]) -> str:
        origin = marker.get("origin") or "script"
        target = marker.get("to") or "?"
        return (f"outra troca de fonte em andamento ({origin} → {target}); "
                "aguarde terminar")

    def foreign_marker(self) -> Optional[Dict[str, Any]]:
        """Marcador válido de outro processo (o `GET` informa o operador)."""
        if self._lock.locked():
            return None
        return self._marker_active()

    def wait(self, timeout: Optional[float] = None) -> SwitchStatus:
        """Espera a thread terminar (usado nos testes e no encerramento)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status

    # -- execução ----------------------------------------------------------
    def _run(self, source_id: str) -> None:
        # `_lib()` fica **dentro** do try: um erro de import (mount trocado,
        # sintaxe quebrada no lib) tem de liberar o lock e auditar, não deixar
        # o estado em `running` com 409 eterno.
        lib = None
        try:
            lib = _lib()
            env = self.runtime.container_env(self.app_container)
            try:
                lib.resolve_source(source_id, dict(env), None)
            except Exception as exc:  # SourceConfigError: id fora do YAML
                self._fail(lib, "id_invalido", str(exc), probe="blocked")
                return

            previous = self._previous_value()
            self.status.previous = previous
            conflict = lib.active_source_conflict(env.get("ACTIVE_SOURCE", ""), "")
            if conflict:
                self._fail(lib, "active_source_no_ambiente", conflict,
                           probe="blocked")
                return

            self._step("precheck", f"lendo a fonte por {self.precheck_seconds}s")
            probe = self.runtime.probe(self.app_container, source_id)
            if not probe.ok:
                # Veredito no vocabulário do switch (`no_frames_on_path` etc.),
                # igual ao script; o motivo cru da sonda vai na mensagem.
                if probe.reason == "timeout":
                    reason = "precheck:timeout"
                else:
                    try:
                        reason = ("precheck:"
                                  + lib.probe_failure_reason({"reason": probe.reason}))
                    except Exception:  # noqa: BLE001 — lib antiga sem a função
                        reason = f"precheck:{probe.reason}"
                self._fail(lib, reason,
                           "pré-check reprovado: a fonte não está entregando "
                           f"vídeo agora ({probe.reason}); nada foi escrito",
                           probe="failed")
                return

            self._step("write", "gravando a fonte ativa")
            offset = self.telemetry_file.stat().st_size \
                if self.telemetry_file.exists() else 0
            self._claim_marker(source_id)
            self._write_control(source_id)

            self._step("restart", "reiniciando o pipeline (30–90 s)")
            try:
                self.runtime.restart(self.app_container)
            except Exception as exc:  # noqa: BLE001 — escrita já aconteceu
                # O arquivo já aponta para a fonte nova e o processo pode não
                # ter reiniciado: sem compensar, o próximo reinício (inclusive
                # do watchdog) subiria numa fonte que nunca passou pelo
                # pós-check. Restaura a anterior e audita sempre.
                logger.exception("restart da troca falhou; restaurando")
                self._restore_control(previous)
                detalhe = ""
                try:
                    self.runtime.restart(self.app_container)
                except Exception as exc2:  # noqa: BLE001
                    logger.exception("restart de compensação também falhou")
                    detalhe = (f"; o restart de compensação também falhou "
                               f"({exc2}) — o processo pode estar na fonte "
                               "anterior, mas o próximo boot já lê a restaurada")
                self._audit(lib, "FAIL", "restart_falhou", probe="ok")
                self._set_failed(
                    "restart_falhou",
                    "o restart do pipeline falhou depois da escrita; "
                    f"a fonte anterior ({previous or 'default_source'}) foi "
                    f"restaurada em active_source ({exc}){detalhe}")
                return
            started_at = self._safe_started_at()

            self._step("wait", f"aguardando telemetria de {source_id}")
            waiter = self._wait_for_source or lib.wait_for_source
            outcome = waiter(
                self.telemetry_file, source_id, self.timeout_s,
                offset=offset, started_at=started_at, recreated_at=time.time())
            decision = lib.decide_rollback(outcome, source_id, previous)

            if decision.action == "keep":
                self._audit(lib, "PASS", "ok", probe="ok")
                self.status.state = "done"
                self.status.step = "concluída"
                self.status.reason = None
                self.status.message = (
                    f"fonte ativa agora é {source_id} "
                    f"({outcome.fps_in:g} fps de entrada na confirmação)"
                    if outcome.fps_in is not None
                    else f"fonte ativa agora é {source_id}")
                self.status.finished_at = _now_iso()
                return

            self._step("rollback", f"pós-check falhou ({decision.reason}); "
                                   "restaurando a fonte anterior")
            self._restore_control(decision.restore_to)
            try:
                self.runtime.restart(self.app_container)
            except Exception as exc:  # noqa: BLE001 — rollback já pode ter valido
                logger.exception("rollback gravado, mas o restart falhou")
                self._audit(lib, "FAIL", decision.reason or "pos_check_falhou",
                            probe="ok")
                self._set_failed(
                    decision.reason or "pos_check_falhou",
                    "pós-check não confirmou a troca; a fonte anterior voltou em "
                    f"active_source, mas o restart falhou ({exc}) — ela só vale "
                    "no próximo reinício do pipeline")
                return
            self._audit(lib, "FAIL", decision.reason or "pos_check_falhou",
                        probe="ok")
            self._set_failed(decision.reason or "pos_check_falhou",
                             "pós-check não confirmou a troca; fonte anterior "
                             "restaurada")
        except SwitchBusy as exc:
            # corrida perdida na reivindicação do marcador: outro processo
            # entrou entre a checagem do `start` e a escrita. É bloqueio, não
            # erro interno — o motivo tem de dizer isso ao operador.
            if lib is not None:
                self._fail(lib, "troca_em_andamento", str(exc), probe="blocked")
            else:
                self._set_failed("troca_em_andamento", str(exc))
        except Exception as exc:  # noqa: BLE001 — a thread não pode morrer calada
            self._set_failed("erro_interno", f"{type(exc).__name__}: {exc}")
            if lib is not None:
                try:
                    self._audit(lib, "FAIL", "erro_interno", probe="failed")
                except Exception:  # noqa: BLE001
                    logger.exception("auditoria do erro interno falhou")
        finally:
            self._clear_marker()
            self._lock.release()

    def _safe_started_at(self) -> Optional[str]:
        """`StartedAt` do container; ausência não invalida a troca."""
        try:
            return self.runtime.started_at(self.app_container)
        except Exception:  # noqa: BLE001
            logger.exception("não foi possível ler StartedAt do container")
            return None

    # -- arquivos ----------------------------------------------------------
    def _previous_value(self) -> Optional[str]:
        path = self.config_dir / "active_source"
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def _write_control(self, value: str) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        target = self.config_dir / "active_source"
        tmp = target.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(f"{value.strip()}\n", encoding="utf-8")
        os.replace(tmp, target)

    def _restore_control(self, previous: Optional[str]) -> None:
        """Volta o `active_source` ao valor anterior (ou remove o arquivo)."""
        if previous:
            self._write_control(previous)
        else:
            (self.config_dir / "active_source").unlink(missing_ok=True)

    # -- marcador: mutex entre processos (script × tela) --------------------
    def _marker_path(self) -> Path:
        return self.config_dir / MARKER_NAME

    def _claim_marker(self, source_id: str) -> None:
        """Reivindica o marcador de forma atômica (`O_EXCL`).

        O lock em memória só separa trocas **deste** processo; o
        `scripts/switch_source.sh` escreve o mesmo arquivo. Sem `O_EXCL`, dois
        caminhos podiam trocar junto (dois restarts, `previous` capturado por
        um e desfeito pelo outro e o marcador do primeiro apagado pelo
        segundo), e o watchdog deixava de suprimir `no_frames` no meio da
        troca. Aqui: cria com `O_EXCL`, valida `expires_at` e apaga (na saída)
        **só** o marcador que este processo criou.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._marker_owner = f"dashboard:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        expires = (datetime.now(timezone.utc)
                   + timedelta(seconds=self.timeout_s + 60))
        payload = json.dumps({
            "to": source_id,
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "owner": self._marker_owner,
            "origin": "dashboard",
        }) + "\n"
        marker = self._marker_path()
        for attempt in (1, 2):
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                active = self._marker_active()
                if active is not None or attempt == 2:
                    raise SwitchBusy(self._busy_message(active or {})) from None
                self._discard_stale_marker()
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            return

    def _discard_stale_marker(self) -> None:
        """Descarta um marcador expirado **sem** roubar o de outro processo.

        `unlink` depois de checar validade é um TOCTOU: entre a checagem e o
        `unlink`, outro processo pode ter criado o marcador dele e nós
        apagaríamos o arquivo errado. O `rename` é atômico e quem renomeia é
        dono do inode; aí se confere a validade **do inode renomeado**: se ele
        estava válido (perdemos a corrida), devolve para o lugar e recusa a
        troca.
        """
        marker = self._marker_path()
        stale = marker.with_name(
            f"{MARKER_NAME}.stale.{uuid.uuid4().hex[:8]}")
        try:
            os.replace(marker, stale)
        except FileNotFoundError:
            return                      # outro já resolveu: tenta criar direto
        except OSError as exc:
            raise SwitchBusy(
                "não foi possível liberar o marcador expirado "
                f"({exc}); remova {marker} se não houver troca em curso") from None
        try:
            if self._marker_valid(stale):
                os.replace(stale, marker)   # era de outro: devolve e recusa
                raise SwitchBusy(self._busy_message(self._read_marker(stale) or {}))
        finally:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    def _marker_valid(self, path: Path) -> bool:
        """True se o marcador em `path` ainda vale (com TTL por `mtime`)."""
        data = self._read_marker(path)
        if data is None:
            return False
        expires = data.get("expires_at")
        if expires:
            try:
                deadline = datetime.strptime(
                    str(expires), "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc)
            except ValueError:
                deadline = None
            if deadline is not None:
                return deadline > datetime.now(timezone.utc)
        # sem validade legível: vale por `mtime + timeout + 60`, o mesmo
        # contrato que o watchdog usa para marcador órfão. Nunca "para sempre".
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age <= (self.timeout_s + 60)

    def _read_marker(self, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        path = path or self._marker_path()
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}      # ilegível (outra ferramenta): tratar como ocupado
        return data if isinstance(data, dict) else {}

    def _marker_active(self) -> Optional[Dict[str, Any]]:
        """Marcador de **outro** dono ainda válido; `None` se livre/expirado."""
        if not self._marker_path().exists():
            return None
        data = self._read_marker()
        if data is None:
            return None
        if data.get("owner") and data.get("owner") == self._marker_owner:
            return None
        return data if self._marker_valid(self._marker_path()) else None

    def _clear_marker(self) -> None:
        """Remove o marcador **só** se ainda for deste processo."""
        if not self._marker_owner:
            return
        data = self._read_marker()
        if not data or data.get("owner") != self._marker_owner:
            return
        try:
            self._marker_path().unlink(missing_ok=True)
        except OSError:
            pass

    def _audit(self, lib, result: str, reason: str, probe: str) -> None:
        """Uma linha em `switch.jsonl`, igual à do script."""
        record = lib.switch_record(
            source_id=self.status.source_id or "",
            previous=self.status.previous or "",
            result=result, reason=reason, probe=probe,
            extra={"origin": "dashboard"},
        )
        try:
            self.switch_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.switch_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # auditoria é best-effort; o status da tela não depende dela

    # -- estado ------------------------------------------------------------
    def _step(self, step: str, message: str) -> None:
        self.status.step = step
        self.status.message = message

    def _fail(self, lib, reason: str, message: str, probe: str) -> None:
        self._audit(lib, "FAIL", reason, probe=probe)
        self._set_failed(reason, message)

    def _set_failed(self, reason: str, message: str) -> None:
        self.status.state = "failed"
        self.status.step = "falhou"
        self.status.reason = reason
        self.status.message = message
        self.status.finished_at = _now_iso()

    # -- introspecção (usado pelo `app.py`) --------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": lib_available(),
            "status": self.status.as_dict(),
            "timeout_s": self.timeout_s,
            "container": self.app_container,
        }
