"""Dashboard backend — Vision Stability Lab (Fase 4).

Consome o JSONL de telemetria do pipeline (data/logs/live.jsonl),
mantém histórico em memória — **uma série por fonte** (E4-T1) — e expõe:

  GET  /api/snapshot        estado atual + fonte ativa + saúde derivada
  GET  /api/sources         catálogo de fontes (path somente leitura) + sessão
  GET  /api/history         amostras de métricas (filtro opcional por fonte)
  GET  /api/events          lista de eventos recentes (filtro opcional)
  POST /api/source/switch   pede a troca da fonte ativa (E4-T3, opt-in)
  GET  /api/source/switch   estado da última troca pedida pela tela
  WS   /ws                  push em tempo real (snapshot + métricas + eventos)

Sem persistência além do próprio JSONL (o histórico é reconstruído
replayando o arquivo na partida).

A chave do histórico é o `source_id` da telemetria (E3-T2). Sessão nova da
mesma fonte (`session_epoch` diferente) começa série vazia: nenhum ponto
atravessa fontes nem execuções (D4, ADR-0006).

A troca (`/api/source/switch`) **não** reconfigura o pipeline em runtime: ela
escreve `data/config/active_source` e reinicia o processo, exatamente como
`scripts/switch_source.sh` — a decisão A3 do ADR-0006 segue valendo. O endpoint
fica desligado (`503`) enquanto `SWITCH_TOKEN` não estiver definido.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import switch_control
from sources_catalog import load_catalog

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dashboard")

TELEMETRY_FILE = Path(os.environ.get(
    "TELEMETRY_FILE", "/app/data/logs/live.jsonl"))
METRICS_HISTORY_MAX = int(os.environ.get("METRICS_HISTORY_MAX", "480"))  # 4h @30s
EVENTS_HISTORY_MAX = int(os.environ.get("EVENTS_HISTORY_MAX", "500"))
POLL_INTERVAL_S = float(os.environ.get("TAIL_POLL_S", "0.5"))
STALE_AFTER_S = float(os.environ.get("STALE_AFTER_S", "90"))

# Retrato do observador de fontes (E7). O Compose monta `../data` como `:ro`
# no dashboard: daqui só se lê, nunca se escreve. O caminho é configurável
# para o host/teste, onde `data/` está no checkout e não em `/app`.
OBSERVER_STATE_FILE = Path(os.environ.get(
    "OBSERVER_STATE_FILE", "/app/data/config/observer_state.json"))
# O watchdog reescreve o arquivo a cada ciclo (~10 s). 120 s tolera alguns
# ciclos perdidos (Docker lento, host sob carga) sem desconfiar de um
# observador vivo; acima disso o retrato é velho demais para afirmar "fonte
# viva" ou "fonte morta" — melhor dizer que não há observador confiável.
OBSERVER_STATE_MAX_AGE_S = float(
    os.environ.get("OBSERVER_STATE_MAX_AGE_S", "120"))

# Cache do último retrato lido: indexado por (caminho, mtime, tamanho), então
# uma escrita nova do watchdog é vista na hora e o arquivo não é relido em
# cada requisição (o `/ws` e a tela pedem snapshot com frequência).
_observer_cache: Dict[str, Any] = {"key": None, "payload": None}


def _observer_unavailable(reason: str, age_s: Optional[float] = None,
                          ts: Optional[str] = None) -> Dict[str, object]:
    """Retrato neutro de "sem observador": mesmas chaves, nada inventado.

    `available: false` existe para a tela poder dizer "sem observador" em vez
    de mostrar valores de ontem como se fossem atuais.
    """
    return {
        "available": False,
        "mode": None,
        "mode_reason": None,
        "last_action": None,
        "last_action_ts": None,
        "ts": ts,
        "age_s": age_s,
        "sources_probe": {},
        "reason": reason,
    }


def read_observer_state() -> Dict[str, object]:
    """Lê `observer_state.json` sem nunca levantar (HTTP 200 sempre).

    Arquivo ausente, ilegível, JSON parcial ou `ts` velho viram
    `available: false` com o motivo em `reason`. O observador é informação
    **aditiva**: se ele quebrar, o snapshot de telemetria continua valendo.
    """
    try:
        info = OBSERVER_STATE_FILE.stat()
        key = (str(OBSERVER_STATE_FILE), info.st_mtime_ns, info.st_size)
    except OSError as exc:
        _observer_cache["key"] = None
        return _observer_unavailable(
            f"observer_state.json indisponível ({exc.__class__.__name__})")

    if _observer_cache["key"] == key and _observer_cache["payload"] is not None:
        return dict(_observer_cache["payload"])

    def _remember(payload: Dict[str, object]) -> Dict[str, object]:
        _observer_cache["key"] = key
        _observer_cache["payload"] = payload
        return dict(payload)

    try:
        with open(OBSERVER_STATE_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return _remember(_observer_unavailable(
            f"observer_state.json ilegível ({exc.__class__.__name__})"))
    if not isinstance(raw, dict):
        return _remember(_observer_unavailable(
            "observer_state.json não é um objeto JSON"))

    ts = raw.get("ts")
    age: Optional[float] = None
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = round(
                (datetime.now(timezone.utc) - parsed).total_seconds(), 1)
        except ValueError:
            ts = None
    if age is None:
        return _remember(_observer_unavailable(
            "observer_state.json sem `ts` utilizável"))
    if age > OBSERVER_STATE_MAX_AGE_S:
        return _remember(_observer_unavailable(
            f"estado do observador velho (há {int(age)}s)", age_s=age, ts=ts))
    # JSON parcial (sem `mode`) não descreve modo nenhum: não dá para dizer
    # "observador disponível" sem ele.
    if not isinstance(raw.get("mode"), str) or not raw.get("mode"):
        return _remember(_observer_unavailable(
            "observer_state.json sem `mode`", age_s=age, ts=ts))

    probes = raw.get("sources_probe")
    return _remember({
        "available": True,
        "mode": raw.get("mode"),
        "mode_reason": raw.get("mode_reason"),
        "last_action": raw.get("last_action"),
        "last_action_ts": raw.get("last_action_ts"),
        "ts": ts,
        "age_s": age,
        "sources_probe": probes if isinstance(probes, dict) else {},
        "reason": None,
    })


def observer_probe(source_id: str,
                   observer: Optional[Dict[str, object]] = None
                   ) -> Dict[str, object]:
    """Sonda de uma fonte para `/api/sources`.

    Sem observador disponível (arquivo ausente/velho) o status é
    `"sem observador"` e **não** há `alive`: `alive: false` afirmaria "fonte
    morta" sem ninguém ter medido. Com observador vivo mas sem sonda desta
    fonte, `"nao_sondada"` — de novo, ausência de medição não é morte.
    """
    if observer is None:
        observer = read_observer_state()
    if not observer.get("available"):
        return {"status": "sem observador"}
    probe = (observer.get("sources_probe") or {}).get(source_id)
    if not isinstance(probe, dict):
        return {"status": "nao_sondada"}
    return {
        "status": "measured",
        "alive": probe.get("alive"),
        "level": probe.get("level"),
        "reason": probe.get("reason"),
        "checked_at": probe.get("checked_at"),
    }

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


LEGACY_SOURCE = "legacy"   # JSONL anterior ao E3-T2, sem `source_id`


@dataclass
class SourceSession:
    """Sessão de uma fonte, com a série de métricas daquela sessão.

    Sessão nova da mesma fonte (outro `session_epoch`) **substitui** a anterior
    em `DashboardState.sessions`: o gráfico nunca mistura execuções (D4) nem
    fontes. `metrics` reusa o ring buffer global, então o custo de memória não
    muda com o número de fontes.
    """

    id: str
    name: str
    epoch: Optional[str] = None
    since: Optional[str] = None
    last_record_ts: Optional[str] = None
    metrics: Deque[dict] = field(
        default_factory=lambda: deque(maxlen=METRICS_HISTORY_MAX))

    def summary(self) -> Dict[str, object]:
        """Resumo para `/api/sources` — sem série completa.

        A idade usa o `ts` da amostra, não o `recv_at`: no replay da partida
        todos os pontos entram com `recv_at` de agora, e uma sessão de ontem
        apareceria como "fresca" no cartão.
        """
        last = self.metrics[-1] if self.metrics else None
        age: Optional[float] = None
        if last and last.get("ts"):
            try:
                ts = datetime.fromisoformat(str(last["ts"]))
                age = round((datetime.now(timezone.utc) - ts).total_seconds(), 1)
            except ValueError:
                age = None
        return {
            "id": self.id,
            "name": self.name,
            "session_epoch": self.epoch,
            "since": self.since,
            "last_ts": self.last_record_ts,
            "points": len(self.metrics),
            "last_metric_age_s": age,
        }


class DashboardState:
    """Estado compartilhado alimentado pelo tailer.

    Uma série por `source_id` (E4-T1). `active_source_id` é a fonte da última
    sessão vista no JSONL — a mesma que o pipeline está publicando agora,
    porque o replay e o tail seguem a ordem do arquivo.
    """

    def __init__(self) -> None:
        self.sessions: Dict[str, SourceSession] = {}
        self.events: Deque[dict] = deque(maxlen=EVENTS_HISTORY_MAX)
        self.active_source_id: Optional[str] = None
        self.has_active_source = False
        self.pipeline_started_at: Optional[str] = None
        self.preview_transport: Optional[str] = None
        self.subscribers: set = set()
        # execução do pipeline: cada 'startup' abre uma nova; registros da
        # partida anterior ficam com run menor (o replay pode carregar histórico)
        self.current_run = 0
        self._runs_seen = 0

    def apply(self, record: dict) -> Optional[str]:
        """Aplica um registro JSONL vindo do pipeline."""
        event = record.get("event")
        if event == "startup":
            self.pipeline_started_at = record.get("ts")
            self._runs_seen += 1
            self.current_run = self._runs_seen
        record["run"] = self.current_run
        session = self._session_for(record)
        if event == "interval_metrics":
            record["recv_at"] = time.time()
            session.metrics.append(record)
        elif event == "preview_branch_ready":
            self.preview_transport = record.get("transport")
            self._push_event(record)
        else:
            # eventos relevantes para a lista (transições, tracks, erros...)
            # `source_active` entra aqui (m8 da revisão): é o evento que registra
            # QUAL fonte subiu e com qual cadeia de mídia (E4: vira o cartão
            # "fonte ativa").
            if event in ("track_entered", "track_exited", "area_transition",
                         "pipeline_error", "pipeline_warning", "eos",
                         "interrupted_by_user", "test_duration_reached",
                         "pipeline_stopped", "pipeline_playing",
                         "source_active", "preview_branch_ready",
                         # não são falha do pipeline (E3-T2): erro do `rtspsrc`
                         # durante reconexão e queda do ramo descartável de
                         # preview. Ficam visíveis na lista, mas fora de
                         # `error_count`.
                         "rtsp_reconnect_error", "preview_branch_error"):
                self._push_event(record)
        return event

    def _session_for(self, record: dict) -> SourceSession:
        """Sessão da fonte do registro; abre uma nova quando o epoch muda."""
        source_id = record.get("source_id") or LEGACY_SOURCE
        epoch = record.get("session_epoch")
        name = record.get("source_name") or source_id
        session = self.sessions.get(source_id)
        if session is None or session.epoch != epoch:
            # Sessão nova começa série vazia; como o JSONL é append-only, a
            # última sessão vista é a que o pipeline está publicando.
            session = SourceSession(id=source_id, name=name, epoch=epoch,
                                    since=record.get("ts"))
            self.sessions[source_id] = session
            self.active_source_id = source_id
            self.has_active_source = True
        elif record.get("source_name"):
            session.name = record["source_name"]
        if record.get("ts"):
            session.last_record_ts = record["ts"]
        return session

    def active_session(self) -> Optional[SourceSession]:
        if not self.has_active_source or self.active_source_id is None:
            return None
        return self.sessions.get(self.active_source_id)

    def _push_event(self, record: dict) -> dict:
        self.events.append(record)
        return record

    def health(self) -> Dict[str, object]:
        """Saúde derivada **apenas** da sessão ativa (E4-T1)."""
        now = time.time()
        session = self.active_session()
        last = session.metrics[-1] if session and session.metrics else None
        if last is None or (now - last["recv_at"]) > STALE_AFTER_S * 3:
            return {"status": "down", "reason": "sem telemetria"}
        age = now - last["recv_at"]
        if age > STALE_AFTER_S:
            return {"status": "stale", "reason": f"telemetria há {int(age)}s"}
        fps = last.get("fps_processed") or 0
        if fps <= 0:
            return {"status": "degraded", "reason": "fps_processed = 0"}
        return {"status": "healthy", "reason": None}

    def source_state(self) -> Optional[Dict[str, object]]:
        """Fonte ativa para o `/api/snapshot`: id, nome, epoch e início."""
        session = self.active_session()
        if session is None:
            return None
        return {
            "id": session.id,
            "name": session.name,
            "session_epoch": session.epoch,
            "since": session.since,
        }

    def history(self, minutes: int = 30,
                source_id: Optional[str] = None) -> List[dict]:
        """Amostras da fonte pedida; sem `source_id`, da fonte ativa.

        Fonte sem histórico devolve `[]` (não erro) — `source=camera` antes da
        primeira sessão da câmera é um estado normal, não uma falha.
        """
        sid = self.active_source_id if source_id is None else source_id
        session = self.sessions.get(sid) if sid else None
        if session is None:
            return []
        cutoff = time.time() - minutes * 60
        return [m for m in session.metrics if m.get("recv_at", 0) >= cutoff]

    def snapshot(self) -> Dict[str, object]:
        session = self.active_session()
        last = session.metrics[-1] if session and session.metrics else None
        # erros contam apenas a execução atual do pipeline (campo 'run')
        errors = [e for e in self.events
                  if e.get("event") == "pipeline_error"
                  and e.get("run") == self.current_run]
        return {
            "server_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "health": self.health(),
            "source": self.source_state(),
            "latest_metrics": last,
            "pipeline_started_at": self.pipeline_started_at,
            "preview_transport": self.preview_transport,
            "error_count": len(errors),
            "last_error": errors[-1] if errors else None,
            # lista de eventos também restrita à execução atual
            "events_recent": [e for e in self.events
                              if e.get("run") == self.current_run][-50:],
            # Observador de fontes (E7): campo aditivo — clientes antigos
            # ignoram; se o arquivo do watchdog faltar, vem `available: false`
            # em vez de erro, porque telemetria e observador são independentes.
            "observer": read_observer_state(),
        }


state = DashboardState()
app = FastAPI(title="Vision Stability Lab")


async def tail_telemetry(start_offset=None):
    """Segue live.jsonl (tolera rotação/truncamento) e difunde novidades.

    start_offset: byte a partir do qual ler; evita perder linhas escritas
    entre o fim do replay inicial e a abertura do arquivo aqui.
    """
    logger.info("tailer aguardando %s", TELEMETRY_FILE)
    while True:
        if not TELEMETRY_FILE.exists():
            await asyncio.sleep(1)
            continue
        try:
            with open(TELEMETRY_FILE) as fh:
                fh.seek(start_offset if start_offset is not None
                        else os.path.getsize(TELEMETRY_FILE))
                start_offset = None
                while True:
                    line = fh.readline()
                    if not line:
                        await asyncio.sleep(POLL_INTERVAL_S)
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    state.apply(record)
                    await broadcast({
                        "type": "record",
                        "data": record,
                    })
        except FileNotFoundError:
            continue
        except Exception:
            logger.exception("tailer falhou; reiniciando em 5s")
            await asyncio.sleep(5)


async def broadcast(message):
    dead = []
    for ws in list(state.subscribers):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.subscribers.discard(ws)


@app.on_event("startup")
async def _start_tailer():
    # Replay do histórico existente antes de seguir ao vivo; o offset final
    # é passado ao tailer para não perder linhas escritas nesse intervalo.
    start_offset = 0
    if TELEMETRY_FILE.exists():
        n = 0
        with open(TELEMETRY_FILE) as fh:
            for line in fh:
                try:
                    state.apply(json.loads(line))
                    n += 1
                except json.JSONDecodeError:
                    continue
            start_offset = fh.tell()
        logger.info("histórico carregado: %d registros", n)
    asyncio.create_task(tail_telemetry(start_offset))


@app.get("/api/snapshot")
async def api_snapshot():
    return state.snapshot()


@app.get("/api/sources")
async def api_sources():
    """Catálogo de fontes + sessão de cada uma (E4-T1).

    O `path` é **somente leitura** e vem sem credencial: quando a URI mora em
    variável (`uri_env`, caso da câmera), `path` é `null` e `path_env` diz o
    nome da variável (DIV-001). Fonte que só existe na telemetria (ou o JSONL
    legado, sem `source_id`) aparece como `path_origin: "telemetria"`.
    """
    catalog = load_catalog()
    known = {str(entry["id"]) for entry in catalog}
    # Uma leitura por requisição (com cache interno) para todas as fontes.
    observer = read_observer_state()
    payload = []
    for entry in catalog:
        session = state.sessions.get(str(entry["id"]))
        payload.append({
            **entry,
            "in_catalog": True,
            "active": entry["id"] == state.active_source_id,
            "session": session.summary() if session else None,
            "observer_probe": observer_probe(str(entry["id"]), observer),
        })
    for source_id, session in state.sessions.items():
        if source_id in known:
            continue
        # Fonte que só existe na telemetria vira cartão apenas quando está
        # ativa (id fora do YAML é sinal de erro de configuração) ou quando não
        # há catálogo nenhum. O JSONL antigo, sem `source_id`, fica como
        # `legacy` no histórico — não polui a lista de fontes.
        if catalog and source_id != state.active_source_id:
            continue
        payload.append({
            "id": source_id,
            "name": session.name,
            "codec": None,
            "target_fps": None,
            "enabled": True,
            "path": None,
            "path_origin": "telemetria",
            "path_env": None,
            "in_catalog": False,
            "active": source_id == state.active_source_id,
            "session": session.summary(),
            "observer_probe": observer_probe(source_id, observer),
        })
    return payload


@app.get("/api/history")
async def api_history(minutes: int = 30, source: Optional[str] = None):
    """Amostras dos últimos N minutos; `source` ausente = fonte ativa."""
    return state.history(minutes=minutes, source_id=source)


@app.get("/api/events")
async def api_events(limit: int = 100, source: Optional[str] = None):
    records = list(state.events)
    if source is not None:
        records = [r for r in records
                   if (r.get("source_id") or LEGACY_SOURCE) == source]
    return records[-limit:]


# ---------------------------------------------------------------- troca (E4-T3)
#
# A troca de fonte pedida pela tela usa **o mesmo contrato** do
# `scripts/switch_source.sh` (D4/D7): pré-check obrigatório, escrita de
# `data/config/active_source`, restart do processo e rollback se o pós-check não
# confirmar. Ver `switch_control.py` para as decisões reusadas.
#
# Segurança — o endpoint só existe quando o operador liga a feature:
#
# - `SWITCH_TOKEN` vazio  -> 503 `switch_disabled` (deploy atual não muda);
# - `SWITCH_ENABLED=0`    -> 503 mesmo com token (desligar sem tirar o token);
# - token divergente      -> 401, comparado com `compare_digest` (o header não
#   aparece em log de acesso por padrão, e nunca ecoamos o valor esperado).
#
# O processo do dashboard precisa do socket do Docker e do repo montados: isso
# vem do overlay `deploy/docker-compose.switch.yml`, nunca do compose padrão —
# DIV-004 no `docs/incident-log.md`.
SWITCH_TOKEN = os.environ.get("SWITCH_TOKEN", "").strip()
SWITCH_ENABLED = os.environ.get("SWITCH_ENABLED", "1").strip() != "0"

switch_state = {"switcher": None}


def switch_feature_reason() -> Optional[str]:
    """Por que a troca está indisponível, ou `None` quando está tudo pronto."""
    if not SWITCH_ENABLED:
        return "switch_enabled_off"
    if not SWITCH_TOKEN:
        return "switch_token_ausente"
    if not switch_control.lib_available():
        return "switch_sem_repo_montado"
    return None


def get_switcher() -> "switch_control.Switcher":
    """`Switcher` único do processo, criado na primeira troca.

    O `DockerRuntime` só é construído aqui (import do SDK tardio): um dashboard
    sem a feature ligada nunca toca no socket do Docker.
    """
    switcher = switch_state.get("switcher")
    if switcher is None:
        # Os dois parâmetros de pré-check vêm do ambiente do dashboard
        # (`PRECHECK_SECONDS`/`PRECHECK_TIMEOUT_S` no overlay): antes o
        # `DockerRuntime()` era criado sem argumentos e a configuração só
        # aparecia no texto da tela.
        precheck_s = float(switch_control.PRECHECK_SECONDS)
        switcher = switch_control.Switcher(
            switch_control.DockerRuntime(probe_seconds=precheck_s),
            precheck_seconds=switch_control.PRECHECK_SECONDS,
            precheck_timeout_s=switch_control.PRECHECK_TIMEOUT_S)
        switch_state["switcher"] = switcher
    return switcher


def _require_switch_token(token: Optional[str]) -> None:
    reason = switch_feature_reason()
    if reason is not None:
        raise HTTPException(status_code=503, detail={"error": reason})
    # `compare_digest` levanta TypeError para `str` não-ASCII: um header
    # malformado derrubava a requisição com 500 em vez de 401. Comparar bytes
    # resolve sem afrouxar a comparação em tempo constante.
    if not token or not secrets.compare_digest(
            token.strip().encode("utf-8", "surrogateescape"),
            SWITCH_TOKEN.encode("utf-8", "surrogateescape")):
        logger.warning("tentativa de troca com token inválido recusada")
        raise HTTPException(status_code=401, detail={"error": "token_invalido"})


@app.post("/api/source/switch", status_code=202)
async def api_switch_source(payload: Dict[str, object],
                            x_switch_token: Optional[str] = Header(None)):
    """Pede a troca da fonte ativa (assíncrona; acompanhe com o `GET`)."""
    _require_switch_token(x_switch_token)
    source_id = str((payload or {}).get("source_id") or "").strip()
    if not source_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "source_id_ausente",
                    "message": "informe `source_id` (id de sources.yaml)"})
    try:
        status = get_switcher().start(source_id)
    except switch_control.SwitchBusy as exc:
        raise HTTPException(status_code=409,
                            detail={"error": "troca_em_andamento",
                                    "message": str(exc)}) from exc
    except switch_control.SwitchRejected as exc:
        raise HTTPException(status_code=400,
                            detail={"error": "source_id_invalido",
                                    "message": str(exc)}) from exc
    except switch_control.SwitchError as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail={"error": "switch_indisponivel",
                                    "message": str(exc)}) from exc
    except ModuleNotFoundError as exc:  # SDK do Docker fora da imagem
        logger.error("SDK do Docker indisponível: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "docker_indisponivel",
                    "message": "o dashboard não tem o SDK do Docker "
                               "(rebuild da imagem com requirements.txt)"}) from exc
    except Exception as exc:  # noqa: BLE001 — socket ausente/sem permissão
        logger.exception("falha ao iniciar a troca de fonte")
        raise HTTPException(
            status_code=503,
            detail={"error": "docker_indisponivel",
                    "message": f"não foi possível falar com o Docker: {exc}"}) from exc
    logger.info("troca de fonte pedida pela tela: %s", source_id)
    return {"accepted": True, "status": status.as_dict()}


@app.get("/api/source/switch")
async def api_switch_status():
    """Estado da última troca + se a feature está ligada (sem token)."""
    reason = switch_feature_reason()
    switcher = switch_state.get("switcher")
    status = (switcher.status.as_dict() if switcher is not None
              else switch_control.SwitchStatus().as_dict())
    # Troca em andamento por outro processo (`switch_source.sh`): o operador
    # precisa saber que o 409 vem do mutex entre processos, não de bug.
    foreign = switcher.foreign_marker() if switcher is not None else None
    return {
        "enabled": reason is None,
        "disabled_reason": reason,
        "container": switch_control.APP_CONTAINER,
        "timeout_s": switch_control.TIMEOUT_S,
        "status": status,
        "in_progress_by": ({
            "origin": foreign.get("origin") or "script",
            "to": foreign.get("to"),
            "expires_at": foreign.get("expires_at"),
        } if foreign else None),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state.subscribers.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot",
                                       "data": state.snapshot()}))
        while True:
            await ws.receive_text()  # keepalive/ping do cliente
    except WebSocketDisconnect:
        pass
    finally:
        state.subscribers.discard(ws)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
