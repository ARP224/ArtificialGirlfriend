"""Smoke tests for backend/youtube/auth.py — the URL-issuing authorization
flow (2026-07-12 変更: ブラウザ自動起動なし・再発行可能). Offline: only URL
generation and pending-state bookkeeping are exercised; no network."""

import json
from types import SimpleNamespace

import pytest

from backend.shared import atomic_io
from backend.youtube import auth


@pytest.fixture
def fake_client_secret(monkeypatch, tmp_path):
    cs = tmp_path / "client_secret.json"
    cs.write_text(json.dumps({"installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}), encoding="utf-8")
    monkeypatch.setattr(auth, "CLIENT_SECRET_FILE", cs)
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "token.json")
    yield
    auth.cancel_authorization()


def test_begin_flow_returns_url_without_opening_browser(fake_client_secret):
    result = auth.begin_authorization_flow()
    assert result["success"]
    url = result["auth_url"]
    assert url.startswith("https://accounts.google.com/")
    assert "test-client-id" in url
    assert "localhost" in url  # loopback redirect_uri with the bound port
    assert auth.get_authorization_status()["status"] == "pending"


def test_begin_flow_is_restartable(fake_client_secret):
    """2回目以降の押下: 前の待受をキャンセルして新URLを発行できる
    (旧実装はブロックしたまま2回目が無反応 — 2026-07-12修正の回帰テスト)."""
    r1 = auth.begin_authorization_flow()
    assert r1["success"]
    r2 = auth.begin_authorization_flow()
    assert r2["success"]
    # 新しい待受(別ポート・別state)に置き換わり、状態はpendingのまま
    assert r2["auth_url"] != r1["auth_url"]
    assert auth.get_authorization_status()["status"] == "pending"
    assert auth.get_authorization_status()["auth_url"] == r2["auth_url"]


def test_begin_flow_without_client_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "CLIENT_SECRET_FILE", tmp_path / "missing.json")
    result = auth.begin_authorization_flow()
    assert not result["success"]


# ---------------------------------------------------------------------------
# token.json: Windows の os.replace は対象が開かれている間 PermissionError
# になる(youtube_store と同じ罠 — get_authorized_channel は状態表示のたびに
# token.json を読む)
# ---------------------------------------------------------------------------

_CHANNEL = {"channel_id": "UCreply0000000000000000x", "channel_title": "reply ch"}


def _fake_creds():
    return SimpleNamespace(
        to_json=lambda: json.dumps({"token": "t", "refresh_token": "r"}))


def test_token_save_retries_replace_on_permission_error(monkeypatch, tmp_path):
    """外部プロセス(AVスキャン等)が対象を握っていても書き込みを失わない。"""
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "token.json")

    real_replace = atomic_io.os.replace
    fails = {"left": 3}

    def flaky_replace(src, dst):
        if fails["left"] > 0:
            fails["left"] -= 1
            raise PermissionError(5, "アクセスが拒否されました。", src)
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)  # test speed

    auth._save_credentials(_fake_creds(), **_CHANNEL)
    assert fails["left"] == 0  # the flaky window was actually exercised
    assert auth.get_authorized_channel() == _CHANNEL


def test_concurrent_channel_reads_do_not_break_token_saves(monkeypatch, tmp_path):
    """get_authorized_channel を別スレッドで連打しながらトークンの書き戻しを
    繰り返しても、書きが落ちず読みも「チャンネル不明」にならない(Windows 実機
    競合のロック。POSIX では元から通る)。"""
    import threading

    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "token.json")
    auth._save_credentials(_fake_creds(), **_CHANNEL)

    stop = threading.Event()
    missing = []

    def reader():
        while not stop.is_set():
            if auth.get_authorized_channel() != _CHANNEL:
                missing.append(1)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for th in threads:
        th.start()
    try:
        for _ in range(200):
            auth._save_credentials(_fake_creds())  # refresh write keeps channel info
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=5)

    assert not missing, "the authorized channel read came back empty during a write"
    assert auth.get_authorized_channel() == _CHANNEL
