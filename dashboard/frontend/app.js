"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  baseline: null,
  lastMetrics: null,
  lastAreaState: null,
  events: [],
  filter: "all",
  ws: null,
};

const fmtTime = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtClock = (iso) => fmtTime.format(new Date(iso));
const fmtAgo = (sec) => {
  sec = Math.max(0, sec);
  return sec < 90 ? `${Math.round(sec)}s` :
    sec < 5400 ? `${Math.round(sec / 60)}min` :
    `${(sec / 3600).toFixed(1)}h`;
};

function setLamp(id, cls) {
  const el = $(id);
  el.className = "bulb " + cls;
  const meaning = cls.includes("blink") ? "falha" :
    cls === "on" ? "ok" : cls === "warn" ? "atenção" :
    cls === "off" ? "desligado" : "—";
  el.setAttribute("role", "img");
  el.setAttribute("aria-label",
    `lampada ${id.replace("lamp-", "")}: ${meaning}`);
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

/* ---------------- WHEP player ---------------- */

const player = {
  pc: null,
  backoff: 2000,
  playing: false,
  wantPlaying: false,

  async connect() {
    const video = $("video");
    this.wantPlaying = true;
    this.playing = true;  // habilita reconexão automática desde a 1ª tentativa
    const whepUrl =
      localStorage.getItem("whep_url") ||
      `${location.protocol}//${location.hostname}:8889/live/whep`;
    setVideoStatus("negociando webrtc…");
    try {
      this.pc = new RTCPeerConnection();
      // sem transceiver, createOffer() gera SDP sem m=video e o MediaMTX
      // rejeita o offer ("no ice-ufrag") — o vídeo nunca conecta
      this.pc.addTransceiver("video", { direction: "recvonly" });
      this.pc.ontrack = (e) => {
        if (video.srcObject !== e.streams[0]) {
          video.srcObject = e.streams[0];
        }
        setLamp("lamp-video", "on");
        setVideoStatus(null);
        this.backoff = 2000;
      };
      this.pc.onconnectionstatechange = () => {
        const s = this.pc?.connectionState;
        if (s === "failed" || s === "disconnected" || s === "closed") {
          setLamp("lamp-video", "off blink");
          setVideoStatus("stream interrompido — reconectando…");
          this.scheduleReconnect();
        }
      };
      const offer = await this.pc.createOffer();
      await this.pc.setLocalDescription(offer);
      await iceComplete(this.pc);
      const res = await fetch(whepUrl, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: this.pc.localDescription.sdp,
      });
      if (!res.ok) throw new Error(`WHEP ${res.status}`);
      const answer = await res.text();
      await this.pc.setRemoteDescription({ type: "answer", sdp: answer });
      $("btn-play").textContent = "Desconectar";
    } catch (err) {
      console.error(err);
      setVideoStatus("sem stream do mediamtx — tentando de novo…");
      setLamp("lamp-video", "off");
      this.scheduleReconnect();
    }
  },

  scheduleReconnect() {
    if (!this.wantPlaying) return;
    this.disconnect(true);
    this.wantPlaying = true;  // desconexão interna não cancela a intenção
    setTimeout(() => this.connect(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, 30000);
  },

  disconnect(silent) {
    this.playing = false;
    this.wantPlaying = false;
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
    $("video").srcObject = null;
    $("btn-play").textContent = "Conectar";
    if (!silent) {
      setVideoStatus("stream desconectado");
      setLamp("lamp-video", "");
    }
  },
};

function iceComplete(pc, timeoutMs = 1500) {
  return new Promise((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const timer = setTimeout(done, timeoutMs);
    function done() {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    }
    function check() {
      if (pc.iceGatheringState === "complete") done();
    }
    pc.addEventListener("icegatheringstatechange", check);
  });
}

function setVideoStatus(text) {
  const box = $("video-status");
  if (text === null) {
    box.classList.add("hidden");
  } else {
    $("video-status-text").textContent = text;
    box.classList.remove("hidden");
  }
}

$("btn-play").addEventListener("click", () => {
  player.playing ? player.disconnect() : player.connect();
});

/* ---------------- WebSocket ---------------- */

function wsConnect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    state.wsBackoff = 1000;
    $("conn-note").textContent = "websocket conectado";
    $("conn-note").className = "ok";
    setLamp("lamp-ws", "on");
  };

  ws.onmessage = (msg) => {
    let envelope;
    try {
      envelope = JSON.parse(msg.data);
    } catch (err) {
      console.warn("frame WS inválido", err);
      return;
    }
    if (envelope.type === "snapshot") {
      renderSnapshot(envelope.data);
    } else if (envelope.type === "record") {
      ingestRecord(envelope.data);
    }
  };

  ws.onclose = () => {
    $("conn-note").textContent = "backend offline — reconectando…";
    $("conn-note").className = "bad";
    setLamp("lamp-ws", "off blink");
    state.wsBackoff = Math.min((state.wsBackoff ?? 1000) * 2, 30000);
    setTimeout(wsConnect, state.wsBackoff);
  };
}

/* ---------------- dados ---------------- */

function renderSnapshot(snap) {
  // snapshot substitui o buffer local (evita duplicação a cada reconexão WS)
  state.events = [];
  for (let i = snap.events_recent.length - 1; i >= 0; i--) {
    pushEvent(snap.events_recent[i], true);
  }
  renderEvents();
  if (snap.latest_metrics) {
    applyMetrics(snap.latest_metrics);
    drawCharts();
  }
  updateDerived(snap);
}

function ingestRecord(record) {
  const event = record.event || "";
  if (event === "interval_metrics") {
    applyMetrics(record);
    if (!state.history) state.history = [];
    const lastTs = state.history.length
      ? state.history[state.history.length - 1].ts : null;
    if (record.ts !== lastTs) state.history.push(record);
    if (state.history.length > 70) state.history.shift();
    drawCharts();
  } else {
    pushEvent(record);
  }
  if (event === "pipeline_error") {
    setLamp("lamp-pipeline", "off blink");
  } else if (event === "pipeline_playing") {
    setLamp("lamp-pipeline", "on");
  }
}

function rssGrowthMb(m) {
  return state.baseline === null ? null : (m.rss_mb ?? 0) - state.baseline;
}

function rssGrowthBad(growth) {
  return growth !== null && growth > (state.baseline ?? 0) * 0.15 + 1;
}

function applyMetrics(m) {
  state.lastMetrics = m;

  if (state.baseline === null && (m.rss_mb || 0) > 0) {
    state.baseline = m.rss_mb;
  }

  $("r-fps-proc").textContent = (m.fps_processed ?? 0).toFixed(1);
  $("r-fps-prev").textContent = (m.fps_preview ?? 0).toFixed(1);
  $("r-drop").textContent = Math.max(m.preview_dropped_fps ?? 0, 0).toFixed(2);
  $("r-people").textContent = m.persons_max_concurrent ?? 0;

  const area = m.area_state ?? "—";
  const word = $("area-word");
  word.textContent = area.toUpperCase();
  document.querySelector(".area-state")
    .classList.toggle("occupied", area === "OCCUPIED");
  if (m.area_state !== undefined && state.lastAreaState !== area) {
    state.lastAreaState = area;
    state.areaChangedAt = Date.now();
  }
  $("area-since").textContent = state.areaChangedAt
    ? `desde ${new Date(state.areaChangedAt).toLocaleTimeString("pt-BR")}`
    : "";

  $("s-tracks").textContent = m.active_tracks ?? 0;
  $("s-entries").textContent = m.total_entries ?? 0;
  $("s-exits").textContent = m.total_exits ?? 0;
  $("s-dwell").textContent = `${(m.avg_dwell_s ?? 0).toFixed(1)} s`;

  $("h-rss").textContent = `${(m.rss_mb ?? 0).toFixed(1)} mb`;
  const growth = rssGrowthMb(m);
  const growthEl = $("h-growth");
  growthEl.textContent = growth === null ? "—" :
    `${growth >= 0 ? "+" : ""}${growth.toFixed(1)} mb`;
  growthEl.className = rssGrowthBad(growth) ? "bad" : "";
  $("h-proc").textContent = `${m.threads ?? "—"} / ${m.open_fds ?? "—"}`;
  $("h-frames").textContent = (m.frames_total_processed ?? 0).toLocaleString("pt-BR");

  const ageSec = Math.max(0, (Date.now() - new Date(m.ts).getTime()) / 1000);
  $("h-age").textContent = `${fmtAgo(ageSec)} atrás`;
  $("h-age").className = ageSec > 90 ? "bad" : "";
}

function updateDerived(snap) {
  const health = snap.health || {};
  const el = $("h-status");
  el.textContent = health.status ?? "—";
  el.className = { healthy: "ok", degraded: "warn", stale: "warn", down: "bad" }[health.status] ?? "";
  $("h-errors").textContent = snap.error_count ?? 0;
  $("h-errors").className = (snap.error_count ?? 0) > 0 ? "bad" : "";

  if (snap.pipeline_started_at) {
    startUptimeTicker(new Date(snap.pipeline_started_at));
  }
  setLamp("lamp-pipeline",
    snap.health?.status === "healthy" ? "on" :
    snap.health?.status === "down" ? "off blink" : "warn");
  const growth = state.lastMetrics ? rssGrowthMb(state.lastMetrics) : null;
  setLamp("lamp-mem", growth !== null && !rssGrowthBad(growth) ? "on" : "warn");
}

let uptimeTimer = null;
let uptimeStartAt = null;
function startUptimeTicker(startedAt) {
  uptimeStartAt = startedAt;  // atualiza a cada snapshot (pipeline pode reiniciar)
  if (uptimeTimer) return;
  uptimeTimer = setInterval(() => {
    const sec = Math.max(0, (Date.now() - uptimeStartAt.getTime()) / 1000);
    const h = String(Math.floor(sec / 3600)).padStart(2, "0");
    const mn = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
    const s = String(Math.floor(sec % 60)).padStart(2, "0");
    $("uptime").textContent = `${h}:${mn}:${s}`;
  }, 1000);
}

/* ---------------- eventos ---------------- */

const EVENT_LABELS = {
  track_entered: "entrada",
  track_exited: "saída",
  area_transition: "área",
  pipeline_error: "erro",
  pipeline_warning: "aviso",
  pipeline_playing: "pipeline ativo",
  pipeline_stopped: "pipeline parado",
  preview_branch_ready: "preview pronto",
  test_duration_reached: "teste concluído",
  interrupted_by_user: "interrompido",
  eos: "fim de fluxo",
};

function eventClass(event) {
  if (event.includes("error")) return "ev-error";
  if (event === "track_entered") return "ev-entered";
  if (event === "track_exited") return "ev-exited";
  if (event === "area_transition") return "ev-transition";
  return "";
}

function eventDetail(r) {
  switch (r.event) {
    case "track_entered": return `track ${r.track_id}`;
    case "track_exited": return `track ${r.track_id} · dwell ${r.dwell_seconds}s`;
    case "area_transition": return `${r.transition} · ${r.active_tracks} track(s)`;
    case "pipeline_error": return r.error || "";
    case "pipeline_warning": return r.warning || "";
    default:
      return Object.entries(r)
        .filter(([k]) => !["ts", "event"].includes(k))
        .map(([k, v]) => `${k}=${v}`).join(" ");
  }
}

function matchesFilter(r) {
  return state.filter === "all" || r._group === state.filter;
}

function buildEventLi(r) {
  const li = document.createElement("li");
  li.className = r._class;
  const time = document.createElement("time");
  time.textContent = fmtClock(r.ts);
  const name = document.createElement("span");
  name.className = "ev-name";
  name.textContent = EVENT_LABELS[r.event];
  const detail = document.createElement("span");
  detail.className = "ev-detail";
  detail.textContent = eventDetail(r);
  li.append(time, name, detail);
  return li;
}

function pushEvent(record, silent) {
  const evName = record.event || "";
  if (!(evName in EVENT_LABELS)) return;
  record._class = eventClass(evName);
  record._group =
    evName.startsWith("track_") || evName === "area_transition" ? "tracks" :
    evName.includes("error") || evName.includes("warning") ? "errors" : "all";
  state.events.unshift(record);
  if (state.events.length > 500) state.events.pop();
  if (silent) return;
  // inserção incremental: só o nó novo entra no DOM
  if (!matchesFilter(record)) return;
  const list = $("events");
  if (list.querySelector(".events-empty")) list.innerHTML = "";
  list.prepend(buildEventLi(record));
  while (list.children.length > 120) list.lastChild.remove();
}

function renderEvents() {
  const list = $("events");
  list.innerHTML = "";
  const visible = state.events.filter(matchesFilter);
  if (!visible.length) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="events-empty">nenhum evento nesta categoria ainda</span>';
    list.appendChild(li);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const r of visible.slice(0, 120)) {
    frag.appendChild(buildEventLi(r));
  }
  list.appendChild(frag);
}

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((c) => {
      c.classList.remove("active");
      c.setAttribute("aria-pressed", "false");
    });
    chip.classList.add("active");
    chip.setAttribute("aria-pressed", "true");
    state.filter = chip.dataset.filter;
    renderEvents();
  });
});

/* ---------------- charts ---------------- */

function drawLine(canvasId, values, color, fillColor, maxElId) {
  const canvas = $(canvasId);
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * ratio;
  canvas.height = h * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, w, h);

  const pad = { l: 34, r: 8, t: 10, b: 16 };
  const maxV = Math.max(...values, 0.001) * 1.15;
  const minV = Math.min(...values, 0);
  const span = maxV - minV || 1;
  const x = (i) => pad.l + (i / Math.max(values.length - 1, 1)) * (w - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - minV) / span) * (h - pad.t - pad.b);

  ctx.strokeStyle = "#262b31";
  ctx.fillStyle = "#5b626b";
  ctx.font = "9px 'JetBrains Mono', monospace";
  ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const gy = pad.t + (g / 3) * (h - pad.t - pad.b);
    const gv = maxV - (g / 3) * span;
    ctx.beginPath();
    ctx.moveTo(pad.l, gy);
    ctx.lineTo(w - pad.r, gy);
    ctx.stroke();
    ctx.fillText(gv.toFixed(gv < 10 ? 1 : 0), 4, gy + 3);
  }

  if (values.length > 1) {
    ctx.beginPath();
    values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.stroke();

    ctx.lineTo(x(values.length - 1), h - pad.b);
    ctx.lineTo(x(0), h - pad.b);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();
  }

  if (maxElId && values.length) {
    $(maxElId).textContent = `atual ${values[values.length - 1].toFixed(2)}`;
  }
}

function drawCharts() {
  const rows = state.history || [];
  if (rows.length < 2) return;
  drawLine("chart-fps", rows.map((m) => m.fps_processed ?? 0),
    "rgb(255,180,84)", "rgba(255,180,84,0.08)", "fps-max");
  drawLine("chart-rss", rows.map((m) => m.rss_mb ?? 0),
    "rgb(110,168,254)", "rgba(110,168,254,0.08)", "rss-max");
}

/* histórico completo para os gráficos */
async function loadHistory() {
  try {
    state.history = await fetchJson("/api/history?minutes=30");
    drawCharts();
  } catch { /* backend ainda subindo */ }
}

let resizePending = false;
window.addEventListener("resize", () => {
  if (resizePending) return;
  resizePending = true;
  setTimeout(() => {
    resizePending = false;
    drawCharts();
  }, 150);
});

/* ---------------- boot ---------------- */

wsConnect();
loadHistory();
setInterval(loadHistory, 60000);
