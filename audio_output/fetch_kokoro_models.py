"""
fetch_kokoro_models.py

Kokoro TTS の固定資材(モデル+英語28声)を kokoro/ (repo直下) へ取得する。
インストーラが pip install の後に実行する(初回のみ約350MBのダウンロード)。

- 冪等: 取得済みファイル(サイズ>0)はスキップ=再実行はほぼ一瞬。
- HF のグローバルキャッシュ(~/.cache/huggingface)は使わず local_dir 直下へ
  置く: アンインストールがリポジトリフォルダ削除だけで痕跡ゼロになる。
- パスの真実源は kokoro_engine (KOKORO_DIR / KOKORO_VOICES_DIR)。

Usage:  venv\\Scripts\\python.exe -m audio_output.fetch_kokoro_models
        venv/bin/python -m audio_output.fetch_kokoro_models
"""

import os
import sys

from .kokoro_engine import KOKORO_DIR, KOKORO_REPO_ID

# Kokoro-82M v1.0 の英語声の全数(2026-07-26 に HF リポジトリ実照会で確定。
# 接頭辞 af_/am_=米・bf_/bm_=英。日本語ほか他言語の声は仕様として同梱しない
# = ja は SBV2 の担当)。
EN_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


def main() -> int:
    from huggingface_hub import hf_hub_download

    # 非ASCIIインストールパス対策: espeak-ng データ(pip 同梱)の ASCII 複製をここで作る
    # (ASCII パスなら no-op)。HF 取得の成否と独立なので先に済ませる
    from .ascii_data_mirror import apply_espeak_data_path
    mirrored = apply_espeak_data_path()
    if mirrored is not None:
        print(f"[Kokoro] espeak-ng data mirrored to {mirrored} (non-ASCII install path)")

    files = ["config.json", "kokoro-v1_0.pth"] + [f"voices/{v}.pt" for v in EN_VOICES]
    fetched = skipped = 0
    for filename in files:
        target = os.path.join(KOKORO_DIR, *filename.split("/"))
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            skipped += 1
            continue
        print(f"[Kokoro] fetching {filename} ...", flush=True)
        try:
            hf_hub_download(
                repo_id=KOKORO_REPO_ID, filename=filename, local_dir=KOKORO_DIR
            )
            fetched += 1
        except Exception as e:
            print(f"[Kokoro] ERROR fetching {filename}: {e}", file=sys.stderr)
            print(
                "[Kokoro] English TTS assets are incomplete - re-run the "
                "installer (or this script) with a working network connection.",
                file=sys.stderr,
            )
            return 1
    print(f"[Kokoro] assets ready in {KOKORO_DIR} "
          f"(fetched {fetched}, already present {skipped})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
