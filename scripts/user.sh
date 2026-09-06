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

if [[ $# -lt 1 ]]; then
  cat <<USAGE
Uso:
  sudo /opt/minivod/scripts/user.sh create USER
  sudo /opt/minivod/scripts/user.sh list
  sudo /opt/minivod/scripts/user.sh disable USER
  sudo /opt/minivod/scripts/user.sh enable USER
USAGE
  exit 1
fi

case "$1" in
  create)
    [[ $# -eq 2 ]] || { echo "Informe o usuário" >&2; exit 1; }
    exec /usr/bin/python3 /opt/minivod/app/hls_server.py \
      --db "$MINIVOD_DB" \
      --create-user "$2"
    ;;
  list)
    exec /usr/bin/python3 /opt/minivod/app/hls_server.py \
      --db "$MINIVOD_DB" \
      --list-users
    ;;
  disable)
    [[ $# -eq 2 ]] || { echo "Informe o usuário" >&2; exit 1; }
    exec /usr/bin/python3 /opt/minivod/app/hls_server.py \
      --db "$MINIVOD_DB" \
      --disable-user "$2"
    ;;
  enable)
    [[ $# -eq 2 ]] || { echo "Informe o usuário" >&2; exit 1; }
    exec /usr/bin/python3 /opt/minivod/app/hls_server.py \
      --db "$MINIVOD_DB" \
      --enable-user "$2"
    ;;
  *)
    echo "Comando inválido: $1" >&2
    exit 1
    ;;
esac
