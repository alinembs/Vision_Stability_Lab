"""Testes da sonda de nível 1 (`scripts/probe/rtsp_describe_probe.py`).

O servidor RTSP é falso, em `127.0.0.1` numa porta efêmera, e responde como o
MediaMTX do laboratório: 404 quando o path não tem publisher, 200 com SDP que
anuncia (ou não) `m=video`. Nada de rede externa, GStreamer ou docker.

A credencial (DIV-001) é o caso central do mascaramento: a URI da câmera tem
usuário e senha, e **nenhuma** saída da sonda pode carregá-los.
"""

import base64
import contextlib
import io
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "probe"))

import rtsp_describe_probe as probe  # noqa: E402

# Credencial fictícia montada por partes: a guarda da DIV-001
# (`deepstream/tests/test_no_hardcoded_secrets.py`) procura `usuario:senha@`
# literal em arquivo versionado.
USUARIO_FALSO = "admin"
SENHA_FALSA = "segredo"
USERINFO = USUARIO_FALSO + ":" + SENHA_FALSA

SDP_VIDEO = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=MediaMTX\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "t=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\n"
    "a=rtpmap:96 H264/90000\r\n"
)
SDP_AUDIO_ONLY = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=MediaMTX\r\n"
    "t=0 0\r\n"
    "m=audio 0 RTP/AVP 8\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
)


class FakeRtspServer:
    """Servidor RTSP mínimo, em thread, com o comportamento pedido.

    `mode`:
      - `"video"`    -> 404 no OPTIONS? não: 200 nos dois, SDP com `m=video`;
      - `"no_video"` -> 200 nos dois, SDP só com áudio;
      - `"404"`      -> OPTIONS 200, DESCRIBE 404 (path sem publisher);
      - `"silent"`   -> aceita a conexão e nunca responde (timeout).
    """

    def __init__(self, mode: str = "video") -> None:
        self.mode = mode
        self.requests: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def uri(self, path: str = "camera", userinfo: str = "") -> str:
        prefix = f"{userinfo}@" if userinfo else ""
        return f"rtsp://{prefix}127.0.0.1:{self.port}/{path}"

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    # -- servidor ----------------------------------------------------------
    def _serve(self) -> None:
        while True:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    self._handle(conn)
                except OSError:
                    continue

    def _handle(self, conn: socket.socket) -> None:
        buffer = b""
        while True:
            while b"\r\n\r\n" not in buffer:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            head, _, buffer = buffer.partition(b"\r\n\r\n")
            text = head.decode(errors="replace")
            lines = text.split("\r\n")
            method = lines[0].split()[0] if lines and lines[0] else ""
            cseq = ""
            for line in lines[1:]:
                if line.lower().startswith("cseq:"):
                    cseq = line.split(":", 1)[1].strip()
            self.requests.append(text)
            if self.mode == "silent":
                continue                     # cliente estoura o timeout
            if method == "OPTIONS":
                self._send(conn, cseq, 200, "OK", "", "")
            elif method == "DESCRIBE":
                if self.mode == "404":
                    self._send(conn, cseq, 404, "Not Found", "", "")
                elif self.mode == "no_video":
                    self._send(conn, cseq, 200, "OK", "application/sdp",
                               SDP_AUDIO_ONLY)
                else:
                    self._send(conn, cseq, 200, "OK", "application/sdp",
                               SDP_VIDEO)

    def _send(self, conn, cseq, code, phrase, content_type, body) -> None:
        headers = [f"RTSP/1.0 {code} {phrase}", f"CSeq: {cseq}"]
        if content_type:
            headers += [f"Content-Type: {content_type}",
                        f"Content-Length: {len(body.encode())}"]
        message = "\r\n".join(headers) + "\r\n\r\n" + body
        conn.sendall(message.encode())


class DescribeProbeTest(unittest.TestCase):
    def _server(self, mode: str) -> FakeRtspServer:
        server = FakeRtspServer(mode)
        self.addCleanup(server.close)
        return server

    def test_404_quando_o_path_nao_tem_publisher(self) -> None:
        server = self._server("404")

        result = probe.describe(server.uri(), timeout=1.0)

        self.assertEqual(result.status, probe.STATUS_NOT_FOUND)
        self.assertFalse(result.alive)
        self.assertEqual(result.code, 404)
        self.assertEqual(result.reason, "rtsp_404")
        # OPTIONS e DESCRIBE na mesma conexão, nesta ordem (contrato do plano).
        metodos = [text.split()[0] for text in server.requests]
        self.assertEqual(metodos, ["OPTIONS", "DESCRIBE"])

    def test_200_com_m_video_e_path_vivo(self) -> None:
        server = self._server("video")

        result = probe.describe(server.uri(), timeout=1.0)

        self.assertEqual(result.status, probe.STATUS_OK)
        self.assertTrue(result.alive)
        self.assertTrue(result.has_video)
        self.assertEqual(result.code, 200)
        self.assertIn("m=video", result.sdp)

    def test_200_sem_m_video_nao_esta_entregando_video(self) -> None:
        """Servidor responde, mas o SDP não anuncia faixa de vídeo."""
        server = self._server("no_video")

        result = probe.describe(server.uri(), timeout=1.0)

        self.assertEqual(result.status, probe.STATUS_NO_VIDEO)
        self.assertFalse(result.alive)
        self.assertFalse(result.has_video)
        self.assertEqual(result.code, 200)
        self.assertEqual(result.reason, "sdp_sem_video")

    def test_timeout_e_respeitado_no_servidor_mudo(self) -> None:
        server = self._server("silent")

        started = time.monotonic()
        result = probe.describe(server.uri(), timeout=0.3)
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, probe.STATUS_TIMEOUT)
        self.assertFalse(result.alive)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2.0, "a sonda não pode herdar timeout infinito")

    def test_uri_invalida_vira_error_sem_levantar(self) -> None:
        result = probe.describe("http://127.0.0.1/live")
        self.assertEqual(result.status, probe.STATUS_ERROR)
        self.assertFalse(result.alive)
        self.assertIn("esquema", result.reason)

    def test_porta_fechada_vira_error(self) -> None:
        """Porta sem listener: erro, nunca exceção.

        A porta vem de um socket **bound sem `listen()`**, não de um
        `FakeRtspServer` fechado: `close()` num listener com outra thread
        bloqueada no `accept()` não derruba o socket de verdade — a chamada
        pendente segura a referência do arquivo e a conexão continua sendo
        aceita, o que fazia este teste conversar com o servidor "fechado".
        `bind` sem `listen` recusa de forma determinística (ECONNREFUSED).
        """
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        self.addCleanup(holder.close)
        porta = holder.getsockname()[1]

        result = probe.describe(f"rtsp://127.0.0.1:{porta}/camera", timeout=0.5)

        self.assertIn(result.status, (probe.STATUS_ERROR, probe.STATUS_TIMEOUT))
        self.assertFalse(result.alive)
        self.assertIn(str(porta), result.uri)   # diagnóstico preservado

    def test_envia_authorization_basic_quando_ha_userinfo(self) -> None:
        server = self._server("video")
        uri = server.uri(userinfo=USERINFO)

        result = probe.describe(uri, timeout=1.0)

        self.assertTrue(result.alive)
        autorizacoes = [line for text in server.requests
                        for line in text.split("\r\n")
                        if line.lower().startswith("authorization:")]
        self.assertTrue(autorizacoes)
        token = autorizacoes[0].split(":", 1)[1].strip().split(" ", 1)[1]
        self.assertEqual(base64.b64decode(token).decode(), USERINFO)


class MaskUriTest(unittest.TestCase):
    """O userinfo da câmera nunca aparece em log/JSON (DIV-001)."""

    CAMERA = f"rtsp://{USERINFO}@cam.local:554/live"

    def test_mask_uri_esconde_usuario_e_senha(self) -> None:
        mascarada = probe.mask_uri(self.CAMERA)
        self.assertEqual(mascarada, "rtsp://***@cam.local:554/live")
        self.assertNotIn(USUARIO_FALSO, mascarada)
        self.assertNotIn(SENHA_FALSA, mascarada)

    def test_mask_uri_sem_credencial_nao_muda(self) -> None:
        self.assertEqual(probe.mask_uri("rtsp://mediamtx:8554/drone"),
                         "rtsp://mediamtx:8554/drone")

    def test_resultado_de_erro_nao_vaza_a_credencial(self) -> None:
        """O `reason` de uma URI inválida sai mascarado, não em texto claro."""
        result = probe.describe(f"rtsp://{USERINFO}@/sem-host")
        texto = json.dumps(result.as_dict(), ensure_ascii=False) + result.reason
        self.assertEqual(result.status, probe.STATUS_ERROR)
        self.assertNotIn(SENHA_FALSA, texto)
        self.assertIn("***", result.uri)

    def test_cli_imprime_json_e_mascara_a_uri(self) -> None:
        server = FakeRtspServer("404")
        self.addCleanup(server.close)
        uri = server.uri(userinfo=USERINFO)
        buffer = io.StringIO()

        with contextlib.redirect_stdout(buffer):
            codigo = probe.main(["--url", uri, "--timeout", "1", "--json"])

        self.assertEqual(codigo, 1)
        saida = buffer.getvalue()
        self.assertNotIn(SENHA_FALSA, saida)
        self.assertIn('"status": "404"', saida)


if __name__ == "__main__":
    unittest.main()
