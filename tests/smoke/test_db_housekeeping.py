"""Smoke tests for the startup WAL sweep (backend/memory/db_housekeeping.py)
and for the embedding-migration scan no longer leaving WAL side files.

WAL 付随ファイル(-wal/-shm)は「最後の接続が正常に閉じた」ときだけ SQLite が消す。
クラッシュ/os._exit で残った -wal は未転記の書き込みを含みうるので、掃きは
「普通に開く→転記(checkpoint)→閉じる」だけを行い、ファイルを手で消さない。
"""

import os
import sqlite3
import subprocess
import sys

from backend.memory import db_housekeeping
from backend.memory.embedding_migration import _count_pending_in_db


def _side_files(db):
    return sorted(p.name for p in db.parent.glob(db.name + "-*"))


def _make_wal_db(db):
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("CREATE TABLE kv_store(namespace TEXT, key TEXT, value TEXT)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()


def _crash_leaving_wal(db, rows):
    """子プロセスで書き込み中に os._exit → 未転記 -wal と -shm が残る(実クラッシュ相当)."""
    code = (
        "import sqlite3, os, sys\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "for i in range(int(sys.argv[2])):\n"
        "    c.execute(\"INSERT INTO kv_store VALUES('ns', 'k'||?, 'v')\", (i,))\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(db), str(rows)], check=True)


def test_release_replays_crash_wal_and_retires_side_files(tmp_path):
    db = tmp_path / "c.db"
    _make_wal_db(db)
    _crash_leaving_wal(db, 50)
    assert _side_files(db) == ["c.db-shm", "c.db-wal"]
    assert os.path.getsize(db.parent / "c.db-wal") > 0  # 未転記データあり

    assert db_housekeeping.release_wal_side_files(str(db)) == "released"

    assert _side_files(db) == []  # 付随ファイルは SQLite が片付けた
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT count(*) FROM kv_store").fetchone()[0] == 50  # 記憶は無傷
    finally:
        conn.close()
    assert _side_files(db) == []


def test_release_leaves_empty_and_garbage_files_untouched(tmp_path):
    empty = tmp_path / "e.db"
    empty.write_bytes(b"")
    assert db_housekeeping.release_wal_side_files(str(empty)) == "released"
    assert empty.stat().st_size == 0  # 未アクティベートの 0 バイト .db はそのまま
    assert _side_files(empty) == []

    garbage = tmp_path / "g.db"
    garbage.write_bytes(b"not a database" * 100)
    assert db_housekeeping.release_wal_side_files(str(garbage)) == "error"
    assert garbage.read_bytes() == b"not a database" * 100  # 壊れたファイルは無変更

    assert db_housekeeping.release_wal_side_files(str(tmp_path / "nope.db")) == "missing"
    assert not (tmp_path / "nope.db").exists()  # 無い DB を作らない


def test_sweep_walks_every_character_db(tmp_path, monkeypatch):
    dbs = []
    for name in ("a", "b"):
        db = tmp_path / f"{name}.db"
        _make_wal_db(db)
        _crash_leaving_wal(db, 3)
        dbs.append(db)
    monkeypatch.setattr(
        db_housekeeping, "iter_character_dbs",
        lambda: [("a", str(dbs[0])), ("b", str(dbs[1])), ("gone", str(tmp_path / "gone.db"))],
    )
    counts = db_housekeeping.sweep_wal_side_files()
    assert counts == {"released": 2, "missing": 1, "error": 0}
    for db in dbs:
        assert _side_files(db) == []


def test_embedding_scan_no_longer_leaves_side_files(tmp_path):
    # 以前は mode=ro で開いていた=作れるが消せず、起動のたび全キャラに 2 本残っていた
    db = tmp_path / "s.db"
    _make_wal_db(db)
    assert _side_files(db) == []
    assert _count_pending_in_db(str(db), "char", "prov::model") == 0
    assert _side_files(db) == []
