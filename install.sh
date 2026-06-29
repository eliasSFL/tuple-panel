#!/usr/bin/env bash
# Install Tuple Panel into ~/.local (user-local, no root needed).
#   - the app  -> ~/.local/bin/tuple-panel        (executable, on your PATH)
#   - launcher -> ~/.local/share/applications/tuple-panel.desktop
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$BIN_DIR" "$APP_DIR"

install -m 0755 "$SRC_DIR/tuple_panel.py" "$BIN_DIR/tuple-panel"
install -m 0644 "$SRC_DIR/tuple-panel.desktop" "$APP_DIR/tuple-panel.desktop"

update-desktop-database "$APP_DIR" 2>/dev/null || true

echo "Installed:"
echo "  $BIN_DIR/tuple-panel"
echo "  $APP_DIR/tuple-panel.desktop"

case ":$PATH:" in
  *":$BIN_DIR:"*) echo "Run it with: tuple-panel" ;;
  *) echo "NOTE: $BIN_DIR is not on your PATH — add it, e.g.:"
     echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.profile' ;;
esac
