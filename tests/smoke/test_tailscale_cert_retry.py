"""
tests/smoke/test_tailscale_cert_retry.py

run_tailscale_cert のリトライ方針の検証(2026-08-04 Mac実機の
「ACME order status: invalid」一過性失敗が発端):
  - 非ゼロ終了は _CERT_ATTEMPTS 回までリトライされ、途中で成功すれば成功扱い
  - 全滅時は従来と同一文言の RuntimeError + ERROR ログ(1障害=英語生ログ1)
  - タイムアウトはリトライせず即時失敗(30秒×3回の無反応を作らない)
  - 進捗コールバック(UI注入)へ試行イベントが届く・壊れていても取得は死なない
"""

import logging
import subprocess

import pytest

from backend.server import tailscale
from backend.shared.i18n import t

HOST = "machine.tailnet.ts.net"


class FakeRun:
    """subprocess.run の差し替え。outcomes の並び順に1呼び出し1結果を返す。"""

    def __init__(self, outcomes, cert_path=None, key_path=None):
        self.calls = 0
        self._outcomes = outcomes
        self._cert = cert_path
        self._key = key_path

    def __call__(self, cmd, **kwargs):
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 30)
        if outcome == "ok":
            self._cert.write_text("cert")
            self._key.write_text("key")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd, 1, stdout="",
            stderr="500 Internal Server Error: acme: order ... status: invalid",
        )


@pytest.fixture
def certs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tailscale, "CERTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def sleeps(monkeypatch):
    """実時間の sleep を殺しつつ、呼ばれた秒数を記録する。"""
    calls = []
    monkeypatch.setattr(tailscale.time, "sleep", calls.append)
    return calls


def test_transient_failure_recovers_on_retry(certs_dir, sleeps, monkeypatch):
    fake = FakeRun(
        ["fail", "ok"],
        cert_path=certs_dir / f"{HOST}.crt",
        key_path=certs_dir / f"{HOST}.key",
    )
    monkeypatch.setattr(tailscale.subprocess, "run", fake)
    events = []

    cert, key = tailscale.run_tailscale_cert(HOST, progress=events.append)

    assert fake.calls == 2
    assert sleeps == [tailscale._CERT_RETRY_WAIT_SEC]
    assert cert.endswith(f"{HOST}.crt")
    assert key.endswith(f"{HOST}.key")
    # 進捗イベント: 試行1→リトライ待ち→試行2(成功)
    assert [(e["phase"], e["attempt"]) for e in events] == [
        ("attempt", 1), ("retry_wait", 1), ("attempt", 2),
    ]
    assert all(
        e["total"] == tailscale._CERT_ATTEMPTS
        and e["wait_sec"] == tailscale._CERT_RETRY_WAIT_SEC
        for e in events
    )


def test_persistent_failure_exhausts_attempts(certs_dir, sleeps, monkeypatch, caplog):
    fake = FakeRun(["fail"] * tailscale._CERT_ATTEMPTS)
    monkeypatch.setattr(tailscale.subprocess, "run", fake)
    events = []

    with caplog.at_level(logging.ERROR, logger="backend.server.tailscale"):
        with pytest.raises(RuntimeError) as exc:
            tailscale.run_tailscale_cert(HOST, progress=events.append)

    assert fake.calls == tailscale._CERT_ATTEMPTS
    assert len(sleeps) == tailscale._CERT_ATTEMPTS - 1
    # 最終エラーはリトライ導入前と同一文言(表示仕様は不変)。文言は t() 化済み
    # (4487712) なので期待値もキーから組み立てる=UI 言語に依存しない
    # (Mac 実測 2026-08-17: LANG=C で日本語ベタ書き assert が落ちた)。
    assert str(exc.value) == t(
        'tailscale.cert_failed', returncode=1,
        stderr="500 Internal Server Error: acme: order ... status: invalid",
    )
    assert "status: invalid" in str(exc.value)
    # 障害の確定は英語生ログ1本で閉まる(1障害=生ログ1+翻訳済み表示1)
    error_logs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_logs) == 1
    assert "failed after" in error_logs[0].message
    assert "status: invalid" in error_logs[0].message
    # 進捗イベント: 最終試行の開始まで届く(retry_wait は最終試行後には出ない)
    assert [(e["phase"], e["attempt"]) for e in events] == [
        ("attempt", 1), ("retry_wait", 1),
        ("attempt", 2), ("retry_wait", 2),
        ("attempt", 3),
    ]


def test_timeout_fails_fast_without_retry(certs_dir, sleeps, monkeypatch, caplog):
    fake = FakeRun(["timeout"])
    monkeypatch.setattr(tailscale.subprocess, "run", fake)
    events = []

    with caplog.at_level(logging.ERROR, logger="backend.server.tailscale"):
        with pytest.raises(RuntimeError) as exc:
            tailscale.run_tailscale_cert(HOST, progress=events.append)

    assert fake.calls == 1
    assert sleeps == []
    assert str(exc.value) == t('tailscale.cert_timeout')
    assert any("timed out" in r.message for r in caplog.records)
    assert [(e["phase"], e["attempt"]) for e in events] == [("attempt", 1)]


def test_broken_progress_callback_does_not_break_acquisition(certs_dir, sleeps, monkeypatch):
    fake = FakeRun(
        ["ok"],
        cert_path=certs_dir / f"{HOST}.crt",
        key_path=certs_dir / f"{HOST}.key",
    )
    monkeypatch.setattr(tailscale.subprocess, "run", fake)

    def broken(_ev):
        raise ValueError("UI side bug")

    cert, key = tailscale.run_tailscale_cert(HOST, progress=broken)

    assert fake.calls == 1
    assert cert.endswith(f"{HOST}.crt")
