"""Testes da sessão de fonte (`deepstream/source_state.py`, E3-T1).

Lógica pura: nenhum caso abre socket, container ou toca em `gi`/`pyds`. O que
se prova aqui são as regras que, se quebrarem em silêncio, contaminam o
isolamento entre fontes (R5) ou quebram o watchdog:

- epoch estável dentro do processo e **distinto** entre sessões (inclusive as
  criadas no mesmo milissegundo);
- `namespace()` com exatamente `source_id` + `session_epoch`;
- `reset()` zera contadores de sessão sem tocar no histórico nem na identidade;
- `frames_total_*` **não** moram aqui (são cumulativos por processo, contrato do
  `scripts/watchdog.py`) — o teste fixa essa decisão para ninguém "consertar"
  depois;
- o módulo importa sem `gi`/`pyds`/`psutil`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import source_state  # noqa: E402
from source_state import SessionState  # noqa: E402

DEEPSTREAM_DIR = str(Path(__file__).resolve().parents[1])


class TestEpoch(unittest.TestCase):
    def setUp(self):
        # A guarda de unicidade é estado de módulo (de propósito: é o processo
        # que precisa ser único). Cada teste começa com ela limpa.
        source_state._last_epoch = None

    def test_epoch_estavel_dentro_da_sessao(self):
        state = SessionState.new("camera")
        primero = state.namespace()["session_epoch"]
        for _ in range(3):
            self.assertEqual(state.namespace()["session_epoch"], primero)
            self.assertEqual(state.epoch, primero)

    def test_duas_sessoes_no_mesmo_milissegundo_sao_distintas(self):
        momento = datetime(2026, 10, 31, 20, 0, 0, 123456, tzinfo=timezone.utc)
        primera = SessionState.new("camera", started_at=momento)
        segunda = SessionState.new("camera", started_at=momento)
        self.assertNotEqual(primera.epoch, segunda.epoch)
        # A guarda avança exatamente 1 ms, não um valor arbitrário.
        self.assertEqual(
            datetime.fromisoformat(segunda.epoch)
            - datetime.fromisoformat(primera.epoch),
            timedelta(milliseconds=1),
        )

    def test_epoch_e_utc_com_milissegundos(self):
        momento = datetime(2026, 10, 31, 20, 0, 0, 987654, tzinfo=timezone.utc)
        state = SessionState.new("drone", started_at=momento)
        parsed = datetime.fromisoformat(state.epoch)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.microsecond % 1000, 0)
        # 987654 µs truncado para 987 ms: resolução de ms, sem arredondar para
        # cima (o epoch não pode ser posterior ao STARTED_AT do processo).
        self.assertEqual(parsed, momento.replace(microsecond=987000))

    def test_epoch_naive_e_tratado_como_utc(self):
        naive = datetime(2026, 10, 31, 20, 0, 0)
        state = SessionState.new("camera", started_at=naive)
        self.assertEqual(
            datetime.fromisoformat(state.epoch),
            naive.replace(tzinfo=timezone.utc),
        )

    def test_processos_consecutivos_tem_epoch_distinto(self):
        """O gate do E6-T2 exige `session_epoch` distinto a cada ciclo."""
        code = (
            "import sys; sys.path.insert(0, "
            f"{DEEPSTREAM_DIR!r}); "
            "from source_state import SessionState; "
            "print(SessionState.new('camera').epoch)"
        )
        primeiro = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        segundo = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertNotEqual(primeiro, segundo)
        datetime.fromisoformat(primeiro)
        datetime.fromisoformat(segundo)


class TestNamespace(unittest.TestCase):
    def test_namespace_tem_exatamente_os_dois_campos(self):
        state = SessionState("phone", "2026-10-31T20:00:00.000+00:00")
        self.assertEqual(
            state.namespace(),
            {
                "source_id": "phone",
                "session_epoch": "2026-10-31T20:00:00.000+00:00",
            },
        )

    def test_source_id_vazio_e_erro(self):
        for ruim in ("", "   ", None):
            with self.subTest(source_id=ruim):
                with self.assertRaises(ValueError):
                    SessionState(ruim, "2026-10-31T20:00:00.000+00:00")

    def test_epoch_vazio_e_erro(self):
        with self.assertRaises(ValueError):
            SessionState("camera", "")

    def test_as_dict_junta_namespace_e_contadores(self):
        state = SessionState.new("camera")
        state.add("total_entries", 2)
        dumped = state.as_dict()
        self.assertEqual(dumped["source_id"], "camera")
        self.assertIn("session_epoch", dumped)
        self.assertEqual(dumped["total_entries"], 2)
        self.assertEqual(dumped["total_exits"], 0)


class TestContadores(unittest.TestCase):
    def test_contadores_padrao_sao_de_sessao(self):
        """`frames_total_*` é por processo (watchdog): não pode estar aqui."""
        state = SessionState.new("camera")
        self.assertEqual(
            state.counters(), {"total_entries": 0, "total_exits": 0}
        )
        self.assertNotIn("frames_total_in", state.counters())
        self.assertNotIn("frames_total_processed", state.counters())

    def test_add_e_counter(self):
        state = SessionState.new("camera")
        self.assertEqual(state.add("total_entries"), 1)
        self.assertEqual(state.add("total_entries", 4), 5)
        self.assertEqual(state.counter("total_entries"), 5)

    def test_contador_nao_desce_abaixo_de_zero(self):
        state = SessionState("camera", "2026-10-31T20:00:00.000+00:00")
        state.add("total_exits", 1)
        self.assertEqual(state.add("total_exits", -5), 0)

    def test_counter_de_nome_novo_e_zero_e_add_cria(self):
        state = SessionState("camera", "2026-10-31T20:00:00.000+00:00")
        self.assertEqual(state.counter("dwell_samples"), 0)
        state.add("dwell_samples", 3)
        self.assertEqual(state.counter("dwell_samples"), 3)

    def test_counters_devolve_copia(self):
        state = SessionState.new("camera")
        copia = state.counters()
        copia["total_entries"] = 99
        self.assertEqual(state.counter("total_entries"), 0)

    def test_reset_zera_preserva_chaves_e_identidade(self):
        state = SessionState.new("drone")
        state.add("total_entries", 7)
        state.add("total_exits", 6)
        state.add("dwell_samples", 4)

        snapshot = state.counters()
        zerado = state.reset()

        self.assertEqual(
            zerado,
            {"total_entries": 0, "total_exits": 0, "dwell_samples": 0},
        )
        self.assertEqual(state.counters(), zerado)
        # Histórico (o que já foi emitido) não é reescrito: o snapshot anterior
        # continua sendo o que era.
        self.assertEqual(snapshot["total_entries"], 7)
        self.assertEqual(state.source_id, "drone")
        self.assertEqual(state.namespace()["session_epoch"], state.epoch)

    def test_reset_devolve_copia(self):
        state = SessionState.new("camera")
        state.add("total_entries", 2)
        zerado = state.reset()
        zerado["total_entries"] = 5
        self.assertEqual(state.counter("total_entries"), 0)


class TestWithNamespace(unittest.TestCase):
    """Injeção do namespace num registro de telemetria (E3-T2).

    É o que `pipeline_real.log_event` usa em todo evento. A regra tem de ser
    aditiva (não sobrescrever campo explícito) e silenciosa quando ainda não há
    sessão — `startup_failed` de config inválida sai sem fonte de propósito.
    """

    def test_injeta_os_dois_campos(self):
        state = SessionState.new("drone")
        record = source_state.with_namespace({"event": "startup"}, state)
        self.assertEqual(record["source_id"], "drone")
        self.assertEqual(record["session_epoch"], state.epoch)
        self.assertEqual(record["event"], "startup")

    def test_campo_explicito_vence(self):
        state = SessionState("drone", "2026-10-31T20:00:00.000+00:00")
        record = source_state.with_namespace({"source_id": "camera"}, state)
        self.assertEqual(record["source_id"], "camera")
        self.assertEqual(
            record["session_epoch"], "2026-10-31T20:00:00.000+00:00"
        )

    def test_sem_sessao_registro_volta_intacto(self):
        original = {"event": "startup_failed", "error": "fonte inválida"}
        record = source_state.with_namespace(dict(original), None)
        self.assertEqual(record, original)
        self.assertNotIn("source_id", record)
        self.assertNotIn("session_epoch", record)

    def test_e_idempotente(self):
        state = SessionState.new("phone")
        uma = source_state.with_namespace({"event": "eos"}, state)
        duas = source_state.with_namespace(dict(uma), state)
        self.assertEqual(uma, duas)

    def test_sobrevive_a_serializacao_jsonl(self):
        """O contrato é a linha JSON que o watchdog e o dashboard leem."""
        state = SessionState.new("camera")
        linha = json.dumps(
            source_state.with_namespace({"event": "interval_metrics"}, state)
        )
        lido = json.loads(linha)
        self.assertEqual(lido["source_id"], state.source_id)
        self.assertEqual(lido["session_epoch"], state.epoch)


class TestModuloPuro(unittest.TestCase):
    def test_importa_sem_gi_pyds_psutil(self):
        code = (
            "import sys; sys.path.insert(0, "
            f"{DEEPSTREAM_DIR!r}); "
            "import source_state; "
            "proibidos = {'gi', 'pyds', 'psutil'} & set(sys.modules); "
            "sys.exit(0 if not proibidos else 1)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
