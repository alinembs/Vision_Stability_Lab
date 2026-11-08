/**
 * Smoke test de DOM do painel de fontes e da troca pela tela (E4-T2/E4-T3).
 *
 * O projeto não tem runner de JS (sem npm, sem build) — o `app.js` é validado
 * com `node --check` e, para o que é comportamento, com autoteste no navegador
 * (`codec-gate-selftest.html`) ou com este arquivo, que usa **jsdom**.
 *
 * jsdom não é dependência do projeto: sem ele o script sai com aviso e código 0
 * (não quebra quem só quer rodar o resto). Para executar:
 *
 *     mkdir -p /tmp/uicheck && cd /tmp/uicheck && npm install jsdom
 *     NODE_PATH=/tmp/uicheck/node_modules node dashboard/tests/ui_smoke.js
 *
 * O que ele cobre (28 verificações): render dos cartões, "ativar" só em fonte
 * trocável, token em `sessionStorage` e fora do DOM, confirmação antes do
 * `POST`, header `X-Switch-Token`, banner de andamento, polling até concluir,
 * confirmação negada sem `POST` e 401 limpando o token. Depois disso, o painel
 * do observador de fontes (E7): "sem observador", lâmpadas de
 * `standby_no_sources`/`watching`, chips da sonda e o nível escondido no
 * `title` (D2/D3 de `docs/plans/2026-11-08-frontend-observador-vsl.md`).
 */
"use strict";

const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch {
  console.log("jsdom ausente — `npm install jsdom` para rodar este smoke test (nada a fazer aqui)");
  process.exit(0);
}

const FRONTEND = path.resolve(__dirname, "..", "frontend");
const html = fs.readFileSync(path.join(FRONTEND, "index.html"), "utf8");
const source = fs.readFileSync(path.join(FRONTEND, "app.js"), "utf8");

const calls = [];
let switchGetCount = 0;
let switchState = "idle";

const sources = [
  { id: "camera", name: "câmera fixa", codec: "h265", target_fps: 20, enabled: true,
    path: null, path_origin: "env", path_env: "RTSP_URI", in_catalog: true,
    active: true,
    session: { id: "camera", name: "câmera fixa", session_epoch: "2026-11-01T13:00:00Z",
               since: "2026-11-01T13:00:00Z", last_ts: "2026-11-01T13:00:00Z",
               points: 0, last_metric_age_s: null } },
  { id: "drone", name: "drone", codec: "h264", target_fps: 25, enabled: true,
    path: "rtsp://mediamtx:8554/drone", path_origin: "config", path_env: null,
    in_catalog: true, active: false,
    session: { id: "drone", name: "drone", session_epoch: "2026-10-31T22:00:00Z",
               since: "2026-10-31T22:00:00Z", last_ts: "2026-10-31T22:03:00Z",
               points: 6, last_metric_age_s: 54358.8 } },
  { id: "phone", name: "celular", codec: "h264", target_fps: 30, enabled: true,
    path: "rtsp://mediamtx:8554/phone", path_origin: "config", path_env: null,
    in_catalog: true, active: false, session: null },
  { id: "velha", name: "fonte aposentada", codec: "h264", target_fps: 30,
    enabled: false, path: "rtsp://x/y", path_origin: "config", path_env: null,
    in_catalog: true, active: false, session: null },
];

const dom = new JSDOM(html, { url: "http://localhost:8081/",
                             runScripts: "outside-only",
                             pretendToBeVisual: true });
const { window } = dom;

// --- dublês de ambiente (jsdom não tem canvas, WebSocket nem fetch) --------
const ctx = new Proxy({}, { get: () => () => {} });
window.HTMLCanvasElement.prototype.getContext = () => ctx;
window.confirm = (msg) => { calls.push(["confirm", msg]); return window.__confirmAnswer ?? true; };
// O app abre `new WebSocket(...)` no boot. Guardar a instância é como este
// teste entrega um snapshot: o `app.js` roda em eval estrito, então as funções
// dele NÃO ficam globais (`window.renderSnapshot` é `undefined`).
const sockets = [];
window.WebSocket = class {
  constructor() { this.readyState = 0; sockets.push(this); }
  send() {}
  close() {}
};
window.fetch = async (url, options = {}) => {
  const method = options.method || "GET";
  calls.push([method, url, options.headers || null, options.body || null]);
  const json = (body, status = 200) => ({
    ok: status < 400, status, json: async () => body,
  });

  if (url.startsWith("/api/sources")) return json(sources);
  if (url.startsWith("/api/history")) return json([]);
  if (url.startsWith("/api/events")) return json([]);
  if (url.startsWith("/api/source/switch")) {
    if (method === "POST") {
      switchState = "running";
      return json({ accepted: true,
                    status: { state: "running", step: "precheck",
                              source_id: "drone", message: "lendo a fonte por 5s" } }, 202);
    }
    switchGetCount += 1;
    if (switchGetCount >= 2) switchState = "done";  // 2ª consulta conclui a troca
    return json({
      enabled: true, disabled_reason: null, container: "vision-stability-app",
      timeout_s: 90,
      status: switchState === "running"
        ? { state: "running", step: "wait", source_id: "drone",
            message: "aguardando telemetria de drone" }
        : { state: "done", step: "concluída", source_id: "drone", reason: null,
            message: "fonte ativa agora é drone (24.5 fps de entrada na confirmação)" },
    });
  }
  return json({}, 404);
};

window.eval(source);

// --- verificações ---------------------------------------------------------
const results = [];
const check = (name, condition, detail = "") => results.push([name, !!condition, detail]);

const cards = () => [...window.document.querySelectorAll(".source-card")];
const activators = () => [...window.document.querySelectorAll(".source-activate")];
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
// Snapshot entra pela mesma porta do backend: o `onmessage` do WebSocket.
const pushSnapshot = (snap) => {
  const ws = sockets[sockets.length - 1];
  ws.onmessage({ data: JSON.stringify({ type: "snapshot", data: snap }) });
};

(async () => {
  await tick(20);

  check("4 cartões renderizados", cards().length === 4, `cards=${cards().length}`);
  check("cada cartão tem área 'ver'",
        window.document.querySelectorAll(".source-view").length === 4);
  check("botões 'ativar' só nas fontes trocáveis", activators().length === 2,
        `ativar=${activators().map((b) => b.dataset.activate).join(",")}`);
  check("fonte ativa não oferece 'ativar'",
        !activators().some((b) => b.dataset.activate === "camera"));
  check("fonte desabilitada não oferece 'ativar'",
        !activators().some((b) => b.dataset.activate === "velha"));
  check("selo 'ativa' no cartão da câmera",
        cards()[0].classList.contains("is-active") &&
        cards()[0].textContent.includes("ativa"));
  check("path da câmera vem da env, sem valor",
        cards()[0].textContent.includes("RTSP_URI (deploy/.env)"));
  check("botão de token visível (nada destravado ainda)",
        window.document.getElementById("btn-token").hidden === false);

  // 1) "ativar" sem token -> abre o formulário, sem POST
  activators().find((b) => b.dataset.activate === "drone").click();
  await tick(5);
  check("formulário de token aberto",
        window.document.getElementById("switch-token-form").hidden === false);
  check("nenhum POST antes do token",
        !calls.some(([m, u]) => m === "POST" && u.includes("/api/source/switch")));
  check("banner pede o token",
        window.document.getElementById("switch-banner").textContent.includes("token"));

  // 2) token + submit -> confirmação -> POST com header
  const form = window.document.getElementById("switch-token-form");
  window.document.getElementById("switch-token").value = "  segredo-local  ";
  form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await tick(10);

  const post = calls.find(([m, u]) => m === "POST" && u.includes("/api/source/switch"));
  check("POST feito", !!post);
  check("token vai no header X-Switch-Token",
        post && post[2] && post[2]["X-Switch-Token"] === "segredo-local",
        post ? JSON.stringify(post[2]) : "sem post");
  check("token não fica no DOM",
        window.document.getElementById("switch-token").value === "");
  check("token não é renderizado na página",
        !window.document.body.innerHTML.includes("segredo-local"));
  check("confirmação pedida antes do POST", calls.some(([m]) => m === "confirm"));
  check("confirmação avisa do restart do pipeline",
        calls.some(([m, msg]) => m === "confirm" && /reinicia/.test(msg)));
  check("corpo do POST tem source_id",
        post && post[3] === '{"source_id":"drone"}');
  check("sessionStorage guarda o token",
        window.sessionStorage.getItem("vsl.switchToken") === "segredo-local");

  // 3) banner acompanha o status
  await tick(20);
  const banner = window.document.getElementById("switch-banner");
  check("banner em andamento", banner.textContent.includes("trocando para drone"),
        banner.textContent);
  check("botões de ativar desabilitados durante a troca",
        activators().every((b) => b.disabled));

  // 4) polling conclui e reabilita a UI
  for (let i = 0; i < 20 && switchGetCount < 2; i += 1) await tick(250);
  check("polling consultou o status", switchGetCount >= 2, `gets=${switchGetCount}`);
  check("banner final de sucesso",
        banner.textContent.includes("fonte ativa agora é drone"), banner.textContent);
  check("lista de fontes recarregada após concluir",
        calls.filter(([m, u]) => m === "GET" && u.startsWith("/api/sources")).length >= 2);

  // 5) painel do observador de fontes (E7)
  const modeEl = window.document.getElementById("observer-mode");
  const observerLamp = window.document.getElementById("lamp-observer");
  const reasonEl = window.document.getElementById("observer-reason");
  const actionEl = window.document.getElementById("observer-action");
  const ageEl = window.document.getElementById("observer-age");
  const probesEl = window.document.getElementById("observer-probes");
  const probeChips = () => [...probesEl.querySelectorAll(".observer-probe")];

  const observerSnap = (observer) => ({
    server_time: "2026-11-08T12:00:00Z",
    health: { status: "healthy" },
    source: { id: "camera" },
    latest_metrics: null,
    pipeline_started_at: null,
    preview_transport: null,
    error_count: 0,
    last_error: null,
    events_recent: [],
    observer,
  });

  // 5a) sem observador: informa o motivo, amarelo, sem inventar valores
  const semObservador = {
    available: false, mode: null, mode_reason: null, last_action: null,
    last_action_ts: null, ts: null, age_s: null, sources_probe: {},
    reason: "observer_state.json indisponível (FileNotFoundError)",
  };
  pushSnapshot(observerSnap(semObservador));
  await tick(5);
  check("observador: painel e lâmpada existem no HTML",
        !!modeEl && !!observerLamp && !!probesEl);
  check("observador: available=false diz 'sem observador'",
        modeEl.textContent.includes("sem observador"), modeEl.textContent);
  check("observador: motivo do available=false vai no cartão",
        reasonEl.textContent.includes("indisponível"), reasonEl.textContent);
  check("observador: available=false -> lâmpada warn (nunca off)",
        observerLamp.classList.contains("warn") &&
        !observerLamp.classList.contains("on") &&
        !observerLamp.classList.contains("off"), observerLamp.className);
  check("observador: title da lâmpada carrega o motivo",
        observerLamp.title.includes("indisponível"), observerLamp.title);

  // 5b) standby com available=true é estado esperado: lâmpada VERDE (D3)
  pushSnapshot(observerSnap({
    available: true, mode: "standby_no_sources", mode_reason: "nenhuma fonte ativa",
    last_action: "none", last_action_ts: null, ts: "2026-11-08T12:00:00Z",
    age_s: 3.2, sources_probe: {}, reason: null,
  }));
  await tick(5);
  check("observador: standby_no_sources -> lâmpada on (D3)",
        observerLamp.classList.contains("on"), observerLamp.className);
  check("observador: standby traduzido",
        modeEl.textContent === "standby — sem fonte ativa", modeEl.textContent);
  check("observador: last_action 'none' -> '— nenhuma'",
        actionEl.textContent === "— nenhuma", actionEl.textContent);
  check("observador: retrato mostra a idade",
        ageEl.textContent === "há 3s", ageEl.textContent);

  // 5c) watching também é verde
  pushSnapshot(observerSnap({
    available: true, mode: "watching", mode_reason: "câmera viva há 12s",
    last_action: "switch_completed", last_action_ts: "2026-11-08T11:59:00Z",
    ts: "2026-11-08T12:00:00Z", age_s: 1.4, sources_probe: {}, reason: null,
  }));
  await tick(5);
  check("observador: watching -> lâmpada on",
        observerLamp.classList.contains("on"), observerLamp.className);
  check("observador: motivo do modo no cartão",
        reasonEl.textContent === "câmera viva há 12s", reasonEl.textContent);

  // 5d) sonda por fonte: 1 vivo, 1 morto, 1 não sondada
  const probesSnap = observerSnap({
    available: true, mode: "watching", mode_reason: "acompanhando fontes",
    last_action: "switch_completed", last_action_ts: "2026-11-08T12:00:00Z",
    ts: "2026-11-08T12:00:00Z", age_s: 2, reason: null,
    sources_probe: {
      phone: { alive: null, level: null, reason: null, checked_at: null },
      camera: { alive: true, level: 2, reason: null, checked_at: null },
      drone: { alive: false, level: 1, reason: "timeout no DESCRIBE",
               checked_at: null },
    },
  });
  pushSnapshot(probesSnap);
  await tick(5);
  check("observador: um chip por fonte sondada", probeChips().length === 3,
        `chips=${probeChips().length}`);
  check("observador: chips ordenados por id",
        probeChips().map((c) => c.dataset.probe).join(",") === "camera,drone,phone",
        probeChips().map((c) => c.dataset.probe).join(","));
  check("observador: 1 vivo, 1 morto, 1 não sondada",
        probeChips().filter((c) => c.classList.contains("alive")).length === 1 &&
        probeChips().filter((c) => c.classList.contains("dead")).length === 1 &&
        probeChips().filter((c) => c.classList.contains("unknown")).length === 1,
        probeChips().map((c) => c.className).join(" | "));
  check("observador: chip usa o nome da fonte e o estado em português",
        probeChips()[0].textContent.includes("câmera fixa") &&
        probeChips()[0].textContent.includes("vivo") &&
        probeChips()[2].textContent.includes("não sondada"),
        probeChips()[0].textContent);
  const probeText = probeChips().map((c) => c.textContent).join(" ");
  check("observador: nível 1/2 não aparece no texto visível (D2)",
        !/n[íi]vel/i.test(probeText), probeText);
  check("observador: level vai no title do chip (D2)",
        probeChips()[0].title.includes("nível 2") &&
        probeChips()[1].title.includes("nível 1") &&
        probeChips()[1].title.includes("timeout no DESCRIBE"),
        probeChips()[1].title);

  // 5e) mesmo payload não reescreve o painel (aria-live não tagarela)
  const chipBefore = probeChips()[0];
  pushSnapshot(probesSnap);
  await tick(5);
  check("observador: snapshot idêntico não reescreve os chips",
        probeChips()[0] === chipBefore);

  // 5f) sem observador não apaga o resto da tela
  const cardsBefore = cards().length;
  pushSnapshot(observerSnap(semObservador));
  await tick(5);
  check("observador: available=false preserva os cartões de fonte",
        cardsBefore === 4 && cards().length === 4,
        `antes=${cardsBefore} depois=${cards().length}`);

  // 6) confirmação negada não dispara POST
  calls.length = 0;
  window.__confirmAnswer = false;
  activators().find((b) => b.dataset.activate === "drone")?.click();
  await tick(10);
  check("confirmação negada não faz POST",
        !calls.some(([m, u]) => m === "POST" && u.includes("/api/source/switch")));

  // 7) token recusado limpa a sessão e reabre o formulário
  calls.length = 0;
  window.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    calls.push([method, url]);
    if (url.includes("/api/source/switch") && method === "POST") {
      return { ok: false, status: 401,
               json: async () => ({ detail: { error: "token_invalido" } }) };
    }
    if (url.includes("/api/source/switch")) {
      return { ok: true, status: 200,
               json: async () => ({ enabled: true, status: { state: "idle" } }) };
    }
    return { ok: true, status: 200, json: async () => [] };
  };
  window.__confirmAnswer = true;
  activators().find((b) => b.dataset.activate === "drone")?.click();
  await tick(20);
  check("401 limpa o token da sessão",
        window.sessionStorage.getItem("vsl.switchToken") === null);
  check("401 reabre o formulário de token",
        window.document.getElementById("switch-token-form").hidden === false);
  check("401 mostra erro",
        window.document.getElementById("switch-banner").textContent.includes("recusado"));

  // relatório
  let failed = 0;
  for (const [name, ok, detail] of results) {
    if (!ok) failed += 1;
    console.log(`${ok ? "ok  " : "FAIL"} ${name}${detail && !ok ? `  [${detail}]` : ""}`);
  }
  console.log(`\n${results.length - failed}/${results.length} verificações passaram`);
  process.exit(failed ? 1 : 0);
})();
