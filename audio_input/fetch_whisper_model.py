"""
fetch_whisper_model.py

Faster-Whisper のローカルSTTモデル(既定=turbo・約1.6GB)を事前取得する。
インストーラが pip install の後に実行する(稜裁定 2026-07-30: 初回STT利用時の
無応答ダウンロード=クラッシュに見える問題の前倒し解消・無条件取得)。

- 取得先はHFグローバルキャッシュ: 実行時の _cached_model_path /
  ensure_model_loaded と同一の解決先で、アンインストーラーの削除
  インベントリ(faster-whisper系キャッシュ掃除)にも含まれている。
  Kokoro(local_dir直下)と方式が違うのは実行時側が既にHFキャッシュ前提のため。
- 冪等: キャッシュ完備なら local_files_only 照会だけで即終了。
- モデルサイズは user_settings.json の audio.stt_local_model に追従
  (未設定=既定 'turbo')。既定以外のサイズへの実行時切替は従来通り
  設定ページ側でオンデマンド取得+進捗表示。

Usage:  venv\\Scripts\\python.exe -m audio_input.fetch_whisper_model
        venv/bin/python -m audio_input.fetch_whisper_model
"""

import sys


def main() -> int:
    # settings_store は stdlib-only の共有葉(設定ディレクトリは無ければ作る)
    from backend.shared.settings_store import get_setting
    from faster_whisper.utils import download_model

    model_size = get_setting("audio", "stt_local_model", "turbo") or "turbo"

    try:
        path = download_model(model_size, local_files_only=True)
        print(f"[Whisper] model '{model_size}' already cached at {path}")
        return 0
    except Exception:
        pass  # 未キャッシュ(LocalEntryNotFoundError等) → オンライン取得へ

    print(
        f"[Whisper] fetching STT model '{model_size}' "
        "(about 1.6GB for the default 'turbo') ...",
        flush=True,
    )
    try:
        path = download_model(model_size)
    except Exception as e:
        print(f"[Whisper] ERROR fetching model '{model_size}': {e}", file=sys.stderr)
        print(
            "[Whisper] The model will be downloaded on first voice-input use "
            "instead - or re-run the installer (or this script) with a "
            "working network connection.",
            file=sys.stderr,
        )
        return 1
    print(f"[Whisper] model '{model_size}' ready at {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
