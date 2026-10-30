#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
iconset="$root/.icon.iconset"
source_png="$root/.icon-base.png"
mkdir -p "$iconset"
magick -size 1024x1024 xc:none \
  -fill '#205449' -draw 'roundrectangle 42,42 982,982 218,218' \
  -fill '#eff5ef' -draw 'roundrectangle 268,263 756,761 43,43' \
  -fill none -stroke '#205449' -strokewidth 37 \
  -draw "path 'M 290,331 L 512,514 L 734,331'" \
  -stroke none -fill '#e4a95c' -draw 'circle 737,725 853,725' \
  -font '/System/Library/Fonts/Supplemental/Georgia.ttf' \
  -pointsize 174 -gravity center -fill '#1b3d37' -annotate +225+220 O \
  "$source_png"
for size in 16 32 128 256 512; do
  magick "$source_png" -resize "${size}x${size}" "$iconset/icon_${size}x${size}.png"
  double=$((size * 2))
  magick "$source_png" -resize "${double}x${double}" "$iconset/icon_${size}x${size}@2x.png"
done
iconutil -c icns "$iconset" -o "$root/icon.icns"
rm -f "$source_png"
