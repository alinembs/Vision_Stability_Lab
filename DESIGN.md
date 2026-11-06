---
name: Vision Stability Lab
description: Bancada de instrumentos para provar estabilidade de pipeline DeepStream ao longo de horas — papel claro de dia, chassi escuro à noite.
colors:
  paper: "#f4f1ea"
  card: "#fffdf9"
  card-raised: "#f8f3eb"
  hairline: "#e4ddd2"
  seam: "#ece5da"
  ink: "#16181d"
  muted: "#6d675e"
  faint: "#776f63"
  action: "#1f6b4a"
  action-hover: "#174f37"
  action-border: "#9dc4b2"
  action-wash: "#e7f3ec"
  on-action: "#ffffff"
  ok: "#1f6b4a"
  warn: "#8a5a0f"
  warn-wash: "#f8efd8"
  bad: "#a32622"
  bad-wash: "#f8e6e4"
  info: "#2f5f86"
  axis: "#6d675e"
  video-pit: "#1b1e24"
  video-letterbox: "#0c0e12"
  video-veil: "#12151b"
  on-video: "#f4f1ea"
  lamp-rest: "#d8d1c4"
  lamp-rest-border: "#b7ae9f"
  lamp-on-border: "#1f6b4a"
  lamp-fail-border: "#7d1d1a"
  alert-wash: "#f8e6e4"
  alert-text: "#7d1d1a"
colors-dark:
  paper: "#101114"
  card: "#181b21"
  card-raised: "#1f232b"
  hairline: "#2a2e36"
  seam: "#23272e"
  ink: "#f3f1ea"
  muted: "#a39c91"
  faint: "#8e877c"
  action: "#8dceb0"
  action-hover: "#a7dcc4"
  action-border: "#3f6b57"
  action-wash: "#1c2c24"
  on-action: "#102018"
  ok: "#8dceb0"
  warn: "#e2b15a"
  warn-wash: "#2c2416"
  bad: "#f0a29c"
  bad-wash: "#2c1817"
  info: "#8fb8e0"
  axis: "#8e877c"
  video-pit: "#0c0e12"
  video-letterbox: "#000000"
  video-veil: "#05070a"
  on-video: "#f3f1ea"
  lamp-rest: "#2a2f37"
  lamp-rest-border: "#3a404a"
  lamp-on-border: "#2c9668"
  lamp-fail-border: "#b03a34"
  alert-wash: "#2c1817"
  alert-text: "#f0a29c"
typography:
  display:
    fontFamily: "IBM Plex Sans Condensed, sans-serif"
    fontSize: "52px"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "0.02em"
  stat:
    fontFamily: "IBM Plex Sans Condensed, sans-serif"
    fontSize: "30px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "0"
  headline:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    letterSpacing: "-0.01em"
  title:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    letterSpacing: "0"
  body:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "14px"
    fontWeight: 400
  label:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "10px"
    fontWeight: 500
    letterSpacing: "0.08em"
  label-sm:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "9px"
    fontWeight: 500
    letterSpacing: "0.06em"
  label-md:
    fontFamily: "IBM Plex Sans, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    letterSpacing: "0.02em"
  data-sm:
    fontFamily: "IBM Plex Mono, monospace"
    fontSize: "12px"
    fontWeight: 400
    letterSpacing: "0"
  readout:
    fontFamily: "IBM Plex Mono, monospace"
    fontSize: "15px"
    fontWeight: 500
    letterSpacing: "0"
rounded:
  sm: "6px"
  md: "10px"
  lg: "12px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "14px"
  xl: "22px"
components:
  sheet:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "14px 16px"
  stat-card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    typography: "{typography.stat}"
    rounded: "{rounded.lg}"
    padding: "12px 14px"
  button-default:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
    typography: "{typography.label-md}"
    rounded: "8px"
    padding: "6px 12px"
  chip-default:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "3px 9px"
  chip-active:
    backgroundColor: "{colors.action-wash}"
    textColor: "{colors.action}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "3px 9px"
  input-text:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.data-sm}"
    rounded: "8px"
    padding: "6px 8px"
---

# Design System: Vision Stability Lab

## Overview

**Creative North Star: "O Caderno de Bancada"**

A tela é uma folha de caderno de laboratório, não um painel de ficção
científica. O fundo é papel quente; os painéis são folhas apoiadas com uma
sombra curta; os números são anotações de instrumento em tinta. À noite, o mesmo
caderno muda para o chassi escuro do turno de madrugada — não é outro produto,
é a mesma página sob outra luz. A alternância de tema é um controle real, não
decoração: o padrão é claro porque a bancada diurna é o caso comum, e o escuro
existe para as seis horas de teste noturno.

A sidebar é a **mesa de fontes**: marca, catálogo de fontes com sessão e troca,
sumário da página, as quatro lâmpadas de saúde e os atalhos de bancada. O
conteúdo principal é a leitura: um resumo em quatro cartões, vídeo anotado,
estado da área, saúde, dois gráficos e o log de eventos. A hierarquia vem de
posição e tamanho de leitura, não de ornamento.

O sistema é contido por decisão. Cor aparece quando carrega estado ou ação:
verde de ação para o que se mede e o que se aciona, âmbar para atenção, vermelho
para falha, azul estreitíssimo para transição de estado. Sombra existe só para
descolar a folha do papel; no escuro ela desaparece. Movimento é curto e
informativo: lâmpada piscando, marcador de troca em andamento.

**Key Characteristics:**
- Papel claro por padrão, chassi escuro opcional; ambos com a mesma estrutura.
- Sidebar como mesa de fontes e sumário, não como menu de aplicação.
- Quatro cartões de resumo no topo; o resto é leitura detalhada.
- Verde de ação como acento; âmbar/vermelho/azul só como estado observado.
- IBM Plex Sans para interface, Condensed para leitura grande, Mono para dado.
- Uma tela no desktop; rolagem só dentro do log de eventos e na sidebar.

## Colors

### Primary

- **Verde de Ação** (`{colors.action}`): o acento do sistema. Marca número
  medido (FPS, RSS, uptime), estado saudável, seleção de fonte, botão primário e
  foco. É também o `ok` — saúde e ação compartilham o verde porque ambas são
  "pode confiar".
- **Verde de Ação Escuro** (`{colors.action-hover}`): hover do botão primário.
- **Borda de Ação** (`{colors.action-border}`): borda de chip ativo, item
  selecionado e botão secundário. Existe para que o verde cheio signifique
  "ativo", não "contorno".
- **Lavagem de Ação** (`{colors.action-wash}`): fundo de seleção, chip ativo e
  destaque de estado ocupado. É verde rebaixado a papel, não um segundo verde.

### Neutral

- **Papel** (`{colors.paper}`): fundo do documento, do topbar e do footbar.
- **Folha** (`{colors.card}`): superfície de cartão e da sidebar. A camada de
  trabalho.
- **Folha Elevada** (`{colors.card-raised}`): hover de cartão de fonte e de
  link de navegação.
- **Hairline** (`{colors.hairline}`): borda de cartão, chip e campo; fundo dos
  grids de 1px.
- **Costura** (`{colors.seam}`): divisor interno de linha de estado e de item
  de evento — um passo abaixo do hairline.
- **Tinta** (`{colors.ink}`): texto de dado e de corpo. Nunca branco puro; o
  branco pertence ao texto sobre verde de ação.
- **Rótulo Apagado** (`{colors.muted}`): rótulo uppercase, linha de estado,
  descrição secundária.
- **Rótulo Desbotado** (`{colors.faint}`): timestamp, nota de rodapé, metadado
  de fonte. Nunca em número que o operador precisa comparar.
- **Rótulo de Eixo** (`{colors.axis}`): escala dentro do canvas; nunca em DOM.

### Estado

- **Verde de Aprovado** (`{colors.ok}`): lâmpada acesa, área ocupada, status
  `healthy`, banner de troca concluída.
- **Âmbar de Atenção** (`{colors.warn}`): área reservada a atenção real —
  `degraded`/`stale`, pico de memória, banner de troca em andamento.
- **Lavagem de Atenção** (`{colors.warn-wash}`): fundo de banner em andamento.
- **Vermelho de Falha** (`{colors.bad}`): falha ativa — erro de pipeline,
  status `down`, token recusado, idade de telemetria acima de 90 s.
- **Lavagem de Falha** (`{colors.bad-wash}`): fundo de alerta e de banner de
  erro.
- **Azul de Transição** (`{colors.info}`): uso estreito e deliberado. Marca o
  ponto de evento de transição de área e a série de RSS no gráfico. Não é um
  segundo acento.

### Superfícies de vídeo e hardware

- **Poço de Vídeo** (`{colors.video-pit}`): fundo da moldura 16:9. Mais escuro
  que qualquer painel, para o vídeo parecer embutido.
- **Letterbox** (`{colors.video-letterbox}`): preto atrás do frame quando o
  vídeo não preenche a moldura.
- **Véu de Sinal Ausente** (`{colors.video-veil}`): camada sobre o vídeo sem
  stream; o texto de estado usa `{colors.on-video}`.
- **Corpo de Lâmpada** (`{colors.lamp-rest}`) e **Borda de Lâmpada**
  (`{colors.lamp-rest-border}`): a lâmpada sem corrente. Só o componente de
  status consome estes tokens.
- **Aro de Lâmpada Acesa** (`{colors.lamp-on-border}`) e **Aro de Falha**
  (`{colors.lamp-fail-border}`): o anel que separa o ponto do papel.
- **Lavagem de Alerta** (`{colors.alert-wash}`) e **Texto de Alerta**
  (`{colors.alert-text}`): faixa de erro da página de captura.

### Named Rules

**The Measured Green Rule.** Verde toca o que foi medido ou o que se pode
acionar. Se um elemento não tem número nem ação, ele não é verde.

**The Status Is Real Rule.** Âmbar, vermelho e azul só aparecem quando existe
estado observado. Sem telemetria a tela mostra `—` e rótulo apagado; não inventa
cor "neutra de categoria".

**The Same Page at Night Rule.** O tema escuro troca valores de token, nunca
estrutura, componente ou semântica. Componente que só existe em um dos temas
está errado.

## Typography

**Interface:** IBM Plex Sans (com system sans de fallback).
**Leitura grande:** IBM Plex Sans Condensed.
**Dado:** IBM Plex Mono.

**Character:** a sans carrega a interface porque o produto é uma ferramenta de
trabalho, não um mostruário; a condensada dá voz aos números grandes (palavra de
área, valor de cartão) sem competir com o corpo; a mono fica estritamente em
dado, endereço, timestamp e eixo de gráfico. Nenhuma das três é decoração.

### Hierarchy

- **Display** (600, 52px, tracking .02em): a palavra de estado da área
  (`OCUPADO` / `VAZIO`). O maior objeto tipográfico da tela; muda de cor com o
  estado, não de tamanho.
- **Stat** (600, 30px, tabular): valor dos quatro cartões de resumo. É a leitura
  de dez segundos: FPS, RSS, tracks, erros.
- **Headline** (600, 16px): título da página no topbar. Uma vez por página.
- **Title** (600, 13px): nome da fonte, cabeçalho de cartão e botão.
- **Body** (400, 14px): corpo e texto de dado em linha.
- **Label** (500, 10px, tracking .08em, caixa alta): rótulo de cartão de
  resumo, cabeçalho de painel, tag de lâmpada.
- **Escala densa:** 9px metadado e badge, 10px rótulo e nota, 11px linha de
  estado e chip, 12px dado denso, 14px corpo. Passos de 1px, não de razão: cada
  passo existe porque uma coluna precisou caber.

### Named Rules

**The Tabular Numbers Rule.** Todo número que atualiza ou se compara em coluna
usa `tabular-nums`. O valor não pode dançar quando a telemetria chega.

**The Uppercase Is Short Rule.** Caixa alta pertence a rótulo e a palavra de
estado de uma linha. Frase e mensagem de erro ficam em caixa normal.

**The Mono Is Data Rule.** Mono não é figurino de "técnico": só aparece em
número medido, caminho, timestamp, eixo e endereço.

## Layout

Shell de duas colunas: sidebar fixa de 248px (mesa de fontes) e coluna
principal. A principal tem topbar, conteúdo e footbar. No desktop a altura é a
viewport: `body` e `.main` em 100vh, `content` com rolagem interna apenas quando
o conteúdo não couber; a sidebar tem rolagem própria.

O conteúdo é um grid de 12 colunas com gap de 14px e padding `16px 22px 14px`.
O resumo ocupa a linha 1 inteira em quatro cartões iguais. A linha 2 é vídeo
(6 colunas), estado da área (3) e saúde (3). A linha 3 são os dois gráficos
(6 + 6). A linha 4 é o log de eventos, que absorve a altura restante.

Só duas linhas são elásticas, e nessa ordem de prioridade: a do vídeo
(`minmax(210px, 1.35fr)`) e a do log (`minmax(140px, 1fr)`). A moldura do vídeo
preenche a altura da linha em vez de impor 16/9 — com o frame em proporção fixa,
o painel empurrava o log para fora da tela em 1440×900. O `object-fit: contain`
do `<video>` cuida da proporção do stream; as sobras viram letterbox preto.

Breakpoints: abaixo de 1180px o conteúdo vira 6 colunas (vídeo na largura toda,
estado e saúde em duas metades, gráficos em duas metades); abaixo de 980px a
sidebar vira faixa superior e o conteúdo empilha; abaixo de 620px o resumo vira
coluna única. Abaixo de 920px de altura (e acima de 1180px de largura, onde o
grid de 12 colunas vale) o conteúdo compacta gaps, valores, altura dos gráficos
e o sumário da sidebar; a linha do vídeo ganha piso de 268px — abaixo disso a
página rola alguns pixels em vez de achatar o frame numa faixa.

### Named Rules

**The One Screen Rule.** No desktop o dashboard cabe na viewport. Conteúdo novo
entra em cartão existente ou em lista com rolagem interna; nunca empurra a
página para baixo.

**The 22px Shell Rule.** Topbar, conteúdo e footbar compartilham padding
horizontal de 22px. Cartão que não alinha com esse eixo está errado.

## Elevation & Depth

Profundidade é papel sobre papel: uma sombra curta e difusa descola o cartão do
fundo no tema claro; no escuro `--shadow` é `none` e a separação fica só na
diferença tonal e no hairline. Não há vidro, gradiente estrutural nem glow
decorativo. Brilho só existe nos anéis das lâmpadas de status, porque ali ele é
estado.

### Shadow Vocabulary

- **Folha apoiada** (`0 1px 2px rgba(22,24,29,.05), 0 8px 24px -18px rgba(22,24,29,.35)`):
  cartões e sidebar no tema claro. Curta, de baixo para cima.
- **Nenhuma sombra** no tema escuro: o hairline e o tom fazem o trabalho.

### Named Rules

**The Glow Is State Rule.** Se um elemento brilha, ele reporta estado vivo.
Nada decorativo recebe sombra ou halo.

## Shapes

Linguagem de folha: raio de 12px em cartão, 10px em moldura de vídeo e cartão
de fonte, 8px em botão/campo/navegação, 6px em ação dentro de cartão, e pílula
em chip de filtro. Nada de raio grande em painel nem de canto vivo em cartão.

Estado não usa trilho lateral: usa **ponto**. O item de evento tem um ponto de
6px à esquerda (verde entrou, âmbar saiu, azul transitou, vermelho erro) e o
cartão de fonte muda borda e fundo, não ganha tarja.

A marca é um quadrado de 34px com raio de 9px, fundo verde de ação e "VS" em
condensada. É o único lugar onde o verde é fundo com texto claro.

### Named Rules

**The Dot Marks State Rule.** Cor em ponto significa mudança ou seleção. Ponto
colorido nunca decora.

**The Sheet Has Corners Rule.** Cartão é folha: 12px de raio, hairline e tom.
Canto vivo ou raio de 2px descaracteriza o caderno.

## Components

### Sidebar / Source Deck

- Largura 248px, fundo de folha, hairline à direita, rolagem própria.
- Marca no topo; seção de fontes com cartões compactos; sumário com âncoras;
  lâmpadas; atalhos de captura/autoteste; botão de tema.
- O cartão de fonte inteiro é o alvo de "ver" (botão `.source-view`); "ativar"
  é um botão irmão, nunca aninhado. Seleção usa borda e lavagem de ação; fonte
  ativa ganha badge "ativa".

### Stat Cards

- Quatro cartões iguais no topo: rótulo de 10px, valor de 30px em condensada
  tabular, dica de 11px.
- O valor não muda de tamanho quando o estado muda; muda de cor. Erro > 0 fica
  vermelho, crescimento de RSS fora do baseline fica vermelho, status
  `degraded`/`stale` fica âmbar.
- Não duplicam os detalhes: o cartão de estado guarda entradas/saídas/dwell/pico
  e o de saúde guarda status/threads/frames/idade; a faixa de vídeo foi
  substituída pela dica do cartão de FPS (preview e drop).

### Buttons

- Primário: fundo verde de ação, texto `on-action`, raio 8px, padding 6px/12px,
  peso 500. Hover escurece (claro) ou clareia (escuro). Desabilitado não é o
  primário esmaecido: vira superfície (`panel-2`) com texto `faint`, porque ação
  indisponível não deve parecer ação pendente.
- Secundário (`ghost`): fundo de folha, hairline, texto de tinta. Usado no
  botão de tema.
- Ação dentro de cartão de fonte: 10px, borda de ação, fundo transparente.
- Foco é outline de 2px em verde de ação com offset 2px.

### Chips

- Pílula, fundo transparente, hairline, rótulo de 10px em `muted`.
- Ativo: lavagem de ação, borda de ação e texto verde; carrega `aria-pressed`.
- Filtro de evento, "seguir ativa" e "destravar troca" compartilham o
  componente. O handler de filtro é escopado a `.event-filter` para não trocar
  o filtro quando o chip é de ação.

### Inputs / Fields

- Fundo de papel, hairline, raio 8px, mono de 11–12px.
- Token de troca: `type=password`, `autocomplete=off`, `spellcheck=false`,
  rótulo `sr-only`; o valor vive só em `sessionStorage` e nunca é renderizado.
- Foco: outline de 2px em verde de ação, offset interno quando o campo está em
  faixa apertada.

### State Word

`OCUPADO` / `VAZIO` / `—` em condensada de 52px, centrada no cartão de estado,
com "desde HH:MM:SS" de 11px embaixo. Ocupado usa verde de ação; vazio usa
`muted`. Sem halo e sem sombra.

### Video Panel

Moldura de 10px sobre letterbox preto, preenchendo a linha elástica do grid; o
`<video>` usa `object-fit: contain`, então a proporção do stream nunca é
esticada — a sobra é barra preta. Enquanto não há stream, a camada de estado
cobre a moldura com véu e frase curta ("stream não conectado", diagnóstico de
certificado, reconexão). Cabeçalho leva o caminho do sinal (rtsp → mediamtx →
webrtc) e as ações.

**Tela cheia:** o alvo é o cartão (`#panel-video`), não o `<video>` — o
cabeçalho permanece visível com o botão "sair", e a moldura ocupa o resto em
fundo de letterbox, sem raio nem sombra. Alterna pelo botão `tela cheia`, pelo
atalho `F` (ignorado quando o foco está em campo de texto) ou `Esc` do
navegador; o rótulo e `aria-pressed` acompanham o estado. Em navegador sem
Fullscreen API para `<div>` (iOS), cai no fullscreen nativo do elemento de
vídeo; sem nenhum dos dois, o botão não é anunciado.

### Theme Toggle

Botão no pé da sidebar, rótulo com o tema de destino ("tema escuro" no claro) e
`aria-pressed` refletindo o escuro. A escolha vai para `localStorage`
(`vsl-theme`) e um script inline no `<head>` aplica `data-theme` antes do
primeiro paint — sem ele a página pisca no claro. As páginas satélites leem a
mesma chave, sem botão próprio, e cada página sincroniza `meta theme-color` para
a barra do navegador. Trocar o tema limpa o cache de tokens do canvas e
redesenha os gráficos.

### Signature: Lamp Cluster

Quatro lâmpadas de 9px na sidebar — pipeline, vídeo, memória, telemetria — com
tag de 10px. Três estados visíveis (`on`, `warn`, `off`), anel de 3px na cor do
estado e `blink` em `steps(2)` a 1.2s, desligado sob `prefers-reduced-motion`.
É o resumo de saúde de uma olhada.

### Charts

Canvas de 150px (118px na compactação de altura), sem eixo em DOM. Grid e eixo
vêm de `--seam` e `--chart-axis`; série de FPS em verde de ação, série de RSS em
azul de transição, preenchimento a 12%. Cada canvas é `role="img"` com
`aria-label`. Sem tooltip, legenda ou animação de entrada.

As cores do canvas não são literais no `app.js`: o desenho lê `--accent`,
`--info`, `--divider` e `--chart-axis` do `:root`. Trocar o tema limpa o cache de
tokens e redesenha — é o que impede gráfico e painel de divergirem.

### Events

Lista mono de 11px com ponto de estado, hora de 62px, nome de 132px e detalhe
elástico. Divisor de 1px em `seam`. Rolagem interna; 120 itens no DOM. Filtros
`tudo` / `tracks` / `erros`.

## Do's and Don'ts

### Do:

- **Do** declarar toda cor nos blocos `:root` (claro) e
  `:root[data-theme="dark"]`; hex fora deles é cor não aprovada.
- **Do** manter a mesma estrutura nos dois temas (The Same Page at Night Rule).
- **Do** usar verde só para medido/acionável e para estado saudável.
- **Do** usar `tabular-nums` em todo valor que atualiza.
- **Do** separar cartões por hairline, tom e sombra curta; no escuro, sem sombra.
- **Do** manter a tela única no desktop; rolagem só em lista e sidebar.
- **Do** dar `aria-label` a canvas, `aria-pressed` a chip e `aria-live` a
  banner/status.
- **Do** desligar `blink` e transições sob `prefers-reduced-motion`.
- **Do** mostrar `—` e rótulo apagado sem telemetria, em vez de zero inventado.
- **Do** manter o frontend vanilla, sem framework nem CDN de runtime: o
  dashboard é appliance de bancada e precisa abrir offline na LAN.

### Don't:

- **Don't** introduzir segunda família tipográfica, raio novo ou breakpoint
  novo sem atualizar este documento.
- **Don't** usar cor em categoria, ícone decorativo ou ênfase editorial.
- **Don't** usar branco puro como texto de corpo; o branco é de `on-action`.
- **Don't** aplicar glow, gradiente ou vidro em superfície.
- **Don't** usar trilho lateral colorido em cartão ou alerta; estado é ponto.
- **Don't** fundir os alvos "ver" e "ativar" de uma fonte.
- **Don't** renderizar token de troca no DOM ou fora de `sessionStorage`.
- **Don't** trocar dados reais por demonstração sintética: número sem fonte é
  ruído.
