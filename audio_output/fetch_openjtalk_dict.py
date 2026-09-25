"""
fetch_openjtalk_dict.py

pyopenjtalk の OpenJTalk 辞書(DL約23MB・展開後約103MB)を事前取得する。
辞書は wheel 非同梱で、従来は初回の日本語音声合成中に暗黙ダウンロード
されていた(クリーンOSの初回体験を遅くする一因)。保存先は venv 内の
pyopenjtalk パッケージディレクトリ=アンインストール(リポジトリ削除)で
痕跡も消える。

- 冪等: 取得済みなら g2p 1回の実読み込み検証だけで即終了。
- 判定は「ディレクトリ存在」ではなく g2p("あ") の実行まで行う:
  pyopenjtalk 自身の判定はディレクトリ存在のみで、展開途中の残骸が
  あると永久に再取得されず sys.dic 不在で落ちる罠があるため。

Usage:  venv\\Scripts\\python.exe -m audio_output.fetch_openjtalk_dict
        venv/bin/python -m audio_output.fetch_openjtalk_dict
"""

import sys


def main() -> int:
    import pyopenjtalk

    # 非ASCIIインストールパス対策: 辞書を ASCII 複製へ向ける(未取得なら先に DL・
    # ASCII パスなら no-op)。初回の日本語合成中に 103MB のコピーが走らないようここで作る
    from .ascii_data_mirror import prepare_openjtalk_dict
    prepare_openjtalk_dict()

    dict_dir = pyopenjtalk.OPEN_JTALK_DICT_DIR
    if isinstance(dict_dir, bytes):
        dict_dir = dict_dir.decode("utf-8", errors="replace")
    print("[OpenJTalk] verifying dictionary (downloads about 23MB "
          "on first run) ...", flush=True)
    try:
        # 未取得なら _lazy_init がDL→展開まで行い、そのまま実読み込みを検証
        pyopenjtalk.g2p("あ")
    except Exception as e:
        print(f"[OpenJTalk] ERROR: {e}", file=sys.stderr)
        print(
            "[OpenJTalk] dictionary is missing or unusable - the first "
            "Japanese synthesis will retry the download. Re-run the "
            "installer (or this script) with a working network connection.",
            file=sys.stderr,
        )
        return 1
    print(f"[OpenJTalk] dictionary ready in {dict_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
