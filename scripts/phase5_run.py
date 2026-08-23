#!/usr/bin/env python3
"""Harness da Fase 5 — testes de estabilidade do Vision Stability Lab.

Uso (no host/Jetson):
    python3 scripts/phase5_run.py baseline            # 30 min
    python3 scripts/phase5_run.py longrun             # 6 h
    python3 scripts/phase5_run.py viewer_slow         # 1 h
    python3 scripts/phase5_run.py dashboard_off       # 1 h
    python3 scripts/phase5_run.py rtsp_interrupt      # 30 min (requer sudo)
    python3 python3 scripts/phase5_run.py restart_cycles  # 3 ciclos

Cada teste captura tegrastats, fatia do live.jsonl e logs dos containers,
e grava em data/logs/phase5/:
    <nome>-tegrastats.log   bruto do tegrastats (1 Hz)
    <nome>-summary.json     métricas agregadas
    <nome>-report.md        relatório legível
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "logs" / "phase5"
LIVE = ROOT / "data" / "logs" / "live.jsonl"

DURATIONS_S = {
    "baseline": 30 * 60,
    "longrun": 6 * 3600,
    "viewer_slow": 60 * 60,
    "dashboard_off": 60 * 60,
    "rtsp_interrupt": 30 * 60,
}

CAMERA_HOST = "192.168.59.108"
RTSP_URL = "rtsp://127.0.0.1:8554/live"


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class TegrastatsCapture:
    def __init__(self, path):
        self.path = path
        self.proc = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        self.proc = subprocess.Popen(
            ["tegrastats", "--interval", "1000"],
            stdout=self.fh, stderr=subprocess.DEVNULL)
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.fh.close()


def parse_ts(ts):
    """Converte ts ISO (UTC com Z/offset ou local) em datetime comparável."""
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def jsonl_slice(start_ts):
    """Registros de interval_metrics com ts >= start (comparação por datetime)."""
    if not LIVE.exists():
        return []
    start_dt = parse_ts(start_ts)
    out = []
    for line in LIVE.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") != "interval_metrics":
            continue
        try:
            if parse_ts(r.get("ts")) >= start_dt:
                out.append(r)
        except ValueError:
            continue
    return out


TEGRA_PATTERNS = {
    "ram_used_mb": re.compile(r"RAM (\d+)/(\d+)MB"),
    "gpu_percent": re.compile(r"GR3D_FREQ (\d+)%"),
    "cpu_percent": re.compile(r"CPU \[(\d+)%@\d+"),
    "temp_gpu_c": re.compile(r"gpu@(\d+(?:\.\d+)?)C"),
    "temp_tj_c": re.compile(r"tj@(\d+(?:\.\d+)?)C"),
    "power_mw": re.compile(r"VDD_IN (\d+)mW"),
}


def parse_tegrastats(path):
    rows = {k: [] for k in TEGRA_PATTERNS}
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        for key, pat in TEGRA_PATTERNS.items():
            m = pat.search(line)
            if m:
                rows[key].append(float(m.group(1)))
    return rows


def container_errors(since_iso, container="vision-stability-app"):
    """Erros GStreamer/CUDA nos logs do container desde since_iso."""
    res = {"gst": [], "cuda": []}
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", since_iso, container],
            capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        res["error"] = str(e)
        return res
    for line in out.splitlines():
        low = line.lower()
        if "cuda" in low and ("err" in low or "fail" in low):
            res["cuda"].append(line.strip()[:200])
        elif ("error" in low or "warn" in low) and "gst" in low:
            res["gst"].append(line.strip()[:200])
        elif '"pipeline_error"' in line:
            res["gst"].append(line.strip()[:200])
    return res


def pct_stats(values):
    vals = sorted(values)
    if not vals:
        return None
    n = len(vals)
    return {
        "min": vals[0], "max": vals[-1],
        "avg": round(sum(vals) / n, 2),
        "p95": vals[int(n * 0.95)],
        "n": n,
    }


def build_summary(name, started_iso, ended_iso, extra=None):
    metrics = jsonl_slice(started_iso)
    teg = parse_tegrastats(OUT / f"{name}-tegrastats.log")
    errs = container_errors(started_iso)

    # reconexões RTSP: erros de conexão seguidos de recuperação de FPS
    reconnects, recoveries = [], []
    prev_fps_zero_at = None
    for r in metrics:
        fps = r.get("fps_processed") or 0
        if fps <= 0:
            if prev_fps_zero_at is None:
                prev_fps_zero_at = r["ts"]
        else:
            if prev_fps_zero_at is not None:
                t0 = datetime.datetime.fromisoformat(prev_fps_zero_at.replace("Z", "+00:00"))
                t1 = datetime.datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                recoveries.append(round((t1 - t0).total_seconds(), 1))
                prev_fps_zero_at = None
    error_events = [r for r in metrics if False]  # eventos não-métricos ficam fora do slice
    # conta erros de conexão nos registros não-métricos do período
    start_dt = parse_ts(started_iso)
    conn_errors = 0
    if LIVE.exists():
        for line in LIVE.read_text().splitlines():
            if '"interval_metrics"' in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                if parse_ts(r.get("ts")) < start_dt:
                    continue
            except ValueError:
                continue
            if r.get("event") == "pipeline_error":
                conn_errors += 1

    summary = {
        "test": name,
        "started_at": started_iso,
        "ended_at": ended_iso,
        "duration_s": DURATIONS_S.get(name),
        "metrics_samples": len(metrics),
        "fps_processed": pct_stats([m["fps_processed"] for m in metrics]),
        "fps_preview": pct_stats([m["fps_preview"] for m in metrics if m.get("fps_preview") is not None]),
        "preview_dropped_fps_max": max((m.get("preview_dropped_fps") or 0) for m in metrics) if metrics else None,
        "rss_mb": pct_stats([m["rss_mb"] for m in metrics]),
        "memory_growth_final_mb": metrics[-1].get("memory_growth_mb") if metrics else None,
        "active_tracks_max": max((m.get("active_tracks") or 0) for m in metrics) if metrics else None,
        "queue_analyze_buffers_max": max((m.get("queue_buffers", {}).get("queue_analyze") or 0) for m in metrics) if metrics else None,
        "queue_preview_buffers_max": max((m.get("queue_buffers", {}).get("queue_preview") or 0) for m in metrics) if metrics else None,
        "jetson_ram_used_mb": pct_stats(teg["ram_used_mb"]),
        "gpu_percent": pct_stats(teg["gpu_percent"]),
        "cpu_percent": pct_stats(teg["cpu_percent"]),
        "temp_gpu_c": pct_stats(teg["temp_gpu_c"]),
        "temp_tj_c": pct_stats(teg["temp_tj_c"]),
        "power_mw": pct_stats(teg["power_mw"]),
        "reconnection_errors": conn_errors,
        "recovery_times_s": recoveries,
        "gstreamer_cuda_errors": errs,
        **(extra or {}),
    }
    return summary


def write_report(s):
    md = [f"# Fase 5 — Teste `{s['test']}`",
          f"- Início: {s['started_at']}",
          f"- Fim: {s['ended_at']}",
          f"- Amostras de métricas: {s['metrics_samples']}", ""]
    for key, label in [
            ("fps_processed", "FPS processado"), ("fps_preview", "FPS preview"),
            ("rss_mb", "RSS (MB)"), ("jetson_ram_used_mb", "RAM Jetson usada (MB)"),
            ("gpu_percent", "GPU %"), ("cpu_percent", "CPU %"),
            ("temp_gpu_c", "Temp GPU °C"), ("temp_tj_c", "Temp TJ °C"),
            ("power_mw", "Potência (mW)")]:
        st = s.get(key)
        if st:
            md.append(f"- **{label}:** avg {st['avg']} · min {st['min']} · max {st['max']} · p95 {st['p95']}")
    for key, label in [("preview_dropped_fps_max", "Máx frames preview descartados/s"),
                       ("active_tracks_max", "Máx tracks ativos"),
                       ("queue_analyze_buffers_max", "Fila análise máx (buffers)"),
                       ("queue_preview_buffers_max", "Fila preview máx (buffers)"),
                       ("memory_growth_final_mb", "Crescimento RSS final (MB)"),
                       ("reconnection_errors", "Erros de reconexão"),
                       ("recovery_times_s", "Tempos de recuperação (s)")]:
        if s.get(key) is not None:
            md.append(f"- **{label}:** {s[key]}")
    errs = s.get("gstreamer_cuda_errors", {})
    if errs.get("gst") or errs.get("cuda"):
        md.append(f"\n## Erros\n\n### GStreamer ({len(errs.get('gst', []))})\n```\n" +
                  "\n".join(errs.get("gst", [])[:20]) + "\n```")
        md.append(f"### CUDA ({len(errs.get('cuda', []))})\n```\n" +
                  "\n".join(errs.get("cuda", [])[:20]) + "\n```")
    (OUT / f"{s['test']}-report.md").write_text("\n".join(md) + "\n")


def run_capture(test, seconds, action=None):
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[{now()}] {test}: início, duracao={seconds}s", flush=True)
    started_iso = now()
    with TegrastatsCapture(OUT / f"{test}-tegrastats.log"):
        if action:
            action()
        deadline = time.time() + seconds
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            print(f"\r[{test}] restam {remaining//60}min{remaining%60:02d}s ", end="", flush=True)
            time.sleep(min(30, remaining))
    print(f"\n[{now()}] {test}: coleta encerrada", flush=True)
    ended_iso = now()
    summary = build_summary(test, started_iso, ended_iso)
    (OUT / f"{test}-summary.json").write_text(json.dumps(summary, indent=2))
    write_report(summary)
    print(f"[{now()}] {test}: summary/report gravados em {OUT}", flush=True)


# ---------------- ações específicas ----------------

def act_viewer_slow():
    """Cliente 'lento': lê o RTSP com latência grande e sink sincronizado."""
    cmd = ["gst-launch-1.0", "-v", "rtspsrc",
           f"location={RTSP_URL}", "latency=3000", "protocols=tcp",
           "!", "decodebin", "!", "fakesink", "sync=true"]
    log = open(OUT / "viewer_slow-client.log", "w")
    subprocess.Popen(cmd, stdout=log, stderr=log)
    print("[viewer_slow] cliente lento iniciado (latency=3000, sync=true)")


def act_dashboard_off():
    subprocess.run(["docker", "stop", "vision-dashboard"], check=True)


def act_rtsp_interrupt():
    """Bloqueia a câmera por 15 min no meio do teste (metade da duração).

    Requer sudo sem senha ou execução prévia de:
      sudo iptables -I OUTPUT -d <camera> -j DROP
    O harness apenas dispara os comandos; ajuste se necessário.
    """
    half = DURATIONS_S["rtsp_interrupt"] // 2

    def block_then_unblock():
        time.sleep(half)
        subprocess.run(["sudo", "-n", "iptables", "-I",
                        "OUTPUT", "-d", CAMERA_HOST, "-j", "DROP"])
        print(f"\n[rtsp_interrupt] câmera BLOQUEADA ({CAMERA_HOST})", flush=True)
        time.sleep(half // 3)  # ~10 min sem câmera
        subprocess.run(["sudo", "-n", "iptables", "-D",
                        "OUTPUT", "-d", CAMERA_HOST, "-j", "DROP"])
        print(f"\n[rtsp_interrupt] câmera LIBERADA — medindo recuperação", flush=True)

    import threading
    threading.Thread(target=block_then_unblock, daemon=True).start()


TESTS = {
    "baseline": dict(seconds=DURATIONS_S["baseline"]),
    "longrun": dict(seconds=DURATIONS_S["longrun"]),
    "viewer_slow": dict(seconds=DURATIONS_S["viewer_slow"], action=act_viewer_slow),
    "dashboard_off": dict(seconds=DURATIONS_S["dashboard_off"], action=act_dashboard_off),
    "rtsp_interrupt": dict(seconds=DURATIONS_S["rtsp_interrupt"], action=act_rtsp_interrupt),
}


def test_restart_cycles(cycles=3):
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    started_iso = now()
    with TegrastatsCapture(OUT / "restart_cycles-tegrastats.log"):
        for i in range(1, cycles + 1):
            pre = len(LIVE.read_text().splitlines()) if LIVE.exists() else 0
            t0 = time.time()
            subprocess.run(["docker", "restart", "vision-stability-app"], check=True)
            print(f"\n[ciclo {i}] container reiniciado; aguardando métricas…", flush=True)
            recovered = None
            while time.time() - t0 < 180:
                time.sleep(5)
                lines = LIVE.read_text().splitlines()[pre:] if LIVE.exists() else []
                for line in lines:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("event") == "interval_metrics" and (r.get("fps_processed") or 0) > 0:
                        recovered = round(time.time() - t0, 1)
                        break
                if recovered:
                    break
            results.append({"cycle": i, "recovery_s": recovered})
            print(f"[ciclo {i}] recuperação: {recovered}s", flush=True)
            if i < cycles:
                time.sleep(120)  # 2 min entre ciclos
    summary = {
        "test": "restart_cycles", "started_at": started_iso, "ended_at": now(),
        "cycles": results,
        "recoveries_ok": all(r["recovery_s"] for r in results),
    }
    (OUT / "restart_cycles-summary.json").write_text(json.dumps(summary, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("test", choices=list(TESTS) + ["restart_cycles"])
    args = p.parse_args()
    if args.test == "restart_cycles":
        test_restart_cycles()
        return
    cfg = TESTS[args.test]
    run_capture(args.test, cfg["seconds"], cfg.get("action"))


if __name__ == "__main__":
    main()
