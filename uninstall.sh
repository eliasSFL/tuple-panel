#!/usr/bin/env bash
# Remove Tuple Panel from ~/.local.
set -euo pipefail

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

rm -fv "$BIN_DIR/tuple-panel" "$APP_DIR/tuple-panel.desktop"
update-desktop-database "$APP_DIR" 2>/dev/null || true
echo "Uninstalled."
