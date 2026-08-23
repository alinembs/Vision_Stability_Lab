# Fase 5 — Avaliação de Estabilidade (em andamento)

Última atualização: 2026-08-23 19:55 (início do longrun)

## Matriz de testes e status

| Teste | Duração | Status | Resultado | Evidências (`data/logs/phase5/`) |
|---|---|---|---|---|
| Baseline | 30 min | ✅ Concluído | APROVADO | `baseline-summary.json` / `-report.md` / `-tegrastats.log` |
| Reinício controlado | 3 ciclos | ✅ Concluído | APROVADO | `restart_cycles-summary.json` |
| Dashboard desligado | 1 h | ✅ Concluído | APROVADO | `dashboard_off-*` |
| Viewer lento | 1 h | ✅ Concluído | APROVADO | `viewer_slow-*` |
| RTSP interrompido | 30 min | ✅ Concluído | APROVADO (com ressalvas) | `rtsp_interrupt-*` |
| **Longa duração** | **6 h** | 🔄 **Em execução** (19:54 → ~01:54) | — | `longrun-run.log`, summary ao terminar |
| Estresse (alta ocupação) | 2 h | ⏸ Standby | Aguarda cena com várias pessoas | — |

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

### DIV-002 — UPS para o Jetson (recomendação)
Duas quedas de energia no mesmo dia (INC-005 e uma segunda às ~16:30,
sem perda de dados — compose reergueu tudo). Auto-recuperação provou-se
eficaz, mas UPS eliminaria os ciclos de reboot.

## Pendências para a próxima sessão

1. Coletar resultado do longrun (terminou às ~01:54):
   `data/logs/phase5/longrun-summary.json` deve existir; se o processo
   morreu, regenerar com:
   `python3 -B -c "import sys; sys.path.insert(0,'scripts'); from phase5_run import build_summary, write_report; import json; s=build_summary('longrun','2026-08-23T19:54:36-03:00','2026-08-24T01:54:36-03:00'); open('data/logs/phase5/longrun-summary.json','w').write(json.dumps(s,indent=2)); write_report(s)"` 
2. Analisar longrun esperando ~12 fps (noite) — comparar RSS inicial vs final (leak?)
3. Agendar/executar teste de estresse quando houver várias pessoas na cena
4. Fechar avaliação da fase em `docs/phase5-evaluation.md` com os resultados finais
5. Backlog: ACH-001 (config câmera?), ACH-002 (watchdog RTSP), DIV-002 (UPS)

## Como reproduzir os testes

```bash
python3 scripts/phase5_run.py baseline          # 30 min
python3 scripts/phase5_run.py restart_cycles    # 3 ciclos docker restart
python3 scripts/phase5_run.py dashboard_off     # 1 h (para o dashboard)
python3 scripts/phase5_run.py viewer_slow       # 1 h (cliente RTSP lento)
# rtsp_interrupt: desconexão física do cabo (coordenada por horário)
# estresse: standby até ter várias pessoas na cena
```
