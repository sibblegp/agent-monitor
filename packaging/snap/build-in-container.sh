#!/usr/bin/env bash
# Runs inside the container built from packaging/snap/Dockerfile.
# Not meant to be run directly — use packaging/build-snap.sh.

set -euo pipefail

# Snapcraft runs as root, so everything it writes — the .snap plus the parts,
# stage and prime trees — lands root-owned in a directory the invoking user has
# to be able to delete afterwards. On a trap rather than at the end, because a
# failed build is exactly when you re-run and would otherwise hit a staging
# directory you have no permission to remove.
give_back() {
  status=$?
  if [ -n "${HOST_UID:-}" ]; then
    chown -R "${HOST_UID}:${HOST_GID:-$HOST_UID}" /build || true
  fi
  exit $status
}
trap give_back EXIT

snapcraft pack --destructive-mode --verbosity=brief "$@"
