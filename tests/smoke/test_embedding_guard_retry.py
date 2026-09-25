"""会話開始前の埋め込みガード: APIプローブのunreachable 1回リトライ（稜依頼 2026-08-14）。

契約: 一過性のネットワーク不調(unreachable)だけ1秒後に1回だけ再照会する。
auth_error / no_key は確定的な状態なので再試行せず即エラー。2回目も
unreachable ならエラー(無限リトライはしない)。
"""

import time

import pytest


@pytest.fixture
def guard(monkeypatch, make_state, make_cm):
    """埋め込み設定とプローブを差し替えたガード実行器を返す。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)  # リトライ間の1秒を省く
    monkeypatch.setattr(
        "backend.shared.api_settings.get_embedding_model",
        lambda: ("openai", "text-embedding-3-large"))
    cm = make_cm(make_state(), {})

    def _run(probe_states):
        calls = []

        def fake_probe(provider):
            calls.append(provider)
            return {"state": probe_states[min(len(calls), len(probe_states)) - 1]}

        monkeypatch.setattr(
            "backend.shared.api_settings.probe_provider_api", fake_probe)
        return cm._check_embedding_ready(), calls

    return _run


def test_transient_unreachable_recovers_on_retry(guard):
    result, calls = guard(["unreachable", "ok"])
    assert result is None  # 会話開始を止めない
    assert len(calls) == 2


def test_persistent_unreachable_fails_after_one_retry(guard):
    result, calls = guard(["unreachable", "unreachable"])
    assert result["error_code"] == "embedding_api_unavailable"
    assert len(calls) == 2  # リトライは1回だけ


def test_auth_error_is_deterministic_no_retry(guard):
    result, calls = guard(["auth_error"])
    assert result["error_code"] == "embedding_api_unavailable"
    assert len(calls) == 1  # 確定的な失敗は再試行しない


def test_ok_makes_single_probe(guard):
    result, calls = guard(["ok"])
    assert result is None
    assert len(calls) == 1
