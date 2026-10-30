/*
 * Publisher WHIP da página de captura (E0-T4) — vanilla, sem build.
 *
 * Porte de `arquo:app/web/src/lib/whipPublisher.ts` (branch
 * `feature/SURVEY-110-m1-phone-ingest-spike`), reduzido ao que o VSL precisa:
 * não há token de sessão (o MediaMTX do VSL não tem autenticação — DIV-003) e
 * não há backend de dashboard no caminho.
 *
 * O caminho (ADR-0005): navegador -> `POST https://<ip>:8889/<path>/whip` com o
 * SDP do offer -> MediaMTX responde `201` + `Location` -> o mesmo stream sai por
 * RTSP no path para o pipeline. Nada de WebSocket de sinalização.
 *
 * Regras que valem revisão:
 *
 * - **H.264 ou nada**: o gate (`CodecGate.pickH264`) roda antes de qualquer SDP
 *   sair, e o offer é conferido de novo na m-line — se o navegador ignorar a
 *   preferência, o publish é recusado em vez de sair em VP8.
 * - **`DELETE` no `Location` ao sair** (`pagehide`/`unload`): o MediaMTX só
 *   libera o path quando a sessão cai. Sem o DELETE o path fica publicado com
 *   um publisher morto e a próxima tentativa recebe `409`.
 * - **Sem `fetch` para `http://`**: mixed content é bloqueado pelo navegador, e
 *   a página é a única origem do experimento. Tudo sai de `location.hostname`.
 * - **Nada de segredo no código**: a página é servida na LAN e não carrega
 *   credencial nenhuma.
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------- *
   * Constantes medidas (D-11). Não são "chute de tuning": 720p a 2,5 Mbps é o
   * ponto que ficou estável em campo no M1 do arquo — acima disso a gravação
   * fragmenta, e evidência fragmentada é pior que evidência em 720p.
   * ---------------------------------------------------------------- */

  /** Teto de espera do `POST`/`DELETE` de sinalização. */
  var REQUEST_TIMEOUT_MS = 10000;

  /** Teto de banda do encoder de vídeo. */
  var MAX_VIDEO_BITRATE_BPS = 2500000;

  /** Alvo de captura: 720p30. `ideal`, não `exact` — o aparelho decide. */
  var TARGET_WIDTH = 1280;
  var TARGET_HEIGHT = 720;
  var TARGET_FPS = 30;

  /** Porta do WHIP no MediaMTX (TLS). */
  var WHIP_PORT = 8889;

  /** Path padrão: o mesmo que o pipeline lê por RTSP. */
  var DEFAULT_PATH = "phone";

  /** Intervalo de leitura de `getStats()`. */
  var STATS_INTERVAL_MS = 1000;

  var TIMEOUT_MESSAGE =
    "O servidor não respondeu em 10 s (Wi-Fi fraco ou MediaMTX fora do ar). " +
    "Confira a rede e tente de novo.";

  var pc = null;
  var stream = null;
  var resourceUrl = null;
  var statsTimer = null;
  var lastSample = null;
  var bitrateCap = null;
  var published = false;

  var el = {};

  function $(id) {
    return document.getElementById(id);
  }

  /* ---------------------------------------------------------------- *
   * Tela
   * ---------------------------------------------------------------- */

  function setStatus(text, kind) {
    if (!el.status) return;
    el.status.textContent = text;
    el.status.className = kind ? "status-" + kind : "";
  }

  function showError(message, detail) {
    if (!el.error) return;
    el.error.textContent = detail ? message + " (" + detail + ")" : message;
    el.error.classList.remove("hidden");
  }

  function clearError() {
    if (!el.error) return;
    el.error.textContent = "";
    el.error.classList.add("hidden");
  }

  function setText(id, value) {
    var node = $(id);
    if (node) node.textContent = value;
  }

  function formatBps(value) {
    if (value === null || value === undefined || !isFinite(value)) return "—";
    return (value / 1000000).toFixed(2) + " Mbps";
  }

  function formatRes(width, height) {
    if (!width || !height) return "—";
    return width + "×" + height;
  }

  function formatFps(value) {
    if (value === null || value === undefined || !isFinite(value)) return "—";
    return value.toFixed(1) + " fps";
  }

  /* ---------------------------------------------------------------- *
   * Endpoints: tudo derivado da origem da página (sem `http://`)
   * ---------------------------------------------------------------- */

  /**
   * URL do recurso WHIP.
   *
   * Default: `https://<hostname da página>:8889/<path>/whip` — o mesmo IP que o
   * celular usou para abrir a página, que é o único que o JavaScript conhece e
   * que está no SAN do certificado. `?whip=` sobrescreve para debug; `?path=`
   * troca o path (o pipeline lê `phone`).
   */
  function whipUrl() {
    var params = new URLSearchParams(window.location.search);
    var override = params.get("whip");
    if (override) return override;
    var pathInput = $("path");
    var typed = pathInput && pathInput.value ? pathInput.value.trim() : "";
    var path = typed || params.get("path") || DEFAULT_PATH;
    return "https://" + window.location.hostname + ":" + WHIP_PORT + "/" +
      encodeURIComponent(path) + "/whip";
  }

  /**
   * URL absoluta do recurso a partir do `Location` da resposta.
   *
   * O MediaMTX 1.21.3 devolve `Location` **relativo** (`/<path>/whip/<uuid>`).
   * Mandar essa string direto no `DELETE` faz o `fetch` resolver contra a origem
   * da **página** (8443, nginx) — volta `200`/`404` do nginx, o `pc` fecha, a
   * sessão WHIP continua viva no MediaMTX e o path segue publicado. Resolver
   * contra a URL do WHIP é o que fecha o recurso certo.
   */
  function resolveResourceUrl(location, base) {
    var raw = (location || "").trim();
    if (!raw) return null;
    try {
      var url = new URL(raw, base);
      if (url.origin !== new URL(base).origin) return null;
      return url.toString();
    } catch (error) {
      return null;
    }
  }

  /** `POST`/`DELETE` com teto de espera (Wi-Fi ruim não pode pendurar a tela). */
  function send(url, method, headers, body, keepalive) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    return fetch(url, {
      method: method,
      headers: headers || {},
      body: body,
      signal: controller.signal,
      keepalive: !!keepalive,
    }).then(function (response) {
      clearTimeout(timer);
      return { kind: "response", response: response };
    }).catch(function (error) {
      clearTimeout(timer);
      return controller.signal.aborted
        ? { kind: "timeout" }
        : { kind: "error", detail: String(error) };
    });
  }

  /* ---------------------------------------------------------------- *
   * Banda: o default do WebRTC é banda de chamada de vídeo
   * ---------------------------------------------------------------- */

  /**
   * Pede ao encoder o teto de banda de 2,5 Mbps.
   *
   * Best-effort de propósito: `setParameters` pode rejeitar (só é confiável
   * depois do `setRemoteDescription`) e **nunca** pode derrubar o publish. O
   * retorno vira texto na tela, porque uma rodada em que o teto não foi
   * aplicado não é uma rodada que testou o teto.
   */
  function raiseVideoBitrate(transceiver) {
    var sender = transceiver && transceiver.sender;
    if (!sender || typeof sender.getParameters !== "function" ||
        typeof sender.setParameters !== "function") {
      return Promise.resolve(false);
    }
    var parameters;
    try {
      parameters = sender.getParameters();
    } catch (error) {
      return Promise.resolve(false);
    }
    var encodings = (parameters && parameters.encodings) || [];
    if (encodings.length === 0) return Promise.resolve(false);
    var updated = Object.assign({}, parameters, {
      encodings: encodings.map(function (encoding) {
        return Object.assign({}, encoding, { maxBitrate: MAX_VIDEO_BITRATE_BPS });
      }),
    });
    return sender.setParameters(updated).then(function () {
      return true;
    }).catch(function () {
      return false;
    });
  }

  /** Aguarda candidatos host, mas não bloqueia o operador indefinidamente. */
  function waitForIceGathering(connection, timeoutMs) {
    if (!connection.iceGatheringState || connection.iceGatheringState === "complete") {
      return Promise.resolve();
    }
    return new Promise(function (resolve) {
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        connection.removeEventListener("icegatheringstatechange", onChange);
        resolve();
      }
      function onChange() {
        if (connection.iceGatheringState === "complete") finish();
      }
      var timer = setTimeout(finish, timeoutMs);
      connection.addEventListener("icegatheringstatechange", onChange);
    });
  }

  /* ---------------------------------------------------------------- *
   * Estatísticas: o que **realmente** está saindo pela rede
   * ---------------------------------------------------------------- */

  function asNumber(value) {
    return typeof value === "number" && isFinite(value) ? value : null;
  }

  /**
   * Traduz o `RTCStatsReport` no vídeo **enviado**.
   *
   * `track.getSettings()` descreve a câmera, não a transmissão: no M1 do arquo
   * ele mostrou 1440×1080/30 fps enquanto o servidor recebia 960×720 a ~18 fps.
   * A única fonte honesta é `outbound-rtp`, e a taxa sai do delta entre duas
   * amostras (`framesSent`/`bytesSent`).
   */
  function streamStatsFrom(report, previous, atMs) {
    var outbound = null;
    report.forEach(function (entry) {
      if (outbound) return;
      if (entry.type !== "outbound-rtp") return;
      if (entry.kind !== "video" && entry.mediaType !== "video") return;
      outbound = entry;
    });
    if (!outbound) return null;

    var frames = asNumber(outbound.framesSent);
    if (frames === null) frames = asNumber(outbound.framesEncoded);
    if (frames === null) frames = 0;
    var bytes = asNumber(outbound.bytesSent);
    if (bytes === null) bytes = 0;
    var sample = { frames: frames, bytes: bytes, atMs: atMs };

    var fps = null;
    var bitrateBps = null;
    if (previous) {
      var elapsedMs = atMs - previous.atMs;
      if (elapsedMs > 0) {
        var deltaFrames = frames - previous.frames;
        // Contador que anda para trás é reinício do encoder: não vira taxa negativa.
        if (deltaFrames >= 0) fps = (deltaFrames * 1000) / elapsedMs;
        var deltaBytes = bytes - previous.bytes;
        if (deltaBytes >= 0) bitrateBps = (deltaBytes * 8000) / elapsedMs;
      }
    }
    if (fps === null) fps = asNumber(outbound.framesPerSecond);

    return {
      stats: {
        width: asNumber(outbound.frameWidth),
        height: asNumber(outbound.frameHeight),
        fps: fps,
        bitrateBps: bitrateBps,
        limitation: typeof outbound.qualityLimitationReason === "string"
          ? outbound.qualityLimitationReason
          : null,
        nackCount: asNumber(outbound.nackCount),
      },
      sample: sample,
    };
  }

  function renderStats(stats) {
    setText("r-out-res", formatRes(stats.width, stats.height));
    setText("r-out-fps", formatFps(stats.fps));
    setText("r-bitrate", formatBps(stats.bitrateBps));
    setText("r-limit", stats.limitation || "—");
    setText("r-nack", stats.nackCount === null ? "—" : String(stats.nackCount));
  }

  function startStats() {
    stopStats();
    statsTimer = setInterval(function () {
      if (!pc || !pc.getStats) return;
      pc.getStats().then(function (report) {
        var reading = streamStatsFrom(report, lastSample, Date.now());
        if (!reading) {
          setText("r-out-res", "—");
          setText("r-out-fps", "—");
          setText("r-bitrate", "—");
          return;
        }
        lastSample = reading.sample;
        renderStats(reading.stats);
      }).catch(function () {
        /* uma leitura perdida não é motivo para tirar a tela do ar */
      });
    }, STATS_INTERVAL_MS);
  }

  function stopStats() {
    if (statsTimer) {
      clearInterval(statsTimer);
      statsTimer = null;
    }
    lastSample = null;
  }

  /* ---------------------------------------------------------------- *
   * Publish
   * ---------------------------------------------------------------- */

  function requestDevice() {
    if (!window.isSecureContext) {
      return Promise.reject(new Error(
        "Esta página não está em contexto seguro (" + window.location.origin +
        "). O Chrome não expõe a câmera em HTTP — abra o endereço HTTPS da porta 8443."));
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error(
        "Este navegador não expõe navigator.mediaDevices. Use o Chrome do Android " +
        "em contexto seguro (HTTPS)."));
    }
    return navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        width: { ideal: TARGET_WIDTH },
        height: { ideal: TARGET_HEIGHT },
        frameRate: { ideal: TARGET_FPS },
      },
    });
  }

  function start() {
    clearError();
    setStatus("abrindo a câmera…", "busy");
    el.btnStart.disabled = true;
    bitrateCap = null;
    setText("r-cap", "não tentado");

    requestDevice().then(function (mediaStream) {
      stream = mediaStream;
      // Prévia local: é o operador conferindo o enquadramento, não o stream que
      // vai para o servidor (esse sai do `outbound-rtp`).
      if (el.preview) {
        el.preview.srcObject = mediaStream;
        el.preview.play().catch(function () {
          /* autoplay bloqueado: a prévia fica no primeiro frame */
        });
      }
      var track = mediaStream.getVideoTracks()[0];
      if (!track) {
        throw new Error("A câmera não entregou nenhuma trilha de vídeo.");
      }
      var settings = track.getSettings ? track.getSettings() : {};
      setText("r-cam", formatRes(settings.width, settings.height) + " · " +
        formatFps(settings.frameRate));

      pc = new RTCPeerConnection({ iceServers: [] });
      pc.addTrack(track, mediaStream);
      var transceivers = pc.getTransceivers();
      var transceiver = transceivers[0];
      if (!transceiver) {
        throw new Error("Não há transceiver de vídeo para publicar.");
      }
      // Publicar, não receber: é o que o WHIP espera do lado do cliente.
      try {
        transceiver.direction = "sendonly";
      } catch (error) {
        /* implementação sem direção ajustável ainda publica: segue */
      }

      // Gate D-2: roda ANTES de qualquer SDP sair daqui.
      var gate = window.CodecGate.pickH264(transceiver);
      if (!gate.ok) {
        var gateError = new Error(gate.message);
        gateError.codecGate = true;
        throw gateError;
      }
      setText("r-codec", gate.codecs.length + " perfil(is) H.264 fixado(s)");

      // Antes do offer, para o encoder já nascer com o teto certo. Pode ser
      // recusado aqui (sender ainda sem encoding negociado) — segunda chance
      // depois do answer.
      return raiseVideoBitrate(transceiver).then(function (applied) {
        if (applied) {
          bitrateCap = true;
          setText("r-cap", "2,5 Mbps aplicado");
        }
        return pc.createOffer().then(function (offer) {
          return pc.setLocalDescription(offer);
        }).then(function () {
          return waitForIceGathering(pc, 2000);
        }).then(function () {
          return publish(transceiver);
        });
      });
    }).catch(function (error) {
      fail(error.codecGate ? error.message : String(error.message || error));
    });
  }

  function publish(transceiver) {
    var sdp = (pc.localDescription && pc.localDescription.sdp) || "";
    // Lição do gate v1: quem diz a ordem efetiva é a m-line, não `getParameters()`.
    if (!window.CodecGate.h264IsFirstInOffer(sdp)) {
      throw new Error(window.CodecGate.OFFER_WITHOUT_H264_MESSAGE);
    }
    setStatus("negociando com o MediaMTX…", "busy");
    var url = whipUrl();
    setText("r-whip", url);

    return send(url, "POST", {
      "Content-Type": "application/sdp",
      Accept: "application/sdp",
    }, sdp).then(function (outcome) {
      if (outcome.kind === "timeout") throw new Error(TIMEOUT_MESSAGE);
      if (outcome.kind === "error") {
        throw new Error("Não foi possível alcançar o servidor de vídeo. " +
          "Confira se o MediaMTX está no ar em https://" + window.location.hostname +
          ":8889 (" + outcome.detail + ")");
      }
      return outcome.response;
    }).then(function (response) {
      if (response.status < 200 || response.status >= 300) {
        return response.text().then(function (body) {
          throw new Error("O servidor recusou a publicação (HTTP " + response.status +
            "): " + body.slice(0, 200));
        });
      }
      resourceUrl = resolveResourceUrl(response.headers.get("location"), url);
      setText("r-session", response.status + " · " +
        (resourceUrl ? "sessão aberta" : "sem Location (DELETE indisponível)"));
      return response.text();
    }).then(function (answer) {
      if (!answer || !answer.trim()) {
        throw new Error("O servidor aceitou o publish mas não devolveu o SDP de resposta.");
      }
      return pc.setRemoteDescription({ type: "answer", sdp: answer });
    }).then(function () {
      // Segunda (e última) tentativa: é o ponto em que o Chrome aceita
      // `setParameters` de forma confiável.
      if (!bitrateCap) {
        return raiseVideoBitrate(transceiver).then(function (applied) {
          bitrateCap = applied;
          setText("r-cap", applied
            ? "2,5 Mbps aplicado (após o answer)"
            : "recusado — transmitindo no default do navegador");
        });
      }
      return null;
    }).then(function () {
      published = true;
      el.btnStop.disabled = false;
      pc.addEventListener("connectionstatechange", function () {
        var state = pc.connectionState;
        if (state === "connected") setStatus("publicando", "ok");
        else if (state === "disconnected") setStatus("conexão instável", "warn");
        else if (state === "failed") {
          setStatus("conexão perdida", "bad");
          showError("A conexão de vídeo caiu. O path já não recebe frames; " +
            "pare e comece de novo.");
        }
      });
      setStatus("publicando", "ok");
      startStats();
    }).catch(function (error) {
      fail(error.message || String(error));
    });
  }

  function fail(message) {
    showError(message);
    setStatus("falhou", "bad");
    el.btnStart.disabled = false;
    el.btnStop.disabled = true;
    published = false;
    stopStats();
    var openResource = resourceUrl;
    resourceUrl = null;
    if (pc) {
      try {
        pc.close();
      } catch (error) {
        /* já fechado */
      }
      pc = null;
    }
    stopTracks();
    if (openResource) {
      // A sessão pode ter subido antes do erro: apaga para não deixar o path
      // publicado por um cliente que já desistiu (`409` na próxima tentativa).
      send(openResource, "DELETE", {}).catch(function () {});
    }
  }

  /**
   * Encerra o publish: `DELETE` no recurso WHIP e fecha a conexão.
   *
   * `keepalive` é o que permite o DELETE sair durante o `pagehide`; sem ele o
   * navegador descarta a requisição ao navegar e o path fica preso no MediaMTX
   * até o ICE expirar.
   */
  function stop(options) {
    var keepalive = !!(options && options.keepalive);
    var url = resourceUrl;
    resourceUrl = null;
    published = false;
    stopStats();
    el.btnStart.disabled = false;
    el.btnStop.disabled = true;
    setText("r-out-res", "—");
    setText("r-out-fps", "—");
    setText("r-bitrate", "—");
    setText("r-limit", "—");
    if (url) {
      send(url, "DELETE", {}, undefined, keepalive).then(function (outcome) {
        setText("r-session", outcome.kind === "response"
          ? "encerrada (HTTP " + outcome.response.status + ")"
          : "DELETE falhou — o MediaMTX vai expirar a sessão");
      });
    } else {
      setText("r-session", "—");
    }
    if (pc) {
      try {
        pc.close();
      } catch (error) {
        /* já fechado */
      }
      pc = null;
    }
    stopTracks();
    if (!keepalive) setStatus("parado", "idle");
  }

  function stopTracks() {
    if (!stream) return;
    stream.getTracks().forEach(function (track) {
      track.stop();
    });
    stream = null;
    if (el.preview) el.preview.srcObject = null;
    setText("r-cam", "—");
  }

  /* ---------------------------------------------------------------- *
   * Bootstrap
   * ---------------------------------------------------------------- */

  function init() {
    el.status = $("status");
    el.error = $("error");
    el.btnStart = $("btn-start");
    el.btnStop = $("btn-stop");
    el.preview = $("preview");

    setText("r-whip", whipUrl());
    var pathInput = $("path");
    if (pathInput) {
      pathInput.addEventListener("input", function () {
        setText("r-whip", whipUrl());
      });
    }
    setText("r-secure", window.isSecureContext
      ? "sim (" + window.location.origin + ")"
      : "NÃO (" + window.location.origin + ")");
    if (!window.isSecureContext) {
      showError("Esta página não está em contexto seguro. O Chrome não libera a câmera " +
        "em HTTP: abra https://" + window.location.hostname + ":8443/capture.html " +
        "com a CA instalada no aparelho.");
    }
    setText("r-req", formatRes(TARGET_WIDTH, TARGET_HEIGHT) + " · " +
      formatFps(TARGET_FPS) + " (ideal)");

    var capabilities = window.CodecGate.defaultGetCapabilities();
    if (!capabilities) {
      setText("r-caps", "API ausente (contexto inseguro ou navegador antigo)");
    } else {
      var h264 = capabilities.filter(function (codec) {
        return codec && typeof codec.mimeType === "string" &&
          codec.mimeType.toLowerCase() === "video/h264";
      });
      setText("r-caps", h264.length > 0
        ? h264.length + " perfil(is) H.264 disponíveis"
        : "nenhum H.264 disponível — este aparelho não pode publicar");
    }

    el.btnStart.disabled = false;
    el.btnStop.disabled = true;
    el.btnStart.addEventListener("click", start);
    el.btnStop.addEventListener("click", function () {
      stop();
    });

    // O MediaMTX só libera o path quando a sessão cai: fechar a aba sem DELETE
    // deixa o publisher morto ocupando o path (a próxima tentativa recebe 409).
    window.addEventListener("pagehide", function () {
      if (published || resourceUrl) stop({ keepalive: true });
    });
    window.addEventListener("beforeunload", function () {
      if (published || resourceUrl) stop({ keepalive: true });
    });
  }

  // Exposto para inspeção no console do aparelho e para o autoteste da página
  // irmã não duplicar a lógica de estatística.
  window.CapturePage = {
    streamStatsFrom: streamStatsFrom,
    resolveResourceUrl: resolveResourceUrl,
    whipUrl: whipUrl,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
