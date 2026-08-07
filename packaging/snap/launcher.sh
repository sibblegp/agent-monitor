#!/bin/sh
# Launch the Electron shell inside the snap.
#
# This exists because snapd will not accept `=` in an app's `command:` field,
# and both flags below need it.
#
# --no-sandbox: Chromium's setuid sandbox cannot work inside a snap. Asking for
#   `browser-support` with allow-sandbox would force a manual store review, so
#   snap confinement does that job instead.
# --class: makes the window's WM_CLASS match StartupWMClass in the desktop
#   entry. Without them agreeing, the taskbar shows a generic icon beside the
#   running app rather than ours.

exec "$SNAP/app/agent-monitor" --no-sandbox --class=agentmonitor "$@"
