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

# Free local models: layer 1 (reads pages) and layer 2 (looks at screenshots)
echo
if command -v ollama >/dev/null 2>&1; then
  a=n
  read -r -p "Download the free local AI models for both layers (about 11 GB, one time)? [y/N] " a </dev/tty || a=n
  case "$a" in
    y|Y|yes|YES) ollama pull qwen2.5:7b && ollama pull qwen2.5vl:7b || echo "Model download failed; you can retry later with: ollama pull qwen2.5:7b && ollama pull qwen2.5vl:7b" ;;
    *) echo "Skipped. Later: ollama pull qwen2.5:7b && ollama pull qwen2.5vl:7b" ;;
  esac
else
  echo "Tip: install Ollama (https://ollama.com) and run 'ollama pull qwen2.5:7b && ollama pull qwen2.5vl:7b'"
  echo "     so the helper works on your computer for free and can read websites from screenshots."
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
