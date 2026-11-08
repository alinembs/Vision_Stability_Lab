# Roteiro de validação — observador de fontes na Jetson

Validação de campo (host Jetson, stack real) do observador que o
`scripts/watchdog.py` executa a partir de `scripts/observer_policy.py`: parar o
pipeline quando não há fonte, religar quando ela volta, não entrar em loop de
restart e conviver com a troca manual a partir do standby. O painel do
dashboard (`#panel-observador`, tarefas 1–3 do plano
`docs/plans/2026-11-08-frontend-observador-vsl.md`) é verificado apenas pelo
que ele consome da API.

## Pré-requisitos

| Item | Como conferir |
|---|---|
| Stack no ar (`vision-app`, `vision-watchdog`, `vision-dashboard`, `vision-mediamtx`) | `make status` |
| **Um** container DeepStream do projeto (regra global: nunca um segundo) | `docker ps -a --filter name=vision-stability-app` |
| `jq` e `docker` no host | `jq --version && docker version --format '{{.Server.Version}}'` |
| Acesso ao host Jetson (para desligar/religar a fonte: passo manual) | — |
| `ACTIVE_SOURCE` vazio no watchdog e no app (senão a política não decide) | `docker exec vision-watchdog printenv ACTIVE_SOURCE` (vazio) |
| Troca automática ligada | `docker exec vision-watchdog printenv OBSERVER_AUTO_SWITCH` → `1` |
| `data/` do host é o volume visto pelos containers | `ls data/config data/logs` |

Duração estimada: **50–70 min**, sendo ~5 min só de observação no cenário 3.

Não coberto aqui:

- troca automática entre **duas** fontes vivas (`SWITCH_TO`), preempção por
  prioridade e o cooldown de 180 s (`SWITCH_COOLDOWN_S`) — outro roteiro;
- renderização visual do painel (`#panel-observador`, lâmpada `observador`) e
  seus breakpoints;
- internals das sondas (`scripts/probe/rtsp_describe_probe.py`,
  `rtsp_read_probe.py`) e o pós-check pela tela (`POST /api/source/switch`);
- montagem do MediaMTX, publicação RTMP do drone real e captura por WHIP
  (ver `docs/runbooks/drone-campo.md` e `docs/runbooks/captura-celular.md`);
- soak/long-run e consumo de memória do standby.

## Como ler as provas

Host = raiz do repositório (`~/Documents/PROJETOS/Vision_Stability_Lab`).

| Onde | O quê |
|---|---|
| `data/logs/watchdog.jsonl` | eventos do watchdog — **todos** com prefixo `watchdog_` (o `emit()` monta `"event": "watchdog_<nome>"`; por isso o evento de partida é `watchdog_watchdog_started`) |
| `data/logs/live.jsonl` | telemetria do pipeline (`interval_metrics`, `pipeline_failed`, `pipeline_stopped`) |
| `data/config/observer_state.json` | retrato do observador, reescrito a cada ciclo de 10 s |
| `data/config/active_source` | fonte ativa pedida ao pipeline (uma linha) |
| `data/config/switch_in_progress` | mutex de troca (existe só durante a troca) |
| `GET /api/snapshot` → `.observer` | `available`, `mode`, `mode_reason`, `last_action`, `last_action_ts`, `ts`, `age_s`, `sources_probe`, `reason` |
| `GET /api/sources` → `observer_probe` | `status: measured \| sem observador \| nao_sondada` (+ `alive`, `level`, `reason`, `checked_at` quando `measured`) |

Leituras usadas em todos os cenários:

```bash
# eventos de transição, ao vivo
tail -f data/logs/watchdog.jsonl | grep -E 'watchdog_(standby_entered|standby_exited|engage_ok|engage_failed|docker_stop|docker_start|source_probe_changed|switch_in_progress_observed)'

# último evento de cada tipo (JSONL: uma linha por evento)
jq -c 'select(.event=="watchdog_standby_entered")' data/logs/watchdog.jsonl | tail -1
jq -c 'select(.event=="watchdog_engage_ok")' data/logs/watchdog.jsonl | tail -1

# retrato do observador (arquivo cru — campos que o dashboard NÃO expõe)
jq '{mode, mode_reason, app_container_running, app_exit_code, active_source, last_action, last_action_ts, sources_probe}' data/config/observer_state.json

# dashboard
curl -fsS http://localhost:8081/api/snapshot | jq '.observer'
curl -fsS http://localhost:8081/api/snapshot | jq '.latest_metrics | {source_id, fps_in, ts}'
curl -fsS http://localhost:8081/api/sources  | jq '.[] | {id, active, observer_probe}'
```

`make health` mostra só o começo do snapshot; para o observador use o `jq`
acima. `make sources` é o atalho para `/api/sources`.

Limiares que dão o ritmo de todos os cenários (valores do código):

| Constante | Valor | Efeito prático |
|---|---|---|
| `CHECK_INTERVAL_S` | 10 s | um ciclo de decisão do watchdog |
| `ACTIVE_FRAME_TIMEOUT_S` | 90 s | fonte ativa declarada morta sem frame (com DESCRIBE falho) |
| `MIN_OK_STREAK` | 2 | duas sondas DESCRIBE seguidas para a fonte ser candidata |
| `LEVEL2_PROBE_COOLDOWN_S` | 180 s | intervalo mínimo da sonda de mídia por fonte |
| `OBSERVER_WAKE_TIMEOUT_S` | 90 s | orçamento do pós-check depois do start/restart |
| `OBSERVER_SWITCH_MARKER_TTL_S` | 150 s | validade do marcador que o observador cria |
| `OBSERVER_STATE_MAX_AGE_S` | 120 s | acima disso o dashboard responde `available: false` |
| `RULE1_FRAME_TIMEOUT_S` / `RULE1_ESCALATE_S` | 60 s / 180 s | `no_frames_detected` / restart da Regra 1 |
| `SOURCE_RECONNECT_MAX_ATTEMPTS` × `SOURCE_RETRY_S` | 5 × 10 s | ~50 s até `source_reconnect_exhausted` |

## Preparação comum

A câmera fixa (`camera`) é a fonte sempre viva do laboratório. Para conseguir o
standby de forma reproduzível, o cenário 1 usa o `drone` e **restringe a
precedência do observador ao path sob teste** — com a câmera na lista o
observador trocaria para ela (`SWITCH_TO`) em vez de dormir.

```bash
# terminal 1 — publisher sintético do drone (foreground; Ctrl-C é a "queda da fonte")
./scripts/probe/rtmp_publisher.sh --path drone

# terminal 2 — pipeline no drone e observador olhando só o drone
make switch SOURCE=drone
OBSERVER_SOURCE_PRIORITY=drone make up

# confirmação da configuração que o watchdog carregou
docker exec vision-watchdog printenv OBSERVER_SOURCE_PRIORITY   # drone
jq -c 'select(.event=="watchdog_watchdog_started") | .observer' data/logs/watchdog.jsonl | tail -1
```

`make switch SOURCE=drone` precisa de `PASS source=drone ... fps_in=...` e
exit 0; se falhar, pare aqui e veja "falhas comuns".

---

## Cenário 1 — fonte cai, app para (standby)

**Objetivo:** sem fonte, o pipeline não reinicia em vazio: o observador o para e
assume `standby_no_sources`.

**Passo manual:** no terminal 1, `Ctrl-C` no `rtmp_publisher.sh` (equivale a derrubar o drone).

### Comando

```bash
# com o publisher morto, ainda no terminal 2
tail -f data/logs/watchdog.jsonl | grep -E 'watchdog_(docker_stop|standby_entered|standby_stop_failed|no_frames_detected|source_probe_changed)'
```

### Prova no watchdog.jsonl

| Evento | Campos que importam |
|---|---|
| `watchdog_no_frames_detected` (pode aparecer) | `idle_s` ≥ 60 |
| `watchdog_docker_stop` | `container: "vision-stability-app"`, `status` (204 = parou agora; 304 = já estava parado), `ok: true` |
| `watchdog_standby_entered` | `previous_mode: "watching"`, `reason: "sem_fonte_viva"`, `exit_code` |

Prova do motivo no pipeline (o `exit_code=2` nasce aqui, não no estado do
observador):

```bash
jq -c 'select(.event=="pipeline_failed")' data/logs/live.jsonl | tail -1
# {"event":"pipeline_failed","reason":"source_reconnect_exhausted","exit_code":2,...}
```

### Prova no observer_state.json

```bash
jq '{mode, mode_reason, app_container_running, app_exit_code, active_source, last_action}' data/config/observer_state.json
```

| Campo | Valor esperado |
|---|---|
| `mode` | `"standby_no_sources"` |
| `mode_reason` | `"sem_fonte_viva"` (ou `"pos_check_timeout"`, se veio de falha de religamento) |
| `app_container_running` | `false` |
| `active_source` | `"drone"` |
| `last_action` | `"stop_standby"` |
| `app_exit_code` | **não é prova estável de `2`**: é o `State.ExitCode` da última saída. Vale `2` se o observador viu o crash antes de a política de restart religar; vale `0`/`143` quando o próprio observador parou um container que estava rodando. O `2` do crash está no `live.jsonl` acima |

Docker:

```bash
docker ps -a --filter name=vision-stability-app --format 'table {{.Names}}\t{{.Status}}'
docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' vision-stability-app
# exited 0 (parada graciosa do pipeline; 143 se o handler de SIGTERM não pegar)
```

### Prova no dashboard

```bash
curl -fsS http://localhost:8081/api/snapshot | jq '.observer | {available, mode, mode_reason, age_s}'
curl -fsS http://localhost:8081/api/sources  | jq '.[] | select(.id=="drone") | .observer_probe'
```

Esperado: `available: true`, `mode: "standby_no_sources"`, `age_s < 120`;
`observer_probe` do drone `status: "measured"` com `alive: false` (a sonda nível
1 continua rodando no standby).

### Aceite

- `mode: standby_no_sources` em até **110 s** após derrubar a fonte;
- container `vision-stability-app` com `Status` `exited` e `docker ps` (sem
  `-a`) não o lista;
- `watchdog_restart_triggered` **não** aparece nessa janela (o observador parou
  antes da escalada de 180 s da Regra 1).

### Tempos esperados

| Etapa | Tempo | Base |
|---|---|---|
| reconnects esgotam e o processo sai 2 | ~50 s | 5 tentativas × 10 s |
| `watchdog_no_frames_detected` | 60 s, se o processo ainda estiver de pé | `RULE1_FRAME_TIMEOUT_S` |
| stop + `standby_entered` | 50–110 s após a queda | ciclo de 10 s / timeout de frame de 90 s |
| estado confiável no dashboard | a cada ciclo de 10 s (`age_s` < 120) | `OBSERVER_STATE_MAX_AGE_S` |

---

## Cenário 2 — fonte volta, app sobe e confirma frame

**Objetivo:** sair do standby somente com mídia confirmada (nível 2), religar o
container parado e voltar a `watching` com `fps_in > 0` da fonte certa.

**Passo manual:** religar a fonte — no terminal 1, executar de novo
`./scripts/probe/rtmp_publisher.sh --path drone`.

### Comando

```bash
tail -f data/logs/watchdog.jsonl | grep -E 'watchdog_(source_probe_changed|docker_start|standby_exited|engage_ok|engage_failed)'
```

### Prova no watchdog.jsonl

| Ordem | Evento | Campos que importam |
|---|---|---|
| 1 | `watchdog_source_probe_changed` | `source: "drone"`, `alive: true`, `previous_alive: false`, `level: 1`, `ok_streak: 1` |
| 2 | `watchdog_docker_start` | `container: "vision-stability-app"`, `status: 204` (ou 304, se já subiu), `ok: true` |
| 3 | `watchdog_standby_exited` | `mode: "switching"`, `reason: "fonte_viva_em_standby"`, `source: "drone"`, `previous_source`, `deadline` |
| 4 | `watchdog_engage_ok` | `source: "drone"`, `previous_source`, `fps_in` > 0, `elapsed_s`, `action: "fonte confirmada com frame"` |

A subida de nível (1 → 2) **não** gera evento: `source_probe_changed` só sai na
transição `alive` ↔ `dead`. A prova do nível 2 está no estado e no dashboard.
O `data/logs/switch.jsonl` ganha uma linha `event: "switch_source"`,
`result: "PASS"`, `reason: "fonte_confirmada"`, `origin: "observer"`.

### Prova no observer_state.json

```bash
jq '{mode, mode_reason, app_container_running, active_source, last_action, probe: .sources_probe.drone}' data/config/observer_state.json
```

| Momento | Campos |
|---|---|
| durante a subida | `mode: "switching"`, `last_action: "start_from_standby"` |
| depois do frame | `mode: "watching"`, `mode_reason: "fonte_confirmada"`, `last_action: "engage_ok"`, `app_container_running: true`, `active_source: "drone"`, `sources_probe.drone.alive: true`, `sources_probe.drone.level: 2` |

### Prova no dashboard

```bash
curl -fsS http://localhost:8081/api/snapshot | jq '.observer | {available, mode, mode_reason, last_action, age_s}'
curl -fsS http://localhost:8081/api/snapshot | jq '.latest_metrics | {source_id, fps_in, ts}'
curl -fsS http://localhost:8081/api/sources  | jq '.[] | select(.id=="drone") | .observer_probe'
```

Esperado: `.observer.mode == "watching"`; `.latest_metrics.source_id ==
"drone"` com `.latest_metrics.fps_in > 0`; `observer_probe` do drone
`status: "measured"`, `alive: true`, `level: 2`.

### Aceite

- `standby_exited` com `mode: "switching"` **antes** do `engage_ok`;
- `engage_ok` com `fps_in > 0` e `source: "drone"`;
- `mode` volta a `watching` sem intervenção, dentro de 90 s do `docker_start`;
- se o frame novo não vier em 90 s: `engage_failed` (`reason:
  "post_check_timeout"`), volta a `standby_no_sources` e o `active_source`
  anterior é restaurado — isso é FAIL deste cenário.

### Tempos esperados

| Etapa | Tempo | Base |
|---|---|---|
| fonte volta → DESCRIBE vivo | ≤ 10 s | ciclo do watchdog |
| `ok_streak` 2 | ~20 s (2 ciclos) | `MIN_OK_STREAK` |
| sonda de mídia (nível 2) | ~5 s | `--seconds 5` do `rtsp_read_probe.py` |
| start → primeiro `interval_metrics` do processo novo | ~34 s + até 30 s | boot do engine + `METRICS_INTERVAL_S` |
| `engage_ok` | ≤ 90 s após o `docker_start` | `OBSERVER_WAKE_TIMEOUT_S` |

---

## Cenário 3 — sem fonte, o container fica parado (sem loop de restart)

**Objetivo:** provar que `restart: unless-stopped` não religa o container parado
pelo observador.

**Pré-estado:** cenário 1 concluído, publisher ainda morto.

### Comando

```bash
# 6 amostras, 60 s de intervalo (5 min de janela)
for i in 1 2 3 4 5 6; do
  date -u +%H:%M:%S
  docker inspect --format '{{.RestartCount}} {{.State.Status}} {{.State.ExitCode}} {{.HostConfig.RestartPolicy.Name}}' vision-stability-app
  sleep 60
done

docker ps -a --filter name=vision-stability-app --format 'table {{.Names}}\t{{.Status}}'
docker ps --filter name=vision  # a app NÃO aparece
```

### Prova no watchdog.jsonl

```bash
jq -c 'select(.event=="watchdog_docker_start" or .event=="watchdog_auto_switch_triggered")' data/logs/watchdog.jsonl | tail -3
```

Durante a janela: **nenhum** `watchdog_docker_start` novo e nenhum
`watchdog_auto_switch_triggered`. O que continua saindo é só o ciclo do
observador (sem evento; o retrato é reescrito a cada 10 s).

### Prova no observer_state.json

```bash
jq '{ts, mode, app_container_running, app_exit_code, last_action, last_action_ts}' data/config/observer_state.json
```

| Campo | Valor esperado |
|---|---|
| `ts` | **avança a cada ~10 s** — é a prova de que o watchdog está vivo e reavaliando, sem tocar no container |
| `mode` | `"standby_no_sources"` (não muda durante a janela) |
| `app_container_running` | `false` |
| `last_action` | `"stop_standby"` |
| `last_action_ts` | fica parado no instante da parada: no standby a política devolve `none` (`standby_sem_fonte_viva`) e a ação não é reescrita |

### Prova no dashboard

```bash
curl -fsS http://localhost:8081/api/snapshot | jq '.observer | {available, mode, ts, age_s}'
curl -fsS http://localhost:8081/api/snapshot | jq '.observer | {available, mode, ts, age_s}'   # 15 s depois
```

Esperado: `available: true`, `mode: "standby_no_sources"`, `age_s < 120` e
`ts` diferente entre as duas leituras (observador vivo, sem religar nada).

### Por que `restart: unless-stopped` não religa

A política de restart do Docker reage à **saída** do container. `docker stop`
feito pelo observador é uma parada explícita: o `unless-stopped` é definido
justamente para **não** trazer de volta container parado dessa forma (é a
diferença em relação a `always`). Já um container que sai sozinho com código 2
é reiniciado pela política — por isso pode haver **1 restart entre o crash e o
`docker stop`**; o que o cenário exige é que o `RestartCount` **estabilize**,
não que ele seja zero. Continuam valendo como "religar por fora" (não faça
durante a janela): `docker start`, `docker restart`, `make restart-app`,
`make restart`, `docker compose up -d` / `make up`, `make switch`, e o botão da
tela em modo troca.

### Aceite

- `RestartCount` idêntico nas 6 amostras;
- `State.Status` `exited` em todas;
- `HostConfig.RestartPolicy.Name` `unless-stopped`;
- nenhum `watchdog_docker_start` na janela.

### Tempos esperados

| Etapa | Tempo |
|---|---|
| janela de observação | 5 min (6 amostras de 60 s) |
| ciclo de reavaliação do observador | 10 s |
| restart pela política, se houver | assim que o processo sai (backoff do Docker), antes do `docker stop` do ciclo seguinte |

---

## Cenário 4 — troca manual durante o standby

**Objetivo:** com o pipeline parado pelo observador, a troca manual continua
funcionando: pré-check antes de subir, mutex respeitado, container recriado,
pós-check PASS.

**Pré-estado:** cenário 1/3 (app `exited`, `mode: standby_no_sources`) e a fonte
de destino publicando (`camera` é a fonte do lab).

### Comando

```bash
cat data/config/active_source                                  # drone
docker ps -a --filter name=vision-stability-app --format '{{.Names}} {{.Status}}'  # exited

./scripts/switch_source.sh camera ; echo "exit=$?"
# ou, pelo Makefile (sem o echo do exit code):
make switch SOURCE=camera
```

Referência de comportamento e de recuperação de falha:
`docs/runbooks/troca-de-fonte.md`.

### Prova no watchdog.jsonl

```bash
tail -f data/logs/watchdog.jsonl | grep -E 'watchdog_(switch_in_progress_observed|container_restarted|switch_marker_invalid|auto_switch_triggered)'
```

| Evento | Campos que importam |
|---|---|
| `watchdog_switch_in_progress_observed` | `to: "camera"`, `expires_at`, `ttl_s` — sai **uma vez** por marcador |
| `watchdog_container_restarted` | `expected_by_switch: true` (o poll precisa cair enquanto o marcador do script vale), `started_at`, `uptime_s` |
| `watchdog_switch_marker_invalid` | só se o marcador estiver corrompido (`reason`, `detail`) — nesse caso o watchdog volta às regras normais |
| `watchdog_auto_switch_triggered` | **não** deve sair por causa desta troca (a política cede ao marcador: `switch_marker_ativo`) |

O marcador é visível enquanto a troca roda:

```bash
cat data/config/switch_in_progress   # {"to": "camera", "expires_at": "...", "owner": "script:...", "origin": "script"}
ls data/config/switch_in_progress    # depois do PASS: No such file or directory
```

### Prova no observer_state.json

```bash
jq '{mode, mode_reason, app_container_running, active_source, last_action, sources_probe}' data/config/observer_state.json
```

| Campo | Valor esperado após o PASS |
|---|---|
| `active_source` | `"camera"` |
| `app_container_running` | `true` |
| `last_action` | `"engage_ok"` se o observador reassumiu, `"stop_standby"` se ainda está no ciclo de standby |
| `mode` | `"watching"` ou ainda `"standby_no_sources"` — ver nota abaixo |

Nota de campo (comportamento real do código): saindo do standby, o observador
só abandona `standby_no_sources` quando a política confirma **mídia (nível 2)**
da fonte; com a fonte ativa já entregando frame, a sonda nível 1 dela é
dispensada para não abrir sessão RTSP concorrente. Resultado: o `mode` pode
continuar `standby_no_sources` por alguns ciclos com o app no ar. **Não use
`mode` como critério deste cenário** — use `active_source`, o container no ar e
o `/api/sources`.

### Prova no dashboard

```bash
curl -fsS http://localhost:8081/api/sources  | jq '.[] | {id, active, observer_probe}'
curl -fsS http://localhost:8081/api/snapshot | jq '.latest_metrics | {source_id, fps_in, ts}'
curl -fsS http://localhost:8081/api/snapshot | jq '.observer | {available, mode, last_action, age_s}'
```

Esperado: entrada `camera` com `active: true`; `observer_probe` em
`measured`/`nao_sondada` (`nao_sondada` é resposta válida — fonte ativa com
frame não é sondada); `latest_metrics.source_id == "camera"` com `fps_in > 0`.

### Exit codes e o que fazer

| Exit | Significado | O que fazer |
|---|---|---|
| 0 | PASS: pós-check confirmou a fonte | nada; seguir para o aceite |
| 1 | pós-check não confirmou; rollback feito para a fonte anterior | ler `reason`/`last_error`; ver `no_frames_on_path`, `source_id_inesperado`, `startup_failed` no runbook |
| 2 | uso incorreto ou id inválido | `./scripts/switch_source.sh --help`; ids em `deepstream/config/sources.yaml` |
| 3 | bloqueado sem escrever: `ACTIVE_SOURCE` no ambiente **ou** outra troca em andamento | remover `ACTIVE_SOURCE` de `deploy/.env` e `make up`; ou esperar o marcador liberar |
| 4 | pré-check reprovado (a fonte não publica) | container **continua parado** (standby preservado); resolver a fonte e repetir |

### Aceite

- stdout com `PASS source=camera ... fps_in=...` e `exit=0`;
- `data/logs/switch.jsonl` com uma linha `result: "PASS"`, `reason: "ok"`;
- `data/config/active_source` = `camera` e `data/config/switch_in_progress`
  ausente;
- `vision-stability-app` `Up` e `/api/sources` com `camera` `active: true`;
- um `watchdog_switch_in_progress_observed` por troca (não um por ciclo).

### Tempos esperados

| Etapa | Tempo | Base |
|---|---|---|
| pré-check | ~5 s | `PRECHECK_SECONDS=5` |
| `up -d --no-deps --force-recreate vision-app` | ~5–15 s | — |
| primeiro `interval_metrics` do processo novo | ~34 s + até 30 s | boot do engine |
| pós-check do script | até 90 s (default `--timeout`) | `switch_source.sh` |
| confirmação do observador (se reassumir) | ≤ 90 s | `OBSERVER_WAKE_TIMEOUT_S` |

---

## Reset e limpeza entre cenários

Voltar ao estado de referência (câmera ativa, precedência padrão, sem marcador):

```bash
./scripts/switch_source.sh camera      # PASS esperado
make up                                # sem OBSERVER_SOURCE_PRIORITY=... volta ao default camera,drone,phone
make status

cat data/config/active_source          # camera
ls data/config/switch_in_progress      # No such file or directory
docker ps -a --filter name=vision-stability-app --format '{{.Names}} {{.Status}}'   # Up
docker exec vision-watchdog printenv OBSERVER_SOURCE_PRIORITY                      # camera,drone,phone
```

- **Não** apague `data/logs/watchdog.jsonl`, `live.jsonl` ou `switch.jsonl`:
  são a evidência da validação. Filtre por horário (`jq 'select(.ts > "...")'`).
- Para repetir o cenário 1 sem reiniciar nada: religue o publisher,
  `make switch SOURCE=drone`, `OBSERVER_SOURCE_PRIORITY=drone make up` e mate o
  publisher de novo.
- Entre cenários, espere o `age_s` do `.observer` voltar a < 20 s (um ciclo
  novo já foi escrito) antes de considerar qualquer leitura.

## Falhas comuns

| Sintoma | Causa provável | O que rodar |
|---|---|---|
| Fonte volta e o estado segue `standby_no_sources` com `sources_probe.drone.alive: false` | publisher não voltou, path errado ou MediaMTX sem o path | `python3 scripts/probe/rtsp_read_probe.py --url rtsp://<LAN_IP>:8554/drone --seconds 5`; `docker logs --tail 50 vision-mediamtx` |
| `available: false` com `age_s` alto | watchdog parado, `observer_state.json` velho ou relógio dessincronizado | `docker ps -a --filter name=vision-watchdog`; `docker logs --tail 50 vision-watchdog`; `jq -r .ts data/config/observer_state.json` vs `date -u` |
| `available: false` e `reason` citando `estado do observador velho (há Ns)` | `ts` do arquivo passou de 120 s (watchdog travado) | idem acima; reiniciar só o watchdog: `make up` |
| Marcador órfão em `data/config/switch_in_progress` | execução morta no meio da troca (`kill -9`) | `cat data/config/switch_in_progress`; se `expires_at` no passado, a próxima troca descarta sozinha; se corrompido, `rm -f data/config/switch_in_progress` (o observador só descarta corrompido após o TTL de 150 s) |
| `watchdog_standby_stop_failed` | `docker.sock` inacessível ou API lenta | `docker info`; `docker inspect --format '{{.State.Status}}' vision-stability-app`; conferir o mount `/var/run/docker.sock` no `vision-watchdog` |
| Troca manual sai 3 com `reason=active_source_no_ambiente` | `ACTIVE_SOURCE` no ambiente vence o arquivo (o observador também para de decidir) | `docker exec vision-watchdog printenv ACTIVE_SOURCE`; `docker exec vision-stability-app printenv ACTIVE_SOURCE`; apagar de `deploy/.env` e `make up` |
| `watchdog_engage_failed` com `reason: post_check_timeout` | o processo novo não publicou frame da fonte em 90 s | `jq -c 'select(.event=="interval_metrics")' data/logs/live.jsonl | tail -3`; ver `source_id`, `fps_in`; o observador já voltou ao standby e restaurou a fonte anterior |
| Standby não acontece e aparece `watchdog_auto_switch_triggered` | há outra fonte viva na precedência (a câmera, tipicamente) | `docker exec vision-watchdog printenv OBSERVER_SOURCE_PRIORITY`; restringir ao path sob teste ou derrubar a outra fonte |
| `watchdog_switch_marker_invalid` | marcador corrompido/sem `expires_at` legível | `cat data/config/switch_in_progress`; o watchdog segue com as regras normais; reiniciar a troca limpa o caso |

## Automatizável × mão humana

| Passo | Modo |
|---|---|
| ler `watchdog.jsonl`, `observer_state.json`, `live.jsonl` | automatizável |
| consultar `/api/snapshot` e `/api/sources` | automatizável |
| `./scripts/switch_source.sh` / `make switch` | automatizável |
| `make up`, `make status`, `docker inspect`/`ps` | automatizável |
| janela de 5 min e amostragem de `RestartCount` | automatizável |
| derrubar a fonte (Ctrl-C no publisher, desligar câmera/drone) | **manual** |
| religar a fonte (rodar o publisher, energizar a câmera) | **manual** |
