"""
token_manager.py

Token management module for handling prompt size limits based on model context windows.

方式はAPI/Ollama統一(2026-08-11 稜裁定): 「上限+削減優先順位」の
APIPromptTokenManager 一本。旧固定セクション配分(PromptTokenManager/
TokenBudget)は撤去済み。Ollamaは num_ctx から生成余白(num_predict)と
ツール定義(ペイロードのtoolsはOllamaがテンプレートでプロンプトへ練り込む
=このマネージャに見えない消費)を reserved_tokens として差し引く。
"""

import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple, Any

logger = logging.getLogger(__name__)

# tiktoken遅延初期化（モジュールレベルでキャッシュ）
_tiktoken_encoding = None

# cl100k_base のBPE定義(約1.7MB)はライブラリ非同梱で、既定キャッシュはOSの
# 一時領域(%TEMP%/data-gym-cache)。クリーンOSでは初回会話ターン中にネット
# 取得が走っていた(稜サブOSテスト2026-08-01)。リポジトリ内に固定して
# インストーラーの事前取得(ensure_tokenizer_cached)を永続化し、
# アンインストール(=リポジトリ削除)で痕跡も消す。真実源はこの定数1箇所
# (事前取得側が別の値を使うと別ディレクトリになり無意味になる)。
TIKTOKEN_CACHE_DIR = str(Path(__file__).resolve().parents[2] / ".cache" / "tiktoken")


def _get_encoding():
    """tiktokenエンコーディングを遅延初期化で取得。利用不可時はFalseを返す。"""
    global _tiktoken_encoding
    if _tiktoken_encoding is None:
        try:
            # tiktokenは環境変数をBPE読み込み時に参照するため import 後の
            # 設定でも間に合うが、明示指定キャッシュへの書き込み失敗は
            # raiseになる(既定tempは握り潰し)。失敗はexceptで文字数推定へ
            # degradeするので致命ではない
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", TIKTOKEN_CACHE_DIR)
            os.makedirs(os.environ["TIKTOKEN_CACHE_DIR"], exist_ok=True)
            import tiktoken
            _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
            logger.info("tiktoken (cl100k_base) initialized successfully")
        except Exception as e:
            logger.warning(f"tiktoken not available, falling back to estimation: {e}")
            _tiktoken_encoding = False
    return _tiktoken_encoding


def ensure_tokenizer_cached() -> bool:
    """インストーラー用: cl100k_baseのBPEファイルをキャッシュへ事前取得する。

    冪等性はtiktoken自身が担保(存在+ハッシュ一致で即return・不一致は再取得)。
    Returns True when the encoder is usable.
    """
    return _get_encoding() is not False


def estimate_token_count(text: str) -> int:
    """
    Count tokens using tiktoken (cl100k_base).
    Falls back to character/word estimation if tiktoken is unavailable.

    Args:
        text: Text to count tokens for

    Returns:
        Token count
    """
    if not text:
        return 0

    enc = _get_encoding()
    if enc:
        return len(enc.encode(text))

    # フォールバック: 文字数/単語数ベースの推定
    char_estimate = len(text) / 3.5
    word_estimate = len(text.split()) * 0.8
    return int(max(char_estimate, word_estimate, 1))


class APIPromptTokenManager:
    """
    Unified token manager (上限+削減優先順位方式)。

    Builds all sections first, then checks total against the available
    context. If over budget, trims sections in trim_priority order.
    API providers use max_context=128K; Ollama passes its num_ctx with
    reserved_tokens = num_predict + tools定義の推定(方式統一 2026-08-11)。
    """

    def __init__(self, max_context: int, trim_priority: List[str],
                 reserved_tokens: int = 0):
        """
        Args:
            max_context: Maximum context window size in tokens (e.g. 128000)
            trim_priority: Ordered list of section names to trim first
                          (e.g. ["long_term_memory", "pc_status", "talk_theme"])
            reserved_tokens: このマネージャに見えない消費の予約
                (生成余白 num_predict / ペイロードのtools定義など)。
                比較は max_context - reserved_tokens に対して行う。
        """
        self.max_context = max_context
        self.trim_priority = list(trim_priority)
        self.reserved_tokens = max(0, int(reserved_tokens))
        self.available_context = max(1, max_context - self.reserved_tokens)

    def manage_prompt(self, system_content: str, talk_theme_content: str,
                      long_term_memory: List[str], messages: List[Dict[str, str]],
                      user_input: str, pc_status_content: str = "",
                      command_instructions: str = "",
                      notes_content: str = "",
                      location_content: str = "",
                      relationship_content: str = "",
                      connection_info_content: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Manage prompt with the unified cap+trim-priority scheme.
        Builds everything, checks total, trims if needed.
        """
        truncation_info = {
            "truncated": False,
            "system_truncated": False,
            "talk_theme_truncated": False,
            "pc_status_truncated": False,
            "long_term_memory_truncated": 0,
            "messages_removed": 0,
            "user_truncated": False,
            "original_tokens": 0,
            "final_tokens": 0
        }

        if not isinstance(long_term_memory, list):
            long_term_memory = []
        if not isinstance(messages, list):
            messages = []

        managed = {
            "system": system_content or "",
            "talk_theme": talk_theme_content or "",
            "pc_status": pc_status_content or "",
            "location": location_content or "",
            "long_term_memory": [s for s in long_term_memory if isinstance(s, str)],
            "messages": [m for m in messages if isinstance(m, dict)],
            "user_input": user_input or "",
            "command_instructions": command_instructions or "",
            "notes": notes_content or "",
            "relationship": relationship_content or "",
            "connection_info": connection_info_content or "",
        }

        def _total_tokens() -> int:
            return (
                estimate_token_count(managed["system"]) +
                estimate_token_count(managed["talk_theme"]) +
                estimate_token_count(managed["pc_status"]) +
                estimate_token_count(managed["location"]) +
                estimate_token_count(managed["command_instructions"]) +
                estimate_token_count(managed["notes"]) +
                estimate_token_count(managed["relationship"]) +
                estimate_token_count(managed["connection_info"]) +
                sum(estimate_token_count(s) for s in managed["long_term_memory"]) +
                sum(estimate_token_count(m.get("content", "")) for m in managed["messages"]) +
                estimate_token_count(managed["user_input"])
            )

        truncation_info["original_tokens"] = _total_tokens()

        # If within budget, return as-is
        if truncation_info["original_tokens"] <= self.available_context:
            truncation_info["final_tokens"] = truncation_info["original_tokens"]
            return managed, truncation_info

        # Over budget — trim in priority order
        for section_name in self.trim_priority:
            if _total_tokens() <= self.available_context:
                break

            if section_name == "long_term_memory":
                while managed["long_term_memory"] and _total_tokens() > self.available_context:
                    managed["long_term_memory"].pop()
                    truncation_info["long_term_memory_truncated"] += 1
                    truncation_info["truncated"] = True

            elif section_name == "pc_status":
                if managed["pc_status"]:
                    managed["pc_status"] = ""
                    truncation_info["pc_status_truncated"] = True
                    truncation_info["truncated"] = True

            elif section_name == "talk_theme":
                if managed["talk_theme"]:
                    managed["talk_theme"] = ""
                    truncation_info["talk_theme_truncated"] = True
                    truncation_info["truncated"] = True

            elif section_name == "location":
                if managed["location"]:
                    managed["location"] = ""
                    truncation_info["truncated"] = True

            elif section_name == "notes":
                if managed["notes"]:
                    managed["notes"] = ""
                    truncation_info["truncated"] = True

        # If still over, trim oldest messages (keep at least 2)
        if _total_tokens() > self.available_context:
            min_keep = min(2, len(managed["messages"]))
            while len(managed["messages"]) > min_keep and _total_tokens() > self.available_context:
                managed["messages"].pop(0)
                truncation_info["messages_removed"] += 1
                truncation_info["truncated"] = True

        truncation_info["final_tokens"] = _total_tokens()

        if truncation_info["truncated"]:
            logger.info(
                f"API prompt trimmed: {truncation_info['original_tokens']} -> "
                f"{truncation_info['final_tokens']} tokens"
            )

        # 溢れ推定ガード: 削り尽くしても収まらない(=プロバイダ側で黙って
        # 切り詰められる可能性)。Ollamaの切り詰めはエラーにならず
        # system先頭欠け=人格崩れとして現れるため、ここで必ず痕跡を残す
        if truncation_info["final_tokens"] > self.available_context:
            logger.warning(
                f"Prompt still exceeds available context after trimming "
                f"({truncation_info['final_tokens']} > {self.available_context}, "
                f"max_context={self.max_context}, reserved={self.reserved_tokens}) "
                f"— the provider may silently truncate the prompt"
            )

        return managed, truncation_info