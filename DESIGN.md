---
name: Vision Stability Lab
description: Instrumento de bancada — chassi preto com um laranja ember como único acento; papel claro para luz alta; Inter na interface e mono no dado.
colors:
  paper: "#f4f1ea"
  card: "#fffdf9"
  card-raised: "#f8f3eb"
  hairline: "#e4ddd2"
  seam: "#ece5da"
  ink: "#16181d"
  muted: "#6d675e"
  faint: "#776f63"
  action: "#c2410c"
  action-hover: "#9a3412"
  action-border: "#e0b9a8"
  action-wash: "#fbeadf"
  on-action: "#ffffff"
  ok: "#0f6b45"
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
  lamp-on-border: "#a3350a"
  lamp-fail-border: "#7d1d1a"
  alert-wash: "#f8e6e4"
  alert-text: "#7d1d1a"
colors-dark:
  paper: "#000000"
  card: "#0c0c0d"
  card-raised: "#16161a"
  hairline: "#232326"
  seam: "#1b1b1e"
  ink: "#f4f4f2"
  muted: "#a5a19a"
  faint: "#8d8982"
  action: "#f54900"
  action-hover: "#ff5a12"
  action-border: "#5e2a13"
  action-wash: "#1c0d06"
  on-action: "#070707"
  ok: "#56c99b"
  warn: "#eab45f"
  warn-wash: "#2a2114"
  bad: "#ff7f6e"
  bad-wash: "#2b1512"
  info: "#93a7bb"
  axis: "#8d8982"
  video-pit: "#08090a"
  video-letterbox: "#000000"
  video-veil: "#050608"
  on-video: "#f4f4f2"
  lamp-rest: "#26262a"
  lamp-rest-border: "#3a3a3f"
  lamp-on-border: "#ff6a24"
  lamp-fail-border: "#a63b30"
  alert-wash: "#2b1512"
  alert-text: "#ff8a76"
typography:
  display:
    fontFamily: "Inter, sans-serif"
    fontSize: "44px"
    fontWeight: 500
    lineHeight: 1.05
    letterSpacing: "-0.035em"
  stat:
    fontFamily: "Inter, sans-serif"
    fontSize: "38px"
    fontWeight: 500
    lineHeight: 1.12
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Inter, sans-serif"
    fontSize: "16px"
    fontWeight: 500
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Inter, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    letterSpacing: "-0.005em"
  body:
    fontFamily: "Inter, sans-serif"
    fontSize: "14px"
    fontWeight: 400
  label:
    fontFamily: "Inter, sans-serif"
    fontSize: "10px"
    fontWeight: 500
    letterSpacing: "0.1em"
  label-sm:
    fontFamily: "Inter, sans-serif"
    fontSize: "9px"
    fontWeight: 500
    letterSpacing: "0.06em"
  label-md:
    fontFamily: "Inter, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    letterSpacing: "0.01em"
  data-sm:
    fontFamily: "ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 400
  readout:
    fontFamily: "ui-monospace, monospace"
    fontSize: "14px"
    fontWeight: 500
rounded:
  sm: "4px"
  md: "8px"
  lg: "20px"
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
    padding: "18px 20px"
  stat-card:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    typography: "{typography.stat}"
    rounded: "0px"
    padding: "0px"
  button-default:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
    typography: "{typography.label-md}"
    rounded: "4px"
    padding: "9px 16px"
  chip-default:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "3px 10px"
  chip-active:
    backgroundColor: "{colors.action-wash}"
    textColor: "{colors.action}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "3px 10px"
  input-text:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.data-sm}"
    rounded: "{rounded.sm}"
    padding: "6px 8px"
---

# Design System: Vision Stability Lab

## Overview

**Creative North Star: "Instrumento de Bancada"**

A tela é um instrumento de medição, não um painel de vitrine. O padrão é o
chassi do turno: fundo preto absoluto, folhas um passo acima do preto, fios de
1px no lugar de sombra e um único laranja **ember** marcando o que se mede e o
que se aciona. O papel quente continua existindo para a bancada de dia com luz
alta — a mesma página, os mesmos componentes, com o ember recalculado para
passar contraste sobre papel.

A tipografia é Inter com tracking negativo nos títulos e nos números; a mono do
sistema entra só onde há instrumento — hora, caminho, epoch, eixo. O rótulo é
caixa de título com conectores minúsculos ("Saúde do Sistema", "Fps Processado");
caixa alta é exceção do enum de telemetria (`EMPTY` / `OCCUPIED`) e de sigla.

A sidebar é a **mesa de fontes**: marca, catálogo de fontes com sessão e troca,
sumário da página, as lâmpadas de saúde e os atalhos de bancada. O conteúdo
principal é a leitura: quatro leituras soltas no topo, vídeo anotado, estado da
área, saúde, dois gráficos e o log de eventos. A hierarquia vem de posição e
tamanho de leitura, não de ornamento.

O sistema é contido por decisão. Ember aparece onde há número medido ou ação
possível; verde só aprova, âmbar só avisa, vermelho só falha, azul só transita.
Sombra existe apenas no papel claro, curta, para descolar a folha; no chassi ela
não existe. Movimento é curto e informativo: lâmpada piscando, marcador de troca
em andamento, traço do gráfico ao trocar de fonte.

**Key Characteristics:**
- Chassi preto por padrão; papel claro opcional, mesma estrutura e mesmos tokens.
- Sidebar como mesa de fontes e sumário, não como menu de aplicação.
- Quatro leituras soltas no topo, sem moldura; o resto é leitura detalhada.
- Um acento: ember no que se mede e no que se aciona.
- Inter na interface e nos números; mono exclusivamente no dado.
- Rótulo em caixa de título; caixa alta só em enum e sigla.
- Uma tela no desktop; rolagem só dentro do log de eventos e na sidebar.

## Colors

### Primary

- **Ember** (`{colors.action}`): o acento do sistema. Botão primário, link,
  seleção de fonte, série de FPS no gráfico, foco e a palavra de área ocupada.
  No chassi é `#f54900`; no papel sobe para `#c2410c` para passar contraste como
  texto pequeno.
- **Ember Escuro** (`{colors.action-hover}`): hover do botão primário.
- **Borda de Ação** (`{colors.action-border}`): contorno de chip ativo, item
  selecionado e botão secundário. Existe para que o ember cheio signifique
  "ativo", não "contorno".
- **Lavagem de Ação** (`{colors.action-wash}`): fundo de seleção, chip ativo e
  destaque de estado ocupado. É ember rebaixado a superfície, não um segundo
  acento.

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
- **Tinta** (`{colors.ink}`): texto de dado e de corpo, e o fio da marca.
- **Rótulo Apagado** (`{colors.muted}`): rótulo de leitura, linha de estado,
  descrição secundária.
- **Rótulo Desbotado** (`{colors.faint}`): timestamp, nota de rodapé, metadado
  de fonte. Nunca em número que o operador precisa comparar.
- **Rótulo de Eixo** (`{colors.axis}`): escala dentro do canvas; nunca em DOM.

### Estado

- **Verde de Aprovado** (`{colors.ok}`): lâmpada acesa, status `healthy`,
  banner de troca concluída.
- **Âmbar de Atenção** (`{colors.warn}`): área reservada a atenção real —
  `degraded`/`stale`, pico de memória, banner de troca em andamento.
- **Lavagem de Atenção** (`{colors.warn-wash}`): fundo de banner em andamento.
- **Vermelho de Falha** (`{colors.bad}`): falha ativa — erro de pipeline,
  status `down`, token recusado, idade de telemetria acima de 90 s.
- **Lavagem de Falha** (`{colors.bad-wash}`): fundo de alerta e de banner de
  erro.
- **Azul de Transição** (`{colors.info}`): uso estreito e deliberado. Marca o
  ponto de evento de transição de área. Não é um segundo acento e não pinta
  gráfico.

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

**The Measured Ember Rule.** Ember toca o que foi medido ou o que se pode
acionar. Se um elemento não tem número nem ação, ele não é ember.

**The Status Is Real Rule.** Âmbar, vermelho e azul só aparecem quando existe
estado observado. Sem telemetria a tela mostra `—` e rótulo apagado; não inventa
cor "neutra de categoria".

**The Same Page at Night Rule.** O tema troca valores de token, nunca estrutura,
componente ou semântica. Componente que só existe em um dos temas está errado.

## Typography

**Interface:** Inter (com system sans de fallback).
**Dado:** mono do sistema — sem download de fonte, para a página abrir offline na
LAN.

**Character:** uma família só carrega a interface inteira porque o produto é uma
ferramenta de trabalho: o que separa título de dado não é a família, é o peso, o
tracking e a coluna. A mono fica estritamente em dado, endereço, timestamp e eixo
de gráfico. Nenhuma das duas é decoração.

### Hierarchy

- **Display** (500, 44px, tracking −.035em): a palavra de estado da área
  (`Ocupado` / `Vazio`). O maior objeto tipográfico da tela; muda de cor com o
  estado, não de tamanho.
- **Stat** (500, 38px, tracking −.03em, tabular): valor das quatro leituras de
  resumo. É a leitura de dez segundos: FPS, RSS, tracks, erros.
- **Headline** (500, 16px, tracking −.02em): título da página no topbar. Uma vez
  por página.
- **Title** (500, 13px, tracking −.005em): nome da fonte, cabeçalho de cartão e
  de gráfico.
- **Body** (400, 14px): corpo e texto de dado em linha.
- **Label** (500, 10px, tracking .1em, caixa de título): rótulo de leitura,
  cabeçalho de painel e tag de lâmpada.
- **Escala densa:** 9px metadado e badge, 10px rótulo e nota, 11px linha de
  estado e chip, 12px dado denso, 14px corpo. Passos de 1px, não de razão: cada
  passo existe porque uma coluna precisou caber.

### Named Rules

**The Tabular Numbers Rule.** Todo número que atualiza ou se compara em coluna
usa `tabular-nums`. O valor não pode dançar quando a telemetria chega.

**The Title Case Rule.** Rótulo e título vão em caixa de título, com conectores
(`de`, `do`, `da`, `e`, `em`, `por`) minúsculos quando não abrem o texto —
"Saúde do Sistema", "Fps Processado", "Erros e Avisos". Caixa alta fica restrita
ao enum de telemetria (`EMPTY` / `OCCUPIED`), a sigla (`RSS`, `RTSP`, `WHEP`) e a
código (`h265`).

**The Mono Is Data Rule.** Mono não é figurino de "técnico": só aparece em
número medido, caminho, timestamp, eixo e endereço.

## Layout

Shell de duas colunas: sidebar fixa de 248px (mesa de fontes) e coluna
principal. A principal tem topbar, conteúdo e footbar. No desktop a altura é a
viewport: `body` e `.main` em 100vh, `content` com rolagem interna apenas quando
o conteúdo não couber; a sidebar tem rolagem própria.

O conteúdo é um grid de 12 colunas com gap de 14px e padding `16px 22px 14px`.
As quatro leituras de resumo ocupam a linha 1 inteira, **sem moldura**: são
quatro colunas de texto separadas por espaço, e a hierarquia vem do tamanho do
número. A linha 2 é vídeo (6 colunas), estado da área (3) e saúde (3). A linha 3
são os dois gráficos (6 + 6). A linha 4 é o log de eventos, que absorve a altura
restante.

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

Profundidade é papel sobre papel no claro: uma sombra curta de 1px descola o
cartão do fundo. No chassi `--shadow` é `none` e a separação fica só na diferença
tonal e no hairline. Não há vidro, gradiente estrutural nem glow decorativo.
Brilho só existe nos anéis das lâmpadas de status, porque ali ele é estado.

### Shadow Vocabulary

- **Folha apoiada** (`0 1px 2px rgba(22,24,29,.06)`): cartões e sidebar no tema
  claro. Um passo, de baixo para cima.
- **Nenhuma sombra** no chassi escuro: o hairline e o tom fazem o trabalho.

### Named Rules

**The Glow Is State Rule.** Se um elemento brilha, ele reporta estado vivo.
Nada decorativo recebe sombra ou halo.

## Shapes

Linguagem de instrumento: raio de **20px** na folha (cartão, painel, sidebar de
seções), **12px** no cartão de fonte, **8px** na moldura de vídeo e no campo
largo, e **4px** em controle — botão, chip, campo e badge quadrado. Pílula fica
restrita ao badge de estado da fonte. Nada de pílula em filtro e nada de canto
vivo em folha.

Regra de encaixe: **raio interno = raio externo − padding**. Folha de 20px com
padding de 12px recebe moldura de 8px; não existe moldura mais redonda que o
berço.

Estado não usa trilho lateral: usa **ponto**. O item de evento tem um ponto de
6px à esquerda (verde entrou, âmbar saiu, azul transitou, vermelho erro) e o
cartão de fonte muda borda e fundo, não ganha tarja.

A marca é um quadrado de 34px com raio de 4px, fundo ember, **"VSL"** em Inter de
12px e fio de 1px na tinta. É o único lugar onde o ember é fundo com texto
escuro.

### Named Rules

**The Dot Marks State Rule.** Cor em ponto significa mudança ou seleção. Ponto
colorido nunca decora.

**The Sheet Has Corners Rule.** Folha é 20px, controle é 4px, moldura é 8px.
Canto vivo em painel descaracteriza; pílula fora do badge também.

## Components

### Sidebar / Source Deck

- Largura 248px, fundo de folha, hairline à direita, rolagem própria.
- Marca no topo; seção de fontes com cartões compactos de 12px; sumário com
  âncoras; lâmpadas; atalhos de captura/autoteste; botão de tema.
- O cartão de fonte inteiro é o alvo de "ver" (botão `.source-view`); "ativar"
  é um botão irmão, nunca aninhado. Seleção usa borda e lavagem de ação; fonte
  ativa ganha badge "Ativa".

### Stat Reads

- Quatro leituras iguais no topo, **sem cartão**: rótulo de 10px em caixa de
  título, valor de 38px em Inter tabular, dica de 11px. O espaço entre elas faz
  o agrupamento.
- O valor não muda de tamanho quando o estado muda; muda de cor. Erro > 0 fica
  vermelho, crescimento de RSS fora do baseline fica vermelho, status
  `degraded`/`stale` fica âmbar.

### Buttons

- Primário: fundo ember, texto `on-action`, raio 4px, padding 9px/16px, peso 500.
  Hover escurece (claro) ou clareia (escuro). Desabilitado não é o primário
  esmaecido: vira superfície (`panel-2`) com texto `faint`, porque ação
  indisponível não deve parecer ação pendente.
- Secundário (`ghost`): fundo de folha, hairline, texto de tinta, raio 4px.
- Ação dentro de cartão de fonte: 10px, borda de ação, fundo transparente.
- Foco é outline de 2px em ember com offset 2px.

### Chips

- Raio 4px, fundo transparente, hairline, rótulo de 10px em `muted`, caixa de
  título.
- Ativo: lavagem de ação, borda de ação e texto ember; carrega `aria-pressed`.
- Filtro de evento, "seguir ativa" e "destravar troca" compartilham o
  componente. O handler de filtro é escopado a `.event-filter` para não trocar
  o filtro quando o chip é de ação.

### Inputs / Fields

- Fundo de papel, hairline, raio 4px, mono de 11–12px.
- Token de troca: `type=password`, `autocomplete=off`, `spellcheck=false`,
  rótulo `sr-only`; o valor vive só em `sessionStorage` e nunca é renderizado.
- Foco: outline de 2px em ember, offset interno quando o campo está em faixa
  apertada.

### State Word

`Ocupado` / `Vazio` / `—` em Inter de 44px, centrada no cartão de estado, com a
enum de telemetria (`OCCUPIED` / `EMPTY`) em mono de 10px logo abaixo e "desde
HH:MM:SS" de 11px embaixo. Ocupado usa ember; vazio usa `muted`. Sem halo e sem
sombra.

### Video Panel

Moldura de 8px sobre letterbox preto, preenchendo a linha elástica do grid; o
`<video>` usa `object-fit: contain`, então a proporção do stream nunca é
esticada — a sobra é barra preta. Enquanto não há stream, a camada de estado
cobre a moldura com véu e frase curta ("stream não conectado", diagnóstico de
certificado, reconexão). Cabeçalho leva o caminho do sinal (rtsp → mediamtx →
webrtc) e as ações.

**Tela cheia:** o alvo é o cartão (`#panel-video`), não o `<video>` — o
cabeçalho permanece visível com o botão "Sair", e a moldura ocupa o resto em
fundo de letterbox, sem raio nem sombra. Alterna pelo botão `Tela Cheia`, pelo
atalho `F` (ignorado quando o foco está em campo de texto) ou `Esc` do
navegador; o rótulo e `aria-pressed` acompanham o estado. Em navegador sem
Fullscreen API para `<div>` (iOS), cai no fullscreen nativo do elemento de
vídeo; sem nenhum dos dois, o botão não é anunciado.

### Theme Toggle

Botão no pé da sidebar, rótulo com o tema de destino ("Tema Claro" no escuro) e
`aria-pressed` refletindo o escuro. O chassi escuro é o padrão; a escolha vai
para `localStorage` (`vsl-theme`) e um script inline no `<head>` aplica
`data-theme` antes do primeiro paint — sem ele a página pisca. As páginas
satélites leem a mesma chave, sem botão próprio, e cada página sincroniza `meta
theme-color` para a barra do navegador. Trocar o tema limpa o cache de tokens do
canvas e redesenha os gráficos.

### Signature: Lamp Cluster

Quatro lâmpadas de 9px na sidebar — pipeline, vídeo, memória, telemetria — com
tag de 10px em caixa de título. Três estados visíveis (`on`, `warn`, `off`), anel
de 3px na cor do estado e `blink` em `steps(2)` a 1.2s, desligado sob
`prefers-reduced-motion`. É o resumo de saúde de uma olhada.

### Charts

Canvas de 150px (118px na compactação de altura), sem eixo em DOM. Grid e eixo
vêm de `--seam` e `--chart-axis`; **as duas séries usam o ember**, porque cada
gráfico é uma série só e um segundo tom aqui viraria arco-íris. Preenchimento a
9%. Cada canvas é `role="img"` com `aria-label`. Sem tooltip, legenda ou
animação de entrada.

As cores do canvas não são literais no `app.js`: o desenho lê `--accent`,
`--divider` e `--chart-axis` do `:root`. Trocar o tema limpa o cache de tokens e
redesenha — é o que impede gráfico e painel de divergirem.

### Events

Lista mono de 11px com ponto de estado, hora de 62px, nome de 132px em caixa de
título e detalhe elástico. Divisor de 1px em `seam`. Rolagem interna; 120 itens
no DOM. Filtros `Tudo` / `Tracks` / `Erros`.

## Do's and Don'ts

### Do:

- **Do** declarar toda cor nos blocos `:root` (claro) e
  `:root[data-theme="dark"]`; hex fora deles é cor não aprovada.
- **Do** manter a mesma estrutura nos dois temas (The Same Page at Night Rule).
- **Do** usar ember só para medido/acionável.
- **Do** usar `tabular-nums` em todo valor que atualiza.
- **Do** escrever rótulo em caixa de título, com conectores minúsculos.
- **Do** separar cartões por hairline, tom e sombra curta; no chassi, sem sombra.
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
- **Don't** escrever rótulo em caixa alta, e nunca usar `text-transform:
  uppercase` em texto de interface.
- **Don't** aplicar glow, gradiente ou vidro em superfície.
- **Don't** usar trilho lateral colorido em cartão ou alerta; estado é ponto.
- **Don't** fundir os alvos "ver" e "ativar" de uma fonte.
- **Don't** renderizar token de troca no DOM ou fora de `sessionStorage`.
- **Don't** trocar dados reais por demonstração sintética: número sem fonte é
  ruído.
