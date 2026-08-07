#!/usr/bin/env bash
# Build "Agent Monitor.app" and a .dmg. Run this on the Mac you're targeting.
#
#   ./packaging/build-macos.sh            unsigned, for your own machine
#   ./packaging/build-macos.sh --signed   signed + notarized (see below)
#
# PyInstaller cannot cross-compile: it freezes the interpreter it is running
# under, so an arm64 app has to be built on Apple Silicon and an Intel one on
# Intel. Building on an M-series Mac produces an arm64-only app, which is what
# you want unless you're shipping to Intel machines too.
#
# Signing and notarization need an Apple Developer account. Export these first:
#
#   export APPLE_ID="you@example.com"
#   export APPLE_APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx"   # appleid.apple.com
#   export APPLE_TEAM_ID="ABCDE12345"
#   export CSC_NAME="Developer ID Application: Your Name (ABCDE12345)"
#
# Without them the app still builds and still runs — macOS just refuses the
# first launch until you right-click → Open, or clear the quarantine flag:
#
#   xattr -dr com.apple.quarantine "/Applications/Agent Monitor.app"

set -euo pipefail
cd "$(dirname "$0")/.."

SIGNED=0
[ "${1:-}" = "--signed" ] && SIGNED=1

if [ "$(uname)" != "Darwin" ]; then
  echo "This builds a macOS app and must run on macOS." >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
command -v npm >/dev/null || { echo "npm not found — install Node.js" >&2; exit 1; }

echo "==> Python environment"
if [ ! -x "env_cv/bin/python" ]; then
  python3 -m venv env_cv
fi
env_cv/bin/pip install --quiet --upgrade pip
env_cv/bin/pip install --quiet -e '.[all,pkg]'

echo "==> Freezing the backend (arch: $(uname -m))"
rm -rf build dist/agent-monitor-backend
(cd packaging && ../env_cv/bin/pyinstaller agent-monitor.spec --noconfirm --distpath ../dist --workpath ../build)

# Fail loudly here rather than shipping an app whose backend never starts.
BACKEND="dist/agent-monitor-backend/agent-monitor-backend"
[ -x "$BACKEND" ] || { echo "PyInstaller produced no backend binary" >&2; exit 1; }
echo "==> Smoke-testing the frozen backend"
"$BACKEND" --help >/dev/null || { echo "the frozen backend cannot start" >&2; exit 1; }

echo "==> Building the app"
cd electron
npm install --silent
if [ "$SIGNED" -eq 1 ]; then
  : "${CSC_NAME:?set CSC_NAME to your Developer ID Application certificate}"
  : "${APPLE_ID:?set APPLE_ID}"
  : "${APPLE_TEAM_ID:?set APPLE_TEAM_ID}"
  npx electron-builder --mac
else
  # Skip the signing step entirely; an ad-hoc signature is enough to launch.
  CSC_IDENTITY_AUTO_DISCOVERY=false npx electron-builder --mac
fi

echo
echo "Done. Artifacts are in dist-app/:"
ls -1 ../dist-app/*.dmg ../dist-app/*.zip 2>/dev/null || true
if [ "$SIGNED" -eq 0 ]; then
  echo
  echo "This build is unsigned. On first launch macOS will refuse to open it;"
  echo "right-click the app and choose Open, or run:"
  echo "  xattr -dr com.apple.quarantine \"/Applications/Agent Monitor.app\""
fi
