# open-webui-service

A thin **deployment wrapper** for [Open WebUI](https://github.com/open-webui/open-webui).
No Open WebUI source lives here — the repo is Docker Compose config that runs a
**pinned** upstream image (`ghcr.io/open-webui/open-webui`, fixed by tag **and**
`sha256` digest) as the chat UI for the `nos-tromo` inference stack, plus a
small stdlib sidecar (`retention/`) that deletes what Open WebUI keeps forever.

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
- The external `inference-net` **and `edge-net`** networks (both created by
  `make network`, idempotent). `edge-net` is declared `external: true`, so a
  missing one makes `make up` fail.
- The external `open-webui-data` volume (created by `make volumes`, idempotent) —
  all app state lives here, kept out of the compose project so teardown can't
  delete it.
- A reachable OpenAI-compatible endpoint on that network. Normally **bring up
  `vllm-service` first** so `vllm-router:4000` resolves; any other
  OpenAI-compatible server (e.g. an Ollama `/v1`) works too.

## Quick start

```bash
cp .env.example .env        # REQUIRED — compose runs with --env-file .env
# edit .env: set OPENAI_API_KEY and EDGE_HOST (the model ids are optional)

make network                # create inference-net + edge-net (idempotent; up/up-dev also do this)
make volumes                # create open-webui-data   (idempotent; up/up-dev also do this)
make pull                   # pull the pinned images
make up-dev                 # start, publishing the UI on the host
```

Then open `http://localhost:${OPEN_WEBUI_HOST_PORT}` (default `3000`). The first account you register becomes
the admin.

For production, use `make up` instead — same service with **no published host
ports**, reachable only on `inference-net` (e.g. behind a reverse proxy there).

## Access — production SSO via the edge gateway

In production users never reach Open WebUI directly. It joins the external
`edge-net` network as alias `open-webui`, and the `edge-plane` gateway is the
only path in — TLS plus Authelia forward-auth, identity injected as
`X-Auth-Email` / `X-Auth-User`, which `compose.yaml` maps onto Open WebUI's
trusted-header variables. Authelia is the only login screen.

The bundled auth is bypassed via that header, not disabled. Because the
container also sits on `inference-net`, a small nginx sidecar (`edge-guard`)
is the only listener in front of it. With the gateway's secret set
(`OPEN_WEBUI_EDGE_SECRET`, the same value as in edge-plane's `.env`), it
refuses every request that does not carry it, so Open WebUI is reachable only
through the gateway and every request has passed Authelia. It forwards the
identity headers only on those requests, and it refuses file uploads, knowledge
bases and retrieval processing for every account, so documents are not stored
here (memories and notes are off too). It also refuses message ratings, each of which
would keep a full copy of its chat that admins could read, and creating or
changing tools and functions, which Open WebUI would run as Python inside its
backend; its code interpreter is off too. Having no
base-path support upstream,
Open WebUI gets its own gateway port rather than a sub-path:
`https://<EDGE_HOST>:8443/`.

See [`CLAUDE.md`](./CLAUDE.md) § Access — edge gateway SSO for the
first-account admin rule and the full header contract.

## Operations

Run everything via `make` from the repo root (`make help` lists all targets). It
wraps `docker compose --env-file .env -f docker/compose.yaml …` so the root
`.env` is always loaded — a bare `docker compose` from `docker/` would not.

| Target        | Does |
|---------------|------|
| `make network`| Create the external `inference-net` and `edge-net` if missing (idempotent). |
| `make volumes` | Create the external `open-webui-data` volume if missing (idempotent). |
| `make pull`   | Pull the pinned images. |
| `make up`     | Start detached, production shape — **no host ports**. |
| `make up-dev` | Like `up`, but publishes the UI on `${OPEN_WEBUI_HOST_PORT:-3000}`. |
| `make down`   | Stop + remove the container (the `open-webui-data` volume is kept). |
| `make restart`| `down` + `up` (⚠ see gotcha — drops the host port). |
| `make logs`   | Tail the container logs. |
| `make ps` / `make health` | Container status; `health` also asks the retention sidecar whether it is sweeping as configured. |
| `make retention-dry-run` | What retention would delete now, as counts and ages; changes and records nothing. `CHAT_DAYS=n` / `AUDIO_MINUTES=n` preview a period. |
| `make bundle` | Save the pinned images of the latest release tag as an airgap `*.tar.gz` (leaves HEAD on that tag). |
| `make bundle-dev` | Same, from the current working tree. |
| `make nuke`   | **Destroy** the `open-webui-data` volume — all state (interactive confirm). |

## Configuration

A single `.env` at the repo root (copied from `.env.example`) drives everything.
`--env-file .env` hard-fails if it is missing, so `cp .env.example .env` is
mandatory before any target.

| Variable               | Purpose | Default |
|------------------------|---------|---------|
| `OPENAI_API_BASE_URL`  | OpenAI-compatible endpoint for chat + RAG. | `http://vllm-router:4000/v1` |
| `OPENAI_API_KEY`       | API key for that endpoint (the router's master key; set it in `.env`). | `sk-1234567890` (placeholder) |
| `TEXT_MODEL`           | Chat model auto-selected for new chats (→ `DEFAULT_MODELS`). | empty (UI auto-selects) |
| `EMBED_MODEL`          | Embedding model for RAG (→ `RAG_EMBEDDING_MODEL`). | `bge-m3:latest` |
| `WHISPER_API_BASE`     | Separate STT endpoint. Unset ⇒ follows `OPENAI_API_BASE_URL`. | — |
| `INFERENCE_NET`        | Name of the shared inference network. | `inference-net` |
| `EDGE_NET`             | Name of the shared gateway network the SPA is reached over. | `edge-net` |
| `EDGE_HOST`            | Gateway hostname; builds the post-sign-out redirect (`WEBUI_AUTH_SIGNOUT_REDIRECT_URL`). | `localhost` |
| `OPEN_WEBUI_HOST_PORT` | Host port for `make up-dev`. | `3000` |
| `OPEN_WEBUI_FORWARD_JWT_SECRET` | Signs the per-call user token the router records; same value as vllm-service's `AUDIT_OPENWEBUI_JWT_SECRET`. Empty ⇒ no user information is forwarded. | empty |
| `OPEN_WEBUI_AUDIO_RETENTION_MINUTES` | Minutes after which speech-to-text recordings, their transcripts and text-to-speech output are deleted; `off` keeps them. | `60` |
| `OPEN_WEBUI_CHAT_RETENTION_DAYS` | Days after a chat was started (a note or upload created) at which it is deleted, with everything that belongs only to it. `off` keeps them (development only). | `365` |
| `RETENTION_NOTICE_DAYS` | Days a new period deletes nothing after it took effect; a shortened one spares what it makes due sooner for as long. Changing it alone moves no date. | `30` |
| `OPEN_WEBUI_RETENTION_INTERVAL_S` | Seconds between two sweeps of the audio caches (chats: at start, then hourly). | `300` |

Model ids must name models the endpoint actually serves, and the same model
carries different ids per provider — `bge-m3:latest` on Ollama,
`BAAI/bge-m3` on `vllm-service` — so set them to match the active
`OPENAI_API_BASE_URL`.

Two behaviours worth knowing before you debug a setting that will not stick:
shell exports shared across the sibling consumers **win over `.env`** (that is
what keeps the whole stack on one provider at a time), and **`.env` is
authoritative over the UI** — `ENABLE_PERSISTENT_CONFIG=false` means admin-UI
"Connections" edits do not survive a restart. Both are spelled out in
[`CLAUDE.md`](./CLAUDE.md) § Configuration and § Gotchas.

## Persistence

All application state — the SQLite DB (users, chats, settings), files and
RAG vectors from before intake was switched off, and speech-to-text recordings
(deleted after an hour by default, see [Retention](#retention)) — lives in the
external Docker volume `open-webui-data`,
mounted at `/app/backend/data`. Being `external`, it is owned by the host
rather than the compose project, so no teardown can delete it: not `make
down`, not `make restart`, not a raw `docker compose down -v`. Only `make
nuke` removes it, behind an interactive confirm, and that wipes every account,
chat and document.

## Retention

Open WebUI never deletes the recordings it transcribes, their transcripts or
its text-to-speech output, and it has no retention setting for chats. The
`retention` service deletes them, and records every deletion in the
federation's data-protection audit log:

- speech-to-text recordings with their transcripts, and text-to-speech output,
  60 minutes after they were written (`OPEN_WEBUI_AUDIO_RETENTION_MINUTES`;
  `off` keeps them);
- chats, with everything that belongs only to them,
  `OPEN_WEBUI_CHAT_RETENTION_DAYS` days after they were started (default 365),
  however recently they changed; nothing extends it. A banner tells every user;
- notes (switched off; those written before) and uploads from before the
  intake was closed, by the same period after they were created.

A period that is new deletes nothing for `RETENTION_NOTICE_DAYS` (default 30)
after it took effect. A changed period never deletes sooner than announced: a
longer one applies at once (within a running first notice, at its end; also to
what was due under the shorter one but not yet deleted), a shorter one gives the
notice only to what it makes due sooner and never ends a notice already running.
Changing `RETENTION_NOTICE_DAYS` alone moves nothing. Switching retention off
forgets when the periods took effect, so switching it on again gives a fresh
notice. See what it would delete: `make retention-dry-run` (or `CHAT_DAYS=180`
for another period) prints counts, ages and the date deletion starts, never
names or content, and deletes and records nothing. What belongs to a chat, how a
deletion is recorded, and why a new Open WebUI image stops chat retention until
it is verified: [`CLAUDE.md`](./CLAUDE.md) § Retention.

## Updating the image

The image is pinned by tag **and** digest in `docker/compose.yaml`, written out
on both services that run it (`volume-permissions` and `open-webui`). Dependabot
proposes new releases as a PR that bumps tag and digest on both lines. To update
by hand, bump both lines together, then:

```bash
make pull && make up        # or up-dev
```

An image whose database schema differs from the verified one fails CI's
`retention-schema` job until chat retention has been checked against it
([`CLAUDE.md`](./CLAUDE.md) § Retention).

## Airgap delivery

`make bundle` checks out the latest annotated release tag, runs `pull`, and
writes the pinned images to a versioned `open-webui-pulled-vX.Y.Z.tar.gz`,
matching the stack's pulled-image convention. On success HEAD stays on that
tag, so the repo files match the bundle; `git switch main` returns. It refuses
on uncommitted changes to tracked files or when no tag is reachable;
`make bundle-dev` bundles the working tree instead (`<date>-<sha>` version). Copy it to the offline host and `docker load < <file>`, then bring
the service up there. Nothing in this deployment fetches models or telemetry at
runtime.

## Audit log

Every authenticated API request writes one audit record (account, verb, URI,
source IP, model id; never a request or response body) to the container log.
Admins cannot read or export other users' chats. With
`OPEN_WEBUI_FORWARD_JWT_SECRET` set, the router's record of each model call
names the user too, from a signed token. Every deletion the retention
sidecar makes is an event of the federation's data-protection audit log in
that container's log. Details: [`CLAUDE.md`](./CLAUDE.md) § Architecture and
§ Retention.

## Layout

```
.
├── docker/
│   ├── compose.yaml            # base services (production shape, no host ports)
│   └── compose.override.yaml   # dev overlay: publishes the UI port
├── nginx/
│   └── edge-guard.conf.template  # sidecar: identity headers only with the gateway's secret
├── retention/                  # sidecar: deletes and records what Open WebUI keeps forever
│   ├── owui_retention.py       #   entry point: serve, dry-run, schema, health, verify
│   ├── audio_caches.py         #   speech-to-text and text-to-speech files
│   ├── chat_retention.py       #   chats in webui.db, schema guard
│   ├── audit_event.py, audit_runtime.py  # vendored from nos-tromo/.github, drift-checked
│   └── tests/                  #   stdlib unittest; fixtures/webui-schema.sql is the pinned schema
├── Makefile                    # operator interface (see `make help`)
├── .env.example                # copy to .env
├── CLAUDE.md                   # architecture, internals, gotchas
└── README.md
```

`.env`, `*.code-workspace`, `.claude/`, and `.remember/` are git-ignored.

## Documentation

- [`CLAUDE.md`](./CLAUDE.md) — architecture rationale, the compose layout, the
  verified gotcha list, and how the image's bundled providers are hard-disabled.
- Sibling repos: inference from
  [vllm-service](https://github.com/nos-tromo/vllm-service), the gateway from
  [edge-plane](https://github.com/nos-tromo/edge-plane), ordered bring-up from
  [deploy](https://github.com/nos-tromo/deploy).
- Questions and bugs: <https://github.com/nos-tromo/open-webui-service/issues>

## License

[Apache License 2.0](./LICENSE). Open WebUI itself is a separate upstream project
under its own license.
