# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A autora do projeto, sozinha, em bancada: acompanha sessões de teste na Jetson
Orin Nano — inclusive rodadas noturnas de 6 h —, confere a saúde do pipeline,
troca a fonte de vídeo e lê gráficos e log de eventos no navegador da LAN.
Uso pessoal de laboratório; não há operador terceirizado nem audiência de
produção.

## Product Purpose

Tornar visível, enquanto o pipeline roda, se o monitoramento contínuo de
ocupação com DeepStream permanece estável: vídeo anotado, estado da área, FPS,
RSS, filas, eventos e saúde do sistema em uma única tela. Sucesso significa
deixar o sistema rodando por horas e, ao olhar o dashboard, saber se ele está
saudável ou degradando — com evidência que sobrevive ao fim da sessão (JSONL +
relatórios).

## Positioning

O produto não vende detecção de pessoas; ele torna **verificável a estabilidade
de longa duração** de um pipeline de visão embarcado. O mecanismo é
observabilidade derivada do próprio pipeline — telemetria JSONL por fonte e por
sessão, ring buffers, replay na partida — acoplada ao stack real na Jetson. Todo
número exibido vem de
uma execução medida, não de estimativa ou demonstração sintética.

## Operating Context

- **Host:** Jetson Orin Nano, DeepStream 9.1, stack Docker (`vision-app`,
  `mediamtx`, `dashboard`). O `Makefile` é a entrada única: `make up`, `status`,
  `health`, `switch`, `logs`, `test`.
- **Acesso:** navegador da LAN em `http://<ip-do-host>:8081` (`make ip` mostra o
  endereço atual — o IP não é fixo no repositório, vem do DHCP); WebRTC/WHEP em
  TLS na 8889 exige a CA interna instalada na máquina que abre o player; HLS na
  8888 é fallback; o celular publica por `https://…:8444/capture.html` (WHIP).
- **Rotina de teste:** baseline 30 min, longa duração 6 h (madrugada), viewer
  lento, RTSP interrompido, dashboard desligado, reinício controlado. Uma fonte
  ativa por vez, trocada pela tela (opt-in com `SWITCH_TOKEN`) ou por
  `make switch`.
- **Fontes reais:** câmera fixa RTSP, drone (RTMP → MediaMTX) e celular. Cada
  troca abre sessão nova (epoch novo); nunca há reconfiguração a quente.
- **Evidência:** ADRs em `docs/adr/` e procedimentos em `docs/runbooks/`; os
  números medidos são os registrados neste documento.

## Capabilities and Constraints

- **Pipeline:** RTSP → decode por hardware → `nvinfer` (YOLO11m fp16, classe
  única `person`) → `nvtracker` IOU com TTL → regra temporal `EMPTY`/`OCCUPIED`
  → `nvdsosd` → H.264 por hardware → MediaMTX. O ramo de preview é descartável
  (leaky queue) e não pode bloquear a inferência.
- **Dashboard:** backend FastAPI + SPA vanilla; `/api/snapshot`,
  `/api/history?source=`, `/api/events`, WebSocket `/ws`; ring buffers de 480
  amostras e 500 eventos; saúde `healthy`/`stale`/`down` derivada do frescor da
  telemetria (<90 s) e de `fps_processed > 0`.
- **Sem persistência além do JSONL:** o histórico dos gráficos é reconstruído
  por replay do arquivo na partida do backend. SQLite é fase futura.
- **Restrições reais:** uma fonte ativa por vez; catálogo com path somente
  leitura; troca desligada por padrão (503 sem token) com rollback quando o
  pós-check não confirma frames; nenhum host hardcoded no frontend (a URL do
  WHEP deriva de `location.hostname`).
- **Vocabulário:** UI em português técnico; `fps proc.`, `RSS`, `tracks`,
  `EMPTY`/`OCCUPIED`, “ocupado”/“vazio” são termos do domínio, não jargão a
  traduzir.
- **Fase 5 em andamento:** falta o teste de estresse (2 h com alta ocupação),
  cancelado por cena vazia; os demais testes estão concluídos e aprovados.

## Brand Commitments

- Nome: **Vision Stability Lab** — “sistema de monitoramento contínuo de
  ocupação com DeepStream e observabilidade embarcada”.
- Voz: técnica, sóbria e honesta sobre falhas — incidentes, ressalvas e
  pendências ficam registrados em público, não maquiados.
- Idioma do produto: português do Brasil.

## Evidence on Hand

- Números medidos em execução real, registrados neste documento — por exemplo
  baseline de 30 min com 19,83 fps médio e RSS 782 → 891 MB, e longa duração de
  6 h com platô de ~947 MB e zero erros CUDA/GStreamer.
- Decisões em `docs/adr/` e procedimentos em `docs/runbooks/` (captura pelo
  celular, drone em campo, troca de fonte).
- **Ausências deliberadas:** não há screenshots, fotos do sistema, depoimentos,
  clientes, benchmarks de terceiros nem promessas de SLA. Nada disso deve ser
  inventado.

## Product Principles

1. **Estabilidade é o produto:** toda tela responde “isto está saudável?” antes
   de qualquer outra coisa.
2. **Número sem fonte é ruído:** métrica exibida precisa vir da telemetria real
   e ser rastreável ao log que a produziu.
3. **Uma fonte, uma sessão:** a interface nunca sugere paralelismo ou
   reconfiguração a quente que o hardware não suporta.
4. **Falha aparece:** degradação é visível e honesta (`stale`/`down`, rollback,
   incidentes) em vez de escondida atrás de um estado verde.
5. **Bancada primeiro:** densidade e legibilidade para leitura de um monitor à
   distância de bancada — não para vitrine.

## Accessibility & Inclusion

Sem padrão formal acordado. Necessidades reais observadas: leitura à distância
de bancada, operação da página de captura no celular em campo e controles com
estado exposto (`aria-pressed`, `aria-live`, rótulos nos canvas).
