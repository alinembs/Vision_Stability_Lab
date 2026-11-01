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
 * confirmação negada sem `POST` e 401 limpando o token.
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
window.WebSocket = class { constructor() { this.readyState = 0; } send() {} close() {} };
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

  // 5) confirmação negada não dispara POST
  calls.length = 0;
  window.__confirmAnswer = false;
  activators().find((b) => b.dataset.activate === "drone")?.click();
  await tick(10);
  check("confirmação negada não faz POST",
        !calls.some(([m, u]) => m === "POST" && u.includes("/api/source/switch")));

  // 6) token recusado limpa a sessão e reabre o formulário
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
