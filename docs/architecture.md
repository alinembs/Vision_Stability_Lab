# Documentação de Arquitetura do Vision Stability Lab

## Visão Geral
O **Vision Stability Lab** é um sistema embarcado para monitoramento contínuo de ocupação em Jetson, combinando ingestão RTSP, aceleração por hardware (NVDEC/NVENC), inferência com TensorRT (`nvinfer`), tracking (`nvtracker`), regras temporais de permanência, OSD, streaming H.264 via MediaMTX (WebRTC/HLS) e dashboard web com métricas e eventos em tempo real.

## Estado atual (2026-08-23 — Fase 4 concluída)

### Pipeline ativo (`deepstream/pipeline_real.py`)
```text
rtspsrc (latency=200, drop-on-latency, protocols=tcp+udp)
  → rtph265depay / h265parse      [alternável para h264 via RTSP_CODEC]
  → nvv4l2decoder                 [saída NVMM]
  → nvstreammux (batch=1, 1280x720, live-source)
  → nvinfer (config/infer.txt — YOLO11m fp16, classe única "person")
  → nvtracker (IOU, config_tracker_IOU.yml)
  → nvdsosd (display-bbox + display-text, process-mode=1/GPU)
  → tee
      ├── queue → fakesink                          [ramo análise — probes]
      └── queue leaky=downstream, max-size-buffers=1 [ramo preview — descartável]
              → nvvideoconvert
              → capsfilter video/x-raw(memory:NVMM),format=NV12
              → nvv4l2h264enc (bitrate PREVIEW_BITRATE, IDR por GOP, SPS/PPS inline)
              → h264parse
              → PREVIEW_TRANSPORT:
                    rtsp (padrão) → rtspclientsink → MediaMTX path 'live'
                    udp           → rtph264pay → udpsink direto (testes Fase 5)
```

### Telemetria e dashboard
```text
pipeline_real.py ── JSONL stdout + data/logs/live.jsonl (TELEMETRY_FILE)
                                   │ (volume ro)
                                   ▼
                     dashboard/backend (FastAPI :8081→8000; host 8080 é do arquo-web)
                       ├── tailer assíncrono do JSONL (replay na partida)
                       ├── ring buffers: 480 amostras de métricas / 500 eventos
                       ├── GET /api/snapshot · /api/history?minutes=N · /api/events
                       └── WS   /ws (snapshot inicial + push por registro)

navegador ──► http://192.168.59.102:8081        (dashboard: player WHEP + cards + gráficos canvas + log de eventos)
navegador ──► https://192.168.59.102:8889/live/whep (WebRTC, sinalização WHEP do MediaMTX — TLS desde o E0-T4)
navegador ──► http://192.168.59.102:8888/live/     (HLS fallback, latência maior)
celular   ──► https://192.168.59.102:8443/capture.html (página de captura; WHIP em https://…:8889/phone/whip)
```

> **Acesse o dashboard pelo IP da LAN** (`http://192.168.59.102:8081`), não por
> `http://localhost:8081`. Dois motivos: (1) as portas do MediaMTX são
> publicadas amarradas no IP da LAN `${LAN_IP:-192.168.59.102}`, não em
> `0.0.0.0` (DIV-003, `docs/incident-log.md`), então `127.0.0.1:8889` não
> responde; (2) `dashboard/frontend/app.js` deriva a URL do WHEP do hostname da
> página (`location.hostname:8889`), logo abrir o dashboard em `localhost` faz o
> player apontar para `localhost:8889` e o vídeo fica morto, sem erro na tela.
>
> Desde o E0-T4 a porta 8889 é **TLS** (`webrtcEncryption: yes`, certificado de
> `deploy/certs/`): o navegador que abre o dashboard precisa confiar na CA
> interna (`deploy/certs/ca.crt`), senão o `fetch` do WHEP falha por
> certificado. A página de captura do celular usa a 8443 pelo mesmo motivo.
>
> **Requisito de CA na máquina do dashboard (não é do servidor).** O `ca.crt`
> tem de ser instalado em cada máquina que abre o player:
>
> ```bash
> sudo cp deploy/certs/ca.crt /usr/local/share/ca-certificates/vsl-ca.crt
> sudo update-ca-certificates   # Chrome/Edge no Linux
> ```
>
> Firefox usa store próprio (*Ver certificados* → *Autoridades* → *Importar*);
> Android, o runbook `docs/runbooks/captura-celular.md` (seção 3). `docker ps`
> mostrar tudo `healthy` **não** prova que o player negocia: a verificação do
> certificado é do navegador. `dashboard/frontend/app.js` distingue os dois
> casos com uma sonda `no-cors` (HTTPS 8889 vs. HTTP 8888) e mostra
> "certificado do MediaMTX não é confiável neste navegador — instale
> deploy/certs/ca.crt" em vez de "sem stream do mediamtx".

- **Saúde derivada** no backend: `healthy` se `interval_metrics` fresco (<90 s) com `fps_processed > 0`; `stale` se telemetria velha; `down` sem telemetria.
- **Sem persistência além do JSONL**: o histórico dos gráficos é reconstruído replayando o arquivo na partida do backend; SQLite fica para fase futura.
- **webrtcAdditionalHosts** no `mediamtx.yml` anuncia o IP LAN do Jetson para o ICE fechar com navegadores fora do host.

### Componentes
1. **`deepstream/pipeline_real.py`** — pipeline instrumentado (OSD, preview H.264 RTSP/UDP, probes, telemetria JSONL).
2. **`deepstream/tracker_state.py`** / **`area_monitor.py`** — tracks com TTL e transições `EMPTY`/`OCCUPIED`.
3. **`deepstream/health_metrics.py`** — coleta psutil; **`tegrastats_reader.py`** — stats nativas do Jetson (uso planejado).
4. **`dashboard/backend/app.py`** — API/WebSocket do dashboard; **`dashboard/frontend/`** — SPA vanilla (player WHEP, lâmpadas de status, gráficos canvas próprios).
5. **`mediamtx/`** — servidor de mídia (RTSP/RTMP/HLS/WebRTC/SRT).
6. **`data/`** — modelo YOLO11m, parser customizado, engines e logs de evidência.
7. **`deploy/docker-compose.yml`** — serviços `vision-app`, `mediamtx`, `dashboard`.

### Observações e dependências
- Scripts legados (`pipeline_rtsp_real.py`, `rtsp_*.py`, `phase1_*.py`, `app.py`+`pipeline.py`) permanecem como histórico de diagnóstico; o caminho suportado é `pipeline_real.py` (CMD da imagem).
- O MediaMTX não ingere RTP/UDP puro; por isso a Fase 4 introduziu `PREVIEW_TRANSPORT=rtsp` como padrão. O modo UDP permanece para os testes de "viewer lento" da Fase 5.
