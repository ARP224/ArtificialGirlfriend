"""
tests/smoke/test_error_marker.py

User-facing error replies are TTS-suppressed via a language-independent
marker (ui/constants.py ERROR_MESSAGE_PREFIX). user_error() is the only
sanctioned constructor; is_error_response() must detect its output in every
UI language — the old wording-prefix list (ERROR_PREFIXES) broke silently
when wording changed, which is exactly what translation does.
"""

from ui.constants import ERROR_MESSAGE_PREFIX
from ui.conversation.generation import is_error_response
from ui.error_handler import user_error


def test_user_error_is_detected_as_error():
    msg = user_error('err.service_unavailable')
    assert msg.startswith(ERROR_MESSAGE_PREFIX)
    assert is_error_response(msg)


def test_user_error_with_placeholder():
    msg = user_error('err.generic', error='boom')
    assert is_error_response(msg)
    assert 'boom' in msg


def test_normal_reply_is_not_error():
    assert not is_error_response("こんにちは！今日はいい天気だね")
    # 旧 ERROR_PREFIXES 形式はもう生成されない=エラー扱いしない(新規約の明文化)
    assert not is_error_response("[Error: legacy style string]")
    assert not is_error_response(None)
