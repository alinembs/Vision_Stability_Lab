# Runbook — captura pelo celular (Android) por WHIP no MediaMTX

> **Escopo deste runbook: Android apenas.**
> **iOS está fora do escopo por decisão do projeto (D6).** Não há iPhone no time,
> o Safari tem stack de codec própria (o `H264 42001f` medido no Chrome Android
> não prova nada sobre o WebKit) e o caminho de contenção é o upload de arquivo.
> Este documento **não** cobre Safari: se aparecer um iPhone em campo, é decisão
> de produto, não um passo deste runbook.
>
> **Estado da validação (2026-10-30, Orin `192.168.59.102`):**
> - **provado nesta máquina:** CA + certificado com SAN de IP validados por
>   `curl --cacert` e `openssl s_client` (`Verify return code: 0`) nas **duas**
>   portas (8889 do WHIP e 8443 da página), WHIP aceitando offer H.264 sintético
>   com candidatos **só** no IP da LAN (`201` + `DELETE 200`), binds em
>   `192.168.59.102` e nada em `0.0.0.0`;
> - **NÃO provado nesta máquina:** celular real publicando (não há Android nesta
>   sessão) e o gate de codec **executado no Chrome** (a lógica está coberta no
>   host: 13/13 fixtures do `codec-gate.js` em Node e sintaxe dos dois `.js`).
>   Os passos 4 e 5 são de campo.

## Arquitetura em uma linha

```
celular (Chrome Android)
  └─ https://192.168.59.102:8443/capture.html   nginx  → contexto seguro + getUserMedia
       └─ POST https://192.168.59.102:8889/phone/whip   MediaMTX (TLS, SDP)
            └─ rtsp://192.168.59.102:8554/phone        pipeline (DeepStream)
```

O path `phone` é o mesmo que o pipeline lê por RTSP. O MediaMTX **não
transcodifica**: o celular tem de publicar **H.264**, e é por isso que o gate de
codec roda no navegador antes do `POST`.

## 1. Gerar a CA e o certificado (uma vez por deployment)

> **Pré-requisito duro de subida, não passo opcional (I3).** Sem
> `deploy/certs/server.crt` **e** `server.key`, os dois serviços que fazem a
> captura existirem entram em **loop de restart com exit 0** e nem a página de
> captura nem o preview do dashboard sobem:
> `open /certs/server.crt: no such file or directory` (MediaMTX, que por isso
> não republica o `live` e derruba o `rtspclientsink` do pipeline) e
> `cannot load certificate "/certs/server.crt"` (nginx da 8443). O sintoma
> engana porque o resto do stack fica `healthy`; desde 2026-10-30 o
> `capture-web` tem `healthcheck` que bate na própria 8443, justamente para
> não repetir "tudo saudável, nada funcionando".
> Rode este script **antes** do primeiro `docker compose up`.

```bash
deploy/certs/generate-certs.sh                  # IPs/nomes atuais do host
VSL_LAN_IP=<ip-do-campo> deploy/certs/generate-certs.sh   # força um IP extra no SAN
```

Saída em `deploy/certs/`: `ca.crt` (vai para o celular) e `server.crt` /
`server.key` (montados em `/certs`, read-only, no **MediaMTX** e no **nginx da
8443**). **Nada disso entra no Git** — a pasta é ignorada por inteiro, exceto o
`.gitignore` dela.

O modo padrão é `--ensure`: o script reaproveita a CA existente e só reemite o
certificado do servidor quando o SAN não cobre os IPs/nomes **atuais** do host.
Na prática você não precisa rodar isto na mão — `make up` chama `make certs`
antes de subir. Trocar de rede (o roteador entrega outro IP por DHCP) reemite
o certificado do servidor **preservando a CA**, então o celular que já instalou
o `ca.crt` continua confiando, sem reinstalar nada (INC-009).

`VSL_LAN_IP` é validado antes de qualquer arquivo ser escrito: precisa ser um
IPv4 com cada octeto entre 0 e 255 (o valor entra no `subjectAltName`, e sem
validação `1.2.3.4,DNS:x` injetaria uma entrada no certificado). O mesmo vale
para `VSL_LOCAL_DOMAIN` (só letras, dígitos, ponto e hífen). Valor inválido sai
com código 2 e **não deixa arquivo no diretório de saída** — a geração acontece
num diretório temporário e só o conjunto completo é movido no fim, então uma
falha no meio não exige `--force` (que invalidaria a CA já instalada).

`--force` regera **tudo, inclusive a CA**, e é o único caminho que obriga todo
device a receber o novo `ca.crt`. Use-o para CA perdida/corrompida ou para
rotacionar a CA de propósito — **não** para troca de IP.

Conferir o SAN:

```bash
openssl x509 -in deploy/certs/server.crt -noout -text | grep -A1 'Subject Alternative Name'
# DNS:localhost, DNS:<hostname>, DNS:<hostname>.local, DNS:vsl.local,
# IP Address:<ip-atual-da-LAN>, IP Address:127.0.0.1 (as bridges do Docker ficam
# fora de propósito: mudam a cada `docker compose` e ninguém acessa por elas)
```

## 2. Subir os serviços

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d mediamtx capture-web
docker ps --filter name=vision- --format '{{.Names}}\t{{.Status}}'
```

A 8443 publica **só** a página de captura e o que ela carrega
(`capture.html`, `capture.js`, `codec-gate.js`, `codec-gate-selftest.html`,
`style.css`). `https://<ip>:8443/` e `…/index.html` respondem **404** de
propósito: o dashboard (SPA do FastAPI, com `/ws` e `/api/history`) não existe
nesta origem, e servi-lo aqui abria uma cópia quebrada em silêncio. O dashboard
continua em `http://<ip>:8081`.

Recriar o `vision-mediamtx` derruba o preview do dashboard por alguns segundos.
**Efeito colateral medido em 2026-10-30 (não é preciso intervir, mas é bom
saber):** ao perder o `rtspclientsink` do preview, o `vision-stability-app`
encerra com **exit 0** e o `restart: unless-stopped` o traz de volta em ~4 s.
Na sequência o `vision-watchdog` pode disparar `no_frames_escalation` (~3 min
sem frames na janela dele) e fazer um `docker restart` — o que zera o
`RestartCount` do container. O estado final observado foi `healthy`,
processando 20 fps, com a câmera intacta (o container **não** é recriado).
Se for medir estabilidade logo depois, espere a telemetria normalizar
(`data/logs/live.jsonl` com `fps_processed > 0` e `interval_metrics` novo).

## 3. Instalar a CA no Android

Sem `ca.crt` instalado, o Chrome do celular recusa o certificado do Jetson e —
pior — **não** considera a página em contexto seguro, então `getUserMedia` nem
aparece.

1. Copie `deploy/certs/ca.crt` para o aparelho (cabo, `adb push`, e-mail, cartão).
   No host: `adb push deploy/certs/ca.crt /sdcard/Download/`
2. **Configurações → Segurança → Criptografia e credenciais → Instalar um
   certificado → Certificado de CA** (em Android 10/11 o rótulo é "Certificado
   de CA"; em versões antigas, "Credencial de VPN e app").
3. Confirme o aviso de que a CA é de terceiros e dê um nome reconhecível
   (`VSL Internal CA`).
4. Confira a impressão digital SHA-256 contra a que o script imprime:
   ```bash
   openssl x509 -in deploy/certs/ca.crt -noout -fingerprint -sha256
   ```

## 4. Capturar (no celular)

1. Abra **`https://<ip-do-host>:8443/capture.html`** — HTTPS, porta **8443**.
   Rode `make ip` no host para ver o endereço do momento; com mDNS na rede,
   `https://<hostname>.local:8443/capture.html` sobrevive à troca de DHCP.
   O cadeado tem de estar presente, sem aviso.
2. Confira a linha **contexto seguro**: `sim (...)`. Se estiver `NÃO`, o Chrome
   não vai liberar a câmera (ver a seção 6).
3. Confira **H.264 no aparelho**: `N perfil(is) H.264 disponíveis`. Se disser
   "nenhum H.264", o aparelho não pode publicar (não existe conversão do lado
   do servidor).
4. **Iniciar captura** → autorize a câmera.
5. As linhas que importam durante a captura:
   - **resolução enviada / fps enviado** — de `outbound-rtp`, é o que o
     MediaMTX recebe de verdade;
   - **banda** — deve ficar perto de **2,5 Mbps** (D-11). Acima disso a gravação
     fragmenta em campo;
   - **limitação do encoder** — `bandwidth` (rede) ou `cpu` (encoder);
   - **câmera (`getSettings`)** — descreve o sensor, **não** a transmissão:
     costuma mentir para cima. Depois do `·` vem a câmera efetiva
     (`traseira`/`frontal`);
   - **aparelho** — o `deviceId` efetivo, com o rótulo que o Chrome informa
     (ex.: `camera2 0, facing back`); é a linha que separa "pedi a traseira" de
     "o aparelho entregou a traseira";
   - **orientação** — tem de dizer **paisagem** (ver "Câmera traseira e
     paisagem" abaixo).
6. **Encerrar** faz `DELETE` na sessão. Fechar a aba também faz (`pagehide`),
   mas o botão é o caminho confiável: o MediaMTX só libera o path quando a
   sessão cai.

### Câmera traseira e paisagem

**Por que a traseira é o padrão.** É a câmera que aponta para a cena que o
pipeline mede. O Chrome Android abria a **frontal** quando o `getUserMedia` não
pedia `facingMode`, e o operador aparecia no lugar do ambiente. O seletor
**câmera** na tela oferece `traseira` (padrão) e `frontal`; a escolha fica em
`localStorage` na chave **`vsl-facing`** (`environment` = traseira,
`user` = frontal), lida com `try/catch` para não quebrar em navegação privada.

A escolha vale **no início da captura**: durante a publicação o seletor fica
`disabled` com a nota "encerre para trocar de câmera" — trocar no meio exigiria
renegociar a trilha com o MediaMTX, caminho que não é suportado. Se o aparelho
não tem a câmera pedida (`OverconstrainedError`), a página cai para `ideal` e
depois para "qualquer câmera" em vez de falhar: um celular só com frontal
publica pela frontal, e o diagnóstico mostra a diferença entre **câmera pedida**
e **câmera (`getSettings`)**.

**Qual versão o celular carregou.** O rodapé mostra o selo
`capture.js 2026-11-08c` (constante `CAPTURE_VERSION`, também no
`console.info`). Sem esse selo não há como distinguir "a correção não funciona"
de "o aparelho está rodando o JS antigo": a página é servida por bind mount,
mas o Chrome guarda o arquivo em cache e pode existir cópia velha em outro
host. Para provar o que o aparelho executou:

1. no celular, leia o rodapé — tem de dizer a mesma versão que está no arquivo
   do host: `grep CAPTURE_VERSION dashboard/frontend/capture.js`;
2. no host, confirme o que o nginx está entregando:
   `curl -sk https://<ip>:8443/capture.js | grep CAPTURE_VERSION`;
3. se o rodapé mostrar versão antiga, o aparelho está com cache. `?v=<algo>` na
   URL **não** invalida `/capture.js` (o script é carregado por `src` fixo):
   recarregue forçado no celular (Chrome → ⋮ → Recarregar, ou feche a aba e
   abra de novo) e confira o rodapé outra vez. Se continuar antigo, o
   host/nginx em uso não é o diretório que você editou.

**Seleção por dispositivo (`deviceId`) — o caminho garantido.** `facingMode`
não é garantia: há aparelho que aceita `exact environment` e devolve a frontal
mesmo assim. Depois que a permissão é concedida, o `capture.js` chama
`enumerateDevices()` e preenche o seletor **dispositivo** com a primeira opção
`automática (traseira/frontal)` (value vazio = cadeia de `facingMode`) e uma
opção por `videoinput`, com o rótulo do aparelho (ex.:
`camera2 0, facing back`; rótulo vazio vira `câmera N`). A opção selecionada é
a da trilha ativa (`track.getSettings().deviceId`).

A escolha fica em `localStorage` na chave **`vsl-facing-device`** (mesmo
`try/catch` de `vsl-facing`) e vale no **próximo início**: durante a publicação
o seletor fica `disabled` com a nota "encerre para trocar de câmera". Com um
`deviceId` gravado, o `getUserMedia` pede `deviceId: {exact: …}` mantendo os
tetos de 1280×720/16:9; se o id não existir mais (`OverconstrainedError`/
`NotFoundError` — aparelho trocado, storage de outra sessão), a preferência é
**apagada** e a página cai na cadeia de `facingMode`, em vez de falhar.
`NotReadableError` continua fatal, com a mensagem de câmera ocupada. Sem
`enumerateDevices` (ou se ele falhar), o seletor fica só com "automática" e
tudo funciona como antes. Para voltar ao automático, escolha
`automática (traseira/frontal)` — a chave é removida do `localStorage`.

**Roteiro de diagnóstico da traseira.** 1) Inicie a captura uma vez — é o que
libera os rótulos dos aparelhos. 2) Encerre e escolha no seletor **dispositivo**
a opção cujo rótulo diz `facing back`. 3) Inicie de novo e leia as três linhas:

- **câmera pedida** — deve começar com `deviceId …`;
- **aparelho** — o rótulo do `deviceId` efetivo (ou o id truncado, quando o
  Chrome não informa nome);
- **câmera (`getSettings`)** — o que o sensor reporta (`traseira`/`frontal`).

Se **aparelho** for o dispositivo com `facing back` e **câmera
(`getSettings`)** ainda disser `frontal`, o pedido foi cumprido e quem rotula
errado é o sensor/Chrome: limitação do aparelho, não da página. A prova final é
a **prévia local** — se ela mostra a cena (e não o operador), é a câmera
traseira que está sendo publicada, independente do rótulo.

**Por que paisagem importa.** O `nvstreammux` do pipeline é fixo em **1280×720**
(`deepstream/pipeline_real.py:425`) e roda **sem `enable-padding`**: ele
reescala o que chega para 1280×720 sem preservar o aspecto. Em retrato o teto de
720p age na **altura**, então a captura sai ~404×720 e o mux estica para 16:9 —
imagem deformada, detecção pior. Em paisagem o pedido de 1280×720/16:9 é
atendido como está. Por isso a página avisa (banner de aviso, não erro) quando
`getSettings()` vem com altura maior que a largura: **o aviso não bloqueia a
publicação**, ele só pede para girar o aparelho.

O aviso (e o seletor de câmera) é avaliado **no início da captura**. Girar o
aparelho com a sessão publicando não renegocia o frame: encerre, confira a linha
**orientação** e inicie de novo — o MediaMTX só libera o path depois do `DELETE`.

Se outro aplicativo (ou outra aba) estiver com a câmera escolhida aberta, o
Chrome lança `NotReadableError` e a página **falha de propósito**, com a mensagem
"está em uso por outro aplicativo": cair calado para a outra câmera publicaria a
imagem errada.

No host, o mesmo path aparece no log do container:

```bash
docker logs -f vision-mediamtx | grep -E '\[path phone\]|\[WebRTC\]'
```

## 5. Verificar sem navegador

```bash
# 1) TLS das duas portas com a CA interna
curl --cacert deploy/certs/ca.crt -s -o /dev/null -w '%{http_code}\n' https://192.168.59.102:8889/   # 404 = serviço no ar
curl --cacert deploy/certs/ca.crt -s -o /dev/null -w '%{http_code}\n' https://192.168.59.102:8443/capture.html  # 200
curl --cacert deploy/certs/ca.crt -s -o /dev/null -w '%{http_code}\n' https://192.168.59.102:8443/index.html    # 404 (whitelist)
curl --cacert deploy/certs/ca.crt -s -o /dev/null -w '%{http_code}\n' https://192.168.59.102:8443/             # 404 (whitelist)
curl --cacert deploy/certs/ca.crt -s -o /dev/null -w '%{http_code}\n' https://192.168.59.102:8443/certs/server.key  # 404
openssl s_client -connect 192.168.59.102:8889 -CAfile deploy/certs/ca.crt -servername vsl.local \
  </dev/null 2>/dev/null | grep 'Verify return code'   # 0 (ok)
openssl s_client -connect 192.168.59.102:8443 -CAfile deploy/certs/ca.crt \
  </dev/null 2>/dev/null | grep 'Verify return code'   # 0 (ok)

# 2) WHIP: candidatos ICE e codec negociado
python3 scripts/probe/whip_probe.py --cacert deploy/certs/ca.crt --path phone
```

O relatório tem de trazer:

- `status: 201` e `location`/`session_url` (e `delete.ok: true` no fim);
- exit code **0** — a sonda sai **4** quando o POST é aceito mas o `DELETE`
  falha (o path pode continuar publicado) e **2** quando o servidor recusa ou
  está inacessível. Script que só olha "rodou" precisa olhar o código;
- `tls: verify:deploy/certs/ca.crt` — se estiver `verify:sistema`, faltou
  `--cacert` e a sonda está falhando fechada de propósito;
- `candidate_hosts: ["<ip-da-LAN>"]` — **nenhum** `127.0.0.1` nem `172.x`;
- `answer_codecs: ["96 H264/90000"]`.

Autoteste do gate de codec (executável, 13 casos): abra
`https://192.168.59.102:8443/codec-gate-selftest.html` no navegador do aparelho
— deve mostrar **PASS 13/13**. No host, dá para rodar os mesmos fixtures sem
navegador:

```bash
node -e "const g=require('./dashboard/frontend/codec-gate.js').CodecGate;\
const r=g.runSelfTest(); r.forEach(x=>console.log((x.pass?'PASS':'FAIL'),x.name));\
process.exit(r.every(x=>x.pass)?0:1)"
```

## 6. Duas perguntas que sempre voltam

**Por que `http://192.168.59.102:8443` não funciona?**
Porque `getUserMedia` exige **contexto seguro**, e o navegador decide isso pela
origem que serviu o **documento** — não pelo destino do `fetch`. Em HTTP na LAN,
`window.isSecureContext` é `false` e o Chrome **nem expõe**
`navigator.mediaDevices` (medido em 2026-10-27 no spike do arquo: `getUserMedia`
exposto = `false`, `isSecureContext` = `false`). Não existe flag do Chrome que
resolva isso no aparelho do operador.

**Por que o TLS da porta 8889 (WHIP) não basta?**
Porque ele autentica o **servidor de mídia**, não a página. Uma página servida em
`http://` continua insegura mesmo fazendo `POST` para `https://` — e o
`POST` de uma origem HTTP para uma HTTPS é bloqueado como mixed content. Daí o
nginx em **8443** (`deploy/nginx/capture-web.conf`) servindo o mesmo diretório
`dashboard/frontend/` com o **mesmo** certificado: um `ca.crt` no celular
cobre as duas portas.

## 7. Checklist de campo — sintoma → causa → o que medir

| Sintoma | Causa mais provável | O que medir |
|---|---|---|
| **Página abre com aviso de certificado / `ERR_CERT_AUTHORITY_INVALID`** | a CA não está instalada no Android (ou é a antiga: `--force` regerou) | `openssl x509 -in deploy/certs/ca.crt -noout -fingerprint -sha256` contra o que aparece na tela do aparelho; reinstalar em Configurações → Segurança → CA |
| **"contexto seguro: NÃO" / câmera não abre** | a página foi aberta em `http://` ou pela porta errada | barra de endereço: tem de ser `https://…:8443/capture.html`. `chrome://flags` não resolve — precisa da CA + HTTPS |
| **WHIP conecta e não há vídeo** | candidato ICE apontando para `127.0.0.1` ou para IP de bridge (`172.x`) | `candidate_hosts` do `whip_probe.py --cacert …`: só pode aparecer o **IP da LAN atual** (`make ip`). Config: `MTX_WEBRTCADDITIONALHOSTS=${LAN_IP}`, injetado pelo compose, + `webrtcIPsFromInterfaces: no` (`mediamtx/mediamtx.yml`) |
| **Vídeo em VP8/VP9** | aparelho sem H.264, ou navegador ignorou a preferência | a tela mostra "sem H.264" e a captura **não** começa (é o gate, não um bug). `ffprobe rtsp://192.168.59.102:8554/phone` confirma o codec no path. Não há conversão no servidor (D-2) |
| **`POST` responde `404` e o log do MediaMTX fica vazio** | endpoint sem o `/whip` final | a linha "endpoint WHIP" na tela tem de terminar em `/phone/whip`. `POST /phone` é rota inexistente |
| **`POST` responde `409`** | o path ainda está publicado por uma sessão anterior (aba fechada sem `DELETE`, ou celular dormiu) | `docker logs vision-mediamtx` (`[path phone] stream is already available`); aguarde o ICE expirar (~30 s) ou reinicie o `vision-mediamtx`; confira se a aba antiga segue aberta |
| **Conecta, mostra "publicando", mas o pipeline não recebe** | mídia não chega: UDP 8189 bloqueado na rede, ou ICE incompleto | `ss -lntup \| grep 8189` (tem de estar em `192.168.59.102`); `ffprobe`/`rtspsrc` no path; no log do MediaMTX, `[path phone] is publishing` |
| **Banda acima de ~2,5 Mbps ou resolução acima de 720p** | o teto de banda **não** foi aplicado (o Chrome recusa `setParameters` antes do offer; a segunda tentativa é depois do answer) | linha "teto de banda": se disser "recusado — transmitindo no default", a rodada **não** testou o teto. A captura vale, a qualidade é a de chamada de vídeo |
| **Resolução enviada abaixo de 1280×720** | o encoder adaptou (1280×720 é `ideal` **e** `max`: teto, não exigência — o aparelho que não tem 720p entrega o que tem) | leia a **resolução enviada**, não `getSettings()`. Aproxime o aparelho e melhore a luz |
| **Vem em retrato / imagem esticada (gente e objeto achatados)** | aparelho na vertical: o teto de 720p age na altura (~404×720) e o `nvstreammux` (1280×720 fixo, **sem** `enable-padding`) estica para 16:9 | linha **orientação**: `retrato` é o sintoma. Gire o aparelho para paisagem (o aviso na tela não bloqueia o publish) e confira **resolução enviada** = 1280×720 |
| **"A câmera traseira está em uso por outro aplicativo"** | `NotReadableError`: outro app (ou outra aba de captura) está com a câmera escolhida | feche o app/aba e clique em **Iniciar captura** de novo. A página não troca de câmera sozinha de propósito — publicaria a imagem errada |
| **fps enviado baixo (< 24)** | quase sempre luz baixa; o Chrome derruba a taxa | "fps enviado" (`outbound-rtp`) e "limitação do encoder" (`cpu` vs `bandwidth`) |
| **`curl --cacert` passa mas `curl` sem ele falha** | esperado: a CA interna não está no store do sistema | é o comportamento correto (fail closed). Sempre passe `--cacert deploy/certs/ca.crt` |
| **`docker ps` mostra tudo `healthy` e nada funciona** | os healthchecks validam config/telemetria, não o caminho de mídia | `whip_probe.py --path phone` + `docker logs vision-mediamtx`; "healthy" não é prova de vídeo |
| **Qualquer host da LAN publica e lê os paths** | **DIV-003**: o MediaMTX do VSL não tem autenticação | só usar em rede confiável; autenticação é dívida aberta (`docs/incident-log.md`) |

## 8. O que ficou pendente de campo

- **Celular Android real publicando pela página** — a sinalização, o TLS e os
  candidatos estão provados por `curl`/sonda, mas `getUserMedia` + encode em
  H.264 **no aparelho** só se comprova com um Android. É a próxima rodada.
- **Autoteste do gate no Chrome do aparelho** (`codec-gate-selftest.html`) —
  os 13 fixtures passam no host (Node); no navegador, com o
  `RTCRtpSender.getCapabilities` real, ainda não foi executado.
- **`qualityLimitationReason` e a banda efetiva em Wi-Fi de campo** — dependem
  do aparelho e do rádio, não do host.
