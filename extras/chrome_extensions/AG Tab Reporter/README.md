# AG Tab Reporter

[English](#english) | [日本語](#日本語)

---

## English

A Chrome extension that provides Chrome tab information to **Artificial Girlfriend (AG)**.

AG's *PC Status* feature lets the AI character see what is currently open on your PC. Without this extension, AG can only see window titles (via the Windows API). With this extension installed, AG also receives the titles of all open Chrome tabs, so the character can talk about what you are browsing.

The extension is **optional** — AG works without it.

### Requirements

- Google Chrome (Manifest V3, Chrome 116 or later recommended)
- AG running **on the same PC** (this extension is for AG's local mode; it connects to `ws://localhost:5002`)

### Installation

1. Open `chrome://extensions` in Chrome
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**
4. Select this folder (`extras/chrome_extensions/AG Tab Reporter`)

No configuration is needed. The extension automatically connects to AG when AG is running, and reconnects automatically when AG restarts.

### Popup

Click the extension icon to check the connection status. The popup shows:

- Connection status (Connected / Disconnected)
- Server URL
- A **Reconnect** button
- A **language selector** (English / 日本語) — the choice is saved and restored

### Privacy

- The extension sends **tab titles only** (plus tab IDs and the active flag). Tab **URLs are never sent**.
- Tab titles are delivered to AG over a WebSocket on `localhost` (port 5002) and may be included in the prompt sent to the AI character's LLM provider when the PC Status feature is used.
- The port is bound to `localhost` only and is not reachable from other machines. Note that the connection is not authenticated: any local process could open the same port. This is the same trust model as other local AG components.

### Adding a language

1. Copy `_locales/en/messages.json` to `_locales/<lang>/messages.json` and translate the values
2. Add one line to `SUPPORTED_LANGUAGES` in `i18n.js`

---

## 日本語

**Artificial Girlfriend (AG)** にChromeタブ情報を提供するChrome拡張機能です。

AGの「PC Status」機能は、AIキャラクターがPC上で今開かれているものを把握できる機能です。この拡張機能がない場合、AGはウィンドウタイトル（Windows API経由）しか見えません。導入すると、開いている全Chromeタブのタイトルも送られるようになり、キャラクターが閲覧中の内容について話せるようになります。

この拡張機能は**任意**です。なくてもAGは動作します。

### 動作条件

- Google Chrome（Manifest V3・Chrome 116以降推奨）
- AG本体が**同じPC上で**動作していること（ローカルモード用。接続先は `ws://localhost:5002`）

### 導入手順

1. Chromeで `chrome://extensions` を開く
2. 右上の**デベロッパーモード**をONにする
3. **パッケージ化されていない拡張機能を読み込む**をクリック
4. このフォルダ（`extras/chrome_extensions/AG Tab Reporter`）を選択

設定は不要です。AGが起動していれば自動で接続し、AGを再起動しても自動で再接続します。

### ポップアップ

拡張機能アイコンをクリックすると接続状態を確認できます:

- 接続状態（接続済み / 未接続）
- サーバーURL
- **再接続**ボタン
- **言語セレクタ**（English / 日本語）— 選択は保存され次回も維持されます

### プライバシー

- 送信するのは**タブのタイトルのみ**（＋タブID・アクティブ状態）。タブの**URLは送信しません**。
- タブタイトルは `localhost`（ポート5002）のWebSocketでAGに渡され、PC Status機能の使用時にAIキャラクターのLLMプロバイダへ送るプロンプトに含まれることがあります。
- ポートは `localhost` のみにバインドされ、他のマシンからは到達できません。ただし接続に認証はなく、同一PC上のプロセスなら同じポートを開けます（他のAGローカルコンポーネントと同じ信頼モデルです）。

### 言語の追加方法

1. `_locales/en/messages.json` を `_locales/<言語コード>/messages.json` にコピーして訳文を書く
2. `i18n.js` の `SUPPORTED_LANGUAGES` に1行追加する
