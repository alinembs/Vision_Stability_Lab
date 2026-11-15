# Vision Stability Lab — entrada única para subir e operar o stack.
#
# Uso típico:
#   make            # ajuda
#   make doctor     # a plataforma bate com a imagem? (roda antes de tudo)
#   make up         # sobe o stack inteiro (constrói o que faltar)
#   make status     # o que está no ar + fonte ativa + saúde
#   make logs       # acompanha os logs do pipeline
#   make test       # suíte do host + suíte do dashboard no container
#
# O mesmo arquivo serve Orin NX (L4T R36 / DeepStream 7.1) e Orin Nano
# (L4T R39 / JetPack 7.2 / DeepStream 9.1, imagem consolidada
# `nvcr.io/nvidia/deepstream:9.1-...`): `make doctor` diagnostica a plataforma
# e diz exatamente o que falta. Ver `docs/plans/2026-11-01-portabilidade-orin-nano.md`.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE_DIR := deploy
COMPOSE := docker compose -f docker-compose.yml
COMPOSE_OVERLAY := docker compose -f docker-compose.yml -f docker-compose.switch.yml
APP := vision-stability-app
DASHBOARD := vision-dashboard
DASHBOARD_URL ?= http://localhost:8081
# Onde o painel é publicado. 0.0.0.0 = LAN (modo leitura). No modo troca
# (`make up-switch`) o padrão vira loopback: o socket do Docker é
# root-equivalente e o token viaja em HTTP claro (H1 da revisão do E4).
DASHBOARD_BIND ?= 0.0.0.0
SWITCH_BIND ?= 127.0.0.1
PYTHON ?= python3
# Porta da página de captura do celular no host (o compose lê a mesma variável).
# 8444 porque a 8443 é do outro projeto de captura deste Jetson.
CAPTURE_PORT ?= 8444
export CAPTURE_PORT

# -- IP da LAN ----------------------------------------------------------------
# Detectado **a cada execução** (`scripts/host_ip.sh`): em campo o roteador
# pode entregar outro endereço ao Jetson (2026-11-02: o Nano saiu de .129 para
# 192.168.0.2) e as portas do MediaMTX ficam amarradas a `LAN_IP` — com o IP
# antigo o `docker compose up` morre em `cannot assign requested address`.
# `LAN_IP=<ip> make up` força um valor; `deploy/.env` não precisa mais dele
# (variável de ambiente vence o `.env` no compose).
LAN_IP ?= $(shell ./scripts/host_ip.sh 2>/dev/null)
export LAN_IP

# -- plataforma ---------------------------------------------------------------
# L4T R36 = JetPack 6 (DeepStream 7.1, imagem `deepstream-l4t`).
# L4T R39 = JetPack 7 (DeepStream 9.1, imagem consolidada `deepstream`).
L4T := $(shell sed -n 's/^# R\([0-9]*\) (release), REVISION: \([0-9.]*\).*/\1.\2/p' \
	/etc/nv_tegra_release 2>/dev/null | head -1)
L4T_MAJOR := $(firstword $(subst ., ,$(L4T)))
ifeq ($(L4T_MAJOR),36)
DS_ESPERADO := 7.1
DS_IMAGEM_PADRAO := nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch
else ifeq ($(L4T_MAJOR),39)
DS_ESPERADO := 9.1
DS_IMAGEM_PADRAO := nvcr.io/nvidia/deepstream:9.1-triton-multiarch
else
DS_ESPERADO := desconhecido
DS_IMAGEM_PADRAO := nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch
endif
# `deploy/.env` vence (é o que o compose lê); sem ele, o padrão da plataforma.
DS_BASE_IMAGE_EFF := $(or $(shell sed -n 's/^DS_BASE_IMAGE=//p' \
	$(COMPOSE_DIR)/.env 2>/dev/null | tail -1),$(DS_IMAGEM_PADRAO))

.PHONY: help doctor up up-build build up-switch down restart restart-app \
	status logs logs-app health test test-host test-dashboard source sources \
	switch shell certs ip url

help: ## Mostra esta ajuda
	@echo "Vision Stability Lab — comandos:"
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Plataforma detectada: L4T $(or $(L4T),desconhecido) / DeepStream esperado $(DS_ESPERADO)"
	@echo "Base de imagem em uso: $(DS_BASE_IMAGE_EFF)"

doctor: ## Diagnostica a plataforma antes de subir (bloqueia só o que impede)
	@echo "== plataforma =="
	@echo "  L4T:            $(or $(L4T),desconhecido)"
	@echo "  DS esperado:    $(DS_ESPERADO)"
	@echo "  imagem base:    $(DS_BASE_IMAGE_EFF)"
	@echo "  IP da LAN:      $(LAN_IP)"
	@echo "  memória livre:  $$(free -m | awk 'NR==2 {print $$7 " MB"}')"
	@echo ""
	@echo "== pré-requisitos =="
	@set -e; \
	if ! docker info >/dev/null 2>&1; then \
		echo "  [FALHA] daemon do Docker inacessível (grupo docker? serviço parado?)"; exit 1; \
	fi; echo "  [ok] docker"; \
	if docker info 2>/dev/null | grep -qi nvidia; then \
		echo "  [ok] runtime NVIDIA"; \
	else \
		echo "  [FALHA] runtime NVIDIA ausente no Docker (nvidia-container-toolkit)"; exit 1; \
	fi; \
	if [ -f $(COMPOSE_DIR)/.env ]; then echo "  [ok] $(COMPOSE_DIR)/.env"; \
	else echo "  [FALHA] $(COMPOSE_DIR)/.env ausente (copie de .env.example e preencha RTSP_URI)"; exit 1; fi; \
	if grep -q '^RTSP_URI=.\+' $(COMPOSE_DIR)/.env; then echo "  [ok] RTSP_URI definida"; \
	else echo "  [FALHA] RTSP_URI vazia em $(COMPOSE_DIR)/.env"; exit 1; fi; \
	if [ -z "$(LAN_IP)" ] || [ "$(LAN_IP)" = "127.0.0.1" ] || [ "$(LAN_IP)" = "0.0.0.0" ]; then \
		echo "  [FALHA] IP da LAN não detectado (scripts/host_ip.sh) — defina LAN_IP=<ip>"; exit 1; \
	fi; echo "  [ok] LAN_IP=$(LAN_IP)"; \
	env_lan_ip=$$(sed -n 's/^LAN_IP=//p' $(COMPOSE_DIR)/.env 2>/dev/null | tail -1 | tr -d '" \r'); \
	if [ -n "$$env_lan_ip" ] && [ "$$env_lan_ip" != "$(LAN_IP)" ]; then \
		echo "  [aviso] $(COMPOSE_DIR)/.env tem LAN_IP=$$env_lan_ip (ignorado: vale o detectado) — remova a linha"; \
	fi; \
	if command -v ip >/dev/null 2>&1 && ! ip -4 addr show | grep -q "inet $(LAN_IP)/"; then \
		echo "  [FALHA] $(LAN_IP) não está em nenhuma interface deste host (confira o IP do roteador)"; exit 1; \
	fi; \
	porta_usada=$$(ss -ltn 2>/dev/null | awk '{print $$4}' | grep -c ':8081$$' || true); \
	if [ "$$porta_usada" -gt 0 ] && ! curl -fsS -m 2 $(DASHBOARD_URL)/api/snapshot >/dev/null 2>&1; then \
		echo "  [FALHA] porta 8081 ocupada por outro processo (o VSL usa 8081 porque 8080 é do arquo-web)"; exit 1; \
	fi; echo "  [ok] porta 8081"
	@echo ""
	@if [ "$(DS_ESPERADO)" = "desconhecido" ]; then \
		echo "[aviso] plataforma não reconhecida: confirme a base de imagem no deploy/.env"; \
	elif [ "$(DS_BASE_IMAGE_EFF)" != "$(DS_IMAGEM_PADRAO)" ]; then \
		echo "[aviso] DS_BASE_IMAGE sobrescrita no deploy/.env: $(DS_BASE_IMAGE_EFF)"; \
		echo "        esperado para L4T $(L4T): $(DS_IMAGEM_PADRAO)"; \
	else \
		echo "[ok] base de imagem casa com a plataforma (DS $(DS_ESPERADO))"; \
	fi
	@if [ "$(DS_ESPERADO)" = "9.1" ]; then \
		if ls $(COMPOSE_DIR)/wheels/pyds-*-cp312-*.whl >/dev/null 2>&1; then \
			echo "[ok] wheel pyds cp312 presente (base com Python 3.12)"; \
		else \
			echo "[FALHA] DS 9.1 usa Python 3.12 e falta o wheel pyds cp312 em $(COMPOSE_DIR)/wheels/"; \
			echo "        baixe o wheel cp312 do release v1.2.2 (ver plano de portabilidade)"; \
			exit 1; \
		fi; \
	fi
	@echo ""
	@echo "doctor: ok"

certs: ## Garante o certificado do servidor para o IP atual (preserva a CA)
	@rc=0; VSL_LAN_IP=$(LAN_IP) ./deploy/certs/generate-certs.sh --ensure || rc=$$?; \
	case $$rc in \
		0) echo "  [ok] certificado já cobre o IP atual ($(LAN_IP))";; \
		10) echo "  [ok] certificado do servidor reemitido para $(LAN_IP) (CA preservada: nada a reinstalar nos clientes)";; \
		11) echo "  [aviso] CA regerada: reinstale deploy/certs/ca.crt nos navegadores/celulares";; \
		*) echo "  [FALHA] generate-certs.sh saiu com $$rc"; exit $$rc;; \
	esac; \
	if [ "$$rc" = "10" ] || [ "$$rc" = "11" ]; then \
		(cd $(COMPOSE_DIR) && $(COMPOSE) restart mediamtx capture-web >/dev/null 2>&1) || true; \
	fi

ip url: ## Mostra o IP detectado e as URLs para abrir no navegador
	@echo "IP da LAN:      $(LAN_IP)"
	@echo "painel:         http://$(LAN_IP):8081"
	@echo "captura:        https://$(LAN_IP):$(CAPTURE_PORT)/capture.html"
	@echo "CA (download):  https://$(LAN_IP):$(CAPTURE_PORT)/ca.crt"
	@echo "WHIP (celular): https://$(LAN_IP):8889/<path>/whip"
	@echo "nome estável:   https://$$(hostname).local:$(CAPTURE_PORT)/capture.html  (se houver mDNS na rede)"

up: doctor certs ## Sobe o stack (use `make up-build` quando mudou imagem/Dockerfile)
	cd $(COMPOSE_DIR) && $(COMPOSE) up -d
	@$(MAKE) --no-print-directory status

up-build: doctor certs ## Constrói as imagens e sobe o stack (Dockerfile/deps mudaram)
	cd $(COMPOSE_DIR) && $(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

build: ## Só constrói as imagens (sem subir)
	cd $(COMPOSE_DIR) && $(COMPOSE) build

up-switch: doctor ## Sobe com o overlay de troca pela tela (exige SWITCH_TOKEN)
	@if [ -z "$(SWITCH_TOKEN)" ]; then \
		echo "ERRO: SWITCH_TOKEN vazio. Uso: make up-switch SWITCH_TOKEN=segredo-forte"; \
		echo "      (o endpoint fica 503 sem token; o socket do Docker só é montado com o overlay)"; \
		exit 1; \
	fi
	@# H1 da revisão do E4: com socket do Docker (root-equivalente) e token em
	@# HTTP claro, o painel NÃO escuta na LAN. Acesso por túnel SSH.
	cd $(COMPOSE_DIR) && DASHBOARD_BIND=$(SWITCH_BIND) SWITCH_TOKEN=$(SWITCH_TOKEN) \
		SWITCH_ENABLED=1 $(COMPOSE_OVERLAY) up -d --build dashboard
	@echo "troca pela tela ligada (POST /api/source/switch)"
	@echo "painel escutando em $(SWITCH_BIND):8081 — acesse por túnel:"
	@echo "  ssh -L 8081:127.0.0.1:8081 <usuario>@$$(hostname -I | awk '{print $$1}')"
	@echo "  depois abra http://localhost:8081 no seu navegador"

down: ## Para o stack (containers removidos, dados preservados)
	cd $(COMPOSE_DIR) && $(COMPOSE_OVERLAY) down --remove-orphans
	@echo "stack parado"

restart: ## Reinicia todos os serviços
	cd $(COMPOSE_DIR) && $(COMPOSE) restart

restart-app: ## Reinicia só o pipeline (é o que a troca de fonte faz)
	docker restart $(APP)
	@echo "aguarde ~40 s: 'make logs-app' mostra startup/source_active/pipeline_playing"

status: ## O que está no ar, fonte ativa e saúde
	@docker ps --filter name=vision --format 'table {{.Names}}\t{{.Status}}'
	@echo ""
	@printf "fonte ativa (arquivo): "; cat data/config/active_source 2>/dev/null || echo "(ausente)"
	@printf "fonte ativa (sessão):  "; curl -fsS -m 2 $(DASHBOARD_URL)/api/snapshot 2>/dev/null \
		| $(PYTHON) -c 'import json,sys; d=json.load(sys.stdin); s=d.get("source") or {}; print(s.get("id"), "|", s.get("name"), "| health:", (d.get("health") or {}).get("status"))' \
		2>/dev/null || echo "(dashboard não respondeu)"

logs: ## Acompanha os logs do pipeline (uma linha JSON por evento)
	docker logs -f --tail 30 $(APP)

logs-app: logs ## Apelido de `logs`

health: ## Saúde do dashboard + última métrica do pipeline
	@curl -fsS $(DASHBOARD_URL)/api/snapshot | $(PYTHON) -m json.tool | head -30

test: test-host test-dashboard ## Suíte completa (host + dashboard no container)

test-host: ## pytest do host (deepstream + scripts, sem GPU)
	$(PYTHON) -m pytest deepstream/tests scripts/tests -q

test-dashboard: ## unittest dentro do container do dashboard
	docker exec -w /app/dashboard/backend $(DASHBOARD) \
		python -m unittest discover -s /app/dashboard/tests

source: ## Mostra a fonte ativa no disco
	@cat data/config/active_source; echo

sources: ## Lista o catálogo pelo dashboard (ativo + paths mascarados)
	@curl -fsS $(DASHBOARD_URL)/api/sources | $(PYTHON) -m json.tool

switch: ## Troca a fonte ativa: make switch SOURCE=drone
	@if [ -z "$(SOURCE)" ]; then echo "ERRO: informe a fonte. Ex.: make switch SOURCE=drone"; exit 1; fi
	./scripts/switch_source.sh $(SOURCE)

shell: ## Shell dentro do container do pipeline
	docker exec -it $(APP) bash
