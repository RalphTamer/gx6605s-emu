#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app="$task_root/StarSat Emulator.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/python"
clang -O3 -Wall -Wextra -Werror -dynamiclib "$task_root/native/csky_fast.c" \
  -o "$task_root/starsat_emu/_csky_fast.dylib"
xcrun swiftc -O -parse-as-library -framework SwiftUI -framework AppKit \
  "$task_root/native/StarsatApp.swift" -o "$app/Contents/MacOS/StarSatEmulator"
cp -R "$task_root/starsat_emu" "$app/Contents/Resources/python/"
printf '%s\n' "$task_root" > "$app/Contents/Resources/workspace.txt"
command -v python3 > "$app/Contents/Resources/python-path.txt"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>StarSatEmulator</string>
<key>CFBundleIdentifier</key><string>local.starsat.emulator</string>
<key>CFBundleName</key><string>StarSat Emulator</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.4.6</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cp "$task_root/COPYING.LESSER" "$app/Contents/Resources/"
cp "$task_root/COPYING.XBOOT" "$app/Contents/Resources/"
cp "$task_root/docs/references.md" "$app/Contents/Resources/"
cp "$task_root/docs/boot-status.md" "$app/Contents/Resources/"
cp "$task_root/docs/graphics-command-notes.md" "$app/Contents/Resources/"
cp "$task_root/docs/av-microcode-notes.md" "$app/Contents/Resources/"
cp "$task_root/native/csky_fast.c" "$app/Contents/Resources/"
printf 'Built %s\n' "$app"
