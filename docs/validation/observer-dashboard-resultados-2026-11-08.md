# Resultado da validação — observador de fontes (Jetson, 2026-11-08)

Execução de campo do roteiro `observer-dashboard-roteiro.md` na Jetson
(`192.168.59.129`, repo `~/Vision_Stability_Lab`), com stack real: watchdog,
pipeline DeepStream, dashboard e MediaMTX no ar. Fonte viva do ciclo: `phone`
(Chrome Android por WHIP) no cenário 2 e a `camera` fixa no cenário 4.

## Pré-requisito que precisou de correção antes de começar

A sonda de nível 1 (`scripts/probe/rtsp_describe_probe.py`) enviava a request
line RTSP **relativa** (`OPTIONS /drone RTSP/1.0`). O MediaMTX recusava
(`closed: invalid URL (/drone)`) e a sonda devolvia sempre
`timeout_no_options`; o observador nunca via fonte viva e ficava preso em
`standby_no_sources`. Corrigido em `acbd1b5` (URL absoluta na request line, sem
userinfo, porta explícita, IPv6 entre colchetes) e provado em campo:

```
$ docker exec vision-watchdog python3 /app/probe/rtsp_describe_probe.py --url rtsp://mediamtx:8554/drone
{"status":"404","alive":false,"code":404,"reason":"rtsp_404","elapsed_s":0.005}
```

Antes: `invalid URL` no log do MediaMTX e `timeout_no_options`. Depois:
`no stream is available on path 'drone'` — o servidor responde RTSP de verdade.

Estado de partida: watchdog `Up` com a imagem reconstruída, pipeline `Exited`,
`mode: standby_no_sources`, `active_source: phone`, câmera fora da LAN.

---

## Cenário 2 — fonte volta, app sobe e confirma frame: **PASSOU**

Executado primeiro porque o celular entrou no ar com o stack em standby.

| Instante (UTC) | Evento | Campos |
|---|---|---|
| 16:32:07 | `watchdog_source_probe_changed` | `source: phone`, `alive: true`, `previous_alive: false`, `level: 1`, `ok_streak: 1` |
| 16:32:46 | `watchdog_docker_start` | `status: 204`, `ok: true` |
| 16:32:46 | `watchdog_standby_exited` | `mode: switching`, `reason: fonte_viva_em_standby`, `source: phone` |
| 16:33:26 | `watchdog_engage_ok` | `source: phone`, `fps_in: 20.32`, `elapsed_s: 51.3` |

`switch.jsonl`: `{"from":"phone","to":"phone","probe":"ok","result":"PASS","reason":"fonte_confirmada","origin":"observer"}`.

Estado depois: `mode: watching`, `mode_reason: fonte_confirmada`,
`last_action: engage_ok`, `app_container_running: true`, `active_source: phone`,
`sources_probe.phone` com `alive: true`, `level: 2`, `reason: ok`.

Dashboard: `.observer` `available: true`, `watching`, `age_s: 2.8`;
`.latest_metrics` `source_id: phone`, `fps_in: 20.32`, `rss_mb: 1157`;
`/api/sources` com `phone` `active: true`.

Aceite conferido: `standby_exited` antes do `engage_ok`, `fps_in > 0` da fonte
certa, retorno a `watching` dentro de 90 s do `docker_start` (51,3 s). Também
prova que a sonda de nível 2 (`rtsp_read_probe.py` com `gi`/Gst/`h264parse`)
funciona dentro da imagem do watchdog.

---

## Cenário 1 — fonte cai, app para (standby): **PASSOU (fora da janela de 110 s)**

Ação manual: publicação do celular encerrada. Para o cenário ser reproduzível
com a câmera viva, a precedência do observador foi restrita ao path sob teste
(`OBSERVER_SOURCE_PRIORITY=phone`, watchdog recriado com `--no-deps`).

| Instante (UTC) | Evento | Campos |
|---|---|---|
| 16:46:24 → 16:47:36 | `rtsp_reconnect_started` | `attempt` 2, 3, 4, 5 de `limit: 5`, `reason: bus_error` |
| 16:48:22 | `pipeline_failed` | `reason: source_reconnect_exhausted`, `exit_code: 2`, `attempts: 5` |
| 16:48:22 | `pipeline_stopped` | `exit_code: 2` |
| 16:48:25 | `watchdog_container_restarted` | `expected_by_switch: false` (restart da política do Docker) |
| 16:48:26 | `watchdog_docker_stop` | `status: 204`, `ok: true` |
| 16:48:26 | `watchdog_standby_entered` | `previous_mode: watching`, `reason: sem_fonte_viva`, `exit_code: 0` |

Estado: `mode: standby_no_sources`, `mode_reason: sem_fonte_viva`,
`last_action: stop_standby`, `app_container_running: false`,
`active_source: phone`. Docker: `exited`, `RestartCount 1`,
`RestartPolicy unless-stopped`, `ExitCode 143`.

`watchdog_restart_triggered` **não** apareceu na janela: o observador parou o
container antes da escalada de 180 s da Regra 1.

**Divergência medida:** o roteiro promete `standby_no_sources` em até 110 s; o
run levou ~130 s (queda percebida às 16:46:1x, standby às 16:48:26). A causa é
o backoff entre tentativas de reconexão, que cresce (11 s, 20 s, 41 s, 46 s),
não os 10 s fixos que o cálculo de 5 × 10 s supõe. O `pipeline_failed` com
`exit_code: 2` continua sendo a prova do motivo, e o `app_exit_code` do estado
(oscila entre `0` e `143`) não serve para isso — como o próprio roteiro avisa.

---

## Cenário 3 — sem fonte, container fica parado: **PASSOU**

6 amostras, 60 s de intervalo, com o celular desligado e a precedência ainda
restrita a `phone` (a câmera viva não podia religar o pipeline):

| Amostra | Hora (UTC) | RestartCount | Status | ExitCode | Policy | `state.ts` | `mode` |
|---|---|---|---|---|---|---|---|
| 1 | 16:48:43 | 1 | exited | 143 | unless-stopped | 16:48:36 | standby_no_sources |
| 2 | 16:49:43 | 1 | exited | 143 | unless-stopped | 16:49:36 | standby_no_sources |
| 3 | 16:50:43 | 1 | exited | 143 | unless-stopped | 16:50:36 | standby_no_sources |
| 4 | 16:51:43 | 1 | exited | 143 | unless-stopped | 16:51:36 | standby_no_sources |
| 5 | 16:52:43 | 1 | exited | 143 | unless-stopped | 16:52:36 | standby_no_sources |
| 6 | 16:53:43 | 1 | exited | 143 | unless-stopped | 16:53:36 | standby_no_sources |

`RestartCount` idêntico nas 6 amostras (o único restart é o da política logo
após o `exit_code=2`, antes do `docker stop`), `State.Status` `exited`,
`HostConfig.RestartPolicy.Name` `unless-stopped`, nenhum
`watchdog_docker_start`/`auto_switch_triggered`/`restart_triggered` na janela.
O `ts` do `observer_state.json` avançou a cada ciclo de 10 s (prova de watchdog
vivo) e o `last_action_ts` ficou congelado em `16:48:25` (a ação de parada), como
o roteiro prevê.

---

## Cenário 4 — troca manual durante o standby: **PASSOU**

Pré-estado: app `exited`, `mode: standby_no_sources`, `active_source: phone`,
precedência restrita a `phone` (para o observador não subir sozinho e roubar a
premissa), fonte de destino `camera` viva — confirmada por
`docker exec vision-watchdog python3 /app/probe/rtsp_describe_probe.py` →
`status: 200`, `alive: true`, `has_video: true`.

```
$ ./scripts/switch_source.sh camera
[switch] pré-check: lendo rtsp://***@192.168.59.108:5543/live/channel0 por 5s
[switch] pré-check PASS: a fonte está entregando vídeo
[switch] data/config/active_source = camera
[switch] container recriado: started_at=2026-11-08T16:53:58.889523585Z
PASS source=camera first_frame_in=36.6s fps_in=19.5 codec=h265 precheck=ok session_epoch=2026-11-08T16:54:00.466+00:00
EXIT=0
```

Depois: `active_source` = `camera`, `switch_in_progress` ausente, container
`running` com `RestartCount 0`, `switch.jsonl` com
`{"from":"phone","to":"camera","probe":"ok","result":"PASS","reason":"ok"}`.
O watchdog emitiu **um** `switch_in_progress_observed`
(`to: camera`, `ttl_s: 140.4`) e nenhum `auto_switch_triggered`.

Estado logo após: `mode: standby_no_sources` ainda,
`app_container_running: true`, `active_source: camera` — exatamente o caso que o
roteiro documenta (não usar `mode` como critério do cenário). Dashboard:
`/api/sources` com `camera` `active: true` e `observer_probe.status: measured`;
`.latest_metrics` `source_id: camera`, `fps_in: 19.5`.

---

## Reset e estado final

`docker compose up -d --no-deps watchdog` sem `OBSERVER_SOURCE_PRIORITY`
(devolve `camera,drone,phone`). Em 45 s o observador passou a `watching`:

```
state: {ts: 16:55:36, mode: watching, app_container_running: true, active_source: camera}
probes: camera (True, level 1, ok) · drone (False, rtsp_404) · phone (False, rtsp_404)
dashboard: available True, watching, age_s 4.9 · latest_metrics camera fps_in 19.9
containers: vision-watchdog Up · vision-stability-app Up (healthy)
```

Nenhum marcador órfão em `data/config/switch_in_progress`; memória disponível
~5,2 GB com o pipeline no ar.

## Resumo

| Cenário | Resultado | Observação |
|---|---|---|
| 2 — fonte volta | PASSOU | `engage_ok` em 51,3 s, `fps_in 20.32`, nível 2 confirmado |
| 1 — fonte cai | PASSOU com ressalva | standby em ~130 s, não em 110 s (backoff de reconexão crescente) |
| 3 — sem loop | PASSOU | `RestartCount` estável em 1 por 5 min, `ts` avançando |
| 4 — troca manual no standby | PASSOU | `PASS` exit 0, `first_frame_in 36.6 s`, um único marcador observado |

Pendências de ajuste sugeridas pelo que foi medido:

1. Revisar a janela de 110 s do cenário 1 no roteiro (ou medir o backoff real de
   `SOURCE_RETRY_S`) — o valor observado foi ~130 s.
2. `rtmp_publisher.sh` não roda nesta Jetson (`nvv4l2h264enc` sem
   `libnvidia-encode.so.1` no loader do host); o publisher usado na bancada foi
   `gst-launch` com `x264enc` (`tune=zerolatency`, `bframes=0`). O script de
   referência precisa de fallback.
