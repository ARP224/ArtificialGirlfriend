"""Smoke tests for backend/youtube/auth.py — the URL-issuing authorization
flow (2026-07-12 変更: ブラウザ自動起動なし・再発行可能). Offline: only URL
generation and pending-state bookkeeping are exercised; no network."""

import json

import pytest

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
