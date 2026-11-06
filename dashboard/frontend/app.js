"use strict";

const $ = (id) => document.getElementById(id);

// Paleta do canvas: lida de style.css para que os tokens continuem sendo a
// única fonte de verdade das cores. Mudar --accent no CSS muda o gráfico.
const tokenCache = new Map();
function token(name) {
  if (!tokenCache.has(name)) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    tokenCache.set(name, v);
  }
  return tokenCache.get(name);
}
const withAlpha = (hex, alpha) => {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
};

/* ---------------- tema (claro por padrão, escuro opcional) ---------------- */

const THEME_KEY = "vsl-theme";

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** Rótulo do botão e `theme-color` acompanham o tema; o resto é CSS. */
function syncThemeChrome() {
  const dark = currentTheme() === "dark";
  const btn = $("btn-theme");
  if (btn) {
    btn.textContent = dark ? "tema claro" : "tema escuro";
    btn.setAttribute("aria-pressed", String(dark));
    btn.title = dark ? "voltar ao tema claro" : "usar o tema escuro";
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = token("--bg");
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "dark" ? "dark" : "light";
  try {
    localStorage.setItem(THEME_KEY, currentTheme());
  } catch {
    // aba privada com storage bloqueado: o tema vale só para esta sessão
  }
  // o canvas lê os tokens do CSS: sem limpar o cache ele fica na paleta antiga
  tokenCache.clear();
  syncThemeChrome();
  drawCharts();
}

const state = {
  baseline: null,
  lastMetrics: null,
  lastAreaState: null,
  events: [],
  filter: "all",
  ws: null,
  // E4: catálogo de fontes e qual fonte o operador está olhando.
  // `selectedSource === null` significa "seguir a fonte ativa"; ao clicar num
  // cartão a seleção fica fixa e os gráficos/eventos passam a ser daquela fonte.
  sources: [],
  selectedSource: null,
  activeSource: null,
  // E4-T3: troca da fonte ativa pela tela. `token` vive só em `sessionStorage`
  // (aba atual, sem persistir no disco) e nunca é renderizado.
  sw: {
    enabled: false,
    reason: null,
    status: null,
    foreign: null,
    token: null,
    pending: null,
    timer: null,
  },
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

/* ---------------- diagnóstico do WHEP ---------------- */

/**
 * True se a URL responde a qualquer coisa (inclusive 404/401).
 *
 * `mode: "no-cors"` é o ponto: sem ele uma recusa de CORS vira `TypeError`
 * igual a certificado inválido e o diagnóstico perderia o sentido. Em no-cors
 * a resposta é opaca, mas a requisição só falha por rede/TLS — exatamente a
 * separação que interessa aqui. `/` na 8889 responde 404 e isso já prova TLS.
 */
async function probeReachable(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  try {
    await fetch(url, { mode: "no-cors", cache: "no-store", signal: controller.signal });
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Por que o WHEP falhou: "tls" (CA não confiável), "offline" (host fora) ou
 * "reachable" (o servidor está de pé; o problema é a sessão).
 *
 * O navegador não expõe o motivo de um `fetch` recusado — certificado inválido
 * e host fora do ar dão o mesmo `TypeError`. A separação sai comparando
 * esquemas no MESMO host: se o HTTPS da 8889 (WebRTC, desde o E0-T4) não
 * responde e o HTTP da 8888 (HLS) responde, o host está de pé e o que falta é
 * confiança no certificado. Sem esse diagnóstico o player anunciava "sem
 * stream do mediamtx" com o MediaMTX `healthy` — inversão de causa que custou
 * horas de bancada.
 */
async function diagnoseWhepFailure(host) {
  if (await probeReachable(`https://${host}:8889/`)) return "reachable";
  if (await probeReachable(`http://${host}:8888/live/`)) return "tls";
  return "offline";
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
      // HTTPS desde o E0-T4: o MediaMTX subiu com `webrtcEncryption: yes` e a
      // 8889 não fala mais HTTP puro. Se o navegador que abre este dashboard
      // não confiar na CA interna, o `fetch` falha com erro de certificado —
      // instale `deploy/certs/ca.crt` na máquina (ou use o atalho
      // `localStorage.whep_url` com uma origem já confiável).
      `https://${location.hostname}:8889/live/whep`;
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
      // Diagnóstico antes da mensagem: "sem stream" com o MediaMTX saudável é
      // o sintoma de CA não instalada neste navegador (o `fetch` da 8889 falha
      // na verificação do certificado, não por servidor fora).
      setVideoStatus("verificando o certificado do mediamtx…");
      const diag = await diagnoseWhepFailure(location.hostname);
      if (diag === "tls") {
        setVideoStatus(
          "certificado do MediaMTX não é confiável neste navegador — " +
          "instale deploy/certs/ca.crt na máquina que abre o dashboard " +
          "(README, seção do dashboard)");
      } else if (diag === "reachable") {
        setVideoStatus(
          "mediamtx respondeu, mas a sessão WHEP falhou — confira o path /live " +
          "e o log do vision-mediamtx");
      } else {
        setVideoStatus("sem stream do mediamtx — tentando de novo…");
      }
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

/* ---------------- tela cheia do vídeo ----------------
 *
 * O alvo é o cartão inteiro, não o `<video>`: assim o cabeçalho (e o próprio
 * botão de sair) continua na tela. A moldura preenche o resto via CSS
 * (`:fullscreen` em style.css) e o `object-fit: contain` do vídeo cuida da
 * proporção. No iOS, onde `requestFullscreen` não vale para `<div>`, cai no
 * fullscreen nativo do elemento de vídeo.
 */

// iOS: o fullscreen é do <video> e não passa pela Fullscreen API do documento
let iosFullscreen = false;

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement ||
    (iosFullscreen ? $("video") : null);
}

function syncFullscreenUi() {
  const ativo = !!fullscreenElement();
  const btn = $("btn-full");
  if (!btn) return;
  btn.textContent = ativo ? "sair" : "tela cheia";
  btn.title = ativo ? "sair da tela cheia (Esc)" : "tela cheia (F)";
  btn.setAttribute("aria-pressed", String(ativo));
}

async function toggleFullscreen() {
  const card = $("panel-video");
  try {
    if (fullscreenElement()) {
      if (iosFullscreen) $("video").webkitExitFullscreen();
      else if (document.exitFullscreen) await document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    } else if (card.requestFullscreen) {
      await card.requestFullscreen();
    } else if (card.webkitRequestFullscreen) {
      card.webkitRequestFullscreen();
    } else if ($("video").webkitEnterFullscreen) {
      $("video").webkitEnterFullscreen();  // iOS: só o <video> aceita
    }
  } catch (err) {
    // política do navegador (iframe sem allowfullscreen) ou gesto inválido
    console.warn("tela cheia recusada pelo navegador", err);
  }
  syncFullscreenUi();
}

$("btn-full").addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", syncFullscreenUi);
document.addEventListener("webkitfullscreenchange", syncFullscreenUi);
// iOS: o fullscreen é do <video> e não passa pela Fullscreen API do documento
$("video").addEventListener("webkitbeginfullscreen", () => {
  iosFullscreen = true;
  syncFullscreenUi();
});
$("video").addEventListener("webkitendfullscreen", () => {
  iosFullscreen = false;
  syncFullscreenUi();
});

// atalho `F`, ignorado enquanto o operador digita (token, path da captura…)
document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (ev.key !== "f" && ev.key !== "F") return;
  const alvo = ev.target;
  const digitando = alvo && (alvo.tagName === "INPUT" ||
    alvo.tagName === "TEXTAREA" || alvo.tagName === "SELECT" ||
    alvo.isContentEditable);
  if (digitando) return;
  ev.preventDefault();
  toggleFullscreen();
});

// sem suporte (iframe sem allowfullscreen e sem o fallback do iOS): não anuncia
if (!document.fullscreenEnabled && !$("panel-video").webkitRequestFullscreen &&
    !$("video").webkitEnterFullscreen) {
  $("btn-full").hidden = true;
}

/* ---------------- fontes (E4) ---------------- */

/**
 * Ícones das três fontes do ADR-0006 (câmera, drone, celular).
 *
 * SVG inline, `currentColor`, sem build e sem dependência externa — a SPA é
 * vanilla. `default` cobre id novo em `sources.yaml` (a etapa de path
 * editável vai permitir isso sem tocar aqui).
 */
const SOURCE_ICONS = {
  camera: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="7.5" width="11" height="9" rx="1.6"/><circle cx="8" cy="12" r="2"/><path d="m13.5 10.5 6-2.5v8l-6-2.5"/><path d="M6 16.5V19h4"/></svg>`,
  drone: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9.4" y="9.4" width="5.2" height="5.2" rx="1.4"/><path d="m9.4 9.4-3-3M14.6 9.4l3-3M9.4 14.6l-3 3M14.6 14.6l3 3"/><circle cx="4.6" cy="4.6" r="2.1"/><circle cx="19.4" cy="4.6" r="2.1"/><circle cx="4.6" cy="19.4" r="2.1"/><circle cx="19.4" cy="19.4" r="2.1"/></svg>`,
  phone: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M10.6 18.6h2.8"/><circle cx="12" cy="5.6" r="0.9"/></svg>`,
  default: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 8.5v7M8.5 12h7"/></svg>`,
};

function sourceIcon(source) {
  const haystack = `${source.id || ""} ${source.name || ""}`.toLowerCase();
  for (const key of Object.keys(SOURCE_ICONS)) {
    if (key !== "default" && haystack.includes(key)) return SOURCE_ICONS[key];
  }
  return SOURCE_ICONS.default;
}

function sourceKey(record) {
  return record.source_id || "legacy";
}

function sourceById(id) {
  return state.sources.find((s) => s.id === id) || null;
}

/** Fonte que os gráficos e a lista de eventos estão mostrando. */
function viewedSourceId() {
  return state.selectedSource ?? state.activeSource;
}

/** Path é somente leitura: mostra a URI mascarada ou de qual env ela vem. */
function sourcePathText(source) {
  if (source.path) return source.path;
  if (source.path_env) return `${source.path_env} (deploy/.env)`;
  return source.path_origin === "telemetria" ? "sem path no config" : "—";
}

function fmtEpoch(epoch) {
  if (!epoch) return "—";
  const date = new Date(epoch);
  return Number.isNaN(date.getTime()) ? String(epoch) : fmtClock(epoch);
}

function sourceMetaText(source) {
  const session = source.session;
  if (!session) return "sem sessão registrada";
  const samples = session.points === 1 ? "1 amostra" : `${session.points} amostras`;
  const sinceMs = session.since ? new Date(session.since).getTime() : NaN;
  const uptime = Number.isNaN(sinceMs)
    ? "—"
    : `há ${fmtAgo((Date.now() - sinceMs) / 1000)}`;
  // o cartão tem 248px de largura: "desde"/"epoch" (quase sempre o mesmo
  // instante) ocupavam a linha toda e cortavam a contagem de amostras.
  // O detalhe completo continua no `title` do elemento.
  return `${uptime} · ${samples}`;
}

function renderSources() {
  const box = $("sources");
  // o painel é re-renderizado a cada atualização: devolve o foco ao mesmo
  // cartão para quem navega por teclado não perder a posição
  const focused = document.activeElement?.dataset?.source;
  box.innerHTML = "";
  if (!state.sources.length) {
    const note = document.createElement("p");
    note.className = "sources-empty";
    note.textContent = "catálogo de fontes indisponível — mostrando só o que a telemetria traz";
    box.appendChild(note);
    return;
  }

  const viewed = viewedSourceId();
  const switching = state.sw.status?.state === "running";
  const frag = document.createDocumentFragment();
  for (const source of state.sources) {
    const card = document.createElement("div");
    card.className = "source-card";
    if (source.id === viewed) card.classList.add("selected");
    if (source.active) card.classList.add("is-active");
    if (source.enabled === false) card.classList.add("is-disabled");

    // "ver" (gráfico/eventos) é um botão; "ativar" é outro — irmãos, nunca
    // aninhados: dois alvos distintos e navegáveis por teclado.
    const view = document.createElement("button");
    view.type = "button";
    view.className = "source-view";
    view.dataset.source = source.id;
    view.setAttribute("aria-pressed", String(source.id === viewed));
    view.title = `ver telemetria de ${source.name}`;

    const icon = document.createElement("span");
    icon.className = "source-icon";
    icon.innerHTML = sourceIcon(source);

    const body = document.createElement("span");
    body.className = "source-body";

    const line = document.createElement("span");
    line.className = "source-line";
    const name = document.createElement("span");
    name.className = "source-name";
    name.textContent = source.name;
    line.appendChild(name);
    if (source.active) {
      const badge = document.createElement("span");
      badge.className = "source-badge";
      badge.textContent = "ativa";
      line.appendChild(badge);
    }

    const id = document.createElement("span");
    id.className = "source-id";
    id.textContent = `${source.id}${source.codec ? ` · ${source.codec}` : ""}`;

    const path = document.createElement("span");
    path.className = "source-path";
    path.textContent = sourcePathText(source);
    path.title = path.textContent;

    const meta = document.createElement("span");
    meta.className = "source-meta";
    meta.textContent = sourceMetaText(source);
    meta.title = source.session
      ? `${source.session.points ?? 0} amostras · desde ${fmtEpoch(source.session.since)}` +
        ` · epoch ${fmtEpoch(source.session.session_epoch)}`
      : "sem sessão registrada";

    body.append(line, id, path, meta);
    view.append(icon, body);
    view.addEventListener("click", () => selectSource(source.id));
    card.appendChild(view);

    // troca disponível só para fonte do catálogo, habilitada e não ativa
    const canActivate = state.sw.enabled && source.enabled !== false &&
      source.in_catalog !== false && !source.active;
    if (canActivate) {
      const activate = document.createElement("button");
      activate.type = "button";
      activate.className = "source-activate";
      activate.dataset.activate = source.id;
      activate.textContent = switching ? "…" : "ativar";
      activate.disabled = switching;
      activate.title = `tornar ${source.name} a fonte ativa do pipeline`;
      activate.addEventListener("click", () => requestSwitch(source));
      card.appendChild(activate);
    }
    frag.appendChild(card);
  }
  box.appendChild(frag);
  if (focused) {
    box.querySelector(`[data-source="${focused}"]`)?.focus();
  }
}

function updateChartTitles() {
  const source = sourceById(viewedSourceId());
  const label = source ? source.name : "fonte ativa";
  $("chart-fps-title").textContent = `fps · ${label} · últimos 30 min`;
  $("chart-rss-title").textContent = `rss mb · ${label} · últimos 30 min`;
  $("top-source").textContent = label;
}

async function refreshSources() {
  try {
    state.sources = await fetchJson("/api/sources");
  } catch {
    return;  // backend subindo: mantém os cartões que já estão na tela
  }
  const active = state.sources.find((s) => s.active);
  state.activeSource = active ? active.id : null;
  renderSources();
  updateChartTitles();
}

function selectSource(id) {
  state.selectedSource = id;
  $("btn-follow").hidden = false;
  renderSources();
  updateChartTitles();
  loadHistory();
  loadEvents();
}

$("btn-follow").addEventListener("click", () => {
  state.selectedSource = null;
  $("btn-follow").hidden = true;
  renderSources();
  updateChartTitles();
  loadHistory();
  loadEvents();
});

/* ---------------- troca da fonte ativa (E4-T3) ----------------
 *
 * O dashboard pede a troca; quem troca é o backend, com o mesmo contrato do
 * `scripts/switch_source.sh`: pré-check obrigatório, `active_source` escrito só
 * depois de aprovado, restart do processo e rollback se o pós-check não
 * confirmar. A tela só acompanha o status — nenhuma decisão mora aqui.
 */

const SWITCH_TOKEN_KEY = "vsl.switchToken";

const SWITCH_DISABLED = {
  switch_token_ausente:
    "troca pela tela desligada — defina SWITCH_TOKEN no overlay deploy/docker-compose.switch.yml",
  switch_sem_repo_montado:
    "troca pela tela desligada — monte scripts/ e deepstream/ no dashboard (overlay)",
  switch_enabled_off: "troca pela tela desligada (SWITCH_ENABLED=0)",
};

function setBanner(kind, text) {
  const banner = $("switch-banner");
  if (!text) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.className = `switch-banner ${kind}`;
  banner.textContent = text;
}

function renderSwitchStatus() {
  const status = state.sw.status;
  if (!state.sw.enabled) {
    setBanner("info", SWITCH_DISABLED[state.sw.reason] || "troca pela tela desligada");
    return;
  }
  // Troca em andamento por outro processo (`switch_source.sh`): o 409 vem do
  // mutex entre processos, não de bug. Sem mostrar isto, o operador só vê o
  // banner voltar para "idle" no próximo poll e tenta de novo.
  const foreign = state.sw.foreign;
  if (foreign && (!status || status.state === "idle" || status.state === "done")) {
    const to = foreign.to ? ` para ${foreign.to}` : "";
    setBanner("running",
      `outra troca em andamento (${foreign.origin}${to}) — aguarde terminar`);
    return;
  }
  if (!status || status.state === "idle") {
    setBanner("", "");
    return;
  }
  if (status.state === "running") {
    const step = status.step ? `${status.step} · ` : "";
    setBanner("running",
      `trocando para ${status.source_id} — ${step}${status.message || "aguardando"}`);
    return;
  }
  if (status.state === "done") {
    setBanner("ok", status.message || `fonte ativa agora é ${status.source_id}`);
    return;
  }
  setBanner("error",
    `troca não concluída (${status.reason || "falha"}) — ${status.message || ""}`
      .trim());
}

function updateSwitchUi() {
  $("btn-token").hidden = !state.sw.enabled || !!state.sw.token;
  renderSwitchStatus();
}

function openTokenForm(sourceId) {
  if (sourceId) state.sw.pending = sourceId;
  $("switch-token-form").hidden = false;
  $("switch-token").focus();
}

function closeTokenForm() {
  $("switch-token-form").hidden = true;
  $("switch-token").value = "";
  state.sw.pending = null;
}

function forgetToken() {
  state.sw.token = null;
  sessionStorage.removeItem(SWITCH_TOKEN_KEY);
}

async function refreshSwitchInfo() {
  try {
    const info = await fetchJson("/api/source/switch");
    state.sw.enabled = !!info.enabled;
    state.sw.reason = info.disabled_reason || null;
    state.sw.status = info.status || null;
    state.sw.foreign = info.in_progress_by || null;
    if (info.status?.state === "running") startSwitchPolling();
  } catch {
    return;  // backend subindo: mantém o último estado conhecido
  }
  updateSwitchUi();
  renderSources();
}

function startSwitchPolling() {
  if (state.sw.timer) return;
  state.sw.timer = setInterval(pollSwitch, 2000);
}

function stopSwitchPolling() {
  if (!state.sw.timer) return;
  clearInterval(state.sw.timer);
  state.sw.timer = null;
}

async function pollSwitch() {
  let info;
  try {
    info = await fetchJson("/api/source/switch");
  } catch {
    return;  // uma falha de rede não cancela o acompanhamento
  }
  state.sw.status = info.status || null;
  state.sw.foreign = info.in_progress_by || null;
  renderSwitchStatus();
  if (state.sw.foreign && !state.sw.timer) startSwitchPolling();
  if (state.sw.status?.state === "running") return;

  stopSwitchPolling();
  renderSources();
  await refreshSources();
  if (state.selectedSource === null) {
    loadHistory();
    loadEvents();
  }
}

async function postSwitch(sourceId) {
  state.sw.foreign = null;
  state.sw.status = { state: "running", step: "pedindo", source_id: sourceId,
                      message: "pedido enviado" };
  renderSwitchStatus();
  renderSources();

  let res;
  try {
    res = await fetch("/api/source/switch", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Switch-Token": state.sw.token || "",
      },
      body: JSON.stringify({ source_id: sourceId }),
    });
  } catch {
    setBanner("error", "o dashboard não respondeu — a troca não foi pedida");
    state.sw.status = null;
    renderSources();
    return;
  }

  if (res.status === 401) {
    forgetToken();
    updateSwitchUi();
    setBanner("error", "token recusado — informe o SWITCH_TOKEN de novo");
    openTokenForm(sourceId);
    state.sw.status = null;
    renderSources();
    return;
  }
  if (res.status === 503) {
    const body = await res.json().catch(() => ({}));
    const reason = body?.detail?.error || null;
    state.sw.enabled = false;
    state.sw.reason = reason;
    state.sw.status = null;
    updateSwitchUi();
    renderSources();
    return;
  }
  if (res.status === 409) {
    setBanner("error", "já existe uma troca em andamento — acompanhe abaixo");
    state.sw.status = { state: "running", step: "aguardando", source_id: sourceId,
                        message: "outra troca em andamento" };
    renderSwitchStatus();
    startSwitchPolling();
    return;
  }
  if (!res.ok) {
    state.sw.status = null;
    setBanner("error", `não foi possível pedir a troca (HTTP ${res.status})`);
    renderSources();
    return;
  }

  const body = await res.json().catch(() => ({}));
  state.sw.status = body.status || { state: "running", source_id: sourceId };
  renderSwitchStatus();
  renderSources();
  startSwitchPolling();
}

async function requestSwitch(source) {
  if (!state.sw.enabled) return;
  if (!state.sw.token) {
    openTokenForm(source.id);
    setBanner("info", "informe o token para destravar a troca (fica só nesta aba)");
    return;
  }
  const active = sourceById(state.activeSource);
  const message =
    `Ativar a fonte "${source.name}"?\n\n` +
    `Fonte ativa agora: ${active ? active.name : "—"}.\n` +
    "O pipeline reinicia (30–90 s) e o vídeo fica fora do ar nesse intervalo.\n" +
    "Se o pré-check não aprovar a fonte, nada é alterado.";
  if (!confirm(message)) return;
  await postSwitch(source.id);
}

$("btn-token").addEventListener("click", () => openTokenForm(null));

$("switch-token-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const value = $("switch-token").value.trim();
  if (!value) return;
  state.sw.token = value;
  sessionStorage.setItem(SWITCH_TOKEN_KEY, value);
  const pending = state.sw.pending;
  closeTokenForm();          // limpa o campo: o token não fica no DOM
  updateSwitchUi();
  renderSources();
  if (pending) {
    const source = sourceById(pending) || { id: pending, name: pending };
    requestSwitch(source);
  }
});

$("switch-token-cancel").addEventListener("click", closeTokenForm);

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

function isViewedRecord(record) {
  return state.selectedSource === null ||
    sourceKey(record) === state.selectedSource;
}

function renderSnapshot(snap) {
  if (snap.source) state.activeSource = snap.source.id;
  refreshSources();

  if (state.selectedSource === null) {
    // seguindo a fonte ativa: o snapshot manda na lista de eventos
    state.events = [];
    for (let i = snap.events_recent.length - 1; i >= 0; i--) {
      pushEvent(snap.events_recent[i], true);
    }
    renderEvents();
  } else {
    // fonte fixada: os eventos dela vêm da API, não da execução atual
    loadEvents();
  }

  if (snap.latest_metrics && isViewedRecord(snap.latest_metrics)) {
    applyMetrics(snap.latest_metrics);
    drawCharts();
  }
  updateDerived(snap);
}

function ingestRecord(record) {
  const event = record.event || "";

  if (event === "source_active") {
    // troca de fonte ou restart: cartões e, se estiver seguindo, histórico
    refreshSources();
    if (state.selectedSource === null) {
      state.history = [];
      loadHistory();
    }
  }

  // fonte fixada pelo operador: registro de outra fonte não entra em nada
  if (state.selectedSource !== null && state.selectedSource !== sourceKey(record)) {
    return;
  }

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
  // `persons_max_concurrent` é o pico do intervalo de telemetria (o pipeline
  // zera o acumulador a cada relatório) — é exatamente "pico simultâneo".
  $("s-peak").textContent = m.persons_max_concurrent ?? 0;

  $("h-rss").textContent = `${(m.rss_mb ?? 0).toFixed(1)} mb`;
  const growth = rssGrowthMb(m);
  const growthEl = $("h-growth");
  growthEl.textContent = growth === null ? "—" :
    `${growth >= 0 ? "+" : ""}${growth.toFixed(1)} mb`;
  growthEl.classList.toggle("bad", rssGrowthBad(growth));
  $("h-proc").textContent = `${m.threads ?? "—"} / ${m.open_fds ?? "—"}`;
  $("h-frames").textContent = (m.frames_total_processed ?? 0).toLocaleString("pt-BR");

  const ageSec = Math.max(0, (Date.now() - new Date(m.ts).getTime()) / 1000);
  $("h-age").textContent = `${fmtAgo(ageSec)} atrás`;
  $("h-age").classList.toggle("bad", ageSec > 90);
}

function updateDerived(snap) {
  const health = snap.health || {};
  const el = $("h-status");
  el.textContent = health.status ?? "—";
  el.classList.remove("ok", "warn", "bad");
  const tone = { healthy: "ok", degraded: "warn", stale: "warn", down: "bad" }[health.status];
  if (tone) el.classList.add(tone);
  $("h-errors").textContent = snap.error_count ?? 0;
  $("h-errors").classList.toggle("bad", (snap.error_count ?? 0) > 0);

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
  preview_branch_error: "erro no preview",
  rtsp_reconnect_error: "reconexão rtsp",
  source_active: "fonte ativa",
  test_duration_reached: "teste concluído",
  interrupted_by_user: "interrompido",
  eos: "fim de fluxo",
};

function eventClass(event) {
  // falhas de reconexão e do ramo de preview não são falha do pipeline
  // (classificação do E3-T2): ficam neutras, sem o vermelho de `pipeline_error`
  if (event === "rtsp_reconnect_error" || event === "preview_branch_error") return "";
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
    case "source_active":
      return `${r.source_name || r.source_id}${r.codec ? ` · ${r.codec}` : ""}`;
    case "rtsp_reconnect_error":
    case "preview_branch_error":
      return r.error || "";
    case "pipeline_playing":
    case "pipeline_stopped":
    case "preview_branch_ready":
    case "eos":
    case "test_duration_reached":
    case "interrupted_by_user":
      return r.source_name || r.source_id || "";
    default:
      // campos internos (`_class`, `_group`) e a proveniência não são conteúdo
      // para o operador: apareciam crus no log antes do filtro abaixo
      return Object.entries(r)
        .filter(([k, v]) =>
          !k.startsWith("_") && v !== null && v !== undefined &&
          !["ts", "event", "run", "source_id", "source_name"].includes(k))
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

// Só os chips do filtro de eventos: os chips de fonte/token usam a mesma
// classe e não podem trocar o filtro ao serem clicados.
document.querySelectorAll(".event-filter .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".event-filter .chip").forEach((c) => {
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
  const casas = maxV >= 50 ? 0 : 1;  // mesma precisão nos 4 rótulos do eixo
  const x = (i) => pad.l + (i / Math.max(values.length - 1, 1)) * (w - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - minV) / span) * (h - pad.t - pad.b);

  ctx.strokeStyle = token("--divider");
  ctx.fillStyle = token("--chart-axis");
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const gy = pad.t + (g / 3) * (h - pad.t - pad.b);
    const gv = maxV - (g / 3) * span;
    ctx.beginPath();
    ctx.moveTo(pad.l, gy);
    ctx.lineTo(w - pad.r, gy);
    ctx.stroke();
    ctx.fillText(gv.toFixed(casas), 4, gy + 3);
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
  drawLine("chart-fps", rows.map((m) => m.fps_processed ?? 0),
    token("--accent"), withAlpha(token("--accent"), 0.12), "fps-max");
  drawLine("chart-rss", rows.map((m) => m.rss_mb ?? 0),
    token("--info"), withAlpha(token("--info"), 0.12), "rss-max");
  if (!rows.length) {
    $("fps-max").textContent = "";
    $("rss-max").textContent = "";
  }
}

/* histórico completo da fonte vista (30 min) */
async function loadHistory() {
  const id = viewedSourceId();
  const url = id
    ? `/api/history?minutes=30&source=${encodeURIComponent(id)}`
    : "/api/history?minutes=30";
  try {
    state.history = await fetchJson(url);
    drawCharts();
  } catch { /* backend ainda subindo */ }
}

/* eventos da fonte vista (a lista completa daquela fonte, não só do run) */
async function loadEvents() {
  const id = viewedSourceId();
  const url = id
    ? `/api/events?limit=200&source=${encodeURIComponent(id)}`
    : "/api/events?limit=200";
  try {
    const records = await fetchJson(url);
    state.events = [];
    for (const record of records) pushEvent(record, true);
    renderEvents();
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

state.sw.token = sessionStorage.getItem(SWITCH_TOKEN_KEY);

$("btn-theme")?.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});
applyTheme(currentTheme());

wsConnect();
refreshSources();
refreshSwitchInfo();
loadHistory();
setInterval(loadHistory, 60000);
setInterval(refreshSources, 30000);
