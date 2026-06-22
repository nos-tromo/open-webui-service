#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091,SC2154  # sources vendored scripts/bundle-lib.sh (sets BUNDLE_*)
#
# Save the pinned upstream image as a versioned airgap tarball. open-webui-service
# builds nothing and pulls a single registry image, so this mirrors data-plane's
# pull-only bundler: pull, then `bundle_collect_pulled` re-tags every
# name:tag@digest ref to name:tag (via bundle_retag) BEFORE `docker save`, so the
# digest-pinned `image:` in docker/compose.yaml resolves after `docker load` on
# the offline host. Without the re-tag, `docker save name:tag@digest` loads back
# without the name:tag binding and compose falls through to a registry pull.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
. scripts/bundle-lib.sh

bundle_version open-webui; VER="$BUNDLE_VERSION"

COMPOSE=(docker compose --env-file .env -f docker/compose.yaml)
"${COMPOSE[@]}" pull
bundle_collect_pulled < <("${COMPOSE[@]}" config --images)

if (( ${#BUNDLE_PULLED[@]} == 0 )); then
  echo "No images resolved from docker/compose.yaml." >&2
  exit 1
fi
echo "Saving images: ${BUNDLE_PULLED[*]}"
docker save "${BUNDLE_PULLED[@]}" | gzip > "open-webui-pulled-${VER}.tar.gz"
echo "Wrote: open-webui-pulled-${VER}.tar.gz"
echo ">> offline host: docker load -i open-webui-pulled-${VER}.tar.gz && make up   # (or up-dev)"
