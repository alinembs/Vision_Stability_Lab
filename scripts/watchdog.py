#!/usr/bin/env python3
"""Watchdog externo do Vision Stability Lab.

Roda FORA do processo do pipeline (container próprio com acesso ao
docker.sock). Um while True dentro do pipeline não protege contra
travamentos nativos, erros de driver ou deadlocks — este processo sim,
pois observa o sistema por fora e pode reiniciar o container.

Regras implementadas (spec Fase 6):
  1. Sem frame por 60s            -> evento no_frames_detected
     (a reconexão RTSP em si acontece dentro do pipeline; se o quadro
      não voltar em NO_FRAME_ESCALATE_S, reinicia o container)
     Crash-loop (>= CRASH_LOOP_MIN_RESTARTS restarts em
     CRASH_LOOP_WINDOW_S) -> evento crash_loop_detected: a carência de
     startup deixaria o watchdog cego nesse estado. O restart só é
     escalado se houver corroboração de que a fonte não entrega frames
     (RULE1_FRAME_TIMEOUT_S sem frame) — restart em série sozinho também
     é a assinatura de `docker compose up -d` e de troca de fonte.
  2. FPS < 30% do alvo por 2 min  -> registra degradação
  3. RSS > 15% acima da baseline  -> emite alerta
  4. RAM do sistema > 90%         -> reinicia o pipeline (docker restart)
  5. Erro CUDA fatal nos logs     -> encerra/reinicia o processo

Evento fora das regras: `container_missing` — a API do Docker devolveu 404
para o container observado (o stack sumiu). Distinto de `api_down`; não
escala restart (reiniciar container inexistente falha calado).

Troca de fonte em andamento (E2-T3): `data/config/switch_in_progress` marca o
intervalo em que `scripts/switch_source.sh` recria o container. Enquanto o
marcador existe e não expirou, a Regra 1 e o crash-loop ficam mudos
(`switch_in_progress_observed` sai uma vez) — `up -d --force-recreate` em série
é, por assinatura, idêntico a crash-loop, e o critério do E2-T3 é que uma troca
não gere alerta. RAM/CUDA continuam valendo. Marcador corrompido é registrado
(`switch_marker_invalid`) e ignorado.

Observador de fontes (E7): além das regras acima, cada ciclo monta um retrato
(`Observation`) e pede a decisão ao `scripts/observer_policy.py` — o watchdog
**mede** (estado do container, telemetria e sondas RTSP) e a política só decide.
O resultado vai para `data/config/observer_state.json` (escrita atômica, lida
pelo dashboard) e os eventos de transição para `watchdog.jsonl`. As sondas são
duas: nível 1 = DESCRIBE via `scripts/probe/rtsp_describe_probe.py` (barata,
todo ciclo) e nível 2 = leitura real de mídia via `rtsp_read_probe.py`
(subprocesso efêmero, só quando a política pede).

Fontes de dados:
  - data/logs/live.jsonl  (telemetria JSONL do pipeline, seguida em tail)
  - /proc/meminfo         (RAM do sistema)
  - docker.sock           (logs do container + restart/stop/start)
  - data/config/active_source, sources RTSP (sondas DESCRIBE)

Sem dependências externas (apenas stdlib).
"""

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# `observer_policy.py` mora ao lado deste arquivo no repositório e em
# `/app/scripts` no container; o Dockerfile também copia a versão para `/app`
# (o compose monta `scripts/` em `/app/scripts`, mas a imagem não depende do
# mount para importar o módulo de decisão).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, "/app/scripts"):
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import observer_policy  # noqa: E402  (depois do ajuste de sys.path)

# Sonda de nível 1 (DESCRIBE, só stdlib): mesmo caminho relativo no host e no
# container (`/app/scripts/probe`). `/app/probe` cobre a cópia da imagem, que
# não depende do mount de `scripts/`.
for _candidate in (os.path.join(_HERE, "probe"), "/app/scripts/probe",
                   "/app/probe"):
    if os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

try:  # pragma: no cover - ausente só se a imagem/mount estiver incompleto
    import rtsp_describe_probe  # noqa: E402
except ImportError:  # a sonda some, o watchdog continua com as regras antigas
    rtsp_describe_probe = None

# ---------------------------------------------------------------------------
# Configuração (env)
# ---------------------------------------------------------------------------
TELEMETRY_FILE = os.environ.get(
    "TELEMETRY_FILE", "/app/data/logs/live.jsonl")
WATCHDOG_LOG = os.environ.get(
    "WATCHDOG_LOG", "/app/data/logs/watchdog.jsonl")

APP_CONTAINER = os.environ.get("APP_CONTAINER", "vision-stability-app")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
# Marcador de troca de fonte em andamento (E2-T3): escrito por
# `scripts/switch_source.sh` com `{"to": "<id>", "expires_at": "<ISO>"}` e
# removido ao fim. Enquanto ele vale, a recriação do container é **esperada** —
# não é crash-loop nem fonte morta.
SWITCH_MARKER = os.environ.get(
    "SWITCH_MARKER", "/app/data/config/switch_in_progress")

CHECK_INTERVAL_S = float(os.environ.get("WATCHDOG_INTERVAL_S", "10"))
STARTUP_GRACE_S = float(os.environ.get("WATCHDOG_GRACE_S", "120"))

# 60 s, não 30 s: o pipeline publica `interval_metrics` a cada
# METRICS_INTERVAL_S=30 s (`deepstream/pipeline_real.py`) e o watchdog só vê
# frame por essa amostra. Com timeout igual ao intervalo, a margem era zero:
# medido em 4.025 intervalos reais do laboratório, 1.367 chegaram com >30 s
# entre publicações **com frames fluindo** -> ~41 `no_frames_detected`
# espúrios na série histórica. Dois intervalos de folga cobrem o atraso do
# timer GLib e o poll de 10 s sem afrouxar a detecção (a escalada em
# NO_FRAME_ESCALATE_S continua em 180 s).
RULE1_FRAME_TIMEOUT_S = float(os.environ.get("FRAME_TIMEOUT_S", "60"))
RULE1_ESCALATE_S = float(os.environ.get("NO_FRAME_ESCALATE_S", "180"))

# Crash-loop: abaixo disso o container nunca vive STARTUP_GRACE_S, então a
# carência de startup vira portão absoluto e cega `_rule1_idle` para sempre
# (medido no duplo de Docker dos testes: 30 min de fonte morta -> 0
# `no_frames_detected`, 0 `restart_triggered`, 29 `container_restarted`).
CRASH_LOOP_WINDOW_S = float(os.environ.get("CRASH_LOOP_WINDOW_S", "900"))
CRASH_LOOP_MIN_RESTARTS = int(os.environ.get("CRASH_LOOP_MIN_RESTARTS", "5"))
# Sem restart novo por este tempo, o ciclo acabou: a Regra 1 volta a valer
# mesmo que a janela ainda tenha histórico de 5 restarts. Sem isso, um
# container que estabilizou ficaria sem `no_frames_detected` até a janela
# esvaziar (até 15 min).
CRASH_LOOP_ACTIVE_S = float(os.environ.get("CRASH_LOOP_ACTIVE_S", "240"))

RULE2_FPS_RATIO = float(os.environ.get("FPS_DEGRADE_RATIO", "0.30"))
RULE2_WINDOW_S = float(os.environ.get("FPS_DEGRADE_WINDOW_S", "120"))
TARGET_FPS = float(os.environ.get("TARGET_FPS", "25"))

RULE3_RSS_RATIO = float(os.environ.get("RSS_GROWTH_RATIO", "0.15"))
RSS_BASELINE_GRACE_S = float(os.environ.get("RSS_BASELINE_GRACE_S", "300"))

RULE4_RAM_PCT = float(os.environ.get("RAM_CRITICAL_PCT", "90"))
RAM_CONFIRMATIONS = int(os.environ.get("RAM_CONFIRMATIONS", "3"))

ALERT_COOLDOWN_S = float(os.environ.get("ALERT_COOLDOWN_S", "300"))
RESTART_COOLDOWN_S = float(os.environ.get("RESTART_COOLDOWN_S", "600"))
LOG_POLL_S = float(os.environ.get("LOG_POLL_S", "30"))

# ---------------------------------------------------------------------------
# Observador de fontes (E7)
# ---------------------------------------------------------------------------
# Retrato do observador: escrito de forma atômica a cada ciclo e consumido pelo
# dashboard. Mesma pasta de `active_source` (rw no compose do watchdog).
OBSERVER_STATE = os.environ.get(
    "OBSERVER_STATE", "/app/data/config/observer_state.json")
# Arquivo de controle da fonte ativa (escrito por `switch_source.sh` e pelo
# observador). Só leitura aqui; quem escreve é a troca.
ACTIVE_SOURCE_FILE = os.environ.get(
    "ACTIVE_SOURCE_FILE", "/app/data/config/active_source")
# `ACTIVE_SOURCE` no ambiente é dono da escolha (`observer_policy` devolve NONE
# com `active_source_env_set`): o watchdog precisa saber disso para não decidir
# troca por cima de uma fonte fixada pelo operador.
ACTIVE_SOURCE_ENV = os.environ.get("ACTIVE_SOURCE", "")
# Ordem de precedência das fontes. Mesma lista/ordem do `sources.yaml`; vem do
# compose (`OBSERVER_SOURCE_PRIORITY`) para não exigir PyYAML nesta imagem.
OBSERVER_SOURCE_PRIORITY = os.environ.get(
    "OBSERVER_SOURCE_PRIORITY", "camera,drone,phone")
# `camera` é a única fonte com credencial e a única cuja URI não é um path do
# MediaMTX: vem de `RTSP_URI`, a mesma variável do serviço do pipeline (DIV-001,
# credencial nunca fica no repositório). Drone/celular são paths publicados no
# MediaMTX, derivados de `MTX_HOST` — exatamente como em `sources.yaml`.
MTX_HOST = (os.environ.get("MTX_HOST") or "mediamtx").strip()
RTSP_URI = os.environ.get("RTSP_URI", "")
LEGACY_SOURCE_ID = "camera"

# Sonda de nível 1 (DESCRIBE): orçamento curto porque roda a cada ciclo de 10 s
# e não pode atrasar as regras de segurança do container.
PROBE_DESCRIBE_TIMEOUT_S = float(
    os.environ.get("OBSERVER_PROBE_TIMEOUT_S", "2.0"))
# Sonda de nível 2 (mídia de verdade): subprocesso efêmero, um por vez e por
# ciclo; o teto é o timeout duro do próprio `rtsp_read_probe.py` + folga.
LEVEL2_PROBE_TIMEOUT_S = float(os.environ.get("OBSERVER_LEVEL2_TIMEOUT_S", "15"))
LEVEL2_PROBE_SECONDS = "5"
LEVEL2_PROBE_MIN_FRAMES = "15"
LEVEL2_PROBE_MIN_FPS = "3.0"
LEVEL2_PROBE_MAX_GAPS = "2"

# Troca automática ligada por default; o knob é do operador (`make up` repassa
# `OBSERVER_AUTO_SWITCH=0`), não do código.
OBSERVER_AUTO_SWITCH = (os.environ.get("OBSERVER_AUTO_SWITCH", "1")
                        .strip().lower() not in ("0", "false", "no", "off"))

# Mutex da troca de fonte (E7-T4): o observador reivindica **o mesmo**
# `switch_in_progress` do `switch_source.sh` e do dashboard, com O_EXCL. Sem
# isso o start automático e uma troca manual disputariam o mesmo container.
# TTL próprio (150 s = 90 s de pós-check + 60 s de folga, a mesma conta do
# script) porque um `kill -9` no meio da ação não pode deixar o mutex preso
# para sempre.
OBSERVER_SWITCH_MARKER_TTL_S = float(
    os.environ.get("OBSERVER_SWITCH_MARKER_TTL_S", "150"))
# Dono gravado no marcador: é o que permite apagar só o **próprio** marcador.
OBSERVER_SWITCH_OWNER = "observer"
# Orçamento do pós-check do `_engage`: o processo novo precisa publicar
# `interval_metrics` com `fps_in > 0` nesse tempo. Mesma janela do
# `switch_source.sh --timeout`: ~34 s de bootstrap do engine + 30 s de
# intervalo de telemetria, com folga. Estourou -> rollback e standby.
OBSERVER_WAKE_TIMEOUT_S = float(
    os.environ.get("OBSERVER_WAKE_TIMEOUT_S", "90"))
# Folga entre `session_epoch` (relógio do pipeline) e `StartedAt` (relógio do
# Docker): os dois vêm de fontes diferentes e arredondam em milissegundos.
WAKE_EPOCH_TOLERANCE_S = 2.0
# Histórico de trocas: mesmo arquivo e mesmo shape do `switch_source.sh`
# (`switch_source_lib.switch_record`), para o operador ler uma série só.
SWITCH_LOG = os.environ.get("SWITCH_LOG", "/app/data/logs/switch.jsonl")

CUDA_FATAL_RE = re.compile(
    r"cudaerr|cuda error|cudaerror|cudafatal|fatal.*cuda|cuda.*fatal|"
    r"nvml error|cuda driver version is insufficient|"
    r"cuda_device_operate_failed|out of memory in cuda",
    re.IGNORECASE)


def emit(event, **fields):
    """Log estruturado do watchdog: stdout + data/logs/watchdog.jsonl."""
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc)
              .isoformat(timespec="milliseconds"),
        "event": f"watchdog_{event}",
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    try:
        with open(WATCHDOG_LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cliente mínimo da Docker API (HTTP sobre unix socket, sem dependências)
# ---------------------------------------------------------------------------
def _dechunk(body):
    out, i = [], 0
    while i < len(body):
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(body[i:j].split(b";")[0], 16)
        except ValueError:
            return body
        if size == 0:
            break
        out.append(body[j + 2:j + 2 + size])
        i = j + 2 + size + 2
    return b"".join(out)


class DockerAPI:
    def __init__(self, sock_path):
        self.sock_path = sock_path
        # Código HTTP da última `container_state`: 404 = container não existe,
        # None = API inalcançável (ou nenhuma leitura ainda).
        self.last_status = None

    def request(self, method, path, timeout=20):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.sock_path)
            sock.sendall(
                f"{method} {path} HTTP/1.1\r\nHost: docker\r\n"
                f"Connection: close\r\n\r\n".encode())
            chunks = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        status = int(lines[0].split()[1]) if lines and lines[0] else 0
        headers = {}
        for hline in lines[1:]:
            if ":" in hline:
                k, v = hline.split(":", 1)
                headers[k.strip().lower()] = v.strip().lower()
        if headers.get("transfer-encoding") == "chunked":
            body = _dechunk(body)
        return status, body

    def logs_since(self, container, since_epoch):
        status, body = self.request(
            "GET",
            f"/containers/{container}/logs?stdout=true&stderr=true"
            f"&timestamps=false&since={int(since_epoch)}")
        if status != 200:
            return ""
        # Stream multiplexado (não-TTY): frames de 8 bytes
        # [tipo, 0, 0, 0, tamanho u32 BE]
        out, i = [], 0
        while i + 8 <= len(body):
            size = int.from_bytes(body[i + 4:i + 8], "big")
            if size == 0 or i + 8 + size > len(body):
                break
            out.append(body[i + 8:i + 8 + size].decode(errors="replace"))
            i += 8 + size
        if not out and body:
            return body.decode(errors="replace")
        return "".join(out)

    def container_state(self, container):
        """`(running, StartedAt, uptime_s, exit_code)`, ou `(None,)*4`.

        Usado para distinguir "a fonte parou" de "o processo do pipeline
        reiniciou": depois de um restart externo (`docker restart`, crash,
        recriação) não há frame porque o processo está subindo, e o watchdog
        não deve cobrar isso como fonte morta. Qualquer falha na API devolve
        None e o watchdog volta ao comportamento antigo.

        `exit_code` é o `State.ExitCode` do Docker — o observador usa o código
        `2` (`source_reconnect_exhausted` do pipeline) como prova direta de que
        a fonte ativa morreu, sem esperar a telemetria envelhecer. Ele é o
        código da **última** saída: com o container rodando vale 0.

        O código HTTP fica em `last_status` para o chamador distinguir
        container ausente (404) de API fora do ar (None).
        """
        try:
            status, body = self.request(
                "GET", f"/containers/{container}/json", timeout=5)
        except OSError:
            self.last_status = None
            return None, None, None, None
        self.last_status = status
        if status != 200:
            return None, None, None, None
        try:
            state = json.loads(body).get("State") or {}
        except (json.JSONDecodeError, AttributeError):
            return None, None, None, None
        started = state.get("StartedAt") or ""
        try:
            # `StartedAt` fora do formato ISO (`nada`, `2026-13-45T99:99:99Z`)
            # levantava ValueError daqui: escapava de `container_state`, caía
            # no `except Exception` do loop (`watchdog_error` a cada poll) e
            # engolia `_rule4_ram()`/`_rule5_cuda_logs()` daquele ciclo.
            uptime = _age_seconds(started)
        except (ValueError, OverflowError):
            uptime = None
        try:
            exit_code = state.get("ExitCode")
            exit_code = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code = None
        return bool(state.get("Running")), started or None, uptime, exit_code

    def restart(self, container, timeout_s=10):
        status, _ = self.request(
            "POST", f"/containers/{container}/restart?t={timeout_s}",
            timeout=timeout_s + 30)
        return status in (204, 200)

    def stop(self, container, timeout_s=10):
        """`POST /containers/{id}/stop?t=<timeout>` — parada graciosa.

        304 ("not modified") é o container que já estava parado: para quem
        chama isso é sucesso, como no `restart()`. O evento é emitido aqui, no
        cliente, para que o resultado da chamada apareça no log estruturado
        mesmo quando quem chama é outro fluxo (observador, dashboard).
        """
        status, _ = self.request(
            "POST", f"/containers/{container}/stop?t={timeout_s}",
            timeout=timeout_s + 30)
        ok = status in (204, 200, 304)
        emit("docker_stop", container=container, timeout_s=timeout_s,
             status=status, ok=ok)
        return ok

    def start(self, container):
        """`POST /containers/{id}/start` — sobe o container parado.

        304 é o container que já estava rodando (sucesso idempotente).
        """
        status, _ = self.request(
            "POST", f"/containers/{container}/start", timeout=30)
        ok = status in (204, 304)
        emit("docker_start", container=container, status=status, ok=ok)
        return ok


# ---------------------------------------------------------------------------
# Tail simples de JSONL (acompanha rotação/truncamento)
# ---------------------------------------------------------------------------
class JsonlTail:
    """Tail de JSONL que sobrevive a rotação e a truncamento no lugar.

    Depois de um truncamento, o offset de leitura fica além do fim do arquivo
    e o tail fica cego — tempo suficiente para o watchdog escalar restart por
    falso positivo. Medido em bancada em 2026-10-30: `no_frames_detected`
    às 17:25:53 e `frames_recovered` só às 17:27:23, sem nenhuma falha real.
    """

    MAX_PASSES = 2          # relê o arquivo novo/truncado na mesma chamada

    def __init__(self, path):
        self.path = path
        self.fh = None
        self.inode = None
        self._primeira_abertura = True

    def _open(self):
        try:
            self.fh = open(self.path)
            self.inode = os.fstat(self.fh.fileno()).st_ino
        except OSError:
            self.fh = None
            return False
        if self._primeira_abertura:
            self.fh.seek(0, os.SEEK_END)   # ignora histórico anterior
            self._primeira_abertura = False
        return True

    def _read(self):
        novas = []
        try:
            for line in self.fh:
                novas.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        return novas

    def _reabrir_se_mudou(self):
        """Fecha o arquivo se ele foi substituído, removido ou truncado."""
        try:
            trocado = os.stat(self.path).st_ino != self.inode
        except OSError:
            trocado = True                  # sumiu: reabre quando voltar
        try:
            posicao = self.fh.tell()
            tamanho = os.fstat(self.fh.fileno()).st_size
        except (OSError, ValueError):
            posicao, tamanho = 0, 0
        if not trocado and tamanho >= posicao:
            return False
        try:
            self.fh.close()
        except OSError:
            pass
        self.fh = None
        return True

    def poll(self):
        """Retorna as novas linhas desde a última chamada."""
        novas = []
        for _ in range(self.MAX_PASSES):
            if self.fh is None and not self._open():
                break
            novas.extend(self._read())
            if not self._reabrir_se_mudou():
                break
        return novas


DOCKER_TS_RE = re.compile(
    r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?")


def _timestamp_epoch(iso_ts):
    """Epoch (UTC) de um timestamp ISO-8601 do Docker/pipeline, ou None.

    O Docker devolve nanossegundos (`...16:59:23.123456789Z`) e `strptime` só
    aceita microssegundos — daí o parse manual da fração. Nunca levanta:
    timestamp malformado (`nada`, `2026-13-45T99:99:99Z`) devolve None.
    """
    match = DOCKER_TS_RE.match(iso_ts or "")
    if not match:
        return None
    try:
        base = datetime.datetime.strptime(
            match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        fracao = float("0." + (match.group(2) or "0")[:6])
    except (ValueError, OverflowError, OSError):
        return None
    return base + fracao


def _age_seconds(iso_ts, now_epoch=None):
    """Idade, em segundos, de um timestamp ISO-8601 do Docker.

    Nunca levanta: `StartedAt` malformado devolve None (o watchdog volta ao
    comportamento antigo, sem carência) e idade negativa — relógio do host
    andou para trás depois do start do container — é tratada como 0, o que dá
    a mesma carência de um processo recém-subido.
    """
    base = _timestamp_epoch(iso_ts)
    if base is None:
        return None
    agora = time.time() if now_epoch is None else now_epoch
    idade = agora - base
    return round(idade, 1) if idade > 0 else 0.0


def read_switch_marker(path, now_epoch=None):
    """Estado do marcador de troca de fonte (E2-T3) — `(ativo, info)`.

    `scripts/switch_source.sh` grava `data/config/switch_in_progress` com
    `{"to": "<id>", "expires_at": "<ISO>"}` e o remove no fim (trap, inclusive
    em falha). O watchdog precisa deste sinal por dois motivos:

    - `up -d --no-deps --force-recreate` em série tem, por assinatura, a mesma
      cara de um crash-loop (5 `StartedAt` novos em minutos) — `crash_loop`
      durante uma troca é ruído garantido, e o critério do E2-T3 é que uma
      troca **não** gere alerta;
    - a carência de startup não cobre todo o intervalo em que o processo novo
      ainda não publicou `interval_metrics` (~34 s).

    Tolerante por construção:

      - arquivo ausente (o caso normal, 99,9% do tempo) -> `(False, absent)`;
      - `expires_at` no passado -> `(False, expired)`: marcador órfão de um
        `kill -9` no meio da troca deixa de valer sozinho;
      - JSON inválido, `expires_at` ausente/ilegível ou arquivo ilegível ->
        `(False, corrupt|unreadable)`: vale o comportamento normal (alertar) e
        o caso vai para o log. Nunca deixar um marcador malformado suprimir
        regra de segurança.

    Devolve `(True, {"to", "expires_at", "ttl_s"})` só quando a troca está em
    andamento e ainda não expirou.
    """
    now = time.time() if now_epoch is None else now_epoch
    try:
        with open(path) as handle:
            raw = handle.read()
    except FileNotFoundError:
        return False, {"reason": "absent"}
    except OSError as exc:
        return False, {"reason": "unreadable", "detail": str(exc)}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, {"reason": "corrupt", "detail": f"json: {exc.msg}"}
    if not isinstance(data, dict):
        return False, {"reason": "corrupt", "detail": "não é um objeto JSON"}
    expires_raw = data.get("expires_at")
    expires_epoch = _timestamp_epoch(str(expires_raw or ""))
    if expires_epoch is None:
        return False, {"reason": "corrupt",
                       "detail": "expires_at ausente ou ilegível"}
    ttl = expires_epoch - now
    if ttl <= 0:
        return False, {"reason": "expired", "to": data.get("to") or "",
                       "expires_at": expires_raw, "ttl_s": round(ttl, 1)}
    return True, {"to": data.get("to") or "", "expires_at": expires_raw,
                  "ttl_s": round(ttl, 1)}


def _iso_utc_seconds(epoch):
    """ISO-8601 UTC com segundos — o formato de `expires_at` do marcador.

    De propósito **sem** milissegundos: é o formato que o
    `switch_source_lib.marker_expired` sabe ler. Com fração ele cairia no
    fallback de mtime e o mutex duraria mais que o nosso pós-check.
    """
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marker_discardable(path, info, now_epoch):
    """O marcador existente pode ser apagado antes de reivindicar?

    Só o que **já não vale**: expirado (órfão de `kill -9`) ou ilegível há mais
    de um TTL — o `switch_source.sh` também cai no mtime quando não há
    `expires_at` legível. Marcador válido de outro dono nunca é apagado.
    """
    reason = (info or {}).get("reason")
    if reason == "expired":
        return True
    if reason not in ("corrupt", "unreadable"):
        return False
    try:
        idade = now_epoch - os.path.getmtime(path)
    except OSError:
        return False
    return idade > OBSERVER_SWITCH_MARKER_TTL_S


def claim_switch_marker(path, source_id, now_epoch=None, ttl_s=None):
    """Reivindica `switch_in_progress` para o observador — `(ok, info)`.

    `open(..., "x")` é O_CREAT|O_EXCL: a **criação** do arquivo é o token, como
    o `set -C` (noclobber) do `switch_source.sh`. Quem perde a corrida recebe
    `FileExistsError` e cede o ciclo (`yield_marker`): durante uma troca alheia
    qualquer ação nossa disputaria o mesmo container.

    Marcador expirado é removido e reivindicado: um `kill -9` no meio da troca
    não pode travar o observador até alguém apagar o arquivo à mão. Depois de
    remover, a reivindicação tenta **uma** vez mais — se outro dono ganhar a
    corrida nesse intervalo, cedemos (nunca sobrescrever marcador válido).
    """
    now = time.time() if now_epoch is None else now_epoch
    ttl = OBSERVER_SWITCH_MARKER_TTL_S if ttl_s is None else ttl_s
    payload = json.dumps({
        "to": source_id,
        "owner": OBSERVER_SWITCH_OWNER,
        "expires_at": _iso_utc_seconds(now + ttl),
    }, ensure_ascii=False) + "\n"
    for _ in range(2):
        try:
            handle = open(path, "x", encoding="utf-8")
        except FileExistsError:
            ativo, info = read_switch_marker(path, now)
            if ativo or not _marker_discardable(path, info, now):
                return False, info
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, {"reason": "unremovable", "detail": str(exc)}
            continue
        except OSError as exc:
            return False, {"reason": "unwritable", "detail": str(exc)}
        try:
            handle.write(payload)
        except OSError as exc:
            handle.close()
            # O arquivo é nosso (acabamos de criá-lo): apagar não toca em
            # marcador alheio.
            try:
                os.unlink(path)
            except OSError:
                pass
            return False, {"reason": "unwritable", "detail": str(exc)}
        handle.close()
        return True, {"to": source_id, "owner": OBSERVER_SWITCH_OWNER,
                      "expires_at": _iso_utc_seconds(now + ttl)}
    ativo, info = read_switch_marker(path, now)
    return False, info


def release_switch_marker(path, source_id):
    """Apaga o marcador **só** quando ele é do observador para `source_id`.

    O arquivo é mutex compartilhado com o `switch_source.sh` e o dashboard:
    apagar o marcador de outro dono libera a troca antes da hora (o mesmo bug
    H2 corrigido no script em 2026-11-01). Conteúdo ilegível também não é
    apagado — pode ser um marcador sendo escrito por outro processo.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if (data.get("owner") or "") != OBSERVER_SWITCH_OWNER:
        return False
    if source_id and (data.get("to") or "") != source_id:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def write_switch_record(*, source_id, previous, result, reason, probe="ok",
                        timestamp=None, extra=None):
    """Linha de `data/logs/switch.jsonl` no shape de `switch_record()`.

    O observador **não** importa `scripts/switch_source_lib.py`: esse módulo não
    é copiado para a imagem do watchdog. O shape é replicado aqui (mesmos
    campos, `origin=observer` para separar troca automática de manual) para o
    histórico continuar sendo uma série só. Falha de escrita não pode derrubar
    o ciclo — vira evento e o observador segue.
    """
    record = {
        "ts": timestamp or _iso_ts(time.time()),
        "event": "switch_source",
        "from": (previous or "").strip(),
        "to": source_id,
        "probe": probe,
        "result": result,
        "reason": reason,
    }
    if extra:
        record.update(extra)
    try:
        directory = os.path.dirname(SWITCH_LOG)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(SWITCH_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        emit("switch_log_write_failed", path=SWITCH_LOG, error=str(exc))


def system_ram_used_pct():
    try:
        with open("/proc/meminfo") as fh:
            info = {}
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        if total <= 0:
            return None
        return round(100.0 * (total - available) / total, 1)
    except OSError:
        return None


def _iso_ts(epoch):
    """Instante ISO-8601 (UTC, milissegundos) — o formato do log e do estado."""
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat(timespec="milliseconds")


def write_json_atomic(path, payload):
    """Grava `payload` como JSON em `path` sem leitor ver arquivo pela metade.

    `.tmp` no **mesmo diretório** + `os.replace` (atômico dentro do mesmo
    filesystem). O dashboard lê `observer_state.json` a cada poucos segundos:
    um `open(..., "w")` direto deixaria uma janela com JSON truncado. Falha de
    escrita não pode derrubar o watchdog — vira evento e o ciclo segue.
    """
    tmp = f"{path}.tmp"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        emit("observer_state_write_failed", path=path, error=str(exc))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def read_active_source(path):
    """Id da fonte ativa de `data/config/active_source`; `""` se não houver.

    Arquivo de uma linha, editado à mão pelo operador (`switch_source.sh`
    também escreve): ignora branco e comentários `#`, aceita BOM/CRLF. Ausente
    ou ilegível devolve `""` — o watchdog não pode morrer por causa do arquivo
    de controle (a validação de id é da política/quem escreve).
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                candidate = line.strip()
                if candidate and not candidate.startswith("#"):
                    return candidate
    except (OSError, ValueError):
        return ""
    return ""


def write_active_source(path, source_id):
    """Grava `data/config/active_source` (uma linha) de forma atômica.

    `.tmp` no mesmo diretório + `os.replace`: o pipeline lê esse arquivo no
    boot e nenhum leitor pode vê-lo pela metade. `source_id` vazio escreve uma
    linha em branco — é o estado de "sem fonte" que `read_active_source` já
    entende como `""`, e o que o rollback usa quando não havia fonte anterior.
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(f"{source_id or ''}\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        emit("active_source_write_failed", path=path, error=str(exc))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def source_uri(source_id):
    """URI RTSP de uma fonte, pela convenção do `deepstream/config/sources.yaml`.

    `camera` é a fonte com credencial (`RTSP_URI`); `drone`/`phone` são paths
    do MediaMTX, derivados de `MTX_HOST`. Não lê o YAML de propósito: esta
    imagem é `python:3.11-slim` sem PyYAML e a lista de fontes chega pelo
    compose (`OBSERVER_SOURCE_PRIORITY`/`MTX_HOST`/`RTSP_URI`).
    """
    if source_id == LEGACY_SOURCE_ID:
        return (RTSP_URI or "").strip()
    host = (MTX_HOST or "").strip()
    if not host:
        return ""
    return f"rtsp://{host}:8554/{source_id}"


@dataclass(frozen=True)
class ObservedSource:
    """Fonte que o observador pode sondar: `id` da precedência + URI RTSP."""

    id: str
    uri: str


@dataclass(frozen=True)
class SourceSnapshot:
    """Retrato das fontes utilizáveis no ciclo (sem I/O depois de montado).

    `sources` já vem na ordem de precedência resolvida — a política só obedece
    (`Observation.source_ids`). `active_from_env` é o que faz a política
    devolver NONE: com `ACTIVE_SOURCE` no ambiente a escolha é do operador.
    """

    sources: Tuple[ObservedSource, ...] = ()
    active_source: str = ""
    active_from_env: bool = False

    @property
    def source_ids(self):
        return tuple(source.id for source in self.sources)

    def source_for(self, source_id):
        for source in self.sources:
            if source.id == source_id:
                return source
        return None


def read_source_snapshot(control_path=None, priority=None):
    """Monta o `SourceSnapshot` do ciclo, tolerante a ambiente incompleto.

    Fontes sem URI (ex.: `camera` sem `RTSP_URI` no ambiente de teste) ficam de
    fora: sondar `""` só produziria falso "fonte morta". A fonte ativa do
    arquivo/env entra na lista mesmo fora da precedência — a política precisa
    do probe dela para decidir (`_active_is_dead`).
    """
    ordered = [item.strip()
               for item in (priority or OBSERVER_SOURCE_PRIORITY).split(",")
               if item.strip()]
    env_active = (ACTIVE_SOURCE_ENV or "").strip()
    file_active = read_active_source(control_path or ACTIVE_SOURCE_FILE)
    active = env_active or file_active or (ordered[0] if ordered else "")
    if active and active not in ordered:
        ordered.insert(0, active)

    sources, seen = [], set()
    for source_id in ordered:
        if source_id in seen:
            continue
        uri = source_uri(source_id)
        if not uri:
            continue
        seen.add(source_id)
        sources.append(ObservedSource(source_id, uri))
    if active not in seen:
        active = sources[0].id if sources else ""
    return SourceSnapshot(tuple(sources), active, bool(env_active))


def _last_json_line(text):
    """Último objeto JSON de um texto (contrato de saída do `rtsp_read_probe`)."""
    for line in reversed((text or "").splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def level2_probe_script():
    """Caminho do `rtsp_read_probe.py` (env > repositório > container)."""
    candidates = (
        os.environ.get("OBSERVER_LEVEL2_SCRIPT"),
        os.path.join(_HERE, "probe", "rtsp_read_probe.py"),
        "/app/scripts/probe/rtsp_read_probe.py",
        "/app/probe/rtsp_read_probe.py",
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return candidates[1]


class Watchdog:
    def __init__(self):
        self.docker = DockerAPI(DOCKER_SOCK)
        self.tail = JsonlTail(TELEMETRY_FILE)
        self.started_at = time.monotonic()

        # Regra 1 — frames
        self.last_frame_at = time.monotonic()
        # Evidência de frame imune ao reset otimista de `last_frame_at`: um
        # restart (ou a carência) reescreve `last_frame_at` para "agora" porque
        # o processo pode ainda estar subindo. Corroborar crash-loop com essa
        # marca daria "fonte viva" para todo restart em série — só a telemetria
        # de verdade mexe aqui.
        self.last_frame_evidence_at = time.monotonic()
        self.no_frame_reported = False
        # Container ausente (404 em /containers/<x>/json): distinto de API fora
        # do ar. Sem ele, o único rastro em teste não assistido era
        # `no_frames_detected` + `restart_result ok:false` repetidos.
        self._container_missing = False
        self._container_missing_reported_at = None
        # StartedAt do processo observado: quando muda, o pipeline reiniciou
        # por fora (docker restart, crash, recriação do compose).
        self._app_started_at = None
        # Crash-loop: instantes dos restarts observados dentro da janela.
        # Sem isso, `uptime < STARTUP_GRACE_S` faz `_rule1_idle` retornar antes
        # de qualquer detecção enquanto o container reiniciar rápido — o
        # watchdog fica cego (e sem escalada) para sempre.
        self._restart_events = deque()
        self._restart_event_emitted_at = None
        self._crash_loop_reported_at = None
        # Última leitura de frames_total_in, que é cumulativo POR PROCESSO do
        # pipeline. None = ainda não há base (primeira amostra ou logo após
        # um restart). Nunca guardar high-water entre processos: era a causa
        # do restart em loop por falso positivo (E-B).
        self._prev_frames_total = None

        # Regra 2 — degradação de FPS
        self.low_fps_since = None
        self.degrade_reported = False

        # Regra 3 — RSS
        self.rss_baseline_mb = None
        self.last_rss_alert_at = 0.0

        # Regra 4 — RAM crítica
        self.ram_high_count = 0
        self.last_restart_at = 0.0

        # Regra 5 — logs/CUDA
        self.log_last_poll = 0.0
        self.log_since_epoch = time.time()

        # Troca de fonte em andamento (E2-T3): enquanto o marcador vale, a
        # recriação do container é esperada. Suprime `no_frames_detected` e
        # `crash_loop_detected` — **não** as regras de RAM/CUDA.
        self._switch_active = False
        self._switch_reported = None          # (to, expires_at) já anunciado
        self._switch_invalid_reported = None  # conteúdo já registrado inválido
        # Recriações causadas por troca (marcador válido). Ficam FORA da janela
        # de crash-loop: se entrassem, o evento sairia no primeiro poll depois
        # da troca (medido em 2026-10-30 21:34:52, `frames_flowing: true`).
        self._switch_restarts = 0

        # -- observador de fontes (E7) --------------------------------------
        # Decisão e estado da política ficam FORA do pipeline de propósito:
        # continuam válidos quando o pipeline morre, que é o caso que importa.
        self._policy = observer_policy.ObserverPolicy()
        self._policy_state = observer_policy.PolicyState()
        self._mode = observer_policy.MODE_WATCHING
        # Motivo da última transição de modo (vai para `mode_reason` no estado).
        # Sem transição ainda, fica vazio: é mais honesto do que inventar um
        # motivo que o observador não registrou.
        self._mode_reason = ""
        self._probes = {}            # source_id -> observer_policy.Probe
        self._probe_checked_at = {}  # source_id -> epoch da última sonda
        self._last_action = observer_policy.Action.NONE.value
        self._last_action_ts = None
        # Último retrato do container, lido em `_observe_container_start` (uma
        # leitura por ciclo) e reaproveitado pela `Observation`.
        self._app_running = None
        self._app_exit_code = None
        # Último `fps_in` da telemetria: com fonte ativa entregando frame não se
        # abre sessão RTSP concorrente nela (a câmera IP não gosta de duas).
        self._last_fps_in = 0.0
        # Namespace da última `interval_metrics` (`source_id`/`session_epoch`,
        # E3-T2). O pós-check do `_engage` só aprova métrica da fonte pedida e
        # do processo novo — nunca a do processo anterior.
        self._last_metric_source = None
        self._last_metric_epoch = None
        # Transição em curso (`_engage`): `{"source", "previous", "deadline",
        # "started_at", "engaged_at"}`. O pós-check roda no ciclo seguinte, sem
        # bloquear o loop — um `sleep` de 90 s aqui pararia RAM/CUDA junto.
        self._wake = None
        # `StartedAt` produzido pelo próprio `_engage`: se o PASS liberar o
        # marcador antes do próximo poll, esse `StartedAt` ainda é esperado e
        # não pode entrar na janela de crash-loop.
        self._expected_started_at = None
        self._snapshot = read_source_snapshot()

    # -- ações -------------------------------------------------------------
    def _can_restart(self):
        if time.monotonic() - self.last_restart_at < RESTART_COOLDOWN_S:
            return False
        self.last_restart_at = time.monotonic()
        return True

    def _restart(self, reason, **fields):
        if not self._can_restart():
            emit("restart_suppressed", reason=reason, cooldown_active=True)
            return
        emit("restart_triggered", reason=reason, container=APP_CONTAINER,
             **fields)
        ok = self.docker.restart(APP_CONTAINER)
        emit("restart_result", reason=reason, ok=ok)
        if ok:
            # período de carência pós-restart para todas as métricas
            self.started_at = time.monotonic()
            self.last_frame_at = time.monotonic()
            self.no_frame_reported = False
            # O processo do pipeline renasceu: o cumulativo começa em zero.
            # Manter a base anterior é o que fazia o watchdog comparar o
            # processo novo (~2.900 frames) com o recorde do antigo
            # (~1.375.018) e escalar no_frames_escalation para sempre.
            self._prev_frames_total = None
            self.low_fps_since = None
            self.degrade_reported = False
            self.rss_baseline_mb = None
            self.ram_high_count = 0
            self.log_since_epoch = time.time()
            # processo novo: nenhum frame ainda; a fonte ativa volta a ser
            # sondada até a telemetria provar que está entregando.
            self._last_fps_in = 0.0

    # -- telemetria ----------------------------------------------------------
    def _handle_metric(self, r):
        now = time.monotonic()

        # Regra 1 — fluxo de frames.
        # frames_total_in é cumulativo POR PROCESSO: só diz "houve frame" na
        # diferença contra a leitura anterior DESTE processo.
        frames_total = r.get("frames_total_in") or 0
        fps_in = r.get("fps_in") or 0
        prev = self._prev_frames_total
        frame_seen = False

        if prev is not None and frames_total < prev:
            # Contador andou para trás: o processo do pipeline reiniciou sem
            # o watchdog participar (docker restart manual, crash, recriação).
            # O valor antigo não é base de nada — rebaseline explícito.
            emit("frames_counter_reset", previous=prev, current=frames_total)
            prev = None

        if prev is None:
            # Primeira amostra deste processo: qualquer contador > 0 já é
            # prova de frame recebido (não dá para comparar com nada).
            frame_seen = frames_total > 0
        elif frames_total > prev:
            frame_seen = True

        # fps_in > 0 é evidência direta de frame na janela, mesmo com o
        # cumulativo estagnado (antes esta checagem só valia se o
        # frames_total também tivesse crescido).
        if fps_in > 0:
            frame_seen = True

        if frame_seen:
            self.last_frame_at = now
            self.last_frame_evidence_at = now
            if self.no_frame_reported:
                emit("frames_recovered")
                self.no_frame_reported = False

        # Sem max(): a base é sempre a última leitura real do processo atual.
        self._prev_frames_total = frames_total
        # O observador usa isso para não abrir sessão RTSP concorrente na fonte
        # ativa: `fps_in > 0` é a prova de que ela está entregando.
        self._last_fps_in = fps_in
        # Namespace da amostra (E3-T2): o pós-check do `_engage` precisa saber
        # de qual fonte e de qual processo a métrica veio. `ts` é o fallback
        # quando o pipeline não publica `session_epoch`.
        self._last_metric_source = (r.get("source_id") or "").strip() or None
        self._last_metric_epoch = (
            _timestamp_epoch(str(r.get("session_epoch") or ""))
            or _timestamp_epoch(str(r.get("ts") or "")))

        # Regra 2 — FPS abaixo de 30% do alvo sustentado
        fps_proc = r.get("fps_processed") or 0
        threshold = RULE2_FPS_RATIO * TARGET_FPS
        if fps_proc < threshold:
            if self.low_fps_since is None:
                self.low_fps_since = now
            elif (now - self.low_fps_since >= RULE2_WINDOW_S
                    and not self.degrade_reported):
                emit("fps_degradation",
                     fps_processed=fps_proc, threshold=round(threshold, 2),
                     target_fps=TARGET_FPS,
                     sustained_s=round(now - self.low_fps_since, 1))
                self.degrade_reported = True
        else:
            if self.degrade_reported:
                emit("fps_recovered", fps_processed=fps_proc)
            self.low_fps_since = None
            self.degrade_reported = False

        # Regra 3 — RSS vs baseline
        rss = r.get("rss_mb")
        if rss:
            if self.rss_baseline_mb is None:
                if now - self.started_at >= RSS_BASELINE_GRACE_S:
                    self.rss_baseline_mb = rss
                    emit("rss_baseline_set", rss_mb=rss)
            elif rss > self.rss_baseline_mb * (1 + RULE3_RSS_RATIO):
                if now - self.last_rss_alert_at >= ALERT_COOLDOWN_S:
                    self.last_rss_alert_at = now
                    emit("rss_growth_alert", rss_mb=rss,
                         baseline_mb=self.rss_baseline_mb,
                         growth_pct=round(
                             100 * (rss / self.rss_baseline_mb - 1), 1))

    # -- regras baseadas em tempo -------------------------------------------
    def _observe_container_start(self):
        """Dá carência quando o processo do pipeline reiniciou por fora.

        Sem isto, um `docker restart` (ou um crash com `restart:
        unless-stopped`) gera `no_frames_detected` só porque o processo levou
        mais que o frame_timeout para voltar — ruído que mascara a diferença
        entre "eu reiniciei" e "a fonte morreu". Retorna a idade do processo
        em segundos (None = desconhecida).

        A carência efetiva é o **monotônico observado** (`self.started_at`),
        não o `uptime` do Docker: com `StartedAt` no futuro (passo de NTP para
        trás) a idade é 0.0 em todos os polls e `uptime < STARTUP_GRACE_S`
        virava portão absoluto — a Regra 1 ficava muda para sempre, sem
        crash-loop para desempatar (D2). O `uptime` do Docker só vale a
        primeira leitura, quando ainda não há monotônico para comparar.
        """
        running, started_at, uptime, exit_code = self.docker.container_state(
            APP_CONTAINER)
        self._app_running = running
        self._app_exit_code = exit_code
        self._check_container_missing()
        if started_at is None:
            return None                      # API indisponível: regra antiga
        if started_at == self._app_started_at:
            return None                      # processo conhecido: carência monotônica
        primeira_leitura = self._app_started_at is None
        self._app_started_at = started_at
        if primeira_leitura:
            return uptime
        # processo novo: mesma carência de um restart feito por nós
        now = time.monotonic()
        self.started_at = now
        self.last_frame_at = now
        self.no_frame_reported = False
        self._prev_frames_total = None
        self._register_restart_event(now, started_at, uptime, running)
        return None                          # carência do processo novo é monotônica

    def _check_container_missing(self):
        """Emite `container_missing` quando o Docker devolve 404 para o app.

        404 é o container não existir mais (stack removido, `docker compose
        down`, rename) — não é fonte morta e não é API fora do ar (nesse caso
        `last_status` é None e o watchdog segue no comportamento antigo).
        Reiniciar um container inexistente só produz `restart_result ok:false`
        repetido, que em teste não assistido parece defeito de pipeline.
        """
        self._container_missing = getattr(
            self.docker, "last_status", None) == 404
        if not self._container_missing:
            self._container_missing_reported_at = None
            return
        now = time.monotonic()
        anterior = self._container_missing_reported_at
        if anterior is not None and now - anterior < ALERT_COOLDOWN_S:
            return
        self._container_missing_reported_at = now
        emit("container_missing", container=APP_CONTAINER, status=404,
             action="container não existe; restart não é acionado")

    def _register_restart_event(self, now, started_at, uptime, running):
        """Anota o restart na janela e emite `container_restarted` no máximo 1x.

        Em crash-loop o processo troca a cada poll de 10 s: emitir uma linha
        por troca enchia o log de ruído e escondia o evento que importa
        (`crash_loop_detected`). Os restarts continuam contados no deque.

        Restart com `switch_in_progress` válido **não** entra no deque: é o
        `up -d --force-recreate` do `switch_source.sh`, não crash-loop. Sem
        essa exclusão o evento sairia mesmo assim — no primeiro poll depois que
        o script removesse o marcador, com a troca já confirmada (medido em
        2026-10-30 21:34:52).
        """
        events = self._restart_events
        esperado = self._switch_expected()
        if not esperado and self._expected_started_at:
            # `StartedAt` que o próprio `_engage` produziu: o PASS pode ter
            # liberado o marcador antes deste poll, e o restart é nosso — não
            # é crash-loop (mesmo tratamento de `_switch_active`).
            esperado = started_at == self._expected_started_at
            if esperado:
                self._expected_started_at = None
        if esperado:
            self._switch_restarts += 1
        else:
            events.append(now)
        while events and now - events[0] > CRASH_LOOP_WINDOW_S:
            events.popleft()
        emitida = self._restart_event_emitted_at
        if emitida is not None and now - emitida < CRASH_LOOP_WINDOW_S:
            return
        self._restart_event_emitted_at = now
        emit("container_restarted", started_at=started_at,
             uptime_s=uptime, running=running,
             restarts_in_window=len(events),
             window_s=CRASH_LOOP_WINDOW_S,
             action="nova carência; base do contador zerada",
             grace_s=STARTUP_GRACE_S,
             expected_by_switch=esperado)

    def _crash_loop_check(self, now):
        """True quando o container está em crash-loop (>= N restarts na janela).

        A carência de startup é legítima para um restart isolado, mas em
        crash-loop ela é um portão absoluto: o processo nunca vive
        STARTUP_GRACE_S, então `_rule1_idle` retornaria antes de
        `no_frames_detected` **e** de `_restart()` para sempre. Por isso esta
        checagem vem antes da carência.

        Restart em série, porém, **não prova fonte morta**: um `docker compose
        up -d` e um ciclo de `switch_source.sh` produzem a mesma assinatura de
        `StartedAt` (5 restarts em 14 min) com a fonte entregando 20 fps o
        tempo todo — medido no duplo de Docker do teste. Por isso:

          - `frames_flowing` (frame na telemetria dentro de
            RULE1_FRAME_TIMEOUT_S): emite `crash_loop_detected` uma vez e **não**
            reinicia; reiniciar aqui custa ~35 s de vídeo bom + 5 min de
            vigilância de RSS cega (RSS_BASELINE_GRACE_S).
          - fonte sem frame: emite e escala `restart` (I1 preservado).

        A escalada e o aviso são um por **tempestade** (sequência máxima de
        restarts com intervalo < CRASH_LOOP_ACTIVE_S), não um por janela: o
        re-reporte de 900 s reincidia sobre um container já estabilizado.
        """
        events = self._restart_events
        while events and now - events[0] > CRASH_LOOP_WINDOW_S:
            events.popleft()
        restarts = len(events)
        if restarts < CRASH_LOOP_MIN_RESTARTS:
            return False
        if now - events[-1] > CRASH_LOOP_ACTIVE_S:
            # o container parou de reiniciar: o ciclo acabou e a vigilância
            # normal (Regra 1) volta a ser dona do caso. Nova tempestade pode
            # voltar a reportar/escalar.
            self._crash_loop_reported_at = None
            return False
        if self._crash_loop_reported_at is not None:
            return True                      # já reportado nesta tempestade
        self._crash_loop_reported_at = now

        source_idle = now - self.last_frame_evidence_at
        source_dead = source_idle >= RULE1_FRAME_TIMEOUT_S
        emit("crash_loop_detected", restarts=restarts,
             window_s=CRASH_LOOP_WINDOW_S,
             min_restarts=CRASH_LOOP_MIN_RESTARTS,
             frames_flowing=not source_dead,
             source_idle_s=round(source_idle, 1),
             action=("fonte sem frame: escalando restart" if source_dead else
                     "frames fluindo: sem restart, Regra 1 segue dona do caso"))
        if not source_dead:
            return True
        self._restart("crash_loop", restarts=restarts,
                      window_s=CRASH_LOOP_WINDOW_S)
        return True

    def _observe_switch_marker(self):
        """Lê `switch_in_progress` e mantém `_switch_active`.

        `switch_in_progress_observed` sai **uma vez** por marcador (chaveado por
        `to` + `expires_at`, não por poll): o watchdog checa a cada 10 s e a
        troca dura ~40 s, então um evento por ciclo encheria o log de ruído —
        o mesmo problema que `container_restarted` já resolveu.

        Marcador corrompido é registrado uma vez (`switch_marker_invalid`) e
        tratado como ausente: sem `expires_at` legível não há como saber até
        quando ele vale, e suprimir alerta por tempo indeterminado é pior que
        um alerta espúrio.
        """
        active, info = read_switch_marker(SWITCH_MARKER)
        self._switch_active = active
        if active:
            chave = (info.get("to"), info.get("expires_at"))
            if chave != self._switch_reported:
                self._switch_reported = chave
                emit("switch_in_progress_observed",
                     to=info.get("to"), expires_at=info.get("expires_at"),
                     ttl_s=info.get("ttl_s"),
                     action="troca de fonte em andamento: sem no_frames/crash_loop")
            return
        self._switch_reported = None
        reason = info.get("reason")
        if reason in ("corrupt", "unreadable"):
            chave = info.get("detail")
            if chave != self._switch_invalid_reported:
                self._switch_invalid_reported = chave
                emit("switch_marker_invalid", path=SWITCH_MARKER,
                     reason=reason, detail=info.get("detail"),
                     action="marcador ignorado; regras normais valem")
            return
        self._switch_invalid_reported = None

    def _rule1_idle(self):
        now = time.monotonic()
        # Antes da carência: o StartedAt do processo precisa ser observado
        # mesmo durante ela, senão um crash-loop passaria invisível.
        uptime = self._observe_container_start()
        if (self._switch_expected()
                or self._mode == observer_policy.MODE_STANDBY):
            # Troca de fonte em andamento (E2-T3) ou transição do observador
            # (E7-T4): o processo novo ainda não publicou `interval_metrics` e a
            # recriação em si já foi anotada como esperada. No standby o
            # container está parado **de propósito** — "sem frame" ali é o
            # estado desejado, não fonte morta, e escalar restart religaria o
            # pipeline em vazio (exatamente o que o standby evita). A decisão
            # de religar é da política de fontes. RAM/CUDA não passam por aqui:
            # continuam valendo sempre.
            return
        if self._crash_loop_check(now):
            return
        if now - self.started_at < STARTUP_GRACE_S:
            return
        if uptime is not None and uptime < STARTUP_GRACE_S:
            return
        idle = now - self.last_frame_at
        if idle >= RULE1_FRAME_TIMEOUT_S and not self.no_frame_reported:
            emit("no_frames_detected", idle_s=round(idle, 1),
                 action="pipeline tenta reconectar RTSP internamente")
            self.no_frame_reported = True
        if idle >= RULE1_ESCALATE_S:
            if self._container_missing:
                # Reiniciar container inexistente falha calado e consome o
                # cooldown: `container_missing` já é o sinal do caso.
                self.last_frame_at = now
                return
            self._restart("no_frames_escalation", idle_s=round(idle, 1))
            self.last_frame_at = now  # evita reinício imediato repetido

    def _rule4_ram(self):
        pct = system_ram_used_pct()
        if pct is None:
            return
        if pct >= RULE4_RAM_PCT:
            self.ram_high_count += 1
            if self.ram_high_count >= RAM_CONFIRMATIONS:
                self._restart("system_ram_critical", ram_pct=pct,
                              threshold_pct=RULE4_RAM_PCT)
                self.ram_high_count = 0
        else:
            if self.ram_high_count > 0:
                emit("ram_recovered", ram_pct=pct)
            self.ram_high_count = 0

    def _rule5_cuda_logs(self):
        now = time.monotonic()
        if now - self.log_last_poll < LOG_POLL_S:
            return
        self.log_last_poll = now
        text = self.docker.logs_since(APP_CONTAINER, self.log_since_epoch)
        self.log_since_epoch = time.time()
        for line in text.splitlines():
            if CUDA_FATAL_RE.search(line):
                self._restart("fatal_cuda_error", log_line=line.strip()[:300])
                return

    # -- observador de fontes (E7) -------------------------------------------
    def _set_action(self, action, now_epoch):
        """Registra a ação do ciclo (`observer_state.json.last_action`)."""
        self._last_action = action
        self._last_action_ts = _iso_ts(now_epoch)

    def _switch_expected(self):
        """True quando a troca/religamento do pipeline é esperado.

        Cobre o marcador de outro processo (`_switch_active`) e a nossa própria
        transição (`MODE_SWITCHING`): o `StartedAt` novo gerado pelo `_engage`
        é esperado e não pode entrar na janela de crash-loop nem fazer a Regra 1
        escalar.
        """
        return (self._switch_active
                or self._mode == observer_policy.MODE_SWITCHING)

    def _set_mode(self, mode, reason="", **fields):
        """Troca o modo do observador e emite a transição (uma vez por entrada).

        `standby_entered`/`standby_exited` só saem quando o modo muda: o ciclo
        inteiro roda a cada 10 s, e um evento por poll encheria o log — o mesmo
        critério de `container_restarted`/`switch_in_progress_observed`.
        """
        if mode == self._mode:
            return
        previous, self._mode = self._mode, mode
        # O dashboard mostra "por que o observador está neste modo": guarda o
        # motivo da transição junto com o modo, não só no evento do log.
        self._mode_reason = reason or ""
        if mode == observer_policy.MODE_STANDBY:
            emit("standby_entered", previous_mode=previous, reason=reason,
                 **fields)
        elif previous == observer_policy.MODE_STANDBY:
            emit("standby_exited", mode=mode, reason=reason, **fields)

    def _active_media_flowing(self, now_monotonic):
        """A fonte ativa está entregando frame (`fps_in > 0` recente)?"""
        if self._last_fps_in <= 0:
            return False
        return (now_monotonic - self.last_frame_evidence_at
                < self._policy.active_frame_timeout_s)

    def _record_probe(self, source_id, alive, *, level=None, reason=None,
                      ok_streak=None, now_epoch=None):
        """Atualiza o probe de uma fonte e emite `source_probe_changed`.

        O evento é de **transição** alive↔dead, nunca por poll: `alive` é
        estável por natureza (uma sonda isolada falhando não muda o estado da
        fonte). `ok_streak` conta DESCRIBE consecutivos com o path vivo — é o
        que impede trocar de fonte por uma falha de rede pontual.
        """
        previous = self._probes.get(source_id)
        if ok_streak is None:
            ok_streak = ((previous.ok_streak + 1)
                         if (previous is not None and previous.alive) else 1)
            if not alive:
                ok_streak = 0
        if level is None:
            level = (previous.level
                     if (previous is not None and previous.alive)
                     else observer_policy.LEVEL_DESCRIBE)
        probe = observer_policy.Probe(alive=alive, level=level,
                                      ok_streak=ok_streak, reason=reason)
        self._probes[source_id] = probe
        checked = time.time() if now_epoch is None else now_epoch
        self._probe_checked_at[source_id] = checked
        if previous is not None and previous.alive != probe.alive:
            emit("source_probe_changed", source=source_id, alive=probe.alive,
                 previous_alive=previous.alive, level=probe.level,
                 ok_streak=probe.ok_streak, reason=probe.reason,
                 checked_at=_iso_ts(checked))

    def _probe_level1(self, now_monotonic, now_epoch):
        """DESCRIBE nas fontes habilitadas — exceto na ativa saudável.

        A fonte ativa com `fps_in > 0` **não** é sondada: abrir uma segunda
        sessão RTSP na câmera IP (`RTSP_URI`) concorre com a sessão do
        pipeline, e a telemetria já responde a pergunta que a sonda faria.
        Cada sonda falha silenciosa (o evento de transição é quem conta).
        """
        if rtsp_describe_probe is None:
            return
        healthy_active = self._active_media_flowing(now_monotonic)
        for source in self._snapshot.sources:
            if source.id == self._snapshot.active_source and healthy_active:
                continue
            try:
                result = rtsp_describe_probe.describe(
                    source.uri, timeout=PROBE_DESCRIBE_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — sonda nunca derruba o watchdog
                continue
            self._record_probe(source.id, result.alive, reason=result.reason,
                               now_epoch=now_epoch)

    def _observation(self, now_monotonic, now_epoch):
        """Retrato do ciclo para `ObserverPolicy.decide` (sem I/O aqui)."""
        snapshot = self._snapshot
        return observer_policy.Observation(
            running=bool(self._app_running),
            exit_code=self._app_exit_code,
            last_frame_age_s=round(
                max(0.0, now_monotonic - self.last_frame_evidence_at), 1),
            probes=dict(self._probes),
            active_source=snapshot.active_source,
            source_ids=snapshot.source_ids,
            now=now_epoch,
            switch_marker_active=self._switch_active,
            active_source_env_set=snapshot.active_from_env,
            mode=self._mode,
        )

    def _run_level2_probe(self, source_id, now_epoch):
        """Confirma mídia de uma fonte no nível 2 (subprocesso, um por ciclo).

        `rtsp_read_probe.py` é o mesmo pré-check da troca de fonte: abre a
        mídia, conta frames e mede gaps. Roda **serializado** (o loop é single
        thread e a chamada é síncrona) com teto de `LEVEL2_PROBE_TIMEOUT_S`;
        nada de thread residente.
        """
        source = self._snapshot.source_for(source_id)
        if source is None:
            return
        command = [
            sys.executable or "python3", level2_probe_script(),
            "--url", source.uri,
            "--seconds", LEVEL2_PROBE_SECONDS,
            "--min-frames", LEVEL2_PROBE_MIN_FRAMES,
            "--min-fps", LEVEL2_PROBE_MIN_FPS,
            "--max-gaps", LEVEL2_PROBE_MAX_GAPS,
            "--json",
        ]
        report, passed, reason = None, False, "probe_error"
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=LEVEL2_PROBE_TIMEOUT_S,
                                       check=False)
            report = _last_json_line(completed.stdout)
            passed = (completed.returncode == 0
                      and (report or {}).get("result") == "PASS")
            reason = (report or {}).get("reason") or (
                f"exit_{completed.returncode}")
        except subprocess.TimeoutExpired:
            reason = "timeout"
        except OSError as exc:
            reason = f"probe_error: {exc}"

        previous = self._probes.get(source_id)
        if passed:
            # `ok_streak` do nível 1 já cumpriu o mínimo: preservar (ou subir
            # até o mínimo) evita derrubar a fonte viva por um arredondamento.
            streak = max(previous.ok_streak if previous else 0,
                         self._policy.min_ok_streak)
            self._record_probe(source_id, True,
                               level=observer_policy.LEVEL_MEDIA,
                               reason=reason, ok_streak=streak,
                               now_epoch=now_epoch)
            return
        # DESCRIBE respondia: a fonte segue "viva" no nível 1, mas sem mídia —
        # volta a ser candidata à sonda depois da janela de cooldown da política.
        self._record_probe(
            source_id, True, level=observer_policy.LEVEL_DESCRIBE,
            reason=f"nivel2_fail: {reason}",
            ok_streak=previous.ok_streak if previous else 0,
            now_epoch=now_epoch)

    def _stop_standby(self, decision, now_epoch):
        """Para o pipeline até uma fonte voltar (`STOP_STANDBY`).

        Idempotente no standby: a política devolve `STOP_STANDBY` a cada ciclo
        enquanto nenhuma fonte viva aparece, e repetir `docker stop` de 10 em
        10 s só gasta API e log. `_set_mode` já garante um único
        `standby_entered` por transição.

        Falha do docker.sock (`stop` devolve False) **não** muda o modo: com o
        container possivelmente rodando, "standby" no estado seria mentira e
        esconderia o motivo. O evento `standby_stop_failed` diz o que houve e o
        próximo ciclo tenta de novo.
        """
        if (self._mode == observer_policy.MODE_STANDBY
                and self._app_running is False):
            self._set_action("stop_standby", now_epoch)
            return
        if not self.docker.stop(APP_CONTAINER):
            emit("standby_stop_failed", container=APP_CONTAINER,
                 reason=decision.reason,
                 action="modo mantido; nova tentativa no próximo ciclo")
            self._set_action("stop_standby_failed", now_epoch)
            return
        self._app_running = False
        self._set_mode(observer_policy.MODE_STANDBY, reason=decision.reason,
                       exit_code=self._app_exit_code)
        self._set_action("stop_standby", now_epoch)

    def _engage(self, source_id, *, restart, reason, now_epoch):
        """Liga o pipeline numa fonte e arma o pós-check — sem bloquear o loop.

        Sequência: guarda de precedência -> mutex -> fonte ativa (guardando a
        anterior para rollback) -> start/restart -> `MODE_SWITCHING`. O
        pós-check (frame do processo novo) é do ciclo seguinte: esperar 90 s
        aqui pararia RAM/CUDA e a Regra 1 junto — o loop é a única thread do
        watchdog e não pode ficar cego.

        `restart=True` (`SWITCH_TO`) recria o processo; `restart=False`
        (`START_FROM_STANDBY`) só sobe o container parado. Os dois precisam de
        processo novo por motivos diferentes: na troca é o D4 (nenhum estado
        atravessa fontes), no standby é o processo da fonte que morreu.
        """
        if (ACTIVE_SOURCE_ENV or "").strip():
            # Precedência: `ACTIVE_SOURCE` no ambiente é escolha do operador.
            # Escrever o arquivo aqui não mudaria a fonte do pipeline e o
            # standby ficaria eterno com o estado mentindo que trocou.
            self._set_action("yield_env", now_epoch)
            return
        claimed, _info = claim_switch_marker(SWITCH_MARKER, source_id,
                                             now_epoch)
        if not claimed:
            # Outro dono (script, dashboard, ou o observador do ciclo anterior
            # cujo `kill -9` ainda não expirou): cede, nunca apaga o alheio.
            self._set_action("yield_marker", now_epoch)
            return
        previous = read_active_source(ACTIVE_SOURCE_FILE)
        if not write_active_source(ACTIVE_SOURCE_FILE, source_id):
            release_switch_marker(SWITCH_MARKER, source_id)
            self._set_action("active_source_write_failed", now_epoch)
            return
        ok = (self.docker.restart(APP_CONTAINER) if restart
              else self.docker.start(APP_CONTAINER))
        if not ok:
            # Nada de modo `switching` sem processo novo: o pós-check nunca
            # confirmaria e o rollback seria inútil. Restaura a fonte anterior
            # (é o arquivo lido no boot do pipeline) e mantém o modo atual.
            write_active_source(ACTIVE_SOURCE_FILE, previous)
            release_switch_marker(SWITCH_MARKER, source_id)
            emit("engage_failed", source=source_id, previous_source=previous,
                 reason=("docker_restart_failed" if restart
                         else "docker_start_failed"),
                 action="fonte anterior restaurada; modo mantido")
            self._set_action("engage_failed", now_epoch)
            return
        _, started_at, _, _ = self.docker.container_state(APP_CONTAINER)
        self._expected_started_at = started_at
        deadline = now_epoch + OBSERVER_WAKE_TIMEOUT_S
        self._wake = {
            "source": source_id,
            "previous": previous,
            "deadline": deadline,
            "started_at": started_at,
            "engaged_at": now_epoch,
        }
        self._set_mode(observer_policy.MODE_SWITCHING, reason=reason,
                       source=source_id, previous_source=previous,
                       deadline=_iso_ts(deadline))
        # Nome da ação política, como antes: o PASS/FAIL sobrescreve depois
        # com `engage_ok`/`engage_failed`.
        self._set_action("switch_to" if restart else "start_from_standby",
                         now_epoch)

    def _wake_confirmed(self, wake):
        """A última `interval_metrics` prova que a fonte pedida entrega frame?

        Exige as três coisas do pós-check do `switch_source.sh`: a fonte pedida,
        `fps_in > 0` e o **processo novo**. Sem a terceira, uma troca para a
        mesma fonte aprovaria com a métrica do processo anterior — "PASS na
        fonte errada".
        """
        if self._last_fps_in <= 0:
            return False
        if self._last_metric_source != (wake.get("source") or None):
            return False
        started = _timestamp_epoch(wake.get("started_at") or "")
        if started is None:
            # Sem `StartedAt` (API mudou, container sumiu) não há como comparar
            # processo: vale a fonte + frame, como o script faz sem boundary.
            return True
        epoch = self._last_metric_epoch
        if epoch is None:
            return False
        return epoch >= started - WAKE_EPOCH_TOLERANCE_S

    def _fail_wake(self, wake, now_epoch):
        """Orçamento do pós-check estourou: para, restaura a fonte e dorme.

        Nunca deixar o container rodando numa fonte que não entregou frame: em
        standby o motor TensorRT não fica queimando memória do Jetson (o motivo
        de o observador existir) e a política tenta de novo quando outra fonte
        aparecer. A fonte anterior volta ao arquivo porque a nova não provou
        nada — o dashboard nunca pode mostrar uma fonte que falhou.
        """
        source_id = wake.get("source") or ""
        previous = wake.get("previous") or ""
        self._wake = None
        stop_ok = self.docker.stop(APP_CONTAINER)
        if stop_ok:
            self._app_running = False
        write_active_source(ACTIVE_SOURCE_FILE, previous)
        release_switch_marker(SWITCH_MARKER, source_id)
        self._set_mode(observer_policy.MODE_STANDBY,
                       reason="pos_check_timeout",
                       exit_code=self._app_exit_code)
        emit("engage_failed", source=source_id, previous_source=previous,
             reason="post_check_timeout",
             timeout_s=OBSERVER_WAKE_TIMEOUT_S, stop_ok=stop_ok,
             action="fonte anterior restaurada; standby")
        write_switch_record(source_id=source_id, previous=previous,
                            result="FAIL", reason="post_check_timeout",
                            extra={"origin": "observer"})
        self._set_action("engage_failed", now_epoch)

    def _check_wake(self, now_epoch):
        """Pós-check da transição em curso: PASS (frame novo) ou FAIL (prazo).

        Um desfecho por transição: no PASS libera o marcador e volta a observar;
        no estouro do prazo chama `_fail_wake`. Enquanto o prazo não venceu não
        faz nada — o ciclo seguinte pergunta de novo à telemetria.
        """
        wake = self._wake
        if not wake:
            # `MODE_SWITCHING` sem alvo só existe se o watchdog reiniciou no
            # meio da transição (o estado em memória se perdeu). Sem alvo não há
            # o que provar: volta a observar em vez de travar em `switching`.
            self._set_mode(observer_policy.MODE_WATCHING,
                           reason="transicao_sem_alvo")
            self._set_action("wake_lost", now_epoch)
            return
        source_id = wake.get("source") or ""
        if self._wake_confirmed(wake):
            self._wake = None
            release_switch_marker(SWITCH_MARKER, source_id)
            self._set_mode(observer_policy.MODE_WATCHING,
                           reason="fonte_confirmada", source=source_id)
            emit("engage_ok", source=source_id,
                 previous_source=wake.get("previous"),
                 fps_in=self._last_fps_in,
                 elapsed_s=round(now_epoch - (wake.get("engaged_at")
                                              or now_epoch), 1),
                 action="fonte confirmada com frame")
            write_switch_record(source_id=source_id,
                                previous=wake.get("previous"),
                                result="PASS", reason="fonte_confirmada",
                                extra={"origin": "observer"})
            self._set_action("engage_ok", now_epoch)
            return
        if now_epoch < (wake.get("deadline") or 0.0):
            return
        self._fail_wake(wake, now_epoch)

    def _handle_decision(self, decision, now_epoch):
        """Executa o que é deste ciclo e registra a decisão.

        - `RUN_LEVEL2_PROBE`: roda a confirmação de mídia agora;
        - `STOP_STANDBY`: para o container (idempotente) e entra em standby;
        - `START_FROM_STANDBY`/`SWITCH_TO`: `_engage` escreve a fonte ativa,
          liga o container e arma o pós-check — sem bloquear o loop.

        Em `MODE_SWITCHING` as ações de container são ignoradas: a transição em
        curso é dona do container e do marcador, e repetir start/stop a cada
        10 s é o caminho mais curto para o ping-pong entre fontes.
        """
        action = decision.action
        if action is observer_policy.Action.NONE:
            return
        if action is observer_policy.Action.RUN_LEVEL2_PROBE:
            self._run_level2_probe(decision.source_id, now_epoch)
            self._set_action(action.value, now_epoch)
            return
        if self._mode == observer_policy.MODE_SWITCHING:
            return
        if action is observer_policy.Action.STOP_STANDBY:
            # Parar o pipeline em vazio é o que evita o `restart:
            # unless-stopped` recarregando o engine sem fonte: vale mesmo com
            # a troca automática desligada.
            self._stop_standby(decision, now_epoch)
            return
        if not OBSERVER_AUTO_SWITCH:
            # Knob do operador: a política continua medindo e o modo segue
            # `standby`, mas ninguém religa/troca o container por conta própria.
            self._set_action(action.value, now_epoch)
            return
        if action is observer_policy.Action.SWITCH_TO:
            emit("auto_switch_triggered", source=decision.source_id,
                 previous_source=self._snapshot.active_source,
                 reason=decision.reason)
            self._engage(decision.source_id, restart=True,
                         reason=decision.reason, now_epoch=now_epoch)
            return
        if action is observer_policy.Action.START_FROM_STANDBY:
            self._engage(decision.source_id, restart=False,
                         reason=decision.reason, now_epoch=now_epoch)

    def _observer_state_payload(self, now_epoch):
        """Retrato consumido pelo dashboard (`data/config/observer_state.json`)."""
        snapshot = self._snapshot
        return {
            "ts": _iso_ts(now_epoch),
            "mode": self._mode,
            "mode_reason": self._mode_reason,
            "app_container_running": self._app_running,
            "app_exit_code": self._app_exit_code,
            "active_source": snapshot.active_source,
            "last_action": self._last_action,
            "last_action_ts": self._last_action_ts,
            "sources_probe": {
                source_id: {
                    "alive": probe.alive,
                    "level": probe.level,
                    "reason": probe.reason,
                    "checked_at": (_iso_ts(self._probe_checked_at[source_id])
                                   if source_id in self._probe_checked_at
                                   else None),
                }
                for source_id, probe in sorted(self._probes.items())
            },
        }

    def _write_observer_state(self, now_epoch=None):
        epoch = time.time() if now_epoch is None else now_epoch
        return write_json_atomic(OBSERVER_STATE,
                                 self._observer_state_payload(epoch))

    def _observer_cycle(self):
        """Um ciclo: fontes -> sondas -> decisão -> estado em disco.

        Sem API do Docker não há retrato confiável (`running` desconhecido não
        é "app morto"): as sondas e a decisão são puladas, mas o arquivo de
        estado é reescrito mesmo assim — o dashboard precisa ver o watchdog
        vivo e a última informação conhecida. O `finally` garante a escrita
        única por ciclo, inclusive quando uma ação levanta.
        """
        now_monotonic = time.monotonic()
        now_epoch = time.time()
        try:
            self._snapshot = read_source_snapshot()
            if self._app_running is None:
                return
            if self._mode == observer_policy.MODE_SWITCHING:
                # A transição em curso manda neste ciclo: nada de sonda nova
                # (gastaria mídia numa fonte que o pipeline talvez já esteja
                # usando) nem de decisão da política. Vale também para o ciclo
                # em que o pós-check acabou: a ação seguinte é do próximo
                # ciclo, com o container já estável.
                self._check_wake(now_epoch)
                return
            self._probe_level1(now_monotonic, now_epoch)
            decision = self._policy.decide(
                self._observation(now_monotonic, now_epoch),
                self._policy_state)
            self._handle_decision(decision, now_epoch)
        finally:
            self._write_observer_state()

    # -- loop principal ------------------------------------------------------
    def run(self):
        emit("watchdog_started", container=APP_CONTAINER,
             target_fps=TARGET_FPS, telemetry=TELEMETRY_FILE,
             rules={
                 "frame_timeout_s": RULE1_FRAME_TIMEOUT_S,
                 "escalate_s": RULE1_ESCALATE_S,
                 "crash_loop_window_s": CRASH_LOOP_WINDOW_S,
                 "crash_loop_min_restarts": CRASH_LOOP_MIN_RESTARTS,
                 "fps_degrade_ratio": RULE2_FPS_RATIO,
                 "fps_degrade_window_s": RULE2_WINDOW_S,
                 "rss_growth_ratio": RULE3_RSS_RATIO,
                 "ram_critical_pct": RULE4_RAM_PCT,
             },
             observer={
                 "auto_switch": OBSERVER_AUTO_SWITCH,
                 "source_priority": OBSERVER_SOURCE_PRIORITY,
                 "state": OBSERVER_STATE,
                 "active_source_file": ACTIVE_SOURCE_FILE,
                 "switch_marker": SWITCH_MARKER,
                 "switch_marker_ttl_s": OBSERVER_SWITCH_MARKER_TTL_S,
                 "wake_timeout_s": OBSERVER_WAKE_TIMEOUT_S,
                 "switch_log": SWITCH_LOG,
             })
        while True:
            try:
                for raw in self.tail.poll():
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("event") == "interval_metrics":
                        self._handle_metric(rec)
                self._observe_switch_marker()
                self._rule1_idle()
                self._rule4_ram()
                self._rule5_cuda_logs()
                self._observer_cycle()
            except Exception as exc:  # noqa: BLE001 — watchdog nunca deve morrer
                emit("watchdog_error", error=str(exc))
            time.sleep(CHECK_INTERVAL_S)


def main():
    Watchdog().run()


if __name__ == "__main__":
    sys.exit(main())
