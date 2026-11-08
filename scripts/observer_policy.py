#!/usr/bin/env python3
"""Decisão do observador de fontes do Vision Stability Lab.

Este módulo é **só decisão**: recebe um retrato do estado (app, telemetria e
resultado das sondas) e devolve a próxima ação. Nada aqui lê arquivo, env,
relógio, docker ou GPU — quem mede e quem executa é a integração
(`scripts/watchdog.py`), e o relógio entra por parâmetro (`Observation.now`).

Por quê: quando a fonte some, o pipeline termina com `exit_code=2`
(`source_reconnect_exhausted`) e o `restart: unless-stopped` do compose
recarrega o engine TensorRT (medido: ~34 s e ~1,1 GB RSS) em laço, sem chance
real de a fonte voltar nesse intervalo. Em vez de reiniciar em vazio, o
observador para o pipeline (`STOP_STANDBY`), confirma que uma fonte voltou
(`RUN_LEVEL2_PROBE` antes de confiar no `DESCRIBE`) e só então religa
(`START_FROM_STANDBY`) ou troca (`SWITCH_TO`).

A lógica vive fora do processo do pipeline de propósito: é testável sem Docker
nem GPU e continua válida justamente quando o pipeline morre — que é o evento
que ela precisa observar. Sem dependências externas (apenas stdlib).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuração (constantes; o construtor de `ObserverPolicy` permite trocar)
# ---------------------------------------------------------------------------
# Código de saída que o pipeline usa quando esgota as reconexões com a fonte
# (`source_reconnect_exhausted` em `deepstream/pipeline_real.py`). É a prova
# direta de que a fonte ativa morreu — não precisa esperar telemetria.
EXIT_SOURCE_RECONNECT_EXHAUSTED = 2

# 90 s sem nenhum `fps_in > 0` na telemetria. O pipeline publica em intervalos
# de 30 s e o watchdog já considera 60 s para `no_frames_detected`; aqui a
# fonte só é declarada morta com 3 intervalos, porque a decisão é mais cara
# (parar o pipeline / trocar a fonte) e exige folga para não reagir a um
# atraso pontual de publicação.
ACTIVE_FRAME_TIMEOUT_S = 90.0

# Nível 1 = DESCRIBE (o caminho responde); nível 2 = confirmação de mídia
# (chega quadro de verdade). `alive` no nível 1 não prova nada sobre o vídeo:
# uma URL pode descrever bem e não entregar frames, então a troca só acontece
# com o nível 2 confirmado.
LEVEL_DESCRIBE = 1
LEVEL_MEDIA = 2

# Duas sondas DESCRIBE consecutivas: uma falha isolada de rede/RTSP não é
# motivo para trocar de fonte.
MIN_OK_STREAK = 2

# Piso entre trocas automáticas. Uma fonte instável que volta e cai em segundos
# não pode virar pingue-pongue entre fontes; 180 s cobre o ciclo de restart do
# container (~34 s de engine + carência de frames) com folga.
SWITCH_COOLDOWN_S = 180.0

# Mesma janela do cooldown, por candidata: a sonda de nível 2 é cara (abre
# mídia) e o resultado não muda em segundos. Sem a janela o observador
# repetiria a sonda a cada ciclo enquanto espera a fonte voltar.
LEVEL2_PROBE_COOLDOWN_S = 180.0

# Preempção desligada por padrão: a fonte ativa saudável manda. O knob existe
# porque é a política que se quer poder inverter em campo (drone/câmera fixa
# com prioridades diferentes), mas ligá-la é decisão explícita de operação.
PREEMPT_HIGHER_PRIORITY = False

# Modos do observador. `switching` é o intervalo em que um helper de troca
# está recriando o container — qualquer decisão ali duplicaria o mutex.
MODE_WATCHING = "watching"
MODE_STANDBY = "standby_no_sources"
MODE_SWITCHING = "switching"


class Action(Enum):
    """Ação decidida. O `source_id` viaja no `Decision` que a acompanha."""

    NONE = "none"
    RUN_LEVEL2_PROBE = "run_level2_probe"
    SWITCH_TO = "switch_to"
    STOP_STANDBY = "stop_standby"
    START_FROM_STANDBY = "start_from_standby"


@dataclass(frozen=True)
class Decision:
    """Resultado de um ciclo.

    `source_id` só é preenchido em `SWITCH_TO`, `RUN_LEVEL2_PROBE` e
    `START_FROM_STANDBY`; em `STOP_STANDBY` e `NONE` fica `None`. `reason` é
    texto curto para o log estruturado (`observer_decision`), não contrato.
    """

    action: Action
    source_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class Probe:
    """Resultado da última sonda de uma fonte.

    `level` diz até onde a sonda chegou: `1` = só DESCRIBE, `2` = mídia
    confirmada. `ok_streak` conta DESCRIBE bem-sucedidos consecutivos —
    `alive=True` com streak baixo ainda é fonte em observação, não candidata.
    """

    alive: bool
    level: int = LEVEL_DESCRIBE
    ok_streak: int = 0
    reason: Optional[str] = None


@dataclass(frozen=True)
class Observation:
    """Retrato de um ciclo, montado pelo chamador (sem I/O aqui dentro).

    `source_ids` já vem na ordem de precedência resolvida: o observador não
    decide prioridade, só obedece. `now` e `last_frame_age_s` vêm de fora para
    o módulo não depender de relógio real.
    """

    running: bool
    exit_code: Optional[int]
    last_frame_age_s: Optional[float]
    probes: Mapping[str, Probe]
    active_source: str
    source_ids: Tuple[str, ...]
    now: float
    switch_marker_active: bool = False
    active_source_env_set: bool = False
    mode: str = MODE_WATCHING


@dataclass
class PolicyState:
    """Memória entre ciclos — o único estado que a política escreve.

    `last_switch_at` é global (uma troca por vez); `last_level2_at` é por
    fonte, porque cada candidata tem a sua janela de sonda. O chamador guarda
    uma instância e passa a mesma a cada ciclo.
    """

    last_switch_at: Optional[float] = None
    last_level2_at: Dict[str, float] = field(default_factory=dict)


class ObserverPolicy:
    """Máquina de estados da decisão, com os limiares do módulo como default.

    O construtor só existe para o teste (e para ajuste em campo) trocar
    limiares sem editar constantes. `decide()` não faz I/O: lê o
    `Observation`, eventualmente atualiza `PolicyState` e devolve `Decision`.
    """

    def __init__(self, *,
                 exit_source_reconnect_exhausted: int =
                 EXIT_SOURCE_RECONNECT_EXHAUSTED,
                 active_frame_timeout_s: float = ACTIVE_FRAME_TIMEOUT_S,
                 min_ok_streak: int = MIN_OK_STREAK,
                 switch_cooldown_s: float = SWITCH_COOLDOWN_S,
                 level2_probe_cooldown_s: float = LEVEL2_PROBE_COOLDOWN_S,
                 preempt_higher_priority: bool = PREEMPT_HIGHER_PRIORITY
                 ) -> None:
        self.exit_source_reconnect_exhausted = exit_source_reconnect_exhausted
        self.active_frame_timeout_s = active_frame_timeout_s
        self.min_ok_streak = min_ok_streak
        self.switch_cooldown_s = switch_cooldown_s
        self.level2_probe_cooldown_s = level2_probe_cooldown_s
        self.preempt_higher_priority = preempt_higher_priority

    # -- decisão -----------------------------------------------------------
    def decide(self, observation: Observation,
               state: PolicyState) -> Decision:
        """Uma ação por ciclo, na ordem: mutex -> standby -> troca -> sonda.

        A ordem importa. O mutex vem primeiro porque durante uma troca
        manual/automática qualquer outra ação disputaria o mesmo container.
        O standby vem antes da troca porque com o app parado não há fonte
        ativa para trocar — o que existe é uma fonte para acordar.
        """
        now = observation.now

        # Cede ao mutex de troca e à guarda de precedência do ACTIVE_SOURCE:
        # os dois são donos do container enquanto valem.
        if observation.switch_marker_active:
            return Decision(Action.NONE, reason="switch_marker_ativo")
        if observation.active_source_env_set:
            return Decision(Action.NONE, reason="active_source_definido")
        if observation.mode == MODE_SWITCHING:
            return Decision(Action.NONE, reason="troca_em_andamento")

        if observation.mode == MODE_STANDBY:
            return self._decide_standby(observation, state, now)

        active_dead = self._active_is_dead(observation)
        app_dead = self._app_is_dead(observation)

        if not (active_dead or app_dead):
            # Sem preempção: fonte ativa saudável não é interrompida, mesmo
            # que exista candidata melhor na ordem de precedência.
            return self._decide_no_preemption(observation, state, now)

        live = self._first_live_candidate(observation)
        if live is not None:
            if not self._cooldown_liberado(state.last_switch_at, now,
                                       self.switch_cooldown_s):
                return Decision(Action.NONE, reason="cooldown_de_troca")
            state.last_switch_at = now
            return Decision(Action.SWITCH_TO, source_id=live,
                            reason="fonte_ativa_morta")

        # Nenhuma fonte sequer responde ao DESCRIBE: parar o pipeline em vez
        # de deixar o `restart` recarregar o engine em vazio.
        if not self._any_source_alive(observation):
            return Decision(Action.STOP_STANDBY,
                            reason="sem_fonte_viva")

        probed = self._maybe_run_level2_probe(observation, state, now)
        if probed is not None:
            return probed
        return Decision(Action.NONE, reason="aguardando_fonte")

    # -- ramos -------------------------------------------------------------
    def _decide_standby(self, observation: Observation, state: PolicyState,
                        now: float) -> Decision:
        """Dormindo: acorda na primeira fonte viva ou confirma a mídia dela.

        A saída do standby usa a ordem de precedência **completa** (inclusive
        a fonte que estava ativa): com o app parado não há troca, o que existe
        é a melhor fonte disponível para religar.
        """
        live = self._first_live_source(observation)
        if live is not None:
            return Decision(Action.START_FROM_STANDBY, source_id=live,
                            reason="fonte_viva_em_standby")

        probed = self._maybe_run_level2_probe(observation, state, now)
        if probed is not None:
            return probed
        return Decision(Action.NONE, reason="standby_sem_fonte_viva")

    def _decide_no_preemption(self, observation: Observation,
                              state: PolicyState, now: float) -> Decision:
        """Fonte ativa saudável: só troca se a preempção estiver ligada."""
        if not self.preempt_higher_priority:
            return Decision(Action.NONE, reason="fonte_ativa_saudavel")
        candidate = self._first_live_candidate(observation)
        if candidate is None:
            return Decision(Action.NONE, reason="fonte_ativa_saudavel")
        if not self._precedes(observation, candidate,
                              observation.active_source):
            return Decision(Action.NONE, reason="fonte_ativa_saudavel")
        if not self._cooldown_liberado(state.last_switch_at, now,
                                   self.switch_cooldown_s):
            return Decision(Action.NONE, reason="cooldown_de_troca")
        state.last_switch_at = now
        return Decision(Action.SWITCH_TO, source_id=candidate,
                        reason="preempcao_por_prioridade")

    def _maybe_run_level2_probe(self, observation: Observation,
                                state: PolicyState,
                                now: float) -> Optional[Decision]:
        """Sonda de nível 2 para a candidata eleita, uma vez por janela.

        Só a primeira fonte elegível na ordem de precedência é sondada: é para
        ela que o observador trocaria/religaria, então sondar as de baixo
        gastaria mídia sem mudar a decisão.
        """
        candidate = self._first_level2_candidate(observation)
        if candidate is None:
            return None
        last = state.last_level2_at.get(candidate)
        if not self._cooldown_liberado(last, now, self.level2_probe_cooldown_s):
            return None
        state.last_level2_at[candidate] = now
        return Decision(Action.RUN_LEVEL2_PROBE, source_id=candidate,
                        reason="confirmar_midia")

    # -- leitura do retrato ------------------------------------------------
    def _active_is_dead(self, observation: Observation) -> bool:
        """Morte da fonte ativa: exit 2, ou 90 s sem frame com DESCRIBE falho.

        A idade do frame sozinha não basta: o `exit_code=2` é prova direta,
        mas telemetria atrasada também envelhece o último frame. Por isso o
        segundo critério exige a sonda de nível 1 dizendo que a fonte não
        responde. Sonda ausente não prova morte — nesse caso o observador
        espera.
        """
        if observation.exit_code == self.exit_source_reconnect_exhausted:
            return True
        age = observation.last_frame_age_s
        if age is None or age < self.active_frame_timeout_s:
            return False
        probe = observation.probes.get(observation.active_source)
        return probe is not None and not probe.alive

    def _app_is_dead(self, observation: Observation) -> bool:
        """App parado ou já saído com `source_reconnect_exhausted`."""
        return (not observation.running) or (
            observation.exit_code == self.exit_source_reconnect_exhausted)

    def _is_live(self, probe: Optional[Probe]) -> bool:
        """Candidata viva: DESCRIBE estável e mídia confirmada (nível 2)."""
        return (probe is not None and probe.alive
                and probe.ok_streak >= self.min_ok_streak
                and probe.level >= LEVEL_MEDIA)

    def _is_level2_candidate(self, probe: Optional[Probe]) -> bool:
        """Apta a sonda de mídia: DESCRIBE estável, nível 2 ainda não feito."""
        return (probe is not None and probe.alive
                and probe.ok_streak >= self.min_ok_streak
                and probe.level < LEVEL_MEDIA)

    def _any_source_alive(self, observation: Observation) -> bool:
        """Existe alguma fonte respondendo ao DESCRIBE (mesmo sem nível 2)."""
        return any(probe.alive for probe in observation.probes.values())

    def _first_live_source(self, observation: Observation) -> Optional[str]:
        """Primeira fonte viva na ordem de precedência (inclusive a ativa)."""
        for source_id in observation.source_ids:
            if self._is_live(observation.probes.get(source_id)):
                return source_id
        return None

    def _first_live_candidate(self, observation: Observation
                              ) -> Optional[str]:
        """Primeira fonte viva que não é a ativa — alvo de `SWITCH_TO`."""
        for source_id in observation.source_ids:
            if source_id == observation.active_source:
                continue
            if self._is_live(observation.probes.get(source_id)):
                return source_id
        return None

    def _first_level2_candidate(self, observation: Observation
                                ) -> Optional[str]:
        """Primeira fonte com DESCRIBE estável que ainda não teve mídia.

        Inclui a fonte ativa: com o app parado, confirmar a mídia dela é o que
        permite religar pela própria fonte.
        """
        for source_id in observation.source_ids:
            if self._is_level2_candidate(observation.probes.get(source_id)):
                return source_id
        return None

    def _precedes(self, observation: Observation, source_id: str,
                  other_id: str) -> bool:
        """`source_id` vem antes de `other_id` na ordem de precedência."""
        ids = observation.source_ids
        try:
            return ids.index(source_id) < ids.index(other_id)
        except ValueError:
            # Fora da lista não há precedência a respeitar.
            return False

    @staticmethod
    def _cooldown_liberado(last_at: Optional[float], now: float,
                       cooldown_s: float) -> bool:
        """Janela liberada? Relógio para trás (reset) mantém o bloqueio."""
        return last_at is None or (now - last_at) >= cooldown_s
