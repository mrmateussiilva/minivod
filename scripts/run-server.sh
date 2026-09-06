#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${MINIVOD_ENV:-/etc/minivod/minivod.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Config não encontrada: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

args=(
  --db "$MINIVOD_DB"
  --cache "$MINIVOD_CACHE"
  --host "$MINIVOD_HOST"
  --port "$MINIVOD_PORT"
  --segment-time "$MINIVOD_SEGMENT_TIME"
  --playlist-wait "$MINIVOD_PLAYLIST_WAIT"
  --max-ffmpeg-jobs "${MINIVOD_MAX_FFMPEG_JOBS:-2}"
)

if [[ -n "${MINIVOD_BASE_URL:-}" ]]; then
  args+=(--base-url "$MINIVOD_BASE_URL")
fi

if [[ "${MINIVOD_COPY_ONLY:-0}" == "1" ]]; then
  args+=(--copy-only)
fi

exec /usr/bin/python3 /opt/minivod/app/hls_server.py "${args[@]}"
