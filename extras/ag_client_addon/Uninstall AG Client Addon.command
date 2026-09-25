#!/bin/bash
# AG Client Addon uninstaller (macOS): double-click to remove.
# Deletes this addon folder (including node_modules and the launcher .app),
# the per-user addon settings, the Electron download cache (leftover of the
# old npm-based setup, pre 2026-07-29) and the Login
# Items entry. Refuses to run when this folder sits inside
# the Artificial Girlfriend program folder - there the main AG uninstaller
# owns the removal.
#
# If double-clicking fails with a permission error (exec bit lost by a raw
# copy from a Windows filesystem), run once:
#   bash "Uninstall AG Client Addon.command"

cd "$(dirname "$0")" || exit 1
# Physical path is what matters for process matching and deletion; keep the
# logical (symlinked) spelling too - processes may have been started with it.
ADDON_LOGICAL="$(pwd)"
ADDON="$(pwd -P)"

# OS guard (same pattern as the installer): a mis-run from Git Bash / WSL on
# another OS must stop before touching anything.
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: This uninstaller is for macOS only."
    echo "       On Windows, run 'Uninstall AG Client Addon.bat' instead."
    exit 1
fi

# --- Network-location guard (2026-07-30): this distribution is for LOCAL
# copies. A network copy holds no per-user settings on this machine, so
# deleting the folder there IS the full uninstall - refuse the script run.
FSDEV="$(df -P . | awk 'NR==2{print $1}')"
case "$FSDEV" in
    /dev/*) ;;
    *)
        echo "ERROR: This folder is on a network volume (file server)."
        echo "Nothing was removed."
        echo "To remove a copy on a file server, simply delete the folder there."
        exit 1
        ;;
esac

# Safety: only ever delete a folder that actually looks like the addon.
if [ ! -f "$ADDON/main.js" ] || [ ! -f "$ADDON/package.json" ]; then
    echo "ERROR: \"$ADDON\" does not look like an AG Client Addon folder."
    echo "Nothing was removed."
    read -r -p "Press Enter to finish. (Close this window manually.)"
    exit 1
fi

# Refuse to run inside the AG program folder (ryo ruling 2026-07-28): there
# the addon is part of the AG install itself - removing only the addon is
# never a normal operation, and the main uninstaller owns that folder.
if [ -f "$ADDON/../../launcher/tray_app.py" ] && [ -f "$ADDON/../../backend/backend.py" ]; then
    echo "This copy of AG Client Addon sits inside the Artificial Girlfriend"
    echo "program folder, so this uninstaller does nothing here."
    echo "To remove AG as a whole, run 'Uninstall Artificial Girlfriend (Mac).command'."
    echo "Nothing was removed."
    read -r -p "Press Enter to finish. (Close this window manually.)"
    exit 1
fi

echo "== AG Client Addon uninstaller =="
echo ""
echo "This will PERMANENTLY delete:"
echo "  - The addon folder:        $ADDON"
echo "  - Addon settings:          ~/Library/Application Support/AG Client Addon"
echo "  - Electron download cache: ~/Library/Caches/electron"
echo "  - Auto-start registration (LaunchAgent) and any Login Items entry"
echo ""
# Typed word, same as every other uninstaller (ryo ruling 2026-07-30:
# one confirmation convention across the board).
printf 'Type Uninstall (exactly, case-sensitive) and press Enter: '
read -r CONFIRM
case "$CONFIRM" in
    Uninstall) ;;
    *)
        echo ""
        echo "Cancelled. Nothing was removed."
        read -r -p "Press Enter to finish. (Close this window manually.)"
        exit 1
        ;;
esac

# cd out of the folder before deleting it. Deleting the folder this script
# lives in is safe: bash holds the script's file descriptor open, so the
# inode survives until the script ends.
cd "$HOME" || exit 1

# --- Stop the running addon first: if this fails, abort with nothing
# --- removed. Patterns are matched with grep -F (fixed string - path
# --- metacharacters can never break a regex); our own command line is this
# --- .command path, which does not contain "/node_modules", so no
# --- self-match.
PATTERNS=("$ADDON/node_modules")
if [ "$ADDON_LOGICAL" != "$ADDON" ]; then
    PATTERNS+=("$ADDON_LOGICAL/node_modules")
fi

kill_addon() {
    local pat
    for pat in "${PATTERNS[@]}"; do
        ps -axo pid=,command= | grep -F -- "$pat" | grep -v grep \
            | awk '{print $1}' | while read -r pid; do
                kill "$1" "$pid" 2>/dev/null
            done
    done
}

addon_alive() {
    local pat
    for pat in "${PATTERNS[@]}"; do
        if ps -axo command= | grep -F -- "$pat" | grep -v grep >/dev/null; then
            return 0
        fi
    done
    return 1
}

echo "Stopping AG Client Addon..."
kill_addon -TERM
sleep 2
kill_addon -KILL
sleep 1
if addon_alive; then
    echo "ERROR: the running AG Client Addon could not be stopped."
    echo "Nothing was removed. Quit it from the menu-bar icon and run this again."
    read -r -p "Press Enter to finish. (Close this window manually.)"
    exit 1
fi

# === Destructive part starts here. Order: external traces first, the addon
# === folder LAST - if the window is closed mid-run, the re-run entry point
# === (this script inside the folder) still exists and every step below is
# === idempotent.
echo "Removing the Login Items entry..."
# Best effort: System Events may ask for an automation permission, and the
# entry may simply not exist - both are fine (a leftover is announced below).
osascript -e 'tell application "System Events" to delete login item "AG Client Addon"' >/dev/null 2>&1

echo "Removing the auto-start registration (LaunchAgent)..."
# Registered by the installer (default ON) / toggled from the tray menu.
rm -f "$HOME/Library/LaunchAgents/com.agclientaddon.plist"
launchctl bootout "gui/$(id -u)/com.agclientaddon" >/dev/null 2>&1 || true

echo "Removing addon settings..."
# ag-client-addon = defensive: userData if Electron ever resolved the
# package.json "name" instead of productName. Caches/electron = the download
# cache created by the installer's "npm install" (re-downloadable). The
# generic "Application Support/Electron" dir is shared with other apps - NOT
# touched.
rm -rf "$HOME/Library/Application Support/AG Client Addon" \
       "$HOME/Library/Application Support/ag-client-addon" \
       "$HOME/Library/Caches/electron"

echo "Removing the addon folder..."
rm -rf "$ADDON"
if [ -d "$ADDON" ]; then
    echo "WARN: could not fully remove \"$ADDON\"."
    echo "      Close anything using that folder, then delete it manually."
    echo ""
    echo "== Uninstall finished with warnings - see above =="
else
    echo "Addon folder removed."
    echo ""
    echo "== Uninstall complete =="
fi
echo "If 'AG Client Addon' still appears in System Settings > General >"
echo "Login Items, remove it there manually."
read -r -p "Press Enter to finish. (Close this window manually.)"
exit 0
