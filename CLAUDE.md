# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A thin **deployment wrapper** for [Open WebUI](https://github.com/open-webui/open-webui) — no application source lives here. The repo is Docker Compose config (`docker/`) plus a small vendored airgap bundler (`scripts/`), running a pinned upstream image (`ghcr.io/open-webui/open-webui`, pinned by tag **and** sha256 digest). Changes here are config/ops changes, not code changes.

## Architecture: where this fits

This is one of several sibling deployments under `/home/user/dev/infra/` (`vllm-service`, `translator`, `docint`, `chorus`, `Nextext`, `afd-pipeline-main`, `data-plane`). They communicate over a single shared Docker bridge network, **`inference-net`**, which is declared `external: true` everywhere and created out-of-band (`docker network create inference-net`; siblings do this idempotently via their `make network` target).

Open WebUI is the chat UI. It speaks the OpenAI API and is pointed at an OpenAI-compatible endpoint on `inference-net`. In this stack that endpoint is the **LiteLLM proxy provided by `vllm-service`** (service `router`, port `4000`, network alias `vllm-router`), which fans out to vLLM model backends (chat, embed, rerank, clip, asr, diarize, vad, gliner). So the dependency chain is:

```
open-webui  ──(OpenAI API over inference-net)──►  vllm-service litellm proxy  ──►  vLLM backends
```

To run Open WebUI usefully, `inference-net` must exist and an upstream OpenAI-compatible API must be reachable on it (normally bring up `vllm-service` first). Open WebUI joins `inference-net` under the alias `open-webui-client`.

**No bundled inference provider.** Like the sibling consumers, this app reaches inference *only* over the OpenAI-compatible API; the upstream image's own backends are hard-disabled in `compose.yaml`. The built-in Ollama integration is off (`ENABLE_OLLAMA_API=false`); RAG embeddings go to the same endpoint (`RAG_EMBEDDING_ENGINE=openai`, model `RAG_EMBEDDING_MODEL`) instead of the image's in-process sentence-transformers; STT goes to the endpoint (`AUDIO_STT_ENGINE=openai`) instead of local Whisper; and `OFFLINE_MODE=true` + `HF_HUB_OFFLINE=1` block any runtime model download from HuggingFace. Net: no local models load, and the only inference traffic leaves via `OPENAI_API_BASE_URL`.

Persistence: **external** volume `open-webui-data` → `/app/backend/data` (SQLite DB, users, chats, uploads, RAG vectors). Declared `external: true` in `compose.yaml`, it is created out-of-band by `make volumes` (like the network) and survives every app teardown — even `docker compose down -v`; only `make nuke` removes it (container `down`, then `docker volume rm`). Destroying it wipes all app state.

## Compose layout

Operate via `make` from the repo root — the `Makefile` wraps `docker compose --env-file .env -f docker/compose.yaml` and mirrors the sibling infra services' schema. Two compose files:

- `compose.yaml` — base service: image, environment, the external `open-webui-data` volume, `inference-net` network, and `local` log driver with rotation (50m × 5 files). This is the production shape — **no host ports**. Its `environment:` block hard-disables the image's bundled providers (see Architecture) and sets `ENABLE_PERSISTENT_CONFIG=false` so `.env` — not the SQLite-persisted admin config — is the source of truth.
- `compose.override.yaml` — publishes the UI port (`${OPEN_WEBUI_HOST_PORT:-3000}` → container `8080`). Layered in only by `make up-dev` (not `make up`), matching how the rest of the stack separates production from dev.

## Common commands

The `Makefile` (run from the repo root) is the operator interface; `make help` lists everything.

```bash
cp .env.example .env     # first-time setup — REQUIRED (--env-file .env must exist)

make network             # create the external inference-net if missing (idempotent)
make volumes             # create the external open-webui-data volume if missing (idempotent)
make pull                # pull the pinned upstream image
make up                  # start, detached, production shape (no host ports)
make up-dev              # like 'up', but publishes the UI on ${OPEN_WEBUI_HOST_PORT:-3000}
make logs                # tail logs
make ps                  # container status   (also: make health)
make down                # stop + remove container (data volume preserved)
make restart             # down + up
make nuke                # DESTROY open-webui-data — all state (interactive confirm)
make bundle              # save the pinned image as an airgap *.tar.gz
```

`up`/`up-dev` are detached and auto-create the network and the external data volume. To bypass make, replicate its flags exactly — `docker compose --env-file .env -f docker/compose.yaml [...]` from the repo root; a bare `docker compose` from `docker/` won't load the root `.env` the same way.

## Configuration

A single `.env` at the **repo root** (copy from `.env.example`) drives everything. It is consumed two ways — don't conflate them:

1. **Compose variable substitution** (`${VAR:-default}` in the YAML): `OPENAI_API_BASE_URL`, `OPENAI_API_KEY`, `TEXT_MODEL` (→ `DEFAULT_MODELS`, the chat model auto-selected for new chats), `RAG_EMBEDDING_MODEL`, `INFERENCE_NET`, `OPEN_WEBUI_HOST_PORT`. The Makefile passes these via `--env-file .env`. `TEXT_MODEL` and `RAG_EMBEDDING_MODEL` follow the stack-wide model-naming convention and must name models the endpoint actually serves.
2. **`env_file: ../.env`** (optional, `required: false`): injects the same root `.env` as environment variables *inside the container*.

`--env-file .env` hard-fails if the file is missing, so `cp .env.example .env` is mandatory before any make/compose target. Per the sibling services, `.env` and `*.tar.gz` bundles are git-ignored (see `.gitignore`).

**Provider switch.** The stack-wide model ids and endpoints are exported in your shell (`~/.bashrc` / `~/.zshrc`) — `TEXT_MODEL`, `EMBED_MODEL`, `WHISPER_MODEL`, `OPENAI_API_BASE_URL`, and the chat/STT endpoints — shared with every sibling consumer. Compose interpolation takes those shell exports over `.env`, so the whole stack stays pinned to one provider at a time (Ollama serves the embed model as `bge-m3:latest`, vllm-service as `BAAI/bge-m3`, etc.). To switch backends, change the exports (and any provider-specific entries in `.env`), then `make up-dev` / `make up`.

**Updating the image:** bump the tag and the `@sha256:` digest on the `image:` line in `compose.yaml`, then `make pull && make up` (or `up-dev`).

## Gotchas (verified)

- **`.env` is authoritative, the UI is not.** `compose.yaml` sets `ENABLE_PERSISTENT_CONFIG=false`, so connection/model settings load from env every boot and admin-UI "Connections" edits **do not persist** across a restart. Change the endpoint or models by editing `.env`, then restart — not in the UI. (Open WebUI's default is the opposite: UI edits get written to the SQLite config table and *shadow* env; this deployment turns that off on purpose, so the upstream image's `OPENAI_API_BASE_URL=http://litellm:4000/v1` default is overridden cleanly to `vllm-router` here.)
- **Shell env beats `.env` for `${...}` interpolation.** The stack exports model-selection vars (`TEXT_MODEL`, `EMBED_MODEL`, …) in your shell (`~/.zshrc`), and docker-compose prefers the shell over `--env-file .env` when interpolating. So `DEFAULT_MODELS` follows the *exported* `TEXT_MODEL` (e.g. `gemma4:31b-cloud`), and any `TEXT_MODEL` set in this repo's `.env` is ignored while the shell var is set. Change the default by changing the stack-wide export (shared with the other consumers), not just `.env`.
- **`make restart` drops the host port.** `restart` = `down` + `up`, and `up` is the production shape with **no published ports**. For local access use `make down && make up-dev` instead.
- **Embedding model must exist on the endpoint.** `RAG_EMBEDDING_MODEL` follows the stack `EMBED_MODEL` (`compose.yaml`: `${EMBED_MODEL:-bge-m3:latest}`) and is requested over the endpoint's `/v1/embeddings`. The id is endpoint-specific — Ollama serves it as `bge-m3:latest`, vllm-service as `BAAI/bge-m3` — so `EMBED_MODEL` must match the active `OPENAI_API_BASE_URL`. Changing it changes vector dimensions, invalidating existing knowledge-base vectors (re-index needed).
- **STT endpoint is separate from chat.** `AUDIO_STT_OPENAI_API_BASE_URL` does *not* inherit `OPENAI_API_BASE_URL` — Open WebUI's built-in default is the literal `api.openai.com` (config.py reassigns the singular var after seeding the real `OPENAI_API_BASE_URLS`). `compose.yaml` wires it to `${WHISPER_API_BASE:-${OPENAI_API_BASE_URL:-…}}`, so STT follows the chat endpoint by default (production `vllm-router` fronts the `asr` backend) and you redirect it with `WHISPER_API_BASE` (e.g. the CPU `asr-only` stack `http://asr-only:8000/v1` when chat is on Ollama). `AUDIO_STT_MODEL` shares the stack's `WHISPER_MODEL`, so it auto-matches what the asr endpoint serves.
- **Ollama cloud models need auth.** When `OPENAI_API_BASE_URL` points at an Ollama whose model list includes `*-cloud` entries, those won't run until that Ollama is signed in (`docker exec -it ollama ollama signin`); unauthenticated, Ollama strips `-cloud` and returns `404 model not found`. Note `TEXT_MODEL`/`DEFAULT_MODELS` may itself be a `*-cloud` model, so the first chat fails until signin. Local models and embeddings are unaffected.
- **Airgap bundles re-tag before `docker save` — they do _not_ drop the digest.** `compose.yaml` pins the image by `name:tag@digest`. A plain `docker save name:tag@digest` loads back *without* a usable `name:tag` binding, so on an offline host compose can't resolve the pinned reference and falls through to a registry pull — `failed to resolve reference … @sha256 …` / `dial tcp: lookup ghcr.io … server misbehaving`. `make bundle` avoids this by delegating to `scripts/bundle_images.sh` → the stack-shared, CI-drift-checked `scripts/bundle-lib.sh` (`bundle_retag`: `docker tag name@digest name:tag` **before** save), so the tarball loads back with **both** bindings and the digest pin stays intact. Offline flow is just `docker load -i open-webui-pulled-*.tar.gz` then the **normal** `make up` / `make up-dev` — no compose override, exactly like the siblings and the `deploy` aggregator's `make load`. (`--no-build` does *not* suppress a registry pull, so always `docker load` first.) `scripts/bundle-lib.sh` is vendored verbatim from `nos-tromo/.github`; don't hand-edit it (CI fails on drift).
