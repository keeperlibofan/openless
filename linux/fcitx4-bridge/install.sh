#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PREFIX="${OPENLESS_BRIDGE_PREFIX:-$HOME/.local/opt/openless-fcitx4-bridge}"
readonly SERVICE_DIR="$HOME/.config/systemd/user"
readonly SERVICE_PATH="$SERVICE_DIR/openless-fcitx4-bridge.service"
readonly PYTHON="/usr/bin/python3"

for command_name in gdbus install parec systemctl xinput xrandr; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  fi
done

if [[ ! -x "$PYTHON" ]]; then
  printf 'System Python was not found at %s\n' "$PYTHON" >&2
  exit 1
fi

"$PYTHON" - <<'PY'
import gi
import tkinter

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: F401
PY

install -d -m 0755 "$PREFIX" "$SERVICE_DIR"
for file in \
  openless_fcitx4_bridge.py \
  openless_history_refresh.py \
  openless_status_overlay.py \
  openless_x11_insert.py; do
  install -m 0755 "$SCRIPT_DIR/$file" "$PREFIX/$file"
done

if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  "$PYTHON" -m venv --system-site-packages "$PREFIX/venv"
fi
"$PREFIX/venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --requirement "$SCRIPT_DIR/requirements.txt"

sed "s|@PREFIX@|$PREFIX|g" \
  "$SCRIPT_DIR/openless-fcitx4-bridge.service.in" \
  >"$SERVICE_PATH.tmp"
chmod 0644 "$SERVICE_PATH.tmp"
mv "$SERVICE_PATH.tmp" "$SERVICE_PATH"

systemctl --user daemon-reload
systemctl --user enable --now openless-fcitx4-bridge.service
systemctl --user restart openless-fcitx4-bridge.service
systemctl --user --no-pager --full status openless-fcitx4-bridge.service

printf '\nInstalled OpenLess Fcitx4/Sogou bridge at %s\n' "$PREFIX"
