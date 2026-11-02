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

## Resultado do P0/P1 (medido em 2026-11-01, SSH em `192.168.59.129`)

Inventário (P0) — todas as dúvidas acima respondidas:

| Item | Medido |
|---|---|
| Plataforma | L4T **R39.2.1**, `nvidia-jetpack 7.2.1-b49` **instalado** → o `Jetpack: MISSING` do `jtop` é cosmético (não detecta o layout do JP 7) |
| Runtime | Python **3.12.3**, GStreamer **1.24.2**, Docker 29.8.1, runtime `nvidia` como **default** |
| Imagens | `deepstream:9.1-samples-multiarch` (16,2 GB) e `deepstream:9.1-triton-multiarch` (28,4 GB) **já presentes** — nenhum pull, nenhum login NGC necessário |
| Memória/disco | 7,5 GB (6,3 GB disponíveis), 805 GB livres |
| Portas | 8081/8554/1935/8889 livres |

P1 — imagem e stack:

- `make doctor` **verde** no Nano (detecta DS 9.1 e exige o wheel cp312);
- `docker compose build vision-app` **verde** com a base consolidada 9.1 e
  `pyds 1.2.2` (cp312) — o wheel novo está em `deploy/wheels/`;
- stack sobe (`make up`), dashboard e watchdog ok, **telemetria funciona** no
  DS 9.1 (GStreamer 1.24.2): `interval_metrics` a cada 30 s e a máquina de
  reconexão da fonte respondendo — nenhum deadlock de loop (INC-006/INC-007);
- **decode de hardware funciona**: teste isolado no Nano entregou 20 buffers de
  H.265 em 1,3 s com `nvv4l2decoder` (NvMMLite abriu), e o RTSP da câmera
  responde `200 OK` a partir do Nano.

Três defeitos de portabilidade encontrados e **corrigidos** neste ciclo:

1. **`nvtracker` com caminho fixo do DS 7.1** (`/opt/nvidia/deepstream/deepstream-7.1/lib/libnvds_nvmultiobjecttracker.so`):
   no DS 9.1 o `dlopen` falha, o `nvtracker` não vai a PLAYING e o
   `pipeline.set_state()` devolve **FAILURE** — mas o pipeline logava
   `pipeline_playing` e ficava com `fps_in=0` para sempre. Corrigido com o
   caminho **sem versão** (symlink estável nas duas bases, sobrescrevível por
   `NVTRACKER_LL_LIB`) e com `pipeline_state_failed` quando o `set_state`
   falha.
2. **Sem NVENC no Orin Nano** (`/dev/v4l2-nvenc` não existe): `nvv4l2h264enc`
   não instancia e o **boot morria** (`startup_failed`). Corrigido com escolha
   do encoder do preview por disponibilidade (hardware → software), pulando o
   hardware quando o device falta, e erro explícito se não houver nenhum.
3. **`privileged: true` no DS 9.1 esconde o encoder**: com `--privileged` o
   `gst-inspect-1.0 nvv4l2h264enc` **não** encontra o elemento; sem o flag,
   encontra (mas o device continua inexistente no Nano). Ainda **sem decisão**:
   tornar `privileged` configurável por plataforma (`APP_PRIVILEGED`) quando o
   preview for habilitado no Nano.

Observação de plataforma: a imagem DS 9.1 **não traz encoder de software**
(`x264enc` e `openh264enc` ausentes, `libgstx264.so` não existe mesmo com
`gstreamer1.0-plugins-ugly` instalado). No Orin Nano o preview fica
`PREVIEW_ENABLED=0`; habilitá-lo exige instalar o encoder de software na imagem
(ou aceitar o custo de CPU, que compete com a inferência).

### Bloqueio atual do P2

`data/parser/libnvdsinfer_custom_impl_Yolo.so` é um **binário sem fonte** no
repositório, compilado contra **CUDA 12** (NX). No Nano (CUDA **13.0**):

```
ERROR: Could not open lib: /app/data/parser/libnvdsinfer_custom_impl_Yolo.so,
       error string: libcudart.so.12: cannot open shared object file
nvinfer: Error in NvDsInferContextImpl::initialize(): Could not open custom lib
→ pipeline_state_failed (exit 1)
```

Opções (decisão pendente, nenhuma delas é "só config"):

1. **Trazer o fonte do parser para o repo e compilar na imagem** (recomendado):
   o parser deixa de ser binário órfão, passa a ser construído por plataforma e
   ganha proveniência/licença registradas. Custo: escolher a origem
   (`NVIDIA-AI-IOT/deepstream_reference_apps` ou `marcoslucianops/DeepStream-Yolo`,
   compatível com DS 9.x), revisar e validar a saída do `NvDsInferParseYolo`.
2. Usar o backend **Triton YOLO** que já vem na imagem 9.1
   (`TritonOnnxYolo`): elimina o parser custom, mas troca `nvinfer` por
   `nvinferserver` + repositório de modelos Triton — mudança de arquitetura.
3. Reexportar o modelo com **EfficientNMS** (end-to-end) e usar o parser
   nativo: muda o ONNX (e o engine) e a pós-processagem.

Enquanto isso, o **NX segue sendo o ambiente de produção** do VSL: nada do que
foi mudado neste ciclo altera o comportamento dele (padrão de imagem DS 7.1,
`ll-lib-file` versionless válido nas duas bases, encoder de hardware presente).

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

- `deploy/.env` sem `LAN_IP`: o `make up` detecta o IP atual
  (`scripts/host_ip.sh`) e reemite o certificado do servidor se o SAN não
  cobrir o endereço novo — preservando a CA. O certificado antigo estava
  amarrado ao IP do NX; o `--ensure` resolve sem `--force`.
- **Distribuir a CA do Nano** para cada máquina que abre o dashboard
  (no host: `scp …'~/Vision_Stability_Lab/deploy/certs/ca.crt'`; validar com
  `curl --cacert … → 404`, instalar no store e reiniciar o navegador). A CA do
  NX **não serve**: mesmo `CN`, chave diferente — INC-008. Trocar o IP depois
  não exige redistribuir de novo (só o certificado do servidor muda).
- `make status`, `make sources`, `make ip` (URLs do momento), player WHEP no
  path `live`.
- **Critério de aceite:** `make ip` mostra o painel, `http://<ip>:8081` abre,
  fontes listadas, preview toca no dashboard **na máquina do operador, com a CA
  do Nano instalada** (não basta `docker ps` saudável: a verificação de
  certificado é do navegador).

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
| Certificado amarrado a um IP | painel não abre depois de troca de DHCP | SAN gerado dos IPs atuais + nome `<hostname>.local`; `make up` reemite o certificado preservando a CA |
| `jtop` com `Jetpack: MISSING` | dúvida sobre o estado da instalação | P0 confirma com `dpkg`/`nv_tegra_release` antes de investir o resto |

## Rollback

Nada muda no NX: `DS_BASE_IMAGE` é opt-in por host (`deploy/.env`, não
versionado) e o padrão do `Dockerfile` continua DS 7.1. Reverter = apagar a
linha do `.env` e `make up-build`.
