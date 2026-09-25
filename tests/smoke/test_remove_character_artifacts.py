"""Smoke test: remove_character leaves no file keyed by the character id
anywhere under character_data/ (memory DB + WAL side files + corruption copies +
recovery dumps + attachments folder, relationship, note, ELYTH session log
(+ its .tmp atomic-write sidecar), ELYTH note / relationships / thread state
(+ own-handle registry entry), generated images).

対象: backend/conversation/character_manager.remove_character /
_remove_memory_artifacts と各ドメインの remove_*(character_id)。
以前は .db / relationship / icon / config しか消さず、他は全部孤児化していた
(2026-08-17 実データ: 孤児 -shm/-wal 14 本=削除キャラ 7 人分)。

Windows は開いたままの .db を削除できないので、別スレッドが接続を開いたまま
削除させる=close_all が先に走ることの実弾でもある。
"""

import json
import sqlite3
import threading
import types
from pathlib import Path

import pytest

from backend.conversation import character_manager as cm
from backend.elyth import (
    elyth_memory, elyth_note_manager, elyth_relationship_manager, elyth_thread_state,
)
from backend.memory import note_manager
from backend.memory.memory_manager import MemoryStore
from backend.shared import image_storage

CID = "11111111-2222-3333-4444-555555555555"


class _FakeState:
    def __init__(self):
        self.background_tasks_lock = threading.Lock()
        self.background_tasks = []
        self.memory_managers = {}
        self.active_llm_cache = {}
        self.active_character_id = CID
        self.conversation_active = True
        self._lock = threading.RLock()

    def get_character_operation_lock(self, character_id):
        return self._lock


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Redirect every per-character directory constant into tmp_path and reset
    the character-config cache so the tmp config is picked up."""
    dirs = {name: tmp_path / name for name in (
        "character_configs", "character_icons", "memory", "attachments",
        "relationship", "notes",
        "elyth_notes", "elyth_relationships", "elyth_sessions", "elyth_thread_state",
        "generated_images")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setattr(cm, "CHARACTER_CONFIGS_DIR", str(dirs["character_configs"]))
    monkeypatch.setattr(cm, "CHARACTER_ICONS_DIR", str(dirs["character_icons"]))
    monkeypatch.setattr(cm, "MEMORY_DIR", str(dirs["memory"]))
    monkeypatch.setattr(cm, "ATTACHMENTS_DIR", dirs["attachments"])
    monkeypatch.setattr(cm, "_character_file_cache", {})
    monkeypatch.setattr(cm, "_character_config_cache", {})
    monkeypatch.setattr(cm, "_last_cache_update", 0)
    from backend.memory import relationship_manager
    monkeypatch.setattr(relationship_manager, "RELATIONSHIP_DIR", dirs["relationship"])
    monkeypatch.setattr(note_manager, "NOTE_DIR", dirs["notes"])
    monkeypatch.setattr(elyth_note_manager, "ELYTH_NOTE_DIR", dirs["elyth_notes"])
    monkeypatch.setattr(elyth_relationship_manager, "ELYTH_RELATIONSHIP_DIR", dirs["elyth_relationships"])
    monkeypatch.setattr(elyth_memory, "ELYTH_SESSION_DIR", dirs["elyth_sessions"])
    monkeypatch.setattr(elyth_thread_state, "ELYTH_THREAD_STATE_DIR", dirs["elyth_thread_state"])
    monkeypatch.setattr(elyth_thread_state, "_OWN_HANDLES_FILE", dirs["elyth_thread_state"] / "_own_handles.json")
    monkeypatch.setattr(image_storage, "GENERATED_IMAGES_DIR", dirs["generated_images"])
    return dirs


def _seed_character(dirs):
    """Create every kind of per-character file the app can produce."""
    db = dirs["memory"] / f"{CID}.db"
    (dirs["character_icons"] / f"{CID}.png").write_bytes(b"png")
    (dirs["character_configs"] / f"{CID}.json").write_text(json.dumps({
        "version": "1.0", "character_id": CID, "name": "del-test",
        "db_file_path": str(db), "icon_path": str(dirs["character_icons"] / f"{CID}.png"),
    }), encoding="utf-8")
    # memory DB (real store, WAL) + derived files
    store = MemoryStore(str(db))
    store.put(("ns",), "k", {"v": 1})
    (dirs["memory"] / f"{CID}.db.corrupt.1700000000").write_bytes(b"x")
    (dirs["memory"] / f"{CID}.db.backup.1700000001").write_bytes(b"x")
    (dirs["memory"] / f"{CID}.recovery.1700000002.123.json").write_text("{}")
    # 会話添付(2026-08-20 に memory/<uuid>/ から attachments/<uuid>/ へ分離)
    (dirs["attachments"] / CID / "images").mkdir(parents=True)
    (dirs["attachments"] / CID / "images" / "a.jpg").write_bytes(b"jpg")
    (dirs["attachments"] / CID / "documents").mkdir()
    (dirs["attachments"] / CID / "documents" / "a.txt").write_text("doc")
    # other per-character files
    (dirs["relationship"] / f"{CID}.txt").write_text("rel")
    (dirs["notes"] / f"{CID}.md").write_text("1. note\n")
    (dirs["elyth_notes"] / f"{CID}.md").write_text("1. enote\n")
    (dirs["elyth_relationships"] / f"{CID}.json").write_text("{}")
    # ELYTH セッションログ本体と、atomic write が途中で落ちた場合の残骸
    # (<uuid>.json の .tmp 兄弟)。本体だけ消すと .tmp が孤児として残る
    (dirs["elyth_sessions"] / f"{CID}.json").write_text('{"sessions": []}')
    (dirs["elyth_sessions"] / f"{CID}.tmp").write_text('{"sessions": []}')
    (dirs["elyth_thread_state"] / f"{CID}.json").write_text("{}")
    (dirs["elyth_thread_state"] / "_own_handles.json").write_text(json.dumps(
        {CID: "deleted_ag", "other-char": "kept_ag"}))
    (dirs["generated_images"] / CID / "thumbnails").mkdir(parents=True)
    (dirs["generated_images"] / CID / "g.png").write_bytes(b"png")
    # unrelated neighbour that must survive
    (dirs["memory"] / "other-char.db").write_bytes(b"")
    (dirs["notes"] / "tool_note_claude.md").write_text("shared")
    return store


def _files_mentioning(root: Path, needle: str):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if needle in p.name)


def test_remove_character_leaves_no_trace(data_root, tmp_path):
    dirs = data_root
    store = _seed_character(dirs)
    state = _FakeState()
    state.memory_managers[CID] = types.SimpleNamespace(store=store)
    state.active_llm_cache[CID] = object()

    # a live connection from another thread (Windows: an open .db cannot be deleted)
    hold = threading.Event()
    ready = threading.Event()

    def worker():
        store.get(("ns",), "k")
        ready.set()
        hold.wait(10)

    t = threading.Thread(target=worker)
    t.start()
    assert ready.wait(5)
    assert _files_mentioning(tmp_path, CID)  # 前提: 痕跡が沢山ある

    try:
        cm.remove_character(state, CID)
    finally:
        hold.set()
        t.join(5)

    assert _files_mentioning(tmp_path, CID) == []  # 痕跡ゼロ
    assert CID not in state.memory_managers
    assert CID not in state.active_llm_cache
    assert state.active_character_id is None and state.conversation_active is False
    # own-handle registry: only this character's key is gone
    reg = json.loads((dirs["elyth_thread_state"] / "_own_handles.json").read_text())
    assert reg == {"other-char": "kept_ag"}
    # neighbours untouched
    assert (dirs["memory"] / "other-char.db").exists()
    assert (dirs["notes"] / "tool_note_claude.md").exists()
    # the store is closed (no silent reconnect that would recreate the .db)
    with pytest.raises(sqlite3.ProgrammingError):
        store.get(("ns",), "k")
    assert not (dirs["memory"] / f"{CID}.db").exists()


def test_refused_while_elyth_session_running(data_root, monkeypatch):
    """ELYTHセッション実行中の削除は AGError(コード付き)で拒否され、何も消えない。

    コードが載ることまで見るのは、UI が t("err.<code>") で翻訳済みトーストを
    出すため。ガードを try: の内側へ動かすと外側の except が素の RuntimeError に
    詰め替えてコードが落ちる=このテストが赤くなる。
    """
    from backend.elyth import elyth_session_manager as esm
    from backend.shared.errors import AGError

    dirs = data_root
    store = _seed_character(dirs)
    state = _FakeState()
    state.elyth_session_active = True

    class _StubManager:
        def is_session_active_for(self, character_id):
            return character_id == CID

    monkeypatch.setattr(esm, "get_elyth_session_manager", lambda state=None: _StubManager())

    try:
        with pytest.raises(AGError) as excinfo:
            cm.remove_character(state, CID)
        assert excinfo.value.ag_code == cm.DELETE_ELYTH_ACTIVE_CODE
        # 拒否なので痕跡は1つも減っていない
        assert (dirs["elyth_sessions"] / f"{CID}.json").exists()
        assert (dirs["character_configs"] / f"{CID}.json").exists()
    finally:
        store.close_all()


def test_refused_while_memory_task_running(data_root):
    """記憶タスク実行中の拒否もコード付き(=UIが翻訳できる)。"""
    from backend.shared.errors import AGError

    dirs = data_root
    store = _seed_character(dirs)
    state = _FakeState()
    alive = threading.Event()
    done = threading.Event()

    def worker():
        alive.set()
        done.wait(10)

    t = threading.Thread(target=worker)
    t.start()
    assert alive.wait(5)
    state.background_tasks.append(
        {"character_id": CID, "task_type": "extraction", "thread": t})

    try:
        with pytest.raises(AGError) as excinfo:
            cm.remove_character(state, CID)
        assert excinfo.value.ag_code == cm.DELETE_MEMORY_TASK_CODE
        assert (dirs["character_configs"] / f"{CID}.json").exists()
    finally:
        done.set()
        t.join(5)
        store.close_all()


def test_remove_memory_artifacts_tolerates_missing_files(data_root):
    dirs = data_root
    db = dirs["memory"] / f"{CID}.db"
    # nothing exists yet — must not raise, must not create anything
    cm._remove_memory_artifacts(CID, str(db))
    assert list(dirs["memory"].iterdir()) == []
    # only a 0-byte .db (created, never activated) — deleted, neighbours kept
    db.write_bytes(b"")
    (dirs["memory"] / "other-char.db-wal").write_bytes(b"")
    cm._remove_memory_artifacts(CID, str(db))
    assert sorted(p.name for p in dirs["memory"].iterdir()) == ["other-char.db-wal"]
