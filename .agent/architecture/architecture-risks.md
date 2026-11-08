# Architectural Risks — Vision Stability Lab

> **Natureza:** diagnóstico de riscos do estado ATUAL. Não contém arquitetura-alvo nem plano de
> implementação — cada risco termina em **decisão pendente**, não em solução.
> **Data:** 2026-11-01 · **Commit de referência:** `cd5bbfb` · **Branch:** `feature/multifonte`
> **Base:** `system-map.md`, `component-catalog.md`, `dependency-map.md` + revisão independente.
> **Nota de escopo:** o worktree recebeu alterações de E4 **depois** desta análise. Este documento
> vale para `cd5bbfb`.
> **Escala:** **P1** = compromete operação/evolução no curto prazo · **P2** = custo crescente,
> deve ser endereçado na próxima onda · **P3** = higiene, sem impacto imediato.

---

## 1. Resumo por severidade

| ID | Risco | Sev. | Confiança | Categoria |
|---|---|---|---|---|
| R1 | Documentação arquitetural canônica desatualizada em 10 pontos | **P1** | [F] | Governança |
| R2 | Contrato de telemetria sem schema/versão, consumido por 3 camadas independentes | **P1** | [F] | Contrato |
| R3 | `pipeline_real.py` concentra 6 responsabilidades em 1215 linhas / 31 métodos | **P1** | [F] | Estrutura |
| R4 | Testes acoplados ao texto do código e ao estado do Git | **P1** | [F] | Testabilidade |
| R5 | Plataforma de GPU sem fixação executável (TensorRT/CUDA/GStreamer/driver/L4T) | **P1** | [F] | Reprodutibilidade |
| R6 | Ciclo de vida do MediaMTX não supervisionado → vídeo morto com pipeline "healthy" | **P1** | [F]+[H] | Operação |
| R20 | Ausência de teste de integração e de contrato produtor→consumidor | **P1** | [F] | Testabilidade |
| R21 | Pressão de memória unificada / OOM sem limite de container | **P1** | [F]+[H] | Operação |
| R7 | Vocabulário de eventos duplicado; evento novo é descartado em silêncio na UI | **P2** | [F] | Contrato |
| R8 | Quatro candidatos a entrypoint com `app.py` enganoso | **P2** | [F] | Estrutura |
| R9 | Direção invertida script-operacional → aplicação, protegida só por teste textual | **P2** | [F] | Acoplamento |
| R10 | Duplicação de conhecimento: IP, ids de fonte, paths, limiares | **P2** | [F] | Acoplamento |
| R11 | Dependências fantasma (pydantic/requests/mqtt/paho-mqtt) | **P2** | [F] | Dependências |
| R12 | Artefatos de runtime não versionados (engine `.engine`, parser `.so`, certs) | **P2** | [F] | Reprodutibilidade |
| R13 | `deploy/certs` e SAN amarrados ao IP da LAN | **P2** | [F] | Operação |
| R14 | Healthchecks e limites de staleness contraditórios (4 valores) | **P2** | [F] | Observabilidade |
| R15 | Gate de latência p95 declarado no YAML sem consumidor no código | **P2** | [F] | Contrato |
| R16 | `watchdog` com `docker.sock` e `vision-app` `privileged` | **P2** | [F]+[H] | Segurança |
| R22 | Dashboard sem autenticação em `0.0.0.0` + código do host montado RW em container privilegiado | **P2** | [F] | Segurança |
| R23 | Telemetria sem rotação/retenção; replay integral no boot | **P2** | [F] | Operação |
| R17 | Configuração de fonte duplicada (`source.yaml` órfão vs `sources.yaml`) | **P3** | [F] | Higiene |
| R18 | `switch.jsonl` e `drone-campo-ingest.jsonl` sem consumidor | **P3** | [F] | Observabilidade |
| R19 | `/healthz` do dashboard devolve constante | **P3** | [F] | Observabilidade |

**Contagem:** P1 = 8 · P2 = 12 · P3 = 3 (total 23).

---

## 2. Riscos P1

### R1 — Documentação arquitetural canônica desatualizada

**Evidência [F].** `docs/architecture.md` tem 90 linhas, data **2026-08-23**, e se declara "Fase 4
concluída". O código está em Fase 5/6 + onda multifonte (ADR-0006, 2026-10-30).

Divergências: protocolos RTSP (D1), codec por fonte vs env (D2), `tracker_state.py` descrito como
ativo mas legado (D3), 3 containers vs 5 (D4), ausência de reconexão/orçamento (D5), de
multifonte/`session_epoch` (D6) e de watchdog (D7), `tegrastats` como "planejado" (D8), `run` e
whitelist ausentes (D9), TLS/captura só em prosa (D10).

**Impacto.** Todo o resto desta lista é lido a partir do documento. Ele subestima o nº de
serviços, ignora o supervisor e o modelo multifonte, e descreve contratos de código que foram
substituídos. É o risco de maior alavancagem e o mais barato de corrigir — por isso está em P1,
apesar de não derrubar nada em runtime.

**Decisão pendente.** `docs/architecture.md` é substituído, versionado como snapshot datado, ou
passa a ser gerado? Quem é o dono do documento?

---

### R2 — Contrato de telemetria sem schema nem versão

**Evidência [F].**
- Produtor único `log_event()` (`pipeline_real.py:204-227`), formato plano
  `{"ts","event","source_id","session_epoch",...}`.
- **3 consumidores independentes** sem import compartilhado: dashboard (`app.py:132-166`),
  watchdog (`watchdog.py:259`), switch (`switch_source_lib.py:344`).
- Sem JSON Schema, sem `schema_version`, sem dataclass, sem validação em nenhum consumidor.
- Escrita best-effort engole `OSError` sem contador (`pipeline_real.py:226-227`).
- **29 tipos de evento distintos** no arquivo; 14 chegam à UI.
- `README.md:329-390` documenta um contrato **aninhado** (`source{}`, `vision{}`, `jetson{}`…)
  que **ninguém emite**.

**Impacto.** Renomear/remover um campo (`fps_processed`, `last_frame_age_s`, `area_state`, `ts`)
degrada silenciosamente: o dashboard mostra `degraded`, o watchdog para de escalar, o switch
aprova uma troca errada. Não há como detectar a regressão sem inspecionar os três consumidores.
A divergência README↔código garante que um novo integrador implemente o formato errado.

**Decisão pendente.** O JSONL passa a ter contrato versionado e validado, ou permanece "aditivo
por disciplina" com testes de contrato? Qual é o formato canônico: plano (real) ou aninhado
(documentado)?

---

### R3 — Concentração de responsabilidades em `pipeline_real.py`

**Evidência [F].** 1215 linhas; **uma classe `VisionPipeline` com 31 métodos** (`:255-1182`);
8 imports internos (`:83-94`); cobre mídia, link dinâmico, 4 probes, classificação de bus, ciclo
de vida do preview (**8 métodos**, `:670-832`), reconexão (**4 métodos**, `:870-939`), liveness
RTSP, métricas e shutdown. Nenhuma outra unidade do repo passa de 900 linhas.

**Impacto.** É o único ponto onde os 8 módulos puros convergem e a origem de 100% da telemetria.
Qualquer mudança tem blast radius que atravessa dashboard, watchdog e fluxo de troca de fonte.
Também é o motivo estrutural de R4: o arquivo não pode ser importado (puxa `gi`/`pyds`/TensorRT),
então a suíte só consegue testá-lo lendo texto.

**Decisão pendente.** A concentração é aceita como consequência consciente de um pipeline único
de GPU, ou a fronteira "orquestração de mídia × políticas" deve ser separada? Se sim, onde está a
linha de corte? *(Não decidir nesta fase — exige proposta do solution-architect.)*

---

### R4 — Testes acoplados ao texto do código e ao estado do Git

**Evidência [F].**
- `deepstream/tests/test_pipeline_source_wiring.py`: **39 testes, 483 linhas**, lê
  `pipeline_real.py`, `deploy/docker-compose.yml` e `scripts/mem_budget.sh` como **string** e
  assere substrings literais (`:33` `"resolve_active_source(load_sources())"`, `:39-47`
  `"self.rtsp_uri = self.source.uri"`).
- `test_sources.py:631-633` verifica a existência de **outro arquivo de teste**;
  `:639-642` verifica ausência textual de `import gi`.
- `test_no_hardcoded_secrets.py:33-40` roda `git ls-files` — depende do índice do Git.
- 13 arquivos de teste; **11** usam `sys.path.insert`. Os **2 que não usam** são exatamente os
  acoplados a texto/Git: os únicos que não testam comportamento.

**Impacto.** Renomear variável local, reformatar expressão ou reordenar argumentos **reprova a
suíte sem regressão funcional** — e, pior, a suíte **passa com o pipeline quebrado** se as
strings estiverem presentes. A docstring do próprio teste (`:50-56`) documenta um caso em que
exatamente isso ocorreu. O principal amortecedor de refatoração do repo está comprometido, o que
bloqueia qualquer endereçamento de R3.

**Decisão pendente.** Ver decisão #4 em §6 — é consequência direta de R3, não uma decisão
independente.

---

### R5 — Plataforma de GPU sem fixação executável

**Evidência [F].** DeepStream 7.1 fixado **só por tag, sem digest** (`deploy/Dockerfile:1`);
`pyds 1.2.0` bem fixado por wheel vendorizado (`Dockerfile:23-25`); MediaMTX 1.21.3 bem fixado.
**TensorRT, CUDA, GStreamer, driver NVIDIA, JetPack/L4T não têm versão em nenhum artefato
executável** — só em prosa (`adr-0006:6`, `phase-1-evaluation.md:9`,
`phase-2-evaluation.md:8`). `apt-get install` sem pin (`Dockerfile:5-13`).

**Impacto.** Assimetria perigosa: o que é Python puro é reprodutível, o que é plataforma de GPU
não é. **[H]** Um host com outro L4T provavelmente constrói e sobe, e falha (ou degrada) em
runtime — a hipótese não foi testada em outro host. O ponto material é que os gates de
estabilidade da Fase 5, que são a razão de existir do projeto, só são válidos para o host onde
foram medidos, e isso não está declarado como restrição.

**Decisão pendente.** O baseline de estabilidade é válido apenas para o host atual (declarado), ou
o sistema precisa ser reprodutível em outro Jetson? Isso muda completamente o custo aceitável.

---

### R6 — Ciclo de vida do MediaMTX não supervisionado

**Evidência [F].** `mediamtx` **não tem healthcheck** (`deploy/docker-compose.yml:2-29`) e
`depends_on` de `vision-app` é sem `condition` (`:118-119`). Estado observado: `vision-mediamtx`
e `vision-capture-web` em **`Exited (255)` há 2 horas**, enquanto `vision-stability-app` estava
`Up (healthy)`. O preview falha com `rtspclientsink0` / `Could not open resource` (`live.jsonl`),
exercitando `ACTION_DROP_PREVIEW`.

**[H]** A saída 255 é a causa do `preview_branch_failed`; raiz não investigada (`docker inspect`
não executado).

**Impacto — e por que é P1.** O sistema **degrada de forma controlada** (a análise continua, o que
é um bom desenho), mas o resultado é um sistema "verde" com **vídeo morto** e nenhum alerta. Três
agravantes verificados:
1. o watchdog vigia o container da aplicação, **não** o MediaMTX nem o nginx;
2. **não existe fallback de vídeo** — sem WHEP não há HLS player (`app.js` só diagnostica);
3. `preview_branch_retry` tem teto de 6 tentativas (`pipeline_real.py:185-196`) e depois emite
   `preview_branch_unavailable` ("só volta com reinício do pipeline", `:781-787`).

Ou seja: uma queda curta do MediaMTX pode deixar o preview permanentemente morto até alguém
reiniciar — e o pipeline continuará reportando `healthy`.

**Decisão pendente.** A saúde do preview é responsabilidade do watchdog (que já fala com
`docker.sock`), do próprio pipeline (que já detecta), ou fica fora de escopo? O comportamento
correto é reiniciar MediaMTX, alertar, ou aceitar?

---

### R20 — Ausência de teste de integração e de contrato

**Evidência [F].** Toda validação de mídia é manual no container único (documentado em
`test_pipeline_source_wiring.py:11-13`). Nenhum teste garante que um campo emitido por
`log_event` chegue ao dashboard; os dois lados são testados isoladamente. Não há teste que suba
o compose. `pytest` **não foi executado** nesta análise (420 funções contadas estaticamente; não
se afirma que passam).

**Impacto.** É o multiplicador de R2: o contrato não tem schema **e** não tem teste que o
verifique. Promovido a P1 por ser o único mecanismo capaz de detectar regressão de contrato hoje.

**Decisão pendente.** Qual é o nível mínimo de teste de integração exigido antes da próxima onda?

---

### R21 — Pressão de memória unificada / OOM sem limite de container

**Evidência [F].**
- `docs/incident-log.md` **INC-004**: `nvinfer` falhou com `Error Code 1: Cuda Runtime (out of
  memory)` ao alocar ~40 MB, **apesar de ~5 GB de RAM livre**, com `lfb 10x4MB` — memória física
  fragmentada. Resolvido com `drop_caches`; reboot como alternativa definitiva.
- **Nenhum serviço do compose tem `mem_limit`, `deploy.resources` ou `shm_size`** (grep vazio).
- `vision-app` roda `privileged` + `runtime: nvidia`, e o DeepStream/TensorRT aloca NVMM contígua.
- O watchdog tem Regra 3 (RSS +15%) e Regra 4 (RAM >90%), mas **nenhuma das duas previne** —
  uma alerta, a outra reinicia o container inteiro.

**Impacto.** É a restrição física dominante do Orin (memória unificada, ~7,4 GB por relato do
ambiente) e já produziu um incidente real, com sintoma enganoso ("há 5 GB livres"). Um OOM
durante uma rodada de estabilidade invalida a medição; durante campo, derruba o serviço. É P1
porque o modo de falha já ocorreu e não há contenção arquitetural — só observação reativa.

**Decisão pendente.** A contenção é limite de recurso por serviço, política de `drop_caches`/
reboot no runbook, ou o baseline é declarado como "dependente de host recém-reiniciado"? Quem
garante `lfb` antes de uma rodada?

---

## 3. Riscos P2

### R7 — Vocabulário de eventos duplicado; descarte silencioso
**[F]** Emissor em `pipeline_real.py`; whitelist **fechada de 14 nomes** em
`dashboard/backend/app.py:77-86`; classificação por **prefixo de string** em
`switch_source_lib.py:78-84`. O arquivo tem **29 tipos distintos**; **15 nunca chegam à UI sem
erro**: `preview_branch_failed`, `preview_branch_error`* , `preview_branch_retry`,
`preview_branch_retry_failed`, `preview_branch_retry_scheduled`, `preview_branch_restored`,
`source_reconnect_action`, `source_reconnect_budget_reset`, `rtsp_reconnect_started`,
`rtsp_reconnect_resumed`, `rtsp_video_pad`, `startup`, `startup_failed`, `shutdown_requested`,
`process_exit`, `experiment_flush_skipped`, `pipeline_failed`.
(*`preview_branch_error` **está** na whitelist; `eos` também está — não são exemplos de descarte.)
**Decisão pendente:** a whitelist é curadoria intencional ou esquecimento?

### R8 — Quatro candidatos a entrypoint, `app.py` enganoso
**[F]** 1 ativo (`pipeline_real.py`), 2 legados (`pipeline.py`, `pipeline_rtsp_real.py`), 1 wrapper
(`app.py`) que importa o **legado** e não é o `CMD`. Nenhuma marcação de obsolescência no código.
Quem editar `app.py` "achando que é o boot" não altera o runtime.
**Decisão pendente:** remover, marcar como legado, ou manter como referência histórica?

### R9 — Direção invertida script → aplicação
**[F]** `scripts/switch_source_lib.py:50-54` faz `sys.path.insert` em `deepstream/` e importa
`sources`. A pureza de `sources.py` (sem `gi`/`pyds`) é o que permite a CLI rodar no host, e essa
invariante é garantida por **teste textual** (`test_sources.py:639-642`), não por fronteira.
**Decisão pendente:** `sources.py` é promovido a biblioteca compartilhada explícita, ou a CLI
passa a consumir apenas o YAML?

### R10 — Duplicação de conhecimento
**[F]** IP da LAN em 6 arquivos; ids de fonte em 5; vocabulário de eventos em 3; limiar de
"telemetria velha" em 4 valores (60/90/180/270 s). Paths MediaMTX hardcoded em 4 camadas:
rename quebra só em runtime.
**Decisão pendente:** fonte única de configuração, ou duplicação aceita e documentada?

### R11 — Dependências fantasma
**[F]** `pydantic`, `requests`, `mqtt`, `paho-mqtt` declarados (`requirements.txt`) e **nunca
importados**; nenhum broker no compose; nenhum ADR/plano menciona MQTT.
**Impacto:** superfície de instalação e, pior, sugere uma integração de mensageria que não existe
— relevante para R2, caso o MQTT tenha sido o barramento previsto.
**Decisão pendente:** MQTT é direção futura (e o JSONL é paliativo) ou resíduo a remover?

### R12 — Artefatos de runtime não versionados
**[F]** `data/model_b1_gpu0_fp16.engine` (43,8 MB, exigido por `infer.txt:5`) e
`data/parser/libnvdsinfer_custom_impl_Yolo.so` (`infer.txt:15`) não estão no Git; nada no build
avisa. `deploy/certs/` gitignored por inteiro.
**Impacto:** clone limpo não produz sistema funcional, e não há garantia de que o engine
corresponde ao `.onnx` atual.
**Decisão pendente:** build/proveniência rastreável, ou setup manual documentado como obrigatório?

### R13 — CA amarrada ao IP da LAN
**[F]** O IP entra no SAN (`generate-certs.sh:188`) e no `server_name` do nginx
(`capture-web.conf:22`). Mudar `LAN_IP` invalida a CA instalada em todos os celulares.
**Decisão pendente:** certificado por IP é aceitável (rede fixa), ou o sistema precisa sobreviver
a troca de rede?

### R14 — Observabilidade com definições contraditórias
**[F]** "Telemetria velha" = 90 s (dashboard `app.py:37`), 270 s (`app.py:96-98`), 180 s
(healthcheck `compose:122`), 60 s (watchdog `watchdog.py:73-75`). Consequência possível: compose
marca `unhealthy` enquanto o dashboard mostra `stale` (ou vice-versa).
**[H]** O `start_period: 180s` (`compose:126`) isola o boot, então **não** se pode inferir que a
janela é apertada no boot; a última observação de `unhealthy` não foi explicada.
**Decisão pendente:** qual é o SLA canônico de frescor da telemetria?

### R15 — Gate de latência declarado sem consumidor
**[F]** `gate.latency_p95_ms` existe em `sources.yaml:34-35`, `:52-54`, `:67-68` e **nenhum
código o consome**; não há timestamp por frame no caminho.
**Impacto:** o critério de aceitação de campo (p95 300 ms) é declarativo; o critério de retorno do
runbook do drone é verificado manualmente.
**Decisão pendente:** o gate é medido por sonda externa (`rtsp_read_probe`) ou instrumentado no
pipeline?

### R16 — Superfície de privilégio
**[F]** `watchdog` monta `/var/run/docker.sock` (`compose:141`) — plano de controle com poder de
recriar a aplicação; `vision-app` roda `privileged: true` (`:72`).
**[H] A justificativa do `privileged` é presumida, não verificada:** é a única ocorrência no repo,
sem comentário (todas as outras linhas não triviais do compose são comentadas) e sem ADR. Não se
demonstrou que o pipeline falha sem ele.
**Decisão pendente:** aceitar e registrar, ou verificar empiricamente e reduzir? (Ver decisão #10.)

### R22 — Dashboard sem autenticação em `0.0.0.0` + código do host RW em container privilegiado
**[F]** O dashboard é o **único** serviço publicado em `0.0.0.0` (`compose:153`) e **não tem
autenticação nem TLS** (nenhum middleware; só GET + WS, `app.py:199-240`). Em paralelo,
`../deepstream` e `../scripts` são montados **RW** (`:115-117`) dentro do container
`privileged: true`: quem comprometer o pipeline escreve no código do host que o próximo
`docker compose up` executa.
**Impacto.** Dois vetores distintos, ambos sem tratamento documentado: leitura de telemetria (e do
vídeo, se o atacante estiver na LAN) por qualquer host que alcance 8081, e escalada de container
privilegiado para execução de código no host. O projeto aceitou portas de mídia amarradas ao
`LAN_IP` (DIV-003) mas não estendeu a mesma disciplina ao dashboard.
**Decisão pendente:** o dashboard é interno-à-LAN por premissa (registrar) ou precisa de auth/
bind restrito?

### R23 — Telemetria sem rotação/retenção
**[F]** `live.jsonl` cobre 2026-08-23 → 2026-11-01 (≈4,2 MB) sem rotação em nenhum script; o
dashboard replaya o arquivo **inteiro** no boot (`app.py:132-166`). O texto de `system-map`
descrevia "tailer tolerante a rotação" para uma rotação que **não existe**.
**Impacto.** Hoje é benigno; cresce sem limite. Num sistema pensado para rodar semanas no Jetson,
o custo aparece como tempo de boot do dashboard e consumo de disco — e não há política declarada.
**Decisão pendente:** retenção é responsabilidade do pipeline (rotação), do dashboard
(leitura incremental por offset) ou de fora (logrotate)?

---

## 4. Riscos P3

| ID | Risco | Evidência | Nota |
|---|---|---|---|
| R17 | Config de fonte duplicada: `source.yaml` (INI) órfão ao lado de `sources.yaml` (real) | `source.yaml` só citado em `README.md:654` | Risco concreto de edição no arquivo errado |
| R18 | `switch.jsonl` e `drone-campo-ingest.jsonl` sem consumidor em código | `switch_source_lib.py:525`; nenhum leitor | Auditoria só para humano; formato pode divergir |
| R19 | `/healthz` do dashboard devolve constante `{"ok": true}` | `app.py:230-232` | Não reflete `state.health()`; não usado pelo compose |

---

## 5. O que NÃO é risco (decisões que estão funcionando)

Registrado para evitar que a próxima onda "conserte" o que está certo. **Revisado após verificação
independente** — um item foi corrigido e um foi rebaixado.

- **Análise sobrevive à queda do preview** — `fakesink` no ramo analítico + `ACTION_DROP_PREVIEW`
  observado em produção. Degradação controlada, comprovada por log real.
- **A reconexão da fonte é agendada, não executada no bus** — `ACTION_RECONNECT` só emite evento e
  agenda (`:646-651`); a execução roda em timer de 250 ms (`:901-971`). Evita deadlock no thread
  do bus.
  **Ressalva [F]:** o caminho `ACTION_DROP_PREVIEW` **faz trabalho de GStreamer inline**
  (`:653-655` → `:745-769`: `pad.unlink`, `release_request_pad`, `set_state(NULL)`,
  `pipeline.remove`). É protegido por `try/except` e limitado a 8 elementos, mas **não** é
  "nenhum GStreamer no callback" — é uma exceção conhecida, não medida. Registrar como risco
  residual, não como certeza.
- **EOS contido no tee** (`:430-433`, `:471-496`) — a fonte pode morrer sem matar o pipeline.
- **Troca de fonte fail-closed** — `active_source` ilegível é erro, não fallback
  (`sources.py:557-570`); precedência determinística.
- **Marcador de troca fail-open** — marcador corrompido **nunca** suprime alerta
  (`watchdog.py:369-421`). Assimetria deliberada e correta.
- **`_teardown` com teto de 1,5 s** evita exit 137 (`pipeline_real.py:1153-1181`), coerente com a
  restrição de memória unificada do Orin.
- **Isolamento dashboard** — backend não importa `deepstream`/`scripts` (`app.py:15-26`).
- **Zero ciclos de import** e módulos puros bem delimitados.

---

## 6. Decisões pendentes (consolidado)

Nenhuma destas decisões foi tomada nesta fase. As **8 primeiras são arquiteturais**; as duas
últimas são registradas como consequência/governança, não como decisão independente.

| # | Decisão | Bloqueia | Risco |
|---|---|---|---|
| 1 | O baseline de estabilidade vale **só para o host/L4T atual** ou precisa ser reprodutível? | R5, R12, R13 | R5 |
| 2 | O JSONL é **contrato versionado** ou "aditivo por disciplina"? Formato canônico: plano ou aninhado? | R2, R7, R14, R19, R23 | R2 |
| 3 | A concentração em `pipeline_real.py` é aceita ou a fronteira mídia×política deve ser separada? | R3, R4, R8 | R3 |
| 4 | *(subitem de #3)* A fragilidade da suíte textual é aceita (com ADR) ou decorre da fronteira importável? | R4, R20 | R4 |
| 5 | A saúde do preview/MediaMTX é responsabilidade de quem, e qual é o comportamento correto? | R6 | R6 |
| 6 | Qual é a contenção de memória: limite por serviço, política de host, ou baseline recém-reiniciado? | R21, R12 | R21 |
| 7 | MQTT é direção futura ou resíduo a remover? | R11, R2 | R11 |
| 8 | SLA canônico de frescor da telemetria (60/90/180/270 s)? | R14 | R14 |
| 9 | Gate de latência p95: sonda externa ou instrumentação no pipeline? | R15 | R15 |
| 10 | Privilégios (`docker.sock`, `privileged`, binds RW) aceitos e registrados, ou reduzidos após verificação empírica? | R16, R22 | R16/R22 |
| 11 | *(governança)* `docs/architecture.md` é substituído, snapshot datado ou gerado? Quem é o dono? | R1 e todos | R1 |
| 12 | *(higiene, não arquitetural)* Legados (`pipeline.py`, `pipeline_rtsp_real.py`, `app.py`, `source.yaml`) removidos ou marcados? | R8, R17 | R8 |
| 13 | *(governança)* O dashboard é interno-à-LAN por premissa registrada, ou precisa de auth/bind restrito? | R22 | R22 |

---

## 7. Lacunas que impedem graduar riscos

1. **`pytest` não executado** — não se sabe se os 420 testes passam. R4 e R20 podem estar
   subestimados.
2. **Causa raiz da saída 255** de `mediamtx`/`capture-web` não investigada (`docker inspect`).
   R6 pode ser mais grave que "sem healthcheck".
3. **Latência real fim a fim não instrumentada** — impossível avaliar R15 quantitativamente.
4. **Correspondência engine `.engine` × `.onnx` atual** não verificada (R12).
5. **Nenhuma medição de memória** desta análise (RSS, `lfb`, fragmentação): R21 é graduado por
   incidente documentado, não por medição própria.
6. **`privileged` não testado sem privilégio** — R16 depende de verificação empírica.
7. **Bloqueio do loop pelo teardown inline do preview** não medido (§5, ressalva).
8. **Rede Docker nominal/subnets** não inspecionadas.
9. **Uso operacional real dos legados** não verificado em runbooks/histórico manual (R8).
10. **Worktree divergiu durante a análise** (E4 em andamento): os documentos valem para `cd5bbfb`;
    uma parte de `dashboard/` já mudou.
