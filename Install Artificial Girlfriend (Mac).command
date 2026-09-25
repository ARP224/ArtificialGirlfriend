#!/bin/bash
# Artificial Girlfriend one-time setup (macOS / Apple Silicon): double-click to install.
# Creates the Python venv, installs requirements-mac.txt, and builds the
# launcher applet "Artificial Girlfriend.app" (osacompile — same layout the
# MotionPNGPlayer installer has used in service).
#
# If double-clicking fails with a permission error (exec bit lost by a raw
# copy from another filesystem), run once:
#   bash "Install Artificial Girlfriend (Mac).command"
set -e
cd "$(dirname "$0")"
echo "== Artificial Girlfriend setup (macOS) =="

# OS guard (稜要望 2026-07-24): Git Bash / WSL 等の Windows・Linux シェルから
# 誤実行されると venv を Mac 用 requirements で作り直すなど repo の状態を
# 壊しうる。Darwin 以外では何もせず即終了する。
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: This installer is for macOS only."
    echo "       On Windows, run 'Install Artificial Girlfriend (Windows).bat' instead."
    exit 1
fi

# Apple Silicon only (2026-07-20 ruling: Intel Macs are not supported)
if [ "$(uname -m)" != "arm64" ]; then
    echo "ERROR: This app supports Apple Silicon (arm64) only."
    exit 1
fi

# Python 3.10 (matches the pinned requirements snapshot; the pre-installed
# macOS Python 3.9 is NOT sufficient — key dependencies require 3.10+).
# The keg-only path is scanned too: Homebrew does not always symlink
# versioned pythons into /opt/homebrew/bin.
find_python() {
    for cand in python3.10 /opt/homebrew/bin/python3.10 \
                /opt/homebrew/opt/python@3.10/bin/python3.10; do
        if command -v "$cand" >/dev/null 2>&1; then echo "$cand"; return 0; fi
    done
    return 1
}
PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    if command -v brew >/dev/null 2>&1; then
        # One-shot flow: offer to install right here (consent required —
        # never touch the system silently).
        echo "Python 3.10 is not installed. It can be installed now via Homebrew."
        read -r -p "Run 'brew install python@3.10' now? [y/N] " answer
        case "$answer" in
            [yY]*) brew install python@3.10 ;;
            *)
                echo "Cancelled. Install Python 3.10 first:  brew install python@3.10"
                exit 1
                ;;
        esac
        PYTHON="$(find_python || true)"
        if [ -z "$PYTHON" ]; then
            echo "ERROR: Python 3.10 still not found after installation."
            echo "Open a NEW Terminal window and run this installer again."
            exit 1
        fi
    else
        echo "ERROR: Python 3.10 not found, and Homebrew is not installed."
        echo "Install Homebrew first (see https://brew.sh):"
        echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        echo "Then run this installer again (it will offer to install Python 3.10)."
        exit 1
    fi
fi
echo "Python: $("$PYTHON" --version)"

if [ ! -d venv ]; then
    echo "Creating venv..."
    "$PYTHON" -m venv venv
fi

echo "Installing dependencies (this takes several minutes)..."
venv/bin/pip install --upgrade pip

# --- English G2P model (en-core-web-sm): idempotent gate ---
# The one URL-pinned line in requirements-mac.txt makes pip re-download the
# 12.8MB wheel from GitHub on EVERY run (pip cannot trust an arbitrary URL's
# content to be unchanged), and pip's HTTP cache makes that re-download fail
# deterministically once a past run succeeded: the cached ETag turns the
# request conditional, GitHub redirects to a per-request signed URL, and the
# resulting 304 is written out as a 0-byte wheel (2026-08-11 finding; the
# 2026-08-05 "transient GitHub failure" was this bug). Same idempotent
# pattern as the model fetch steps below: install -r without that line, then
# fetch it separately only when missing/outdated - with --no-cache-dir so the
# poisoned cache is never consulted. The URL's single
# source of truth stays requirements-mac.txt (the line is extracted verbatim);
# the version below is kept in sync by tests/meta/test_installer_model_pin_sync.py.
EN_CORE_WEB_SM_VER="3.8.0"
MODEL_LINE="$(grep '^en-core-web-sm @ ' requirements-mac.txt || true)"
REQS_FILTERED="$(mktemp)"
grep -v '^en-core-web-sm @ ' requirements-mac.txt > "$REQS_FILTERED"
venv/bin/pip install -r "$REQS_FILTERED"
rm -f "$REQS_FILTERED"
if [ -n "$MODEL_LINE" ]; then
    if venv/bin/python -c "import sys; from importlib import metadata; sys.exit(0 if metadata.version('en-core-web-sm')=='$EN_CORE_WEB_SM_VER' else 1)" 2>/dev/null; then
        echo "English G2P model (en-core-web-sm $EN_CORE_WEB_SM_VER) already installed - skipping."
    else
        echo "Installing English G2P model (en-core-web-sm)..."
        venv/bin/pip install --no-deps --no-cache-dir "$MODEL_LINE"
    fi
fi

# --- SBV2 user voice model folder ---
# sbv2_models/ is where the user drops Style-Bert-VITS2 voice models. The app
# also creates it on first launch; creating it here keeps the "install, then
# put models in this folder" flow visible in Finder (before 2026-08-02 the old
# tts_models/ appeared at install time only as a side effect of the
# kokoro/bert fetch targets living inside it).
mkdir -p sbv2_models

# --- Kokoro TTS assets (English voices) ---
# Fixed model + 28 English voices into kokoro/ at the repo root (~350MB, first
# run only - the fetch is idempotent and skips files already present). Failure is
# non-fatal: the app runs without English local TTS; re-running this
# installer completes the fetch.
echo "Fetching Kokoro TTS assets - about 350MB on first run..."
if ! venv/bin/python -m audio_output.fetch_kokoro_models; then
    echo "WARNING: Kokoro TTS fetch failed. English local TTS will be"
    echo "unavailable until you re-run this installer with a working network."
fi

# --- Faster-Whisper STT model (voice input) ---
# Default local STT model (turbo, ~1.6GB) into the HuggingFace cache - the
# same location the app and the uninstaller use. Fetching here instead of on
# first voice-input use, where the silent download looks like a crash.
# Idempotent; failure is non-fatal (falls back to the old
# download-on-first-use behavior).
echo "Fetching Whisper STT model - about 1.6GB on first run..."
if ! venv/bin/python -m audio_input.fetch_whisper_model; then
    echo "WARNING: Whisper model fetch failed. It will be downloaded on"
    echo "first voice-input use instead."
fi

# --- Japanese BERT for SBV2 TTS ---
# SBV2 (ja) loads ku-nlp/deberta-v2-large-japanese-char-wwm at character
# activation. Fetching here (~1.3GB, first run only, into bert/ja at the repo
# root) instead of during the first character load, where the silent download froze
# the UI for about a minute on a clean OS (2026-08-01). Idempotent; failure
# is non-fatal (falls back to the old download-on-first-use behavior).
echo "Fetching Japanese BERT model - about 1.3GB on first run..."
if ! venv/bin/python -m audio_output.fetch_bert_model; then
    echo "WARNING: Japanese BERT fetch failed. It will be downloaded on the"
    echo "first Japanese character load instead."
fi

# --- OpenJTalk dictionary (Japanese g2p) ---
# pyopenjtalk otherwise downloads its dictionary (~23MB) during the first
# Japanese synthesis. Idempotent (verifies by actually loading it); failure
# is non-fatal.
echo "Fetching OpenJTalk dictionary - about 23MB on first run..."
if ! venv/bin/python -m audio_output.fetch_openjtalk_dict; then
    echo "WARNING: OpenJTalk dictionary fetch failed. It will be downloaded"
    echo "on the first Japanese synthesis instead."
fi

# --- tiktoken BPE (token counting) ---
# cl100k_base (~1.7MB) is fetched into the in-repo cache (.cache/tiktoken)
# so the first conversation turn never waits on a network fetch. Idempotent;
# failure is non-fatal (token counts fall back to estimation).
echo "Fetching tiktoken encoder - about 1.7MB on first run..."
if ! venv/bin/python -c "import sys; from backend.shared.token_manager import ensure_tokenizer_cached; sys.exit(0 if ensure_tokenizer_cached() else 1)"; then
    echo "WARNING: tiktoken fetch failed. Token counting will fall back to"
    echo "estimation until it can be downloaded."
fi

# --- Build "Artificial Girlfriend.app" (launcher applet) ---
# Why an applet: TCC permissions (microphone / screen recording / input
# monitoring / automation) attach to the RESPONSIBLE PROCESS. Launching the
# venv python from this applet — both on double-click and from the
# LaunchAgent (auto-start writes "/usr/bin/open -a <this app>") — keeps a
# single permission identity. The applet also exports LANG from the macOS
# locale so the UI language auto-detection works when launched outside a
# terminal (applet PATH/env is minimal).
#
# TCC接地事実(2026-07-23): TCC の照合は Bundle ID でなく csreq(コード署名
# 要件)で行われ、ad-hoc 署名の csreq は cdhash=ビルドごとに変わる。つまり
# アプレットを再ビルドすると付与済み権限は設定画面で「許可済み」に見えた
# まま全て無効化される(マイクは次回アクセスで再プロンプトが出て復活する
# が、入力監視/画面収録は再プロンプトが出ず沈黙する非対称に注意)。
# → ソース(AppleScript+ロゴ+手順版数)が変わらない限り再ビルドしない。
#   強制再ビルドしたいときは .app を削除してから実行する。
APP="Artificial Girlfriend.app"
PLIST="$APP/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
# .app icon = colour AG logo in the macOS rounded square (稜裁定 2026-08-16;
# the white silhouette Artificial_Girlfriend_Logo_Mac.png stays the MENU BAR
# icon — a colour icon looks out of place there, 07-22 ruling unchanged).
# The PNG is a 1024px derivative of Artificial_Girlfriend_Logo(High Scale).png:
# body 824px centred on a transparent canvas, corner radius 185 (Apple's
# macOS 11+ icon grid), made with PIL (LANCZOS + rounded_rectangle mask).
MAC_APP_ICON="app_images/Artificial_Girlfriend_Logo_MacApp.png"

# Notification relay removed (稜裁定 2026-08-16): the applet used to show
# tray banners under its own identity via a .tray_notify handoff (cb34ab3),
# but macOS silently dropped them on a rebuilt applet (no permission prompt,
# no System Settings entry) and the tray could not detect it. Tray
# notifications now go through osascript (Script Editor identity) — see
# launcher/tray_app.py TrayApp.notify. This source change rebuilds the .app
# once (fingerprint) = the TCC re-grant warning below fires one more time.
APPLET_TMPDIR="$(mktemp -d)"
APPLET_SRC="$APPLET_TMPDIR/applet.applescript"
cat > "$APPLET_SRC" <<'APPLESCRIPT'
on run
    set appPath to POSIX path of (path to me)
    set parentDir to do shell script "dirname " & quoted form of appPath
    set sh to "LOC=$(defaults read -g AppleLocale 2>/dev/null | cut -d'@' -f1); export LANG=\"${LOC:-en_US}.UTF-8\"; "
    set sh to sh & quoted form of (parentDir & "/venv/bin/python") & " " & quoted form of (parentDir & "/launcher/tray_app.py") & " --open-front > /dev/null 2>&1 &"
    do shell script sh
end run
APPLESCRIPT

# fingerprint = アプレットソース + アイコン + ビルド手順版数(手順を変えたら bump)
FINGERPRINT="$( { cat "$APPLET_SRC"; cat "$MAC_APP_ICON" 2>/dev/null; echo build-steps-v1; } \
    | shasum -a 256 | awk '{print $1}' )"
FP_FILE="$APP/Contents/Resources/.ag_applet_fingerprint"

if [ -d "$APP" ] && [ "$(cat "$FP_FILE" 2>/dev/null)" = "$FINGERPRINT" ]; then
    echo "Artificial Girlfriend.app is up to date — skipping rebuild (permissions preserved)."
else
    APPLET_EXISTED=0
    [ -d "$APP" ] && APPLET_EXISTED=1
    echo "Building Artificial Girlfriend.app..."
    rm -rf "$APP"
    osacompile -o "$APP" "$APPLET_SRC"

    # --- Bundle identity (稜実機 2026-07-22: permission dialogs said "applet") ---
    # osacompile names the executable "applet"; TCC dialogs and the Privacy &
    # Security lists show that name, which users cannot connect to AG. Rename
    # the binary + align every identity key. (Bundle ID alone does NOT keep
    # TCC grants across rebuilds — see the csreq note above; the fingerprint
    # skip is what actually preserves them.)
    if [ -f "$APP/Contents/MacOS/applet" ]; then
        mv "$APP/Contents/MacOS/applet" "$APP/Contents/MacOS/Artificial Girlfriend"
    fi
    $PB -c "Set :CFBundleExecutable Artificial Girlfriend" "$PLIST" 2>/dev/null || true
    $PB -c "Set :CFBundleName Artificial Girlfriend" "$PLIST" 2>/dev/null || \
        $PB -c "Add :CFBundleName string Artificial Girlfriend" "$PLIST" 2>/dev/null || true
    $PB -c "Set :CFBundleDisplayName Artificial Girlfriend" "$PLIST" 2>/dev/null || \
        $PB -c "Add :CFBundleDisplayName string Artificial Girlfriend" "$PLIST" 2>/dev/null || true
    $PB -c "Set :CFBundleIdentifier com.artificialgirlfriend.launcher" "$PLIST" 2>/dev/null || \
        $PB -c "Add :CFBundleIdentifier string com.artificialgirlfriend.launcher" "$PLIST" 2>/dev/null || true
    # CFBundleIconName points at the Assets.car default (scroll) icon and WINS
    # over the icns (M1切り分け 2026-07-22) — remove it so CFBundleIconFile /
    # applet.icns (replaced below) is what macOS actually renders.
    $PB -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true

    # --- App icon: colour AG logo, rounded square (稜裁定 2026-08-16) ---
    # CFBundleIconFile stays "applet"; only the icns content is replaced.
    # `sips -s format icns` cannot build a multi-size icns from a single
    # source (M1実測: 黙って失敗しデフォルトアイコンのまま) — resample an
    # iconset and compile it with iconutil instead (1024px source = 512@2x).
    if [ -f "$MAC_APP_ICON" ]; then
        ICONTMP="$(mktemp -d)"
        ICONSET="$ICONTMP/AppIcon.iconset"
        mkdir -p "$ICONSET"
        for size in 16 32 128 256 512; do
            sips -z "$size" "$size" "$MAC_APP_ICON" \
                --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1 || true
            sips -z "$((size * 2))" "$((size * 2))" "$MAC_APP_ICON" \
                --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
        done
        if iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns" 2>/dev/null; then
            touch "$APP"  # nudge Finder/LaunchServices to refresh the icon
            echo "App icon applied (colour AG logo)."
        else
            echo "WARN: app icon build failed (keeping the default applet icon)"
        fi
        rm -rf "$ICONTMP"
    fi

    # fingerprint は codesign より前に格納(署名対象に含める)
    printf %s "$FINGERPRINT" > "$FP_FILE"

    # Re-sign ad hoc: the renames above invalidate the original signature.
    codesign --force --deep --sign - "$APP" 2>/dev/null || \
        echo "WARN: ad-hoc codesign failed (the app may still run)"

    if [ "$APPLET_EXISTED" = 1 ]; then
        echo ""
        echo "*** IMPORTANT: the applet was REBUILT, so its macOS permission identity changed."
        echo "*** System Settings > Privacy & Security may still SHOW the old grants as ON,"
        echo "*** but they NO LONGER apply. For each of: Microphone / Input Monitoring /"
        echo "*** Screen Recording / Automation — remove 'Artificial Girlfriend' (−),"
        echo "*** then launch AG and approve the prompts again."
        echo "*** 再ビルドにより権限の実体が無効化されています。プライバシーとセキュリティの"
        echo "*** 各項目から Artificial Girlfriend を一度削除(−)し、AG起動後に再許可してください。"
    fi
fi
rm -rf "$APPLET_TMPDIR"

# --- MotionPNGPlayer (標準搭載 Electron 立ち絵プレイヤー) one-shot setup ---
# 標準搭載=確認プロンプトなしで自動セットアップ(稜裁定 2026-08-01・旧オプション扱いを廃止)。
# このインストーラ1本で完結させる(稜裁定 2026-07-24: Mac のインストーラは
# このファイルだけ=サーバー機の MotionPNGPlayer セットアップはここが持ち場)。
# MotionPNGPlayer/Install MotionPNGPlayer.command はクライアント端末への
# コピー用(2026-07-28 再導入・repo 内では実行拒否・applet ビルドは本節と
# 同期を保つこと)。Node.js/npm は不使用(2026-07-29 案B): Electron ランタイム
# zip を GitHub Releases から直接取得し node_modules/electron/dist へ展開
# (旧 npm レイアウトの動作部分集合 = dist/ + path.txt のみ)。判定を
# node_modules でなく実バイナリにすることで他OSコピーも自己修復される。
# 冒頭の arm64 ガード済みにつき darwin-arm64 固定。
MPP_EBIN="MotionPNGPlayer/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
# Asset/ is where users drop character motion folders. main.js creates it at
# first launch, but the installer does not auto-launch anything, so create it
# here - otherwise there is no folder to drop assets into until the first
# launch (gitignored = absent from fresh clones, 2026-07-30).
[ -d "MotionPNGPlayer" ] && mkdir -p "MotionPNGPlayer/Asset"
if [ -d "MotionPNGPlayer" ] && [ ! -x "$MPP_EBIN" ]; then
    echo ""
    echo "Setting up MotionPNGPlayer - downloads Electron, about 100 MB on first run..."
    (
        cd MotionPNGPlayer
        # バージョンの真実源は package.json。
        EVER="$(sed -n 's/.*"electron": "\([0-9.]*\)".*/\1/p' package.json)"
        if [ -z "$EVER" ]; then
            echo "ERROR: could not read the Electron version from package.json."
            exit 1
        fi
        # ダウンロード成功後に初めて既存 node_modules を消す
        # (失敗時に動作中インストールを壊さない)。
        EZIP="$(mktemp -t electron_zip)"
        echo "Downloading Electron v$EVER (darwin-arm64, about 100 MB)..."
        if ! curl -fL --retry 3 -o "$EZIP" "https://github.com/electron/electron/releases/download/v$EVER/electron-v$EVER-darwin-arm64.zip"; then
            echo "ERROR: download failed."
            rm -f "$EZIP"
            exit 1
        fi
        echo "Removing any existing node_modules..."
        rm -rf node_modules
        mkdir -p node_modules/electron/dist
        # ditto は Electron.app 内の symlink・実行ビットを保持する。
        ditto -xk "$EZIP" node_modules/electron/dist
        rm -f "$EZIP"
        printf 'Electron.app/Contents/MacOS/Electron\n' > node_modules/electron/path.txt
        echo -n "Electron: "
        "node_modules/electron/dist/Electron.app/Contents/MacOS/Electron" --version
        # 単体起動用アプレット: 実証済みレイアウト(8ヶ月運用の
        # osacompile 方式=Electron 実バイナリを detach 起動して
        # 即終了・トレイアイコンは Electron 自身の名義で描画)
        echo "Building MotionPNGPlayer.app..."
        rm -rf "MotionPNGPlayer.app"
        osacompile -o "MotionPNGPlayer.app" <<'MPP_APPLESCRIPT'
on run
    set appPath to POSIX path of (path to me)
    set parentDir to do shell script "dirname " & quoted form of appPath
    set electronPath to parentDir & "/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
    do shell script quoted form of electronPath & " " & quoted form of parentDir & " > /dev/null 2>&1 &"
end run
MPP_APPLESCRIPT
        MPP_PLIST="MotionPNGPlayer.app/Contents/Info.plist"
        $PB -c "Set :CFBundleName MotionPNGPlayer" "$MPP_PLIST" 2>/dev/null || \
            $PB -c "Add :CFBundleName string MotionPNGPlayer" "$MPP_PLIST" 2>/dev/null || true
        echo "MotionPNGPlayer setup complete."
    ) || echo "WARN: MotionPNGPlayer setup failed — re-run this installer to retry."
fi

echo ""
echo "== Done. Double-click 'Artificial Girlfriend.app' to start (menu bar icon). =="
echo "   First run: macOS will ask for permissions (named 'Artificial Girlfriend')"
echo "   — see the macOS section in README.md."
