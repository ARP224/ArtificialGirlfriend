"""音声入力の書き起こし全文が app.log に永続化されないことの契約テスト。

背景(2026-08-18): STT 経路だけ発話全文が info ログに出ており、40MB の
ローテーションログに平文で残り続けていた(app.log 4スロットに実測 855 行)。
AI 返答は文字数だけ("AI reply generated successfully (N chars)")、テキスト
入力は元から記録なしで、音声入力だけが非対称だった。

契約:
  ① recording.py は書き起こし全文を debug でしか出さない(info は文字数のみ)
  ② add_log_message("debug", ...) は画面のシステムログには積まれるが、
     INFO 以上のハンドラ(app.log・コンソール)には届かない

②が崩れると①だけ守っても意味がないので両方見る。①を "info" に戻す1語の
変更でこのテストが赤くなる。
"""

import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDING_PY = REPO_ROOT / "ui" / "conversation" / "recording.py"


def test_transcript_is_logged_only_at_debug_level():
    """① 全文を出す行は必ず add_log_message("debug", ...) であること。"""
    lines = RECORDING_PY.read_text(encoding="utf-8").splitlines()
    transcript_lines = [ln.strip() for ln in lines if "User said:" in ln]

    # 経路は2本(ブラウザマイク / ローカルマイク)。増減したら気づけるように固定
    assert len(transcript_lines) == 2, transcript_lines
    for ln in transcript_lines:
        assert 'add_log_message("debug"' in ln, (
            f"書き起こし全文が debug 以外で出力されている: {ln}")


def test_debug_message_reaches_ui_log_but_not_info_handlers():
    """② debug は log_messages に積まれ、INFO ハンドラには届かない。

    conftest.py:32 の logging.disable(CRITICAL)(アプリが stdio fd を閉じる件の
    対策)が全レコードを止めるので、この1テストの間だけ解除して自前の
    インメモリハンドラで観測し、finally で必ず元に戻す。ルートのハンドラも
    一時的に自前1本だけにするので、解除中にどこかへ書き出されることはない。
    """
    from ui.state import app_state

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    # app.log / コンソールと同じ INFO 下限のハンドラで観測する
    handler = _Capture(level=logging.INFO)
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_disable = logging.root.manager.disable
    try:
        logging.disable(logging.NOTSET)
        root.handlers = [handler]
        root.setLevel(logging.DEBUG)
        before = len(app_state.log_messages)
        app_state.add_log_message("debug", "User said: SECRET-TRANSCRIPT")
        app_state.add_log_message("info", "Transcribed (19 chars)")
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        logging.disable(saved_disable)

    # 画面のシステムログには両方出る
    added = app_state.log_messages[before:]
    assert any("SECRET-TRANSCRIPT" in m for m in added)
    # INFO 以上のハンドラ(=app.log/コンソール)には全文が届かない
    assert not any("SECRET-TRANSCRIPT" in m for m in records)
    assert any("Transcribed (19 chars)" in m for m in records)
