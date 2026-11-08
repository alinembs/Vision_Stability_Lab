# E4-T1/E4-T2 — dashboard multi-fonte (implementação e validação local)

- **Data:** 2026-11-01
- **Branch:** `feature/multifonte` (working tree sobre `cd5bbfb`)
- **Escopo entregue:** E4-T1 (backend por fonte), E4-T2 (frontend: fonte ativa,
  histórico e eventos por fonte) e **E4-T3** (troca da fonte ativa pela tela,
  seção 5)
- **Decisão do operador (2026-11-01):** o **path é somente leitura** nesta
  rodada. Editar path pela tela fica para uma etapa posterior, com probe e
  override próprios.
- **Status:** implementado e validado localmente; **sem parecer independente**
  (não é gate do `deepstream-tester`: não toca `deepstream/*`).

---

## 1. O que mudou

### Backend (`dashboard/backend/`)

- **`sources_catalog.py` (novo)** — lê `deepstream/config/sources.yaml` e devolve
  `id`, `name`, `codec`, `target_fps`, `enabled`, `path`, `path_origin`,
  `path_env`.
  - `uri_env` (câmera) **não** é lida: `path` fica `null` e `path_env` diz
    `RTSP_URI` (DIV-001).
  - `uri` literal (drone/celular) é expandida (`${MTX_HOST:-mediamtx}`) e
    mascarada; nenhuma credencial sai na resposta.
  - Arquivo ausente/ilegível devolve `[]` — o dashboard não cai.
- **`app.py`** — histórico por `(source_id, session_epoch)`:
  - `sessions` guarda uma `SourceSession` por fonte; epoch novo **substitui** a
    série (D4: nada atravessa sessões).
  - `health()` e `latest_metrics` passam a olhar **apenas a sessão ativa**.
  - `snapshot()` ganhou `source: {id, name, session_epoch, since}`.
  - `/api/sources` (novo): catálogo + sessão de cada fonte + `active`. Fonte que
    só existe na telemetria aparece como cartão apenas se for a ativa ou se não
    houver catálogo (o JSONL legado, sem `source_id`, fica fora da lista).
  - `/api/history?source=` e `/api/events?source=`: filtro por fonte; id
    desconhecido devolve `[]`, não erro.
- **`requirements.txt`** — `pyyaml>=6` declarado (já vinha via
  `uvicorn[standard]`).

### Frontend (`dashboard/frontend/`)

- Painel **“fontes · uma ativa por vez”** com três cartões (ícones SVG inline de
  câmera, drone e celular), nome, `id` + codec, **path somente leitura** e
  metadados da sessão (`desde`, `epoch`, número de amostras).
- Clicar no cartão fixa a fonte: gráficos (`/api/history?source=`) e lista de
  eventos (`/api/events?source=`) passam a ser daquela fonte. O botão
  **“seguir ativa”** volta a acompanhar a fonte que o pipeline está publicando.
- Cartão da fonte ativa ganha o selo **“ativa”**; o player WHEP continua no path
  `live` (o preview é o mesmo para qualquer fonte).
- Grid e CSS passaram a usar classes (`chart-fps-panel`/`chart-rss-panel`) em vez
  de `:nth-of-type`, que quebraria com o painel novo.

### Deploy (`deploy/docker-compose.yml`)

- Serviço `dashboard`: `SOURCES_CONFIG=/app/deepstream/config/sources.yaml`,
  `MTX_HOST` repassado e `../deepstream/config` montado `ro`.
- Nenhum container DeepStream novo: só `vision-dashboard` foi recriado (regra
  global de um único container DeepStream por projeto).

---

## 2. Evidências

```text
$ python3 -m pytest dashboard/tests -q
4 passed, 8 skipped          # catálogo roda no host; estado exige fastapi

$ docker exec -w /app/dashboard/backend vision-dashboard \
      python -m unittest discover -s /app/dashboard/tests
Ran 12 tests ... OK

$ node --check dashboard/frontend/app.js
$ node --check dashboard/frontend/capture.js
(app.js ok / capture.js ok)

$ curl -s localhost:8081/api/sources
camera | ativa: True  | path: RTSP_URI (env)            | pontos: 0
drone  | ativa: False | path: rtsp://mediamtx:8554/drone | pontos: 6
phone  | ativa: False | path: rtsp://mediamtx:8554/phone | pontos: 1

$ curl -s "localhost:8081/api/history?minutes=1440&source=drone"  -> 6 pontos, ids={drone}
$ curl -s "localhost:8081/api/history?minutes=1440&source=phone"  -> 1 ponto,  ids={phone}
$ curl -s "localhost:8081/api/history?minutes=1440&source=camera" -> 0 pontos (sessão atual sem métricas)
$ curl -s "localhost:8081/api/events?limit=500&source=drone"      -> 94 eventos, ids={drone}
$ grep -iE "senha|password|://[^/]*:[^/]*@" /api/sources          -> nenhuma credencial
```

Nenhuma série de uma fonte aparece na de outra (`ids` de cada consulta tem um
único valor), incluindo duas sessões da mesma fonte (`test_new_epoch_resets_series`).

---

## 3. Estado do ambiente durante a validação (não é regressão do E4)

- `vision-stability-app` estava **sem frames da câmera**: `watchdog.jsonl`
  registra `watchdog_restart_triggered` / `no_frames_escalation` a cada ~10 min
  e o pipeline não emite `interval_metrics` desde `2026-11-01T01:41:46Z`.
- Consequência no dashboard: `health = down` e gráfico da fonte ativa vazio —
  **correto** para a sessão atual. Antes do E4 a saúde era derivada do último
  `interval_metrics` do replay (que entra com `recv_at` de agora) e podia
  aparecer “healthy” com telemetria de horas antes.
- Pendente de investigação (fora do escopo do E4): por que o pipeline não emite
  `interval_metrics` mesmo com tracks/entrada de área na mesma sessão.

---

## 4. Fora de escopo desta rodada (pendências explícitas)

1. **Editar path.** Read-only nesta rodada (decisão do operador). A camada 2
   precisa de override (`data/config/sources.local.yaml`), probe antes de
   aplicar e emenda ao ADR-0006.
2. **Parecer independente** do E4 (revisão de código) antes de marcar o épico
   como fechado no plano.
3. **Troca pela tela em produção**: implementada e ligada por overlay
   (seção 5), mas ainda **não exercitada com uma fonte entregando vídeo** —
   depende de um publisher de drone/celular ou de a câmera voltar a publicar.

---

## 5. E4-T3 — trocar a fonte ativa pela tela

Implementado em 2026-11-01, na mesma rodada, a pedido do operador. O path
segue **somente leitura**: o que a tela ganhou foi o **botão de troca**, não a
edição de URI.

### 5.1 Decisão de arquitetura

A troca **continua sendo a de D4** (ADR-0006): escrever
`data/config/active_source` e reiniciar o processo. A3 (endpoint que
reconfigura o pipeline em runtime) **segue descartada** — o dashboard não fala
com o GStreamer do pipeline, ele reinicia o processo como o script faz.

Para não existir um segundo caminho de troca, o backend **reusa** o
`scripts/switch_source_lib.py`: `resolve_source` (id válido e habilitado no
YAML), `active_source_conflict` (guarda de precedência do `ACTIVE_SOURCE`),
`wait_for_source` com `process_boundary` (só PASS se a amostra veio do processo
novo) e `decide_rollback`. A parte "impura" é que difere:

| | `scripts/switch_source.sh` | `POST /api/source/switch` |
|---|---|---|
| reinício | `docker compose up -d --force-recreate` | `docker restart` (o volume relê o arquivo) |
| pré-check | `rtsp_read_probe.py` no **host** (tem GStreamer) | `scripts/probe/source_probe.py` **dentro do `vision-app`** via `docker exec` |
| auditoria | `data/logs/switch.jsonl` | mesma linha, com `"origin": "dashboard"` |
| marcador | `data/config/switch_in_progress` | idêntico (é o que impede o watchdog de escalar restart) |

O pré-check dentro do container é o único jeito: o dashboard não tem GStreamer,
e o ambiente do `vision-app` é o que o pipeline vai usar depois do restart
(`RTSP_URI`, `MTX_HOST`, `SOURCES_CONFIG`), não o `deploy/.env` do host.

### 5.2 Arquivos

- **`scripts/probe/source_probe.py` (novo)** — sonda RTSP por **id** dentro do
  container; expõe `ok`/`reason`/`masked_uri` (DIV-001: só URI mascarada);
  exit `0` aprovado, `4` reprovado, `2` id/config inválidos.
- **`dashboard/backend/switch_control.py` (novo)** — `Switcher` (uma troca por
  vez, thread própria), `DockerRuntime` (SDK do Docker, import tardio),
  `SwitchStatus`, `switch_record` com `origin=dashboard`.
- **`dashboard/backend/app.py`** — `POST /api/source/switch` (202, assíncrono) e
  `GET /api/source/switch` (status público, sem token).
- **`deploy/docker-compose.switch.yml` (novo)** — overlay opt-in: socket do
  Docker, `data/config` e `data/logs` graváveis, `scripts/` e `deepstream/` em
  `ro`, `SWITCH_TOKEN`.
- **`dashboard/frontend/`** — cartão com dois alvos separados ("ver" e
  "ativar"), confirmação com aviso de restart, token em `sessionStorage` (nunca
  no DOM nem em `localStorage`), banner de andamento e polling de 2 s.
- **`dashboard/tests/test_switch_control.py`** e **`dashboard/tests/ui_smoke.js`**.

### 5.3 Segurança

- **Opt-in por token**: `SWITCH_TOKEN` vazio → `503 switch_token_ausente`;
  `SWITCH_ENABLED=0` → `503` mesmo com token; token errado → `401` comparado
  com `secrets.compare_digest`. Nada muda no deploy atual até o overlay ser
  usado.
- **Socket do Docker** só no overlay — dívida **DIV-004** registrada em
  `docs/incident-log.md` (acesso root-equivalente; um dashboard comprometido
  vira root no host).
- O `GET` de status não exige token e não devolve segredo; o `POST` nunca ecoa
  o token, e o teste de UI verifica que ele não aparece no DOM.

### 5.4 Evidências

```text
$ python3 -m pytest dashboard/tests -q
17 passed, 17 skipped, 6 subtests passed        # host: Switcher + catálogo

$ docker exec -w /app/dashboard/backend vision-dashboard \
      python -m unittest discover -s /app/dashboard/tests
Ran 34 tests ... OK                             # + rotas (token, 409, 400, 503)

$ NODE_PATH=/tmp/uicheck/node_modules node dashboard/tests/ui_smoke.js
28/28 verificações passaram                     # render, token, POST, polling, 401

$ curl -s localhost:8081/api/source/switch      # sem SWITCH_TOKEN
{"enabled": false, "disabled_reason": "switch_token_ausente", ...}

$ curl -o /dev/null -w '%{http_code}' -X POST .../api/source/switch
503                                             # feature desligada

# com o overlay ligado (token de teste) e troca real pedida para `drone`:
POST /api/source/switch {"source_id":"drone"}   -> 202
GET  /api/source/switch  -> state=failed,
     reason="precheck:pipeline_error: Could not open resource for reading and writing."
active_source=camera  restart_count=0  marcador ausente
data/logs/switch.jsonl: {"to":"drone","probe":"failed","result":"FAIL",
                         "reason":"precheck:...","origin":"dashboard"}
```

A última linha é o resultado **esperado** no ambiente de hoje: o `drone` não
tem publisher, então o pré-check reprova e **nada é escrito nem reiniciado** —
exatamente o contrato do D7. O caminho de sucesso (pré-check aprovado →
`active_source` → restart → `interval_metrics` do processo novo) está coberto
pelos testes com runtime dublê (`SwitcherHappyPathTest`), mas ainda **não** foi
visto com vídeo real nesta máquina.

### 5.5 Como ligar

```bash
cd deploy
SWITCH_TOKEN="$(openssl rand -hex 24)" \
  docker compose -f docker-compose.yml -f docker-compose.switch.yml up -d dashboard
# na tela: botão "destravar troca" -> cole o token -> "ativar" no cartão
```

Para desligar, suba de novo **sem** o overlay (`docker compose up -d dashboard`):
o container volta a ficar sem socket e sem token.

