"""Testes da política do observador (`scripts/observer_policy.py`).

O módulo é pura decisão: nenhum docker, GPU, rede ou relógio real. O `now`
entra por parâmetro (aqui, constante) e o estado da política é uma instância
de `PolicyState` que cada teste controla — o mesmo desenho do
`test_watchdog_sources.py`, que também troca o relógio por um valor fixo.

Nota: `scripts/tests` não tem `__init__.py` de propósito (ver
`test_probe_logic.py`); o import abaixo usa o caminho do diretório.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import observer_policy as op  # noqa: E402

AGORA = 1000.0


def sonda(viva: bool = True, nivel: int = op.LEVEL_MEDIA,
          streak: int = op.MIN_OK_STREAK) -> op.Probe:
    """Atalho para montar uma sonda; defaults = fonte viva e confirmada."""
    motivo = None if viva else "connection_refused"
    return op.Probe(alive=viva, level=nivel, ok_streak=streak, reason=motivo)


def retrato(**campos) -> op.Observation:
    """Retrato padrão: app rodando, fonte ativa saudável, duas fontes.

    Cada teste sobrescreve só o que o cenário pede, o que deixa a intenção
    do caso visível na chamada.
    """
    base = dict(
        running=True,
        exit_code=None,
        last_frame_age_s=5.0,
        probes={"cam1": sonda(), "cam2": sonda()},
        active_source="cam1",
        source_ids=("cam1", "cam2"),
        now=AGORA,
    )
    base.update(campos)
    return op.Observation(**base)


def decidir(observation: op.Observation,
            state: op.PolicyState | None = None,
            policy: op.ObserverPolicy | None = None) -> op.Decision:
    return (policy or op.ObserverPolicy()).decide(
        observation, state or op.PolicyState())


# ---------------------------------------------------------------------------
# (a) fonte viva exige 2 DESCRIBE ok + nível 2
# ---------------------------------------------------------------------------
def test_streak_1_nao_sobe_para_nivel_2() -> None:
    """Uma sonda DESCRIBE ok não basta nem para sondar mídia."""
    obs = retrato(
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(nivel=op.LEVEL_DESCRIBE, streak=1)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.NONE
    assert decisao.source_id is None


def test_streak_2_com_nivel_1_pede_confirmacao_de_midia() -> None:
    obs = retrato(
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(nivel=op.LEVEL_DESCRIBE, streak=2)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.RUN_LEVEL2_PROBE
    assert decisao.source_id == "cam2"


def test_nivel_2_confirmado_vira_candidata() -> None:
    obs = retrato(
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(nivel=op.LEVEL_MEDIA, streak=2)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.SWITCH_TO
    assert decisao.source_id == "cam2"


# ---------------------------------------------------------------------------
# (b) exit 2 sem candidata viva -> standby
# ---------------------------------------------------------------------------
def test_exit_2_sem_fonte_viva_vai_para_standby() -> None:
    obs = retrato(
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.STOP_STANDBY
    assert decisao.source_id is None


# ---------------------------------------------------------------------------
# (c) 90 s sem frame + nível 1 falho -> mesma decisão
# ---------------------------------------------------------------------------
def test_90s_sem_frame_com_nivel_1_falho_vai_para_standby() -> None:
    obs = retrato(
        running=True,
        exit_code=None,
        last_frame_age_s=90.0,
        active_source="cam1",
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.STOP_STANDBY
    assert decisao.source_id is None


def test_90s_sem_frame_sozinho_nao_derruba_fonte() -> None:
    """Sem prova da sonda, frame velho é atraso de telemetria, não morte."""
    obs = retrato(
        last_frame_age_s=200.0,
        probes={"cam1": sonda(), "cam2": sonda()},
    )
    assert decidir(obs).action is op.Action.NONE


# ---------------------------------------------------------------------------
# (d) cooldown de 180 s
# ---------------------------------------------------------------------------
def test_cooldown_bloqueia_e_libera_troca() -> None:
    morta = {"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
             "cam2": sonda(nivel=op.LEVEL_MEDIA, streak=2)}
    policy = op.ObserverPolicy()
    state = op.PolicyState(last_switch_at=AGORA)

    dentro = policy.decide(retrato(exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
                                   probes=morta, now=AGORA + 100.0), state)
    assert dentro.action is op.Action.NONE
    assert dentro.reason == "cooldown_de_troca"
    assert state.last_switch_at == AGORA

    depois = policy.decide(retrato(exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
                                   probes=morta, now=AGORA + 181.0), state)
    assert depois.action is op.Action.SWITCH_TO
    assert depois.source_id == "cam2"
    # A troca consome a janela: a próxima só depois de mais 180 s.
    assert state.last_switch_at == AGORA + 181.0


# ---------------------------------------------------------------------------
# (e) sem preempção por padrão
# ---------------------------------------------------------------------------
def test_sem_preempcao_nao_interrompe_fonte_saudavel() -> None:
    """cam2 tem precedência maior, mas cam1 está saudável e manda."""
    obs = retrato(
        active_source="cam1",
        source_ids=("cam2", "cam1"),
        probes={"cam1": sonda(), "cam2": sonda()},
    )
    policy = op.ObserverPolicy()
    assert policy.preempt_higher_priority is False
    assert op.PREEMPT_HIGHER_PRIORITY is False
    decisao = policy.decide(obs, op.PolicyState())
    assert decisao.action is op.Action.NONE
    assert decisao.reason == "fonte_ativa_saudavel"


# ---------------------------------------------------------------------------
# (f) standby só com app parado/exit 2 e todas as fontes mortas
# ---------------------------------------------------------------------------
def test_standby_nao_entra_com_app_rodando_e_frames_frescos() -> None:
    obs = retrato(
        running=True,
        exit_code=None,
        last_frame_age_s=5.0,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    assert decidir(obs).action is not op.Action.STOP_STANDBY


def test_standby_nao_entra_com_app_parado_mas_fonte_respondendo() -> None:
    obs = retrato(
        running=False,
        exit_code=0,
        probes={"cam1": sonda(nivel=op.LEVEL_DESCRIBE, streak=1),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    assert decidir(obs).action is not op.Action.STOP_STANDBY


def test_standby_entra_com_app_parado_e_todas_mortas() -> None:
    obs = retrato(
        running=False,
        exit_code=0,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    assert decidir(obs).action is op.Action.STOP_STANDBY


# ---------------------------------------------------------------------------
# (g) saída do standby pela fonte de maior precedência viva
# ---------------------------------------------------------------------------
def test_standby_acorda_na_fonte_de_maior_precedencia() -> None:
    obs = retrato(
        mode=op.MODE_STANDBY,
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        active_source="cam1",
        source_ids=("cam2", "cam1"),
        probes={"cam1": sonda(), "cam2": sonda()},
    )
    state = op.PolicyState(last_switch_at=AGORA - 1.0)
    decisao = op.ObserverPolicy().decide(obs, state)
    assert decisao.action is op.Action.START_FROM_STANDBY
    assert decisao.source_id == "cam2"


def test_standby_sem_fonte_viva_continua_dormindo() -> None:
    obs = retrato(
        mode=op.MODE_STANDBY,
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    assert decidir(obs).action is op.Action.NONE


# ---------------------------------------------------------------------------
# (h) mutex e guarda de precedência
# ---------------------------------------------------------------------------
def test_switch_marker_ativo_cede_a_decisao() -> None:
    obs = retrato(
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        switch_marker_active=True,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.NONE
    assert decisao.reason == "switch_marker_ativo"


def test_active_source_env_set_cede_a_decisao() -> None:
    obs = retrato(
        running=False,
        exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        active_source_env_set=True,
        probes={"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
                "cam2": sonda(nivel=op.LEVEL_MEDIA, streak=2)},
    )
    decisao = decidir(obs)
    assert decisao.action is op.Action.NONE
    assert decisao.reason == "active_source_definido"


# ---------------------------------------------------------------------------
# (i) sonda de nível 2 uma única vez por janela
# ---------------------------------------------------------------------------
def test_nivel_2_nao_repete_na_janela() -> None:
    policy = op.ObserverPolicy()
    state = op.PolicyState()
    probes = {"cam1": sonda(viva=False, nivel=op.LEVEL_DESCRIBE, streak=0),
              "cam2": sonda(nivel=op.LEVEL_DESCRIBE, streak=2)}

    primeira = policy.decide(retrato(
        running=False, exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes=probes, now=AGORA), state)
    assert primeira.action is op.Action.RUN_LEVEL2_PROBE
    assert primeira.source_id == "cam2"
    assert state.last_level2_at["cam2"] == AGORA

    # Mesmo ciclo seguinte (e até o fim da janela) não repete a sonda.
    repetida = policy.decide(retrato(
        running=False, exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes=probes, now=AGORA), state)
    assert repetida.action is op.Action.NONE

    ainda_na_janela = policy.decide(retrato(
        running=False, exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes=probes, now=AGORA + 179.0), state)
    assert ainda_na_janela.action is op.Action.NONE

    proxima_janela = policy.decide(retrato(
        running=False, exit_code=op.EXIT_SOURCE_RECONNECT_EXHAUSTED,
        probes=probes, now=AGORA + 181.0), state)
    assert proxima_janela.action is op.Action.RUN_LEVEL2_PROBE
    assert state.last_level2_at["cam2"] == AGORA + 181.0
