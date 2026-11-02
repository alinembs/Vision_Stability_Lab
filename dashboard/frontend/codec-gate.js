/*
 * Gate de codec do E0-T4: **H.264 ou não publica**.
 *
 * Porte de `arquo:app/web/src/lib/codecGate.ts` (branch
 * `feature/SURVEY-110-m1-phone-ingest-spike`), em vanilla JS e sem build, como
 * o resto de `dashboard/frontend/`.
 *
 * O MediaMTX **não transcodifica**: se o Chrome do celular publicar em VP8/VP9,
 * o path `phone` entrega ao pipeline um codec que o caminho RTSP→DeepStream não
 * quer, e o "fallback silencioso" esconde isso até a medição. Então o gate é
 * explícito: sem H.264 na frente da m-line, não há POST.
 *
 * **A lição que este arquivo carrega (gate v1 do T0.1, ADR-0005).** A primeira
 * versão lia `sender.getParameters().codecs` e deu NO-GO falso no Chrome
 * Android: aquele campo só é populado **depois** da negociação, então vazio ali
 * significa "ainda não negociado", não "H.264 não é o primeiro". A ordem efetiva
 * se lê da **m-line do SDP do offer** — que é literalmente o que o MediaMTX
 * recebe.
 *
 * Este arquivo não toca em `navigator.mediaDevices`, `fetch` ou
 * `RTCPeerConnection`: são funções puras sobre SDP e sobre um transceiver
 * injetado, o que permite rodar os fixtures em `codec-gate-selftest.html` sem
 * câmera, sem rede e sem servidor.
 */
(function (global) {
  "use strict";

  /** Mensagens de recusa: dizem o que **fazer**, não só o que faltou. */
  var CODEC_GATE_MESSAGES = {
    api_missing:
      "Este navegador não expõe a escolha de codec. Abra esta página em HTTPS " +
      "(https://<ip-do-host>:8443) no Chrome do Android e recarregue.",
    h264_unavailable:
      "Este aparelho não oferece H.264 para publicação ao vivo, e o servidor não " +
      "converte vídeo. Use um Android com Chrome atualizado.",
    preferences_missing:
      "O navegador não permite fixar H.264 como codec da transmissão. " +
      "Atualize o Chrome do aparelho.",
    preferences_rejected:
      "O navegador recusou fixar H.264 nesta direção. O aparelho não sabe " +
      "publicar em H.264.",
  };

  /** Mensagem de recusa quando o offer saiu sem H.264 na frente. */
  var OFFER_WITHOUT_H264_MESSAGE =
    "O navegador não colocou H.264 como codec do vídeo no SDP (o servidor " +
    "receberia outro formato e não há conversão). Nada foi publicado.";

  /**
   * `true` quando o **primeiro** payload de vídeo do SDP é H.264.
   *
   * Lê a m-line (não `getParameters()`): é a ordem que o MediaMTX recebe. Só a
   * primeira m-line de vídeo conta — as demais seriam outros fluxos, que este
   * publish não usa.
   */
  function h264IsFirstInOffer(sdp) {
    if (!sdp) return false;
    var lines = String(sdp).split(/\r?\n/);
    var videoLine = null;
    for (var i = 0; i < lines.length; i += 1) {
      if (lines[i].indexOf("m=video ") === 0) {
        videoLine = lines[i];
        break;
      }
    }
    if (!videoLine) return false;
    var payloadTypes = videoLine.trim().split(/\s+/).slice(3);
    var first = payloadTypes[0];
    if (!first) return false;
    var prefix = "a=rtpmap:" + first + " ";
    var rtpmap = null;
    for (var j = 0; j < lines.length; j += 1) {
      if (lines[j].indexOf(prefix) === 0) {
        rtpmap = lines[j];
        break;
      }
    }
    if (!rtpmap) return false;
    var encoding = rtpmap.slice(prefix.length).split("/")[0].trim().toUpperCase();
    return encoding === "H264";
  }

  /** Capacidades de vídeo do browser, ou `undefined` quando a API não existe. */
  function defaultGetCapabilities() {
    if (typeof RTCRtpSender === "undefined" ||
        typeof RTCRtpSender.getCapabilities !== "function") {
      return undefined;
    }
    var capabilities = RTCRtpSender.getCapabilities("video");
    return capabilities && capabilities.codecs ? capabilities.codecs : undefined;
  }

  /** Preferência entre perfis H.264: o que o MediaMTX espera vem primeiro. */
  function h264PreferenceScore(codec) {
    var fmtp = (codec && codec.sdpFmtpLine) || "";
    var score = 0;
    // `packetization-mode=1` é o que o caminho WHIP → MediaMTX → RTSP espera.
    if (/packetization-mode=1/.test(fmtp)) score -= 2;
    // `42001f` (constrained baseline L3.1) foi o perfil medido no Chrome Android.
    if (/profile-level-id=42001f/i.test(fmtp)) score -= 1;
    return score;
  }

  /**
   * Fixa H.264 no transceiver — o **único** `setCodecPreferences` da página.
   *
   * Devolve `{ok: false}` quando não há H.264: quem chama **não publica**.
   */
  function pickH264(transceiver, getCapabilities) {
    var capabilities = (getCapabilities || defaultGetCapabilities)();
    if (!capabilities) {
      return { ok: false, reason: "api_missing", message: CODEC_GATE_MESSAGES.api_missing };
    }
    var h264 = capabilities
      .filter(function (codec) {
        return codec && typeof codec.mimeType === "string" &&
          codec.mimeType.toLowerCase() === "video/h264";
      })
      .sort(function (left, right) {
        return h264PreferenceScore(left) - h264PreferenceScore(right);
      });
    if (h264.length === 0) {
      return {
        ok: false,
        reason: "h264_unavailable",
        message: CODEC_GATE_MESSAGES.h264_unavailable,
      };
    }
    if (!transceiver || typeof transceiver.setCodecPreferences !== "function") {
      return {
        ok: false,
        reason: "preferences_missing",
        message: CODEC_GATE_MESSAGES.preferences_missing,
      };
    }
    try {
      transceiver.setCodecPreferences(h264);
    } catch (error) {
      // `InvalidModificationError`: o device não sabe enviar H.264 nesta direção.
      return {
        ok: false,
        reason: "preferences_rejected",
        message: CODEC_GATE_MESSAGES.preferences_rejected,
      };
    }
    return { ok: true, codecs: h264 };
  }

  /* ---------------------------------------------------------------------- *
   * Fixtures de autoteste (evidência executável, sem framework de teste).
   *
   * `kind: "offer"` roda `h264IsFirstInOffer(sdp)`; `kind: "pick"` roda
   * `pickH264(transceiver, () => capabilities)` e confere `ok` + `reason`.
   * As expectativas descrevem a regra, não o código: qualquer regressão no
   * gate vira FAIL visível em `codec-gate-selftest.html`.
   * ---------------------------------------------------------------------- */

  var SDP_H264_FIRST = [
    "v=0",
    "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
    "s=-",
    "t=0 0",
    "a=group:BUNDLE 0",
    "m=video 9 UDP/TLS/RTP/SAVPF 100 101",
    "c=IN IP4 0.0.0.0",
    "a=mid:0",
    "a=sendonly",
    "a=rtpmap:100 H264/90000",
    "a=fmtp:100 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
    "a=rtpmap:101 rtx/90000",
    "a=fmtp:101 apt=100",
    "",
  ].join("\r\n");

  var SDP_VP8_FIRST = [
    "v=0",
    "s=-",
    "t=0 0",
    "m=video 9 UDP/TLS/RTP/SAVPF 96 100",
    "a=rtpmap:96 VP8/90000",
    "a=rtpmap:100 H264/90000",
    "",
  ].join("\r\n");

  var SDP_VP9_FIRST = [
    "v=0",
    "s=-",
    "t=0 0",
    "m=video 9 UDP/TLS/RTP/SAVPF 98 100",
    "a=rtpmap:98 VP9/90000",
    "a=rtpmap:100 H264/90000",
    "",
  ].join("\r\n");

  var SDP_AUDIO_FIRST = [
    "v=0",
    "s=-",
    "t=0 0",
    "a=group:BUNDLE 0 1",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111",
    "a=mid:0",
    "a=rtpmap:111 opus/48000/2",
    "m=video 9 UDP/TLS/RTP/SAVPF 100",
    "a=mid:1",
    "a=sendonly",
    "a=rtpmap:100 H264/90000",
    "",
  ].join("\r\n");

  var SDP_H265_FIRST = [
    "v=0",
    "s=-",
    "t=0 0",
    "m=video 9 UDP/TLS/RTP/SAVPF 97 100",
    "a=rtpmap:97 H265/90000",
    "a=rtpmap:100 H264/90000",
    "",
  ].join("\r\n");

  var SDP_NO_VIDEO = [
    "v=0",
    "s=-",
    "t=0 0",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111",
    "a=rtpmap:111 opus/48000/2",
    "",
  ].join("\r\n");

  // O Chrome Android manda H.264 em minúsculas em versões antigas do SDP; o
  // gate normaliza para maiúsculas (era um falso NO-GO no spike).
  var SDP_H264_LOWERCASE = [
    "v=0",
    "s=-",
    "t=0 0",
    "m=video 9 UDP/TLS/RTP/SAVPF 100",
    "a=rtpmap:100 h264/90000",
    "",
  ].join("\r\n");

  var CAPS_H264 = [
    { mimeType: "video/VP8", clockRate: 90000 },
    {
      mimeType: "video/H264",
      clockRate: 90000,
      sdpFmtpLine: "level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=42e01f",
    },
    {
      mimeType: "video/H264",
      clockRate: 90000,
      sdpFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
    },
    { mimeType: "video/VP9", clockRate: 90000 },
  ];

  var CAPS_WITHOUT_H264 = [
    { mimeType: "video/VP8", clockRate: 90000 },
    { mimeType: "video/VP9", clockRate: 90000 },
  ];

  var FIXTURES = [
    {
      name: "H.264 primeiro no offer",
      kind: "offer",
      sdp: SDP_H264_FIRST,
      expected: true,
    },
    {
      name: "VP8 primeiro (H.264 depois) → bloqueia",
      kind: "offer",
      sdp: SDP_VP8_FIRST,
      expected: false,
    },
    {
      name: "VP9 primeiro (H.264 depois) → bloqueia",
      kind: "offer",
      sdp: SDP_VP9_FIRST,
      expected: false,
    },
    {
      name: "H.264 com áudio antes no SDP → passa",
      kind: "offer",
      sdp: SDP_AUDIO_FIRST,
      expected: true,
    },
    {
      name: "H.265 primeiro (H.264 depois) → bloqueia",
      kind: "offer",
      sdp: SDP_H265_FIRST,
      expected: false,
    },
    {
      name: "Sem m-line de vídeo → bloqueia",
      kind: "offer",
      sdp: SDP_NO_VIDEO,
      expected: false,
    },
    {
      name: "SDP vazio → bloqueia",
      kind: "offer",
      sdp: "",
      expected: false,
    },
    {
      name: "rtpmap em minúsculas (h264) → passa",
      kind: "offer",
      sdp: SDP_H264_LOWERCASE,
      expected: true,
    },
    {
      name: "pickH264 com H.264 disponível → fixa e libera",
      kind: "pick",
      capabilities: CAPS_H264,
      expectedOk: true,
      // `packetization-mode=1` tem de vir antes do 0 (o que o MediaMTX espera).
      expectedFirstFmtp: /packetization-mode=1/,
      expectedCodecCount: 2,
    },
    {
      name: "pickH264 sem H.264 (só VP8/VP9) → recusa",
      kind: "pick",
      capabilities: CAPS_WITHOUT_H264,
      expectedOk: false,
      expectedReason: "h264_unavailable",
    },
    {
      name: "pickH264 sem RTCRtpSender.getCapabilities → recusa",
      kind: "pick",
      capabilities: undefined,
      expectedOk: false,
      expectedReason: "api_missing",
    },
    {
      name: "pickH264 em transceiver sem setCodecPreferences → recusa",
      kind: "pick",
      capabilities: CAPS_H264,
      transceiver: { sender: {} },
      expectedOk: false,
      expectedReason: "preferences_missing",
    },
    {
      name: "pickH264 quando o navegador lança InvalidModificationError → recusa",
      kind: "pick",
      capabilities: CAPS_H264,
      transceiver: {
        sender: {},
        setCodecPreferences: function () {
          throw new Error("InvalidModificationError");
        },
      },
      expectedOk: false,
      expectedReason: "preferences_rejected",
    },
  ];

  /** Transceiver falso que só registra a preferência recebida. */
  function fakeTransceiver() {
    var captured = null;
    return {
      sender: {},
      setCodecPreferences: function (codecs) {
        captured = codecs;
      },
      captured: function () {
        return captured;
      },
    };
  }

  /**
   * Roda os fixtures e devolve um resultado por caso.
   *
   * Cada item: `{ name, expected, actual, pass, detail }`. É o que a página de
   * autoteste imprime como PASS/FAIL — a prova executável de que a regra do
   * gate continua valendo.
   */
  function runSelfTest(fixtures) {
    var cases = fixtures || FIXTURES;
    return cases.map(function (fixture) {
      if (fixture.kind === "offer") {
        var actual = h264IsFirstInOffer(fixture.sdp);
        return {
          name: fixture.name,
          expected: fixture.expected ? "H.264 primeiro (passa)" : "bloqueia",
          actual: actual ? "H.264 primeiro (passa)" : "bloqueia",
          pass: actual === fixture.expected,
        };
      }

      var transceiver = fixture.transceiver || fakeTransceiver();
      var result = pickH264(transceiver, function () {
        return fixture.capabilities;
      });
      var problems = [];
      if (result.ok !== fixture.expectedOk) {
        problems.push("ok=" + result.ok + " (esperado " + fixture.expectedOk + ")");
      }
      if (fixture.expectedReason && result.reason !== fixture.expectedReason) {
        problems.push("reason=" + result.reason + " (esperado " + fixture.expectedReason + ")");
      }
      if (fixture.expectedCodecCount !== undefined &&
          (!result.codecs || result.codecs.length !== fixture.expectedCodecCount)) {
        problems.push("codecs=" + (result.codecs ? result.codecs.length : "—") +
          " (esperado " + fixture.expectedCodecCount + ")");
      }
      if (fixture.expectedFirstFmtp && result.codecs) {
        if (!fixture.expectedFirstFmtp.test(result.codecs[0].sdpFmtpLine || "")) {
          problems.push("primeiro codec = " + (result.codecs[0].sdpFmtpLine || "—"));
        }
      }
      return {
        name: fixture.name,
        expected: fixture.expectedOk ? "libera (H.264)" : String(fixture.expectedReason),
        actual: result.ok ? "libera (H.264)" : String(result.reason),
        pass: problems.length === 0,
        detail: problems.join("; "),
      };
    });
  }

  global.CodecGate = {
    CODEC_GATE_MESSAGES: CODEC_GATE_MESSAGES,
    OFFER_WITHOUT_H264_MESSAGE: OFFER_WITHOUT_H264_MESSAGE,
    FIXTURES: FIXTURES,
    h264IsFirstInOffer: h264IsFirstInOffer,
    pickH264: pickH264,
    defaultGetCapabilities: defaultGetCapabilities,
    runSelfTest: runSelfTest,
  };
})(typeof window !== "undefined" ? window : this);
