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

exec /usr/bin/python3 /opt/minivod/app/scan_vod.py \
  "$MINIVOD_MEDIA" \
  --db "$MINIVOD_DB"
