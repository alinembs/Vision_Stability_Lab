#!/usr/bin/env python3
"""Watchdog externo do Vision Stability Lab.

Roda FORA do processo do pipeline (container próprio com acesso ao
docker.sock). Um while True dentro do pipeline não protege contra
travamentos nativos, erros de driver ou deadlocks — este processo sim,
pois observa o sistema por fora e pode reiniciar o container.

Regras implementadas (spec Fase 6):
  1. Sem frame por 30s            -> evento no_frames_detected
     (a reconexão RTSP em si acontece dentro do pipeline; se o quadro
      não voltar em NO_FRAME_ESCALATE_S, reinicia o container)
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

RULE1_FRAME_TIMEOUT_S = float(os.environ.get("FRAME_TIMEOUT_S", "30"))
RULE1_ESCALATE_S = float(os.environ.get("NO_FRAME_ESCALATE_S", "180"))

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

    def restart(self, container, timeout_s=10):
        status, _ = self.request(
            "POST", f"/containers/{container}/restart?t={timeout_s}",
            timeout=timeout_s + 30)
        return status in (204, 200)


# ---------------------------------------------------------------------------
# Tail simples de JSONL (acompanha rotação/truncamento)
# ---------------------------------------------------------------------------
class JsonlTail:
    def __init__(self, path):
        self.path = path
        self.fh = None
        self.inode = None

    def poll(self):
        """Retorna as novas linhas desde a última chamada."""
        if self.fh is None:
            try:
                self.fh = open(self.path)
                self.inode = os.fstat(self.fh.fileno()).st_ino
                self.fh.seek(0, os.SEEK_END)  # ignora histórico
            except OSError:
                return []
        new = []
        for line in self.fh:
            new.append(line.rstrip("\n"))
        # arquivo truncado/substituído -> reabrir
        try:
            if os.stat(self.path).st_ino != self.inode:
                self.fh.close()
                self.fh = None
        except OSError:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
        return new


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
        self.no_frame_reported = False

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
            self.low_fps_since = None
            self.degrade_reported = False
            self.rss_baseline_mb = None
            self.ram_high_count = 0
            self.log_since_epoch = time.time()

    # -- telemetria ----------------------------------------------------------
    def _handle_metric(self, r):
        now = time.monotonic()

        # Regra 1 — fluxo de frames (frames_total_in é cumulativo)
        frames_total = r.get("frames_total_in") or 0
        fps_in = r.get("fps_in") or 0
        if (frames_total > getattr(self, "_prev_frames_total", -1)
                and frames_total > 0) or fps_in > 0:
            if frames_total > getattr(self, "_prev_frames_total", -1):
                self.last_frame_at = now
                if self.no_frame_reported:
                    emit("frames_recovered")
                    self.no_frame_reported = False
        self._prev_frames_total = max(frames_total,
                                      getattr(self, "_prev_frames_total", 0))

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
    def _rule1_idle(self):
        now = time.monotonic()
        if now - self.started_at < STARTUP_GRACE_S:
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
