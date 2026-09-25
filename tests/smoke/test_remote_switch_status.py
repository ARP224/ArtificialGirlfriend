"""
tests/smoke/test_remote_switch_status.py

システムページのリモート切替状態表示のマッピングを固定する。
t() は欠落キーをキー文字列のまま返すため、'system.' で始まらないことの
検証が i18n キー欠落の検出を兼ねる(Tailscale未接続の警告文が出ない
リグレッションをここで止める)。
"""

import pytest

from backend.server.remote_switch_listener import get_remote_switch_listener
from ui.handlers.remote_switch import remote_switch_status_text


@pytest.mark.parametrize("status", [
    'starting', 'waiting_tailscale', 'listening', 'cert_failed',
])
def test_each_state_maps_to_translated_text(monkeypatch, status):
    listener = get_remote_switch_listener()
    monkeypatch.setattr(listener, 'status', status)
    monkeypatch.setattr(listener, 'listen_url', 'https://example.ts.net:7860')
    text = remote_switch_status_text()
    assert text, f"empty status line for '{status}'"
    assert not text.startswith('system.'), (
        f"i18n key missing for '{status}': got '{text}'"
    )


def test_listening_shows_url(monkeypatch):
    listener = get_remote_switch_listener()
    monkeypatch.setattr(listener, 'status', 'listening')
    monkeypatch.setattr(listener, 'listen_url', 'https://example.ts.net:7860')
    assert 'https://example.ts.net:7860' in remote_switch_status_text()


def test_stopped_is_blank(monkeypatch):
    listener = get_remote_switch_listener()
    monkeypatch.setattr(listener, 'status', 'stopped')
    assert remote_switch_status_text() == ''
