#!/usr/bin/env bash
# Install MultiZip on Linux for this user (no root rights needed):
#   - the program in ~/.local/lib/multizip, and the "multizip" command in ~/.local/bin
#   - an entry in the applications menu, with its icon
#   - MultiZip in the "Open with" list for zip, 7z, rar, tar ... (your default programs are not changed)
#   - right-click menu entries (Extract Here, Add to archive ...) in Files, Dolphin, Thunar, Nemo and Caja
# The menu entries themselves are written by MultiZip:  MultiZip --linux-integration install | remove
#
#   bash install-linux.sh               install, or update an earlier install
#   bash install-linux.sh --uninstall   remove it again (your settings in ~/.config/MultiZip are kept)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DIR="$HOME/.local/lib/multizip"
BIN="$HOME/.local/bin/multizip"
ICON="$DATA/icons/hicolor/256x256/apps/multizip.png"

refresh() {         # let the menu and the "Open with" lists notice the change (fine if these tools are missing)
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q "$DATA/applications" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
    fi
}

if [ "$(uname -s)" != Linux ]; then
    echo "This installer is for Linux. On macOS, drag dist/MultiZip.app into Applications."
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    if [ -x "$DIR/MultiZip" ]; then
        "$DIR/MultiZip" --linux-integration remove || true
    fi
    rm -f "$DATA/applications/multizip.desktop" "$ICON"
    if [ -L "$BIN" ] && [ "$(readlink "$BIN")" = "$DIR/MultiZip" ]; then
        rm -f "$BIN"                            # only our own link, never someone else's "multizip"
    fi
    rm -rf "$DIR"
    refresh
    echo "MultiZip was removed."
    echo "Your settings are still in ${XDG_CONFIG_HOME:-$HOME/.config}/MultiZip - delete that folder to remove them too."
    exit 0
fi

SRC=""
for c in "$HERE/MultiZip" "$HERE/dist/MultiZip"; do
    if [ -f "$c" ]; then
        SRC="$c"
        break
    fi
done
if [ -z "$SRC" ]; then
    echo "ERROR: the MultiZip program was not found next to this script."
    echo "Build it first with:  bash build.sh"
    exit 1
fi

mkdir -p "$DIR" "$(dirname "$BIN")" "$(dirname "$ICON")"
# copy, then rename: replacing works even while MultiZip is running
cp -f "$SRC" "$DIR/MultiZip.new"
chmod 755 "$DIR/MultiZip.new"
mv -f "$DIR/MultiZip.new" "$DIR/MultiZip"
if [ "$HERE" != "$DIR" ]; then                  # kept, so --uninstall and repairs work from there
    cp -f "$HERE/$(basename "$0")" "$DIR/install-linux.sh"
    if [ -f "$HERE/multizip.png" ]; then cp -f "$HERE/multizip.png" "$DIR/multizip.png"; fi
fi
if [ -f "$DIR/multizip.png" ]; then
    cp -f "$DIR/multizip.png" "$ICON.new"
    mv -f "$ICON.new" "$ICON"
fi

if [ -e "$BIN" ] && [ ! -L "$BIN" ]; then
    echo "NOTE: $BIN already exists and is not MultiZip's, so the multizip command was not added."
else
    ln -sfn "$DIR/MultiZip" "$BIN"
fi

echo
echo "Adding MultiZip to your menus ..."
"$DIR/MultiZip" --linux-integration install || { echo "ERROR: MultiZip could not add its menu entries."; exit 1; }
refresh

echo
echo "MultiZip is installed. Find it in your applications menu (you may need to log out and in once)."
case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "From a terminal:  multizip   or   multizip x archive.zip" ;;
    *) echo "From a terminal:  $BIN   (~/.local/bin is not on your PATH yet; it usually is after logging in again)" ;;
esac
echo "Make it the default for zip files:  xdg-mime default multizip.desktop application/zip"
echo "Remove it again:  bash $DIR/install-linux.sh --uninstall"
