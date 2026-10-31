"""Sessão de fonte: identidade, namespace de telemetria e reset (E3-T1).

O JSONL de telemetria é **append-only**: trocar de fonte não apaga histórico
(plano §4.2). O isolamento entre sessões é por namespace — cada registro
carrega `source_id` e `session_epoch` — e o estado de visão (trilhas, eventos,
dwell) é reiniciado no boot da sessão nova, não em runtime (D4: troca = restart
do processo).

Este módulo é **puro** (sem `gi`/`pyds`/`psutil`) para rodar no host, dentro do
container e sob `pytest`, como `sources.py` e `source_reconnect.py`.

Duas armadilhas que o desenho evita:

1. **`session_epoch` não é `id()` nem contador de sessão.** É o instante ISO8601
   UTC (milissegundos) do início do processo, comparável com
   `docker inspect .State.StartedAt` — o mesmo campo que `scripts/mem_budget.sh`
   já expõe. Duas execuções consecutivas nunca compartilham epoch, e duas
   sessões criadas no **mesmo milissegundo** também não (guarda monotônica em
   `next_epoch`).
2. **Nem todo contador é da sessão.** `frames_total_in` e
   `frames_total_processed` são **cumulativos por processo** de propósito: é o
   contrato que `scripts/watchdog.py` usa desde o fix de high-water (plano
   §4.2). Reiniciá-los a cada sessão quebraria a detecção de
   `no_frames_escalation`. Os contadores que de fato atravessariam a troca e
   fariam a fonte nova herdar a anterior (R5) são os de trilha — `total_entries`
   e `total_exits` — e esses vivem aqui.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

# Resolução do epoch. Milissegundos: comparável com `StartedAt` do Docker e
# suficiente para separar sessões de um mesmo dia sem inflar o JSONL.
EPOCH_TIMESPEC = "milliseconds"

# Contadores **de sessão** (zerados a cada fonte). Ver o docstring do módulo
# para por que `frames_total_*` não estão aqui.
DEFAULT_COUNTERS = ("total_entries", "total_exits")

# Último epoch emitido neste processo. A guarda é necessária porque dois
# `SessionState.new()` podem cair no mesmo milissegundo (boot rápido, teste) e
# epoch repetido faria duas sessões parecerem a mesma no dashboard e no gate do
# E6-T2 ("session_epoch distinto em cada ciclo").
_epoch_lock = threading.Lock()
_last_epoch: Optional[datetime] = None


def format_epoch(moment: datetime) -> str:
    """Formata um instante como ISO8601 UTC com milissegundos.

    `datetime` sem timezone é tratado como UTC: o pipeline roda no container com
    `TZ` indefinido e um epoch naive seria ambíguo no confronto com o
    `StartedAt` do Docker (que é UTC).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec=EPOCH_TIMESPEC)


def next_epoch(started_at: Optional[datetime] = None) -> str:
    """Próximo epoch do processo, único mesmo em chamadas no mesmo ms.

    `started_at` é o `STARTED_AT` do pipeline (início real do processo); sem
    ele, usa o relógio agora. Quando o candidato não é estritamente maior que o
    último emitido — porque dois `new()` caíram no mesmo milissegundo, ou porque
    o chamador passou o mesmo `STARTED_AT` duas vezes — avança 1 ms até ser.
    """
    global _last_epoch
    with _epoch_lock:
        moment = started_at or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc).replace(
            microsecond=(moment.microsecond // 1000) * 1000
        )
        if _last_epoch is not None and moment <= _last_epoch:
            moment = _last_epoch + timedelta(milliseconds=1)
        _last_epoch = moment
        return format_epoch(moment)


class SessionState:
    """Sessão de uma fonte: quem é, desde quando e com que contadores.

    Não é `frozen`: os contadores de trilha mudam durante a sessão. O que não
    muda nunca é a identidade (`source_id`, `epoch`) — se mudasse, a amostra
    sairia com namespace inconsistente.
    """

    def __init__(
        self,
        source_id: str,
        epoch: str,
        counters: Optional[Iterable[str]] = None,
    ) -> None:
        clean_id = str(source_id or "").strip()
        if not clean_id:
            raise ValueError(
                "SessionState exige `source_id` não vazio: o namespace da "
                "telemetria não pode sair sem a fonte que produziu a amostra"
            )
        clean_epoch = str(epoch or "").strip()
        if not clean_epoch:
            raise ValueError(
                "SessionState exige `epoch` não vazio: sem ele não há como "
                "separar duas sessões da mesma fonte no JSONL append-only"
            )
        self.source_id = clean_id
        self.epoch = clean_epoch
        nomes = counters if counters is not None else DEFAULT_COUNTERS
        self._counters: Dict[str, int] = {str(name): 0 for name in nomes}

    # -- construção --------------------------------------------------------
    @classmethod
    def new(
        cls,
        source_id: str,
        started_at: Optional[datetime] = None,
        counters: Optional[Iterable[str]] = None,
    ) -> "SessionState":
        """Cria a sessão com epoch derivado de `started_at` (padrão: agora).

        É o construtor que o pipeline usa no boot: `started_at` é o
        `STARTED_AT` do processo, então o epoch é estável dentro do processo e
        distinto entre execuções consecutivas.
        """
        return cls(source_id, next_epoch(started_at), counters=counters)

    # -- namespace ---------------------------------------------------------
    def namespace(self) -> Dict[str, str]:
        """Campos que **todo** registro de telemetria passa a carregar."""
        return {"source_id": self.source_id, "session_epoch": self.epoch}

    # -- contadores de sessão ---------------------------------------------
    def counter(self, name: str) -> int:
        """Valor atual de um contador (0 quando ainda não foi tocado)."""
        return self._counters.get(str(name), 0)

    def add(self, name: str, amount: int = 1) -> int:
        """Soma `amount` a um contador de sessão e devolve o novo valor.

        Contador não desce abaixo de zero: `total_entries`/`total_exits` são
        contagens de evento, e um valor negativo na telemetria é sempre bug de
        quem chamou, não informação.
        """
        key = str(name)
        value = self._counters.get(key, 0) + int(amount)
        self._counters[key] = max(0, value)
        return self._counters[key]

    def counters(self) -> Dict[str, int]:
        """Cópia dos contadores (mutar o retorno não afeta a sessão)."""
        return dict(self._counters)

    def reset(self) -> Dict[str, int]:
        """Zera os contadores de sessão e devolve o estado zerado.

        "Reset" aqui é **memória**, não histórico: o JSONL append-only não é
        tocado, e o namespace (`source_id`, `epoch`) permanece — quem chama
        continua sabendo de qual sessão são as amostras seguintes. As chaves
        existentes são preservadas (inclusive as criadas em runtime), para o
        formato da telemetria não mudar no meio da sessão.
        """
        for name in self._counters:
            self._counters[name] = 0
        return self.counters()

    # -- diagnóstico -------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - conveniência de debug
        return (
            f"SessionState(source_id={self.source_id!r}, epoch={self.epoch!r}, "
            f"counters={self._counters!r})"
        )

    def as_dict(self) -> Dict[str, Any]:
        """Namespace + contadores, para relatório/diagnóstico.

        A telemetria de produção usa `namespace()` e `counters()` separados
        (o formato do JSONL é contrato); isto existe para quem precisa do
        objeto inteiro de uma vez, como o futuro `/api/sources` (E4-T1).
        """
        return {**self.namespace(), **self.counters()}
