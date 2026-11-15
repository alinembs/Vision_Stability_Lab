# Quatro versões do dashboard — estudo de design

Quatro leituras visuais da mesma tela do **Vision Stability Lab**, cada uma
construída a partir de um dos sistemas de design da pasta `DESIGN_3.0`:

| Versão | Sistema de origem | Caráter |
|---|---|---|
| `v1-aplix.html` | `DESIGN-aplix-framer-website.md` | preto absoluto, laranja único, editorial |
| `v2-darkdasher.html` | `DESIGN-darkdasher-webflow-io.md` | cartões azul-noite, ciano, grade compacta |
| `v3-dashbrd.html` | `DESIGN-dashbrdtemplate-webflow-io.md` | faixa de KPIs antes do vídeo, azul |
| `v4-task-dasher.html` | `DESIGN-task-dasher-webflow-io.md` | claro, generoso, laranja queimado |

Abra **`index.html`** para ver as quatro lado a lado, entrar em tamanho cheio e
comparar tokens. Funciona direto do disco (`file://`) ou por HTTP estático.

## O que está vivo na prévia

Não é uma imagem da tela. Cada versão é uma página independente, com a mesma
camada de dados e de comportamento:

- **troca de fonte** altera KPIs, séries, sessão e log (uma fonte ativa por vez);
- **filtro do log** filtra de fato (`tudo`, `tracks`, `erros e avisos`);
- **conectar preview** passa por negociação, mostra o quadro anotado com as
  caixas de detecção e volta para `desconectar`;
- **estado vazio** do celular: sessão nova, sem amostra no JSONL — o produto não
  desenha série que não existe;
- **carregamento**: barra de progresso, valores `—` e séries desenhadas quando o
  primeiro snapshot chega;
- **teclado**: setas movem entre as fontes; filtros e CTA são botões reais.

## De onde vêm os números

Os valores são medidos, não estimados:

- **câmera fixa** — regime atual de 7,0 fps, RSS em platô de 947,2 MB
  (`docs/baselines/2026-10-30-e1t2-baseline.md`, `data/logs/phase5/longrun-report.md`);
- **drone** — 29,9 fps processados, RSS 858–882 MB
  (`docs/baselines/2026-10-31-drone-campo.md`);
- **celular** — nenhuma amostra; é o estado vazio real de uma sessão recém-aberta;
- **eventos** — vocabulário de `EVENT_LABELS` em `dashboard/frontend/app.js`
  (entrada, saída, área, aviso, fonte ativa, preview pronto);
- **observador** — modos e motivos de `docs/plans/2026-11-08-frontend-observador-vsl.md`.

As séries de 30 min são **reconstruídas por replay** com a cadência real (uma
amostra a cada 15 s, 120 pontos), porque o JSONL de bancada não está no
repositório. O texto do rodapé de cada versão diz isso ao operador: é estudo de
design, não telemetria ao vivo.

## Como o código está dividido

```
design-3.0/
  index.html            galeria: prévias, comparação de tokens, régua de revisão
  data.js               fontes, séries, eventos, observador (uma fonte de verdade)
  view.js               render, troca de fonte, filtro, gráficos, estados
  base.css              estrutura comum: grade, painéis, OSD, log, responsivo
  v1-aplix.css          identidade de cada sistema
  v2-darkdasher.css     (cor, tipo, raio, densidade, composição)
  v3-dashbrd.css
  v4-task-dasher.css
  v1…v4.html            a mesma marcação, um sistema por página
```

As quatro páginas compartilham marcação e ids. Isso é intencional: a comparação
mede **o sistema visual**, não quatro implementações diferentes. Para promover
uma versão ao dashboard real, o caminho é trocar `style.css` pelos tokens da
escolhida e apontar `app.js` para os mesmos ids — a marcação já os usa.

## Decisões de fidelidade

- **Nada de gradiente, brilho, vidro, sombra decorativa, ícone colorido em KPI,
  rosca, sparkline, menu lateral de enfeite, selo de "ao vivo" ou eyebrow em
  todo título.** A régua completa (20 aplicadas, 20 ausentes) está na galeria.
- **Dark Dasher sem o roxo `#a534ff`**: usado junto do ciano, vira gradiente
  roxo-azul, que está fora do escopo. O ciano é o único acento da versão.
- **Acentos têm duas variantes** onde o acento puro não passa em 4,5:1 como
  texto pequeno (`--accent` para superfície, `--accent-text` para texto). Em V3
  o azul sobe para `#4a92ff`; em V4 o laranja desce para `#b8390f`.
- **Raio interno = externo − padding** em cartão, quadro de vídeo e controle.
- **Vídeo é esquemático de propósito**: o repositório não tem captura do
  sistema e inventar uma foto seria pior. O quadro mostra o OSD real (fonte,
  codec, área, tracks, fps, hora) e as caixas aparecem ao conectar o preview.
- **Path da câmera aparece como `RTSP_URI (deploy/.env)`**, porque a URI tem
  credencial e o catálogo é somente leitura (DIV-001).

## Próximo passo

Escolher uma versão, portar os tokens para `dashboard/frontend/style.css` e
validar no stack real (1440×900 de bancada, além de 390 px da página de
captura). As quatro páginas continuam servindo de referência para regressão
visual.
