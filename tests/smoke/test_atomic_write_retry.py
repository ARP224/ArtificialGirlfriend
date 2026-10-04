"""Every atomic-write site (tmp file + os.replace) must survive a briefly
locked target.

On Windows os.replace fails with PermissionError while any handle is open on
the target (AV scan, indexer, a reader in another thread). A bare os.replace
loses the write: a note save fails the whole reply turn, a character edit is
dropped, a log flush is skipped. All sites go through
backend.shared.atomic_io.replace_with_retry; this test makes os.replace fail
three times and checks, per site, that the retries were used and the data
landed.
"""

import json
import logging
import os

import pytest

from backend.shared import atomic_io

logger = logging.getLogger(__name__)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _note(tmp_path, monkeypatch):
    from backend.shared import note_io
    note_io.save_note_entries(tmp_path, "c1", ["a", "b"], logger)
    return (tmp_path / "c1.md").read_text(encoding="utf-8") == "1. a\n2. b\n"


def _relationship(tmp_path, monkeypatch):
    from backend.memory import relationship_manager as rm
    monkeypatch.setattr(rm, "RELATIONSHIP_DIR", tmp_path)
    rm.save_relationship("c1", "text")
    return (tmp_path / "c1.txt").read_text(encoding="utf-8") == "text"


def _elyth_relationship(tmp_path, monkeypatch):
    from backend.elyth import elyth_relationship_manager as erm
    monkeypatch.setattr(erm, "ELYTH_RELATIONSHIP_DIR", tmp_path)
    erm.save_elyth_relationships("c1", {"relationships": {"x": 1}})
    return _read_json(tmp_path / "c1.json")["relationships"] == {"x": 1}


def _elyth_session_log(tmp_path, monkeypatch):
    from backend.elyth import elyth_memory as em
    monkeypatch.setattr(em, "ELYTH_SESSION_DIR", tmp_path)
    em.save_session_log("c1", {"session_id": "s1", "turns": []})
    return _read_json(tmp_path / "c1.json")["sessions"][0]["session_id"] == "s1"


def _elyth_thread_state(tmp_path, monkeypatch):
    from backend.elyth import elyth_thread_state as ets
    monkeypatch.setattr(ets, "ELYTH_THREAD_STATE_DIR", tmp_path)
    ets._write_json(tmp_path / "c1.json", {"a": 1})  # swallows its own errors
    return (tmp_path / "c1.json").exists() and _read_json(tmp_path / "c1.json") == {"a": 1}


def _elyth_cycle_log(tmp_path, monkeypatch):
    from backend.elyth.elyth_cycle_logger import ELYTHCycleLogger
    cycle_log = ELYTHCycleLogger(output_path=tmp_path / "cycle.json")
    cycle_log._data = {"x": 1}
    cycle_log._flush()  # swallows its own errors
    return (tmp_path / "cycle.json").exists() and _read_json(tmp_path / "cycle.json") == {"x": 1}


def _youtube_session_log(tmp_path, monkeypatch):
    from backend.youtube.youtube_session_logger import YouTubeSessionLogger
    path = tmp_path / "youtube_session_log.json"
    YouTubeSessionLogger(output_path=path).start_session("c1", "name", True, "UCx")
    return path.exists() and _read_json(path)["last_run"]["character_id"] == "c1"


def _character_config(tmp_path, monkeypatch):
    from backend.conversation import character_manager as cm
    cm._save_config_file(tmp_path / "c1.json", {"name": "x"})
    return _read_json(tmp_path / "c1.json") == {"name": "x"}


def _api_settings(tmp_path, monkeypatch):
    from backend.shared import api_settings
    monkeypatch.setattr(api_settings, "API_SETTINGS_FILE", tmp_path / "api_settings.json")
    monkeypatch.setattr(api_settings, "_settings_cache", None)
    monkeypatch.setattr(api_settings, "_settings_cache_mtime", None)
    api_settings.save_api_settings({"k": 1})
    return _read_json(tmp_path / "api_settings.json") == {"k": 1}


def _atomic_file_operation(tmp_path, monkeypatch):
    from backend.backend import atomic_file_operation
    target = tmp_path / "f.json"
    target.write_text("old", encoding="utf-8")
    with atomic_file_operation(target) as tmp:
        tmp.write_text("new", encoding="utf-8")
    return target.read_text(encoding="utf-8") == "new"


SITES = [
    _note, _relationship, _elyth_relationship, _elyth_session_log,
    _elyth_thread_state, _elyth_cycle_log, _youtube_session_log,
    _character_config, _api_settings, _atomic_file_operation,
]


@pytest.mark.parametrize("write", SITES, ids=lambda f: f.__name__.lstrip("_"))
def test_write_survives_briefly_locked_target(write, tmp_path, monkeypatch):
    real_replace = os.replace
    fails = {"left": 3}

    def flaky_replace(src, dst):
        if fails["left"] > 0:
            fails["left"] -= 1
            raise PermissionError(5, "アクセスが拒否されました。", src)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda s: None)  # test speed

    landed = write(tmp_path, monkeypatch)

    # atomic_file_operation has a non-atomic shutil.move fallback that would
    # land the data even without a retry — so also require that the retries
    # were actually spent on os.replace.
    assert fails["left"] == 0, "os.replace was not retried"
    assert landed, "the write was lost"
