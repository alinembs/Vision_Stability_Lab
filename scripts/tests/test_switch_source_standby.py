"""Troca de fonte a partir do standby, com o `.sh` de ponta a ponta (Tarefa 4).

O `scripts/switch_source.sh` agora convive com o observador (`watchdog.py`), que
pode ter **parado** o container (`docker stop`) quando não há fonte, e que usa o
**mesmo** marcador `data/config/switch_in_progress` como mutex das trocas. Até
aqui nenhum teste executava o script inteiro: `test_switch_source.py` cobre só a
metade pura (`switch_source_lib.py`) e `test_watchdog_standby.py` cobre só o
lado do observador. Sem prova de ponta a ponta, os três comportamentos abaixo
eram promessas sem teste:

1. **subir do standby**: com o container parado, o `docker exec ... printenv
   ACTIVE_SOURCE` da guarda de precedência falha — e a troca **segue**, não
   aborta. O `docker compose up -d --force-recreate` do passo 5 é o que inicia o
   container de novo (é `up`, não `start`);
2. **ceder ao mutex do observador**: marcador de outro dono com `expires_at` no
   futuro -> `exit 3 reason=troca_em_andamento`, `active_source` intocado,
   `compose up` **não** chamado e o marcador continua byte a byte igual (o
   `cleanup` do script só remove o marcador do próprio owner);
3. **descartar órfão**: marcador expirado (`kill -9` no meio de outra troca) não
   trava a troca para sempre — é descartado e a troca segue até o PASS.

Como o teste roda sem docker, sem rede, sem GStreamer e sem `sleep`: um shim
`docker` (arquivo executável, nunca um container) é preposto ao `PATH` e
responde `exec`, `inspect` e `compose up`, gravando cada invocação em JSONL. O
`compose up` do shim é quem escreve o primeiro `interval_metrics` — igual ao
pipeline de verdade, e **depois** do offset de telemetria medido pelo script,
então a espera termina no primeiro registro lido (sem dormir).

Estado do teste em diretório temporário (`SWITCH_CONFIG_DIR`, `ENV_FILE`,
`TELEMETRY_FILE`, `SWITCH_LOG`) e cwd temporário: o `data/config/` e o
`data/logs/` do repositório não são tocados.

Nota: `scripts/tests` não tem `__init__.py` de propósito (ver
`test_probe_logic.py`); não há import de módulo do projeto — o alvo é o script.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
SCRIPT = RAIZ / "scripts" / "switch_source.sh"

FONTE = "drone"
CONTAINER = "vision-stability-app"
SERVICO = "vision-app"
# `camera` é `uri_env: RTSP_URI` no sources.yaml real; o YAML é validado
# inteiro, então a variável precisa existir mesmo pedindo `drone`. Sem
# credencial de propósito (DIV-001 e a guarda de segredos).
URI_CAMERA_SEM_CREDENCIAL = "rtsp://192.168.59.108:5543/live/channel0"

# `StartedAt` antigo e fixo: a telemetria do processo "novo" (escrita pelo shim)
# tem `session_epoch` posterior, que é o que o pós-check exige.
STARTED_AT = "2026-01-01T00:00:00Z"
SESSION_EPOCH = "2026-01-01T01:00:00.000+00:00"

# Shim de `docker`: arquivo executável, não container (regra global: um único
# container DeepStream por projeto — e nenhum extra "de teste").
SHIM_DOCKER = '''#!/usr/bin/env python3
"""`docker` de mentira para o teste de ponta a ponta do switch_source.sh."""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv, ensure_ascii=False) + "\\n")


def flag(name):
    return os.environ.get(name, "0") == "1"


if argv[:1] == ["exec"]:
    # Container parado em standby: é exatamente o erro do docker de verdade.
    if not flag("SHIM_RUNNING"):
        print("Error response from daemon: container " + argv[1] +
              " is not running", file=sys.stderr)
        raise SystemExit(1)
    valor = os.environ.get("SHIM_ACTIVE_SOURCE_VALUE", "")
    if valor:
        print(valor)
    raise SystemExit(0)

if argv[:1] == ["inspect"]:
    print(os.environ.get("SHIM_STARTED_AT", "2026-01-01T00:00:00Z"))
    raise SystemExit(0)

if argv[:1] == ["compose"] and "up" in argv:
    # O processo novo do pipeline escreve o primeiro `interval_metrics`; o
    # `source_id` vem do controle já gravado pelo script (prova a ordem:
    # arquivo escrito antes do `up`).
    source_id = os.environ.get("SHIM_SOURCE_ID", "")
    controle = os.environ.get("SHIM_ACTIVE_SOURCE_FILE", "")
    if controle and os.path.exists(controle):
        with open(controle, encoding="utf-8") as handle:
            lido = handle.read().strip()
        if lido:
            source_id = lido
    registro = {"event": "interval_metrics", "source_id": source_id,
                "fps_in": 19.9,
                "session_epoch": os.environ["SHIM_SESSION_EPOCH"]}
    with open(os.environ["SHIM_TELEMETRY"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(registro) + "\\n")
    raise SystemExit(0)

# Qualquer outra invocação (ex.: `compose ps`) é sucesso sem efeito.
raise SystemExit(0)
'''

# Shim de `PYTHON` (o `.sh` respeita a variável): reproduz a corrida do TOCTOU
# do marcador órfão. Na **primeira** decisão `marker-expired` sobre o marcador
# original, outro dono (observador/dashboard) reescreve o path com um marcador
# **válido** e só então respondemos "expirado" (exit 0) — exatamente a janela em
# que o script antigo movia e apagava o marcador alheio. Todas as outras
# chamadas vão para o Python real (`os.execv`), então a revalidação do arquivo
# movido devolve o veredito de verdade.
SHIM_PYTHON = '''#!/usr/bin/env python3
"""`PYTHON` de mentira para o teste da corrida do marcador expirado."""
import os
import sys

argv = sys.argv[1:]
alcance = os.environ.get("SHIM_TARGET_MARKER", "")
delta = os.environ.get("SHIM_RACER_STAMP", "")
alheio = os.environ.get("SHIM_RACER_MARKER", "")
decisao = "marker-expired" in argv and "--file" in argv
alvo = argv[argv.index("--file") + 1] if decisao else ""
if (alheio and alcance and delta and decisao
        and alvo == alcance
        and not os.path.exists(delta)):
    # Outro dono ganhou a corrida: marcador válido no path, e a decisão que o
    # script recebe é a do órfão que ele leu antes.
    with open(delta, "w", encoding="utf-8") as handle:
        handle.write("1")
    with open(alcance, "w", encoding="utf-8") as handle:
        handle.write(alheio)
    raise SystemExit(0)
# Terceira ponta da corrida (tripla): quando o script revalida o arquivo já
# deslocado, um **segundo** dono ocupa o path canônico e a resposta é
# "válido" (exit 1) — o `mv -n` de volta não sobrescreve ninguém, então o
# marcador deslocado fica órfão de arquivo até o `cleanup`.
delta_b = os.environ.get("SHIM_RACER_STAMP_B", "")
alheio_b = os.environ.get("SHIM_RACER_MARKER_B", "")
if (alheio_b and alcance and delta_b and decisao
        and alvo.startswith(alcance + ".stale.")
        and not os.path.exists(delta_b)):
    with open(delta_b, "w", encoding="utf-8") as handle:
        handle.write("1")
    with open(alcance, "w", encoding="utf-8") as handle:
        handle.write(alheio_b)
    raise SystemExit(1)
real = os.environ["SHIM_REAL_PYTHON"]
os.execv(real, [real, *argv])
'''


def _em(segundos: float) -> str:
    """`expires_at` no formato que script e observador usam."""
    instante = datetime.now(timezone.utc) + timedelta(seconds=segundos)
    return instante.strftime("%Y-%m-%dT%H:%M:%SZ")


class SwitchHarness(unittest.TestCase):
    """Ambiente isolado do `.sh`: shim no PATH e estado todo em tmp."""

    # Conteúdo que o shim de `PYTHON` grava no marcador "roubado"; vazio
    # desliga o racer (os demais casos continuam iguais). `racer_marker_b` é o
    # terceiro dono da corrida tripla: ocupa o path canônico na revalidação.
    racer_marker = ""
    racer_marker_b = ""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

        self.bin = self.dir / "bin"
        self.bin.mkdir()
        shim = self.bin / "docker"
        shim.write_text(SHIM_DOCKER, encoding="utf-8")
        shim.chmod(0o755)
        python_shim = self.bin / "python"
        python_shim.write_text(SHIM_PYTHON, encoding="utf-8")
        python_shim.chmod(0o755)
        self.python_shim = python_shim

        # Só o que `switch_source_lib.resolve` precisa para o `sources.yaml`
        # real: `drone` é `uri: rtsp://${MTX_HOST:-mediamtx}:8554/drone`, e
        # MTX_HOST evita o fallback que consulta o IP da LAN do host. O
        # `camera` usa `uri_env: RTSP_URI`, exigida mesmo quando a fonte pedida
        # não é ela (o YAML é validado inteiro) — URI sem credencial.
        self.env_file = self.dir / "deploy.env"
        self.env_file.write_text(
            "MTX_HOST=192.168.59.102\n"
            f"RTSP_URI={URI_CAMERA_SEM_CREDENCIAL}\n"
            "ACTIVE_SOURCE=\n",
            encoding="utf-8")

        self.config_dir = self.dir / "config"
        self.config_dir.mkdir()
        self.control_file = self.config_dir / "active_source"
        self.marker = self.config_dir / "switch_in_progress"
        self.telemetry = self.dir / "live.jsonl"
        self.telemetry.write_text("", encoding="utf-8")
        self.switch_log = self.dir / "switch.jsonl"
        self.shim_log = self.dir / "docker.jsonl"
        self.compose_file = self.dir / "docker-compose.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")

    # -- infraestrutura -----------------------------------------------------
    def run_switch(self, *args: str, running: bool = False,
                   source: str = FONTE) -> subprocess.CompletedProcess:
        """Executa o `.sh` com o shim no PATH (cwd temporário, sem docker real)."""
        env = os.environ.copy()
        env.pop("ACTIVE_SOURCE", None)
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
            "PYTHON": str(self.python_shim),
            "ENV_FILE": str(self.env_file),
            "COMPOSE_FILE": str(self.compose_file),
            "SWITCH_CONFIG_DIR": str(self.config_dir),
            "TELEMETRY_FILE": str(self.telemetry),
            "SWITCH_LOG": str(self.switch_log),
            "APP_CONTAINER": CONTAINER,
            "COMPOSE_SERVICE": SERVICO,
            "SHIM_LOG": str(self.shim_log),
            "SHIM_RUNNING": "1" if running else "0",
            "SHIM_STARTED_AT": STARTED_AT,
            "SHIM_SESSION_EPOCH": SESSION_EPOCH,
            "SHIM_SOURCE_ID": source,
            "SHIM_ACTIVE_SOURCE_FILE": str(self.control_file),
            "SHIM_TELEMETRY": str(self.telemetry),
            # Racers da corrida do marcador: vazio = shim só delega ao Python real.
            "SHIM_REAL_PYTHON": sys.executable,
            "SHIM_TARGET_MARKER": str(self.marker),
            "SHIM_RACER_STAMP": str(self.dir / "racer.stamp"),
            "SHIM_RACER_MARKER": self.racer_marker,
            "SHIM_RACER_STAMP_B": str(self.dir / "racer_b.stamp"),
            "SHIM_RACER_MARKER_B": self.racer_marker_b,
        })
        return subprocess.run(
            [str(SCRIPT), source, "--skip-precheck", *args],  # pré-check = socket
            cwd=str(self.dir), env=env, capture_output=True, text=True,
            timeout=60, check=False)

    def docker_calls(self) -> list:
        if not self.shim_log.exists():
            return []
        return [json.loads(linha) for linha in
                self.shim_log.read_text(encoding="utf-8").splitlines()
                if linha.strip()]

    def compose_up_calls(self) -> list:
        return [argv for argv in self.docker_calls()
                if argv[:1] == ["compose"] and "up" in argv]

    def write_marker(self, owner: str, expires_at: str,
                     to: str = "camera") -> bytes:
        payload = (f'{{"to": "{to}", "owner": "{owner}", '
                   f'"expires_at": "{expires_at}"}}\n')
        self.marker.write_text(payload, encoding="utf-8")
        return self.marker.read_bytes()

    def switch_records(self) -> list:
        if not self.switch_log.exists():
            return []
        return [json.loads(linha) for linha in
                self.switch_log.read_text(encoding="utf-8").splitlines()
                if linha.strip()]

    # -- asserções comuns ---------------------------------------------------
    def assert_troca_confirmada(self, proc: subprocess.CompletedProcess) -> None:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"PASS source={FONTE}", proc.stdout)
        self.assertEqual(len(self.compose_up_calls()), 1,
                         f"compose up deveria ser 1x: {self.docker_calls()}")
        self.assertEqual(
            self.control_file.read_text(encoding="utf-8").strip(), FONTE)
        self.assertFalse(self.marker.exists(), "marcador deveria sair no fim")
        registros = self.switch_records()
        self.assertEqual(registros[-1]["result"], "PASS")
        self.assertEqual(registros[-1]["to"], FONTE)
        self.assertEqual(registros[-1]["probe"], "skipped")


class SubidaDoStandbyTest(SwitchHarness):
    """Cenário 1: container parado (observador em standby) e a troca o sobe."""

    def test_standby_nao_bloqueia_e_up_sobe_o_container(self) -> None:
        proc = self.run_switch(running=False)

        self.assert_troca_confirmada(proc)
        # A guarda de precedência tentou o exec e seguiu (container parado).
        self.assertTrue(any(argv[:1] == ["exec"] for argv in self.docker_calls()))
        # O `up` é o que inicia o container parado — e foi chamado com o
        # serviço certo, uma única vez.
        self.assertIn(SERVICO, self.compose_up_calls()[0])
        # `--force-recreate` é obrigatório (volume, não configuração).
        self.assertIn("--force-recreate", self.compose_up_calls()[0])


class MutexDoObservadorTest(SwitchHarness):
    """Cenário 2: a troca cede ao marcador do observador, sem apagar nada."""

    def test_marcador_do_observador_bloqueia_sem_tocar_em_nada(self) -> None:
        original = self.write_marker("observer:watchdog", _em(3600))

        proc = self.run_switch(running=False)

        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("reason=troca_em_andamento", proc.stdout)
        # Nada subiu, nada foi escrito, o marcador do outro dono está intacto.
        self.assertEqual(self.compose_up_calls(), [])
        self.assertFalse(self.control_file.exists())
        self.assertEqual(self.marker.read_bytes(), original)
        # A tentativa bloqueada também fica auditada.
        self.assertEqual(self.switch_records()[-1]["result"], "FAIL")
        self.assertEqual(self.switch_records()[-1]["reason"],
                         "troca_em_andamento")

    def test_mutex_vence_mesmo_com_o_container_parado(self) -> None:
        original = self.write_marker("observer:watchdog", _em(3600))

        proc = self.run_switch(running=False)

        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.compose_up_calls(), [])
        self.assertFalse(any(argv[:1] == ["inspect"]
                             for argv in self.docker_calls()))
        self.assertEqual(self.marker.read_bytes(), original)


class MarcadorOrfaoTest(SwitchHarness):
    """Cenário 3: marcador expirado (processo morto) não trava a troca."""

    def test_marcador_expirado_e_descartado_e_a_troca_prossegue(self) -> None:
        self.write_marker("script:999:1", _em(-3600))

        proc = self.run_switch(running=True)

        self.assert_troca_confirmada(proc)
        # O marcador do próprio script também saiu (nada de `*.stale.*` sobrando).
        self.assertEqual(list(self.config_dir.glob("switch_in_progress*")), [])

    def test_orfao_de_outro_dono_tambem_e_descartado(self) -> None:
        """Respeitar outro dono vale **enquanto o marcador é válido**.

        Um observador que morreu no meio da troca deixa o marcador com o
        próprio `owner`; se órfão também bloqueasse, a troca ficaria travada
        até alguém apagar o arquivo à mão — e o observador é justamente quem
        para o container quando não há fonte. A validade (`expires_at`) é o
        critério, não o dono.
        """
        self.write_marker("observer:watchdog", _em(-3600))

        proc = self.run_switch(running=True)

        self.assert_troca_confirmada(proc)
        self.assertEqual(list(self.config_dir.glob("switch_in_progress*")), [])


class CorridaDoOrfaoTest(SwitchHarness):
    """Achado 1 (HIGH): o órfão pode virar marcador válido antes do descarte.

    Entre `marker_expired` (leitura) e o `mv`/`rm` do descarte, outro dono pode
    remover o órfão e criar um marcador **válido** no mesmo path. Sem revalidar
    o arquivo movido, o script apagava o mutex alheio e reivindicava a troca —
    dois atores mexendo no mesmo container. O shim de `PYTHON` reproduz a
    corrida: responde "expirado" e, antes de devolver, grava um marcador válido
    de outro dono (o observador) no path.
    """

    @property
    def racer_marker(self) -> str:  # type: ignore[override]
        return ('{"to": "camera", "owner": "observer:watchdog", '
                f'"expires_at": "{_em(3600)}", "origin": "watchdog"}}\n')

    def test_orfao_que_vira_valido_e_devolvido_e_a_troca_cede(self) -> None:
        self.write_marker("script:999:1", _em(-3600))   # órfão de quem morreu
        esperado = self.racer_marker

        proc = self.run_switch(running=True)

        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("reason=troca_em_andamento", proc.stdout)
        # O marcador do outro dono voltou ao path original, byte a byte...
        self.assertEqual(self.marker.read_text(encoding="utf-8"), esperado)
        # ...e não sobrou `.stale.*` do descarte.
        self.assertEqual(
            list(self.config_dir.glob("switch_in_progress.*")), [],
            "arquivo temporário do descarte ficou para trás")
        # Nada foi escrito e o pipeline não foi tocado.
        self.assertFalse(self.control_file.exists())
        self.assertEqual(self.compose_up_calls(), [])
        registros = self.switch_records()
        self.assertEqual(registros[-1]["result"], "FAIL")
        self.assertEqual(registros[-1]["reason"], "troca_em_andamento")
        self.assertEqual(registros[-1]["probe"], "blocked")


class CorridaTriplaTest(SwitchHarness):
    """Órfão expirado + dois marcadores válidos na janela do descarte.

    Sequência reproduzida pelo shim de `PYTHON`:

    1. o script lê o órfão e recebe "expirado" (exit 0), mas antes disso o
       observador (**A**) gravou um marcador válido no path canônico;
    2. o script move A para `switch_in_progress.stale.$$` e revalida: o
       dashboard (**B**) ocupa o path canônico e a resposta é "válido" (exit 1);
    3. o `mv -n` de volta não sobrescreve B, então A fica preso no `.stale.$$`.

    O mutex continua correto (o canônico tem B e a troca cede: exit 3). O
    defeito era o arquivo de A sobrar no diretório de configuração para
    sempre: o `cleanup` agora registra o conteúdo dele no log — evidência de
    quem era o marcador deslocado — e remove **só** o `.stale.$$` do próprio
    processo.
    """

    @property
    def racer_marker(self) -> str:  # type: ignore[override]
        """Marcador válido de A, o que o `mv` leva para o `.stale.$$`."""
        return ('{"to": "camera", "owner": "observer:watchdog", '
                f'"expires_at": "{_em(3600)}", "origin": "watchdog"}}\n')

    @property
    def racer_marker_b(self) -> str:  # type: ignore[override]
        """Marcador válido de B, o que ocupa o canônico no `mv -n` de volta."""
        return ('{"to": "drone", "owner": "dashboard:teste", '
                f'"expires_at": "{_em(3600)}", "origin": "dashboard"}}\n')

    def test_deslocado_sem_lugar_e_logado_e_removido_no_cleanup(self) -> None:
        self.write_marker("script:999:1", _em(-3600))   # órfão de quem morreu
        deslocado = json.loads(self.racer_marker)
        ocupante = self.racer_marker_b

        proc = self.run_switch(running=True)

        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("reason=troca_em_andamento", proc.stdout)
        # Nada foi escrito e o pipeline não foi tocado.
        self.assertFalse(self.control_file.exists())
        self.assertEqual(self.compose_up_calls(), [])
        # O marcador canônico é o de B: nem sobrescrito pelo `mv -n`, nem
        # apagado pelo `cleanup` (a regra de não apagar marcador alheio vale).
        self.assertEqual(self.marker.read_text(encoding="utf-8"), ocupante)
        # O deslocado de A não sobra no diretório de configuração...
        self.assertEqual(
            list(self.config_dir.glob("switch_in_progress.stale.*")), [],
            "marcador deslocado ficou para trás")
        # ...mas o dono dele fica registrado no log antes da remoção.
        self.assertIn(deslocado["owner"], proc.stderr)
        registros = self.switch_records()
        self.assertEqual(registros[-1]["result"], "FAIL")
        self.assertEqual(registros[-1]["reason"], "troca_em_andamento")
        self.assertEqual(registros[-1]["probe"], "blocked")


if __name__ == "__main__":
    unittest.main()
