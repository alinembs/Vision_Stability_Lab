"""Guarda da separação decisão/execução do ramo de preview.

`pipeline_real.py` não pode ser importado nestes testes (importa `gi`/`pyds` e
instancia TensorRT), então o invariante é verificado no **texto/AST** do
arquivo — o mesmo recurso de `test_pipeline_source_wiring.py`.

O invariante existe por causa do incidente de 2026-11-01 (thread dump em
`preview_control.py`): `_drop_preview_branch` roda no callback do bus, isto é,
no thread do loop GLib. Chamar `set_state(NULL)` no `rtspclientsink` ali deixou
o loop preso no mutex do elemento quando o MediaMTX estava fora do ar —
`interval_metrics` parou de sair, `SIGTERM` deixou de ser despachado e o
restart do watchdog virou `SIGKILL`. A regra: o callback do bus **só marca
estado e loga**; derrubar/recriar o ramo acontece em thread própria,
despachada pelo timer `_preview_step`.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PIPELINE = (Path(__file__).resolve().parents[1] / "pipeline_real.py")

# Chamadas de GStreamer que não podem aparecer no caminho do callback do bus
# nem no corpo do timer que despacha o trabalho.
TRABALHO_DE_GSTREAMER = ("_detach_preview_branch", "_install_preview_branch",
                         "_make_preview_elements", "set_state", "remove",
                         "release_request_pad", "unlink")


def _funcoes(codigo: str) -> dict:
    arvore = ast.parse(codigo)
    return {no.name: no for no in ast.walk(arvore)
            if isinstance(no, ast.FunctionDef)}


def _chamadas(no) -> set:
    nomes = set()
    for sub in ast.walk(no):
        if isinstance(sub, ast.Call):
            alvo = sub.func
            if isinstance(alvo, ast.Attribute):
                nomes.add(alvo.attr)
            elif isinstance(alvo, ast.Name):
                nomes.add(alvo.id)
    return nomes


class CallbackDoBusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codigo = PIPELINE.read_text(encoding="utf-8")
        self.funcoes = _funcoes(self.codigo)
        self.callback = self.funcoes["_drop_preview_branch"]
        self.chamadas = _chamadas(self.callback)

    def test_callback_do_bus_nao_toca_gstreamer(self) -> None:
        proibidas = self.chamadas & set(TRABALHO_DE_GSTREAMER)
        self.assertFalse(
            proibidas,
            "_drop_preview_branch roda no thread do loop GLib (callback do "
            f"bus) e chamou {sorted(proibidas)} — o trabalho tem de ir para "
            "a thread do worker (ver preview_control.py)")

    def test_callback_do_bus_so_pede_a_derrubada(self) -> None:
        self.assertIn("request_drop", self.chamadas,
                      "_drop_preview_branch deveria só registrar a falha no "
                      "controlador puro (preview_control.request_drop)")


class TimerDoPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codigo = PIPELINE.read_text(encoding="utf-8")
        self.funcoes = _funcoes(self.codigo)
        self.timer = self.funcoes["_preview_step"]
        self.chamadas = _chamadas(self.timer)

    def test_timer_nao_executa_gstreamer_no_loop(self) -> None:
        proibidas = self.chamadas & set(TRABALHO_DE_GSTREAMER)
        self.assertFalse(
            proibidas,
            f"_preview_step roda no loop GLib e chamou {sorted(proibidas)}; "
            "use _start_preview_op para executar em thread própria")

    def test_timer_entrega_o_trabalho_ao_worker(self) -> None:
        self.assertIn("_start_preview_op", self.chamadas)
        self.assertIn("step", self.chamadas,
                      "_preview_step deveria consultar preview_control.step")

    def test_timer_de_configuracao_e_registrado(self) -> None:
        self.assertIn("GLib.timeout_add(PREVIEW_STEP_MS, self._preview_step)",
                      self.codigo,
                      "o timer do preview não está agendado no run(): o "
                      "worker nunca é despachado")


if __name__ == "__main__":
    unittest.main()
