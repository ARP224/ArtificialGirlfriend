# fonts/ — 同梱フォント

UIの本文フォント（Gradioウィジェット部分）を外部通信なしで配信するための同梱フォント。
AGは「必然性のない外部通信を混入させない」方針のため、Google Fonts等のCDN参照は使わず、
ここにあるファイルを `ui/local_fonts.py` が base64 data URI として @font-face 埋め込みで配信する。

## 出所（無改変コピー）

- 書体: **Source Sans 3**（Adobe・旧名 Source Sans Pro の後継）
- 取得元: https://github.com/adobe-fonts/source-sans — リリース **3.052R** の
  `WOFF2-source-sans-3.052R.zip` 内 `WOFF2/TTF/` より抽出
- ファイルは**一切改変していない**（サブセット化・再圧縮・改名等なし。リリースzip内のファイル名のまま）

| ファイル | ウェイト | SHA-256 (先頭16桁) |
|---|---|---|
| `SourceSans3-Regular.ttf.woff2` | 400 | `53492fb3a0def773` |
| `SourceSans3-Semibold.ttf.woff2` | 600 | `47b9b661b9f395fe` |

## ライセンス

SIL Open Font License 1.1。全文と著作権表示・Reserved Font Name 宣言は `OFL.txt`
（リリースタグ 3.052R の `LICENSE.md` の逐語コピー）を参照。
無改変再配布のため OFL の改名義務（RFN 条項）は発生しない。単体販売は不可。
