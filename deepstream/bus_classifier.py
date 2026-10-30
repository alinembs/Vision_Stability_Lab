"""Decisão pura sobre mensagens de erro/EOS do bus do GStreamer.

Fica separado de `pipeline_real.py` de propósito: aquele módulo exige `gi`,
`pyds` e um ambiente DeepStream completo só para ser importado, e é justamente
a decisão "o que fazer com esta mensagem" que precisa de teste rápido e em
bancada sem GPU.

Motivação (E-A, `docs/baselines/2026-10-30-e1t2-baseline.md`): o ramo de
preview é declarado descartável no cabeçalho de `pipeline_real.py`, mas
qualquer `Gst.MessageType.ERROR` — inclusive o de `rtspclientsink` quando o
MediaMTX reinicia — encerrava o pipeline inteiro e a análise morria junto,
com `State.ExitCode = 0`.

Motivação (E2, 2026-10-30): erro do **`rtspsrc`** também reconecta. Com a
fonte fora do ar o `rtspsrc` não publica EOS, publica ERROR
(`Could not open resource for reading (5)` — o MediaMTX recusa uma sessão para
um path sem publisher). Classificado como "erro da análise" o processo saía
com `exit 1` e o `restart: unless-stopped` escondia o ciclo num crash loop,
sem gastar o orçamento de reconexões e sem chegar ao `exit 2`
(`EXIT_SOURCE_LOST`) que o runbook documenta.
"""

from __future__ import annotations

import re

# Ações possíveis. Quem executa é o pipeline; aqui só se decide.
ACTION_QUIT = "quit"                    # erro do ramo de análise: encerra (exit != 0)
ACTION_RECONNECT = "reconnect"          # fim de stream da fonte: reconecta o rtspsrc
ACTION_DROP_PREVIEW = "drop_preview"    # erro no ramo descartável: derruba só ele
ACTION_IGNORE = "ignore"                # mensagem sem ação (warning/info/desconhecida)

MSG_ERROR = "error"
MSG_EOS = "eos"

# Elementos que só existem no ramo de preview, para os transportes suportados
# por `pipeline_real.py` (rtsp via rtspclientsink e udp via rtph264pay).
#
# `queue` está FORA de propósito: o ramo de análise também tem um, e classificar
# o erro da fila da análise como "descartável" esconderia uma análise quebrada.
# O pipeline passa o conjunto exato de nomes que construiu (`queue_preview`
# incluída), então este conjunto só vale como default para testes e para
# chamadas sem contexto.
PREVIEW_ELEMENTS = frozenset({
    "rtspclientsink",
    "rtph264pay",
    "udpsink",
    "nvv4l2h264enc",
    "h264parse",
    "nvvideoconvert",
    "capsfilter",
})

_TRAILING_INDEX = re.compile(r"\d+$")

# Elemento da fonte. Só ele: erro de depay/parser/decoder/mux é mídia quebrada
# de verdade (não se resolve reconectando o `rtspsrc`) e continua encerrando.
SOURCE_ELEMENTS = frozenset({"rtspsrc"})


def base_element_name(element_name: str) -> str:
    """Remove o índice que o GStreamer acrescenta: `rtspclientsink0` -> `rtspclientsink`."""
    return _TRAILING_INDEX.sub("", element_name or "")


def is_source_element(src_name: str) -> bool:
    """True se o elemento de origem é a própria fonte (`rtspsrc`)."""
    return base_element_name(src_name or "") in SOURCE_ELEMENTS


def _preview_names(preview_elements) -> frozenset[str]:
    return PREVIEW_ELEMENTS if preview_elements is None else frozenset(preview_elements)


def is_preview_element(src_name: str, preview_elements=None) -> bool:
    """True se o elemento de origem pertence ao ramo de preview."""
    names = _preview_names(preview_elements)
    name = src_name or ""
    return name in names or base_element_name(name) in names


def _element_from_text(error_message: str, names: frozenset[str]) -> str:
    """Última tentativa quando `src` não identifica o elemento: o texto do erro
    costuma nomeá-lo ("rtspclientsink0: Could not connect to server")."""
    text = (error_message or "").lower()
    for name in sorted(names):
        if name in text:
            return name
    return ""


def classify_bus_message(message_type: str, src_name: str = "",
                         error_message: str = "",
                         preview_elements=None) -> str:
    """Decide a ação para uma mensagem do bus.

    Args:
        message_type: `MSG_ERROR` ou `MSG_EOS` (outros valores -> `ACTION_IGNORE`).
        src_name: nome do elemento que publicou a mensagem (`message.src`).
        error_message: texto do erro; usado só quando `src_name` é inútil.
        preview_elements: substitui `PREVIEW_ELEMENTS`. O pipeline passa os
            nomes exatos do ramo que construiu.

    Returns:
        Uma das constantes `ACTION_*`.
    """
    kind = (message_type or "").lower()
    if kind not in (MSG_ERROR, MSG_EOS):
        return ACTION_IGNORE

    source = src_name or ""
    if not source:
        source = _element_from_text(error_message, _preview_names(preview_elements))

    if is_preview_element(source, preview_elements):
        return ACTION_DROP_PREVIEW

    if kind == MSG_EOS:
        # Fim de stream da fonte: o processo não deve morrer, a fonte reconecta
        # pelo mesmo caminho usado para inatividade (RTSP_IDLE_RECONNECT_S).
        return ACTION_RECONNECT

    if is_source_element(source):
        # Fonte indisponível (não EOS): mesmo caminho do EOS — o orçamento de
        # `SourceReconnectBudget` decide quando declarar a fonte perdida
        # (exit 2), em vez de sair com exit 1 e ser mascarado pelo restart.
        return ACTION_RECONNECT

    # Ramo de análise (ou elemento que não dá para provar ser descartável):
    # comportamento conservador — encerra com código de erro.
    return ACTION_QUIT


def classify_bus_error(src_name: str, error_message: str = "",
                       preview_elements=None) -> str:
    """Atalho para `Gst.MessageType.ERROR`."""
    return classify_bus_message(MSG_ERROR, src_name, error_message,
                                preview_elements)
