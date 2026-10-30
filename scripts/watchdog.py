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

Fontes de dados:
  - data/logs/live.jsonl  (telemetria JSONL do pipeline, seguida em tail)
  - /proc/meminfo         (RAM do sistema)
  - docker.sock           (logs do container + restart)

Sem dependências externas (apenas stdlib).
"""

import datetime
import json
import os
import re
import socket
import sys
import time
from collections import deque

# ---------------------------------------------------------------------------
# Configuração (env)
# ---------------------------------------------------------------------------
TELEMETRY_FILE = os.environ.get(
    "TELEMETRY_FILE", "/app/data/logs/live.jsonl")
WATCHDOG_LOG = os.environ.get(
    "WATCHDOG_LOG", "/app/data/logs/watchdog.jsonl")

APP_CONTAINER = os.environ.get("APP_CONTAINER", "vision-stability-app")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

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
        """(running, StartedAt, uptime_s) do container, ou (None,)*3.

        Usado para distinguir "a fonte parou" de "o processo do pipeline
        reiniciou": depois de um restart externo (`docker restart`, crash,
        recriação) não há frame porque o processo está subindo, e o watchdog
        não deve cobrar isso como fonte morta. Qualquer falha na API devolve
        None e o watchdog volta ao comportamento antigo.
        """
        try:
            status, body = self.request(
                "GET", f"/containers/{container}/json", timeout=5)
        except OSError:
            return None, None, None
        if status != 200:
            return None, None, None
        try:
            state = json.loads(body).get("State") or {}
        except (json.JSONDecodeError, AttributeError):
            return None, None, None
        started = state.get("StartedAt") or ""
        try:
            # `StartedAt` fora do formato ISO (`nada`, `2026-13-45T99:99:99Z`)
            # levantava ValueError daqui: escapava de `container_state`, caía
            # no `except Exception` do loop (`watchdog_error` a cada poll) e
            # engolia `_rule4_ram()`/`_rule5_cuda_logs()` daquele ciclo.
            uptime = _age_seconds(started)
        except (ValueError, OverflowError):
            uptime = None
        return bool(state.get("Running")), started or None, uptime

    def restart(self, container, timeout_s=10):
        status, _ = self.request(
            "POST", f"/containers/{container}/restart?t={timeout_s}",
            timeout=timeout_s + 30)
        return status in (204, 200)


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


def _age_seconds(iso_ts, now_epoch=None):
    """Idade, em segundos, de um timestamp ISO-8601 do Docker.

    O Docker devolve nanossegundos (`...16:59:23.123456789Z`) e `strptime`
    só aceita microssegundos — daí o parse manual da fração.

    Nunca levanta: `StartedAt` malformado devolve None (o watchdog volta ao
    comportamento antigo, sem carência) e idade negativa — relógio do host
    andou para trás depois do start do container — é tratada como 0, o que dá
    a mesma carência de um processo recém-subido.
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
    agora = time.time() if now_epoch is None else now_epoch
    idade = agora - base - fracao
    return round(idade, 1) if idade > 0 else 0.0


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
        running, started_at, uptime = self.docker.container_state(APP_CONTAINER)
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

    def _register_restart_event(self, now, started_at, uptime, running):
        """Anota o restart na janela e emite `container_restarted` no máximo 1x.

        Em crash-loop o processo troca a cada poll de 10 s: emitir uma linha
        por troca enchia o log de ruído e escondia o evento que importa
        (`crash_loop_detected`). Os restarts continuam contados no deque.
        """
        events = self._restart_events
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
             grace_s=STARTUP_GRACE_S)

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

    def _rule1_idle(self):
        now = time.monotonic()
        # Antes da carência: o StartedAt do processo precisa ser observado
        # mesmo durante ela, senão um crash-loop passaria invisível.
        uptime = self._observe_container_start()
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
                self._rule1_idle()
                self._rule4_ram()
                self._rule5_cuda_logs()
            except Exception as exc:  # noqa: BLE001 — watchdog nunca deve morrer
                emit("watchdog_error", error=str(exc))
            time.sleep(CHECK_INTERVAL_S)


def main():
    Watchdog().run()


if __name__ == "__main__":
    sys.exit(main())
