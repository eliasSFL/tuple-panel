#!/usr/bin/env bash
# Remove Tuple Panel from ~/.local.
set -euo pipefail

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

rm -fv "$BIN_DIR/tuple-panel" "$BIN_DIR/update-tuple" \
       "$APP_DIR/app.tuple.Panel.desktop" "$APP_DIR/tuple-panel.desktop" \
       "$ICON_DIR/tuple-panel.svg"
update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" 2>/dev/null || true
echo "Uninstalled the panel. The 'tuple' CLI and your login were left untouched"
echo "(remove /usr/bin/tuple manually if you want it gone)."
