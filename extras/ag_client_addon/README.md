# AG Client Addon

**English** | [日本語](#日本語)

A tray / menu-bar companion app for **Artificial Girlfriend (AG) server mode**.
Run it on the *client* machine (the one you open AG in a browser on) to get
**global hotkeys for starting / stopping voice recording** — even when the
browser is not focused (or is in your backpack).

It replaces the former "AG Key Listener" Chrome extension: it works with any
browser, needs no developer-mode extension loading, and — because it uses
OS-level hotkey registration (Electron `globalShortcut`) — it receives **only
the key combos you configure. It does not observe or log any other
keystrokes**, and it needs no macOS Accessibility / Input Monitoring
permission.

## How it works

```
global hotkey → AG Client Addon → WebSocket → AG server
             → AG validates state → your AG browser page clicks
               the record button (all existing safeguards apply)
```

The addon only sends `hotkey_start/stop/toggle_recording` messages; audio is
still captured by your browser page as usual. An AG page must be open in a
browser on this machine (or anywhere — the server broadcasts the click to the
connected desktop page).

## Setup

1. Copy this folder to the client machine. On Windows, avoid OneDrive-synced
   locations (Desktop/Documents may be redirected there) — the installer
   creates many `node_modules` files that OneDrive would sync, and
   files later dehydrated to online-only break the app.
2. Run the installer — no prerequisites; it downloads the Electron runtime
   (about 100 MB) from GitHub, so an internet connection is all you need:
   - **Windows**: double-click `Install AG Client Addon.bat`
   - **macOS**: double-click `Install AG Client Addon.command`
     (if Finder refuses: `bash "Install AG Client Addon.command"`)
3. Start:
   - **Windows**: `AG Client Addon.bat` — the icon appears in the task
     tray (it may hide behind the "hidden icons" chevron).
   - **macOS**: double-click **AG Client Addon.app** (built by the installer)
     — the icon appears in the menu bar.
   - Auto-start at sign-in is **ON by default** (registered by the
     installer; toggle it from the tray icon menu, "Start at sign-in").

## Settings

Open the settings window from the tray icon (double-click on Windows, or
tray menu → "Open Settings…"):

- **AG server URL** — the same URL you open AG with in the browser
  (e.g. `https://<tailscale-host>:7860`). `ws://` / `wss://` URLs are also
  accepted as-is.
- **Enable AG link** — master switch. OFF stops the connection, all
  reconnect attempts, and the hotkeys. The **Connect** button next to Save
  connects immediately (skips the retry backoff of 5→15→30→60 s).
  A 30-second keepalive ping detects silently dead links (mobile
  tethering / NAT), so the status display stays truthful.
- **Start / Stop recording key** — click the field and press a combo.
  **Set the same combo on both to get toggle behaviour** (default: `Ctrl+1`).
- **Language** — UI language (English / 日本語). Adding a language = adding
  one `locales/<code>.json` file (its `_name` key is the dropdown label).

The log area at the bottom shows what the server actually did for each key
press (executed / rejected and why) — useful when the browser page is not
visible.

## Uninstall

Run the uninstaller **in the copied folder on the client machine**:

- **Windows**: double-click `Uninstall AG Client Addon.bat`
- **macOS**: double-click `Uninstall AG Client Addon.command`
  (if Finder refuses: `bash "Uninstall AG Client Addon.command"`)

It stops the running addon, then removes this folder (including
`node_modules` and the Mac `.app`), the addon settings, the Electron
download cache (leftover of the old npm-based setup) and the auto-start
registration.
Inside the AG program folder itself it refuses to run — there the addon
is removed together with AG by AG's own uninstaller.

## Notes

- If a combo fails to register, another app owns it; the settings window
  tells you which one failed. Pick a different combo.
- If you run the addon **on the AG server machine itself**, note that AG's
  built-in local hotkeys (Ctrl+1 / Ctrl+0 via pynput) are separate — use
  different combos to avoid double-triggering.

---

# 日本語

**Artificial Girlfriend (AG) サーバーモード**用のトレイ／メニューバー常駐アプリです。
AGをブラウザで開く側（**クライアント側**）のマシンで動かすと、ブラウザが
非フォーカスでも（リュックの中でも）**グローバルホットキーで音声入力の
開始・停止**ができます。

旧「AG Key Listener」Chrome拡張の置き換えです: どのブラウザでも動き、
デベロッパーモードでの拡張読み込みが不要になりました。OSのホットキー
登録API（Electron `globalShortcut`）を使うため、**受け取るのは設定した
キーコンボだけ**です。他のキー入力を監視・記録することはなく、macOSの
アクセシビリティ／入力監視の許可も不要です。

## 仕組み

```
グローバルホットキー → AG Client Addon → WebSocket → AGサーバー
   → AGが状態を検証 → ブラウザのAGページが録音ボタンをクリック
     （既存の安全装置はすべてそのまま通ります）
```

Addonが送るのは `hotkey_start/stop/toggle_recording` メッセージだけで、
音声の録音自体はこれまでどおりブラウザのAGページが行います。

## セットアップ

1. このフォルダをクライアント側マシンにコピー。Windows では OneDrive
   同期下（デスクトップ/ドキュメントがリダイレクトされていることがある）
   を避ける — インストーラーが作る `node_modules` の大量ファイルが同期
   対象になり、後日「オンラインのみ」化されると起動できなくなる。
2. インストーラを実行（事前準備は不要 — Electron ランタイム約100MBを
   GitHub から自動ダウンロードするため、ネット接続だけ必要）:
   - **Windows**: `Install AG Client Addon.bat` をダブルクリック
   - **macOS**: `Install AG Client Addon.command` をダブルクリック
     （Finderに拒否されたら: `bash "Install AG Client Addon.command"`）
3. 起動:
   - **Windows**: `AG Client Addon.bat` — タスクトレイにアイコンが
     出ます（「隠れているインジケーター」側に入ることがあります）。
   - **macOS**: インストーラが生成した **AG Client Addon.app** をダブル
     クリック — メニューバーにアイコンが出ます。
   - ログイン時の自動起動は**既定でON**です（インストーラが登録。
     トレイアイコンのメニュー「ログイン時に自動起動」で切替可）。

## 設定

トレイアイコンから設定ウィンドウを開きます（Windowsはダブルクリック、
またはメニューの「設定を開く…」）:

- **AGサーバーのURL** — ブラウザでAGを開くときと同じURL
  （例: `https://<Tailscaleホスト>:7860`）。`ws://`・`wss://` の直接指定も可。
- **AG連携を有効にする** — マスタースイッチ。OFFで接続・再接続試行・
  ホットキーをすべて停止します。保存ボタン横の**「接続」ボタン**は
  再接続の待ち時間（5→15→30→60秒バックオフ）を飛ばして即接続します。
  30秒ごとのkeepaliveで無言切断（テザリング/NAT）を検知するので、
  接続状態表示は実態とズレません。
- **録音開始キー／停止キー** — 欄をクリックしてキーを押すと設定されます。
  **両方に同じキーを設定するとトグル動作**になります（初期値: `Ctrl+1`）。
- **言語** — UI言語（English / 日本語）。言語の追加は
  `locales/<コード>.json` を1ファイル置くだけです（`_name` キーが表示名）。

下部のログ欄には、キーを押した結果サーバーが実際に何をしたか
（実行された／却下された理由）が表示されます。ブラウザ画面が見えない
運用のときの確認に便利です。

## アンインストール

**クライアント側マシンにコピーしたフォルダ内**のアンインストーラを実行します:

- **Windows**: `Uninstall AG Client Addon.bat` をダブルクリック
- **macOS**: `Uninstall AG Client Addon.command` をダブルクリック
  （Finderに拒否されたら: `bash "Uninstall AG Client Addon.command"`）

起動中のAddonを停止した上で、このフォルダ（`node_modules`・Macの
`.app` 含む）、Addonの設定、Electronのダウンロードキャッシュ
（旧npm方式の残置物）、ログイン時自動起動の登録を削除します。
AG本体のプログラムフォルダ内では実行を拒否します — そこではAddonは
AG本体のアンインストーラで本体ごと削除されます。

## 補足

- キー登録に失敗する場合は他のアプリがそのコンボを使用しています。
  設定画面にどのキーが失敗したか表示されるので、別のコンボを選んで
  ください。
- **AGサーバー機自身**でAddonを使う場合、AG本体のローカルホットキー
  （pynputのCtrl+1／Ctrl+0）とは別系統です。二重発火を避けるため、
  重複しないキーを割り当ててください。
