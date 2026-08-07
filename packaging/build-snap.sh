#!/usr/bin/env bash
# Build the Agent Monitor snap.
#
#   ./packaging/build-snap.sh            build agentmonitor_<version>_amd64.snap
#   ./packaging/build-snap.sh --shell    drop into the build container instead
#
# Unlike the macOS build this one runs anywhere Docker does, because the real
# build environment is the container — see packaging/snap/Dockerfile for why it
# has to be Ubuntu 24.04 specifically.
#
# The tree is copied to dist-snap/src before building rather than built in
# place, so a root-owned parts/ and stage/ never appear in your working
# directory and an interrupted build cannot leave the checkout dirty.
#
# Afterwards, to try it locally (needs snapd running on this machine):
#   sudo snap install --dangerous dist-app/agentmonitor_*.snap
# To publish, see packaging/publish-snap.sh.

set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=agent-monitor-snapcraft
WORK=dist-snap
OUT=dist-app

command -v docker >/dev/null || { echo "docker not found — install Docker first" >&2; exit 1; }
command -v rsync >/dev/null || { echo "rsync not found" >&2; exit 1; }

echo "==> Build environment"
docker build -q -t "$IMAGE" packaging/snap

echo "==> Staging a clean copy of the tree"
rm -rf "$WORK"
mkdir -p "$WORK/src" "$OUT"
rsync -a \
  --exclude '.git/' \
  --exclude 'env_cv/' --exclude '.venv/' \
  --exclude 'node_modules/' \
  --exclude 'dist/' --exclude 'dist-app/' --exclude 'dist-snap/' \
  --exclude 'parts/' --exclude 'stage/' --exclude 'prime/' \
  --exclude '__pycache__/' --exclude '*.pyc' \
  ./ "$WORK/src/"

if [ "${1:-}" = "--shell" ]; then
  exec docker run --rm -it \
    -v "$PWD/$WORK/src:/build" \
    -v agent-monitor-snap-cache:/root/.cache \
    "$IMAGE" bash
fi

echo "==> snapcraft"
# The cache volume keeps Electron's ~100 MB download and the npm cache between
# runs; without it every build re-fetches them.
docker run --rm \
  -v "$PWD/$WORK/src:/build" \
  -v agent-monitor-snap-cache:/root/.cache \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  "$IMAGE" bash packaging/snap/build-in-container.sh "$@"

shopt -s nullglob
built=("$WORK"/src/*.snap)
[ ${#built[@]} -gt 0 ] || { echo "snapcraft produced no .snap" >&2; exit 1; }
mv -f "${built[@]}" "$OUT/"

echo
echo "Done:"
for f in "${built[@]}"; do
  printf '  %s  (%s)\n' "$OUT/$(basename "$f")" "$(du -h "$OUT/$(basename "$f")" | cut -f1)"
done
