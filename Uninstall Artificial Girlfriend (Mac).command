#!/bin/bash
# Artificial Girlfriend uninstaller (macOS): double-click to remove.
# Deletes the program folder (including ALL conversation memories, characters,
# logs and TTS models), the per-user settings, the LaunchAgent auto-start,
# MotionPNGPlayer data, and the Whisper/BERT models this app downloaded into
# the HuggingFace cache. Nothing is removed until you type "Uninstall" at the
# confirmation prompt. Shared tools (Python, Homebrew, Ollama) are kept.
#
# If double-clicking fails with a permission error (exec bit lost by a raw
# copy or a ZIP download), run once:
#   bash "Uninstall Artificial Girlfriend (Mac).command"

cd "$(dirname "$0")" || exit 1
# Physical path is what matters for process matching and deletion; keep the
# logical (symlinked) spelling too - processes may have been started with it.
REPO_LOGICAL="$(pwd)"
REPO="$(pwd -P)"

# OS guard (same pattern as the installer): a mis-run from Git Bash / WSL on
# another OS must stop before touching anything.
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: This uninstaller is for macOS only."
    echo "       On Windows, run 'Uninstall Artificial Girlfriend (Windows).bat' instead."
    exit 1
fi

# Safety: only ever delete a folder that actually looks like AG.
if [ ! -f "$REPO/launcher/tray_app.py" ] || [ ! -f "$REPO/backend/backend.py" ]; then
    echo "ERROR: \"$REPO\" does not look like an Artificial Girlfriend folder."
    echo "Nothing was removed."
    read -r -p "Press Enter to finish. (Close this window manually.)"
    exit 1
fi

# HuggingFace cache location (same resolution order as huggingface_hub:
# default, then XDG_CACHE_HOME, then HF_HOME wins).
HFROOT="${HF_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/huggingface}"
HFHUB="$HFROOT/hub"

echo "== Artificial Girlfriend uninstaller =="
echo ""
echo "This will PERMANENTLY delete:"
echo "  - The program folder:        $REPO"
echo "    including ALL conversation memories, characters, logs and TTS models"
echo "  - Settings:                  ~/.config/ArtificialGirlfriend"
echo "  - Auto-start:                ~/Library/LaunchAgents/com.artificialgirlfriend.tray.plist"
echo "  - MotionPNGPlayer data:      ~/Library/Application Support/MotionPNGPlayer"
echo "  - Downloaded speech models:  $HFHUB"
echo "    only the Whisper / BERT models this app uses - other models stay"
echo "  - English TTS data:          ~/nltk_data - only the files this app downloaded"
echo ""
echo "Kept: Python 3.10, Homebrew, Ollama and its models."
echo ""
printf 'Type Uninstall (exactly, case-sensitive) and press Enter: '
read -r CONFIRM
if [ "$CONFIRM" != "Uninstall" ]; then
    echo ""
    echo "Cancelled. Nothing was removed."
    read -r -p "Press Enter to finish. (Close this window manually.)"
    exit 1
fi

# cd out of the repo before deleting it. Deleting the folder this script
# lives in is safe: bash holds the script's file descriptor open, so the
# inode survives until the script ends.
cd "$HOME" || exit 1

# --- Stop running processes first: if this fails, abort with nothing
# --- removed (no half-uninstalled state). Patterns are matched with
# --- grep -F (fixed string - path metacharacters can never break a regex);
# --- our own command line is the .command path, which contains neither
# --- "/venv" nor "/node_modules", so no self-match.
PATTERNS=("$REPO/venv" "$REPO/MotionPNGPlayer/node_modules")
if [ "$REPO_LOGICAL" != "$REPO" ]; then
    PATTERNS+=("$REPO_LOGICAL/venv" "$REPO_LOGICAL/MotionPNGPlayer/node_modules")
fi

kill_ag() {
    # $1 = signal. Two passes are driven by the caller: the tray watchdog
    # could respawn the backend in the gap after a first-pass kill.
    local pat
    for pat in "${PATTERNS[@]}"; do
        ps -axo pid=,command= | grep -F -- "$pat" | grep -v grep \
            | awk '{print $1}' | while read -r pid; do
                kill "$1" "$pid" 2>/dev/null
            done
    done
}

ag_alive() {
    local pat
    for pat in "${PATTERNS[@]}"; do
        if ps -axo command= | grep -F -- "$pat" | grep -v grep >/dev/null; then
            return 0
        fi
    done
    return 1
}

echo "Stopping Artificial Girlfriend processes..."
kill_ag -TERM
sleep 2
kill_ag -KILL
sleep 1
if ag_alive; then
    echo "ERROR: some Artificial Girlfriend processes could not be stopped."
    echo "Nothing was removed. Quit them and run the uninstaller again."
    read -r -p "Press Enter to finish. (Close this window manually.)"
    exit 1
fi

# === Destructive part starts here. Order: auto-start, then external traces,
# === then the program folder LAST - if the window is closed mid-run, the
# === re-run entry point (this script inside the repo) still exists and
# === every step below is idempotent.
echo "Removing auto-start registration..."
rm -f "$HOME/Library/LaunchAgents/com.artificialgirlfriend.tray.plist"
launchctl bootout "gui/$(id -u)/com.artificialgirlfriend.tray" >/dev/null 2>&1

# Best effort: clear the stale privacy grants (mic / input monitoring /
# screen recording) so they do not linger in System Settings. Harmless if
# none exist or tccutil refuses.
echo "Resetting macOS permission grants..."
tccutil reset All com.artificialgirlfriend.launcher >/dev/null 2>&1

echo "Removing settings and app data..."
rm -rf "$HOME/.config/ArtificialGirlfriend" \
       "$HOME/.config/AirtificialGirlfriend"
# motion-pngtuber-player = pre-rename Electron userData; Caches/electron =
# the download cache created by the old npm-based installs (pre 2026-07-29).
# "Application Support/Electron" is shared with other apps - NOT touched.
rm -rf "$HOME/Library/Application Support/MotionPNGPlayer" \
       "$HOME/Library/Application Support/motion-pngtuber-player" \
       "$HOME/Library/Caches/MotionPNGPlayer" \
       "$HOME/Library/Caches/motion-pngtuber-player" \
       "$HOME/Library/Caches/electron" \
       "$HOME/Library/Saved Application State/com.artificialgirlfriend.launcher.savedState"

echo "Removing English TTS data (nltk)..."
# Only the three resources audio_output downloads; parents are removed only
# when they end up empty (rmdir = fails silently on non-empty).
NLTK="$HOME/nltk_data"
if [ -d "$NLTK" ]; then
    rm -rf "$NLTK/taggers/averaged_perceptron_tagger_eng" \
           "$NLTK/taggers/averaged_perceptron_tagger_eng.zip" \
           "$NLTK/taggers/averaged_perceptron_tagger" \
           "$NLTK/taggers/averaged_perceptron_tagger.zip" \
           "$NLTK/corpora/cmudict" \
           "$NLTK/corpora/cmudict.zip"
    rmdir "$NLTK/taggers" "$NLTK/corpora" "$NLTK" 2>/dev/null
fi

echo "Removing downloaded speech models (HuggingFace cache)..."
if [ -d "$HFHUB" ]; then
    # (Kokoro assets live in kokoro/ at the repo root, removed with the
    # program folder; the hexgrad entry only covers manual/legacy downloads.)
    for d in "$HFHUB"/models--Systran--faster-whisper-* \
             "$HFHUB/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" \
             "$HFHUB/models--ku-nlp--deberta-v2-large-japanese-char-wwm" \
             "$HFHUB/models--microsoft--deberta-v3-large" \
             "$HFHUB/models--hexgrad--Kokoro-82M"; do
        [ -e "$d" ] || continue
        rm -rf "$d" "$HFHUB/.locks/$(basename "$d")"
    done
    # If the cache holds nothing but hub metadata now (no other models, no
    # login token), this machine only ever used it for AG - remove it whole.
    if [ ! -f "$HFROOT/token" ] \
       && ! find "$HFHUB" -maxdepth 1 \( -name 'models--*' -o -name 'datasets--*' -o -name 'spaces--*' \) 2>/dev/null | grep -q .; then
        rm -rf "$HFROOT"
    fi
fi

echo "Removing the program folder - this may take a while..."
rm -rf "$REPO"
FAILED=""
if [ -d "$REPO" ]; then
    FAILED=1
    echo "WARN: could not fully remove \"$REPO\"."
    echo "      Close anything using that folder, then delete it manually."
else
    echo "Program folder removed."
fi

echo ""
if [ -n "$FAILED" ]; then
    echo "== Uninstall finished with warnings - see above =="
else
    echo "== Uninstall complete =="
fi
echo "Kept: Python 3.10, Homebrew, Ollama and its models - remove those"
echo "      separately (e.g. brew uninstall python@3.10) if you no longer"
echo "      need them."
read -r -p "Press Enter to finish. (Close this window manually.)"
exit 0
