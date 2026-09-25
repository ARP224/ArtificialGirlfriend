<p align="right">
  <strong>日本語</strong> | <a href="./CONTRIBUTING.md">English</a>
</p>

# 開発に参加する

Artificial Girlfriend に興味を持ってくれてありがとうございます。バグ報告・機能の要望・Pull Request、どれも歓迎です。**日本語・英語どちらでも構いません。**

個人プロジェクトのため、返答やレビューが遅れることがあります。気長に待ってもらえると助かります。

## 報告・相談の場所

- **[Issues](https://github.com/ARP224/ArtificialGirlfriend/issues)** — バグ報告と機能の要望はこちらへ。テンプレートを用意していますが、項目は目安です。書ける範囲で自由に書いてください
- **[Discussions](https://github.com/ARP224/ArtificialGirlfriend/discussions)** — 使い方の質問・雑談・アイデアの相談はこちらへ

バグ報告に、OS（Windows / Mac）・モード（ローカル / サーバー）・何をしたら何が起きたか・「📋 System Logs」の表示か `logs/app.log` の該当部分を添えてもらえると、調査が速く進みます。**会話内容などの私的な部分は伏せてから貼ってください。**

## Pull Request の方針

- **小さな修正（typoや明らかなバグの修正など）は、そのまま送ってもらって大丈夫です**
- **大きな変更は、先にIssueで相談してください。** 方向性が合わないまま作業してもらうと、お互いの時間がもったいないので
- このリポジトリの `master` は配信チャネルです。ユーザーは `git pull` で直接 `master` を受け取るため、**マージはそのままリリースになります**。取り込みは慎重に行います
- 挙動に触る変更は、自動テストとAIコードレビューに加えて、メンテナが実際にアプリを動かして確認します。そのぶん取り込みまで時間がかかることがあります。**「この操作をすると、こうなるはず」という確認手順を一言添えてもらえると、確認が速く進みます**

## 送る前のチェック

開発環境は、READMEのインストール手順で作った環境がそのまま使えます（特別なセットアップはありません）。そのうえで:

- テストを実行する（APIキー不要・オフラインで1分かからず完走します）:
  - Windows: `venv\Scripts\python.exe -m pytest tests/`
  - macOS: `venv/bin/python -m pytest tests/`
  - 成否の判定は**ドットの並びと終了コード0**です（環境によって最終サマリ行が表示されないことがありますが、正常です）
- 触ったファイルにlintをかける:
  - Windows: `venv\Scripts\python.exe -m ruff check <paths>`
  - macOS: `venv/bin/python -m ruff check <paths>`
- `git config core.autocrlf true` を**設定しないでください**。このリポジトリは意図的にファイルごとの行末コード（CRLF / LF）を維持しています。正規化すると差分が行末変更で埋まり、レビューも履歴も読めなくなります。エディタの自動正規化もオフにしてください

## コードの決まりごと

最低限、次の4つを守ってもらえれば大丈夫です。

### 1. 層は上から下へだけ依存する

```
ui/                                            UI層（Gradio合成根 app.py＋ハンドラ）
backend/backend.py, conversation_manager.py    アプリ層（公開API境界＋会話の進行役）
backend/server/                                通信層（WebSocket・セッション）
backend/llm|conversation|memory|tools|elyth/   ドメイン層（横並び）
backend/shared/                                共有層（設定・定数・状態）
```

import は上の層から下の層への一方向だけです。逆向き（例: `backend/` から `ui/` を import する）は書かないでください。

### 2. 新しいコードの置き場所

- 新しいUIハンドラ → `ui/handlers/`（`ui/app.py` は配線だけの場所。ロジックを書かない）
- 新しいドメインロジック・ツール → `backend/llm|conversation|memory|tools|elyth/` の該当パッケージ
- WebSocket・通信まわり → `backend/server/`
- 共有の設定・定数・状態 → `backend/shared/`

### 3. goldenテストはbyte単位の契約

`tests/golden/` は、LLMに送られるプロンプトやツール定義のbyte単位のスナップショットです。ここは1文字の差でもキャラクターの挙動が変わりうる場所なので、byte単位で固定しています。

goldenテストが赤くなったら、それは「説明が必要な変更」です。差分が意図どおりであることをPRで説明してください。説明のないまま `UPDATE_GOLDEN=1` でスナップショットを再生成してテストを緑にする、はNGです。

### 4. 構造の変更と挙動の変更を混ぜない

ファイルの移動・リネーム・分割と、動作を変える修正は、別のコミットに分けてください。混ざっていると、あとから「どのコミットで挙動が変わったのか」を追えなくなります。

## 貢献のライセンス

Pull Requestを送った時点で、あなたの貢献はこのプロジェクトと同じ **GNU Affero General Public License v3**（`LICENSE` 参照）で提供されることに同意したものとします。著作権はあなた自身に残ります。CLA（貢献者ライセンス同意書）はありません。

一部のディレクトリは別ライセンスです。`MotionPNGPlayer/`・`extras/ag_client_addon/`・`extras/chrome_extensions/AG Tab Reporter/` はMITで、そこへの貢献はそのディレクトリのライセンスで受け付けます。`nircmd-x64/` はサードパーティのフリーウェアで、改変できません。詳細は `LICENSE` を参照してください。
