#!/usr/bin/env bash
# Re-render the shipped brand PNGs from the SVG sources in this directory.
# Needs rsvg-convert (macOS: brew install librsvg; Debian: apt install librsvg2-bin).
set -euo pipefail
cd "$(dirname "$0")"
command -v rsvg-convert >/dev/null || { echo "rsvg-convert not found" >&2; exit 1; }

B=../custom_components/history_mover/brand
rsvg-convert -w 256 -h 256 icon.svg      -o "$B/icon.png"
rsvg-convert -w 512 -h 512 icon.svg      -o "$B/icon@2x.png"
rsvg-convert -h 128        logo.svg      -o "$B/logo.png"
rsvg-convert -h 256        logo.svg      -o "$B/logo@2x.png"
rsvg-convert -h 128        logo-dark.svg -o "$B/dark_logo.png"
rsvg-convert -h 256        logo-dark.svg -o "$B/dark_logo@2x.png"
echo "Rendered brand PNGs into $B"
