#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly APP_DIR="$REPO_ROOT/openless-all/app"
readonly VERSION="$(node -p "require('$APP_DIR/package.json').version")"
readonly PREFIX="${OPENLESS_INSTALL_PREFIX:-$HOME/.local/opt/openless-$VERSION}"
readonly BIN_DIR="$HOME/.local/bin"
readonly APPLICATION_DIR="$HOME/.local/share/applications"
readonly ICON_DIR="$HOME/.local/share/icons/hicolor/128x128/apps"

for command_name in cargo install node npm sed systemctl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  fi
done

"$REPO_ROOT/linux/fcitx4-bridge/install.sh"

(
  cd "$APP_DIR"
  npm ci
  npm run build
)

(
  cd "$APP_DIR/src-tauri"
  cargo build --release
)

install -d -m 0755 \
  "$PREFIX/bin" \
  "$PREFIX/lib" \
  "$BIN_DIR" \
  "$APPLICATION_DIR" \
  "$ICON_DIR"
install -m 0755 "$APP_DIR/src-tauri/target/release/openless" "$PREFIX/bin/openless"
install -m 0644 "$APP_DIR/src-tauri/icons/128x128.png" "$ICON_DIR/openless.png"

sed "s|@PREFIX@|$PREFIX|g" "$REPO_ROOT/linux/openless-wrapper.in" \
  >"$BIN_DIR/openless.tmp"
chmod 0755 "$BIN_DIR/openless.tmp"
mv "$BIN_DIR/openless.tmp" "$BIN_DIR/openless"

sed "s|@HOME@|$HOME|g" "$REPO_ROOT/linux/OpenLess.desktop.in" \
  >"$APPLICATION_DIR/OpenLess.desktop.tmp"
chmod 0644 "$APPLICATION_DIR/OpenLess.desktop.tmp"
mv "$APPLICATION_DIR/OpenLess.desktop.tmp" "$APPLICATION_DIR/OpenLess.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATION_DIR" >/dev/null 2>&1 || true
fi

if LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  ldd "$PREFIX/bin/openless" | grep -q 'not found'; then
  printf 'The OpenLess binary still has missing runtime libraries:\n' >&2
  LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    ldd "$PREFIX/bin/openless" | grep 'not found' >&2
  exit 1
fi

printf '\nInstalled OpenLess %s at %s\n' "$VERSION" "$PREFIX"
printf 'Launch it with: %s/openless\n' "$BIN_DIR"
