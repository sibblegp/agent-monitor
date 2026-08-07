#!/usr/bin/env bash
# Rasterize packaging/icon.svg into every PNG the packaging needs.
#
# Run this after editing the SVG; the PNGs are committed so a build never needs
# rsvg-convert present.
#
#   electron/build/icon.png   1024px — electron-builder derives .icns/.ico from
#                                      this automatically when no platform icon
#                                      is supplied, so macOS gets it too
#   snap/gui/agentmonitor.png  512px — the snap desktop entry's icon

set -euo pipefail
cd "$(dirname "$0")/.."

command -v rsvg-convert >/dev/null || {
  echo "rsvg-convert not found (package: librsvg)" >&2
  exit 1
}

mkdir -p electron/build snap/gui

render() { # size, destination
  rsvg-convert -w "$1" -h "$1" packaging/icon.svg -o "$2"
  echo "  $2 (${1}px)"
}

echo "Rendering icons from packaging/icon.svg:"
render 1024 electron/build/icon.png
render 512 snap/gui/agentmonitor.png
