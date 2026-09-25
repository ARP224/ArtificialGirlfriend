"""
backend/shared/playback_state.py

TTS再生中フラグ — LLMタスクのタイムアウト時計の停止判定（稜裁定 2026-08-12）。

読み上げが始まった時点で応答生成は成功しており、そこからタイムアウトさせる
意味がない（切ると「UIはエラー・履歴には応答が保存」の二重記録になる=実機
2026-08-12）。TTS区間（合成+再生）は、LLMタスクの2つの時計

  1. 呼出側: conversation_manager._enqueue_llm_task の future 待ち
  2. キュー側: queue_manager._execute_task_with_timeout のスライスループ

の両方が本フラグを見て残り時間の減算を止める。停止は有界:
_play_tts_blocking の再生待ちは「再生時間+5秒」の内部タイムアウトと
WS送達失敗時の即スキップを持ち、フラグは try/finally で必ず解除される。

停止はグローバル（タスク個別でない）: LLMキューは単一FIFOワーカーのため
TTS再生中は他のタスクも実行されておらず、後ろで待つタスクの呼出側時計も
一緒に止まるのが正しい（止めないと長い読み上げの後ろのタスクが誤キャンセル
されうる）。stdlib のみに依存する共有葉。
"""

import threading

_tts_active = threading.Event()


def mark_tts_active() -> None:
    """TTS区間の開始（_play_tts_blocking の入口）。"""
    _tts_active.set()


def clear_tts_active() -> None:
    """TTS区間の終了（try/finally で必ず呼ぶこと）。"""
    _tts_active.clear()


def is_tts_active() -> bool:
    """タイムアウト時計を止めるべきか（両時計の停止判定が読む）。"""
    return _tts_active.is_set()
