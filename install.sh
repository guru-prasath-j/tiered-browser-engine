#!/bin/bash
# One-time setup for Browser Helper on macOS / Linux.
# Run it from this folder:  bash install.sh   (or double-click "Install (Mac).command")
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

echo "== Browser Helper setup =="
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed."
  echo "Install it from https://www.python.org/downloads/ , then run this again."
  exit 1
fi
echo "Using $($PY --version)"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
echo "Installing (this takes a few minutes the first time)..."
.venv/bin/python -m pip install -q -e ".[home]"
.venv/bin/python -m playwright install chromium

if ! grep -qs '^GOOGLE_API_KEY=' "$HOME/.browser_engine/home.env"; then
  echo
  .venv/bin/browser-engine-home --set-key || true
fi

# Double-clickable launcher
if [ "$(uname)" = "Darwin" ]; then
  L="$HOME/Desktop/Browser Helper.command"
else
  mkdir -p "$HOME/Desktop"; L="$HOME/Desktop/browser-helper.sh"
fi
cat > "$L" <<LAUNCH
#!/bin/bash
cd "$HERE" && exec .venv/bin/browser-engine-home
LAUNCH
chmod +x "$L"

echo
echo "Done. To start: double-click '$(basename "$L")' on your Desktop"
echo "(or run: $HERE/.venv/bin/browser-engine-home). A chat page opens in your browser."
