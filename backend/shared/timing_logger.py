"""
会話ターンのタイミング計測用ロガー

会話ターンの各処理ステップの所要時間を可視化します。
ログは統一ログシステムを通じて、UI/コンソール/ファイルに出力されます。

使用方法:
    from backend.shared.timing_logger import start_turn, end_turn, timing_log, timing_block

    # ターン開始
    start_turn()

    # 個別のステップをログ
    timing_log("step_name")

    # ブロック計測（開始/終了を自動ログ）
    with timing_block("process_name"):
        # 処理...

    # ターン終了
    end_turn()

環境変数:
    AG_TIMING_LOG=0 で無効化（デフォルトは有効）
"""
import logging
import time
import os
from contextlib import contextmanager
from typing import Optional


# 環境変数で有効/無効を制御
TIMING_ENABLED = os.environ.get('AG_TIMING_LOG', '1') == '1'

# 専用のlogger
# Note: ハンドラーはsetup_logging()で統一管理される
# propagate=True（デフォルト）でルートロガーに伝播
_logger = logging.getLogger("AG_TIMING")
_logger.setLevel(logging.INFO)

# グローバル変数でターン開始時刻を管理
# AGは同時に1つの会話ターンのみ処理するため、グローバル変数で十分
_turn_start: Optional[float] = None


def start_turn() -> None:
    """
    会話ターンの計測開始

    generate_reply()の最初で呼び出す
    """
    global _turn_start
    if not TIMING_ENABLED:
        return
    _turn_start = time.time()
    _logger.info("=" * 70)
    _logger.info(f"[TIMING] {'TURN_START':40s} | 0.000s")


def end_turn() -> None:
    """
    会話ターンの計測終了

    TTS再生完了後に呼び出す
    """
    global _turn_start
    if not TIMING_ENABLED or _turn_start is None:
        return
    elapsed = time.time() - _turn_start
    _logger.info(f"[TIMING] {'TURN_COMPLETE':40s} | {elapsed:.3f}s")
    _logger.info("=" * 70)
    _turn_start = None


def timing_log(step_name: str) -> None:
    """
    タイミングログを出力（ターン開始からの累積時間）

    Args:
        step_name: ステップ名（40文字以内推奨）
    """
    if not TIMING_ENABLED or _turn_start is None:
        return
    elapsed = time.time() - _turn_start
    _logger.info(f"[TIMING] {step_name:40s} | {elapsed:.3f}s")


@contextmanager
def timing_block(block_name: str):
    """
    処理ブロックの計測（開始と終了を自動ログ）

    Args:
        block_name: ブロック名（35文字以内推奨、_start/_doneが付加される）

    使用例:
        with timing_block("llm_invoke"):
            result = chat_llm.invoke(messages)

    出力例:
        [TIMING] llm_invoke_start                         | 0.743s
        [TIMING] llm_invoke_done                          | 19.797s (block: 19.054s)
    """
    if not TIMING_ENABLED or _turn_start is None:
        yield
        return

    block_start = time.time()
    timing_log(f"{block_name}_start")
    try:
        yield
    finally:
        block_elapsed = time.time() - block_start
        total_elapsed = time.time() - _turn_start
        _logger.info(
            f"[TIMING] {block_name + '_done':40s} | "
            f"{total_elapsed:.3f}s (block: {block_elapsed:.3f}s)"
        )
