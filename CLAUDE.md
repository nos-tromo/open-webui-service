# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## Planning

For any non-trivial change (>1 file or any root-cause fix), present a plan and wait for approval BEFORE editing code. Do not start with Edit/Write on a fresh investigation.

## What this repo is

A thin **deployment wrapper** for [Open WebUI](https://github.com/open-webui/open-webui) — no Open WebUI source lives here. The repo is Docker Compose config (`docker/`) plus a small vendored airgap bundler (`scripts/`), running a pinned upstream image (`ghcr.io/open-webui/open-webui`, pinned by tag **and** sha256 digest), and the stdlib `retention/` sidecar that deletes what that image keeps forever (§ Retention). Apart from `retention/`, changes here are config/ops changes, not code changes.

## Architecture: where this fits

This is one of several sibling deployments in the `infra/` workspace (`vllm-service`, `translator`, `docint`, `chorus`, `Nextext`, `afd-pipeline-main`, `data-plane`). They communicate over a single shared Docker bridge network, **`inference-net`**, which is declared `external: true` everywhere and created out-of-band (`docker network create inference-net`; siblings do this idempotently via their `make network` target).

Open WebUI is the chat UI. It speaks the OpenAI API and is pointed at an OpenAI-compatible endpoint on `inference-net`. In this stack that endpoint is the **LiteLLM proxy provided by `vllm-service`** (service `router`, port `4000`, network alias `vllm-router`), which fans out to vLLM model backends (chat, embed, rerank, clip, asr, diarize, vad, gliner). So the dependency chain is:

```
open-webui  ──(OpenAI API over inference-net)──►  vllm-service litellm proxy  ──►  vLLM backends
```

To run Open WebUI usefully, `inference-net` must exist and an upstream OpenAI-compatible API must be reachable on it (normally bring up `vllm-service` first). Open WebUI joins `inference-net` under the alias `open-webui-client`.

**No bundled inference provider.** Like the sibling consumers, this app reaches inference *only* over the OpenAI-compatible API; the upstream image's own backends are hard-disabled in `compose.yaml`. The built-in Ollama integration is off (`ENABLE_OLLAMA_API=false`); RAG embeddings go to the same endpoint (`RAG_EMBEDDING_ENGINE=openai`, model `RAG_EMBEDDING_MODEL`) instead of the image's in-process sentence-transformers; STT goes to the endpoint (`AUDIO_STT_ENGINE=openai`) instead of local Whisper; and `OFFLINE_MODE=true` + `HF_HUB_OFFLINE=1` block any runtime model download from HuggingFace. Net: no local models load, and the only inference traffic leaves via `OPENAI_API_BASE_URL`.

**Audit and admin access.** `compose.yaml` turns on Open WebUI's built-in audit log at `AUDIT_LOG_LEVEL=METADATA`: one record per authenticated API request (account, verb, URI, source IP, model id), never a request or response body, so no chat content. The upstream defaults skip the `/chats`, `/chat` and `/folders` routes and every GET, which are exactly the edits and reads of stored chats, so `AUDIT_EXCLUDED_PATHS` is set empty and `ENABLE_AUDIT_GET_REQUESTS=true`. Records go to stdout (`ENABLE_AUDIT_STDOUT=true`) and are collected with the container logs; the file sink is off (`ENABLE_AUDIT_LOGS_FILE=false`) because upstream rotates that file by size but never deletes it, so it would grow on the data volume forever. Admin reach into other users' data is closed: `ENABLE_ADMIN_CHAT_ACCESS=false`, `ENABLE_ADMIN_EXPORT=false`, `BYPASS_ADMIN_ACCESS_CONTROL=false` (workspace content stays private to its owner unless shared), `ENABLE_COMMUNITY_SHARING=false`. The telemetry opt-outs the image already sets (`SCARF_NO_ANALYTICS`, `DO_NOT_TRACK`, `ANONYMIZED_TELEMETRY`) are pinned in compose so an upstream default change cannot undo them. Verified against the pinned image's `backend/open_webui/env.py`, `config.py` and `utils/audit.py`; re-check these names on an image bump.

**Signed user attribution at the router.** The router (`vllm-service`) records one `ai_call` event per model call, but every caller shares its master key, so on its own it cannot name the person behind an Open WebUI call. With `OPEN_WEBUI_FORWARD_JWT_SECRET` set, `compose.yaml` turns on `ENABLE_FORWARD_USER_INFO_HEADERS` and passes the value as `FORWARD_USER_INFO_HEADER_JWT_SECRET`. Every call to the endpoint then carries one HS256 token in `X-OpenWebUI-User-Jwt` (`sub`, `email`, `name`, `role`, `iss: open-webui`, `exp`); the router, given the same value as `AUDIT_OPENWEBUI_JWT_SECRET`, verifies it and records the e-mail as the call's actor (vllm-service `docs/audit.md`). The token lives 60 s (`FORWARD_USER_INFO_HEADER_JWT_EXPIRES_SECONDS`, upstream default 300): it is minted per call and checked as the call arrives, so a short lifetime only narrows the replay window. The secret itself switches forwarding on (`${OPEN_WEBUI_FORWARD_JWT_SECRET:+true}`), never without it: forwarding with no secret sends the user's name, id, e-mail and role as plain `X-OpenWebUI-User-*` headers, which any caller on `inference-net` could send too. Unset, nothing is forwarded and the router names no Open WebUI user. The secret is used for nothing else — not `WEBUI_SECRET_KEY` (the session key) and not the endpoint's API key; whoever holds it could mis-attribute router events, not mint Open WebUI sessions. Rotation: change it in both `.env` files and restart both stacks. Verified against the pinned image's `env.py` and `utils/headers.py`; re-check on an image bump.

**No content intake.** Open WebUI is a chat UI here, not a document store: documents and personal facts belong in the apps that audit what they store. Chat file and web uploads, knowledge bases, memories and notes are therefore off (notes: § Retention). `compose.yaml` sets `USER_PERMISSIONS_CHAT_FILE_UPLOAD=false`, `USER_PERMISSIONS_CHAT_WEB_UPLOAD=false`, `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS=false` (already the upstream default, pinned), `USER_PERMISSIONS_FEATURES_MEMORIES=false` and `ENABLE_MEMORIES=false`; with `ENABLE_PERSISTENT_CONFIG=false` these load from env on every boot. The flags only hide the controls, with one exception: `ENABLE_MEMORIES=false` the server enforces itself. In the pinned image the files router never checks the upload permission (only the UI does), and admins pass the knowledge permission. So `edge-guard` does the enforcing: it answers `403` (JSON `detail`) to `POST`/`PUT`/`PATCH` under `/api/v1/files`, `/api/v1/knowledge` and `/api/v1/retrieval/process`, for every account. It matches on nginx's decoded, normalized path, ignoring case. Reading and deleting existing files and knowledge bases still works, as do `…/file/remove` and `…/reset`, so anything stored before can be cleaned up. Attaching a file or pasting an image into a chat fails with that `403`. Verified against the pinned image's `routers/files.py`, `knowledge.py`, `retrieval.py`, `memories.py` and `models/config.py`; re-check the route list on an image bump.

**No message ratings.** Rating an answer stores a full copy of the chat with the rating (the `feedback` row's `snapshot`). The copy outlives the chat in Open WebUI's own delete, and an admin can read it through `GET /api/v1/evaluations/feedback/{id}` (any rating) and `GET /api/v1/evaluations/feedbacks/all/export`, whatever `ENABLE_ADMIN_CHAT_ACCESS` says. `compose.yaml` sets `ENABLE_MESSAGE_RATING=false` and `USER_PERMISSIONS_CHAT_RATE_RESPONSE=false`, which only hide the controls: the pinned image's `routers/evaluations.py` checks neither. So `edge-guard` refuses, with `403` (JSON `detail`) and for every account, creating or updating a rating (`POST`/`PUT`/`PATCH` under `/api/v1/evaluations/feedback`), reading a single rating (`GET`/`HEAD` `/api/v1/evaluations/feedback/<id>`) and the export, matched like the intake routes. The lists (`/feedbacks/user`, `/feedbacks/list`, `/feedbacks/all/ids`) and the leaderboard carry no snapshot and still pass, as do the deletes, so ratings stored before can be cleared; the chat sweeper also removes a chat's ratings with it. Verified against the pinned image's `routers/evaluations.py` and `models/feedbacks.py`; re-check on an image bump.

Persistence: **external** volume `open-webui-data` → `/app/backend/data` (SQLite DB, users, chats, uploads, RAG vectors). Declared `external: true` in `compose.yaml`, it is created out-of-band by `make volumes` (like the network) and survives every app teardown — even `docker compose down -v`; only `make nuke` removes it (container `down`, then `docker volume rm`). Destroying it wipes all app state. The `retention` sidecar mounts it too, to delete what upstream keeps forever (§ Retention).

## Access — edge gateway SSO

Open WebUI is never reached directly by users; it sits behind the
federation's edge gateway (`edge-plane`) on the external `edge-net`
network, joined under the alias `open-webui`. The gateway terminates TLS,
enforces Authelia forward-auth, and — after successful login — injects
`X-Auth-Email` / `X-Auth-User` on the proxied request. `compose.yaml` wires
`WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Auth-Email` and
`WEBUI_AUTH_TRUSTED_NAME_HEADER=X-Auth-User`, so Open WebUI trusts that
header instead of its own bundled login: an authenticated gateway session
auto-creates/logs in the matching Open WebUI account and the app's native
login screen is never shown. **Authelia is the only login** — the
bundled Open WebUI auth is bypassed entirely via the trusted header, not
disabled.

**edge-guard keeps that header unforgeable.** The container is also on
`inference-net`, where any other container could reach it, and Open WebUI
cannot check where a header came from. So Open WebUI listens on
`127.0.0.1:8081` only (`HOST`/`PORT` in `compose.yaml`), and the
`edge-guard` service, an `nginx-unprivileged` sidecar with
`network_mode: "service:open-webui"`, is the only listener on `:8080` in
that shared network namespace. Both networks and the dev host port therefore
reach the guard, never Open WebUI directly. The guard forwards the identity
headers only when `X-Edge-Secret` equals `OPEN_WEBUI_EDGE_SECRET`, the value
the edge gateway sends (edge-plane's `docs/identity-contract.md`).
Otherwise it drops them, and the request reaches Open WebUI with no identity.
It always strips `X-Edge-Secret`, proxies WebSockets (socket.io) and
streams unbuffered. With the secret unset it forwards the headers as before
(legacy). Config: `nginx/edge-guard.conf.template`, rendered by the image's
envsubst, which is why compose always defines the variable, even when empty.
Gotchas: recreating `open-webui` must recreate `edge-guard` too, since it
lives in that container's namespace (`make up` does both). The image's
healthcheck follows `PORT`, so it probes `8081` inside the container.

Open WebUI has no base-path / sub-path support (confirmed empirically —
its built-in frontend serves root-absolute asset paths, e.g. `/_app/...`,
`/static/...`, with no `WEBUI_BASE_URL`-style rewrite hook in
`open_webui/env.py`), so unlike the path-prefixed apps (`/chorus`,
`/docint`, …) it cannot live under `/webui` on the shared `:443` site. The
gateway instead gives it a **dedicated port**, `:8443` (see `edge-plane`'s
`caddy/Caddyfile`); the shared site's `/webui` route just redirects there.
Reachable at `https://<EDGE_HOST>:8443/` once both stacks are up.

## Retention

Open WebUI deletes neither the files its audio endpoints leave on
`open-webui-data` nor, having no retention setting, any chat, note or upload.
The `retention` service does, and records every deletion as a `delete` event of the
federation's data-protection audit log: hash-chained lines
(`service: open-webui-service`) in that container's log, which obs-plane
collects. Code: `retention/`, stdlib Python mounted read-only into the pinned
`python` image; nothing is installed. It runs as uid 10001, Open WebUI's and
the owner of everything on the volume, with a read-only root, no capabilities,
no network and `open-webui-data` at `/data`.

- **Audio caches, on by default.** Every regular file in
  `cache/audio/transcriptions/` (each recording sent to speech-to-text, the
  copies upstream converts, compresses and chunks it into, and the transcript
  `.json` beside them) and in `cache/audio/speech/` (text-to-speech output, with
  the request beside it, which holds the text spoken) is deleted once it is
  older than `OPEN_WEBUI_AUDIO_RETENTION_MINUTES` (compose default 60; `off`
  keeps them, as upstream does), checked every
  `OPEN_WEBUI_RETENTION_INTERVAL_S` (300). Directories and symlinks are left
  alone, and a cache directory that is a symlink or resolves off the volume is
  refused. Files have no transactions: each batch of at most 200 is recorded
  first and deleted after, and a file that could not be deleted is recorded
  again with `outcome: error`. Verified against the pinned image's
  `routers/audio/__init__.py` and `routers/openai.py` (which caches speech into
  the same directory).
- **The retention period, on by default.** `OPEN_WEBUI_CHAT_RETENTION_DAYS`
  (compose default 365; `off` only for development, logged as a warning and
  stated in the chain's `config.audit`) is how long chats, notes and uploads
  are kept after they were *created*. Using or continuing one never extends
  it; there is no extension of any kind. A period that is new on the host, or
  changed, deletes nothing for `RETENTION_NOTICE_DAYS` (default 30) after it
  took effect, recorded per store (`chats`, `notes`, `files`) in
  `/data/.retention-state.json` (mode 0600; a file that cannot be read or
  parsed refuses that store, `reason: state`). The `config.audit` event states
  the period, the notice and when each store's period took effect. A banner
  (`WEBUI_BANNERS`, English and German via its `i18n`, the UI's `de-DE`) tells
  every user that chats are deleted that many days after they were started;
  a notice per chat would need a change to the upstream UI.
- **Chats.** Every chat started more than the period ago (its `created_at`, or
  `updated_at` where that is missing; neither: kept) is deleted at start and
  then hourly, in Open WebUI's SQLite
  database (`webui.db`) while Open WebUI keeps running. With it go its messages
  (`chat_message`), shared snapshots (`shared_chat`) and their access grants,
  file links (`chat_file`), comments and their reactions, the notifications
  about it, the ratings of its messages (`feedback`, each holding a full copy of
  the chat in `snapshot`), legacy tag links (`chatidtag`), the internal
  (sub-agent) chats spawned from it at any depth, and the owner's tags that no
  remaining visible chat of theirs carries (internal chats do not count, as in
  upstream's own check). Automation runs keep their record and lose the
  link; uploaded files and folders belong to their owner and stay. Pinned and
  archived chats are not exempt. An internal chat whose parent still exists
  lives as long as the parent; one whose parent is gone expires on its own age.
  A note's chat (`meta.note_id`) is due once its note is gone.
- **Notes, switched off.** `ENABLE_NOTES=false` and
  `USER_PERMISSIONS_FEATURES_NOTES=false`; the pinned image's notes router
  checks `notes.enable` on every route (`routers/notes.py`
  `check_notes_access`), admins included. Notes written before are deleted
  once they were created more than the period ago (their `created_at` is in
  nanoseconds), with their pins (`pinned_note`), comments and reactions,
  notifications and access grants (`retention/note_retention.py`, from
  `models/notes.py` and `models/comments.py`).
- **Uploads from before the intake was closed.** Every `file` row created more
  than the period ago goes with its chat and knowledge links, its access grants
  and the file in `/data/uploads/` (by base name, as the image deletes it; a
  recorded path outside `/app/backend/data/uploads/` keeps its row, logged; a
  symlink is removed itself, never its target). The record names uploads by id
  only (`kind: upload`). What the image derived into its vector store
  (`file-<id>` in Chroma) is beyond the stdlib sidecar: every upload record
  says so (`vectors_left_in_store: true`), the sweep logs a warning, the dry
  run mentions it while uploads are due, and an admin clears it with *Reset
  vector storage* (Admin settings, Documents). A `uploads/` that is a symlink
  or not a directory refuses the store (`reason: not_a_directory`); a name in
  it that is not a regular file or link keeps its row, logged. Verified against `models/files.py`, `routers/files.py`
  and `storage/provider.py`.
  Upstream's own delete (`models/chats.py`, `routers/chats.py`) leaves the share
  grants, the ratings and the file links behind; the declared cascades never
  fire, as Open WebUI does not enable SQLite's foreign keys.
- **Records.** One `delete` / `retention.sweep` event per batch, actor
  `system`: `target` by reference (file names, with the cache directory as
  `container`; chat ids) and `count`; `criteria` with the rule
  (`max_age_minutes` or `period_days`, and the `cutoff`) and, for chats, the
  `owner` and how many rows of each kind went with them. The owner is Open
  WebUI's opaque account id, never an e-mail or name: while the account
  exists it resolves in Open WebUI's `user` table (an admin's
  `GET /api/v1/users/<id>`), and Open WebUI's own per-request audit records
  carry the same id with the account, for as long as its log is kept.
  More than 200 ids continue in `retention.sweep.ids` events. Never a title,
  message, tag name or file content. At start, `admin` / `config.audit` states
  the periods; `admin` / `retention.refused` (once) and `retention.resumed`
  mark a store that is not being swept.
- **No deletion without its record.** A chat batch's record is written inside
  the transaction that deletes it, before the commit; if the record fails the
  batch rolls back, and a batch whose commit fails is recorded again with
  `outcome: error`. A record that cannot be written stops the service (exit
  1): nothing more is deleted until the restarted container can record
  (`restart: unless-stopped`). That shows in `make health` and in the ops log
  (stderr), which holds counts only.
- **Locking.** In WAL mode the sweeper's reads take no lock. Deletes run in
  `BEGIN IMMEDIATE` transactions of at most 50 chats and 200 ids (a chat with
  more internal chats than that goes alone), each waiting up to 30 s for the
  write lock, with a pause between batches so Open WebUI's
  own writes (busy timeout 5 s) get through; notes and uploads are swept the
  same way. `secure_delete` overwrites the
  deleted rows in the database file instead of leaving them in free pages.
  Earlier versions of those rows can still sit in the write-ahead log
  (`webui.db-wal`), so a sweep that deleted chats ends with a `TRUNCATE`
  checkpoint: the zeroed pages replace the old ones in the database file and
  the log is emptied. When an Open WebUI reader keeps the log busy past the
  busy timeout, the sweep logs a warning and a later checkpoint carries it.
- **Schema guard.** Chat retention runs only on the schema it was verified
  against: migration revision `e42c8f710a36` (Open WebUI v0.13.0;
  `VERIFIED_REVISIONS` in `retention/chat_retention.py`), every column its
  statements use, and no table besides the ones it cleans that has a `chat_id`
  column or a foreign key to `chat`. It is checked before the sweep and again
  inside every transaction. A mismatch refuses chat retention while the audio
  caches are still swept: `retention.refused` is recorded, the ops log names
  the table or column, and the healthcheck turns unhealthy. With
  `DATABASE_URL` in `.env`, Open WebUI keeps its chats elsewhere and chat
  retention refuses too; compose passes only whether the variable is set.
- **Dry run.** `make retention-dry-run` (the service must be up; it runs in it
  with `docker compose exec`) prints what the configured periods would delete
  now: counts and age ranges, never names, ids or content. It reads what it
  counts, through a read-only connection, and records and deletes nothing.
  `CHAT_DAYS=n` / `AUDIO_MINUTES=n` preview a period before it is configured.
- **Image bumps.** CI's `retention-schema` job migrates a database with the
  pinned image and fails when its schema differs from
  `retention/tests/fixtures/webui-schema.sql`, which the tests build their
  databases from, or the guard refuses it. Then re-read upstream's delete paths
  for anything new that belongs to a chat, regenerate the fixture with
  `retention/tests/schema_dump.py` from such a database, extend the cleanup if
  needed, and add the revision to `VERIFIED_REVISIONS`. Deployed before that, a
  bump only stops chat retention, visibly; it never deletes by an outdated
  picture of the schema.
- **Audit store.** obs-plane's verifier accepts chains only from the emitters
  it lists; this one is `open-webui-service` / `retention`.
- **Tests:** `python3 -m unittest discover -s retention/tests -t retention`
  (stdlib only, synthetic data); CI runs them on the runner and in the pinned
  image, read-only as uid 10001.

## Compose layout

Operate via `make` from the repo root — the `Makefile` wraps `docker compose --env-file .env -f docker/compose.yaml` and mirrors the sibling infra services' schema. Two compose files:

- `compose.yaml` — the services (`volume-permissions`, `open-webui`, `edge-guard`, `retention`): images, environment, the external `open-webui-data` volume, the `inference-net` and `edge-net` networks, and the `local` log driver with rotation (50m × 5 files). This is the production shape — **no host ports**. Its `environment:` block hard-disables the image's bundled providers (see Architecture) and sets `ENABLE_PERSISTENT_CONFIG=false` so `.env` — not the SQLite-persisted admin config — is the source of truth.
- `compose.override.yaml` — publishes the UI port (`${OPEN_WEBUI_HOST_PORT:-3000}` → container `8080`). Layered in only by `make up-dev` (not `make up`), matching how the rest of the stack separates production from dev.

## Common commands

The `Makefile` (run from the repo root) is the operator interface; `make help` lists everything.

```bash
cp .env.example .env     # first-time setup — REQUIRED (--env-file .env must exist)

make network             # create the external inference-net if missing (idempotent)
make volumes             # create the external open-webui-data volume if missing (idempotent)
make pull                # pull the pinned images
make up                  # start, detached, production shape (no host ports)
make up-dev              # like 'up', but publishes the UI on ${OPEN_WEBUI_HOST_PORT:-3000}
make logs                # tail logs
make ps                  # container status   (also: make health, which asks the retention sidecar too)
make retention-dry-run   # what retention would delete now: counts and ages, changes nothing (CHAT_DAYS=n previews)
make down                # stop + remove container (data volume preserved)
make restart             # down + up
make nuke                # DESTROY open-webui-data — all state (interactive confirm)
make bundle              # save the latest release tag's pinned images as an airgap *.tar.gz (HEAD stays on the tag)
make bundle-dev          # same, from the current working tree
```

`up`/`up-dev` are detached and auto-create the network and the external data volume. To bypass make, replicate its flags exactly — `docker compose --env-file .env -f docker/compose.yaml [...]` from the repo root; a bare `docker compose` from `docker/` won't load the root `.env` the same way.

## Configuration

A single `.env` at the **repo root** (copy from `.env.example`) drives everything. It is consumed two ways — don't conflate them:

1. **Compose variable substitution** (`${VAR:-default}` in the YAML): `OPENAI_API_BASE_URL`, `OPENAI_API_KEY`, `TEXT_MODEL` (→ `DEFAULT_MODELS`, the chat model auto-selected for new chats), `RAG_EMBEDDING_MODEL`, `INFERENCE_NET`, `OPEN_WEBUI_HOST_PORT`, and the retention periods `OPEN_WEBUI_AUDIO_RETENTION_MINUTES`, `OPEN_WEBUI_CHAT_RETENTION_DAYS`, `OPEN_WEBUI_RETENTION_INTERVAL_S` (§ Retention). The Makefile passes these via `--env-file .env`. `TEXT_MODEL` and `RAG_EMBEDDING_MODEL` follow the stack-wide model-naming convention and must name models the endpoint actually serves.
2. **`env_file: ../.env`** (optional, `required: false`): injects the same root `.env` as environment variables *inside the container*.

`--env-file .env` hard-fails if the file is missing, so `cp .env.example .env` is mandatory before any make/compose target. Per the sibling services, `.env` and `*.tar.gz` bundles are git-ignored (see `.gitignore`).

**Provider switch.** The stack-wide model ids and endpoints are exported in your shell (`~/.bashrc` / `~/.zshrc`) — `TEXT_MODEL`, `EMBED_MODEL`, `WHISPER_MODEL`, `OPENAI_API_BASE_URL`, and the chat/STT endpoints — shared with every sibling consumer. Compose interpolation takes those shell exports over `.env`, so the whole stack stays pinned to one provider at a time (Ollama serves the embed model as `bge-m3:latest`, vllm-service as `BAAI/bge-m3`, etc.). To switch backends, change the exports (and any provider-specific entries in `.env`), then `make up-dev` / `make up`.

**Updating the image:** Dependabot (`docker-compose` ecosystem) opens a PR bumping the tag and `@sha256:` digest on both `image:` lines in `compose.yaml` (`volume-permissions` and `open-webui` run the same image; the ref is repeated literally because Dependabot cannot rewrite a value behind a YAML anchor, so keep the two identical). By hand: bump both lines, then `make pull && make up` (or `up-dev`). A new image whose database schema differs fails CI's `retention-schema` job until chat retention is re-verified against it (§ Retention).

## Gotchas (verified)

- **Container hardening (deploy ADR 0001).** The service runs as
  `user: 10001:10001` with a read-only rootfs, `no-new-privileges`,
  `cap_drop: ALL` and a `/tmp` tmpfs. `HOME=/tmp` (the image's `/root` is
  unwritable) and `WEBUI_SECRET_KEY_FILE` lives on the data volume — which
  also keeps sessions valid across container recreation. Two verified
  consequences: (1) on an **empty** `open-webui-data` volume the first mount
  copies image content in root-owned, so the one-time
  `chown -R 10001:10001` must happen **after** that first mount; (2) at every
  boot the app fails (non-fatally, logged as ERROR) to copy branding assets
  into its read-only static dir — stock assets serve fine from the image.
- **`.env` is authoritative, the UI is not.** `compose.yaml` sets `ENABLE_PERSISTENT_CONFIG=false`, so connection/model settings load from env every boot and admin-UI "Connections" edits **do not persist** across a restart. Change the endpoint or models by editing `.env`, then restart — not in the UI. (Open WebUI's default is the opposite: UI edits get written to the SQLite config table and *shadow* env; this deployment turns that off on purpose, so the upstream image's `OPENAI_API_BASE_URL=http://litellm:4000/v1` default is overridden cleanly to `vllm-router` here.)
- **Shell env beats `.env` for `${...}` interpolation.** The stack exports model-selection vars (`TEXT_MODEL`, `EMBED_MODEL`, …) in your shell (`~/.zshrc`), and docker-compose prefers the shell over `--env-file .env` when interpolating. So `DEFAULT_MODELS` follows the *exported* `TEXT_MODEL` (e.g. `gemma4:31b-cloud`), and any `TEXT_MODEL` set in this repo's `.env` is ignored while the shell var is set. Change the default by changing the stack-wide export (shared with the other consumers), not just `.env`.
- **Speech-to-text keeps recordings.** Each transcription request stores the uploaded audio and its transcript under `cache/audio/transcriptions/` on `open-webui-data`, and upstream never deletes them (pinned image, `routers/audio/__init__.py`); the text-to-speech cache under `cache/audio/speech/` is never pruned either. The `retention` sidecar deletes both, 60 minutes after a file was written by default (§ Retention).
- **`make restart` drops the host port.** `restart` = `down` + `up`, and `up` is the production shape with **no published ports**. For local access use `make down && make up-dev` instead.
- **Embedding model must exist on the endpoint.** `RAG_EMBEDDING_MODEL` follows the stack `EMBED_MODEL` (`compose.yaml`: `${EMBED_MODEL:-bge-m3:latest}`) and is requested over the endpoint's `/v1/embeddings`. The id is endpoint-specific — Ollama serves it as `bge-m3:latest`, vllm-service as `BAAI/bge-m3` — so `EMBED_MODEL` must match the active `OPENAI_API_BASE_URL`. Changing it changes vector dimensions, invalidating existing knowledge-base vectors (re-index needed).
- **STT endpoint is separate from chat.** `AUDIO_STT_OPENAI_API_BASE_URL` does *not* inherit `OPENAI_API_BASE_URL` — Open WebUI's built-in default is the literal `api.openai.com` (config.py reassigns the singular var after seeding the real `OPENAI_API_BASE_URLS`). `compose.yaml` wires it to `${WHISPER_API_BASE:-${OPENAI_API_BASE_URL:-…}}`, so STT follows the chat endpoint by default (production `vllm-router` fronts the `asr` backend) and you redirect it with `WHISPER_API_BASE` (e.g. the CPU `asr-only` stack `http://asr-only:8000/v1` when chat is on Ollama). `AUDIO_STT_MODEL` shares the stack's `WHISPER_MODEL`, so it auto-matches what the asr endpoint serves.
- **Ollama cloud models need auth.** When `OPENAI_API_BASE_URL` points at an Ollama whose model list includes `*-cloud` entries, those won't run until that Ollama is signed in (`docker exec -it ollama ollama signin`); unauthenticated, Ollama strips `-cloud` and returns `404 model not found`. Note `TEXT_MODEL`/`DEFAULT_MODELS` may itself be a `*-cloud` model, so the first chat fails until signin. Local models and embeddings are unaffected.
- **Airgap bundles re-tag before `docker save` — they do _not_ drop the digest.** `compose.yaml` pins the image by `name:tag@digest`. A plain `docker save name:tag@digest` loads back *without* a usable `name:tag` binding, so on an offline host compose can't resolve the pinned reference and falls through to a registry pull — `failed to resolve reference … @sha256 …` / `dial tcp: lookup ghcr.io … server misbehaving`. `make bundle` avoids this by delegating to `scripts/bundle_images.sh` → the stack-shared, CI-drift-checked `scripts/bundle-lib.sh` (`bundle_retag`: `docker tag name@digest name:tag` **before** save), so the tarball loads back with **both** bindings and the digest pin stays intact. Offline flow is just `docker load -i open-webui-pulled-*.tar.gz` then the **normal** `make up` / `make up-dev` — no compose override, exactly like the siblings and the `deploy` aggregator's `make load`. (`--no-build` does *not* suppress a registry pull, so always `docker load` first.) `scripts/bundle-lib.sh` is vendored verbatim from `nos-tromo/.github`; don't hand-edit it (CI fails on drift).

## Git & PR Workflow

- Never commit directly to `main`; always branch (`feat/`, `fix/`) and open a PR.
- Never create a NEW PR when an existing PR for the work is open — push additional commits to that branch.
- Release order is strict: bump VERSION file -> commit -> tag. Never tag before the VERSION bump.
- Use single, non-compound shell commands for `gh` operations (no `&&` chains); if `gh pr merge` is blocked, fall back to the GitHub MCP merge tool.

## Verification

Before claiming a check is green, run the actual command and paste the output. `git ls-files` does not cover untracked files — use `pre-commit run --all-files`. After opening a PR, confirm CI actually triggered on the latest push before declaring done.

## Communication Style

Keep changes minimal and scoped. Do not add explanatory code comments for trivial or self-evident changes. Do not overwrite existing test files with Write — use Edit to append or modify tests.
