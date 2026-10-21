#!/bin/sh
# Regenerates every Chamo brand asset from source (macOS only: AppKit + iconutil).
set -eu
cd "$(dirname "$0")"
python3 make_chamo.py
swift make_wordmark.swift > chamo-wordmark.svg
swift make_wordmark.swift dark > chamo-wordmark-dark.svg
mkdir -p png build
swiftc -O render_svg.swift -o build/render_svg
R=build/render_svg
# App icon: hand-tuned small artwork for 16/32 px, full artwork from 64 px up.
ICONSET=build/Chamo.iconset; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for spec in 16:16:small 32:16@2x:small 32:32:small 64:32@2x:full 128:128:full 256:128@2x:full 256:256:full 512:256@2x:full 512:512:full 1024:512@2x:full; do
  px=${spec%%:*}; rest=${spec#*:}; name=${rest%%:*}; art=${rest#*:}
  src=chamo-icon.svg; [ "$art" = small ] && src=chamo-icon-small.svg
  $R "$src" "$ICONSET/icon_${name}x${name%%@*}.png" "$px" >/dev/null
done
# iconutil expects icon_16x16.png, icon_16x16@2x.png, ...
for f in "$ICONSET"/*; do b=$(basename "$f"); case "$b" in *@2x*) n=${b#icon_}; n=${n%%@2x*}; mv "$f" "$ICONSET/icon_${n}x${n}@2x.png";; esac; done
iconutil -c icns "$ICONSET" -o Chamo.icns
$R chamo-icon.svg png/chamo-icon-1024.png 1024 >/dev/null
$R chamo-mascot.svg png/chamo-mascot-1024.png 1024 >/dev/null
# Wordmark PNGs are for ad-hoc use only and are not committed.
$R chamo-wordmark.svg build/chamo-wordmark.png 872 >/dev/null
$R chamo-wordmark-dark.svg build/chamo-wordmark-dark.png 872 >/dev/null
echo "built Chamo.icns, png/* and build/chamo-wordmark*.png"
