"""
backend/youtube/get_token.py

Stage-A CLI for YouTube OAuth authorization — a thin wrapper over
backend/youtube/auth.py (spec §2). The stage-B UI calls the same auth.py.

Usage:
    venv/Scripts/python.exe -m backend.youtube.get_token [client_secret.json のパス]

- 初回はダウンロードした client_secret.json のパスを渡す
  （character_data/youtube/ へコピーされる。以後は引数なしで再認可できる）。
- ブラウザは自動で開かない（複数プロファイル環境で誤ったプロファイルが
  開くため・2026-07-12変更）。表示されたURLを「返信に使うチャンネルに
  ログインしているブラウザ/プロファイル」で開き、Googleアカウント選択の
  あと「投稿者にしたいチャンネル」（例: キャラ用チャンネル）を選ぶこと。
  ここで選んだチャンネル名義で返信が投稿される。
"""

import sys

from backend.youtube import auth


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv:
        result = auth.import_client_secret(argv[0])
        if not result["success"]:
            print(f"client_secret.json の取り込みに失敗: {result['error']}")
            return 1
        print(f"client_secret.json を {auth.CLIENT_SECRET_FILE} へコピーしました。")
    elif not auth.has_client_secret():
        print("client_secret.json が見つかりません。")
        print("使い方: python -m backend.youtube.get_token <client_secret.jsonのパス>")
        return 1

    def _print_url(url: str) -> None:
        print()
        print("以下のURLを、返信に使うチャンネルにログインしているブラウザ")
        print("（プロファイル）で開いて認可してください。チャンネル選択画面では")
        print("投稿者にするチャンネルを選ぶこと（選んだ名義で返信が投稿されます）:")
        print()
        print(url)
        print()
        print("認可の完了を待っています...")

    result = auth.run_authorization_flow(url_callback=_print_url)
    if not result["success"]:
        print(f"認可に失敗しました: {result['error']}")
        return 1

    title = result.get("channel_title") or "(チャンネル名の取得に失敗)"
    cid = result.get("channel_id") or "?"
    print("認可が完了しました。")
    print(f"認証済みチャンネル: {title} ({cid})")
    print(f"トークン保存先: {auth.TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
