#!/usr/bin/env bash
# CHIP desktop app — Linux installer (run once, from the repo folder)
set -euo pipefail
cd "$(dirname "$0")"

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
    echo "python3 required. Install: sudo apt install python3"
    exit 1
fi

# ---- system deps: WebKitGTK bridge ---------------------------------------
if ! $PY -c "import gi; gi.require_version('WebKit2','4.1')" 2>/dev/null && \
   ! $PY -c "import gi; gi.require_version('WebKit2','4.0')" 2>/dev/null; then
    echo "Installing GTK/WebKit bridge (needs sudo)..."
    sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
        gir1.2-webkit2-4.1 || \
    sudo apt-get install -y python3-gi gir1.2-webkit2-4.0 || \
    { echo "Could not auto-install; try:"
      echo "  sudo apt install python3-gi gir1.2-webkit2-4.1"
      exit 1; }
fi

# ---- python deps ----------------------------------------------------------
echo "Installing Python deps (openai)..."
pip3 install --user --quiet openai || true

# ---- native cores (optional) ----------------------------------------------
if command -v g++ >/dev/null 2>&1; then
    echo "Building C++ core..."
    ( cd server && ./build.sh )
else
    echo "(skipping C++ core — no g++; pure-Python mode is fine)"
fi
if command -v cargo >/dev/null 2>&1; then
    echo "Building Rust core..."
    ( cd server && ./build_rs.sh )
else
    echo "(skipping Rust core — no cargo)"
fi

# ---- install to ~/.local ---------------------------------------------------
DST="$HOME/.local/share/chip-app"
mkdir -p "$DST" "$HOME/.local/share/icons/hicolor/scalable/apps" \
         "$HOME/.local/share/applications"

cp -r "server" "$DST/server"
cp chip_app.py "$DST/chip_app.py"
cp chip.svg    "$HOME/.local/share/icons/hicolor/scalable/apps/chip.svg"

sed "s|%INSTALL_DIR%|$DST|g" chip.desktop \
    > "$HOME/.local/share/applications/chip.desktop"

gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo
echo "Installed. Launch it from your app menu ('CHIP'), or run:  $DST/chip_app.py"
echo
echo "Tip: add your API key first — edit $DST/server/chip_web.py → OPENROUTER_API_KEY / ~/.config/chip/key"