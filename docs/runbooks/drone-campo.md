# Runbook de campo — drone (primeira sessão)

> **Estado:** o drone **nunca foi ligado** neste projeto. Este é o roteiro da
> **primeira** sessão de campo. Tudo que depende do equipamento (app, codec,
> bitrate, URL custom) é **observação a registrar**, não fato conhecido.
>
> Documentos irmãos — leia em vez de repetir daqui:
> `docs/runbooks/troca-de-fonte.md` (comando de troca, exit codes, rollback),
> `docs/runbooks/captura-celular.md` (Android/WHIP — **não** é o caminho do
> drone), `docs/plans/2026-10-30-troca-de-fonte-vsl.md` (D1–D7, E6-T1) e
> `docs/incident-log.md` (DIV-003 e INC-004).

## Contexto técnico (valores reais desta máquina)

```
drone ──RTMP──▶ rtmp://192.168.59.102:1935/drone   (MediaMTX: vision-mediamtx)
                   └─ república ─▶ rtsp://192.168.59.102:8554/drone
                                      └─▶ pipeline (vision-stability-app, DeepStream)
```

| Item | Valor real | Fonte |
|---|---|---|
| Push do drone | `rtmp://<ip-da-LAN>:1935/drone` (`make ip` mostra o IP atual) | `deploy/docker-compose.yml` (`${LAN_IP:?}:1935:1935`, IP detectado no `make up`) |
| Leitura do pipeline | `rtsp://${MTX_HOST:-mediamtx}:8554/drone` (no host: `192.168.59.102`) | `deepstream/config/sources.yaml` |
| Path declarado | `drone:` em `paths:` | `mediamtx/mediamtx.yml` |
| Codec exigido | `codec: h264` | `sources.yaml` (drone) |
| Transporte | `transport: tcp`, `latency_ms: 200` | `sources.yaml` |
| FPS alvo | `target_fps: 25` | `sources.yaml` |
| Gate de latência | `latency_p95_ms: null` + `report_only: true` | `sources.yaml` — **D3** |
| Transcodificação | **nenhuma** — o MediaMTX só repassa | `mediamtx/mediamtx.yml` (`rtspTransports: [tcp]`) |
| Autenticação | **nenhuma** (DIV-003) | `docs/incident-log.md` |

Consequência dura de "sem transcodificação": **HEVC/H.265 no RTMP quebra o
caminho** — o path funciona, o probe lê, mas o pipeline monta
`rtph264depay ! h264parse` (o par vem de `codec: h264`) e nunca linka os pads.
O sintoma é "sonda PASS, pipeline sem frame".

## 1. Objetivo e escopo da sessão

**Provar, com o equipamento real, que o caminho `RTMP → MediaMTX → RTSP →
pipeline` funciona de ponta a ponta** — e medir o que só o campo produz:

| # | O que provar | Instrumento |
|---|---|---|
| 1 | Ingestão do drone real é processada (`source_id=drone`, `fps_in > 0`) | `switch_source.sh drone` + `data/logs/live.jsonl` |
| 2 | Latência do drone é **medida e reportada** (p50/p95/máx) | método manual da seção 4e — **não** gate (D3) |
| 3 | O pipeline **sobrevive à queda de sinal** e volta sozinho | queda provocada + eventos de reconexão |
| 4 | O stack cabe na memória com o drone no ar | `scripts/mem_budget.sh` |

**Fora de escopo nesta sessão** (não é esquecimento, é decisão):

- **gravação** de vídeo/arquivo (não existe caminho de gravação no stack hoje);
- **multi-fonte simultânea** (D1/ADR-0006: uma fonte ativa por vez);
- **iOS** (D6: celular é Android; iPhone não entra por este ou por nenhum runbook);
- **gate de latência do drone** (D3: reportada, não reprovada);
- **autenticação no MediaMTX** (DIV-003 segue aberta; a sessão roda em LAN confiável).

## 2. Antes de sair (30 minutos antes)

Rode **na Orin**, com o drone ainda na caixa. Se qualquer item falhar, o
problema é de bancada — resolva aqui, não no campo.

### 2.1 Stack no ar — 5 containers, 1 DeepStream

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

Esperado, exatamente estes cinco (nomes reais do compose) e nenhum outro com
imagem DeepStream:

| Container | Serviço |
|---|---|
| `vision-stability-app` | pipeline (**o único** DeepStream) |
| `vision-mediamtx` | ingestão RTMP / RTSP / WHIP |
| `vision-capture-web` | página de captura (8444) |
| `vision-dashboard` | dashboard (8081) |
| `vision-watchdog` | watchdog de telemetria/memória |

```bash
# Regra do host: exatamente 1 container DeepStream
docker ps --format '{{.Names}}' | grep -cE 'vision-stability-app|deepstream'   # esperado: 1
docker ps -a --format '{{.Names}}' | grep -iE 'deepstream|ds-' || echo "nenhum container DeepStream extra (ok)"
```

O stack **arquo** fica parado durante a sessão (é a condição dos baselines em
`docs/baselines/`).

### 2.2 Path `drone` declarado

```bash
grep -nA1 '^  drone:' mediamtx/mediamtx.yml
```

Esperado: a linha `  drone:` sob `paths:`. Path **não** declarado no
`mediamtx.yml` faz o RTMP ser recusado na publicação — e o log do MediaMTX é
quem diz isso (`[RTMP] ... path 'drone' is not configured`), não o app do drone.

### 2.3 Portas publicadas **só** no IP da LAN

```bash
ss -lntu | awk 'NR>1 {print $5}' | grep -E ':(1935|8554)$' | sort
```

Esperado, e **nada além disso** (o IP é o que `make ip` mostrar):

```text
<ip-da-LAN>:1935
<ip-da-LAN>:8554
```

Se aparecer `0.0.0.0:1935` ou `*:1935`, o compose não recebeu `LAN_IP` — rode
`make up` (que detecta o IP e valida no `make doctor`) em vez de
`docker compose up` direto (DIV-003: publish anônimo exposto em todas as
interfaces). **Não saia de casa assim.**

### 2.4 Publisher sintético — a linha de base

Antes de existir drone, o caminho já tem que funcionar com o publisher
sintético (`scripts/probe/rtmp_publisher.sh`, E0-T3, já validado). Note o
`--fps 25`: o default do script é **30**, e o `target_fps` do drone é **25** —
a linha de base tem de casar com o alvo, senão a comparação de fps mente.

```bash
./scripts/probe/rtmp_publisher.sh --path drone --fps 25 --seconds 90 &
sleep 2

docker logs vision-mediamtx --since 1m 2>&1 | grep '\[path drone\]'

python3 scripts/probe/rtsp_read_probe.py \
    --url rtsp://192.168.59.102:8554/drone --seconds 10
```

Esperado: linha de log `[path drone] stream is available and online, 1 track
(H264)` e sonda com

```text
PASS ok | codec=h264 frames=... fps=~25 first_frame=~0.3s gaps>500ms=0/0
```

Se a sonda reprovar **aqui**, o problema é do stack — não leve o drone.

### 2.5 Suíte de testes verde

```bash
python3 -m pytest deepstream/tests scripts/tests -q
bash -n scripts/switch_source.sh scripts/probe/rtmp_publisher.sh
```

Nenhum teste falhando. Teste vermelho antes de sair = você vai depurar em campo
sem bancada.

### 2.6 Fonte ativa = `camera` (fallback conhecido)

```bash
cat data/config/active_source          # esperado: camera
tail -n 3 data/logs/live.jsonl | grep interval_metrics
```

O estado seguro da máquina é `camera` com `fps_in > 0`. É para **este** estado
que a sessão volta no fim (§4f) e em qualquer emergência.

### 2.7 Baseline de memória, para comparar depois

```bash
bash scripts/mem_budget.sh
```

Guarde a linha JSON: é o "antes". Gates de 1 fonte: RSS **750–1050 MB** (faixa do
baseline da **câmera**, H.265; fonte H.264 pode medir um pouco abaixo), RAM
**≤ 70 %**, `oom_killed: false`, `exit 137 = 0`. Sobre `lfb`: no Orin desta
máquina **16–60 MB já é o normal** (correção registrada em
`docs/baselines/2026-10-30-e1t2-baseline.md`); não trate `lfb` baixo como
falha.

## 3. Antes de voar — equipamento

### 3.1 Checklist físico

- [ ] drone, hélices, bateria carregada (**e** a bateria do controle)
- [ ] controle + cabo/telefone que o app exige
- [ ] cartão de memória (gravação local do drone é independente do stream)
- [ ] área de decolagem com **luz suficiente** (o detector do lab já errou em
      cena escura — anote a condição de luz na ficha)
- [ ] notebook/telefone com o dashboard aberto em `http://192.168.59.102:8081`
- [ ] cronômetro (o mesmo aparelho serve para a medição de latência, §4e)
- [ ] esta página aberta e a ficha da §6 pronta para preencher

### 3.2 A pergunta crítica: **o app aceita URL RTMP customizada?**

> **Respondido pelo operador em 2026-10-31: sim, o app aceita URL RTMP
> customizada.** O roteiro abaixo continua sendo passo de campo (confirmar no
> aparelho, com a versão do app anotada), mas deixa de ser incógnita: **não há
> plano B a decidir em voo**. Se o campo desmentir, o plano B abaixo continua
> valendo.

Antes de decolar, responda com o app na mão:

```text
1. Abrir a tela de transmissão/streaming do app.
2. Existe campo de "RTMP URL" / "custom RTMP server" / "endereço de destino"?
3. Colar:  rtmp://192.168.59.102:1935/drone
4. Existe campo de "stream key" separado?
      -> se sim, deixe VAZIO e cole a URL completa acima no campo de servidor;
         se o app exigir "/<key>", use  rtmp://192.168.59.102:1935/drone/
         (o path do MediaMTX é `drone`; uma key extra vira outro path e o
         pipeline não enxerga).
```

- **Aceita URL custom** → siga para a §4.
- **NÃO aceita** (só YouTube/Facebook/plataformas fixas) → **plano B**, decida
  em campo e **registre a decisão**:
  1. **app alternativo** que aceite RTMP custom (é o caminho de menor custo:
     o contrato de ingestão `rtmp://<LAN>:1935/drone` não muda);
  2. **proxy/relay** — um segundo publisher que consome o que o app publica e
     republica em `rtmp://192.168.59.102:1935/drone`
     (`ffmpeg -i <origem> -c copy -f flv rtmp://192.168.59.102:1935/drone`).
     **Só faz sentido se o app publicar em algo alcançável pela LAN** (um
     servidor próprio na mesma rede). Se o app publica direto na nuvem do
     fabricante, o proxy **não** resolve: isso é limitação do app, e a sessão
     de campo vira sessão de bancada até trocar o app.
- **Não decidir em voo.** App sem URL custom = não há como testar ingestão.

### 3.3 O que anotar **antes** do primeiro push

| Campo | Como obter |
|---|---|
| Nome e **versão** do app | tela "sobre"/loja — versão exata, é o que permite repetir |
| Codec configurado | menu de transmissão (H.264 / H.265 / "auto") |
| Resolução | 1080p / 720p / 4K — **o app oferece 720p e 1080p; medir as duas** (§4a) |
| FPS | 30 / 25 / 24 |
| Bitrate | kbps ou "auto" — se for "auto", registre o valor que o app mostrar em voo |
| Distância de rádio | "próximo (< 5 m)" / "médio" / "longe" — o link é parte do experimento |

> **A tela do app não é medição.** Codec, resolução, fps e bitrate que valem
> para a ficha são os que o caminho entrega, medidos com
> `scripts/probe/ingest_report.py` no §4a. A tela serve de conferência (e é o
> único lugar onde aparece "auto"); quando divergirem, vale o medido e a
> divergência vira observação da ficha.

**Regra desta sessão:** o app tem de publicar **H.264**. Se ele só oferecer
H.265/HEVC, **pare** — o MediaMTX não transcodifica e o pipeline está com
`codec: h264` (`sources.yaml`). Trocar isso é mudança de configuração, não
improviso de campo.

## 4. Procedimento numerado

Todos os comandos rodam **na Orin** (raiz do repo), com o drone no ar e o push
ativo no app. A ordem importa: cada passo prova uma coisa antes de o próximo
mexer em algo.

### 4a. Subir o push do drone e confirmar no MediaMTX

1. No app: iniciar a transmissão para `rtmp://192.168.59.102:1935/drone`.
2. Confirmar que o MediaMTX viu o publish:

```bash
docker logs vision-mediamtx --since 2m 2>&1 | grep -E '\[RTMP\].*(opened|closed)|\[path drone\]'
```

Esperado: uma linha `[RTMP] [conn 192.168.59.102:xxxxx] opened` (o IP de origem
é o **telefone/controle**, não o drone) seguida de
`[path drone] stream is available and online, 1 track (H264)`.
`1 track` = **só vídeo**. `2 tracks` costuma significar áudio junto (aceitável);
`0`/nenhum path = o push não chegou.

3. Sondar o path com o instrumento do pré-check:

```bash
python3 scripts/probe/rtsp_read_probe.py \
    --url rtsp://192.168.59.102:8554/drone --seconds 10 --max-gaps -1
```

(`--max-gaps -1` aqui de propósito: nesta etapa você **mede** o link de rádio,
não reprova por ele — o gate de gaps é do `switch_source.sh`.)

O que conferir no JSON/linha de resumo:

| Campo | Esperado | Se não |
|---|---|---|
| `codec` | **`h264`** | `h265` = o app está publicando HEVC → **pare** (§5) |
| `fps` | próximo do que o app configura (alvo 25) | muito abaixo = bitrate/link |
| `first_frame_s` | < 1 s com o push já no ar | > 3 s = path recém-criado ou rede |
| `gaps_over_500ms` / `max_gap_ms` | 0–1 / < 500 ms | rádio instável: anote distância |
| `reason` | `ok` | `no_video_pad` = só áudio ou HEVC (§5) |

#### 4a.1 Medir codec, resolução, fps e bitrate reais (um comando)

A sonda acima diz **se** chega vídeo; este instrumento diz **o que** chega — é
ele que preenche as linhas de codec/resolução/fps/bitrate da ficha (§6):

```bash
python3 scripts/probe/ingest_report.py \
    --url rtsp://192.168.59.102:8554/drone --seconds 20 --ficha
```

Saída esperada (resumo em stderr + uma linha JSON em stdout):

```text
OK ok | rtsp://192.168.59.102:8554/drone
codec=h264 1920x1080 fps=29.97 bitrate=5980kbps frames=599 janela=20.0s
first_frame=0.31s gaps>500ms=0 max_gap=34ms audio_tracks=0 dur=20.1s
| Codec real | H.264 |
| Resolução do app | 1920x1080 (1080p) |
| FPS do app | 29.97 (medido) |
| Bitrate do app | 5980 kbps (medido no RTP) |
| Áudio junto | 0 faixa(s) |
```

Sem gate: exit **0 = OK** (chegou vídeo), **1 = FAIL** (motivo no `reason`),
**2 = uso incorreto**. Ele **não** reprova fonte nem substitui o pré-check do
`switch_source.sh` (D7).

Regras de leitura:

| O que | Por quê |
|---|---|
| `codec` tem de ser `h264` | `h265`/`HEVC` **para a sessão** (§5): `sources.yaml` declara H.264 e não há transcodificação |
| `resolution_class` | 720p/1080p: é a variável que muda bitrate, fps e latência — número sem ela não é comparável (R10) |
| `bitrate_kbps` | medido **no fio** (RTP, cabeçalho incluso; medição de bancada: 6.053 kbps para encoder de 6.000 kbps, ~1 % de overhead); compare com o que o app declara |
| `audio_tracks` | > 0 é aceitável (o pipeline ignora áudio), mas anote: muda o bitrate total |
| `gaps_over_500ms` | aqui é **observação** do link de rádio; o gate de gap é do `switch_source.sh` |

**Rode duas vezes: uma em 720p e uma em 1080p** (são as opções do app), sempre
com `--seconds 20`. O par (resolução, bitrate) medido é o que decide qual
configuração entra na janela de regime do §7 — e o `fps_processed ≥ 95 % de
fps_in` é o critério que condena a resolução alta (§4c). Registre as duas
linhas JSON na ficha; não escolha "a melhor" e apague a outra.

### 4b. Trocar a fonte para o drone

```bash
./scripts/switch_source.sh drone
```

O script faz o pré-check de **5 s** (`rtsp_read_probe.py --seconds 5`, D7) e
**aborta sem tocar em nada** se o path não estiver entregando (exit 4). O
comportamento completo, exit codes e rollback estão em
`docs/runbooks/troca-de-fonte.md` — aqui só o que este teste específico deve
mostrar:

```text
PASS source=drone first_frame_in=~34s fps_in=~25 codec=h264 precheck=ok session_epoch=...
```

- `fps_in` ~ igual ao medido na §4a (é o mesmo stream, agora pelo pipeline);
- `first_frame_in` ~34 s é **normal** (import do DeepStream + `METRICS_INTERVAL_S=30`
  s); o `--timeout` default de 90 s cobre. **Não** aperte `--timeout 40` no
  primeiro teste de campo;
- exit **4** = o drone parou de publicar entre a §4a e agora (o app pode ter
  derrubado o push ao trocar de tela). Reconecte e repita;
- exit **1** = rollback automático para `camera`; o `reason` diz o porquê
  (`no_frames_on_path`, `source_id_inesperado`, …). **O pipeline está em
  `camera`, não em `drone`** — confirme com `cat data/config/active_source`.

### 4c. Conferir a telemetria

```bash
tail -n 20 data/logs/live.jsonl | python3 -c '
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    if r.get("event") == "interval_metrics":
        print(r["ts"], "source=", r["source_id"],
              "fps_in=", r["fps_in"], "fps_processed=", r["fps_processed"],
              "queue=", r["queue_buffers"],
              "frame_age_s=", r["last_frame_age_s"], "rss_mb=", r["rss_mb"])
'
```

Critérios deste passo (um `interval_metrics` a cada 30 s):

| Campo | Esperado | Significado quando falha |
|---|---|---|
| `source_id` | `drone` | pipeline subiu com outra fonte (ver exit 3 em `troca-de-fonte.md`) |
| `fps_in` | > 0, ~ igual à §4a | push morreu ou o pipeline não linka (codec) |
| `fps_processed` | ≥ 95 % de `fps_in` | inferência não acompanha: bitrate/resolução altos |
| `queue_buffers` | `queue_analyze` e `queue_preview` ≤ 1 | fila crescendo = mesma causa de `fps_processed` baixo |
| `last_frame_age_s` | < 2 | > 30 dispara reconexão por inatividade (§5) |
| `rss_mb` | faixa de referência 750–1050 MB (baseline da **câmera**, H.265); H.264 pode ficar um pouco abaixo — o que condena é crescimento contínuo ou > 1050 | acima = pressão de memória no Orin |

Se `fps_processed` < 95 % de `fps_in` **de forma sustentada** (3 intervalos),
anote resolução/fps/bitrate reais do app: é quase sempre bitrate alto demais
para o link ou resolução acima do que o decoder do pipeline espera. **Não**
"resolva" trocando `target_fps` em campo.

### 4d. Teste de queda de sinal

Objetivo: provar que o pipeline **volta sozinho** e medir em quanto tempo.

Antes de provocar, deixe um observador de eventos aberto em outro terminal:

```bash
tail -f data/logs/live.jsonl | grep -E '"(eos|rtsp_reconnect_started|rtsp_reconnect_resumed|source_reconnect_budget_reset|source_reconnect_exhausted|pipeline_error)"'
```

**Como provocar** (comece pelo mais barato):

1. **Corte do push no app** ("parar transmissão") por ~10 s e religue. É a queda
   mais limpa: o path do MediaMTX é destruído e recriado.
2. **Se o app não deixar parar/religar rápido**, afaste o telefone/controle para
   fora de alcance por ~10 s (ou desligue o Wi-Fi do aparelho, se o push for por
   Wi-Fi) e volte.
3. **Não desligue o drone em voo para este teste.** A queda de rádio é a mesma
   coisa e o risco não se justifica.

**O que observar, na ordem** (é o que o código faz):

| Evento | Quando aparece | O que significa |
|---|---|---|
| `eos` (`src=...rtspsrc`) | path do MediaMTX morre | o MediaMTX fechou o stream para o leitor |
| `rtsp_reconnect_started` (`reason=eos`) | ~imediato | só o `rtspsrc` é resetado (o resto do pipeline fica de pé) |
| `rtsp_reconnect_resumed` | **+2 s** (timer fixo) | **não é prova de vídeo** — prova de que o reset foi concluído |
| `last_frame_age_s` voltando a < 2 no `interval_metrics` | até 30 s de granularidade | prova objetiva de que frame voltou |
| `rtsp_reconnect_started` (`reason=no_frames`, `idle_s≈30`) | se o path ficar **mudo** sem EOS | regra de inatividade: `RTSP_IDLE_RECONNECT_S=30` |
| `source_reconnect_exhausted` | após **5** tentativas sem frame | o processo encerra com exit 2 (fonte perdida); `restart: unless-stopped` recria o container |
| `rtsp_reconnect_error` | a cada tentativa com o path fora do ar | erro do `rtspsrc` durante a reconexão: **esperado** numa queda, não é falha do pipeline. Desde 2026-10-31 sai com nome próprio e **não** entra em `error_count` do dashboard |
| `pipeline_error` | qualquer momento | **falha de verdade** (erro de análise: mux, decoder, `nvinfer`) — anote e investigue |

**Como medir o tempo de retorno (alvo ≤ 20 s):**

> O alvo era **≤ 5 s** até 2026-10-31. Ele foi **revisado depois da primeira
> medição de campo**, não antes: o backoff de reconexão começa em **10 s**
> (`deepstream/source_reconnect.py`, `retry_base_s=10.0`: 10 → 20 → 40 → 45 s) e
> o `rtspsrc` leva ~10 s para renegociar depois que o path volta — então ~10 s é
> o **piso do desenho**, e ≤ 5 s exigiria mudar o código, não o teste. Medido em
> 2026-10-31: **11,56 s** do `is publishing` do MediaMTX ao `rtsp_video_pad`
> (9,56 s do path online ao pad). O alvo de 20 s cobre a 2ª tentativa (10 s de
> espera) mais a renegociação; se o backoff mudar, o alvo acompanha.

- **método principal:** delta entre o instante em que o app **volta** a
  publicar (log do MediaMTX: `is publishing to path 'drone'`) e o evento
  `rtsp_video_pad` (o primeiro frame novo). É o número que entra na ficha.
- **conferência objetiva:** delta entre o `ts` do primeiro `interval_metrics`
  com `last_frame_age_s < 2` e o `ts` de `rtsp_reconnect_resumed`. As métricas
  saem a cada 30 s, então esse número é um **limite superior**, não a medida
  fina.
- **registre também** quanto tempo o pipeline levou para **perceber** a queda
  (`ts` de `rtsp_reconnect_started` − `ts` do último frame). Se o path ficar
  mudo sem EOS, esse número tende a ~30 s: é a regra de inatividade, não um bug
  — e é a razão de o alvo valer para o **retorno**, não para a detecção.

Se o corte passar de ~1 min, é provável que o orçamento de reconexão
(`SOURCE_RECONNECT_MAX_ATTEMPTS=5`) estoure e o processo saia com **exit 2**
(desenhado, não crash). Anote como "encerrou por fonte perdida" e confirme que
o container voltou; a diferença entre "reconectou" e "reiniciou" muda a
conclusão da sessão.

### 4e. Medição de latência — **reportada** (D3)

O drone **não** tem gate de latência (`gate.latency_p95_ms: null`,
`report_only: true`). Medimos para **relatar**, não para reprovar.

> **Não use `now − PTS`.** O `rtspsrc` desloca o PTS junto com `latency=200`,
> então a conta se cancela e produz número enganoso (risco R10 do plano;
> ADR-0005 do projeto arquo, achado 1). Também não existe hoje
> `scripts/probe/latency_flash.py` — é entrega do E6-T1; nesta sessão o
> instrumento é manual.

**Método (relógio comum, ~5 min):**

1. Coloque na frente da câmera do drone um visor com **relógio de
   milissegundos** (telefone com cronômetro no modo paisagem, ou notebook com
   uma página de relógio com ms). O visor tem de ser legível no vídeo.
2. Abra o preview anotado (`live`) no dashboard, em tela cheia, no maior
   tamanho possível. Congele o drone (pairado ou no chão, com o push ativo) —
   latência com o drone em movimento só piora a leitura.
3. Capture ~**20 amostras**: para cada uma, tire um print do preview e leia os
   dígitos do relógio visíveis na imagem (`T_vídeo`).
4. `latência ≈ T_captura − T_vídeo`, arredondando para o ms mais próximo que o
   visor permite (um relógio de segundos inteiros **não** serve: o erro de
   leitura seria ±1 s).
5. Registre as 20 amostras cruas na ficha; calcule **p50, p95 e máx**.

Regras de honestidade do número:

- a mesma captura inclui **encode do drone + rede + MediaMTX + decode + preview
  do pipeline**: é latência de *caminho*, que é o que interessa ao projeto;
- declare no relatório **resolução, fps e bitrate** do app durante a medição e a
  distância do drone (R10: número sem essas variáveis não é comparável);
- latência medida no preview `live` inclui o ramo de preview do pipeline
  (`queue_preview`, NVENC). Se quiser o número "de análise", diga no relatório
  qual ramo foi lido — não misture os dois;
- se a medição for feita com o drone em 4G (fora da LAN) o número **não** é
  comparável com o da LAN: anote o caminho.

O resultado entra em um arquivo novo em `docs/baselines/` (§6), com a ressalva
"reportado, sem gate (D3)" no topo.

### 4f. Encerrar a sessão — voltar ao estado seguro

```bash
# 1. parar o push no app ANTES de mexer no pipeline (evita "path mudo" no meio)

# 2. voltar para a câmera
./scripts/switch_source.sh camera

# 3. não deixar publisher esquecido (nem o sintético, nem relay do plano B)
pkill -f rtmp_publisher.sh || true
pkill -f 'rtmp://192.168.59.102:1935/drone' || true
pgrep -af 'rtmp_publisher|rtmp://192.168.59.102:1935' || echo "nenhum publisher ativo (ok)"
```

Confirmação final, nesta ordem:

```bash
cat data/config/active_source                                   # camera
tail -n 5 data/logs/live.jsonl | grep -c '"source_id": "camera"' # > 0
docker ps --format '{{.Names}}' | grep -cE 'deepstream|vision-stability-app'  # 1
bash scripts/mem_budget.sh                                      # exit_code 0, oom false
```

Só vá embora com `camera` processando e **zero** publisher ativo. Publisher
esquecido mantém o path ocupado e contamina a próxima sessão (e, por DIV-003,
é publish anônimo na LAN).

## 5. Sintoma → causa → ação

| Sintoma | Causa provável | Ação |
|---|---|---|
| App diz "transmitindo", `docker logs vision-mediamtx` **não** tem `[path drone]` | path não declarado no `mediamtx.yml`, porta errada, ou push foi para outro host | `grep -nA1 '^  drone:' mediamtx/mediamtx.yml`; URL exatamente `rtmp://192.168.59.102:1935/drone`; `ss -lntu \| grep 1935` |
| Log tem `[RTMP] ... opened` mas nenhum `[path drone]` | app publicou em **outro path** (key extra) ou em `rtmp://…/live` | rever o campo de URL/key no app; path tem de ser `drone` |
| Sonda PASS com `codec: h265` (ou `HEVC`) | app publicando **HEVC** | **pare**: trocar o app para H.264. `sources.yaml` declara `codec: h264` e não há transcodificação |
| Sonda FAIL `no_video_pad` | só áudio no stream, ou vídeo em codec fora de H.264/H.265 | desligar áudio no app; conferir codec |
| Pipeline sobe (`source_id=drone`) mas `fps_in = 0`, sem `pipeline_error` | config diz `h264`, stream é HEVC: pads nunca linkam | mesmo caso acima — o sintoma é *silencioso*, por isso a §4a existe |
| `fps_processed` < 95 % de `fps_in`; `queue_analyze` subindo | bitrate/resolução acima do que o link e a GPU entregam | baixar bitrate/resolução no app; repetir §4a; anotar valores |
| `fps_in` da **câmera** cai sozinho e volta (15,8 → 6,5 → 12,0) | **baixa luminosidade**: a câmera reduz o fps no escuro (auto-exposição/auto-framerate). Medido em 2026-10-31 com a luz sendo ligada/desligada; com luz estável volta a 20,0 | conferir a luz antes de culpar o pipeline ou o rádio; anotar a condição na ficha (`Distância / condição de luz`) |
| `last_frame_age_s` > 30 com `rtsp_reconnect_started reason=no_frames` | path mudo (push morreu sem EOS) | religar o push; se repetir, é o link — medir distância |
| `source_reconnect_exhausted` + container com exit **2** | 5 tentativas sem frame (corte longo) | esperado em corte > ~1 min; confirmar que o container voltou; **não** confundir com crash |
| `eos` repetidos a cada poucos segundos | push instável (rádio/Wi-Fi do controle) | aproximar o drone/telefone; anotar distância e condição |
| Drone em **4G** não alcança `192.168.59.102` | o IP é **privado**: não existe rota da internet para a LAN | o push **tem** de sair de um aparelho **na mesma LAN** (Wi-Fi do campo). Se o drone/controle só tiver 4G, é preciso **port forward** no roteador do campo (expor `1935` publicamente, o que por DIV-003 é publish anônimo na internet — **não fazer**) ou **VPN** que coloque o aparelho na LAN (túnel para a Orin). Sem uma das duas, não há ingestão: decisão de infraestrutura, não de campo |
| Erro de **ICE/TLS** | só afeta o caminho do **celular** (WHIP, 8889/TLS, 8189/ICE) | **irrelevante para o drone**: RTMP em 1935 é TCP puro, sem ICE e sem TLS |
| SSID/rede diferente | telefone/controle em outra rede (hotspot, 5 GHz isolado, "guest") | conferir o IP do aparelho com o push ativo: tem de ser `192.168.59.x`; SSID de convidado costuma isolar clientes |
| `docker logs vision-mediamtx` mostra `no route to host`/`i/o timeout` no RTMP | firewall do host ou cliente em VLAN isolada | testar `nc -vz 192.168.59.102 1935` **do aparelho** (não da Orin) |

## 6. Ficha de coleta (preencher e anexar em `docs/baselines/`)

Copie para `docs/baselines/2026-10-31-drone-campo.md` e preencha. Série bruta
(`mem_budget.sh --loop 60 12` e as linhas de `live.jsonl` da janela) vai em um
`.jsonl` ao lado, como nos baselines existentes.

```bash
bash scripts/mem_budget.sh --loop 60 12 > data/logs/drone-campo-mem.jsonl &

# as duas medições de ingestão (§4a.1), uma por resolução do app
python3 scripts/probe/ingest_report.py \
    --url rtsp://192.168.59.102:8554/drone --seconds 20 \
    >> data/logs/drone-campo-ingest.jsonl          # app em 720p
python3 scripts/probe/ingest_report.py \
    --url rtsp://192.168.59.102:8554/drone --seconds 20 \
    >> data/logs/drone-campo-ingest.jsonl          # app em 1080p
```

As linhas `| ... |` da ficha saem prontas com `--ficha` (§4a.1) — cole e
confira, não redigite de memória.

| Campo | Valor |
|---|---|
| Data / hora (local e UTC) | |
| Host / `LAN_IP` | `192.168.59.102` (ou o do campo, se mudou) |
| Modelo do drone | |
| App + **versão** | |
| URL de push usada | `rtmp://192.168.59.102:1935/drone` (confirmar o que o app salvou) |
| Codec real | H.264 / H.265 — **medido** (`ingest_report.py` ou `codec=` da sonda) |
| Resolução do app | 720p / 1080p — **medida** (`resolution_class`) |
| FPS do app | medido (`fps`) |
| Bitrate do app | kbps — **medido** (`bitrate_kbps`, no fio/RTP) |
| Resoluções testadas (bitrate/fps em cada) | 720p: ___ kbps / ___ fps · 1080p: ___ kbps / ___ fps |
| Distância / condição de luz | |
| `fps_in` (telemetria, mediana) | |
| `fps_processed` / `fps_in` | % |
| Sonda §4a: `fps`, `first_frame_s` | |
| Gaps > 500 ms / `max_gap_ms` | |
| `queue_analyze` / `queue_preview` (pico) | |
| Queda provocada: método | corte do push / afastamento |
| Tempo até **perceber** a queda | s |
| Tempo até o **primeiro frame de volta** (alvo ≤ 5 s) | s |
| Evento final da queda | `rtsp_reconnect_resumed` / `source_reconnect_exhausted` |
| Latência p50 / p95 / máx (nº de amostras) | ms — **reportada (D3)** |
| Ramo medido na latência | `live` (preview) / análise |
| RSS (MB) mín–máx | |
| RAM do sistema (%) | |
| `lfb` (MB) | |
| `restarts` / `OOMKilled` / exit code | |
| Containers DeepStream ativos | 1 |
| Observações (o que surpreendeu, o que refazer) | |

## 7. Critérios de aceite da sessão

A sessão é **aprovada** quando, na janela de regime (depois do PASS da §4b e
**fora** da queda provocada):

- [ ] path `drone` online e processado por **≥ 10 min** sem `pipeline_error` na
      telemetria;
- [ ] `fps_processed ≥ 0,95 × fps_in` nos `interval_metrics` da janela;
- [ ] reconexão após queda provocada com **primeiro frame de volta ≤ 20 s**
      (§4d) — e o evento `rtsp_reconnect_resumed` (ou, se o corte foi longo,
      `source_reconnect_exhausted` registrado como tal);
- [ ] **zero** container com exit **137**;
- [ ] **zero** `OOMKilled: true` em `docker inspect` e em `mem_budget.sh`;
- [ ] **exatamente 1** container DeepStream (`vision-stability-app`);
- [ ] ficha da §6 preenchida e anexada em `docs/baselines/`;
- [ ] sessão encerrada em `camera`, sem publisher ativo (§4f).

**Não reprova a sessão:**

- **latência** — por **D3** o drone é `report_only`; p95 alto é dado, não falha
  (nesse caso, registre o número e a condição do link);
- `eos` / `rtsp_reconnect_started` na janela da queda provocada — é o
  comportamento esperado;
- `rtsp_reconnect_error` (erro do `rtspsrc` durante a reconexão) e
  `preview_branch_error` (ramo descartável) — **não** são falha do pipeline;
  `pipeline_error` continua reprovando;
- exit **2** (`source_reconnect_exhausted`) em corte longo — é o caminho
  desenhado para fonte perdida, desde que o container tenha voltado e o retorno
  do vídeo esteja registrado;
- `lfb` baixo (16–60 MB é o normal desta Orin).

**Reprova a sessão** (não é "quase passou"): `pipeline_error` em regime,
segundo container DeepStream, exit 137, `OOMKilled`, ou ingestão que só funciona
com o publisher sintético.

## 8. Segurança e operação

**DIV-003 — o MediaMTX não tem autenticação.** Qualquer máquina que alcance
`192.168.59.102:1935` (RTMP), `:8554` (RTSP), `:8888/8889` (HLS/WebRTC) ou
`:8890/udp` (SRT) pode **publicar** e **ler** os paths `live`, `drone` e
`phone`, sem credencial (`mediamtx/mediamtx.yml` com `api: no` e nenhum
`authMethod`; `docs/incident-log.md`, DIV-003). Consequências práticas em campo:

- **não** rodar a sessão em rede aberta (Wi-Fi de praça, hotel, evento): o vídeo
  do drone e o preview ficam legíveis por qualquer cliente da rede, e qualquer
  um pode injetar vídeo no path que o pipeline está consumindo;
- **não** abrir `1935` na internet para "resolver" drone em 4G (port forward é
  publish anônimo global). Se 4G for requisito, a resposta é **VPN**, e com a
  dívida DIV-003 tratada antes;
- **não** deixar publisher rodando (nem o sintético): path ocupado + publish
  anônimo = a próxima sessão lê um stream que não é o esperado.

**Regra do host — um único container DeepStream.** Nunca subir um segundo
container DeepStream para "testar o drone em paralelo": `switch_source.sh` já
recria **só** `vision-stability-app`, e é assim que a troca funciona. Um segundo
pipeline no Orin (7,6 GB de RAM unificada) é o caminho para exit 137 / INC-004.

**Se o stack ficar instável em campo** (fila crescendo sem parar, `fps_processed`
colapsando, watchdog reiniciando em ciclo, RAM > 70 %):

```bash
# 1. rollback imediato para o estado seguro
./scripts/switch_source.sh camera

# 2. parar o push do drone no app (o path pode ficar mudo sem custo)

# 3. coletar evidência ANTES de qualquer restart manual
mkdir -p data/logs/drone-incidente
tail -n 500 data/logs/live.jsonl          > data/logs/drone-incidente/live.jsonl
tail -n 200 data/logs/watchdog.jsonl      > data/logs/drone-incidente/watchdog.jsonl
tail -n 200 data/logs/switch.jsonl        > data/logs/drone-incidente/switch.jsonl
docker logs --since 30m vision-stability-app > data/logs/drone-incidente/app.log 2>&1
docker logs --since 30m vision-mediamtx      > data/logs/drone-incidente/mediamtx.log 2>&1
docker inspect vision-stability-app > data/logs/drone-incidente/inspect.json
bash scripts/mem_budget.sh | tee data/logs/drone-incidente/mem.json

# 4. só então, se preciso, reiniciar o pipeline com a fonte segura
docker compose --env-file deploy/.env -f deploy/docker-compose.yml \
    up -d --no-deps --force-recreate vision-app
```

Log perdido é sessão perdida: a coleta vem **antes** do restart. Se o incidente
tiver cara de memória (`exit 137`, `OOMKilled`, lfb colapsando), registre em
`docs/incident-log.md` no mesmo formato de INC-004.
