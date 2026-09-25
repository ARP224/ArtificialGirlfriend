#!/bin/bash
# MotionPNGPlayer one-time setup (macOS): double-click to install.
# For a COPY of this folder on a client machine (remote mode). Inside the
# Artificial Girlfriend program folder it refuses to run - there the AG
# installer at the folder root owns the MotionPNGPlayer setup.
# No prerequisites: the Electron runtime zip (about 100 MB) is downloaded
# straight from GitHub releases - Node.js/npm are NOT needed (2026-07-29).
#
# If double-clicking fails with a permission error (happens when this folder
# was raw-copied from a Windows filesystem, which drops exec bits), run once:
#   bash "Install MotionPNGPlayer.command"
set -e
cd "$(dirname "$0")"
echo "== MotionPNGPlayer setup (macOS) =="

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

# The refusal must run BEFORE anything destructive: on the AG server machine
# this folder's node_modules is the live runtime the backend launches, and
# the clean-install step below would delete it.
if [ -f "../launcher/tray_app.py" ] && [ -f "../backend/backend.py" ]; then
    echo "This copy of MotionPNGPlayer sits inside the Artificial Girlfriend"
    echo "program folder, so this installer does nothing here."
    echo "Run 'Install Artificial Girlfriend (Mac).command' at the AG folder"
    echo "root instead - it sets MotionPNGPlayer up too."
    exit 1
fi

# Asset/ is where users drop character motion folders. main.js creates it at
# first launch, but nothing auto-launches after install, so create it here
# for pre-launch asset drops (absent from fresh copies, 2026-07-30).
mkdir -p Asset

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

# --- Build MotionPNGPlayer.app (launcher applet) ---
# Keep in sync with the same build in "Install Artificial Girlfriend
# (Mac).command" (the server-machine path). osacompile applet that spawns
# node_modules' own Electron binary as a detached child, then quits — the
# Electron process keeps Electron.app's bundle identity, so the menu-bar
# (tray) icon renders under LaunchServices. Shell-wrapper .apps fail there.
echo "Building MotionPNGPlayer.app..."
rm -rf "MotionPNGPlayer.app"
osacompile -o "MotionPNGPlayer.app" <<'APPLESCRIPT'
on run
    set appPath to POSIX path of (path to me)
    set parentDir to do shell script "dirname " & quoted form of appPath
    set electronPath to parentDir & "/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    do shell script quoted form of electronPath & " " & quoted form of parentDir & " > /dev/null 2>&1 &"
end run
APPLESCRIPT

# Cosmetic bundle identity (the applet quits right after spawning Electron)
PLIST="MotionPNGPlayer.app/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
$PB -c "Set :CFBundleName MotionPNGPlayer" "$PLIST" 2>/dev/null || \
    $PB -c "Add :CFBundleName string MotionPNGPlayer" "$PLIST" 2>/dev/null || true

# Auto-start at sign-in (default ON - toggle from the menu-bar icon menu).
# LaunchAgent via `open -a <applet>`: same TCC-identity design as AG's own
# startup_registry. Label must stay in sync with LA_LABEL in main.js; the
# uninstaller removes the plist. Intentionally NOT done by the root AG
# installer - on the server machine the backend launches the player itself.
LA="$HOME/Library/LaunchAgents/com.motionpngplayer.plist"
mkdir -p "$HOME/Library/LaunchAgents"
rm -f "$LA"
$PB -c "Add :Label string com.motionpngplayer" "$LA" 2>/dev/null
$PB -c "Add :RunAtLoad bool true" "$LA"
$PB -c "Add :ProgramArguments array" "$LA"
$PB -c "Add :ProgramArguments:0 string /usr/bin/open" "$LA"
$PB -c "Add :ProgramArguments:1 string -a" "$LA"
$PB -c "Add :ProgramArguments:2 string $PWD/MotionPNGPlayer.app" "$LA"
echo "Auto-start at sign-in: ON - toggle from the menu-bar icon menu."

echo "== Done. Double-click 'MotionPNGPlayer.app' to start (tray icon);"
echo "   set the AG server URL in its settings. =="
