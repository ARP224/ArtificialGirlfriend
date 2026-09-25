"""
tests/smoke/test_auto_prompt_history_filter.py

AutoPrompt固定メッセージの会話ページ履歴フィルタ(稜裁定 2026-08-21)。

固定文はLLM文脈のためuserメッセージとしてDBに残る(ライブ表示には出ない)が、
End→Startの履歴再ロードで区別なく表示されていた。ローダーの判定
is_auto_prompt_user_message が metadata 印(正)と内容一致(過去行の救済)で
スキップする。Historyページは全量表示のまま(このフィルタを共有しない)。
"""

from ui.conversation.history import (
    current_auto_prompt_texts,
    is_auto_prompt_user_message,
)

FIXED_JA = "これは自動送信です。ユーザーは無言状態が続いています。"
TEXTS = {FIXED_JA}


def test_metadata_flag_hides_message():
    msg = {"role": "user", "content": "任意の文面", "metadata": {"is_auto_prompt": True}}
    assert is_auto_prompt_user_message(msg, TEXTS)


def test_legacy_content_match_hides_message():
    # 印が付く前に保存された過去行: 内容一致(前後空白は無視)で救済
    msg = {"role": "user", "content": f"  {FIXED_JA}  "}
    assert is_auto_prompt_user_message(msg, TEXTS)


def test_normal_user_message_kept():
    msg = {"role": "user", "content": "こんにちは"}
    assert not is_auto_prompt_user_message(msg, TEXTS)


def test_assistant_message_kept_even_with_matching_content():
    # AIが固定文と同一文面を話しても消さない(userロール限定)
    msg = {"role": "assistant", "content": FIXED_JA}
    assert not is_auto_prompt_user_message(msg, TEXTS)


def test_empty_content_kept():
    # 固定文が空に編集されていた場合に通常の空メッセージを誤って消さない
    msg = {"role": "user", "content": ""}
    assert not is_auto_prompt_user_message(msg, set())
    assert not is_auto_prompt_user_message(msg, TEXTS)


def test_current_texts_strip_and_drop_empty():
    # 現行設定から作る集合はstrip済み・空除去済み(既定値2言語が入る)
    texts = current_auto_prompt_texts()
    assert "" not in texts
    assert all(t == t.strip() for t in texts)
