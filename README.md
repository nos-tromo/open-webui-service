# open-webui-service

A thin **deployment wrapper** for [Open WebUI](https://github.com/open-webui/open-webui).
No application source lives here — the repo is Docker Compose config that runs a
**pinned** upstream image (`ghcr.io/open-webui/open-webui`, fixed by tag **and**
`sha256` digest) as the chat UI for the `nos-tromo` inference stack.

Open WebUI speaks the OpenAI API. Here it is pointed at an OpenAI-compatible
endpoint on the shared `inference-net` Docker network — in production the
**LiteLLM proxy from `vllm-service`** (alias `vllm-router`, port `4000`), which
fans out to vLLM backends (chat, embeddings, STT, …):

```
open-webui ──(OpenAI API over inference-net)──► vllm-service LiteLLM proxy ──► vLLM backends
```

This deployment ships **no bundled inference provider** of its own. The image's
built-in backends are hard-disabled: Ollama off (`ENABLE_OLLAMA_API=false`), RAG
embeddings and speech-to-text routed to the OpenAI endpoint instead of in-process
models, and `OFFLINE_MODE=true` + `HF_HUB_OFFLINE=1` block any model download
from HuggingFace at runtime. The only inference traffic that leaves the container
goes out via `OPENAI_API_BASE_URL`.

> For architecture rationale, the full gotcha list, and internals, see
> [`CLAUDE.md`](./CLAUDE.md).

## Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose`, not `docker-compose`).
- The external `inference-net` network (created by `make network`, idempotent).
- The external `open-webui-data` volume (created by `make volume`, idempotent) —
  all app state lives here, kept out of the compose project so teardown can't
  delete it.
- A reachable OpenAI-compatible endpoint on that network. Normally **bring up
  `vllm-service` first** so `vllm-router:4000` resolves; any other
  OpenAI-compatible server (e.g. an Ollama `/v1`) works too.

## Quick start

```bash
cp .env.example .env        # REQUIRED — compose runs with --env-file .env
# edit .env: set OPENAI_API_BASE_URL / OPENAI_API_KEY and the model ids

make network                # create inference-net    (idempotent; up/up-dev also do this)
make volume                 # create open-webui-data   (idempotent; up/up-dev also do this)
make pull                   # pull the pinned upstream image
make up-dev                 # start, publishing the UI on the host
```

Then open `http://localhost:${OPEN_WEBUI_HOST_PORT}` (the example `.env` sets
`8080`; the built-in default is `3000`). The first account you register becomes
the admin.

For production, use `make up` instead — same service with **no published host
ports**, reachable only on `inference-net` (e.g. behind a reverse proxy there).

## Operations

Run everything via `make` from the repo root (`make help` lists all targets). It
wraps `docker compose --env-file .env -f docker/compose.yaml …` so the root
`.env` is always loaded — a bare `docker compose` from `docker/` would not.

| Target        | Does |
|---------------|------|
| `make network`| Create the external `inference-net` if missing (idempotent). |
| `make volume` | Create the external `open-webui-data` volume if missing (idempotent). |
| `make pull`   | Pull the pinned upstream image. |
| `make up`     | Start detached, production shape — **no host ports**. |
| `make up-dev` | Like `up`, but publishes the UI on `${OPEN_WEBUI_HOST_PORT:-3000}`. |
| `make down`   | Stop + remove the container (the `open-webui-data` volume is kept). |
| `make restart`| `down` + `up` (⚠ see gotcha — drops the host port). |
| `make logs`   | Tail the container logs. |
| `make ps` / `make health` | Container status. |
| `make bundle` | Save the pinned image as an airgap `*.tar.gz`. |
| `make nuke`   | **Destroy** the `open-webui-data` volume — all state (interactive confirm). |

## Configuration

A single `.env` at the repo root (copied from `.env.example`) drives everything.
`--env-file .env` hard-fails if it is missing, so `cp .env.example .env` is
mandatory before any target.

| Variable               | Purpose | Default |
|------------------------|---------|---------|
| `OPENAI_API_BASE_URL`  | OpenAI-compatible endpoint for chat + RAG. | `http://vllm-router:4000/v1` |
| `OPENAI_API_KEY`       | API key for that endpoint. | `sk-1234567890` |
| `TEXT_MODEL`           | Chat model auto-selected for new chats (→ `DEFAULT_MODELS`). | empty (UI auto-selects) |
| `EMBED_MODEL`          | Embedding model for RAG (→ `RAG_EMBEDDING_MODEL`). | `bge-m3:latest` |
| `WHISPER_API_BASE`     | Separate STT endpoint. Unset ⇒ follows `OPENAI_API_BASE_URL`. | — |
| `INFERENCE_NET`        | Name of the shared external network. | `inference-net` |
| `OPEN_WEBUI_HOST_PORT` | Host port for `make up-dev`. | `3000` |

Model ids follow the **stack-wide naming convention** and must name models the
endpoint actually serves. The same id can differ per provider — e.g. the embed
model is `bge-m3:latest` on Ollama but `BAAI/bge-m3` on `vllm-service` — so set
them to match the active `OPENAI_API_BASE_URL`.

**Provider switch.** The model ids and endpoints are exported stack-wide in your
shell (e.g. `~/.bashrc` / `~/.zshrc`), shared with the sibling consumers
(`docint`, `chorus`, `Nextext`, `translator`). Compose interpolation **prefers
those shell exports over `.env`**, which keeps the whole stack pointed at one
provider at a time. To switch backends (vLLM ↔ Ollama), change the exported vars
(and any provider-specific entries in `.env`), then `make up` / `make up-dev`.

**`.env` is authoritative, the UI is not.** `compose.yaml` sets
`ENABLE_PERSISTENT_CONFIG=false`, so connection/model settings load from env on
every boot and admin-UI "Connections" edits **do not persist** across a restart.
Change the endpoint or models by editing `.env` (or the shell exports) and
restarting — not in the UI.

## Persistence

All application state — the SQLite DB (users, chats, settings), uploaded files,
and RAG vectors — lives in the Docker volume **`open-webui-data`**, mounted at
`/app/backend/data`. It is declared `external` in `compose.yaml` and created
out-of-band by `make volume` (like the network), so it is owned by the host, not
the compose project: **app teardown can never delete it** — not `make down`, not
`make restart`, not even a raw `docker compose down -v`. Only `make nuke` removes
it (container `down`, then `docker volume rm`, behind an interactive confirm);
doing so wipes every account, chat, and document.

## Updating the image

The image is pinned by tag **and** digest in `docker/compose.yaml`. To update,
bump both the tag and the `@sha256:` digest on the `image:` line, then:

```bash
make pull && make up        # or up-dev
```

## Airgap delivery

`make bundle` runs `pull` and writes the pinned image to a versioned
`open-webui-pulled-<timestamp>.tar.gz`, matching the stack's pulled-image
convention. Copy it to the offline host and `docker load < <file>`, then bring
the service up there. Nothing in this deployment fetches models or telemetry at
runtime.

## Layout

```
.
├── docker/
│   ├── compose.yaml            # base service (production shape, no host ports)
│   └── compose.override.yaml   # dev overlay: publishes the UI port
├── Makefile                    # operator interface (see `make help`)
├── .env.example                # copy to .env
├── CLAUDE.md                   # architecture, internals, gotchas
└── README.md
```

`.env`, `*.code-workspace`, `.claude/`, and `.remember/` are git-ignored.

## License

[Apache License 2.0](./LICENSE). Open WebUI itself is a separate upstream project
under its own license.
