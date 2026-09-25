#!/bin/bash
# AG Client Addon one-time setup (macOS): double-click to install.
# Installs the Mac Electron runtime and builds the launcher applet.
# No prerequisites: the Electron runtime zip (about 100 MB) is downloaded
# straight from GitHub releases - Node.js/npm are NOT needed (2026-07-29).
#
# If double-clicking fails with a permission error (happens when this folder
# was raw-copied from a Windows filesystem, which drops exec bits), run once:
#   bash "Install AG Client Addon.command"
set -e
cd "$(dirname "$0")"
echo "== AG Client Addon setup (macOS) =="

# --- Network-location guard (2026-07-30): this distribution is for LOCAL
# copies. A run on a file server / network volume is unsupported (and has
# corrupted a distribution master once) - refuse before touching anything.
# Only /dev/* filesystems (local disks incl. USB) pass.
FSDEV="$(df -P . | awk 'NR==2{print $1}')"
case "$FSDEV" in
    /dev/*) ;;
    *)
        echo "ERROR: This folder is on a network volume (file server)."
        echo "Copy the folder to this Mac's local disk and run the installer there."
        echo "Nothing was installed."
        exit 1
        ;;
esac

# Electron version: the single source of truth is package.json.
EVER="$(sed -n 's/.*"electron": "\([0-9.]*\)".*/\1/p' package.json)"
if [ -z "$EVER" ]; then
    echo "ERROR: could not read the Electron version from package.json."
    exit 1
fi

case "$(uname -m)" in
    arm64) EPLAT="darwin-arm64" ;;
    *)     EPLAT="darwin-x64" ;;
esac

# Download BEFORE deleting anything - a failed download must not destroy
# a working install.
EZIP="$(mktemp -t electron_zip)"
echo "Downloading Electron v$EVER ($EPLAT, about 100 MB)..."
if ! curl -fL --retry 3 -o "$EZIP" "https://github.com/electron/electron/releases/download/v$EVER/electron-v$EVER-$EPLAT.zip"; then
    echo "ERROR: download failed. Check your internet connection and retry."
    rm -f "$EZIP"
    exit 1
fi

# A node_modules copied from another OS holds the wrong Electron binary —
# always start clean. The layout written here is a working subset of what
# npm used to create (dist/ + path.txt only; no cli.js/package metadata).
echo "Removing any existing node_modules..."
rm -rf node_modules
mkdir -p node_modules/electron/dist
# ditto preserves the symlinks and exec bits inside Electron.app.
if ! ditto -xk "$EZIP" node_modules/electron/dist; then
    echo "ERROR: extraction failed."
    rm -f "$EZIP"
    exit 1
fi
rm -f "$EZIP"
printf 'Electron.app/Contents/MacOS/Electron\n' > node_modules/electron/path.txt

EBIN="node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
if [ ! -x "$EBIN" ]; then
    echo "ERROR: Electron binary missing after extraction."
    exit 1
fi
echo -n "Electron: "
"$EBIN" --version

# --- Build AG Client Addon.app (launcher applet) ---
# Same layout as MotionPNGPlayer (実踏 2026-07-13): an osacompile applet that
# spawns node_modules' own Electron binary as a detached child, then quits.
# The Electron process keeps Electron.app's own bundle identity, so the
# menu-bar (tray) icon renders under LaunchServices. Shell-wrapper .apps
# (exec replaces the bundle executable) fail there.
echo "Building AG Client Addon.app..."
rm -rf "AG Client Addon.app"
osacompile -o "AG Client Addon.app" <<'APPLESCRIPT'
on run
    set appPath to POSIX path of (path to me)
    set parentDir to do shell script "dirname " & quoted form of appPath
    set electronPath to parentDir & "/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    do shell script quoted form of electronPath & " " & quoted form of parentDir & " > /dev/null 2>&1 &"
end run
APPLESCRIPT

# Cosmetic bundle identity (the applet quits right after spawning Electron)
PLIST="AG Client Addon.app/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
$PB -c "Set :CFBundleName 'AG Client Addon'" "$PLIST" 2>/dev/null || \
    $PB -c "Add :CFBundleName string 'AG Client Addon'" "$PLIST" 2>/dev/null || true

# Auto-start at sign-in (default ON - toggle from the menu-bar icon menu).
# LaunchAgent via `open -a <applet>`: same TCC-identity design as AG's own
# startup_registry. Label must stay in sync with LA_LABEL in main.js; the
# uninstaller removes the plist.
LA="$HOME/Library/LaunchAgents/com.agclientaddon.plist"
mkdir -p "$HOME/Library/LaunchAgents"
rm -f "$LA"
$PB -c "Add :Label string com.agclientaddon" "$LA" 2>/dev/null
$PB -c "Add :RunAtLoad bool true" "$LA"
$PB -c "Add :ProgramArguments array" "$LA"
$PB -c "Add :ProgramArguments:0 string /usr/bin/open" "$LA"
$PB -c "Add :ProgramArguments:1 string -a" "$LA"
$PB -c "Add :ProgramArguments:2 string $PWD/AG Client Addon.app" "$LA"
echo "Auto-start at sign-in: ON - toggle from the menu-bar icon menu."

echo "== Done. Double-click 'AG Client Addon.app' to start (menu-bar icon). =="
