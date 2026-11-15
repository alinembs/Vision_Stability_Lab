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

  /**
   * Câmera escolhida pelo operador (`environment` = traseira).
   *
   * Mesma convenção das outras preferências locais da página (`vsl-theme`):
   * `localStorage`, sem backend. O padrão é a **traseira** porque é a câmera
   * que aponta para a cena que o pipeline mede — a frontal do Chrome Android
   * abria por padrão e enquadrava o operador, não o ambiente.
   */
  var FACING_STORAGE_KEY = "vsl-facing";
  var DEFAULT_FACING = "environment";

  /**
   * Selo de versão deste arquivo, impresso no rodapé e no console.
   *
   * A página é servida por bind mount, mas o celular pode ter carregado um
   * `capture.js` antigo do cache (ou de outro host). Sem um selo visível não há
   * como provar, em campo, qual JS o aparelho executou — e a investigação da
   * câmera traseira travou justamente nessa dúvida. Suba esta data a cada
   * mudança neste arquivo.
   */
  var CAPTURE_VERSION = "2026-11-15a";

  /**
   * Dispositivo físico escolhido pelo operador (`deviceId`).
   *
   * `facingMode` não é garantia: o Chrome Android pode aceitar
   * `exact environment` e entregar a frontal mesmo assim. O `deviceId` é o
   * único pedido que amarra a câmera física. Vazio = cadeia de `facingMode`,
   * que é o comportamento anterior.
   */
  var DEVICE_STORAGE_KEY = "vsl-facing-device";

  /**
   * Aviso de retrato (não bloqueia a publicação: é aviso, não gate).
   *
   * O `nvstreammux` do pipeline é fixo em 1280×720 (`deepstream/pipeline_real.py`)
   * e roda **sem** `enable-padding`: um frame retrato chega 720 de altura e ~404
   * de largura, e o mux estica para 16:9. A imagem sai deformada, então o custo
   * de girar o aparelho é menor que o de gravar evidência esticada.
   */
  var PORTRAIT_MESSAGE =
    "A câmera está entregando retrato. Gire o aparelho para paisagem: em " +
    "retrato o teto de 720p reduz a captura (ex.: 404×720) e o pipeline " +
    "reescala para 1280×720 sem preservar o aspecto, então a imagem sai " +
    "esticada. A publicação continua — o aviso é de enquadramento.";

  /**
   * Formato de path aceito na URL do WHIP.
   *
   * Sem validação, `phone/whip` (ou um path colado de outro lugar) vira
   * `…/phone%2Fwhip/whip` depois do `encodeURIComponent` e o MediaMTX
   * responde 404 — sintoma que parece servidor fora do ar.
   */
  var PATH_RE = /^[A-Za-z0-9_-]+$/;

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
  /** Qual candidato de `facingMode` o `getUserMedia` aceitou (diagnóstico). */
  var lastFacingAttempt = null;
  /** Rótulo cru de cada `deviceId`, preenchido por `enumerateDevices`. */
  var deviceLabels = Object.create(null);
  /** Se os controles de câmera estão travados (publicação em andamento). */
  var controlsLocked = false;

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
    // O banner é compartilhado com o aviso de retrato; a classe de aviso não
    // pode sobreviver a um erro de verdade.
    el.error.classList.remove("alert-warn");
    el.error.classList.remove("hidden");
  }

  /**
   * Aviso que não interrompe o fluxo (hoje: captura em retrato).
   *
   * Reaproveita o banner `#error` em vez de criar componente novo, mas com a
   * cor de `warn`: pintar de vermelho algo que a captura segue fazendo ensina o
   * operador a ignorar vermelho. `showError` limpa a classe para o próximo erro
   * voltar a ser vermelho.
   */
  function showWarning(message) {
    if (!el.error) return;
    el.error.textContent = message;
    el.error.classList.add("alert-warn");
    el.error.classList.remove("hidden");
  }

  function clearError() {
    if (!el.error) return;
    el.error.textContent = "";
    el.error.classList.remove("alert-warn");
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
   * Path pedido: o campo da tela, `?path=` ou `phone`.
   *
   * É o único que vale para a URL do WHIP e para o `rtsp://` mostrado ao
   * operador — os dois não podem divergir.
   */
  function currentPath() {
    var params = new URLSearchParams(window.location.search);
    var pathInput = $("path");
    var typed = pathInput && pathInput.value ? pathInput.value.trim() : "";
    return typed || params.get("path") || DEFAULT_PATH;
  }

  /**
   * URL do recurso WHIP.
   *
   * Default: `https://<hostname da página>:8889/<path>/whip` — o mesmo IP que o
   * celular usou para abrir a página, que é o único que o JavaScript conhece e
   * que está no SAN do certificado. `?whip=` sobrescreve para debug; `?path=`
   * troca o path (o pipeline lê `phone`).
   *
   * Devolve `null` quando o path não é válido: quem chama mostra o erro na tela
   * em vez de publicar num endpoint que não existe (404 enganoso).
   */
  function whipUrl() {
    var params = new URLSearchParams(window.location.search);
    var override = params.get("whip");
    if (override) {
      // `?whip=` é atalho de depuração e a sinalização do E0-T4 é HTTPS:
      // `http://` aqui seria bloqueado como mixed content, e qualquer esquema
      // aceito faria a câmera publicar em outra máquina. Esquema errado é
      // ignorado (com aviso no console), não aceito.
      if (/^https:\/\//i.test(override)) return override;
      console.warn("?whip= ignorado: exige https:// — recebido:", override);
    }
    var path = currentPath();
    if (!PATH_RE.test(path)) return null;
    return "https://" + window.location.hostname + ":" + WHIP_PORT + "/" +
      encodeURIComponent(path) + "/whip";
  }

  /**
   * URL absoluta do recurso a partir do `Location` da resposta.
   *
   * O MediaMTX 1.21.3 devolve `Location` **relativo** (`/<path>/whip/<uuid>`).
   * Mandar essa string direto no `DELETE` faz o `fetch` resolver contra a origem
   * da **página** (8444 no host, nginx) — volta `200`/`404` do nginx, o `pc` fecha, a
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

  /* ---------------------------------------------------------------- *
   * Câmera: traseira por padrão, paisagem como alvo
   * ---------------------------------------------------------------- */

  /**
   * Câmera escolhida, lida de `localStorage`.
   *
   * `try/catch` porque em navegação privada o `localStorage` existe mas lança em
   * `getItem`; preferência de câmera não pode ser motivo para a captura não
   * abrir. Valor desconhecido (versão antiga, storage editado) volta ao padrão.
   */
  function readFacing() {
    try {
      var stored = localStorage.getItem(FACING_STORAGE_KEY);
      if (stored === "environment" || stored === "user") return stored;
    } catch (error) {
      /* sem storage: fica o padrão */
    }
    return DEFAULT_FACING;
  }

  function saveFacing(value) {
    try {
      localStorage.setItem(FACING_STORAGE_KEY, value);
    } catch (error) {
      /* sem storage: a escolha vale só para esta aba */
    }
  }

  /** Nome da câmera como o operador lê na tela. */
  function facingLabel(mode) {
    if (mode === "environment") return "traseira";
    if (mode === "user") return "frontal";
    return "—";
  }

  /* ---------------------------------------------------------------- *
   * Dispositivo: o caminho garantido quando `facingMode` não basta
   * ---------------------------------------------------------------- */

  /**
   * `deviceId` gravado pelo operador, lido de `localStorage`.
   *
   * Mesmo `try/catch` de `readFacing`: navegação privada pode lançar em
   * `getItem`, e preferência de câmera não pode ser motivo para a captura não
   * abrir. Valor vazio = usar a cadeia de `facingMode`.
   */
  function readStoredDevice() {
    try {
      var stored = localStorage.getItem(DEVICE_STORAGE_KEY);
      if (stored) return stored;
    } catch (error) {
      /* sem storage: fica a cadeia de facingMode */
    }
    return "";
  }

  function saveStoredDevice(value) {
    try {
      if (value) localStorage.setItem(DEVICE_STORAGE_KEY, value);
      else localStorage.removeItem(DEVICE_STORAGE_KEY);
    } catch (error) {
      /* sem storage: a escolha vale só para esta aba */
    }
  }

  /** `deviceId` completo não cabe na tela; o prefixo basta para comparar. */
  function shortDeviceId(value) {
    if (!value) return "";
    return value.length > 8 ? value.slice(0, 8) + "…" : value;
  }

  /**
   * Nome do aparelho para a linha "aparelho" do diagnóstico.
   *
   * O rótulo cru vem de `enumerateDevices` (ex.: "camera2 0, facing back") e só
   * existe depois da permissão. Sem rótulo sobra o `deviceId` truncado — é o
   * que dá para comparar com a lista do seletor.
   */
  function describeDevice(deviceId) {
    if (!deviceId) return "—";
    var label = deviceLabels[deviceId];
    if (label) return label;
    return "deviceId " + shortDeviceId(deviceId);
  }

  function deviceOptionExists(deviceId) {
    if (!deviceId || !el.device) return false;
    var options = el.device.options || [];
    for (var i = 0; i < options.length; i++) {
      if (options[i].value === deviceId) return true;
    }
    return false;
  }

  /**
   * Remonta a lista de dispositivos, já com a trilha ativa selecionada.
   *
   * Antes do primeiro `getUserMedia` o Chrome devolve rótulos vazios; por isso a
   * enumeração roda **depois** do sucesso. Falha de enumeração (API ausente,
   * aparelho estranho) não muda nada: sem lista, a página continua na cadeia de
   * `facingMode`, que é o comportamento anterior.
   *
   * O valor escrito aqui é **estado**, não preferência: com o controle travado o
   * `change` é ignorado, então o resultado de um fallback nunca vira a escolha
   * da próxima captura.
   */
  function refreshDeviceList(activeDeviceId) {
    if (!el.device || !navigator.mediaDevices ||
        typeof navigator.mediaDevices.enumerateDevices !== "function") {
      return Promise.resolve(false);
    }
    return navigator.mediaDevices.enumerateDevices().then(function (devices) {
      var videos = [];
      devices.forEach(function (device) {
        if (device && device.kind === "videoinput") videos.push(device);
      });
      if (videos.length === 0) return false;

      while (el.device.firstChild) el.device.removeChild(el.device.firstChild);

      // A primeira opção é o caminho de sempre: sem `deviceId`, a página pede a
      // traseira por `facingMode` e deixa o aparelho decidir.
      var auto = document.createElement("option");
      auto.value = "";
      auto.textContent = "Automática (traseira/frontal)";
      el.device.appendChild(auto);

      deviceLabels = Object.create(null);
      videos.forEach(function (device, index) {
        var label = (device.label || "").trim();
        var option = document.createElement("option");
        option.value = device.deviceId || "";
        // Rótulo vazio é comum em aparelho que não informa o nome: um índice
        // legível ("câmera 2") vale mais que uma opção em branco.
        option.textContent = label || "câmera " + (index + 1);
        // Guarda o texto que o operador vê, não o rótulo cru: assim a linha
        // "aparelho" do diagnóstico fala a mesma língua do seletor.
        deviceLabels[device.deviceId || ""] = option.textContent;
        el.device.appendChild(option);
      });

      var wanted = activeDeviceId || readStoredDevice();
      el.device.value = deviceOptionExists(wanted) ? wanted : "";
      // A lista só existe depois da permissão: se a captura já está publicando,
      // o seletor recém-preenchido nasce travado.
      el.device.disabled = controlsLocked;
      // Reforça a linha "aparelho" agora que o rótulo existe: na primeira
      // captura a enumeração termina depois do `setText` de `start`, e sem isto
      // o operador só veria o `deviceId` truncado.
      if (activeDeviceId) setText("r-device", describeDevice(activeDeviceId));
      return true;
    }).catch(function () {
      /* sem enumeração: tudo funciona como antes (facingMode) */
      return false;
    });
  }

  /**
   * Cadeia de pedidos de câmera, do mais específico ao mais permissivo.
   *
   * `exact` é o único que **garante** a câmera certa: com `ideal` o Chrome pode
   * aceitar a traseira e entregar a frontal sem erro nenhum — foi o que
   * aconteceu antes, com a frontal abrindo por padrão. Mas `exact` derruba o
   * aparelho que não tem a câmera pedida (`OverconstrainedError`), e nesse caso
   * um fallback honesto vale mais que a tela de erro: aparelho só com frontal
   * publica pela frontal (o diagnóstico mostra "frontal" mesmo tendo pedido
   * "traseira"). O último candidato nem manda `facingMode` — é a saída para
   * sensor que não se identifica.
   */
  function facingChain(facing) {
    return [
      {
        facing: facing,
        constraint: { exact: facing },
        label: facingLabel(facing) + " (exact)",
      },
      {
        facing: facing,
        constraint: { ideal: facing },
        label: facingLabel(facing) + " (ideal)",
      },
      {
        facing: null,
        constraint: null,
        label: "qualquer câmera (sem facingMode)",
      },
    ];
  }

  /**
   * Tetos de captura comuns aos dois caminhos (facingMode e deviceId).
   *
   * `ideal` sozinho não é teto: o aparelho podia entregar 1080p e o encoder
   * gastar banda à toa (o pedido tem de ser cumprido como limite). `max` fecha o
   * teto sem exigir o valor exato — `exact` derrubaria aparelhos que não têm
   * 720p. `aspectRatio` é `ideal` de propósito: com `exact` um aparelho que só
   * entrega retrato ficaria sem câmera — e retrato aqui é aviso, não bloqueio.
   */
  function baseVideoConstraints() {
    return {
      width: { ideal: TARGET_WIDTH, max: TARGET_WIDTH },
      height: { ideal: TARGET_HEIGHT, max: TARGET_HEIGHT },
      frameRate: { ideal: TARGET_FPS },
      // Paisagem é o alvo do pipeline (16:9 em 1280×720).
      aspectRatio: { ideal: 16 / 9 },
    };
  }

  /** Câmera ocupada é erro fatal: trocar calado publicaria a imagem errada. */
  function cameraBusyError(facing) {
    // `facing === null` é o candidato "qualquer câmera": não existe rótulo para
    // citar, e "A câmera — está em uso" não se lê.
    var alvo = facing ? "A câmera " + facingLabel(facing) : "A câmera escolhida";
    return new Error(
      alvo + " está em uso por outro aplicativo (ou por outra aba publicando). " +
      "Feche-o e tente de novo.");
  }

  /** `NotReadableError` e primos: câmera ocupada, não constraint inválida. */
  function isCameraBusy(name) {
    return name === "NotReadableError" || name === "TrackStartError" ||
      name === "AbortError";
  }

  /**
   * Tenta o candidato `index`; `OverconstrainedError`/`NotFoundError` só fazem
   * cair para o próximo. Erro de permissão (`NotAllowedError`), câmera ocupada
   * (`NotReadableError`) e falta de `mediaDevices` continuam fatais: insistir
   * neles só repetiria o mesmo erro — e, no caso da câmera ocupada, trocar de
   * câmera calada publicaria a imagem errada.
   */
  function tryFacingCandidate(chain, index, lastError) {
    if (index >= chain.length) {
      // Nenhum candidato resolveu: o erro do último (o mais permissivo) é o que
      // descreve o aparelho — pedir a câmera "errada" não é o problema.
      return Promise.reject(lastError || new Error("Nenhuma câmera disponível."));
    }
    var candidate = chain[index];
    var video = baseVideoConstraints();
    if (candidate.constraint) video.facingMode = candidate.constraint;
    return navigator.mediaDevices.getUserMedia({
      audio: false,
      video: video,
    }).then(function (mediaStream) {
      lastFacingAttempt = candidate.label;
      return mediaStream;
    }).catch(function (error) {
      var name = error && error.name;
      if (isCameraBusy(name)) {
        // Câmera ocupada por outro app ou por outra aba publicando. Cair para a
        // outra câmera aqui publicaria a imagem errada **sem o operador
        // perceber** — pior que falhar. Erro fatal, com a causa em português.
        throw cameraBusyError(candidate.facing);
      }
      var retriable = name === "OverconstrainedError" ||
        name === "ConstraintNotSatisfiedError" || name === "NotFoundError";
      if (!retriable) throw error;
      return tryFacingCandidate(chain, index + 1, error);
    });
  }

  /**
   * Pedido por `deviceId`: o único que amarra a câmera física.
   *
   * Duas tentativas de propósito. A primeira mantém os tetos do caminho normal
   * (escolher o dispositivo não muda a qualidade pedida). Se ela falhar com
   * `OverconstrainedError`, **não dá para concluir que o id morreu**: o conflito
   * pode ser do teto de 720p/16:9 com o que aquele sensor oferece. A segunda
   * tentativa pede o mesmo dispositivo com resolução só `ideal` — sem `max`, sem
   * `aspectRatio` — e, se essa também falhar, aí sim a preferência é descartada
   * e a página volta para a cadeia de `facingMode`.
   */
  function tryDeviceCandidate(deviceId, facing) {
    var video = baseVideoConstraints();
    video.deviceId = { exact: deviceId };
    return navigator.mediaDevices.getUserMedia({
      audio: false,
      video: video,
    }).then(function (mediaStream) {
      lastFacingAttempt = deviceAttemptLabel(deviceId, "");
      return mediaStream;
    }).catch(function (error) {
      var name = error && error.name;
      if (isCameraBusy(name)) throw cameraBusyError(facing);
      if (name === "NotFoundError") {
        return discardDevicePreference(name, facing, error);
      }
      if (name !== "OverconstrainedError" &&
          name !== "ConstraintNotSatisfiedError") {
        throw error;
      }
      // Segunda chance: mesmo aparelho, resolução sem teto rígido. `ideal`
      // ainda pede 720p — o aparelho que não aceita entrega o que tem, e o
      // encoder do celular (2,5 Mbps) e o `nvstreammux` (1280×720) fecham a
      // conta adiante.
      return navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          deviceId: { exact: deviceId },
          width: { ideal: TARGET_WIDTH },
          height: { ideal: TARGET_HEIGHT },
          frameRate: { ideal: TARGET_FPS },
        },
      }).then(function (mediaStream) {
        lastFacingAttempt = deviceAttemptLabel(deviceId, " sem teto rígido");
        return mediaStream;
      }).catch(function (secondError) {
        var secondName = secondError && secondError.name;
        if (isCameraBusy(secondName)) throw cameraBusyError(facing);
        if (secondName === "OverconstrainedError" ||
            secondName === "ConstraintNotSatisfiedError" ||
            secondName === "NotFoundError") {
          return discardDevicePreference(secondName, facing, error);
        }
        throw secondError;
      });
    });
  }

  /** Texto da linha "câmera pedida" para o caminho por `deviceId`. */
  function deviceAttemptLabel(deviceId, sufixo) {
    var label = deviceLabels[deviceId];
    return "deviceId " + shortDeviceId(deviceId) +
      (label ? " (" + label + ")" : "") + (sufixo || "");
  }

  /**
   * Id que não serve mais: limpa a preferência e volta para `facingMode`.
   *
   * Descartar é melhor que falhar: um `deviceId` de outra sessão/aparelho não
   * pode impedir a captura. O diagnóstico diz que descartou e por quê — se
   * acontecer sempre, o problema é do sensor, não da página.
   */
  function discardDevicePreference(name, facing, originalError) {
    saveStoredDevice("");
    if (el.device) el.device.value = "";
    setText("r-device", "preferência descartada (" + (name || "erro") + ")");
    return tryFacingCandidate(facingChain(facing), 0, originalError);
  }

  function requestDevice() {
    if (!window.isSecureContext) {
      return Promise.reject(new Error(
        "Esta página não está em contexto seguro (" + window.location.origin +
        "). O Chrome não expõe a câmera em HTTP — abra o endereço HTTPS da porta 8444."));
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error(
        "Este navegador não expõe navigator.mediaDevices. Use o Chrome do Android " +
        "em contexto seguro (HTTPS)."));
    }
    var facing = chosenFacing();
    var deviceId = readStoredDevice();
    if (!deviceId) return tryFacingCandidate(facingChain(facing), 0, null);
    // Lista já carregada e o id não está nela: preferência morta. Não vale
    // gastar um `getUserMedia` que só pode terminar em `OverconstrainedError`.
    if (el.device && el.device.options.length > 1 &&
        !deviceOptionExists(deviceId)) {
      saveStoredDevice("");
      el.device.value = "";
      setText("r-device", "preferência descartada (aparelho não está na lista)");
      return tryFacingCandidate(facingChain(facing), 0, null);
    }
    return tryDeviceCandidate(deviceId, facing);
  }

  /**
   * Câmera a pedir agora: o seletor manda, o `localStorage` é o fallback.
   *
   * O seletor é **preferência do operador**, e nunca é escrito pelo código: se
   * o `getUserMedia` cair para outra câmera, quem conta a verdade são as linhas
   * "câmera pedida" e "câmera (getSettings)" do diagnóstico. Reescrever o
   * controle com o resultado faria a próxima captura pedir a câmera do fallback
   * como se o operador tivesse escolhido ela.
   */
  function chosenFacing() {
    if (el.facing) {
      var value = el.facing.value;
      if (value === "user" || value === "environment") return value;
    }
    return readFacing();
  }

  /**
   * Retrato = o frame que vai ser publicado tem altura maior que largura.
   *
   * A fonte é `getSettings()` da trilha, não `screen.orientation`: com o
   * aparelho deitado o navegador pode continuar entregando o frame girado, e
   * quem sofre o reescalonamento é o frame, não a tela.
   */
  function isPortrait(settings) {
    return !!(settings && settings.width && settings.height &&
      settings.height > settings.width);
  }

  /** Trava/destrava os seletores de câmera (troca só entre capturas). */
  function setFacingLocked(locked) {
    controlsLocked = !!locked;
    if (el.facing) el.facing.disabled = controlsLocked;
    if (el.facingNote) el.facingNote.classList.toggle("hidden", !controlsLocked);
    // O seletor de dispositivo segue a mesma regra: a escolha só vale no próximo
    // início. Antes da primeira permissão ele tem só "automática", que é
    // exatamente o comportamento antigo — por isso não precisa nascer travado.
    if (el.device) el.device.disabled = controlsLocked;
    if (el.deviceNote) el.deviceNote.classList.toggle("hidden", !controlsLocked);
  }

  function start() {
    clearError();
    setStatus("Abrindo a câmera…", "busy");
    el.btnStart.disabled = true;
    // Trava antes do `getUserMedia`: trocar o seletor no meio do pedido abriria
    // uma câmera diferente da que o operador acha que escolheu.
    setFacingLocked(true);
    bitrateCap = null;
    lastFacingAttempt = null;
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
      // "aparelho" é o `deviceId` efetivo: se o operador escolheu um aparelho
      // com `facing back` e esta linha mostra outro, o pedido não foi cumprido
      // (sensor/Chrome) — não é a página.
      setText("r-device", describeDevice(settings.deviceId));
      // A lista de dispositivos só fica completa depois da permissão: remonta
      // aqui, já com a trilha ativa selecionada. Sem `enumerateDevices` a
      // promise resolve `false` e nada muda.
      refreshDeviceList(settings.deviceId);
      // "câmera (getSettings)" agora diz também **qual** câmera o aparelho
      // escolheu: a linha existe para separar pedido de realidade.
      setText("r-cam", formatRes(settings.width, settings.height) + " · " +
        formatFps(settings.frameRate) + " · " + facingLabel(settings.facingMode));
      setText("r-facing", lastFacingAttempt || "—");
      setText("r-orient", isPortrait(settings) ? "retrato" : "paisagem");
      if (isPortrait(settings)) {
        // Aviso, não gate: o pipeline recebe a imagem esticada, e é melhor
        // gravar assim do que não gravar. O banner (e não `setStatus`) porque o
        // estado é sobrescrito por `connectionstatechange` em segundos.
        showWarning(PORTRAIT_MESSAGE);
      }

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
    setStatus("Negociando com o MediaMTX…", "busy");
    var url = whipUrl();
    if (!url) {
      throw new Error("Path inválido: \"" + currentPath() + "\". Use apenas " +
        "letras, dígitos, hífen e _ (sem barras nem espaço) — é o nome do " +
        "path no MediaMTX, ex.: phone.");
    }
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
        if (state === "connected") setStatus("Publicando", "ok");
        else if (state === "disconnected") setStatus("Conexão instável", "warn");
        else if (state === "failed") {
          setStatus("Conexão perdida", "bad");
          showError("A conexão de vídeo caiu. O path já não recebe frames; " +
            "pare e comece de novo.");
        }
      });
      setStatus("Publicando", "ok");
      startStats();
    }).catch(function (error) {
      fail(error.message || String(error));
    });
  }

  function fail(message) {
    showError(message);
    setStatus("Falhou", "bad");
    el.btnStart.disabled = false;
    el.btnStop.disabled = true;
    // Falhou: não há captura para trocar, o seletor volta a valer.
    setFacingLocked(false);
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
    setFacingLocked(false);
    // O aviso de retrato é sobre a captura que acabou: deixá-lo na tela depois
    // de "Encerrar" faria o operador procurar um problema que já não existe.
    clearError();
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
    if (!keepalive) setStatus("Parado", "idle");
  }

  function stopTracks() {
    if (!stream) return;
    stream.getTracks().forEach(function (track) {
      track.stop();
    });
    stream = null;
    if (el.preview) el.preview.srcObject = null;
    setText("r-cam", "—");
    setText("r-device", "—");
    setText("r-facing", "—");
    setText("r-orient", "—");
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
    el.facing = $("facing");
    el.facingNote = $("facing-note");
    el.device = $("device");
    el.deviceNote = $("device-note");

    // Selo de versão: é o que prova, no celular, qual `capture.js` o aparelho
    // carregou (bind mount + cache do Chrome tornam a dúvida real). Também no
    // console, para quando o rodapé está apertado em paisagem.
    setText("build-line", "capture.js " + CAPTURE_VERSION);
    console.info("capture.js " + CAPTURE_VERSION);

    // Seletor de câmera: o valor gravado volta antes de qualquer captura; a
    // gravação acontece só na escolha do operador (troca programática não é
    // preferência dele).
    if (el.facing) {
      el.facing.value = readFacing();
      el.facing.addEventListener("change", function () {
        // `change` com o controle travado não é escolha do operador (é o
        // código escrevendo): gravar aqui transformaria o fallback de uma
        // captura na preferência da próxima.
        if (el.facing.disabled) return;
        saveFacing(el.facing.value === "user" ? "user" : "environment");
      });
    }
    // Seletor de dispositivo: a escolha gravada vale já no próximo start. A
    // lista nasce vazia (o Chrome só revela os aparelhos depois da permissão) e
    // é remontada em `refreshDeviceList`. Mesma regra do `facing`: programático
    // não grava.
    if (el.device) {
      el.device.addEventListener("change", function () {
        if (el.device.disabled) return;
        saveStoredDevice(el.device.value || "");
      });
    }
    setFacingLocked(false);

    setText("r-whip", whipUrl() || "path inválido — verifique o campo");
    // Host derivado da origem (M6): a página vale por IP da LAN e por
    // `vsl.local` (que está no SAN); IP fixo no HTML quebraria o segundo caso.
    var host = window.location.hostname;
    setText("rtsp-hint", "rtsp://" + host + ":8554/" + currentPath());
    // Porta derivada da origem (8444 no host desde 2026-11-15, `CAPTURE_PORT`).
    setText("origin-line", window.location.origin + " · whip em :8889");
    var pathInput = $("path");
    if (pathInput) {
      pathInput.addEventListener("input", function () {
        setText("r-whip", whipUrl() || "path inválido — verifique o campo");
        setText("rtsp-hint", "rtsp://" + host + ":8554/" + currentPath());
      });
    }
    setText("r-secure", window.isSecureContext
      ? "sim (" + window.location.origin + ")"
      : "NÃO (" + window.location.origin + ")");
    if (!window.isSecureContext) {
      showError("Esta página não está em contexto seguro. O Chrome não libera a câmera " +
        "em HTTP: abra https://" + window.location.hostname + ":8444/capture.html " +
        "com a CA instalada no aparelho.");
    }
    setText("r-req", formatRes(TARGET_WIDTH, TARGET_HEIGHT) + " · " +
      formatFps(TARGET_FPS) + " · paisagem 16:9 (ideal, com teto)");

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
    version: CAPTURE_VERSION,
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
