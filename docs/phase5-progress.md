# Fase 5 — Avaliação de Estabilidade (em andamento)

Última atualização: 2026-08-24 10:00 (estresse cancelado — sem ocupação; refazer depois)

## Matriz de testes e status

| Teste | Duração | Status | Resultado | Evidências (`data/logs/phase5/`) |
|---|---|---|---|---|
| Baseline | 30 min | ✅ Concluído | APROVADO | `baseline-summary.json` / `-report.md` / `-tegrastats.log` |
| Reinício controlado | 3 ciclos | ✅ Concluído | APROVADO | `restart_cycles-summary.json` |
| Dashboard desligado | 1 h | ✅ Concluído | APROVADO | `dashboard_off-*` |
| Viewer lento | 1 h | ✅ Concluído | APROVADO | `viewer_slow-*` |
| RTSP interrompido | 30 min | ✅ Concluído | APROVADO (com ressalvas) | `rtsp_interrupt-*` |
| **Longa duração** | **6 h** | ✅ **Concluído** (19:54 → 01:54) | **APROVADO** | `longrun-summary.json` / `-report.md` / `-run.log` / `-tegrastats.log` |
| Estresse (alta ocupação) | 2 h | ⏸ Standby | Iniciado 09:55 e cancelado (cena vazia); refazer com alta ocupação | — |

## Resultados consolidados

### Baseline (30 min, tarde, câmera a ~20 fps)
- FPS processado: avg 19.83 · p95 19.97 · min 17.0
- RSS: estável após warm-up (782 → 891 MB, plano)
- Filas do tee: sempre 0 buffers · preview drops: máx 0.04/s
- Temp GPU/TJ: ~67 °C · Potência: ~12.5 W médios
- 0 erros GStreamer/CUDA · 0 reconexões

### Reinício controlado (3 ciclos `docker restart`)
- Recuperação até primeira métrica saudável: 16 s / 46 s / 46 s
- Sem falhas; auto-recuperação do compose + supervisor consistentes

### Dashboard desligado (1 h sem consumidor do dashboard)
- Pipeline intocado: FPS avg 19.84, 0 drops, filas 0
- Telemetria JSONL continuou gravando normalmente

### Viewer lento (1 h com cliente RTSP lento: latency=3000ms, sync=true)
- Impacto zero no pipeline: FPS estável, 0 drops, filas 0
- Sessão confirmada no MediaMTX; perda isolada por consumidor funciona como projetado

### RTSP interrompido (desconexão física do cabo, ~5 min fora)
- Detecção da queda levou **~4 min** (timeout TCP do rtspsrc) — ver Achados
- Supervisor reiniciou o pipeline em ciclos de ~60 s durante a queda (15 tentativas)
- Após religar: vídeo de volta em **<1 min** (pad às 22:21:11 UTC, fps saudável às 22:21:42)
- Zero erros CUDA; filas 0 durante todo o teste

### Longa duração (6 h overnight, 19:54 → 01:54, 720 amostras)
- **Sem leak de memória:** RSS 790 MB (início) → platô ~947 MB após ~1 h,
  estável entre 946–947 MB nas últimas 4 h. Dentro do limiar (+15% ≈ 1025 MB
  sobre o baseline da Fase 3). `memory_growth` final (899.95 MB) é o crescimento
  acumulado desde o start do processo (inclui warm-up), não vazamento ativo.
- FPS p95 = 12.03 — consistente com o modo noturno (ACH-001), NÃO é degradação
- **Novo (ACH-004):** 175/720 amostras (~24%) com quedas intermitentes para
  ~7 fps, em rajadas de 1–10 min espalhadas por toda a noite. Padrão da câmera
  (exposição noturna variável), não do pipeline: RSS plano, filas 0,
  0 erros nas mesmas janelas.
- Recursos: GPU avg 39% · CPU avg 7.7% · Temp TJ avg 58.4 °C (máx 62.5) ·
  Potência avg ~7.9 W · RAM Jetson máx 4.6 GB
- 0 erros GStreamer/CUDA · 0 reconexões · filas do tee sempre 0 ·
  preview drops máx 0.04/s · tracks máx 1

## Achados e pendências

### ACH-001 — FPS da câmera varia com modo noturno (confirmado)
A câmera entrega ~20 fps de dia e reduz para **12 fps à noite** (exposição mais
longa). Timeline de hoje: 19.8 fps até ~18h local, 13.5 no crepúsculo, 12.0 à noite.
Confirmado com `gst-discoverer` (`framerate: 12/1`) e histórico do `fps_in`.
**Implicações:**
- O longrun de 6 h overnight roda inteiro a ~12 fps — NÃO é degradação
- Thresholds de saúde baseados em FPS devem considerar variação dia/noite
- Se o laboratório exigir 20 fps noturno: fixar exposure/WDR na configuração da câmera

### ACH-002 — Detecção lenta de queda de câmera (~4 min)
O rtspsrc leva ~4 min (timeout TCP) para perceber a desconexão. Durante esse
período não há erro nem métrica indicando problema. Melhorias candidatas:
- `tcp-timeout`/`timeout` menores no rtspsrc ou watchdog próprio no probe
- Expor "última mensagem RTP recebida" na telemetria do dashboard
- Registrar como backlog da Fase 5 ou item da Fase 6

### ACH-003 — Comportamento positivo: supervisor em ciclos de 60 s
Durante a queda, o supervisor reiniciou o pipeline a cada ~60 s
(startup → playing → error → stopped), recuperando sozinho quando a câmera
voltou. Sem intervenção manual necessária.

### ACH-004 — Quedas noturnas intermitentes para ~7 fps (novo, longrun)
Durante o longrun de 6 h, ~24% das amostras caíram para ~7 fps (mín 6.27),
em rajadas de 1–10 min distribuídas por toda a noite (ex.: 20:17–20:23,
00:01–00:09). Sem correlação com recursos do pipeline (RSS plano, filas 0,
0 erros). Hipótese: exposição automática da câmera à noite oscilando entre
dois modos. Investigar logs da câmera / fixar exposure na config.

### DIV-002 — UPS para o Jetson (recomendação)
Duas quedas de energia no mesmo dia (INC-005 e uma segunda às ~16:30,
sem perda de dados — compose reergueu tudo). Auto-recuperação provou-se
eficaz, mas UPS eliminaria os ciclos de reboot.

## Pendências para a próxima sessão

- [x] Coletar resultado do longrun (`longrun-summary.json` gerado às 01:54)
- [x] Analisar longrun: sem leak; FPS conforme modo noturno (ACH-001/ACH-004)
- [ ] Agendar/executar teste de estresse quando houver várias pessoas na cena
      (tentativa de 2026-08-24 09:55 cancelada — cena vazia; comando:
      `python3 -B scripts/phase5_run.py estresse`, já suportado pelo harness)
- [ ] Fechar avaliação da fase em `docs/phase5-evaluation.md` com os resultados finais
- [ ] Backlog: ACH-001 (config câmera?), ACH-002 (watchdog RTSP), ACH-004 (oscilação noturna), DIV-002 (UPS)

## Como reproduzir os testes

```bash
python3 scripts/phase5_run.py baseline          # 30 min
python3 scripts/phase5_run.py restart_cycles    # 3 ciclos docker restart
python3 scripts/phase5_run.py dashboard_off     # 1 h (para o dashboard)
python3 scripts/phase5_run.py viewer_slow       # 1 h (cliente RTSP lento)
# rtsp_interrupt: desconexão física do cabo (coordenada por horário)
# estresse: standby até ter várias pessoas na cena
```
