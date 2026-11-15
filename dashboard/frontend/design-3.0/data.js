/* Amostra de dados das versões de estudo do dashboard.
 *
 * Nada aqui é inventado por conveniência: os números vêm de execuções medidas
 * no repositório e estão citados no rodapé de cada versão.
 *
 *   câmera  → regime atual de 7,0 fps; RSS em platô de ~947 MB
 *             (`docs/baselines/2026-10-30-e1t2-baseline.md`,
 *              `data/logs/phase5/longrun-report.md`)
 *   drone   → 29,9 fps processados, RSS 858–882 MB
 *             (`docs/baselines/2026-10-31-drone-campo.md`)
 *   celular → sessão recém-aberta, sem amostra no JSONL (D4: nada atravessa
 *             sessões) — é o estado vazio real do produto
 *
 * As séries são reconstruídas por replay com o mesmo formato da telemetria
 * (uma amostra a cada 15 s, janela de 30 min). O celular fica vazio de
 * propósito: serve para revisar o estado vazio, não para enfeitar o gráfico.
 */
(function () {
  "use strict";

  var JANELA_MIN = 30;
  var PASSO_S = 15;
  var AMOSTRAS = (JANELA_MIN * 60) / PASSO_S; // 120

  /** Congruência linear: mesma série em todo recarregamento (sem "gráfico novo" a cada F5). */
  function rng(seed) {
    var s = seed >>> 0;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  /**
   * Série com início, fim e ruído determinísticos.
   * `curva` < 1 concentra a variação no começo (platô no fim), que é o formato
   * real do RSS depois do warm-up.
   */
  function serie(opts) {
    var r = rng(opts.seed);
    var fase = r() * Math.PI * 2;
    var out = [];
    for (var i = 0; i < AMOSTRAS; i++) {
      var t = AMOSTRAS === 1 ? 1 : i / (AMOSTRAS - 1);
      var base = opts.inicio + (opts.fim - opts.inicio) * Math.pow(t, opts.curva || 1);
      var onda = (opts.onda || 0) * Math.sin(t * Math.PI * 2 * (opts.ciclos || 2) + fase);
      var v = base + onda + (r() - 0.5) * 2 * (opts.ruido || 0);
      out.push(Math.round(v * 100) / 100);
    }
    return out;
  }

  var series = {
    camera: {
      fps: serie({ seed: 20260823, inicio: 7.3, fim: 7.0, curva: 1, ruido: 0.16, onda: 0.18, ciclos: 3 }),
      rss: serie({ seed: 20261030, inicio: 882.0, fim: 947.2, curva: 0.34, ruido: 0.7, onda: 0.5, ciclos: 2 }),
    },
    drone: {
      fps: serie({ seed: 20261031, inicio: 29.88, fim: 29.94, curva: 1, ruido: 0.09, onda: 0.07, ciclos: 2 }),
      rss: serie({ seed: 20261032, inicio: 858.2, fim: 861.9, curva: 0.5, ruido: 0.55, onda: 0.35, ciclos: 1.5 }),
    },
    phone: {
      fps: [],
      rss: [],
    },
  };

  // o último ponto é o valor que o KPI mostra: série e instrumento não divergem
  series.camera.fps[AMOSTRAS - 1] = 7.0;
  series.camera.rss[AMOSTRAS - 1] = 947.2;
  series.drone.fps[AMOSTRAS - 1] = 29.9;
  series.drone.rss[AMOSTRAS - 1] = 861.9;

  var fontes = [
    {
      id: "camera",
      nome: "câmera fixa",
      icone: "camera",
      codec: "h265",
      mux: "1920×1080",
      alvo: 20,
      path: "RTSP_URI (deploy/.env)",
      amostras: AMOSTRAS,
      sessao: 22328, // segundos desde o início da sessão (6 h 12 min)
      estado: {
        area: "OCCUPIED",
        desde: 48 * 60 + 12,
        entradas: 34,
        saidas: 33,
        dwell: 42.6,
        pico: 3,
      },
      metricas: {
        fpsProc: 7.0,
        fpsIn: 7.1,
        fpsPreview: 6.8,
        drop: 0.02,
        rss: 947.2,
        rssBase: 881.9,
        tracks: 2,
        erros: 0,
        frames: 156240,
        threads: "38 / 214",
        ram: "3 330 / 7 619 MB",
        ramPct: "43.7%",
        telemetria: 3,
      },
    },
    {
      id: "drone",
      nome: "drone",
      icone: "drone",
      codec: "h264",
      mux: "960×720",
      alvo: 25,
      path: "rtsp://mediamtx:8554/drone",
      amostras: AMOSTRAS,
      sessao: 1104, // 18 min 24 s
      estado: {
        area: "OCCUPIED",
        desde: 126,
        entradas: 9,
        saidas: 8,
        dwell: 18.4,
        pico: 2,
      },
      metricas: {
        fpsProc: 29.9,
        fpsIn: 29.93,
        fpsPreview: 28.4,
        drop: 0.01,
        rss: 861.9,
        rssBase: 858.2,
        tracks: 1,
        erros: 0,
        frames: 32292,
        threads: "38 / 196",
        ram: "3 104 / 7 619 MB",
        ramPct: "40.7%",
        telemetria: 2,
      },
    },
    {
      id: "phone",
      nome: "celular",
      icone: "phone",
      codec: "h264",
      mux: "1280×720",
      alvo: 25,
      path: "rtsp://mediamtx:8554/phone",
      amostras: 0,
      sessao: 42,
      estado: null,
      metricas: null,
    },
  ];

  /* Eventos com o mesmo vocabulário de `EVENT_LABELS` em app.js.
     `ha` = segundos atrás; a hora é derivada na renderização para a lista
     nunca parecer congelada. */
  var eventos = [
    { fonte: "camera", ha: 132, evento: "track_entered", detalhe: "track 118" },
    { fonte: "camera", ha: 386, evento: "track_exited", detalhe: "track 117 · dwell 61,4 s" },
    { fonte: "camera", ha: 648, evento: "area_transition", detalhe: "OCCUPIED · 2 track(s)" },
    { fonte: "camera", ha: 1092, evento: "pipeline_warning", detalhe: "fps processado 7,0 abaixo do alvo 20" },
    { fonte: "camera", ha: 2761, evento: "track_entered", detalhe: "track 116" },
    { fonte: "camera", ha: 3180, evento: "track_exited", detalhe: "track 115 · dwell 18,2 s" },
    { fonte: "camera", ha: 4820, evento: "pipeline_warning", detalhe: "fila de preview com 2 amostras" },
    { fonte: "camera", ha: 7215, evento: "area_transition", detalhe: "EMPTY · 0 track(s)" },
    { fonte: "camera", ha: 9600, evento: "preview_branch_ready", detalhe: "câmera fixa" },
    { fonte: "camera", ha: 22320, evento: "source_active", detalhe: "câmera fixa · h265" },

    { fonte: "drone", ha: 96, evento: "track_entered", detalhe: "track 41" },
    { fonte: "drone", ha: 132, evento: "area_transition", detalhe: "OCCUPIED · 1 track(s)" },
    { fonte: "drone", ha: 373, evento: "track_exited", detalhe: "track 40 · dwell 22,9 s" },
    { fonte: "drone", ha: 540, evento: "source_active", detalhe: "drone · h264" },
    { fonte: "drone", ha: 1080, evento: "preview_branch_ready", detalhe: "drone" },

    { fonte: "phone", ha: 38, evento: "source_active", detalhe: "celular · h264" },
  ];

  var grupos = {
    track_entered: "tracks",
    track_exited: "tracks",
    area_transition: "tracks",
  };

  var observador = {
    modo: "vigiando",
    motivo: "frames frescos e fps acima do piso da fonte",
    acao: "nenhuma desde 09:20",
    sondas: [
      { id: "camera", nome: "câmera", estado: "viva" },
      { id: "drone", nome: "drone", estado: "viva" },
      { id: "phone", nome: "celular", estado: "não sondada" },
    ],
  };

  window.VSL = {
    janela: { minutos: JANELA_MIN, passo_s: PASSO_S, amostras: AMOSTRAS },
    fontes: fontes,
    series: series,
    eventos: eventos,
    observador: observador,

    fonte: function (id) {
      for (var i = 0; i < fontes.length; i++) if (fontes[i].id === id) return fontes[i];
      return fontes[0];
    },

    /** Grupo do filtro do log: o mesmo critério de `pushEvent` em app.js. */
    grupo: function (evento) {
      if (grupos[evento]) return grupos[evento];
      if (evento.indexOf("error") !== -1 || evento.indexOf("warning") !== -1) return "errors";
      return "all";
    },

    /** Eventos da fonte, mais recente primeiro. */
    eventosDe: function (id) {
      return eventos.filter(function (e) { return e.fonte === id; });
    },
  };
})();
