"""backend/shared/atomic_io.py — os.replace with a brief PermissionError retry.

Windows: os.replace fails with PermissionError while any handle is open on
the target (AV scan, indexer, a reader in another thread). The helper is the
single place that absorbs that; these tests pin its contract.
"""

import pytest

from backend.shared import atomic_io


def _flaky(monkeypatch, failures, exc_factory):
    real_replace = atomic_io.os.replace
    state = {"left": failures, "calls": 0}

    def flaky_replace(src, dst):
        state["calls"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            raise exc_factory(src)
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)  # test speed
    return state


def _denied(src):
    return PermissionError(5, "アクセスが拒否されました。", src)


def test_replace_lands_after_transient_permission_errors(tmp_path, monkeypatch):
    src, dst = tmp_path / "data.tmp", tmp_path / "data.json"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    state = _flaky(monkeypatch, 3, _denied)

    atomic_io.replace_with_retry(src, dst)

    assert state["left"] == 0  # the flaky window was actually exercised
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_replace_gives_up_and_raises_when_target_stays_locked(tmp_path, monkeypatch):
    src, dst = tmp_path / "data.tmp", tmp_path / "data.json"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    state = _flaky(monkeypatch, 10 ** 6, _denied)

    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry(src, dst)

    assert state["calls"] == atomic_io._ATTEMPTS
    assert dst.read_text(encoding="utf-8") == "old"  # target untouched
    assert src.exists()                              # tmp cleanup stays the caller's


def test_other_errors_propagate_without_retry(tmp_path, monkeypatch):
    src, dst = tmp_path / "data.tmp", tmp_path / "data.json"
    src.write_text("new", encoding="utf-8")
    state = _flaky(monkeypatch, 5, lambda s: FileNotFoundError(2, "missing", s))

    with pytest.raises(FileNotFoundError):
        atomic_io.replace_with_retry(src, dst)

    assert state["calls"] == 1
