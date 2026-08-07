#!/usr/bin/env bash
# Launch Agent Monitor.
#
#   ./run.sh                  native window, watching this repo
#   ./run.sh ~/code/project   native window, watching that repo
#   ./run.sh --browser        browser instead of the native window
#
# Falls back to the browser automatically if Electron isn't available.

set -euo pipefail
cd "$(dirname "$0")"

BROWSER=0
TARGET=""
for arg in "$@"; do
  case "$arg" in
    --browser) BROWSER=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) TARGET="$arg" ;;
  esac
done

# Prefer the project venv; fall back to whatever python is on PATH.
if [ -x "env_cv/bin/python" ]; then
  PY="env_cv/bin/python"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

if ! "$PY" -c "import agent_monitor" 2>/dev/null; then
  echo "Dependencies are missing. Install them with:" >&2
  echo "  $PY -m pip install -e '.[all]'" >&2
  exit 1
fi

# Refuse to start a second copy silently — the port and window would collide.
if pgrep -f "python -m agent_monitor" >/dev/null 2>&1; then
  echo "Agent Monitor already appears to be running." >&2
  echo "Stop it first, or use its existing window." >&2
  exit 1
fi

REPO="$(cd "${TARGET:-.}" 2>/dev/null && pwd)" || { echo "no such directory: $TARGET" >&2; exit 1; }

if [ "$BROWSER" -eq 0 ] && command -v electron >/dev/null 2>&1; then
  # The Electron shell spawns the backend itself and picks a free port.
  exec electron electron --repo "$REPO"
fi

if [ "$BROWSER" -eq 0 ] && [ -x "electron/node_modules/.bin/electron" ]; then
  exec electron/node_modules/.bin/electron electron --repo "$REPO"
fi

if [ "$BROWSER" -eq 0 ]; then
  echo "Electron not found — falling back to the browser." >&2
  echo "For the native window: cd electron && npm install" >&2
fi

exec "$PY" -m agent_monitor "$REPO"
