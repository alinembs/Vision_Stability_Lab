# Registro de Incidentes

## INC-001 — Relatório da Fase 1 registrava sucesso sem inferência executada
- **Data:** 2026-08-23
- **Severidade:** Alta (integridade de evidências)
- **Descrição:** O teste de 30 min documentado em `docs/phase-1-evaluation.md` foi executado pelo script antigo `deepstream/pipeline_rtsp_real.py`, que não criava `nvstreammux` nem `nvinfer` (comentário no próprio código: "ambiente sem plugins de SDK DeepStream"). Nenhum frame passou por inferência e nenhuma detecção foi contada; o relatório, ainda assim, declarava a fase concluída.
- **Ação corretiva:** `deepstream/pipeline_real.py` reescrito com a cadeia completa `rtspsrc → rtph265depay → h265parse → nvv4l2decoder → nvstreammux → nvinfer → fakesink`, probes pyds e logs JSON. Teste de ≥30 min será reexecutado com inferência ativa antes de nova conclusão.

## INC-002 — Depayloader incompatível com o codec real da câmera
- **Data:** 2026-08-23
- **Severidade:** Média
- **Descrição:** A câmera publica H.265 (`encoding-name=(string)H265`, confirmado por inspeção de caps ao vivo), mas o pipeline antigo usava `rtph264depay`/`h264parse`. O fluxo só chegou ao decoder por tolerância do parser/caps negotiation, mascarando o erro.
- **Ação corretiva:** `pipeline_real.py` usa `rtph265depay`/`h265parse`, com alternância por `RTSP_CODEC` e correção registrada na avaliação da fase.

## INC-003 — pyds ausente no container e bindings incompatíveis com DeepStream 7.1
- **Data:** 2026-08-23 (resolvido)
- **Severidade:** Média (bloqueia validação da Fase 1)
- **Descrição:** A imagem `deepstream-l4t:7.1-triton-multiarch` não traz `pyds`; o wheel oficial para Python 3.10 não existe (404 no release 1.2.2). O build dos bindings v1.1.8 falha contra os headers do DS 7.1, que renomeou as structs de tracker (`NvDsPastFrameObj{,List,Stream,Batch}` → `NvDsTargetMiscData{Frame,Object,Stream,Batch}`).
- **Resolução:** o wheel compatível existe no release **v1.2.0** (`pyds-1.2.0-cp310-cp310-linux_aarch64.whl`, DeepStream 7.1 + Python 3.10). Instalado via pip dentro do container, persistido em `deploy/wheels/` e referenciado no `deploy/Dockerfile`. O build manual do pyds 1.1.8 foi abandonado. Validado ponta a ponta no teste de 30 min da Fase 1.

## INC-004 — CUDA OOM por fragmentação de memória física do Jetson
- **Data:** 2026-08-23 (resolvido)
- **Severidade:** Média (bloqueou a primeira execução da Fase 3)
- **Descrição:** Ao subir o container da Fase 3, o `nvinfer` falhou com `NVDSINFER_CONFIG_FAILED`: o TensorRT não conseguiu deserializar o engine (`Error Code 1: Cuda Runtime (out of memory)` ao alocar ~40 MB), apesar de ~5 GB de RAM livre reportados. O `tegrastats` mostrava `lfb 10x4MB` — memória física severamente fragmentada após dias de uptime com ciclos de containers. Alocações NVMM/TRT no Jetson exigem blocos contíguos.
- **Resolução:** `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` no host liberou page cache e recompactou os blocos livres (`lfb` → 196x4MB ≈ 784 MB contíguos). O teste então passou na primeira tentativa. Reboot seria a alternativa definitiva.
- **Prevenção:** verificar `lfb` no `tegrastats` antes de testes longos (candidato a check automático no script de testes da Fase 5); considerar reboot programado antes da bateria de estabilidade.

## INC-005 — Queda de energia interrompeu sessão de implementação da Fase 4
- **Data:** 2026-08-23 (resolvido)
- **Severidade:** Baixa (sem perda de dados)
- **Descrição:** Falha de energia derrubou o host às ~13:56 (horário local), no meio da aplicação de correções de review e da documentação da Fase 4. A última mensagem da sessão ficou truncada, mas todos os arquivos editados até então foram gravados em disco íntegros.
- **Resolução:** após o retorno da energia, os containers subiram automaticamente (`restart: unless-stopped`): pipeline republicou o preview via RTSP no MediaMTX e o dashboard reconectou. Revisão pós-queda encontrou e corrigiu: race no tailer do backend entre o replay do histórico e o `seek(END)` (linhas novas podiam ser perdidas), lâmpada de memória com baseline `null`, reconexão WHEP que nunca disparava na primeira falha, duplicação de eventos a cada reconexão do WebSocket e problemas menores de acessibilidade/contraste.
- **Prevenção:** considerar UPS para o Jetson; a auto-recuperação do compose provou-se eficaz como camada única de resiliência.

## INC-006 — Fonte perdida congelava o loop GLib: `docker stop` sempre em exit 137
- **Data:** 2026-10-30 (resolvido)
- **Severidade:** Alta (indisponibilidade + quebra do contrato de encerramento)
- **Descrição:** Com a fonte perdida (publisher do drone morto) o pipeline **congelava**. Depois de `eos` + `rtsp_reconnect_started` o loop GLib deixava de despachar **tudo** por ≥ 49 s observados: `interval_metrics` (30 s), `_check_rtsp_alive` (5 s) e até o handler de `SIGTERM` recém-instalado. Medições: `docker stop` com a fonte morta levava **10,5 s** e terminava em `ExitCode 137`, **zero** `shutdown_requested`/`pipeline_stopped` e `docker events` com `kill 15 → kill 9`; a reconexão anunciada nunca acontecia e o watchdog escalava por `no_frames` (180 s) ou `crash_loop`.
- **Causa raiz (thread dump do processo congelado):** o callback do bus (`_on_source_eos`) fazia trabalho de GStreamer **no thread do loop**: `rtspsrc.set_state(NULL)` e `pad.send_event(flush_stop)`. O `flush_stop` é um evento **serializado** e `gst_pad_push_event` espera o stream lock do pad; a thread de streaming do `nvstreammux` (`nvstreammux0:sr`) mantém esse lock enquanto espera um batch que nunca chega (fonte em NULL). Pilha do thread 1 (loop/Python): `gst_pad_send_event → gst_pad_event_default → gst_pad_forward → gst_pad_push_event (×6) → pthread_mutex_lock`, com o mutex recursivo (stream lock) cujo `__owner` era o LWP `nvstreammux0:sr`, parado em `g_cond_wait` dentro de `libnvdsgst_multistream.so`. Deadlock permanente, não uma espera longa.
- **Achado secundário:** sem EOS (o publisher morre e o MediaMTX só derruba o TCP) o `rtspsrc` publica **ERROR** (`Could not open resource for reading (5)`, `Not found (3)`). Classificado como "erro da análise" isso saía com `exit 1` e o `restart: unless-stopped` transformava o caso em crash loop de ~4 s, sem gastar o orçamento de reconexões e sem chegar ao `exit 2` documentado.
- **Resolução:** (1) o callback do bus só decide e agenda; o trabalho virou fatias de 250 ms de um timer GLib persistente (`_reconnect_step`), cada fase com teto (`SOURCE_STOP_TIMEOUT_S`, `SOURCE_FRAME_TIMEOUT_S`) e `get_state(0)` sem espera; `set_state` do `rtspsrc` roda em thread própria. (2) A decisão virou máquina de estados pura (`deepstream/source_reconnect.py`, 16 testes), com desfecho obrigatório: fonte de volta (`rtsp_reconnect_resumed`) ou orçamento esgotado (`source_reconnect_exhausted` → `exit 2`). (3) Erro do `rtspsrc` passou a entrar no mesmo caminho (`bus_classifier.SOURCE_ELEMENTS`). (4) O EOS da fonte é **contido no tee** por um pad probe: sem ele os dois ramos ficam presos em fim de stream e o pipeline segue "rodando" com `fps_processed = 0` (medido: 30 fps na entrada, zero processados) — foi o que motivou, na versão anterior, o `flush_stop` que travava o loop. (5) A parada do pipeline no encerramento ganhou teto (`PIPELINE_STOP_TIMEOUT_S`) para nunca virar 137.
- **Evidência pós-correção:** queda do publisher → `interval_metrics` seguem (fps_in 0,0 e source_id=drone), reconexão em processo com `rtsp_video_pad` + `rtsp_reconnect_resumed` (mesmo `session_epoch`) e `fps_processed` = `fps_in` no retorno; `docker stop` com a fonte morta: **0,53 s / 0,60 s**, `ExitCode 0`, `pipeline_stopped exit_code=0`, e nenhum `kill 9`; orçamento esgotado: `pipeline_failed reason=source_reconnect_exhausted exit_code=2` → `die exit=2`.
- **Prevenção:** testes de fiação em `deepstream/tests/test_pipeline_source_wiring.py` proíbem `set_state`/`send_event`/`get_state` no callback do bus, `send_event` no módulo inteiro, `get_state` sem timeout e `_fail` com keyword que colida com seus parâmetros (o `TypeError` do `reason=` apareceu na primeira versão da correção e matava o `exit 2` em silêncio).


## Dívidas registradas

### DIV-001 — Credenciais RTSP hardcoded como default em `pipeline_real.py`
- **Data:** 2026-08-23
- **Tipo:** Dívida de segurança (não incidente — ainda sem exposição conhecida)
- **Descrição:** `deepstream/pipeline_real.py` usa usuário/senha da câmera como valor default de `RTSP_URI` (`rtsp://admin:...@...`). Qualquer clone do repositório expõe a credencial.
- **Ação requerida:** remover as credenciais hardcoded (exigir `RTSP_URI` via ambiente/secret) **antes** de compartilhar o repo ou iniciar a Fase 3.
- **Status:** **resolvido em 2026-10-30.** `RTSP_URI` passou a ser obrigatória via ambiente (`deepstream/rtsp_env.py`; `require_rtsp_uri()` encerra com mensagem clara se ausente). A URI saiu de `pipeline_real.py`, de `pipeline_rtsp_real.py` e dos 6 scripts `rtsp_*_test.py`/`rtsp_bin_complete.py`, e de `deploy/docker-compose.yml` (agora `${RTSP_URI:-}` a partir de `deploy/.env`, não versionado; modelo em `deploy/.env.example`). Guarda de regressão em `deepstream/tests/test_no_hardcoded_secrets.py`.
- **Pendência:** a credencial **permanece no histórico do Git**. Trocar a senha da câmera e, se o repo for publicado, considerar reescrita de histórico (`git filter-repo`).

### DIV-003 — MediaMTX sem autenticação: publish/leitura anônimos na LAN
- **Data:** 2026-10-30
- **Tipo:** Dívida de segurança (não incidente — nenhuma exposição conhecida)
- **Descrição:** O MediaMTX roda com `api: no` (`mediamtx/mediamtx.yml:24`) e **sem** `authMethod`/`authUser`/`authPass`: qualquer máquina que alcance as portas 1935 (RTMP), 8888/8889 (HLS/WebRTC), **8890/UDP (SRT)** ou 8554 (RTSP) pode **publicar** e **ler** os paths `live`, `drone` e `phone` sem credencial. O `api: no` só desliga a API de controle — não é autenticação. O SRT (`8890`) também é publicado no compose **sem autenticação nem passphrase**: além de ler o preview, qualquer host da LAN pode publicar por SRT em um path e alimentar o pipeline com vídeo forjado. Consequências: substituição do stream por terceiro (o path `live` é o preview consumido pelo dashboard), injeção de vídeo falso e leitura do vídeo da câmera/celular por quem estiver na mesma rede. Identificado em **2026-10-30**, ao revisar os contratos de ingestão do plano de troca de fonte.
- **Mitigação aplicada nesta tarefa (E0-T2):** as portas do MediaMTX deixaram de ser publicadas em `0.0.0.0` e passaram a ser amarradas ao **IP da LAN** (`${LAN_IP:-192.168.59.102}`), o que remove a exposição via `tailscale0` e as bridges do Docker. Isso reduz a superfície, **não** autentica ninguém: qualquer host da LAN confiável continua podendo publicar.
- **Ação requerida:** adicionar autenticação (`authMethod: internal` com `authUser`/`authPass`, ou `jwt`/`http` com allowlist) e/ou uma allowlist de IP por path **antes** de usar o stack fora da rede confiável (evento em campo, Wi-Fi compartilhado, VPN de terceiros).
- **Status:** aberta (mitigação parcial em 2026-10-30).
