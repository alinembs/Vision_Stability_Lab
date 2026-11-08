# Baseline limpo — E1-T2 (2026-10-30, segunda medição)

Baseline **válido** de uma fonte com o stack em container, medido depois das
correções de estabilidade (watchdog sem falso positivo, preview isolado, EOS
reconectando). Substitui a janela de 15:47–16:01, que continha um restart
espúrio do watchdog e está marcada como inválida no documento anterior.

- **Série bruta:** `docs/baselines/2026-10-30-e1t2-baseline-limpo.jsonl` (15 amostras, 60 s)
- **Instrumento:** `scripts/mem_budget.sh`
- **Janela:** 2026-10-30T19:20:25Z → 19:34:57Z (14,5 min)
- **Host:** Jetson Orin, L4T R36.4.7, 7619 MB de RAM unificada

## Ambiente

| Item | Valor |
|---|---|
| Fonte | câmera fixa `192.168.59.108:5543/live/channel0` (H.265) |
| Pipeline | `fps_in` 19,97–20,03 · `fps_processed` 19,97–20,03 (**100%**) |
| Containers | `vision-stability-app`, `vision-mediamtx` (TLS), `vision-capture-web`, `vision-dashboard`, `vision-watchdog` |
| Stack arquo | parado (baseline comparável) |
| Imagem | `deploy-vision-app:latest` (DeepStream 7.1 L4T) |

## Resultados

| Métrica | Medido | Gate | |
|---|---|---|---|
| RSS do pipeline | **947 MB constante** (15/15 amostras, amplitude 0 MB) | 750–1050 MB | ✅ |
| RAM do sistema | **47,4–48,5%** | ≤ 70% | ✅ |
| `OOMKilled` / exit 137 | `false` / `0` | nenhum | ✅ |
| Restarts do container | **0** | 0 | ✅ |
| Containers DeepStream | 1 | exatamente 1 | ✅ |
| Idade da telemetria | ≤ 29 s | < 180 s | ✅ |
| Eventos de watchdog na janela | **nenhum** (`no_frames_detected`, `restart_triggered`, `crash_loop_detected`) | nenhum | ✅ |
| `lfb` livre | 8–16 MB | relativo (ver abaixo) | ✅ |

RSS idêntico em todas as amostras é o comportamento esperado de um pipeline
estável — não é leitura travada: o próprio pipeline reporta `rss_mb: 944,05` na
telemetria (`watchdog_rss_baseline_set`), coerente com os 947 MB medidos por
fora.

## Gate de `lfb` — correção definitiva

`lfb` de 8–16 MB **não é sinal de problema** nesta máquina: após reboot limpo,
sem carga e com 32% de RAM, ele já mede 16 MB. O gate absoluto `≥ 400 MB`
previsto na primeira versão do plano era inválido e foi removido.

O gate adotado é **relativo**: `lfb_ref` = mediana pós-warm-up; alerta só quando
`lfb` cai a < 50% de `lfb_ref` por 3 amostras **e** o RSS está subindo. Isolado,
`lfb` baixo não significa nada aqui.

## Faixa de fps da fonte

Durante o dia a câmera entregou **~20 fps** de forma estável (esta janela,
100% processado) e caiu a **7,0 fps cravados** entre ~18:03 e ~19:00, voltando
sozinha a 20 fps depois. O watchdog emitiu `fps_degradation` nesse intervalo.

Conclusão: **a fonte varia por condição própria** (luz/encoder), não por gargalo
do Jetson — o pipeline processou 100% do que recebeu nas duas situações. Por
isso o limiar de degradação foi reescrito no plano para "60% do alvo **da
fonte**" (baseline móvel), e não "30% de um `TARGET_FPS=25` fixo" que a câmera
nunca entrega. Isso é a Etapa 5.

## Conclusão

Baseline **válido e dentro de todos os gates**. O ambiente está estável o
suficiente para começar a Etapa 2 (config declarativa de fontes e troca de
fonte por comando).
