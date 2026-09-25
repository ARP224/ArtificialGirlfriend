"""
tests/smoke/test_queue_cancelled_future.py

LLMTaskQueue: 待ち手が諦めて cancel 済みの future にワーカーが結果を書こうと
しても例外を吐かず（旧: InvalidStateError の ERROR トレースバック2本・
2026-08-16 Windows 実機）、WARNING 1行で捨ててワーカーは次のタスクへ進む。

対象: backend/shared/queue_manager.py の _settle_future。
"""

import logging
import threading
import time

from backend.shared.queue_manager import LLMTaskQueue


def test_worker_survives_result_delivery_to_cancelled_future(caplog):
    q = LLMTaskQueue(max_queue_size=10, default_task_timeout=10)
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=5)
        return "A"

    def quick():
        return "B"

    try:
        fut_a = q.enqueue_llm_task(blocker, timeout=10)
        time.sleep(0.3)  # ワーカーが A を実行中
        # 待ち手が諦めた体で cancel: キューは set_running_or_notify_cancel を
        # 呼ばないので実行中でも PENDING のまま=cancel が通る(本番と同じ)
        assert fut_a.cancel() is True

        with caplog.at_level(logging.WARNING, logger="backend.shared.queue_manager"):
            gate.set()
            # 完走した A の結果は捨てられる(旧実装は set_result → InvalidStateError
            # → ERROR traceback)。ワーカーが生きている証拠として C が普通に完走する
            fut_c = q.enqueue_llm_task(quick, timeout=10)
            assert fut_c.result(timeout=5) == "B"

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], [r.getMessage() for r in errors]
        discarded = [r for r in caplog.records
                     if r.levelno == logging.WARNING and "result discarded" in r.getMessage()]
        assert len(discarded) == 1
    finally:
        q.shutdown(cancel_pending=True, timeout=5)
