# ADR-0006 — Troca de fonte no Vision Stability Lab: três fontes alternativas, uma ativa por vez

- **Status:** Aceito (implementação iniciada — E0 e E1-T1 concluídas em `feature/multifonte`)
- **Data:** 2026-10-30
- **Plano:** [`docs/plans/2026-10-30-troca-de-fonte-vsl.md`](../plans/2026-10-30-troca-de-fonte-vsl.md)
- **Hardware:** Jetson Orin, L4T R36.4.7, aarch64, **7.802.160 kB** (≈7,44 GiB) de memória unificada, `MemAvailable` 4.067.412 kB — `/proc/meminfo`, 2026-10-30
- **Filiação:** primeiro ADR materializado neste repositório. A numeração **0006** continua a série do repositório irmão `arquo` (`docs/adr/adr-0001…adr-0005`, branch `feature/SURVEY-110-m1-phone-ingest-spike`), de onde vêm o MediaMTX, o `whip_probe.py` e o método de medição de latência. O README do VSL não tem seção de decisões; a partir daqui as decisões arquiteturais ficam em `docs/adr/`.

---

## Contexto

O Vision Stability Lab nasceu como um experimento de **uma** fonte: uma câmera RTSP H.265, um
`nvinfer` YOLO11m, um tracker IOU, 30 min por fase (`README.md:1-20`, `docs/phase-1-evaluation.md`
a `docs/phase-3-evaluation.md`). O pipeline real (`deepstream/pipeline_real.py`) foi construído
para esse caso único e assume isso em três lugares concretos:

- `nvstreammux` com `batch_size=1` (`pipeline_real.py:190`; `deepstream/config/infer.txt` tem
  `batch-size=1` casado com o engine `model_b1_gpu0_fp16.engine`);
- `RTSP_CODEC` como variável de ambiente **global do container** (`deploy/docker-compose.yml`),
  escolhendo `rtph265depay`/`h265parse` ou o par H.264 no `__init__` (`pipeline_real.py:186-187`);
- credencial única vinda de `RTSP_URI` (`deepstream/rtsp_env.py`, DIV-001).

O objetivo do laboratório, no entanto, sempre foi "provar estabilidade operacional", não "provar
uma câmera". Em campo, o material disponível é heterogêneo: uma câmera fixa IP, um drone que só
publica **RTMP/H.264**, e um celular Android que publica **WHIP/H.264**. As Etapas 0 construíram
a infraestrutura para os três chegarem no mesmo lugar — o MediaMTX foi pinado em 1.21.3 com os
paths explícitos `live`/`drone`/`phone` (`mediamtx/mediamtx.yml`), ICE migrado para
`webrtcLocalUDPAddress`/`webrtcLocalTCPAddress`, TLS na 8889 (E0-T4) e as sondas de aceitação
(`scripts/probe/rtsp_read_probe.py`, `scripts/probe/whip_probe.py`, `scripts/probe/rtmp_publisher.sh`)
com contratos de exit code testados (E0-T3).

Falta a pergunta de arquitetura: **o pipeline deve aprender a lidar com três fontes ao mesmo
tempo, ou deve continuar com um pipeline de uma fonte e aprender a trocar de fonte?**

A restrição que decide essa pergunta não é conceitual, é de memória. O host tem 7,44 GiB
**unificados** (CPU e GPU compartilham o mesmo pool), o container do pipeline já pesa
**17,8 GB de imagem** (`docker images`, 2026-10-30) e o RSS em operação fica em
**913–947 MB** com uma fonte (`data/logs/live.jsonl`). O carveout CG (CMA) tem 256 MiB, dos
quais 63,8 MiB livres (`CmaTotal`/`CmaFree` em `/proc/meminfo`, 2026-10-30). A regra global do
projeto proíbe dois containers DeepStream simultâneos no mesmo host exatamente por isso
(`AGENTS.md`, regra 1 — registrada em 2026-08-25 depois de um container de teste derrubar o de
produção nesta mesma Orin).

## Decisão

1. **D1 — Uma fonte ativa por vez.** Câmera fixa, drone (RTMP → MediaMTX) e celular
   (WHIP → MediaMTX) são **alternativas**, não simultâneas. `batch_size=1` é mantido; não há
   tiler, não há grade no OSD, não há engine b3.
2. **D2 — Estado por fonte, com isolamento e reset entre sessões.** Todo registro de telemetria
   carrega `source_id` e `session_epoch`; o estado de visão (tracks, eventos, dwell, contadores,
   orçamento de reconexão) é reiniciado a cada sessão. O `useUniqueID: 0` do tracker **não** é
   tocado — o isolamento é por namespace de telemetria, não por reconfiguração do tracker.
3. **D3 — O drone fica fora do gate de latência p95 ≤ 300 ms.** A latência do drone é
   **reportada**. O gate do drone é reconexão, estabilidade e integridade do stream.
4. **D4 — A troca é `data/config/active_source` + `scripts/switch_source.sh` + reinício do
   processo**, não reconfiguração em runtime.
5. **D5 — Codec por fonte** (`depay_factory`): câmera H.265; drone e celular H.264.
6. **D6 — Celular somente Android.** iOS fora de escopo.
7. **D7 — O `switch_source.sh` faz pré-check obrigatório** com
   `rtsp_read_probe.py --seconds 5`; exit `0` = pode trocar. Qualquer outro código aborta a
   troca **sem** tocar em `active_source`.
8. **D8 — Branch `feature/multifonte`.**
9. **D9 — Pull anônimo do `nvcr.io` funciona** (token de convidado; `docker manifest inspect`
   sem login). Nenhuma etapa depende de conta NGC.

## Alternativas descartadas (e por quê)

### A1 — Multi-fonte simultânea com `batch_size=3`

**Descartada por memória e por contrato de modelo, não por preferência.**

- O engine existente é `b1` (`data/model_b1_gpu0_fp16.engine`, referenciado em
  `deepstream/config/infer.txt` como `model-engine-file`). Rodar batch 3 exige **engine nova**,
  build de TensorRT fora do escopo declarado e um segundo artefato de 100+ MB para versionar.
- Com 7,44 GiB **unificados** e RSS de ~950 MB por fonte, três streams concorrentes colocam o
  `nvinfer` no carveout junto com NVDEC/NVENC de três pipelines de decode e três ramos de
  preview. O histórico já tem evidência de CUDA runtime OOM a 40 MB de alocação com a RAM do
  sistema em 25 % (`docs/incident-log.md`, INC-004; análise em
  `docs/baselines/2026-10-30-e1t2-baseline.md`).
- O tracker IOU único passaria a ver três streams no mesmo batch, e a correlação de tracks entre
  fontes é justamente o que o escopo diz não fazer (`README.md:91-107`: "Múltiplas câmeras" está
  na lista de não incluídos).
- Operacionalmente, três fontes simultâneas trocam **uma** pergunta ("a fonte estável é a mesma
  coisa que o pipeline estável?") por várias. O laboratório perderia o poder de atribuição de
  causa que é o motivo de ele existir.

### A2 — Tiler / grade (`nvmultistreamtiler`) com fontes em janelas

**Descartada porque não resolve o problema e cobra caro.** O tiler existe para *visualizar*
múltiplas fontes em um mosaico; ele não reduz o custo de decode, inferência nem encode (pelo
contrário: adiciona `nvvideoconvert` + composição por frame). No pipeline atual o OSD é
`process_mode=1` (GPU) sobre o buffer do `nvstreammux` (`pipeline_real.py:208-209`), e o ramo de
preview já tem um `nvvideoconvert` + NVENC. Uma grade tornaria o vídeo de preview menos legível
justamente quando ele é usado para diagnosticar (o OSD é a única forma barata de ver se a
detecção está certa). Se um dia houver mosaico, ele é **downstream**, no dashboard, não no
pipeline — e mesmo assim exigiria múltiplas fontes, que A1 recusa.

### A3 — Endpoint de troca em runtime (HTTP no pipeline, ou API do MediaMTX)

**Descartada por custo de máquina de estados sem ganho de produto.** Trocar de fonte "ao vivo"
significa: deslinkar `rtspsrc` → resolver pads antigos → reconstruir depay+parse para outro codec
(caps diferentes) → religar no `nvstreammux` → lidar com o EOS/flush pendente de cada elemento →
renegociar o preview no MediaMTX. Cada um desses passos tem uma janela em que o pipeline está
inconsistente, e todos eles vivem no processo que o watchdog pode reiniciar a qualquer momento.
O resultado prático seria *exatamente* o restart do processo, com mais código e mais modos de
falha. Além disso, um endpoint de troca é superfície de ataque nova num stack que já tem
DIV-003 aberta (MediaMTX sem autenticação). O tempo de troca aceitável nesta fase é de **dezenas
de segundos** (o restart leva ~11-40 s, medido na prática pelos `container_restarted` em
`data/logs/watchdog.jsonl`), e D4 entrega isso.

### A4 — Trocar o `location` do `rtspsrc` com `g_object_set`

**Descartada porque não funciona com codec diferente e não é verificável.** O `rtspsrc` está
linkado a **um** depayloader concreto desde o `__init__`
(`pipeline_real.py:186, 255-256`): o `pad-added` liga o pad de vídeo ao sink pad do `rtph265depay`
(H.265) ou `rtph264depay` (H.264). Mudar `location` para uma fonte H.264 mantendo o depay H.265
produz falha de caps negociada em runtime, não uma troca. Fazer certo obrigaria a deslinkar,
recriar e religar a cadeia — que é A3 de novo. Há ainda o problema de estado do elemento: o
`rtspsrc` guarda sessão RTSP, jitterbuffer e relógio; reconfigurá-lo em PLAYING não tem contrato
documentado de "recomeça limpo" e o único jeito de provar seria medir — e o custo de medir é
igual ao custo de implementar o restart do processo, que é determinístico.

### A5 — Suporte a iOS

**Descartada por ausência de device e por stack de codec distinta.** Não há iPhone no time
(2026-10-30). O `H264 42001f` com `packetization-mode=1` medido no Chrome Android não diz nada
sobre o WebKit — são stacks de codec diferentes (o ADR-0005 do arquo registra exatamente essa
distinção). Suportar iOS sem poder medir significaria prometer um caminho não verificado, que é o
tipo de erro que já custou retrabalho neste projeto (INC-001: relatório declarando sucesso sem
inferência executada). O caminho de contenção é o mesmo do projeto irmão: upload de arquivo, fora
deste épico.

### A6 — Recuperar a imagem do preview a partir de outro host (offload de encode/decode)

**Descartada por não atacar o gargalo e adicionar um serviço novo.** A motivação seria tirar
carga da Orin movendo decode/encode para outra máquina. Mas o custo dominante aqui não é encode
(o NVENC já é hardware, e o ramo de preview é `queue leaky max-size-buffers=1` justamente para
não bloquear a análise) — é a memória unificada durante a inferência. Mover o preview para outro
host não libera inferência; adiciona uma dependência de rede no caminho de observação e um
segundo MediaMTX/relay para operar. Também colide com o princípio "offline-capable" que orienta a
borda.

### A7 — Manter as três fontes "cadastradas" mas com `RTSP_CODEC` trocado por `docker exec`

**Descartada por ser pior que o problema.** Seria a mudança mínima (variável de ambiente por
container), mas exige editar o compose ou exportar dentro do container, não deixa rastro auditável
da troca, não valida que a fonte de destino está viva e não tem rollback. É o que D7 e o
`switch.jsonl` existem para substituir.

## Consequências

### Positivas

- **Uma variável por vez.** Todo achado de estabilidade continua atribuível a *uma* fonte e *uma*
  configuração de pipeline; o `batch_size=1` preserva a validade das Fases 1–4.
- O custo de memória do épico é **zero por fonte adicional** — as três nunca coexistem. O gate de
  RSS continua sendo 750–1050 MB (`docs/baselines/2026-10-30-e1t2-baseline.md`; medição atual
  913–947 MB em `data/logs/live.jsonl`).
- A troca vira operação auditável: `data/config/active_source` (estado), `data/logs/switch.jsonl`
  (histórico), `session_epoch` (amarração com a telemetria).
- O pré-check obrigatório (D7) elimina a classe de falha "troquei para uma fonte que não estava
  publicando e o stack ficou cego", que é a mais cara de diagnosticar em campo.

### Negativas / custos aceitos

- **Troca não é instantânea**: reiniciar o processo custa o tempo de subida do DeepStream
  (dezenas de segundos) e zera o estado de visão. Aceitável porque D1 já implica sessões discretas.
- **Sem continuidade de histórico de ocupação entre fontes**: `session_epoch` corta a série. É
  intencional (D2), mas significa que "tempo total ocupado" não pode ser somado entre fontes sem
  uma decisão de produto futura.
- **Código de fonte duplicado no YAML**: cada fonte nova exige uma entrada em
  `deepstream/config/sources.yaml` e um caminho de publicação no MediaMTX. Não há autodescoberta.
- **O rollback depende de manter o caminho legado vivo**: `RTSP_URI`/`RTSP_CODEC` não podem ser
  removidos do compose enquanto existir `VSL_SOURCE_SELECTION=off`.

### Dívidas abertas por esta decisão

1. **DIV-004 (proposta) — autoridade de "fonte perdida" não está definida.** Hoje há três
   candidatos com opiniões diferentes: o pipeline sai com **exit 2** após esgotar
   `SOURCE_RECONNECT_MAX_ATTEMPTS = 5` (`deepstream/pipeline_real.py:100`,
   `deepstream/source_budget.py`); o watchdog escala `no_frames_escalation` após 180 s
   (`scripts/watchdog.py`); e o Docker **reinicia em qualquer caso** por causa de
   `restart: unless-stopped` (`deploy/docker-compose.yml`). Como o compose reinicia até no exit 2,
   o evento correto é mascarado como "reinício" — o mesmo sintoma que motivou o fix de EOS
   (`c29cf8b`). Precisa ser decidido antes do E6 (opções e recomendação na seção 11 do plano).
2. **DIV-003 fica mais exposta, não menos.** O MediaMTX não tem autenticação
   (`mediamtx/mediamtx.yml`, `api: no`, sem `authMethod`) e agora **dois** caminhos de ingestão
   (`drone` por RTMP 1935 e `phone` por WHIP 8889) são publicáveis por qualquer host da LAN. As
   portas continuam amarradas ao IP da LAN (E0-T2), o que reduz a superfície mas **não autentica**.
   Ação requerida antes de qualquer uso em rede não confiável continua sendo `authMethod: internal`
   ou allowlist por path.
3. **Gates de memória do drone e do celular são provisórios.** Foram escritos iguais aos da câmera
   (750–1050 MB) para não se inventar número; precisam de medição própria no E7-T1 e de atualização
   com fonte.
4. **Latência por fonte ainda não tem instrumento.** Não existe medição de latência no pipeline
   hoje (o único `latency` no código é o jitterbuffer do `rtspsrc`, `pipeline_real.py:184`), e a
   medição por `now − PTS` é inválida porque o `rtspsrc` desloca o PTS junto com o `latency`
   (ADR-0005 do arquo, achado 1). O E6-T1 precisa construir o instrumento (alternância de cor +
   relógio comum), e até lá **nenhum número de latência deve ser citado como medido**.
5. **Rotação de log pendente.** `data/logs/live.jsonl` já acumula 4.176 amostras de
   `interval_metrics` desde 2026-08-23; 12 ciclos mais três testes de 6 h crescem essa série sem
   teto, e o dashboard reconstrói o histórico inteiro na partida
   (`docs/architecture.md:76`).
6. **Baseline de 1 fonte ainda inválida.** `docs/baselines/2026-10-30-e1t2-baseline.md` está
   marcado inválido por conter restart de falso positivo do watchdog (15:54) além de um EOS real
   (15:51). O E1-T2 refaz a janela antes de qualquer comparação nova.

## Evidência de que a decisão é executável (Estado 0 → E1-T1)

O que já foi provado nesta branch antes de escrever este ADR:

| Item | Evidência | Onde |
|---|---|---|
| MediaMTX recebe e republica as três origens nos paths certos | `mediamtx/mediamtx.yml` com `live`/`drone`/`phone`; versão pinada 1.21.3 | commit `2ec5193` |
| WHIP do celular funciona com TLS e candidato ICE correto | `whip_probe.py --cacert` devolve `201` + `DELETE 200`; candidatos só no IP da LAN | `docs/runbooks/captura-celular.md`, E0-T4 |
| Drone/celular de bancada entram por RTMP/H.264 | `scripts/probe/rtmp_publisher.sh --path drone\|phone` | E0-T3 |
| Pré-check de fonte tem contrato de exit code utilizável pelo script de troca | `rtsp_read_probe.py`: 0 = PASS, 1 = FAIL, 2 = uso incorreto | `scripts/probe/rtsp_read_probe.py` |
| Imagem DeepStream sobe **sem login NGC** | `deploy-vision-app:latest`, 17,8 GB, stack completo no ar | E1-T1, `docker images` |
| Credencial continua fora do repositório | guarda de regressão verde | `deepstream/tests/test_no_hardcoded_secrets.py` |

## Reprodução

```bash
# 1. estado do stack (um container DeepStream, no máximo)
docker ps -a | grep -i vision

# 2. as três origens alcançáveis
python3 scripts/probe/rtsp_read_probe.py --url "$RTSP_URI" --seconds 5 --json        # câmera
./scripts/probe/rtmp_publisher.sh --path drone --seconds 60 &                        # bancada do drone
python3 scripts/probe/whip_probe.py --cacert deploy/certs/ca.crt --path phone        # caminho WHIP

# 3. orçamento de memória da sessão corrente
APP_CONTAINER=vision-stability-app bash scripts/mem_budget.sh --loop 60 30

# 4. testes puros (sem GPU) — o que sustenta esta decisão sem hardware
python3 -m pytest deepstream/tests scripts/tests -q
```

## Emenda (2026-11-01) — botão de troca no dashboard (E4-T3)

O operador pediu a troca da fonte ativa pela tela. **Nenhuma decisão desta ADR
muda:**

- **D4 continua sendo o caminho**: `data/config/active_source` + reinício do
  processo. O endpoint `POST /api/source/switch` do dashboard é um **atalho**
  para o mesmo script, e reusa as decisões puras do
  `scripts/switch_source_lib.py` (validação do id, guarda de precedência do
  `ACTIVE_SOURCE`, `process_boundary` no pós-check, `decide_rollback`).
- **A3 segue descartada**: não há reconfiguração em runtime. Quando o endpoint
  é chamado, o processo do pipeline é **reiniciado** — o dashboard não fala com
  o GStreamer, apenas com o Docker.
- **D7 continua obrigatório**: o pré-check roda **antes** de escrever o arquivo,
  dentro do `vision-stability-app` (via `docker exec`), porque o dashboard não
  tem GStreamer e o ambiente que vale é o do container.
- **Superfície de ataque**: o endpoint é **opt-in**. Sem `SWITCH_TOKEN` ele
  responde `503`; com token, exige o header `X-Switch-Token` (`401` se
  divergir). O socket do Docker só é montado no overlay
  `deploy/docker-compose.switch.yml` — a exposição está registrada como
  **DIV-004** em `docs/incident-log.md`.

Consequência aceita: um dashboard comprometido passa a ter acesso
root-equivalente ao host (socket do Docker). O mitigante é o deploy padrão
seguir **sem** o overlay e sem token.

### Emenda à emenda (2026-11-01, pós-revisão independente)

A revisão independente do E4 (`architecture-reviewer`, mesmo dia) reprovou
quatro pontos que a versão original do E4-T3 não cobria. Nenhum muda decisão
de arquitetura; três mudam **implementação** e um muda o **deploy**:

1. **Transporte do token (H1).** O token ia em HTTP claro para um endpoint com
   socket do Docker, e o painel escutava na LAN. Com o overlay ativo, o
   dashboard passa a ser publicado em **`127.0.0.1`**
   (`DASHBOARD_BIND`, fixado por `make up-switch`); o acesso é por túnel SSH.
   O modo leitura (compose padrão) continua na LAN. Expor o endpoint de troca
   na rede passa a ser escolha explícita (`SWITCH_BIND=0.0.0.0`), com o risco
   registrado em DIV-004.
2. **Exclusão mútua entre os dois caminhos de troca (H2).** O marcador
   `data/config/switch_in_progress` vira **mutex entre processos**: reivindicado
   com `O_EXCL` (dashboard) e `noclobber` (script), com `expires_at` e `owner`,
   e cada lado só apaga **o próprio** marcador. Troca em curso de qualquer
   origem → `409` (tela) ou `exit 3` (script), com `GET` informando
   `in_progress_by`. Sem isso, dois `restart`s podiam se sobrepor e uma troca
   podia terminar "PASS" sem o pós-check de ninguém.
3. **Falha do restart depois da escrita (H3).** Se o `restart` falhar depois de
   gravar `active_source`, a fonte anterior é **restaurada** (com nova tentativa
   de restart) e a execução é auditada como `FAIL restart_falhou`. Antes, o
   arquivo ficava apontando para uma fonte que nunca passou pelo pós-check.
4. **Teto de tempo na sonda (H4).** O pré-check roda sob `timeout`
   (`PRECHECK_TIMEOUT_S`, 20 s). Sonda pendurada vira `precheck:timeout` com
   auditoria e o lock é liberado — antes, todo `POST` respondia `409` até
   reiniciar o dashboard.

Contratos preservados: D4, D7 e a proibição de reconfiguração em runtime (A3)
seguem intactos; o `switch.jsonl` continua com uma linha por execução, agora
com o motivo normalizado no mesmo vocabulário do script
(`precheck:no_frames_on_path`, `precheck:timeout`, `restart_falhou`).

## Referências

- Plano executável: `docs/plans/2026-10-30-troca-de-fonte-vsl.md`
- ADR irmão (WHIP/MediaMTX, método de latência, gate de codec no device):
  `arquo:docs/adr/adr-0005-m1-phone-ingest-whip-mediamtx.md`, branch
  `feature/SURVEY-110-m1-phone-ingest-spike`
- Baseline de memória (inválida como baseline, útil como referência de ordem de grandeza):
  `docs/baselines/2026-10-30-e1t2-baseline.md`
- Runbook do celular: `docs/runbooks/captura-celular.md`
- **Dívidas e incidentes:** `docs/incident-log.md` (DIV-001 resolvida, DIV-003 e
  **DIV-004** abertas — DIV-004 é o socket do Docker no dashboard, criado pela
  emenda de 2026-11-01), `docs/phase5-progress.md:89` (DIV-002, UPS)
- Regra global de containers DeepStream: `~/.config/opencode/AGENTS.md`
