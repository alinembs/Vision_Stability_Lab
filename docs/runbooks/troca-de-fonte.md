# Runbook — troca de fonte (câmera, drone, celular)

Uma fonte ativa por vez (D1, ADR-0006). A escolha é configuração, não `vim` no
Python: `data/config/active_source` + `deepstream/config/sources.yaml`, e troca
de fonte = reinício do pipeline (D4).

## Trocar em um comando

```bash
./scripts/switch_source.sh drone      # ou camera, phone
./scripts/switch_source.sh camera --timeout 120
```

O script faz, nesta ordem: valida o id no `sources.yaml`; recusa trocar se
`ACTIVE_SOURCE` estiver definida no ambiente do container; roda o pré-check
RTSP (`rtsp_read_probe.py --seconds 5`, D7); grava o controle de forma atômica;
recria **só** o container `vision-stability-app`; espera até 90 s por um
`interval_metrics` novo com o `source_id` pedido **e** `fps_in > 0`.

Saída (contrato de uma linha):

```text
PASS source=camera first_frame_in=12.3s fps_in=19.9 codec=h265 precheck=ok
FAIL source=drone reason="no_frames_on_path" observed="camera" rollback=restored:camera last_error="—"
```

| Exit | Significado | O que fazer |
|---|---|---|
| 0 | PASS: pós-check confirmou a fonte | nada |
| 1 | pós-check não confirmou; **rollback feito** para a fonte anterior | ler `reason`/`last_error` abaixo |
| 2 | uso incorreto ou id inválido | ver `--help`; ids válidos em `deepstream/config/sources.yaml` |
| 3 | `ACTIVE_SOURCE` no ambiente vence o arquivo | remover `ACTIVE_SOURCE` de `deploy/.env` (deixar vazio) e recriar |
| 4 | pré-check reprovado | a fonte não está publicando; nada foi escrito e o container está intacto |

Histórico de trocas: `data/logs/switch.jsonl` (uma linha por execução, com
`from`, `to`, `probe`, `result`, `reason`). Durante a troca existe
`data/config/switch_in_progress` (o watchdog vai usá-lo no E5-T1 e ele é
removido ao fim, inclusive em falha).

## Antes de trocar: a fonte pedida está publicando?

| Fonte | Como publicar | Observação |
|---|---|---|
| `camera` | já é a fonte do lab (RTSP fixo em `RTSP_URI`) | H.265, com áudio PCMA — os pads de áudio são ignorados |
| `drone` | `./scripts/probe/rtmp_publisher.sh --path drone` (sintético) ou o drone real por RTMP | H.264; o MediaMTX reexpõe em `rtsp://<lan>:8554/drone` |
| `phone` | página de captura (`https://<lan>:8443/capture.html`, `docs/runbooks/captura-celular.md`) | H.264 por WHIP em `<lan>:8889/phone` |

`mediamtx` no `sources.yaml` é o DNS do serviço **dentro** do compose; no host
o pré-check troca por `MTX_HOST`, `LAN_IP` ou `192.168.59.102` (as portas do
MediaMTX são publicadas no IP da LAN).

## Quando falha

1. `reason=no_frames_on_path` — o path não está entregando vídeo.
   `python3 scripts/probe/rtsp_read_probe.py --url rtsp://<lan>:8554/drone --seconds 5`
   mostra o motivo cru (`no_video_pad`, `pipeline_error: ...`). Drone: publisher
   no ar? Celular: WHIP conectado na página de captura?
2. `reason=source_id_inesperado (<outra>)` — o pipeline subiu com outra fonte:
   quase sempre `ACTIVE_SOURCE` no ambiente (exit 3 acima) ou `SOURCES_CONFIG`
   apontando para outro YAML.
3. `last_error="startup_failed: ..."` — erro de configuração dentro do
   container; `docker logs vision-stability-app | tail -20`. As mensagens de
   `sources.py` listam os ids válidos.
4. Container não sobe: `docker compose --env-file deploy/.env -f
   deploy/docker-compose.yml ps` (é **um** container DeepStream por projeto —
   nunca subir um segundo para "testar").

## Editar à mão (quando não quiser o script)

Funciona: o arquivo aceita comentários (`#`), linhas em branco, BOM e CRLF, e
só a **primeira** linha válida vale (`data/config/active_source.example` é a
referência).

```bash
printf 'drone\n' > data/config/active_source
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --no-deps vision-app
tail -f data/logs/live.jsonl   # conferir source_id e fps_in
```

A diferença é que isso não faz pré-check, não faz rollback e não prova nada:
um typo no id sobe o pipeline com erro (`startup_failed` no log) e ninguém é
avisado. Prefira o script.
