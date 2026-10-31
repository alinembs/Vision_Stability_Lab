# Sessão de campo — drone (2026-10-31)

- **Veredito:** sessão **aprovada no critério revisado** (retorno ≤ 20 s),
  com uma ressalva que precisa ficar explícita: o alvo era **≤ 5 s** e foi
  revisado **depois** desta medição (decisão do operador em 2026-10-31), porque
  ≤ 5 s é incompatível com o backoff de 10 s do código. Medido: **11,56 s** do
  `is publishing` do MediaMTX ao `rtsp_video_pad` (9,56 s do path online). Todos
  os outros critérios passaram, com **0** `pipeline_error` na janela de regime,
  `fps_processed/fps_in` entre 0,999 e 1,001 e `Restarts=0`.
- **Data / hora:** 2026-10-31, 21:29 → 21:58 UTC (18:29 → 18:58 local, UTC−3)
- **Host / `LAN_IP`:** Jetson Orin, `192.168.59.102` (L4T R36.4.7, 7619 MB)
- **Série bruta:** `data/logs/live.jsonl` (janela `21:37:18 → 21:48:18`) e
  `data/logs/drone-campo-ingest.jsonl` (duas medições de ingestão)
- **Instrumentos:** `scripts/probe/rtsp_read_probe.py` (§4a),
  `scripts/probe/ingest_report.py` (§4a.1), `scripts/switch_source.sh` (§4b),
  `scripts/mem_budget.sh` (§7)

## Ficha de coleta (§6)

| Campo | Valor |
|---|---|
| Data / hora (local e UTC) | 2026-10-31 18:29–18:58 (UTC−3) · 21:29–21:58 UTC |
| Host / `LAN_IP` | Jetson Orin / `192.168.59.102` |
| Modelo do drone | DJI **Mini 3** (informado pelo operador) |
| App + **versão** | app do drone informado como "MINI 3", **1.21.18** |
| URL de push usada | `rtmp://192.168.59.102:1935/drone` (origem `192.168.59.106`) |
| Codec real | **H.264** (`encoding-name=H264`), **2 tracks**: H264 + MPEG-4 Audio (AAC) |
| Resolução do app | **"720p" → 960x720** · **"1080p" → 1440x1080** (rótulos 4:3, não 16:9) |
| FPS do app | **30,1** (720p) · **29,9** (1080p) |
| Bitrate do app | **4.067 kbps** (720p) · **4.847 kbps** (1080p) — medido no fio (RTP, áudio incluso) |
| Resoluções testadas (bitrate/fps em cada) | 720p: **4.067 kbps / 30,1 fps** · 1080p: **4.847 kbps / 29,9 fps** |
| Distância / condição de luz | **< 1,5 m**, LAN, push por **Wi-Fi** do controle; drone **no chão** (sem voo); sala com luz |
| `fps_in` (telemetria, mediana) | **29,93** (23 amostras: 28,97–30,33) |
| `fps_processed` / `fps_in` | **0,999 – 1,001** (99,9–100,1 %) |
| Sonda §4a: `fps`, `first_frame_s` | **30,062** · **2,077 s** |
| Gaps > 500 ms / `max_gap_ms` | **0** · 118,6 ms (sonda) / 110,3 ms (ingestão 1080p) |
| `queue_analyze` / `queue_preview` (pico) | **0 / 0** em todas as 23 amostras |
| Queda provocada: método | corte do push no app ("parar transmissão") por **~23 s** |
| Tempo até **perceber** a queda | **1,1 s** (21:36:03 → `eos` 21:36:04.106) |
| Tempo até o **primeiro frame de volta** (alvo ≤ 20 s) | **11,56 s** — `is publishing` 21:36:26 → `rtsp_video_pad` 21:36:37.560 (9,56 s do path online às 21:36:28). **Atende ao critério revisado**; não atendia ao alvo antigo de 5 s |
| Evento final da queda | `rtsp_reconnect_resumed` (attempt **3/5**), **mesmo** `session_epoch` |
| Latência p50 / p95 / máx (nº de amostras) | **não medida** — §4e exige visor com relógio de ms; ficou para outra sessão |
| Ramo medido na latência | — |
| RSS (MB) mín–máx | **858,2 – 882** (platô de 861,9 MB na janela de regime) |
| RAM do sistema (%) | **52,1** (3972/7619 MB) no fechamento |
| `lfb` (MB) | **8** |
| `restarts` / `OOMKilled` / exit code | **0 / false / 0** |
| Containers DeepStream ativos | **1** (`vision-stability-app`) |
| Observações (o que surpreendeu, o que refazer) | ver "Achados" abaixo |

## Janela de regime (§7)

Depois do PASS do §4b e **fora** da queda provocada (`21:37:18 → 21:48:18`):

| Métrica | Medido |
|---|---|
| Duração / amostras | **660,0 s** · **23** `interval_metrics` |
| `pipeline_error` / `pipeline_failed` / `eos` | **0 / 0 / 0** |
| `fps_processed / fps_in` | **0,999 – 1,001** |
| Filas | **0 / 0** (todas as amostras) |
| RSS | **861,72 – 862,07 MB** (praticamente plano) |
| Inferência | funcionando: 6 `track_entered`, 5 `track_exited`, área `OCCUPIED` |
| `frames_total_in` / `frames_total_processed` | 40.679 / 40.678 |

## Queda de sinal provocada (§4d)

| Marco | Instante |
|---|---|
| Push parou no app (MediaMTX `stream unpublished`) | 21:36:03 |
| `pipeline_warning` (`Could not read from resource (9)`) | 21:36:03.749 |
| `eos` (contido no tee) | 21:36:04.106 |
| `rtsp_reconnect_started` attempt 1 (`reason=eos`) | 21:36:04.196 |
| attempt 2 / attempt 3 (`reason=bus_error`) | 21:36:04.453 / **21:36:14.977** |
| Push voltou (RTMP aberto / path online) | 21:36:26 / **21:36:28** |
| `rtsp_video_pad` (primeiro frame novo) | **21:36:37.560** |
| `rtsp_reconnect_resumed` (attempt 3) | 21:36:38.522 |
| FPS de volta ao regime | 29,07/29,07 às 21:37:18 |

`session_epoch` permaneceu **21:33:47.316** e `Restarts=0`: foi **reconexão em
processo**, não restart. O orçamento gastou **3 de 5** tentativas em 23 s de
path fora.

## Critérios de aceite da sessão (§7)

- [x] path `drone` online e processado por **≥ 10 min** sem `pipeline_error` na
      telemetria — **660 s, 0 erro**
- [x] `fps_processed ≥ 0,95 × fps_in` na janela — **0,999–1,001**
- [x] reconexão após queda provocada com **primeiro frame de volta ≤ 20 s**
      (critério revisado; ver Achados 2) — **11,56 s** ✅
- [x] **zero** container com exit **137**
- [x] **zero** `OOMKilled: true`
- [x] **exatamente 1** container DeepStream
- [x] ficha preenchida e anexada (`docs/baselines/2026-10-31-drone-campo.md`)
- [x] sessão encerrada em `camera`, sem publisher ativo (ver estado final)

## Achados (o que surpreendeu)

1. **O rótulo de resolução do app é 4:3, não 16:9.** "720p" publica **960x720**
   e "1080p" publica **1440x1080** — nenhum dos dois é 1280x720/1920x1080. Quem
   comparar com o `nvstreammux` (fixo em 1280x720) precisa saber que há
   reescala nos dois casos. O bitrate também é baixo para 1080p (**4,8 Mbps** a
   30 fps, no preset "suave"), o que ajuda o link mas limita detalhe.
2. **O retorno após queda é ~10 s por desenho, não ~2 s.** O `session_epoch` se
   mantém e a reconexão funciona, mas o backoff exponencial começa em **10 s**
   (`deepstream/source_reconnect.py`, `retry_base_s=10.0`: 10 → 20 → 40 → 45 s).
   O path voltou às 21:36:28 e a tentativa em curso só pegou vídeo às 21:36:37.5.
   **Critério revisado** de ≤ 5 s para **≤ 20 s** (decisão do operador em
   2026-10-31, **depois** desta medição): 20 s cobre a 2ª tentativa (10 s de
   espera) mais a renegociação do `rtspsrc` (~10 s, medidos 9,56 s). O alvo de
   5 s só volta a valer se a base do backoff cair (ex.: 2 s) — mudança de
   código, com gate novo.
3. **Queda normal do push gerava `pipeline_error`.** Durante os 23 s de path
   fora saíram **4** `pipeline_error` (`Could not open resource (5)`,
   `Not found (3)`) — são o `rtspsrc` falhando nas tentativas de reconexão, não
   falha do pipeline (ele voltou sozinho, no mesmo processo). Efeito colateral:
   o dashboard passou a mostrar `error_count: 4` e `last_error: "Not found (3)"`.
   **Corrigido em 2026-10-31:** erro de `ACTION_RECONNECT` sai como
   `rtsp_reconnect_error` e o do ramo de preview como `preview_branch_error`;
   `pipeline_error` fica só para falha de análise (mux/decoder/`nvinfer`).
   O dashboard lista os dois na run, mas não os conta em `error_count`.
4. **O preview exige CA confiável no navegador.** A imagem só apareceu depois de
   instalar `deploy/certs/ca.crt` na máquina do operador (`192.168.59.136`) —
   a página do dashboard é HTTP, mas o vídeo é WHEP sobre TLS na 8889. O erro
   na tela é claro ("certificado do MediaMTX não é confiável"), e o path
   `live` estava de pé o tempo todo (sonda PASS, 30,9 fps).

## Série bruta

- `data/logs/drone-campo-ingest.jsonl` — duas linhas JSON (720p e 1080p), cada
  uma com `resolution`, `fps`, `bitrate_kbps`, `audio_tracks`, `gaps_over_500ms`.
- `data/logs/live.jsonl` — janela `21:33:44 → 21:58` da sessão do drone.
- `data/logs/switch.jsonl` — linha `camera → drone` PASS às 21:33:47.
- `data/logs/watchdog.jsonl` — sem alerta na janela de regime.

## Estado final

- `active_source=camera` (H.265), container `healthy`, `Restarts=0`,
  `OOMKilled=false`, `exit 0`, `StartedAt=2026-10-31T22:10:13.386Z`,
  `session_epoch=2026-10-31T22:10:16.353`, câmera em **20,0/20,0 fps**.
- **1** container DeepStream; `vision-mediamtx`, `vision-dashboard`,
  `vision-watchdog` e `vision-capture-web` no ar.
- Sem publisher, sonda ou capturer órfão; `deploy/.env` intocado.
- `error_count=0` / `last_error=null` no dashboard.

## Correção da classificação (mesma data)

O achado 3 foi corrigido e validado no mesmo dia: erro do `rtspsrc` durante a
reconexão sai como **`rtsp_reconnect_error`** e erro do ramo descartável como
**`preview_branch_error`**; `pipeline_error` ficou só para falha de análise.
Gate `deepstream-tester` de 2026-10-31 (22:06 → 22:15, **PASS**): na queda
reproduzida, **4 `rtsp_reconnect_error` e 0 `pipeline_error`**, `error_count=0`
no dashboard, reconexão no mesmo `session_epoch`; na queda do MediaMTX,
**`preview_branch_error`** com a análise seguindo em 20/20 fps. Suíte: **420
testes**.
