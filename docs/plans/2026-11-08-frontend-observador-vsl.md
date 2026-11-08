# Plano — Tarefa 3: painel do observador de fontes no dashboard

- **Data:** 2026-11-08
- **Branch:** `feature/dashboard-caderno-de-bancada`
- **Escopo:** mostrar no dashboard o que o observador de fontes (watchdog) já
  publica em `data/config/observer_state.json`, sem tocar no pipeline e sem
  criar estado novo no backend.
- **Depende de:** tarefas 1 e 2 (já no working tree): `scripts/observer_policy.py`,
  escrita atômica do `observer_state.json` em `scripts/watchdog.py:1771-1799` e o
  campo aditivo `observer` no snapshot do backend (`dashboard/backend/app.py:364-387`).
- **Fora de escopo:** abrir as tarefas 4 e 5 (ver §6), mexer em `observer_policy.py`,
  no watchdog, no contrato do backend, ou em `capture.html`.

> Rascunho anterior do planner usou numeração 0–6, que colide com as tarefas 1–5 do
> brief. Aqui a numeração é **T3.x**: são os passos desta tarefa 3.

---

## 1. Decisões de UX (adotadas)

| # | Decisão | Adotada | Motivo |
|---|---|---|---|
| D1 | Onde vive o painel | **Nova linha abaixo dos gráficos** (`grid-row: 4`), empurrando o log de eventos para `grid-row: 5` | Não comprime `state-card`/`health-card`, que já têm 4 linhas de conteúdo cada; um cartão full-width acomoda “modo + motivo + última ação + sonda por fonte” sem quebrar em 3 colunas. |
| D2 | Como a sonda aparece | **Só vivo / morto / não sondada** na superfície | `level` 1/2 é detalhe de implementação da sonda (`scripts/observer_policy.py:102-108`): nível 1 é só DESCRIBE, não prova vídeo. Exposto por extenso, ensina o operador a ler um sinal que não significa “vídeo ok”. `level` continua visível no `title` do chip (detalhe sob demanda, sem poluir). |
| D3 | Cor da lâmpada em `standby_no_sources` | **Verde quando `available: true`** | `standby_no_sources` é estado *esperado* (modo sem fonte ativa, sem ação pendente), não degradação. O que merece amarelo é **não ter observador confiável** (`available: false`) e vermelho, nada — a tela já usa vermelho para falha de pipeline/vídeo/memória. |

Consequência de D3, registrada para não ficar implícita: a lâmpada nova
`observador` tem três tons — `on` (verde) para `watching` e `standby_no_sources`,
`warn` (amarelo) para `switching` e para `available: false` (com o motivo no
`title` e no cartão), e nunca `off`.

---

## 2. Contrato consumido (não alterar)

`GET /api/snapshot` → `snapshot.observer` (`dashboard/backend/app.py:364-387`).

```json
{
  "available": true,
  "mode": "watching|standby_no_sources|switching",
  "mode_reason": "...",
  "last_action": "none|...",
  "last_action_ts": "2026-11-08T12:00:00+00:00",
  "ts": "...", "age_s": 3.2,
  "sources_probe": {"camera": {"alive": true, "level": 2, "reason": null, "checked_at": "..."}},
  "reason": null
}
```

`available: false` vem com as mesmas chaves (`mode: null`, `sources_probe: {}`) e o
motivo em `reason` (`app.py:75-93`) — a tela **não** pode tratar isso como erro nem
inventar valores.

Segunda fonte de verdade, já existente e independente deste painel:
`GET /api/sources` → cada entrada traz `observer_probe`
(`status: measured|sem observador|nao_sondada`, `app.py:165-188`). O painel lê a
sonda do `snapshot.observer.sources_probe` para não acoplar o desenho ao
`refreshSources()`; o `observer_probe` por fonte fica como está (sem badge novo no
cartão da fonte nesta tarefa).

---

## 3. Passos

### T3.1 — Marcação do painel (`dashboard/frontend/index.html`)

- Nova `<section class="card observer-panel" id="panel-observador">` **depois** de
  `chart-rss-panel` (após `index.html:184`) e **antes** de `events-panel`.
- `panel-head` com eyebrow “observador de fontes” + um `<span class="chart-max">`
  (ou classe equivalente já existente) com `id="observer-mode"` para o modo.
- Corpo com `state-rows` (padrão de `health-card`, `index.html:161-166`):
  - `motivo` → `#observer-reason`
  - `última ação` → `#observer-action`
  - `retrato` → `#observer-age` (ex.: “há 3 s”)
  - `sonda por fonte` → `#observer-probes` (container dos chips, `aria-live="polite"`)
- Link novo em `.side-nav` (`index.html:59-65`): `#panel-observador` → “observador”.
- Lâmpada nova em `#lamps` (`index.html:68-73`): `<span class="bulb" id="lamp-observer">`
  com tag `observador` (5º item; o grid de 2 colunas já comporta).

Aceite: HTML parseável, `id`s únicos, nenhuma remoção de `id` existente.

### T3.2 — Layout (`dashboard/frontend/style.css`)

- Desktop (regras de `style.css:570-575`): `.observer-panel { grid-column: 1 / -1; grid-row: 4; }`
  e `.events-panel { grid-row: 5; }`.
- Breakpoint de 6 colunas (`style.css:955-966`): `.observer-panel { grid-column: 1 / -1; grid-row: 5; }`,
  `.events-panel { grid-row: 6; }`, e `grid-template-rows` com um `auto` a mais.
- Breakpoint móvel (`style.css:984-993`): `.observer-panel { grid-row: auto; }`.
- Chips de sonda: classe nova `.observer-probe` reaproveitando tokens
  (`--accent`, `--bad`, `--dim`, `--panel-2`); estados `.alive`, `.dead`, `.unknown`.
  Sem cor literal — o CSS é a fonte (regra do tema em `index.html:8-19`).
- Alturas: o cartão não pode ganhar `min-height` fixo; conteúdo curto = cartão curto.

Aceite: em 1440px, 6 colunas e móvel nada sobrepõe; sem `!important`; sem cor hex nova
fora dos tokens.

### T3.3 — Render (`dashboard/frontend/app.js`)

- `$()` helper e `fmtAgo` já existem — reusar.
- `function renderObserver(observer)` chamada de `updateDerived(snap)`
  (`app.js:~953`), imune a `snap.observer` ausente:
  - `available !== true` → modo “sem observador”, motivo = `observer.reason`,
    sonda “—”, lâmpada `warn` com `title` do motivo. Nunca lançar.
  - modo → `MODE_LABELS = { watching: "vigiando", standby_no_sources: "standby — sem fonte ativa", switching: "troca em andamento" }`,
    valor desconhecido cai no literal cru (não vira “—”).
  - `mode_reason` vazio → “—”.
  - `ultima ação` = `last_action` + `hora` de `last_action_ts` (`fmtClock` se existir;
    `last_action === "none"` ou nulo → “— nenhuma").
  - `retrato` = `há ${fmtAgo(age_s)}`; `age_s` nulo → “—”.
  - `sources_probe`: um chip por chave, **ordenado por id**, rótulo
    `nome da fonte (name de state.sources) + vivo|morto|não sondada`; `title` =
    `reason ?? ""` + `level` (D2). `alive` ausente/nulo → “não sondada”.
  - Lâmpada: `setLamp("lamp-observer", ...)` com a tabela de D3.
- Re-render sem thrash: só reescrever `textContent`/chips quando o payload mudou
  (guardar `state.observerKey = JSON.stringify(observer)`), para o `aria-live` não
  falar a cada snapshot (~1 s).

Aceite: `node --check dashboard/frontend/app.js` limpo; `renderObserver` não referencia
`document` no topo do módulo (é chamado só em runtime).

### T3.4 — Smoke de DOM (`dashboard/tests/ui_smoke.js`)

- Estender o dublê de `window.fetch`/`WebSocket` para entregar um snapshot com
  `observer` — o teste nunca chamou `renderSnapshot` até hoje; chamar
  `window.renderSnapshot(...)` diretamente é o caminho mais barato (as funções de
  `app.js` ficam globais por `window.eval(source)`, `ui_smoke.js:71-96`).
- Verificações novas (mínimo 6):
  1. `available: false` → painel diz “sem observador” e lâmpada `warn`;
  2. `mode: "standby_no_sources"`, `available: true` → lâmpada **`on`** (D3) e
     “standby — sem fonte ativa”;
  3. `mode: "watching"` → lâmpada `on`;
  4. chips por fonte: 1 vivo, 1 morto, 1 não sondada — contagem e classes;
  5. chip **não** expõe “nível 1/2” no texto visível, mas o `title` carrega o `level` (D2);
  6. `available: false` não apaga o resto da tela (contagem de cartões de fonte intacta).
- Manter as 28 verificações atuais passando.

Aceite: `NODE_PATH=/tmp/uicheck/node_modules node dashboard/tests/ui_smoke.js` com
`0` de falha quando jsdom existir; sem jsdom, o script continua saindo `0` com aviso.

### T3.5 — Paridade de id/contrato

- `grep` de que nenhum `id` novo colide e de que todo `getElementById`/`$("...")` novo
  tem nó correspondente no HTML.
- Conferir que `dashboard/frontend/capture.html` e `codec-gate-selftest.html` não
  incluem este painel (não devem).

### T3.6 — Verificação final

```bash
node --check dashboard/frontend/app.js
NODE_PATH=/tmp/uicheck/node_modules node dashboard/tests/ui_smoke.js   # jsdom
python3 -m pytest dashboard/tests -q                                    # host: parte do catálogo
git diff --stat
```

Backend não muda nesta tarefa — se `git diff dashboard/backend/app.py` crescer, é
regressão de escopo.

---

## 4. Critério de aceite da tarefa

1. Com `observer_state.json` presente e fresco, a tela mostra modo, motivo, última
   ação, idade do retrato e um chip por fonte sondada, com lâmpada verde.
2. Sem observador (arquivo ausente/velho), a tela diz “sem observador” com o motivo e
   lâmpada amarela — **sem** erro no console e **sem** sumir com telemetria/fontes.
3. `standby_no_sources` com `available: true` pinta a lâmpada de verde (D3).
4. Nenhum texto visível fala em “nível 1/2” (D2).
5. `node --check` limpo e smoke de DOM sem falhas.

---

## 5. Riscos

- **Re-render a cada snapshot**: `renderSnapshot` roda por mensagem WS; sem guarda, o
  `aria-live` do painel vira metralhadora. Mitigado por `state.observerKey` (T3.3).
- **Duas fontes de sonda**: `snapshot.observer.sources_probe` e `/api/sources.observer_probe`
  podem discordar por segundos (cache de 1 requisição, `app.py:72`). O painel assume o
  snapshot como verdade; a divergência não é erro.
- **Grid de 3 breakpoints**: `.content` define `grid-row` explícito por cartão; esquecer
  um breakpoint faz o painel sobrepor o log de eventos. Coberto em T3.2.

---

## 6. Pendências que **não** entram aqui

- **Tarefa 4 (troca manual)** e **tarefa 5 (roteiro)**: não abrir nesta tarefa. O texto
  do planner que as dava como entregues em “E4-T3” está **parcialmente confirmado**:
  a troca manual pela tela é de fato o **E4-T3**, entregue em 2026-11-01
  (`docs/plans/2026-10-30-troca-de-fonte-vsl.md:396` e `docs/incident-log.md:88-90`,
  código em `app.js:562-794`); **“roteiro” não aparece em lugar nenhum do repositório**
  como funcionalidade. As duas seguem pendentes até arbitragem humana.
