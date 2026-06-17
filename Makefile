# open-webui-service operator targets.
#
# A thin deployment of the upstream Open WebUI image
# (ghcr.io/open-webui/open-webui, pinned by tag + digest in
# docker/compose.yaml). It runs as the chat UI on the shared, external
# `inference-net` and speaks the OpenAI API to an OpenAI-compatible endpoint
# on that network (the LiteLLM proxy from vllm-service). All state lives in
# the external `open-webui-data` volume, created out-of-band by `make volume`.
#
# Requires a .env at the repo root (compose is invoked with --env-file .env).
# Copy it from the example first:  cp .env.example .env

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

# Read the shared network name from .env if present, otherwise fall back.
# Keep in sync with docker/compose.yaml (networks.inference-net.name).
INFERENCE_NET ?= $(or $(strip $(shell test -f .env && grep -E '^INFERENCE_NET=' .env | cut -d= -f2)),inference-net)

# External data volume. Declared `external: true` in docker/compose.yaml, so it
# must be created out-of-band (like the network) and survives `down`/`down -v`.
# Keep in sync with docker/compose.yaml (volumes.open-webui-data).
OPEN_WEBUI_VOLUME := open-webui-data

COMPOSE     := docker compose --env-file .env -f docker/compose.yaml
COMPOSE_DEV := docker compose --env-file .env -f docker/compose.yaml -f docker/compose.override.yaml
TS          := $(shell date -u +%Y%m%dT%H%M%SZ)

.PHONY: help network volume pull bundle up up-dev stop down restart logs ps health nuke

help:
	@echo "open-webui-service — Open WebUI chat UI on inference-net."
	@echo
	@echo "Setup (first time):"
	@echo "  cp .env.example .env   # then set OPENAI_API_BASE_URL / OPENAI_API_KEY"
	@echo
	@echo "Lifecycle:"
	@echo "  make network    create the external $(INFERENCE_NET) if missing"
	@echo "  make volume     create the external $(OPEN_WEBUI_VOLUME) if missing"
	@echo "  make pull       pull the pinned upstream image"
	@echo "  make bundle     save the pinned image as an airgap .tar.gz"
	@echo "  make up         start open-webui (production shape, no host ports)"
	@echo "  make up-dev     like 'up', but publishes the UI port on the host"
	@echo "  make down       stop + remove the container (data volume preserved)"
	@echo "  make restart    down + up"
	@echo "  make nuke       DESTROY the open-webui-data volume (interactive)"
	@echo
	@echo "Observability:"
	@echo "  make ps         container status"
	@echo "  make health     status + uptime"
	@echo "  make logs       tail open-webui logs"
	@echo
	@echo "Note: OPENAI_API_BASE_URL must resolve on $(INFERENCE_NET);"
	@echo "the vllm-service LiteLLM proxy is reachable there as 'vllm-router'."

# Create the shared external network (one-time per host; idempotent).
network:
	@docker network inspect $(INFERENCE_NET) >/dev/null 2>&1 \
	  || (echo ">> creating external network $(INFERENCE_NET)" \
	      && docker network create $(INFERENCE_NET))

# Create the external data volume (one-time per host; idempotent). Declared
# external in compose.yaml so app teardown — even `down -v` — can never delete
# user data; only `make nuke` removes it, explicitly.
volume:
	@docker volume inspect $(OPEN_WEBUI_VOLUME) >/dev/null 2>&1 \
	  || (echo ">> creating external volume $(OPEN_WEBUI_VOLUME)" \
	      && docker volume create $(OPEN_WEBUI_VOLUME))

# Pull the pinned upstream image.
pull:
	$(COMPOSE) pull

# Save the pinned image as a versioned airgap tarball for transfer to an
# offline host. Mirrors the *-pulled-*.tar.gz convention of the stack's
# other pulled-image services.
bundle: pull
	@img=$$($(COMPOSE) config --images | head -n1); \
	  out="open-webui-pulled-$(TS).tar.gz"; \
	  echo ">> saving $$img -> $$out"; \
	  docker save "$$img" | gzip > "$$out"; \
	  echo ">> wrote $$out"

# Start in production shape: detached, no host ports — reachable only on
# inference-net (e.g. behind a reverse proxy on that network).
up: network volume
	$(COMPOSE) up --no-build -d

# Like 'up' but layers compose.override.yaml to publish the UI port
# (OPEN_WEBUI_HOST_PORT, default 3000) on the host.
up-dev: network volume
	$(COMPOSE_DEV) up --no-build -d

# Stop the container without removing it.
stop:
	$(COMPOSE) stop

# Stop + remove the container. The open-webui-data volume is preserved.
down:
	$(COMPOSE) down

restart: down up

# Tail logs.
logs:
	$(COMPOSE) logs --tail=200 -f

ps:
	$(COMPOSE) ps

health:
	@$(COMPOSE) ps --format '{{.Name}}\t{{.State}}\t{{.Status}}'

# Destructive: stop + remove the container, then delete the external data
# volume (every chat, user, setting, upload, and RAG vector). Interactive
# confirm. The volume is external, so `down -v` does NOT remove it — it must
# be deleted explicitly once the container (its only user) is gone.
nuke:
	@echo "This will DESTROY all open-webui state (volume: open-webui-data)."
	@read -p "Type 'nuke' to confirm: " confirm && [ "$$confirm" = "nuke" ] \
	  || (echo "aborted"; exit 1)
	$(COMPOSE) down
	docker volume rm $(OPEN_WEBUI_VOLUME)
