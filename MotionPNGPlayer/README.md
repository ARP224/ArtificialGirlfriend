# MotionPNGPlayer

**English** | [日本語](#日本語)

The desktop player of **Artificial Girlfriend (AG)**: it shows a looping video
of your character in motion (a *motion asset*) on the desktop and moves her
lips in time with her voice.

**About this file**: to use MotionPNGPlayer on a client device in server mode,
you copy this folder to that device (it is a copy — the folder inside AG stays
as it is). The copy does not bring AG's own documentation with it, so this
short README lives in the folder to give you the essentials there. For
everything else — how to use the player, server mode, and AG itself — see the
main README on the AG repository page:
https://github.com/ARP224/ArtificialGirlfriend

- **Local mode** (the same PC as AG): AG starts the player itself when you
  press **Appear** in the desktop UI.
- **Server mode** (a client device): the player sits in the tray and connects
  to the AG server over Tailscale.

> This app is a derivative of [MotionPNGTuber](https://github.com/rotejin/MotionPNGTuber)
> by rotejin (MIT License).

## Motion assets

Motion folders go under `Asset/<folder name>/` inside this folder — the same
place in both modes. Create them with
[MotionPNGCreator-for-ArtificialGirlfriend](https://github.com/ARP224/MotionPNGCreator-for-ArtificialGirlfriend).

- The folder name is what you pick as the character's **Motion Folder Name**
  in AG. Japanese folder names work on both Windows and macOS.
- When carrying assets from a Mac to Windows, copy them as plain folders (via
  a shared folder, OneDrive, USB, etc.), not as a zip — a zip made with
  Finder's "Compress" carries no character-encoding flag for file names, so
  Windows garbles Japanese folder names when extracting it.

## Setup

**On the same PC as AG (local mode)**: nothing to do. AG's own installer sets
the player up, and **Appear** launches it.

**On a client device (server mode)**:

1. Copy this folder to the client device. On Windows, avoid OneDrive-synced
   locations (Desktop/Documents may be redirected there) — the installer
   creates many `node_modules` files that OneDrive would sync, and files
   later dehydrated to online-only break the app. Make sure `Asset/` holds
   the Motion folders you use: the server sends only the folder name, so the
   client needs the same folders.
2. Run the installer — no prerequisites; it downloads the Electron runtime
   (about 100 MB) from GitHub, so an internet connection is all you need
   (on Windows 10, version 1803 or later):
   - **Windows**: double-click `Install MotionPNGPlayer.bat`
   - **macOS**: double-click `Install MotionPNGPlayer.command`
     (if Finder refuses: `bash "Install MotionPNGPlayer.command"`)

   The installer refuses to run on a network drive or file server — copy the
   folder to the device's local disk first.
3. Start:
   - **Windows**: `MotionPNGPlayer.bat` — the icon appears in the task tray
     (it may hide behind the "hidden icons" chevron).
   - **macOS**: double-click **MotionPNGPlayer.app** (built by the installer)
     — the icon appears in the menu bar.
   - Auto-start at sign-in is **ON by default** (registered by the installer;
     toggle it from the tray icon menu, **Start at sign-in**).
4. Open **Open Settings…** from the tray icon menu, enter the **Server URL**
   (the Tailscale hostname of the AG host — the same one you open AG at in
   the browser) and the **Server Port** (default 7860), check with
   **Test Connection**, then **Save**. The player's **Language** is chosen
   here too (in local mode it follows AG's UI language).

Inside the AG program folder itself this installer refuses to run — there,
AG's own installer owns the setup.

## Uninstall (client copies only)

- **Windows**: double-click `Uninstall MotionPNGPlayer.bat`
- **macOS**: double-click `Uninstall MotionPNGPlayer.command`
  (if Finder refuses: `bash "Uninstall MotionPNGPlayer.command"`)

It asks you to type `Uninstall` to confirm, stops the running player, then
removes this folder (`node_modules`, the Mac `.app` and **all motion assets in
`Asset/`**), the player settings, the Electron download cache and the
auto-start registration.
Inside the AG program folder it refuses to run — there AG's own uninstaller
removes the player together with AG. On a network drive it refuses as well:
a copy there holds no settings on this device, so simply delete the folder.

## License

[MIT](LICENSE)

---

# 日本語

**Artificial Girlfriend (AG)** のデスクトッププレイヤーです。キャラクターが動いて
いるループ動画（モーション素材）をデスクトップに表示して、声に合わせて口を
動かします。

**このファイルについて**: サーバーモードで MotionPNGPlayer をクライアント端末で
使うには、このフォルダをクライアント端末にコピーします（複製なので、AG本体側の
フォルダはそのまま残ります）。コピー先には AG本体の説明が付いてこないため、
最低限のことをまとめた簡易版の README をこのフォルダに入れてあります。使い方の
詳しい説明（プレイヤーの操作・サーバーモード・AG本体のこと）は、GitHub のリポジトリ
ページにある AG本体の README を見てください（ページ上部の「日本語」で日本語版に
切り替わります）:
https://github.com/ARP224/ArtificialGirlfriend

- **ローカルモード**（AGと同じPC）: デスクトップUIの「Appear」を押すと、
  AGがプレイヤーを起動します。
- **サーバーモード**（クライアント端末）: プレイヤーがトレイに常駐し、
  Tailscale経由でAGサーバーに接続します。

> 本アプリは rotejin 氏の [MotionPNGTuber](https://github.com/rotejin/MotionPNGTuber)
> (MIT License) を基にした派生版です。

## モーション素材

Motionフォルダは、このフォルダ内の `Asset/<フォルダ名>/` に置きます（どちらの
モードでも同じ場所です）。素材の作成は
[MotionPNGCreator-for-ArtificialGirlfriend](https://github.com/ARP224/MotionPNGCreator-for-ArtificialGirlfriend)
で行います。

- フォルダ名が、AGでキャラクターに設定する「Motionフォルダ名」になります。
  日本語のフォルダ名も Windows / Mac とも使えます。
- Mac から Windows へ素材を渡すときは、zip にせずフォルダのまま（共有フォルダ・
  OneDrive・USB 等で）コピーしてください — Finder の「圧縮」で作った zip は
  ファイル名の文字コード情報を持たないため、Windows で展開すると日本語の
  フォルダ名が文字化けします。

## セットアップ

**AGと同じPCで使う場合（ローカルモード）**: 何もする必要はありません。
AG本体のインストーラーがプレイヤーもセットアップし、「Appear」で起動します。

**クライアント端末で使う場合（サーバーモード）**:

1. このフォルダをクライアント端末にコピーします。Windows では OneDrive 同期下
   （デスクトップ／ドキュメントがリダイレクトされていることがあります）を
   避けてください — インストーラーが作る `node_modules` の大量ファイルが同期
   対象になり、後日「オンラインのみ」化されると起動できなくなります。
   `Asset/` に使う Motionフォルダが入っていることを確認してください —
   サーバーからはフォルダ名だけが送られるので、クライアント側にも同じ
   フォルダが必要です。
2. インストーラーを実行します（事前準備は不要 — Electron ランタイム約100MBを
   GitHub から自動ダウンロードするため、ネット接続だけ必要です。Windows 10 は
   バージョン1803以降）:
   - **Windows**: `Install MotionPNGPlayer.bat` をダブルクリック
   - **macOS**: `Install MotionPNGPlayer.command` をダブルクリック
     （Finderに拒否されたら: `bash "Install MotionPNGPlayer.command"`）

   ネットワークドライブやファイルサーバー上では実行を拒否します — 先に端末の
   ローカルディスクへコピーしてください。
3. 起動:
   - **Windows**: `MotionPNGPlayer.bat` — タスクトレイにアイコンが出ます
     （「隠れているインジケーター」側に入ることがあります）。
   - **macOS**: インストーラーが生成した **MotionPNGPlayer.app** をダブル
     クリック — メニューバーにアイコンが出ます。
   - ログイン時の自動起動は**既定でON**です（インストーラーが登録します。
     トレイアイコンのメニュー「ログイン時に自動起動」で切替可）。
4. トレイアイコンのメニュー「設定を開く…」で、「サーバーURL」（AGをホストして
   いるPCの Tailscale ホスト名 — ブラウザでAGを開くときと同じもの）と
   「サーバーポート」（既定 7860）を入力し、「接続テスト」で確かめてから
   「保存」します。プレイヤーの「言語」もここで選べます（ローカルモードでは
   AGのUI言語に追従します）。

AG本体のプログラムフォルダ内では、このインストーラーは実行を拒否します
（そこでは AG本体のインストーラーが持ち場のため）。

## アンインストール（クライアント端末のコピーのみ）

- **Windows**: `Uninstall MotionPNGPlayer.bat` をダブルクリック
- **macOS**: `Uninstall MotionPNGPlayer.command` をダブルクリック
  （Finderに拒否されたら: `bash "Uninstall MotionPNGPlayer.command"`）

確認のため `Uninstall` と入力すると、起動中のプレイヤーを停止した上で、
このフォルダ（`node_modules`・Mac の `.app`・**`Asset/` のモーション素材を
含む**）、プレイヤーの設定、Electron のダウンロードキャッシュ、ログイン時
自動起動の登録を削除します。
AG本体のプログラムフォルダ内では実行を拒否します — そこでは AG本体の
アンインストーラーが本体ごと削除します。ネットワークドライブ上でも拒否します —
そこにあるコピーはこの端末に設定を持たないので、フォルダを消すだけで済みます。

## License

[MIT](LICENSE)
