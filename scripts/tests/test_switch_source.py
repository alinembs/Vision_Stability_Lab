"""Testes das funções puras do `scripts/switch_source.sh` (E2-T3).

O script tem duas metades: o `.sh` (docker, compose, sonda RTSP) e
`scripts/switch_source_lib.py` (decisões a partir de texto). Aqui se testa **só
a metade pura** — nenhum teste chama `docker`, abre socket ou toca em
GStreamer; é o que permite validar a troca de fonte sem GPU nem rede.

O que estes testes protegem, em ordem:

1. **id inválido nunca vira troca**: erro com a lista de ids válidos, e não
   fallback para `default_source` (a troca pareceria feita);
2. **guarda de precedência**: `ACTIVE_SOURCE` no ambiente do container vence o
   arquivo — sem bloquear, o pós-check aprovaria a fonte errada;
3. **URI do pré-check no host**: `mediamtx` é DNS do compose e não resolve no
   host; a URI do precheck troca o host sem perder porta/path;
4. **espera sobre JSONL**: só PASS com `source_id` pedido **e** `fps_in > 0`, e
   só com registros escritos **depois** do offset (senão a métrica da fonte
   anterior aprovaria uma troca que não pegou);
5. **decisão de rollback**: sem confirmação volta ao valor anterior (ou apaga o
   arquivo, quando não havia controle);
6. **argumentos**: default de 90 s, `--skip-precheck`, `--help`, id com
   alfabeto inválido.

Nota: `scripts/tests` não tem `__init__.py` de propósito (ver
`test_probe_logic.py`); o import abaixo usa o caminho do diretório.
"""

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import switch_source_lib as lib  # noqa: E402

RAIZ = Path(__file__).resolve().parents[2]
URI_DRONE_MEDIA = "rtsp://mediamtx:8554/drone"
URI_DRONE_HOST = "rtsp://192.168.59.102:8554/drone"


def _iso(epoch: float) -> str:
    """Timestamp UTC no formato que `pipeline_real.py` grava na telemetria."""
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat(timespec="milliseconds")

# Credencial de mentira montada em runtime: a guarda da DIV-001
# (`deepstream/tests/test_no_hardcoded_secrets.py`) varre os arquivos
# rastreados atrás de `usuario:senha@` e um literal aqui seria um falso
# positivo — e um dia, um positivo de verdade.
USUARIO_FALSO = "admin"
SENHA_FALSA = "senhaFalsa123"


def _com_credencial(uri: str) -> str:
    esquema, resto = uri.split("://", 1)
    return f"{esquema}://{USUARIO_FALSO}:{SENHA_FALSA}@{resto}"


URI_CAMERA = "rtsp://192.168.59.108:5543/live/channel0"


class FakeClock:
    """Relógio controlado pelo teste: a espera não dorme de verdade."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class JsonlFixture:
    """`data/logs/live.jsonl` de mentira, append-only como o de verdade."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "live.jsonl"
        self.path.write_text("", encoding="utf-8")

    def append(self, **record) -> "JsonlFixture":
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self

    def metric(self, source_id: str, fps_in: float, **extra) -> "JsonlFixture":
        return self.append(event="interval_metrics", source_id=source_id,
                           fps_in=fps_in, **extra)

    def size(self) -> int:
        return self.path.stat().st_size


class BaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock()
        self.jsonl = JsonlFixture(self.dir)


# ---------------------------------------------------------------------------
# Argumentos
# ---------------------------------------------------------------------------
class ArgumentosTest(unittest.TestCase):
    def test_default_de_timeout_e_90s(self) -> None:
        args = lib.parse_args(["camera"])
        self.assertEqual(args.source_id, "camera")
        self.assertEqual(args.timeout_s, 90.0)
        self.assertFalse(args.skip_precheck)
        self.assertFalse(args.help_requested)

    def test_flags(self) -> None:
        args = lib.parse_args(["drone", "--skip-precheck", "--timeout", "120"])
        self.assertEqual(args.source_id, "drone")
        self.assertTrue(args.skip_precheck)
        self.assertEqual(args.timeout_s, 120.0)

    def test_help_curto_e_longo(self) -> None:
        for flag in ("--help", "-h"):
            self.assertTrue(lib.parse_args([flag]).help_requested)
        # `--help` vence o id ausente: é ajuda, não erro de uso.
        self.assertTrue(lib.parse_args(["--help", "--timeout", "0"]).help_requested)

    def test_sem_id_e_uso_incorreto(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            lib.parse_args([])
        self.assertEqual(ctx.exception.code, 2)

    def test_id_fora_do_alfabeto_e_uso_incorreto(self) -> None:
        for invalido in ("Camera", "drone 1", "drone/1", "../drone"):
            with self.assertRaises(SystemExit) as ctx:
                lib.parse_args([invalido])
            self.assertEqual(ctx.exception.code, 2)

    def test_timeout_nao_positivo_e_uso_incorreto(self) -> None:
        for valor in ("0", "-5"):
            with self.assertRaises(SystemExit) as ctx:
                lib.parse_args(["camera", "--timeout", valor])
            self.assertEqual(ctx.exception.code, 2)


# ---------------------------------------------------------------------------
# Validação do id contra o sources.yaml
# ---------------------------------------------------------------------------
YAML_FONTES = """
version: 1
default_source: camera
sources:
  - id: camera
    name: "câmera fixa"
    uri: "rtsp://192.168.59.108:5543/live/channel0"
    codec: h265
  - id: drone
    name: "drone"
    uri: "rtsp://${MTX_HOST:-mediamtx}:8554/drone"
    codec: h264
  - id: phone
    name: "celular"
    uri: "rtsp://${MTX_HOST:-mediamtx}:8554/phone"
    codec: h264
    enabled: false
"""


class ValidacaoDoIdTest(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.dir / "sources.yaml"
        self.config.write_text(YAML_FONTES, encoding="utf-8")

    def resolver(self, source_id: str, **env) -> object:
        return lib.resolve_source(source_id, env, str(self.config))

    def test_fonte_valida(self) -> None:
        fonte = self.resolver("drone")
        self.assertEqual(fonte.id, "drone")
        self.assertEqual(fonte.codec, "h264")

    def test_id_inexistente_lista_os_validos(self) -> None:
        with self.assertRaises(lib.sources.SourceConfigError) as ctx:
            self.resolver("tablet")
        mensagem = str(ctx.exception)
        self.assertIn("tablet", mensagem)
        self.assertIn("camera, drone, phone", mensagem)

    def test_fonte_desabilitada_e_recusada(self) -> None:
        with self.assertRaises(lib.sources.SourceConfigError) as ctx:
            self.resolver("phone")
        self.assertIn("enabled: false", str(ctx.exception))

    def test_sources_yaml_do_repositorio_tem_as_tres_fontes(self) -> None:
        for source_id in ("camera", "drone", "phone"):
            fonte = lib.resolve_source(
                source_id, {"RTSP_URI": "rtsp://192.168.59.108:5543/x"},
                str(RAIZ / "deepstream" / "config" / "sources.yaml"))
            self.assertEqual(fonte.id, source_id)


# ---------------------------------------------------------------------------
# URI do pré-check (host)
# ---------------------------------------------------------------------------
class PrecheckUrlTest(unittest.TestCase):
    def test_host_do_compose_vira_lan_ip(self) -> None:
        self.assertEqual(lib.precheck_url(URI_DRONE_MEDIA, {}), URI_DRONE_HOST)
        self.assertEqual(
            lib.precheck_url(URI_DRONE_MEDIA, {"LAN_IP": "10.0.0.7"}),
            "rtsp://10.0.0.7:8554/drone")

    def test_mtx_host_tem_precedencia_sobre_lan_ip(self) -> None:
        self.assertEqual(
            lib.precheck_url(URI_DRONE_MEDIA,
                             {"MTX_HOST": "127.0.0.1", "LAN_IP": "10.0.0.7"}),
            "rtsp://127.0.0.1:8554/drone")

    def test_uri_fora_do_compose_fica_intacta(self) -> None:
        """A câmera já tem host alcançável: a URI (e a credencial) não muda."""
        camera = _com_credencial(URI_CAMERA)
        self.assertEqual(lib.precheck_url(camera, {}), camera)
        fonte = lib.sources.SourceConfig(id="camera", name="câmera",
                                         uri=camera, codec="h265")
        relatorio = lib.source_report(fonte, {})
        self.assertEqual(relatorio["precheck_uri"], camera)
        self.assertNotIn(SENHA_FALSA, relatorio["masked_uri"])
        self.assertIn("***@192.168.59.108:5543", relatorio["masked_uri"])

    def test_porta_e_path_preservados(self) -> None:
        uri = lib.precheck_url("rtsps://mediamtx:8322/phone?x=1", {})
        self.assertEqual(uri, "rtsps://192.168.59.102:8322/phone?x=1")

    def test_uri_logada_e_a_que_a_sonda_abriu(self) -> None:
        """F5: o log citava `mediamtx` e a sonda abria o host da LAN.

        Diagnóstico com o endereço errado manda o operador olhar para um host
        que a sonda nunca tocou. A URI do log é a sondada, mascarada.
        """
        fonte = lib.resolve_source(
            "drone", {"MTX_HOST": "10.0.0.7", "RTSP_URI": URI_CAMERA},
            str(RAIZ / "deepstream" / "config" / "sources.yaml"))
        relatorio = lib.source_report(fonte, {"MTX_HOST": "10.0.0.7"})
        self.assertEqual(relatorio["precheck_masked_uri"], relatorio["precheck_uri"])
        self.assertIn("10.0.0.7", relatorio["precheck_masked_uri"])
        self.assertNotIn("mediamtx", relatorio["precheck_masked_uri"])

    def test_credencial_nao_vaza_na_uri_do_precheck_logada(self) -> None:
        """O host do drone é alcançável e tem credencial: só `***@` vai ao log."""
        camera = _com_credencial(URI_CAMERA)
        fonte = lib.sources.SourceConfig(id="camera", name="câmera",
                                         uri=camera, codec="h265")
        relatorio = lib.source_report(fonte, {})
        self.assertNotIn(SENHA_FALSA, relatorio["precheck_masked_uri"])
        self.assertIn("***@192.168.59.108:5543", relatorio["precheck_masked_uri"])


# ---------------------------------------------------------------------------
# deploy/.env
# ---------------------------------------------------------------------------
class EnvFileTest(BaseTest):
    def test_formato_do_compose(self) -> None:
        camera = _com_credencial(URI_CAMERA)
        env = lib.parse_env_file(
            "# comentário\n"
            f"RTSP_URI={camera}\n"
            'MTX_HOST="192.168.59.102"\r\n'
            "export LAN_IP='10.0.0.7'\n"
            "\n"
            "linha sem igual\n"
            "ACTIVE_SOURCE=\n"
        )
        self.assertEqual(env["RTSP_URI"], camera)
        self.assertEqual(env["MTX_HOST"], "192.168.59.102")
        self.assertEqual(env["LAN_IP"], "10.0.0.7")
        self.assertEqual(env["ACTIVE_SOURCE"], "")
        self.assertNotIn("linha sem igual", env)

    def test_arquivo_ausente_e_dicionario_vazio(self) -> None:
        self.assertEqual(lib.read_env_file(self.dir / "nao_existe"), {})

    def test_ambiente_do_shell_vence_o_env_file(self) -> None:
        arquivo = self.dir / ".env"
        arquivo.write_text("MTX_HOST=mediamtx\nLAN_IP=10.0.0.7\n",
                           encoding="utf-8")
        merged = lib.merged_env(arquivo, {"MTX_HOST": "127.0.0.1"})
        self.assertEqual(merged["MTX_HOST"], "127.0.0.1")
        self.assertEqual(merged["LAN_IP"], "10.0.0.7")


# ---------------------------------------------------------------------------
# Guarda de precedência
# ---------------------------------------------------------------------------
class GuardaDePrecedenciaTest(unittest.TestCase):
    def test_sem_active_source_pode_seguir(self) -> None:
        self.assertIsNone(lib.active_source_conflict("", ""))
        self.assertIsNone(lib.active_source_conflict("  ", "\n"))

    def test_container_preenchido_bloqueia(self) -> None:
        mensagem = lib.active_source_conflict("drone", "")
        assert mensagem is not None
        self.assertIn("ACTIVE_SOURCE=drone", mensagem)
        self.assertIn("deploy/.env", mensagem)

    def test_env_file_preenchido_bloqueia(self) -> None:
        mensagem = lib.active_source_conflict("", "phone")
        assert mensagem is not None
        self.assertIn("phone", mensagem)


# ---------------------------------------------------------------------------
# Espera do pós-check sobre o JSONL
# ---------------------------------------------------------------------------
class EsperaDaTelemetriaTest(BaseTest):
    def esperar(self, source_id: str, timeout_s: float = 90.0,
                offset: int = 0) -> lib.WaitOutcome:
        return lib.wait_for_source(
            self.jsonl.path, source_id, timeout_s, offset=offset,
            poll_s=1.0, since=self.clock.now(), now=self.clock.now,
            sleep=self.clock.sleep)

    def test_metricas_da_fonte_com_frames_e_pass(self) -> None:
        self.jsonl.append(event="source_active", source_id="drone")
        self.jsonl.metric("drone", 24.5)
        outcome = self.esperar("drone")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.source_id, "drone")
        self.assertEqual(outcome.fps_in, 24.5)
        self.assertEqual(outcome.reason, "ok")
        self.assertIsNotNone(outcome.first_frame_in)

    def test_metricas_da_fonte_errada_nao_aprovam(self) -> None:
        self.jsonl.metric("camera", 19.9)
        outcome = self.esperar("drone", timeout_s=3.0)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.source_id, "camera")
        self.assertIn("source_id_inesperado", outcome.reason)
        self.assertEqual(self.clock.value - 1_700_000_000.0, 3.0)

    def test_fps_zero_nao_conta_como_frame(self) -> None:
        """`source_id` certo e `fps_in = 0` = fonte muda (o caso do rollback)."""
        self.jsonl.metric("drone", 0)
        outcome = self.esperar("drone", timeout_s=2.0)
        self.assertFalse(outcome.ok)
        self.assertIn("sem_frames", outcome.reason)

    def test_registro_anterior_ao_offset_nao_aprova(self) -> None:
        """A métrica da fonte velha, escrita antes da recriação, não vale."""
        self.jsonl.metric("drone", 30.0)
        offset = self.jsonl.size()
        self.jsonl.metric("camera", 19.9)
        outcome = self.esperar("drone", timeout_s=2.0, offset=offset)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.source_id, "camera")

    def test_arquivo_ausente_espera_e_falha_com_motivo(self) -> None:
        outcome = lib.wait_for_source(
            self.dir / "nao_existe.jsonl", "drone", 2.0,
            since=self.clock.now(), now=self.clock.now,
            sleep=self.clock.sleep)
        self.assertFalse(outcome.ok)
        self.assertIn("sem_metricas", outcome.reason)

    def test_startup_failed_entra_no_diagnostico(self) -> None:
        self.jsonl.append(event="startup_failed",
                          error="fonte ativa 'drone' não existe")
        outcome = self.esperar("drone", timeout_s=2.0)
        self.assertFalse(outcome.ok)
        self.assertIn("startup_failed", outcome.last_error or "")
        self.assertIn("startup_sem_metricas", outcome.reason)

    def test_linha_parcial_nao_e_consumida(self) -> None:
        """O pipeline escrevendo no meio de uma linha não pode quebrar a espera."""
        registros, offset = lib.read_new_records(self.jsonl.path, 0)
        self.assertEqual((registros, offset), ([], 0))
        with open(self.jsonl.path, "a", encoding="utf-8") as handle:
            handle.write('{"event": "interval_metrics", "source_id": "dro')
        registros, offset = lib.read_new_records(self.jsonl.path, 0)
        self.assertEqual((registros, offset), ([], 0))
        with open(self.jsonl.path, "a", encoding="utf-8") as handle:
            handle.write('ne", "fps_in": 24.0}\n')
        registros, offset = lib.read_new_records(self.jsonl.path, 0)
        self.assertEqual(len(registros), 1)
        self.assertEqual(registros[0]["source_id"], "drone")
        self.assertEqual(offset, self.jsonl.size())

    def test_jsonl_com_linha_corrompida_nao_derruba_a_espera(self) -> None:
        with open(self.jsonl.path, "a", encoding="utf-8") as handle:
            handle.write("isto não é json\n")
        self.jsonl.metric("drone", 12.0)
        registros, _offset = lib.read_new_records(self.jsonl.path, 0)
        self.assertEqual(len(registros), 1)
        self.assertTrue(self.esperar("drone").ok)


# ---------------------------------------------------------------------------
# Prova de qual processo escreveu a métrica (F1)
# ---------------------------------------------------------------------------
class ParseIsoEpochTest(unittest.TestCase):
    def test_formato_da_telemetria(self) -> None:
        """`ts`/`session_epoch` do pipeline: UTC com `+00:00` e ms."""
        epoch = lib.parse_iso_epoch("2026-10-30T21:07:23.703+00:00")
        self.assertAlmostEqual(epoch, 1793394443.703, places=3)

    def test_started_at_do_docker_com_nanossegundos(self) -> None:
        epoch = lib.parse_iso_epoch("2026-10-30T21:07:20.664208041Z")
        self.assertAlmostEqual(epoch, 1793394440.664208, places=3)

    def test_offset_numerico_e_respeitado(self) -> None:
        """Sem tratar o offset, um fuso `-03:00` deslocaria a comparação em 3 h."""
        self.assertAlmostEqual(
            lib.parse_iso_epoch("2026-10-30T18:07:23.703-03:00"),
            lib.parse_iso_epoch("2026-10-30T21:07:23.703+00:00"), places=3)

    def test_lixo_devolve_none(self) -> None:
        for invalido in (None, "", "nada", "2026-13-45T99:99:99Z", 1234):
            self.assertIsNone(lib.parse_iso_epoch(invalido))


class ProcessoNovoTest(unittest.TestCase):
    """`session_epoch` >= `StartedAt` é o que prova o processo novo."""

    STARTED_AT = "2026-10-30T20:54:25.890000000Z"
    EPOCH_ANTIGO = "2026-10-30T20:49:16.000+00:00"   # processo que já morreu
    EPOCH_NOVO = "2026-10-30T20:54:29.786+00:00"

    def test_boundary_prefere_o_started_at_do_docker(self) -> None:
        boundary = lib.process_boundary(self.STARTED_AT, 1_700_000_000.0)
        self.assertAlmostEqual(boundary,
                               lib.parse_iso_epoch(self.STARTED_AT), places=3)

    def test_sem_started_at_usa_o_epoch_da_recriacao(self) -> None:
        self.assertEqual(lib.process_boundary("nada", 1_700_000_000.0),
                         1_700_000_000.0)
        self.assertIsNone(lib.process_boundary(None, None))

    def test_amostra_do_processo_antigo_e_recusada(self) -> None:
        """O caso medido: troca câmera -> câmera, `source_id` igual."""
        boundary = lib.process_boundary(self.STARTED_AT)
        self.assertFalse(lib.sample_is_from_process(
            {"source_id": "camera", "session_epoch": self.EPOCH_ANTIGO},
            boundary))

    def test_amostra_do_processo_novo_e_aceita(self) -> None:
        boundary = lib.process_boundary(self.STARTED_AT)
        self.assertTrue(lib.sample_is_from_process(
            {"session_epoch": self.EPOCH_NOVO}, boundary))

    def test_tolerancia_cobre_o_jitter_entre_docker_e_pipeline(self) -> None:
        boundary = lib.process_boundary(self.STARTED_AT)
        # `session_epoch` é o instante do import: sempre DEPOIS do StartedAt.
        self.assertGreater(lib.parse_iso_epoch(self.EPOCH_NOVO), boundary)
        # 1 s antes do StartedAt (relógio do Docker x do processo)
        self.assertTrue(lib.sample_is_from_process(
            {"session_epoch": _iso(boundary - 1.0)}, boundary))
        # fora da tolerância: é de outro processo
        self.assertFalse(lib.sample_is_from_process(
            {"session_epoch": _iso(boundary - 5.0)}, boundary))

    def test_sem_session_epoch_o_ts_da_amostra_serve(self) -> None:
        """JSONL antigo, sem `session_epoch`: o `ts` ainda separa os processos."""
        boundary = lib.process_boundary(self.STARTED_AT)
        self.assertTrue(lib.sample_is_from_process(
            {"ts": self.EPOCH_NOVO}, boundary))
        self.assertFalse(lib.sample_is_from_process(
            {"ts": self.EPOCH_ANTIGO}, boundary))

    def test_sem_nenhum_timestamp_e_recusada(self) -> None:
        """Sem prova, não aprova: silêncio aqui é "PASS na fonte errada"."""
        boundary = lib.process_boundary(self.STARTED_AT)
        self.assertFalse(lib.sample_is_from_process({"source_id": "camera"},
                                                   boundary))

    def test_sem_boundary_mantem_o_comportamento_antigo(self) -> None:
        self.assertTrue(lib.sample_is_from_process(
            {"session_epoch": self.EPOCH_ANTIGO}, None))


class EsperaDoProcessoNovoTest(BaseTest):
    """A espera do pós-check amarrada ao processo, não só ao `source_id`."""

    def esperar_novo(self, source_id: str, timeout_s: float = 5.0,
                     started_at: str | None = None, since: float | None = None,
                     sleep=None) -> lib.WaitOutcome:
        return lib.wait_for_source(
            self.jsonl.path, source_id, timeout_s, poll_s=1.0,
            since=since, started_at=started_at,
            now=self.clock.now, sleep=sleep or self.clock.sleep)

    def test_troca_para_a_mesma_fonte_nao_passa_com_a_metrica_antiga(self) -> None:
        """F1 medido em bancada: `camera -> camera` PASSava às 18,6 s.

        A métrica usada era do processo anterior (`session_epoch` 20:49:16,
        `StartedAt` do container novo 20:54:25). Agora ela é recusada e o
        motivo aparece na saída.
        """
        self.jsonl.metric("camera", 19.9, session_epoch=_iso(
            self.clock.now() - 120))
        outcome = self.esperar_novo("camera", timeout_s=3.0,
                                    started_at=_iso(self.clock.now() - 60))
        self.assertFalse(outcome.ok)
        self.assertIn("sem_amostra_do_processo_novo", outcome.reason)
        self.assertIn(_iso(self.clock.now() - 120), outcome.reason)

    def test_metrica_do_processo_novo_aprova_e_reporta_o_epoch(self) -> None:
        epoch = _iso(self.clock.now() + 3)
        self.jsonl.metric("camera", 19.9, session_epoch=epoch)
        outcome = self.esperar_novo("camera", started_at=_iso(
            self.clock.now() - 20))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.session_epoch, epoch)
        self.assertEqual(outcome.as_lines()["session_epoch"], epoch)

    def test_orcamento_conta_da_recriacao_e_nao_do_inicio_do_script(self) -> None:
        """F2 medido em bancada: `--timeout 40` reprovava troca que funcionou.

        Custo fixo real: pré-check 5 s + parada/recriação ~10 s + 30 s do
        `METRICS_INTERVAL_S`. Contando do início do script, os 40 s acabavam
        antes de existir a primeira métrica do processo novo.
        """
        inicio_do_script = self.clock.now()
        recriacao = inicio_do_script + 60.0     # pré-check + stop/recreate

        # Comportamento antigo (orçamento do início do script): reprova.
        self.clock.value = recriacao
        antigo = self.esperar_novo("drone", timeout_s=40.0,
                                   since=inicio_do_script)
        self.assertFalse(antigo.ok)
        self.assertIn("sem_metricas", antigo.reason)

        # Com o boundary da recriação: a métrica chega 20 s depois e passa.
        escritos = {"feito": False}

        def sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            if not escritos["feito"]:
                escritos["feito"] = True
                self.clock.sleep(19.0)
                self.jsonl.metric("drone", 24.0,
                                  session_epoch=_iso(recriacao + 3))

        self.clock.value = recriacao
        novo = self.esperar_novo("drone", timeout_s=40.0,
                                 started_at=_iso(recriacao),
                                 since=inicio_do_script, sleep=sleep)
        self.assertTrue(novo.ok)
        self.assertAlmostEqual(novo.first_frame_in, 20.0, delta=1.5)


class ReadNewRecordsTest(BaseTest):
    def test_offset_maior_que_o_arquivo_recomeca(self) -> None:
        """Log rotacionado entre a medição e a leitura não pode travar a espera."""
        self.jsonl.metric("drone", 21.0)
        registros, offset = lib.read_new_records(self.jsonl.path, 10_000)
        self.assertEqual(len(registros), 1)
        self.assertEqual(offset, self.jsonl.size())

    def test_leitura_incremental_nao_repete_registro(self) -> None:
        self.jsonl.metric("camera", 19.9)
        registros, offset = lib.read_new_records(self.jsonl.path, 0)
        self.assertEqual(len(registros), 1)
        self.jsonl.metric("drone", 25.0)
        registros, offset = lib.read_new_records(self.jsonl.path, offset)
        self.assertEqual([r["source_id"] for r in registros], ["drone"])


# ---------------------------------------------------------------------------
# Pré-check reprovado
# ---------------------------------------------------------------------------
class MotivoDoPrecheckTest(unittest.TestCase):
    def test_path_mudo_vira_no_frames_on_path(self) -> None:
        for bruto in ("no_frames", "no_video_pad", "first_frame_too_late",
                      "pipeline_error: could not resolve mediamtx"):
            self.assertEqual(
                lib.probe_failure_reason({"result": "FAIL", "reason": bruto}),
                "no_frames_on_path")

    def test_instabilidade_tem_motivo_proprio(self) -> None:
        for bruto in ("too_many_gaps (3 > 0)", "fps_below_min (2.1 < 5.0)"):
            self.assertEqual(
                lib.probe_failure_reason({"result": "FAIL", "reason": bruto}),
                "unstable_path")

    def test_relatorio_vazio_nao_quebra(self) -> None:
        self.assertEqual(lib.probe_failure_reason({}), "probe_failed")


# ---------------------------------------------------------------------------
# Decisão de rollback
# ---------------------------------------------------------------------------
class DecisaoDeRollbackTest(unittest.TestCase):
    def outcome(self, ok: bool, source_id: str | None,
                reason: str = "ok") -> lib.WaitOutcome:
        return lib.WaitOutcome(ok=ok, elapsed_s=12.3,
                               first_frame_in=12.3 if ok else None,
                               source_id=source_id, fps_in=24.0 if ok else None,
                               last_error=None, reason=reason)

    def test_pos_check_confirmado_mantem_a_troca(self) -> None:
        decisao = lib.decide_rollback(self.outcome(True, "drone"), "drone",
                                      "camera")
        self.assertEqual(decisao.action, "keep")
        self.assertIsNone(decisao.restore_to)

    def test_source_id_errado_dispara_rollback(self) -> None:
        decisao = lib.decide_rollback(
            self.outcome(True, "camera", "ok"), "drone", "camera")
        self.assertEqual(decisao.action, "rollback")
        self.assertEqual(decisao.restore_to, "camera")

    def test_sem_arquivo_anterior_restaura_para_vazio(self) -> None:
        decisao = lib.decide_rollback(
            self.outcome(False, None, "sem_metricas em 90s"), "drone", "")
        self.assertEqual(decisao.action, "rollback")
        self.assertEqual(decisao.restore_to, "")
        self.assertIn("sem_metricas", decisao.reason)


# ---------------------------------------------------------------------------
# Registro de switch.jsonl
# ---------------------------------------------------------------------------
class RegistroDaTrocaTest(unittest.TestCase):
    def test_linha_minima(self) -> None:
        record = lib.switch_record(source_id="drone", previous="camera",
                                   result="PASS", reason="ok", probe="ok",
                                   timestamp="2026-10-30T20:00:00.000+00:00")
        self.assertEqual(record["event"], "switch_source")
        self.assertEqual(record["from"], "camera")
        self.assertEqual(record["to"], "drone")
        self.assertEqual(record["probe"], "ok")
        self.assertEqual(record["result"], "PASS")
        json.dumps(record)  # tem de ser serializável como está

    def test_precheck_pulado_fica_registrado(self) -> None:
        record = lib.switch_record(source_id="drone", previous="",
                                   result="PASS", reason="ok", probe="skipped")
        self.assertEqual(record["probe"], "skipped")

    def test_tentativa_bloqueada_tem_motivo_e_probe_proprio(self) -> None:
        """F4: o guard de precedência abortava sem deixar auditoria."""
        record = lib.switch_record(
            source_id="camera", previous="drone", result="FAIL",
            reason="active_source_no_ambiente", probe="blocked")
        self.assertEqual(record["result"], "FAIL")
        self.assertEqual(record["reason"], "active_source_no_ambiente")
        self.assertEqual(record["from"], "drone")
        self.assertEqual(record["to"], "camera")
        self.assertEqual(record["probe"], "blocked")
        json.dumps(record)


# ---------------------------------------------------------------------------
# Contrato do script (texto, sem executar)
# ---------------------------------------------------------------------------
class ContratoDoScriptTest(unittest.TestCase):
    RAIZ = RAIZ
    SCRIPT = RAIZ / "scripts" / "switch_source.sh"

    def setUp(self) -> None:
        self.codigo = self.SCRIPT.read_text(encoding="utf-8")

    def test_script_e_executavel_e_sintaticamente_valido(self) -> None:
        self.assertTrue(self.SCRIPT.is_file())
        self.assertTrue(self.SCRIPT.stat().st_mode & 0o111,
                        "switch_source.sh precisa ser executável")

    def test_modo_estrito(self) -> None:
        self.assertIn("set -euo pipefail", self.codigo)

    def test_ordem_probe_antes_da_escrita(self) -> None:
        """D7: o pré-check roda **antes** de tocar no arquivo de controle."""
        self.assertLess(self.codigo.index("probe/rtsp_read_probe.py"),
                        self.codigo.index("write_control \"$SOURCE_ID\""))

    def test_recria_o_container_unico_do_projeto(self) -> None:
        self.assertIn("up -d --no-deps", self.codigo)
        self.assertIn("vision-app", self.codigo)

    def test_recriacao_e_forcada(self) -> None:
        """`up -d` puro responde "Container ... Running" e não relê a fonte.

        O arquivo de controle chega ao container por **volume**, não por
        configuração: sem `--force-recreate` o pipeline continua com a fonte
        antiga e o pós-check daria FAIL por um motivo errado.
        """
        self.assertIn("--force-recreate", self.codigo)

    def test_guarda_de_precedencia_sai_3(self) -> None:
        self.assertIn("exit 3", self.codigo)
        self.assertIn("printenv ACTIVE_SOURCE", self.codigo)

    def test_nenhuma_uri_crua_na_saida(self) -> None:
        """Só `masked_uri`/`precheck_uri` circulam; nada de `echo $uri`."""
        for proibido in ("echo \"$RTSP_URI", 'echo "$SOURCE_URI',
                         "printf '%s' \"$RTSP_URI"):
            self.assertNotIn(proibido, self.codigo)

    # -- F1/F2: prova do processo novo e orçamento --------------------------
    def test_pos_check_amarra_a_amostra_ao_processo_novo(self) -> None:
        """F1: sem o boundary da recriação a espera volta a aceitar a métrica velha."""
        self.assertIn("--started-at \"$STARTED_AT\"", self.codigo)
        self.assertIn("--recreated-at \"$RECREATED_AT\"", self.codigo)
        self.assertIn("State.StartedAt", self.codigo)
        # O boundary é lido DEPOIS da recriação (antes dela não existe).
        self.assertLess(self.codigo.index("recreate_app\nRECREATED_AT"),
                        self.codigo.index("--started-at \"$STARTED_AT\""))

    def test_saida_pass_mostra_o_session_epoch_usado(self) -> None:
        self.assertIn("session_epoch=$session_epoch", self.codigo)
        self.assertIn("session_epoch=\"$(kv \"$wait_out\" session_epoch)\"",
                      self.codigo)

    def test_orcamento_do_timeout_nao_conta_o_inicio_do_script(self) -> None:
        """F2: `--since $SWITCH_START` era o bug — o custo fixo comia o orçamento."""
        self.assertNotIn("--since \"$SWITCH_START\"", self.codigo)
        self.assertNotIn("SWITCH_START", self.codigo)

    # -- F4/F5 --------------------------------------------------------------
    def test_tentativa_bloqueada_fica_no_switch_jsonl(self) -> None:
        """F4: uma linha por execução, inclusive a recusada pelo guard."""
        guarda = self.codigo.index('guard_out=$("$PYTHON" "$LIB" guard')
        registro = self.codigo.index(
            'record_switch "FAIL" "active_source_no_ambiente"')
        saida = self.codigo.index("die 3", guarda)
        self.assertLess(guarda, registro)
        self.assertLess(registro, saida,
                        "o registro precisa acontecer antes do exit 3")

    def test_valor_anterior_e_lido_antes_do_guarda(self) -> None:
        self.assertLess(self.codigo.index('PREVIOUS_VALUE="$(cat -- "$CONTROL_FILE")"'),
                        self.codigo.index('guard_out=$("$PYTHON" "$LIB" guard'))

    def test_precheck_loga_a_uri_sondada_e_nao_a_do_yaml(self) -> None:
        """F5: `$MASKED_URI` é a do YAML (`mediamtx`), não a que a sonda abriu."""
        linha = [l for l in self.codigo.splitlines()
                 if l.strip().startswith("log \"pré-check: lendo")]
        self.assertEqual(len(linha), 1, "o log do pré-check mudou de forma")
        self.assertIn("$PRECHECK_MASKED_URI", linha[0])
        self.assertNotIn("$MASKED_URI", linha[0])
        self.assertIn('--url "$PRECHECK_URI"', self.codigo)



if __name__ == "__main__":
    unittest.main()
