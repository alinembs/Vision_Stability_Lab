# Portabilidade para o Orin Nano (JetPack 7.2.1 / DeepStream 9.1)

## Objetivo

Rodar **este** stack no Orin Nano com **um comando** (`make up`), sem fork do
repositório e sem manutenção paralela: a mesma árvore serve o Orin NX (L4T
R36 / JetPack 6 / DeepStream 7.1) e o Orin Nano (L4T R39 / JetPack 7.2.1 /
DeepStream 9.1).

## Situação hoje (verificado em 2026-11-01)

| Ambiente | Papel | Plataforma | Estado neste repo |
|---|---|---|---|
| **Orin NX** (host atual, `192.168.59.102`) | projetos existentes | L4T **R36.4.7** / JP 6 / DS **7.1** / Python **3.10** no container | **funcionando** — é a base do `deploy/Dockerfile` |
| **Orin Nano** (`192.168.59.129`, `pr3via-jetson`) | projetos novos / validação | JP **7.2.1** / DS **9.1** / CUDA 13.2 / TRT 10.16.2.10 / Ubuntu 24.04 / Python 3.12 | **não suportado ainda** |
| PC | desenvolvimento | CUDA atual + DS 9.1 | não é alvo de deploy |

Fatos checados nesta máquina:

- a imagem consolidada `nvcr.io/nvidia/deepstream:9.1-triton-multiarch`
  **existe** (`docker manifest inspect` resolveu sem login); o catálogo NGC
  informa que `deepstream-l4t` (a base atual) é legado e cobre **até 9.0** —
  9.1+ usa a imagem consolidada;
- o wheel do `pyds` em `deploy/wheels/` é **cp310** (DS 7.1). Em Python 3.12 o
  wheel do release v1.2.2 é **cp312** (mesma nota do INC-003);
- o `deploy/Dockerfile` já escolhe o wheel pelo interpretador da base
  (`pyds-*-cp<tag>-*-linux_aarch64.whl`) e falha com mensagem clara quando não
  há nenhum — falta apenas o wheel cp312;
- `DS_BASE_IMAGE` é build arg (padrão DS 7.1) e o `make doctor` compara L4T ×
  base de imagem, avisando quando não casam.

Pendências de confirmação **no próprio Nano** (sem acesso hoje):

- `jtop` reporta **`Jetpack: MISSING`** com L4T 39.2.1 — confirmar se o
  meta-pacote do JetPack está instalado ou se é só o `jtop` que não detecta;
- versão do Python e do GStreamer **dentro** da imagem 9.1 (o código usa
  `GLib`/`Gst` e `nvv4l2decoder`, `nvinfer`, `nvtracker`);
- se a variante `-triton-multiarch` cabe na memória unificada do Nano (~8 GB),
  ou se convém a variante enxuta;
- se as configs de `nvinfer`/`nvtracker` (`deepstream/config/`) seguem válidas
  no DS 9.1.

## Tarefas

### P0 — Inventário do Nano (0,5 d)

- Rodar no Nano: `cat /etc/nv_tegra_release`, `dpkg -l | grep -i deepstream`,
  `python3 -V`, `free -m`, `docker info | grep -i nvidia`,
  `docker images | grep -i deepstream`, `ss -ltn | grep -E '8081|8554'`.
- Confirmar o `Jetpack: MISSING` (é bloqueio de instalação ou cosmético?).
- **Critério de aceite:** quadro preenchido, com versão de Python/GStreamer da
  imagem 9.1 e memória disponível.

### P1 — Imagem DS 9.1 (0,5–1 d)

- Adicionar `deploy/wheels/pyds-1.2.2-cp312-cp312-linux_aarch64.whl`.
- No Nano, `deploy/.env` com
  `DS_BASE_IMAGE=nvcr.io/nvidia/deepstream:9.1-triton-multiarch`.
- `make doctor && make up-build`.
- **Critério de aceite:** build verde; dentro do container
  `python3 -c "import pyds; print(pyds.__file__)"` e
  `gst-inspect-1.0 nvinfer` funcionam; `make doctor` diz
  `[ok] base de imagem casa com a plataforma (DS 9.1)`.

### P2 — Smoke do pipeline (1 d)

- `make test` (suítes do host e do dashboard) e
  `scripts/test_deepstream_pipeline.sh` dentro do container.
- Subir com a câmera e comparar com o NX: `fps_in`, `fps_processed`,
  `fps_preview`, RSS.
- Ajustar o que o DS 9.1 mudou (nomes/limites de configs de `nvinfer`,
  `tracker`, `nvdsosd`) — sem tocar no contrato de telemetria.
- **Critério de aceite:** `interval_metrics` contínuos com
  `fps_processed/fps_in ≥ 0,95` e `health=healthy` no dashboard.

### P3 — Dashboard, watchdog e certificados (0,5 d)

- `deploy/.env` com `LAN_IP=192.168.59.129`; regenerar o certificado do painel
  (`deploy/certs/generate-certs.sh --force` com `VSL_LAN_IP=192.168.59.129`) —
  o certificado atual está amarrado ao IP do NX.
- `make status`, `make sources`, player WHEP no path `live`.
- **Critério de aceite:** `http://192.168.59.129:8081` abre, fontes listadas,
  preview toca no dashboard.

### P4 — Gate de aceitação (0,5 d)

- `make doctor && make up` num Nano "limpo" (sem containers do VSL) sobe tudo.
- Troca de fonte pela tela (`make up-switch SWITCH_TOKEN=...`) com rollback.
- **Critério de aceite:** sessão de 10 min com `fps_in ≈ 20`,
  `fps_processed ≈ fps_in`, `fps_preview > 0`, **zero** `ExitCode 137 / OOM`,
  `docker stop` em ≤ 2 s com `pipeline_stopped exit_code=0`.

## Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| Memória unificada (~8 GB) com imagem `-triton-multiarch` | OOM/137 no boot | medir em P0; se estourar, usar variante enxuta ou `batch_size`/intervalo de inferência já configurados |
| DS 9.1 muda configs de `nvinfer`/`nvtracker` | pipeline não sobe | P2 ajusta configs; telemetria e testes não mudam |
| `pyds` cp312 ausente/quebrado | sem bindings | P1 traz o wheel do release v1.2.2 e valida `import pyds` |
| Certificado amarrado ao IP do NX | painel não abre no Nano | P3 regenera com `VSL_LAN_IP` do Nano |
| `jtop` com `Jetpack: MISSING` | dúvida sobre o estado da instalação | P0 confirma com `dpkg`/`nv_tegra_release` antes de investir o resto |

## Rollback

Nada muda no NX: `DS_BASE_IMAGE` é opt-in por host (`deploy/.env`, não
versionado) e o padrão do `Dockerfile` continua DS 7.1. Reverter = apagar a
linha do `.env` e `make up-build`.
