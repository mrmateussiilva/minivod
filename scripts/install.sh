#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Rode com sudo: sudo ./scripts/install.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/minivod
CONFIG_DIR=/etc/minivod

command -v python3 >/dev/null || { echo "python3 não encontrado" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg não encontrado. Instale: apt install ffmpeg" >&2; exit 1; }

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

rm -rf "$INSTALL_DIR/app" "$INSTALL_DIR/scripts"
cp -a "$REPO_DIR/app" "$INSTALL_DIR/app"
cp -a "$REPO_DIR/scripts" "$INSTALL_DIR/scripts"
chmod +x "$INSTALL_DIR/app/"*.py "$INSTALL_DIR/scripts/"*.sh

if [[ ! -f "$CONFIG_DIR/minivod.env" ]]; then
  cp "$REPO_DIR/config/minivod.env.example" "$CONFIG_DIR/minivod.env"
  echo "Config criada em $CONFIG_DIR/minivod.env"
  echo "EDITE esse arquivo antes de iniciar o serviço."
else
  echo "Mantendo config existente: $CONFIG_DIR/minivod.env"
fi

cp "$REPO_DIR/systemd/minivod.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/minivod-scan.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/minivod-scan.timer" /etc/systemd/system/

systemctl daemon-reload

echo
echo "Instalado em $INSTALL_DIR"
echo "Próximos passos:"
echo "  1. nano /etc/minivod/minivod.env"
echo "  2. systemctl enable --now minivod"
echo "  3. systemctl enable --now minivod-scan.timer"
echo "  4. journalctl -u minivod -f"
