"""Smoke tests for MemoryStore's connection registry (close_all / prune).

対象: backend/memory/memory_manager.py MemoryStore._register_conn / close_all。
終了時(shutdown_backend)とキャラ削除(remove_character)が全接続を閉じることで
SQLite に WAL 付随ファイル(-wal/-shm)を片付けさせる、その前提を実弾で固定する:
- 他スレッドが開いた接続も close_all で閉じられ、閉じ切ると付随ファイルが消える
- close_all 後の使用は明示エラー(黙って再接続して .db を作り直さない)
- 所有スレッドが終了した接続は次の接続作成時に prune される(台帳が溜まらない)
"""

import sqlite3
import threading

import pytest

from backend.memory.memory_manager import MemoryStore


def _side_files(db_path):
    return sorted(p.name for p in db_path.parent.glob(db_path.name + "-*"))


def test_close_all_closes_other_threads_connections_and_retires_wal_files(tmp_path):
    db = tmp_path / "t.db"
    store = MemoryStore(str(db))
    hold = threading.Event()
    done = threading.Event()

    def worker():
        store.put(("ns",), "from_thread", {"v": 1})
        done.set()
        hold.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    assert done.wait(5)
    store.put(("ns",), "from_main", {"v": 2})
    assert _side_files(db) == ["t.db-shm", "t.db-wal"]  # WAL モードで開いている間は存在

    closed = store.close_all()
    assert closed == 2  # ワーカーの接続 + メインの接続
    assert _side_files(db) == []  # 最後の接続が閉じた=SQLite が片付けた

    hold.set()
    t.join(5)
    # データは無傷(別接続で読み直す)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT count(*) FROM kv_store").fetchone()[0]
    finally:
        conn.close()
    assert rows == 2
    assert _side_files(db) == []


def test_use_after_close_all_raises_instead_of_reconnecting(tmp_path):
    db = tmp_path / "t.db"
    store = MemoryStore(str(db))
    store.put(("ns",), "k", {"v": 1})
    store.close_all()
    with pytest.raises(sqlite3.ProgrammingError):
        store.put(("ns",), "k2", {"v": 2})
    # 別スレッドからも同じ(黙って新規接続を張らない)
    err = []

    def worker():
        try:
            store.get(("ns",), "k")
        except sqlite3.ProgrammingError as e:
            err.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert err, "closed store must refuse new connections from any thread"


def test_dead_thread_connections_are_pruned_on_next_connect(tmp_path):
    store = MemoryStore(str(tmp_path / "t.db"))
    for i in range(5):
        t = threading.Thread(target=lambda i=i: store.put(("ns",), f"k{i}", {"v": i}))
        t.start()
        t.join(5)
    # 5本の短命スレッドは終了済み。次の接続作成(別スレッド)で prune される
    t = threading.Thread(target=lambda: store.get(("ns",), "k0"))
    t.start()
    t.join(5)
    owners = [o for o, _ in store._conns]
    assert len(owners) <= 2  # main(初期化時の接続) + 直近スレッド(自分自身は残る)
    assert set(owners) <= {threading.main_thread(), t}  # 5本の死んだ接続は消えている
    store.close_all()
