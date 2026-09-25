"""
fetch_bert_model.py

SBV2(ja) が要求する日本語BERT (ku-nlp/deberta-v2-large-japanese-char-wwm)
を bert/ja/ (repo直下) へ取得する。インストーラが pip install の後に実行する
(初回のみ約1.3GBのダウンロード)。従来は初回キャラ選択時の同期DLになって
おり、クリーンOSで「キャラ読み込みに約1分」の正体だった(稜サブOSテスト
2026-08-01)。

- 冪等: 取得済みファイル(サイズ>0)はスキップ=再実行はほぼ一瞬
  (fetch_kokoro_models と同型。snapshot_download を使わないのは
  (a) pytorch_model.bin まで落として総量が倍(約2.6GB)になる
  (b) オンライン再実行で全ファイルのetag照会が走る、の2点を避けるため)。
- config.json は必ず最後に取得する: 受け側 _resolve_bert_model は
  config.json の存在だけでこのフォルダを採用しHFキャッシュへフォール
  バックしない。途中中断で config.json だけ残ると日本語合成が恒久的に
  壊れるため、重みより先に置かない。
- HF のグローバルキャッシュは使わず local_dir 直下へ置く: アンインストール
  がリポジトリフォルダ削除だけで痕跡ゼロになる(Kokoro と同じ思想)。
- パスの真実源は audio_output (DEFAULT_BERT_BASE_PATH / DEFAULT_BERT_MODELS)。

Usage:  venv\\Scripts\\python.exe -m audio_output.fetch_bert_model
        venv/bin/python -m audio_output.fetch_bert_model
"""

import os
import sys

from .audio_output import DEFAULT_BERT_BASE_PATH, DEFAULT_BERT_MODELS

# transformers が実際に要求するファイルのみ(HFキャッシュ実測 2026-08-01)。
# pytorch_model.bin(1.3GB) は safetensors と重複するため取得しない。
# tokenizer は BertJapaneseTokenizer(basic+character) で vocab.txt だけで動く
# = fugashi/MeCab/sentencepiece 不要。config.json は最後(モジュール先頭の
# コメント参照)。
BERT_FILES = [
    "model.safetensors",
    "tokenizer_config.json",
    "vocab.txt",
    "special_tokens_map.json",
    "config.json",
]


def main() -> int:
    from huggingface_hub import hf_hub_download

    lang = "ja"
    repo_id = DEFAULT_BERT_MODELS[lang]
    target_dir = os.path.join(DEFAULT_BERT_BASE_PATH, lang)
    fetched = skipped = 0
    for filename in BERT_FILES:
        target = os.path.join(target_dir, filename)
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            skipped += 1
            continue
        print(f"[BERT] fetching {filename} ...", flush=True)
        try:
            hf_hub_download(
                repo_id=repo_id, filename=filename, local_dir=target_dir
            )
            fetched += 1
        except Exception as e:
            print(f"[BERT] ERROR fetching {filename}: {e}", file=sys.stderr)
            print(
                "[BERT] Japanese BERT assets are incomplete - the first "
                "Japanese synthesis will fall back to downloading from "
                "HuggingFace. Re-run the installer (or this script) with a "
                "working network connection.",
                file=sys.stderr,
            )
            return 1
    print(f"[BERT] assets ready in {target_dir} "
          f"(fetched {fetched}, already present {skipped})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
