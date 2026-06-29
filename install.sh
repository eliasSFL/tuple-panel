#!/usr/bin/env bash
# Install Tuple Panel into ~/.local (user-local, no root needed).
#   - the app  -> ~/.local/bin/tuple-panel        (executable, on your PATH)
#   - launcher -> ~/.local/share/applications/tuple-panel.desktop
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

install -m 0755 "$SRC_DIR/tuple_panel.py" "$BIN_DIR/tuple-panel"
install -m 0644 "$SRC_DIR/tuple-panel.desktop" "$APP_DIR/tuple-panel.desktop"
install -m 0644 "$SRC_DIR/tuple-panel.svg" "$ICON_DIR/tuple-panel.svg"

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" 2>/dev/null || true

echo "Installed:"
echo "  $BIN_DIR/tuple-panel"
echo "  $APP_DIR/tuple-panel.desktop"
echo "  $ICON_DIR/tuple-panel.svg"

case ":$PATH:" in
  *":$BIN_DIR:"*) echo "Run it with: tuple-panel" ;;
  *) echo "NOTE: $BIN_DIR is not on your PATH — add it, e.g.:"
     echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.profile' ;;
esac
