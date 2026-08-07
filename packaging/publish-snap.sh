#!/usr/bin/env bash
# Publish the Agent Monitor snap to the Snap Store.
#
#   ./packaging/publish-snap.sh login            authenticate once (interactive)
#   ./packaging/publish-snap.sh edge             upload and release to a channel
#   ./packaging/publish-snap.sh stable
#   ./packaging/publish-snap.sh --no-release     upload only, release by hand later
#
# Authentication happens inside the same container the build uses, because that
# is the only place snapcraft exists on a non-Ubuntu host. `login` stores a
# credentials file on this machine at 0600, scoped to this snap and to the
# store operations publishing needs — nothing else in the account. Delete that
# file to revoke this machine's access.
#
# Releasing to `stable` makes the build the default download for everyone who
# runs `snap install agentmonitor`. `edge` is the one to use while you are still
# checking that it launches on a real machine.

set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=agent-monitor-snapcraft
SNAP_NAME=agentmonitor
CRED_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/agent-monitor"
CRED_FILE="$CRED_DIR/snapcraft.credentials"

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "build environment missing — run ./packaging/build-snap.sh first" >&2
  exit 1
}

# ---- login ----------------------------------------------------------

if [ "${1:-}" = "login" ]; then
  # snapcraft prompts for an email, a password and a 2FA code, all of which
  # need a real terminal. Say so plainly — without this the only symptom is
  # docker's "cannot attach stdin to a TTY-enabled container", which does not
  # suggest the fix.
  if [ ! -t 0 ] || [ ! -t 1 ]; then
    echo "This step is interactive: it asks for your Ubuntu One email," >&2
    echo "password and 2FA code, which needs a real terminal." >&2
    echo >&2
    echo "Run it in a terminal window directly, not through a tool, pipe or" >&2
    echo "editor integration:" >&2
    echo "    ./packaging/publish-snap.sh login" >&2
    echo >&2
    echo "Already have credentials from elsewhere? Skip this and export" >&2
    echo "SNAPCRAFT_STORE_CREDENTIALS instead." >&2
    exit 1
  fi
  mkdir -p "$CRED_DIR"
  docker run --rm -it -v "$CRED_DIR:/creds" "$IMAGE" \
    snapcraft export-login \
      --snaps "$SNAP_NAME" \
      --acls package_access,package_push,package_update,package_release \
      --channels stable,candidate,beta,edge \
      /creds/snapcraft.credentials
  chmod 600 "$CRED_FILE"
  echo
  echo "Stored at $CRED_FILE (0600). Delete it to revoke."
  exit 0
fi

# ---- upload ---------------------------------------------------------

shopt -s nullglob
snaps=(dist-app/"$SNAP_NAME"_*.snap)
[ ${#snaps[@]} -gt 0 ] || { echo "no snap in dist-app/ — run ./packaging/build-snap.sh" >&2; exit 1; }
if [ ${#snaps[@]} -gt 1 ]; then
  echo "more than one snap in dist-app/; remove the ones you don't want:" >&2
  printf '  %s\n' "${snaps[@]}" >&2
  exit 1
fi
SNAP_FILE="${snaps[0]}"

if [ -n "${SNAPCRAFT_STORE_CREDENTIALS:-}" ]; then
  CREDS="$SNAPCRAFT_STORE_CREDENTIALS"
elif [ -r "$CRED_FILE" ]; then
  CREDS="$(cat "$CRED_FILE")"
else
  echo "not authenticated — run: ./packaging/publish-snap.sh login" >&2
  exit 1
fi

args=(upload "/snap/$(basename "$SNAP_FILE")")
case "${1:-}" in
  ""|--no-release) ;;
  *) args+=(--release "$1") ;;
esac

echo "==> Uploading $(basename "$SNAP_FILE") to '$SNAP_NAME'${1:+, releasing to $1}"
# Only ask for a TTY when there is one: the upload itself is non-interactive
# once credentials are in the environment, so it should still work when driven
# by a script or a tool.
tty_flag=()
[ -t 0 ] && [ -t 1 ] && tty_flag=(-it)
docker run --rm "${tty_flag[@]}" \
  -v "$PWD/$(dirname "$SNAP_FILE"):/snap:ro" \
  -e SNAPCRAFT_STORE_CREDENTIALS="$CREDS" \
  "$IMAGE" snapcraft "${args[@]}"

echo
echo "Check it with:  snap info $SNAP_NAME"
