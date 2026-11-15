/* Camada de visão comum às quatro versões.
 *
 * Cada versão traz o seu CSS e a sua composição; esta camada cuida do que
 * precisa ser verdade em todas: dados por fonte, troca de fonte que muda os
 * números, filtro que filtra de fato, estado de carregamento e gráficos.
 *
 * Contrato de marcação (ids iguais nas quatro páginas):
 *   #sources, #active-source-name, #active-source-meta, #btn-preview,
 *   #video-frame, #osd-*, #area-*, #stat-*, #chart-*, #health-*,
 *   #observer-*, #events, #events-count, #foot-source
 */
(function () {
  "use strict";

  var D = window.VSL;
  if (!D) return;

  var state = {
    fonte: "camera",       // fonte que os painéis estão mostrando
    ativa: "camera",       // fonte que o pipeline está publicando
    filtro: "all",
    preview: "idle",       // idle | connecting | live
    pronto: false,
  };

  var reduziuMovimento = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function $(id) { return document.getElementById(id); }
  function set(id, texto) {
    var el = $(id);
    if (el) el.textContent = texto;
  }
  function setClass(id, cls, on) {
    var el = $(id);
    if (el) el.classList.toggle(cls, !!on);
  }

  var dois = function (n) { return String(n).padStart(2, "0"); };

  function hhmmss(data) {
    return dois(data.getHours()) + ":" + dois(data.getMinutes()) + ":" + dois(data.getSeconds());
  }

  function duracao(seg) {
    seg = Math.max(0, Math.round(seg));
    var h = Math.floor(seg / 3600);
    var m = Math.floor((seg % 3600) / 60);
    var s = seg % 60;
    return (h > 0 ? h + ":" : "") + dois(m) + ":" + dois(s);
  }

  function desde(seg) {
    if (seg >= 3600) return Math.floor(seg / 3600) + " h " + dois(Math.floor((seg % 3600) / 60)) + " min";
    if (seg >= 60) return Math.floor(seg / 60) + " min";
    return seg + " s";
  }

  function megabytes(n) {
    return n.toFixed(1);
  }

  /** Milhar com espaço fino: separa "156 240" de "947.2" sem ambiguidade. */
  function milhar(n) {
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, "\u2009");
  }

  /* ---------------- fontes ---------------- */

  var ICONES = {
    camera: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="7.5" width="11" height="9" rx="1.6"/><circle cx="8" cy="12" r="2"/><path d="m13.5 10.5 6-2.5v8l-6-2.5"/><path d="M6 16.5V19h4"/></svg>',
    drone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9.4" y="9.4" width="5.2" height="5.2" rx="1.4"/><path d="m9.4 9.4-3-3M14.6 9.4l3-3M9.4 14.6l-3 3M14.6 14.6l3 3"/><circle cx="4.6" cy="4.6" r="2.1"/><circle cx="19.4" cy="4.6" r="2.1"/><circle cx="4.6" cy="19.4" r="2.1"/><circle cx="19.4" cy="19.4" r="2.1"/></svg>',
    phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M10.6 18.6h2.8"/><circle cx="12" cy="5.6" r="0.9"/></svg>',
  };

  function desenharFontes() {
    var lista = $("sources");
    if (!lista) return;
    lista.innerHTML = "";
    D.fontes.forEach(function (f) {
      var li = document.createElement("li");
      li.className = "source" + (f.id === state.fonte ? " selected" : "");
      li.dataset.source = f.id;
      li.innerHTML =
        '<button class="source-btn" type="button" aria-pressed="' + (f.id === state.fonte) + '">' +
          '<span class="source-icon">' + (ICONES[f.icone] || "") + "</span>" +
          '<span class="source-body">' +
            '<span class="source-name">' + f.nome +
              (f.id === state.ativa ? '<span class="source-role">publicando</span>' : "") +
            "</span>" +
            '<span class="source-meta">' + f.id + " · " + f.codec + " · " + f.mux + "</span>" +
          "</span>" +
        "</button>";
      lista.appendChild(li);
    });
  }

  function aoTrocarFonte(id) {
    if (id === state.fonte) return;
    state.fonte = id;
    resetarPreview();
    desenharFontes();
    renderizar({ animar: true });
  }

  /* ---------------- vídeo ---------------- */

  function resetarPreview() {
    state.preview = "idle";
    var frame = $("video-frame");
    if (frame) frame.dataset.state = "idle";
    var btn = $("btn-preview");
    if (btn) {
      btn.textContent = "conectar preview";
      btn.setAttribute("aria-pressed", "false");
      btn.disabled = false;
    }
    set("video-state-text", "preview não conectado — o vídeo anotado vem por WebRTC (WHEP)");
  }

  var previewTimer = null;

  function alternarPreview() {
    var btn = $("btn-preview");
    var frame = $("video-frame");
    if (!btn || !frame) return;

    if (state.preview === "connecting") return;

    if (state.preview === "live") {
      resetarPreview();
      return;
    }

    if (!D.fonte(state.fonte).metricas) {
      set("video-state-text", "sem amostra desta sessão — o preview abre quando o pipeline publicar frames");
      return;
    }

    state.preview = "connecting";
    frame.dataset.state = "connecting";
    btn.textContent = "conectando…";
    btn.setAttribute("aria-pressed", "true");
    btn.disabled = true;
    set("video-state-text", "negociando WHEP com mediamtx:8889…");

    clearTimeout(previewTimer);
    previewTimer = setTimeout(function () {
      state.preview = "live";
      frame.dataset.state = "live";
      btn.disabled = false;
      btn.textContent = "desconectar";
      btn.setAttribute("aria-pressed", "true");
    }, reduziuMovimento ? 150 : 1100);
  }

  /* ---------------- render ---------------- */

  function renderizar(opcoes) {
    var animar = !!(opcoes && opcoes.animar);
    var f = D.fonte(state.fonte);
    var m = f.metricas;
    var s = D.series[f.id] || { fps: [], rss: [] };

    document.documentElement.dataset.source = f.id;

    set("active-source-name", f.nome);
    set("active-source-meta", f.id + " · " + f.codec + " · " + f.mux + " · alvo " + f.alvo + " fps");
    set("foot-source", f.nome + " · sessão " + duracao(f.sessao));
    set("events-scope", f.nome);

    // cabeçalho
    set("uptime", duracao(f.sessao));
    var saudavel = !!m && m.fpsProc > 0;
    set("health-word", !m ? "sem amostra" : saudavel ? "saudável" : "sem frames");
    setClass("health-word", "warn", !saudavel);
    set("conn-note", m ? "telemetria há " + m.telemetria + " s" : "sessão nova · nenhuma amostra no JSONL");

    // OSD do vídeo
    set("osd-source", f.nome);
    set("osd-codec", f.codec + " · " + f.mux);
    set("osd-area", m ? (f.estado.area === "OCCUPIED" ? "OCUPADO" : "VAZIO") : "—");
    set("osd-fps", m ? m.fpsProc.toFixed(1) + " fps" : "—");
    set("osd-tracks", m ? m.tracks + " tracks" : "—");

    // sem amostra não há preview para abrir: o botão diz isso em vez de falhar
    var btnPreview = $("btn-preview");
    if (btnPreview && state.preview !== "connecting") {
      btnPreview.disabled = !m;
      btnPreview.textContent = m ? "conectar preview" : "aguardando amostra";
      btnPreview.setAttribute("aria-pressed", "false");
    }

    if (state.preview !== "live") {
      set("video-state-text", m
        ? "preview não conectado — o vídeo anotado vem por WebRTC (WHEP)"
        : "sessão aberta há " + desde(f.sessao) + " — o preview abre quando o pipeline publicar o primeiro frame");
    }
    var frame = $("video-frame");
    if (frame) {
      frame.dataset.tracks = m ? String(m.tracks) : "0";
      frame.dataset.area = m ? f.estado.area : "UNKNOWN";
    }

    // KPIs
    set("stat-fps", m ? m.fpsProc.toFixed(1) : "—");
    set("stat-fps-hint", m
      ? "fonte entrega " + m.fpsIn.toFixed(1) + " · alvo " + f.alvo + " · drop " + m.drop.toFixed(2)
      : "aguardando primeira amostra");
    set("stat-rss", m ? megabytes(m.rss) + " MB" : "—");
    set("stat-rss-hint", m
      ? (m.rss - m.rssBase >= 0 ? "+" : "") + (m.rss - m.rssBase).toFixed(1) + " MB desde o warm-up"
      : "sem série nesta sessão");
    set("stat-tracks", m ? String(m.tracks) : "—");
    set("stat-tracks-hint", m ? "pessoas na cena agora · pico " + f.estado.pico : "—");
    set("stat-errors", m ? String(m.erros) : "—");
    set("stat-errors-hint", m ? "nesta execução do pipeline" : "—");

    // estado da área
    var e = f.estado;
    set("area-word", e ? (e.area === "OCCUPIED" ? "ocupado" : "vazio") : "—");
    set("area-enum", e ? e.area : "sem amostra");
    set("area-since", e ? "desde " + hhmmss(new Date(Date.now() - e.desde * 1000)) + " · " + desde(e.desde) : "—");
    set("area-entries", e ? String(e.entradas) : "—");
    set("area-exits", e ? String(e.saidas) : "—");
    set("area-dwell", e ? e.dwell.toFixed(1) + " s" : "—");
    set("area-peak", e ? String(e.pico) : "—");
    setClass("area-block", "occupied", !!(e && e.area === "OCCUPIED"));

    // saúde
    set("health-status", !m ? "aguardando telemetria" : "saudável · frescor " + m.telemetria + " s");
    set("health-proc", m ? m.threads : "—");
    set("health-frames", m ? milhar(m.frames) : "0");
    set("health-age", m ? m.telemetria + " s" : "—");
    set("health-ram", m ? m.ram + " · " + m.ramPct : "—");

    // sessão da fonte (path é somente leitura no catálogo)
    set("session-path", f.path);
    set("session-codec", f.codec + " · " + f.mux);
    set("session-samples", f.amostras ? milhar(f.amostras) + " · 1 a cada " + D.janela.passo_s + " s" : "0 · sessão nova");
    set("session-epoch", hhmmss(new Date(Date.now() - f.sessao * 1000)));
    set("session-frames", m ? milhar(m.frames) : "0");

    // observador
    set("observer-mode", D.observador.modo);
    set("observer-reason", D.observador.motivo);
    set("observer-action", D.observador.acao);
    var probes = $("observer-probes");
    if (probes) {
      probes.innerHTML = D.observador.sondas.map(function (p) {
        var cls = p.estado === "viva" ? "alive" : p.estado === "morta" ? "dead" : "unknown";
        return '<span class="probe ' + cls + '">' + p.nome + ' <b>' + p.estado + "</b></span>";
      }).join("");
    }

    // gráficos
    desenharGrafico("fps", s.fps, animar);
    desenharGrafico("rss", s.rss, animar);
    set("series-note", s.fps.length
      ? s.fps.length + " amostras · 1 a cada " + D.janela.passo_s + " s"
      : "sem amostras · a série começa na primeira telemetria da sessão");

    // eventos
    renderizarEventos();
  }

  /* ---------------- gráficos ---------------- */

  var graficos = {};

  function desenharGrafico(nome, valores, animar) {
    var svg = $("chart-" + nome);
    var vazio = $("chart-" + nome + "-empty");
    if (!svg) return;
    var agora = $("chart-" + nome + "-now");

    var temSerie = valores && valores.length > 1;
    svg.style.display = temSerie ? "" : "none";
    if (vazio) vazio.hidden = !!temSerie;
    if (!temSerie) {
      if (agora) agora.textContent = "";
      return;
    }

    var w = Math.max(280, Math.round(svg.clientWidth || svg.parentNode.clientWidth || 520));
    var h = Math.max(110, Math.round(svg.clientHeight || 132));
    svg.setAttribute("viewBox", "0 0 " + w + " " + h);

    var pad = { t: 12, r: 10, b: 20, l: nome === "rss" ? 46 : 34 };
    var max = Math.max.apply(null, valores);
    var min = Math.min.apply(null, valores);
    var folga = (max - min) * 0.16 || Math.max(0.5, max * 0.04);
    max += folga;
    min -= folga;
    var span = max - min || 1;

    var x = function (i) { return pad.l + (i / (valores.length - 1)) * (w - pad.l - pad.r); };
    var y = function (v) { return pad.t + (1 - (v - min) / span) * (h - pad.t - pad.b); };

    var casas = max >= 100 ? 0 : 1;
    var partes = [];

    for (var g = 0; g <= 3; g++) {
      var gy = pad.t + (g / 3) * (h - pad.t - pad.b);
      var gv = max - (g / 3) * span;
      partes.push('<line class="chart-grid" x1="' + pad.l + '" y1="' + gy.toFixed(1) +
        '" x2="' + (w - pad.r) + '" y2="' + gy.toFixed(1) + '"/>');
      partes.push('<text class="chart-axis" x="' + (pad.l - 8) + '" y="' + (gy + 3.5).toFixed(1) +
        '" text-anchor="end">' + gv.toFixed(casas) + "</text>");
    }

    var linha = "";
    var area = "";
    valores.forEach(function (v, i) {
      var px = x(i).toFixed(1), py = y(v).toFixed(1);
      linha += (i ? "L" : "M") + px + " " + py;
      area += (i ? "L" : "M") + px + " " + py;
    });
    area += "L" + x(valores.length - 1).toFixed(1) + " " + (h - pad.b).toFixed(1) +
            "L" + x(0).toFixed(1) + " " + (h - pad.b).toFixed(1) + "Z";

    var rotulos = [
      { i: 0, txt: "-" + D.janela.minutos + " min" },
      { i: Math.round((valores.length - 1) / 2), txt: "-" + Math.round(D.janela.minutos / 2) + " min" },
      { i: valores.length - 1, txt: "agora" },
    ];
    rotulos.forEach(function (r) {
      var anchor = r.i === 0 ? "start" : r.i === valores.length - 1 ? "end" : "middle";
      partes.push('<text class="chart-axis chart-x" x="' + x(r.i).toFixed(1) + '" y="' + (h - 5) +
        '" text-anchor="' + anchor + '">' + r.txt + "</text>");
    });

    partes.push('<path class="chart-area" d="' + area + '"/>');
    partes.push('<path class="chart-line" d="' + linha + '"/>');
    partes.push('<circle class="chart-dot" cx="' + x(valores.length - 1).toFixed(1) + '" cy="' +
      y(valores[valores.length - 1]).toFixed(1) + '" r="3"/>');

    svg.innerHTML = partes.join("");
    svg.setAttribute("role", "img");

    if (agora) {
      var ultimo = valores[valores.length - 1];
      agora.textContent = "agora " + (nome === "rss" ? megabytes(ultimo) : ultimo.toFixed(1));
    }

    if (animar && !reduziuMovimento) {
      var linhaEl = svg.querySelector(".chart-line");
      if (linhaEl && linhaEl.getTotalLength) {
        var len = linhaEl.getTotalLength();
        linhaEl.style.strokeDasharray = len;
        linhaEl.style.strokeDashoffset = len;
        linhaEl.getBoundingClientRect();
        linhaEl.style.transition = "stroke-dashoffset .7s ease-out";
        linhaEl.style.strokeDashoffset = "0";
      }
    }

    graficos[nome] = true;
  }

  // Guarda de renderização: sem série não há gráfico, mas o eixo não pode
  // desaparecer sem deixar o vazio explicado (ver `#chart-*-empty`).

  /* ---------------- eventos ---------------- */

  function renderizarEventos() {
    var lista = $("events");
    if (!lista) return;

    var eventos = D.eventosDe(state.fonte);
    var hoje = Date.now();
    var visiveis = eventos.filter(function (ev) {
      return state.filtro === "all" || D.grupo(ev.evento) === state.filtro;
    });

    set("events-count", visiveis.length + " de " + eventos.length + " eventos");

    if (!visiveis.length) {
      lista.innerHTML = '<li class="ev-empty">nenhum evento nesta categoria nesta sessão</li>';
      return;
    }

    var rotulos = {
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

    var classes = {
      track_entered: "ev-entered",
      track_exited: "ev-exited",
      area_transition: "ev-transition",
      pipeline_warning: "ev-warning",
      pipeline_error: "ev-error",
      preview_branch_error: "ev-error",
      rtsp_reconnect_error: "ev-error",
    };

    lista.innerHTML = visiveis.map(function (ev) {
      var hora = hhmmss(new Date(hoje - ev.ha * 1000));
      var cls = classes[ev.evento] || "ev-info";
      return '<li class="ev ' + cls + '">' +
        "<time>" + hora + "</time>" +
        '<span class="ev-name">' + (rotulos[ev.evento] || ev.evento) + "</span>" +
        '<span class="ev-detail">' + ev.detalhe + "</span>" +
        "</li>";
    }).join("");
  }

  /* ---------------- interação ---------------- */

  function ligarInteracao() {
    var lista = $("sources");
    if (lista) {
      lista.addEventListener("click", function (ev) {
        var alvo = ev.target.closest("[data-source]");
        if (alvo) aoTrocarFonte(alvo.dataset.source);
      });
    }

    var btn = $("btn-preview");
    if (btn) btn.addEventListener("click", alternarPreview);

    document.querySelectorAll("[data-filter]").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll("[data-filter]").forEach(function (outro) {
          var ativo = outro === b;
          outro.classList.toggle("active", ativo);
          outro.setAttribute("aria-pressed", ativo ? "true" : "false");
        });
        state.filtro = b.dataset.filter;
        renderizarEventos();
      });
    });

    // setas do teclado entre as fontes: a lista se comporta como um radiogroup
    if (lista) {
      lista.addEventListener("keydown", function (ev) {
        if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
        var itens = Array.prototype.slice.call(lista.querySelectorAll("[data-source]"));
        var i = itens.findIndex(function (el) { return el.dataset.source === state.fonte; });
        var prox = (i + (ev.key === "ArrowRight" ? 1 : itens.length - 1)) % itens.length;
        var botao = itens[prox].querySelector("button");
        if (botao) botao.focus();
        aoTrocarFonte(itens[prox].dataset.source);
        ev.preventDefault();
      });
    }

    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function () {
        var f = D.fonte(state.fonte);
        var s = D.series[f.id] || { fps: [], rss: [] };
        desenharGrafico("fps", s.fps, false);
        desenharGrafico("rss", s.rss, false);
      });
      ["chart-fps", "chart-rss"].forEach(function (id) {
        var el = $(id);
        if (el && el.parentNode) ro.observe(el.parentNode);
      });
    }

    setInterval(function () {
      var f = D.fonte(state.fonte);
      var agora = new Date();
      set("osd-clock", hhmmss(agora));
      var relogio = $("session-clock");
      if (relogio) relogio.textContent = hhmmss(agora);
      if (f.metricas) {
        var seg = f.sessao + (Date.now() - inicio) / 1000;
        set("uptime", duracao(seg));
      }
    }, 1000);
  }

  /* ---------------- boot ---------------- */

  var inicio = Date.now();

  function iniciar() {
    document.body.dataset.status = "loading";
    desenharFontes();
    ligarInteracao();
    set("osd-clock", hhmmss(new Date()));

    // Um quadro de esqueleto curto: é o que a tela faz enquanto o primeiro
    // `/api/snapshot` não chega. Sem isso o painel "pisca" vazio.
    setTimeout(function () {
      document.body.dataset.status = "ready";
      renderizar({ animar: true });
    }, reduziuMovimento ? 60 : 620);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();
