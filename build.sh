#!/usr/bin/env bash
# MultiZip builder for Linux and macOS (build.bat does the same on Windows).
#
#   bash build.sh
#
# Linux -> dist/MultiZip       (one program file: double-click it or run ./dist/MultiZip)
# macOS -> dist/MultiZip.app   (drag it into Applications)
#
# Needs Python 3 with Tk:
#   Debian / Ubuntu:  sudo apt install python3 python3-venv python3-tk
#   Fedora:           sudo dnf install python3 python3-tkinter
#   macOS:            the installer from https://www.python.org/downloads/  (or: brew install python-tk)
# Optional, bundled when found: 7-Zip's 7zz program (RAR, ISO, CAB, DMG ... support)
#   Debian / Ubuntu: sudo apt install 7zip      macOS: brew install sevenzip
# Written for the bash 3.2 that macOS ships, so no newer bash features.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
if [ "$OS" = Darwin ]; then
    WORK="$HOME/Library/Caches/MultiZipBuild"
else
    WORK="${XDG_CACHE_HOME:-$HOME/.cache}/MultiZipBuild"
fi
LOG="$WORK/build.log"

fail() {
    echo
    echo "ERROR: $*"
    if [ -f "$LOG" ]; then
        echo "---- last lines of the build log ----"
        tail -n 30 "$LOG" || true
        echo "-------------------------------------"
        cp -f "$LOG" "$SRC/build.log" 2>/dev/null || true
    fi
    echo "BUILD FAILED. Full details are in: $LOG"
    exit 1
}

echo
echo "==== MultiZip builder ===="
echo
[ -f "$SRC/multizip.py" ] || fail "multizip.py was not found next to build.sh. Extract the whole download first."
mkdir -p "$WORK" || fail "could not create the work folder $WORK"
echo "MultiZip build log" > "$LOG"
echo "Building in: $WORK"

# ---------- 1. Python with Tk - 3.13 / 3.12 first: brand-new versions get only an old, unsafe py7zr
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
[ -n "$PY" ] || fail "Python 3 was not found. Install it (see the top of build.sh)."
"$PY" -c "import tkinter" >/dev/null 2>&1 || fail "this Python has no Tk, which the app window needs.
  Debian / Ubuntu: sudo apt install python3-tk    Fedora: sudo dnf install python3-tkinter
  macOS: use the installer from python.org, or: brew install python-tk"
MINOR="$("$PY" -c 'import sys; print(sys.version_info[1])')"
echo "Using Python 3.$MINOR"

# ---------- 2. private virtual environment (system Python untouched, no admin rights needed)
VPY="$WORK/venv/bin/python"
if [ -x "$VPY" ] && [ "$("$VPY" -c 'import sys; print(sys.version_info[1])' 2>/dev/null)" != "$MINOR" ]; then
    rm -rf "$WORK/venv"                         # made with another Python version: make it again
fi
if [ ! -x "$VPY" ]; then
    rm -rf "$WORK/venv"
    "$PY" -m venv "$WORK/venv" >>"$LOG" 2>&1 || fail "could not create a virtual environment.
  Debian / Ubuntu: sudo apt install python3-venv"
fi

echo
echo "[1/4] Installing PyInstaller ..."
"$VPY" -m pip install --upgrade pip pyinstaller >>"$LOG" 2>&1 || fail "could not install PyInstaller - check your internet connection."

# ---------- 3. Pillow is required, the rest are optional (skipped if they cannot install)
echo
echo "[2/4] Installing packages ..."
"$VPY" -m pip install --upgrade pillow >>"$LOG" 2>&1 || fail "could not install Pillow."
"$VPY" -c "from PIL import Image, ImageTk, ImageOps, ImageFont" >>"$LOG" 2>&1 || fail "Pillow installed but does not work."
# Pillow's own PyInstaller hook brings the image formats; AVIF (4 MB) is never used by MultiZip.
# py7zr only falls back to setuptools' copies of standard modules, which would drag setuptools in.
OPT=(--exclude-module PIL._avif --exclude-module PIL.AvifImagePlugin
     --exclude-module setuptools --exclude-module pkg_resources)

optional() {        # optional <pip name> <module> [hook]   - "hook": let PyInstaller's hook pick the files
    "$VPY" -m pip install --upgrade "$1" >>"$LOG" 2>&1 || true
    if "$VPY" -c "import $2" >>"$LOG" 2>&1; then
        echo "      $1 OK"
        if [ -z "${3:-}" ]; then OPT+=(--collect-all "$2"); fi
    else
        echo "      $1 could not be installed - building without it"
    fi
}
# py7zr before 0.20.2 has a known path-traversal bug. On a Python too new for current py7zr only such an
# old one installs: then it is left out, and the 7-Zip engine handles 7z files instead.
"$VPY" -m pip install --upgrade py7zr >>"$LOG" 2>&1 || true
if ! "$VPY" -c "import py7zr" >/dev/null 2>&1; then
    echo "      py7zr could not be installed - building without it"
elif "$VPY" -c 'import py7zr, re, sys; sys.exit(tuple(int(x) for x in re.findall(r"\d+", py7zr.__version__)[:3]) < (0, 20, 2))' >/dev/null 2>&1; then
    echo "      py7zr OK"
    OPT+=(--collect-all py7zr)
else
    echo "      py7zr: only an old version with a known security bug installs on Python 3.$MINOR -"
    echo "      leaving it out, so 7z files use the 7-Zip engine. Python 3.13 gets a current py7zr."
    OPT+=(--exclude-module py7zr)
fi
optional rarfile rarfile
optional tkinterdnd2 tkinterdnd2 hook          # only this computer's drag-and-drop library
if [ "$MINOR" -lt 14 ]; then                   # Python 3.14+ has zstd built in
    optional zstandard zstandard
else
    OPT+=(--exclude-module zstandard)
fi
for m in pyzstd pyppmd bcj inflate64 Cryptodome brotli multivolumefile texttable; do
    if "$VPY" -c "import $m" >/dev/null 2>&1; then OPT+=(--hidden-import "$m"); fi
done

# ---------- 4. the 7-Zip engine: only the standalone 7zz program (p7zip's 7z is a script plus a library)
echo
echo "[3/4] Bundling the 7-Zip engine (RAR, ISO, CAB, WIM, VHD, DMG ... support) ..."
SEVEN=""
if command -v 7zz >/dev/null 2>&1; then
    SEVEN="$("$VPY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$(command -v 7zz)")"
    if [ "$(head -c 2 "$SEVEN")" = "#!" ]; then SEVEN=""; fi
fi
if [ -n "$SEVEN" ]; then
    OPT+=(--add-binary "$SEVEN:.")
    echo "      bundled $SEVEN"
else
    echo "      7zz not found - the app will use 7-Zip if it is installed on the computer (see the top of build.sh)"
fi

# ---------- 5. build
echo
echo "[4/4] Building MultiZip - takes a minute or two ..."
if [ "$OS" = Darwin ]; then
    MODE=(--onedir --windowed)                 # a .app bundle (one-file .app bundles are deprecated)
    NAME="MultiZip.app"
else
    MODE=(--onefile)
    NAME="MultiZip"
fi
if [ -f "$SRC/multizip.png" ]; then OPT+=(--add-data "$SRC/multizip.png:."); fi      # the window icon
rm -rf "$WORK/dist" "$WORK/pkg"
"$VPY" -m PyInstaller --noconfirm --clean "${MODE[@]}" --name MultiZip --distpath "$WORK/dist" \
    --workpath "$WORK/pybuild" --specpath "$WORK" "${OPT[@]}" "$SRC/multizip.py" >>"$LOG" 2>&1 \
    || fail "PyInstaller failed to build MultiZip."
[ -e "$WORK/dist/$NAME" ] || fail "PyInstaller finished, but $NAME is missing."
echo "      built OK"

# ---------- 6. Linux: one archive to hand out - the program, its installer and icon
TARBALL=""
if [ "$OS" != Darwin ]; then
    [ -f "$SRC/install-linux.sh" ] || fail "install-linux.sh is missing next to build.sh."
    TARBALL="MultiZip-linux-$(uname -m).tar.gz"
    mkdir -p "$WORK/pkg/MultiZip"
    cp "$WORK/dist/MultiZip" "$SRC/install-linux.sh" "$WORK/pkg/MultiZip/"
    if [ -f "$SRC/multizip.png" ]; then cp "$SRC/multizip.png" "$WORK/pkg/MultiZip/"; fi
    # licences: MultiZip's own, and those of what the program bundles (7-Zip's must ship with it)
    for f in LICENSE THIRD-PARTY-NOTICES.md licenses; do
        if [ -e "$SRC/$f" ]; then cp -R "$SRC/$f" "$WORK/pkg/MultiZip/"; fi
    done
    chmod 755 "$WORK/pkg/MultiZip/MultiZip" "$WORK/pkg/MultiZip/install-linux.sh"
    tar -C "$WORK/pkg" -czf "$WORK/dist/$TARBALL" MultiZip >>"$LOG" 2>&1 || fail "could not pack $TARBALL."
    echo "      packed $TARBALL"
fi

# ---------- 7. copy the results next to build.sh (fall back to the work folder if that is not writable)
OUT="$SRC/dist"
if mkdir -p "$OUT" 2>/dev/null && rm -rf "$OUT/$NAME" && cp -R "$WORK/dist/$NAME" "$OUT/" 2>/dev/null \
        && { [ -z "$TARBALL" ] || cp -f "$WORK/dist/$TARBALL" "$OUT/" 2>/dev/null; }; then
    :
else
    OUT="$WORK/dist"
    echo "      NOTE: this folder is write-protected, so the results were saved here instead:"
    echo "            $OUT"
fi
cp -f "$LOG" "$SRC/build.log" 2>/dev/null || true

echo
echo "==== SUCCESS ===="
echo "Your app:  $OUT/$NAME"
if [ "$OS" = Darwin ]; then
    echo "(drag it into Applications. The first time, right-click it and choose Open: it is not signed by Apple.)"
else
    echo "Install it with a menu entry:  bash install-linux.sh"
    echo "To give it to someone else:    $OUT/$TARBALL"
    echo "  (they unpack it and run:  bash MultiZip/install-linux.sh)"
fi
