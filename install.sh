#!/usr/bin/env bash
# Install Tuple Panel into ~/.local (user-local; only the tuple CLI needs root).
#   - the app    -> ~/.local/bin/tuple-panel      (executable, on your PATH)
#   - launcher   -> ~/.local/share/applications/tuple-panel.desktop
#   - updater    -> ~/.local/bin/update-tuple     (installs/updates the tuple CLI)
#   - the tuple CLI itself is bootstrapped via update-tuple if it's missing.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons/hicolor/scalable/apps"

mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

install -m 0755 "$SRC_DIR/tuple_panel.py" "$BIN_DIR/tuple-panel"
install -m 0755 "$SRC_DIR/scripts/update-tuple" "$BIN_DIR/update-tuple"
install -m 0644 "$SRC_DIR/data/tuple-panel.desktop" "$APP_DIR/tuple-panel.desktop"
install -m 0644 "$SRC_DIR/data/tuple-panel.svg" "$ICON_DIR/tuple-panel.svg"

# Point Exec at the absolute install path so it launches from the menu even if
# ~/.local/bin isn't on the graphical session's PATH.
sed -i "s|^Exec=.*|Exec=$BIN_DIR/tuple-panel|" "$APP_DIR/tuple-panel.desktop"

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$DATA_DIR/icons/hicolor" 2>/dev/null || true

echo "Installed:"
echo "  $BIN_DIR/tuple-panel"
echo "  $BIN_DIR/update-tuple"
echo "  $APP_DIR/tuple-panel.desktop"
echo "  $ICON_DIR/tuple-panel.svg"

# The panel is just a front-end — it needs the tuple CLI. Bootstrap it if absent.
if command -v tuple >/dev/null 2>&1; then
  echo "tuple CLI: already installed ($(command -v tuple)) — run 'update-tuple' to update it."
else
  echo
  echo "The 'tuple' CLI was not found; installing the latest release into $BIN_DIR."
  if "$BIN_DIR/update-tuple"; then
    echo "tuple CLI installed."
  else
    echo "WARNING: could not install the tuple CLI automatically." >&2
    echo "Run '$BIN_DIR/update-tuple' yourself to finish setup." >&2
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) echo "Run it with: tuple-panel" ;;
  *) echo "NOTE: $BIN_DIR is not on your PATH — add it, e.g.:"
     echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.profile' ;;
esac
