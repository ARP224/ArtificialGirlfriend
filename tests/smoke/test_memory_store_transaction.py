"""Smoke tests for MemoryStore.transaction() and the atomicity of memory
extraction — fragments / memory_count / last_extracted must land in ONE
SQLite transaction (hard-kill: all-or-nothing, no duplicate re-extraction).

対象: backend/memory/memory_manager.py の MemoryStore.transaction と
_apply_extraction_results(断片+last_extracted の同時コミット)。
"""

import json

import pytest

from backend.memory.memory_manager import MemoryManager, MemoryStore

CHAR = "test_char"


# ---------------------------------------------------------------------------
# MemoryStore.transaction
# ---------------------------------------------------------------------------

def test_transaction_commits_all_writes(tmp_path):
    store = MemoryStore(str(tmp_path / "t.db"))
    with store.transaction():
        store.put(("ns",), "k1", {"v": 1})
        store.put(("ns",), "k2", {"v": 2})
    assert store.get(("ns",), "k1").value == {"v": 1}
    assert store.get(("ns",), "k2").value == {"v": 2}


def test_transaction_rolls_back_all_writes_on_error(tmp_path):
    store = MemoryStore(str(tmp_path / "t.db"))
    store.put(("ns",), "pre", {"v": 0})
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put(("ns",), "k1", {"v": 1})
            raise RuntimeError("boom")
    assert store.get(("ns",), "k1") is None   # ロールバック済み
    assert store.get(("ns",), "pre").value == {"v": 0}  # 既存データは無傷


def test_transaction_allows_inner_put_and_delete_without_deadlock(tmp_path):
    # _write_lock が RLock であることの実弾検証(plain Lock だとここで固まる)
    store = MemoryStore(str(tmp_path / "t.db"))
    store.put(("ns",), "old", {"v": 0})
    with store.transaction():
        store.put(("ns",), "new", {"v": 1})
        store.delete(("ns",), "old")
    assert store.get(("ns",), "old") is None
    assert store.get(("ns",), "new").value == {"v": 1}


# ---------------------------------------------------------------------------
# Extraction atomicity (fragments + last_extracted in one transaction)
# ---------------------------------------------------------------------------

EXTRACTION_JSON = json.dumps({
    "additions": [{"category": "user_fact", "content": "テスト用の記憶断片"}],
    "updates": [],
})


@pytest.fixture
def mm(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryManager, "DISABLE_EMBEDDINGS", True)
    manager = MemoryManager(str(tmp_path / "mem.db"), CHAR)
    monkeypatch.setattr(manager, "_get_character_provider", lambda: "openai")
    monkeypatch.setattr(manager, "_get_related_existing_memories", lambda msgs: [])
    monkeypatch.setattr(manager, "_call_extraction_llm",
                        lambda msgs, existing: EXTRACTION_JSON)
    manager.add_message("user", "こんにちは")
    manager.add_message("assistant", "こんにちは!")
    return manager


def test_extraction_writes_fragments_and_last_extracted_together(mm):
    mm.extract_memories()

    memories = mm.store.list((CHAR, "memories"))
    assert len(memories) == 1
    assert memories[0].value["content"] == "テスト用の記憶断片"

    last = mm.store.get((CHAR, "meta"), "last_extracted")
    assert last is not None
    assert last.value["index"] == mm.get_message_count()


def test_extraction_failure_rolls_back_fragments_with_last_extracted(mm):
    # last_extracted の書き込みだけ失敗させる = 旧実装で「断片は残るが
    # マーカー未前進→次回再抽出=重複」だった狭間を再現
    original_put = mm.store.put

    def failing_put(namespace, key, value):
        if key == "last_extracted":
            raise RuntimeError("simulated kill window")
        return original_put(namespace, key, value)

    mm.store.put = failing_put
    try:
        mm.extract_memories()  # 内部で捕捉されFalse扱い(例外は漏れない)
    finally:
        mm.store.put = original_put

    # 全てロールバック: 断片ゼロ・マーカー未前進(=次回まるごとリトライ可能)
    # ※ last_extracted は __init__ が {"index": 0} で seed する(:569-570)ため
    #   「存在しない」ではなく「0 のまま前進していない」を確認する
    assert mm.store.list((CHAR, "memories")) == []
    assert mm.store.get((CHAR, "meta"), "last_extracted").value["index"] == 0
    assert mm.extraction_failures > 0  # サーキットブレーカーが計上している


def test_extraction_retry_after_failure_does_not_duplicate(mm):
    original_put = mm.store.put

    def failing_put(namespace, key, value):
        if key == "last_extracted":
            raise RuntimeError("simulated kill window")
        return original_put(namespace, key, value)

    mm.store.put = failing_put
    try:
        mm.extract_memories()
    finally:
        mm.store.put = original_put

    mm.extraction_failures = 0  # サーキットブレーカーを解除してリトライ
    mm.extract_memories()

    # リトライ後も断片は1件だけ=重複抽出なし
    assert len(mm.store.list((CHAR, "memories"))) == 1
    assert mm.store.get((CHAR, "meta"), "last_extracted").value["index"] == mm.get_message_count()


# ---------------------------------------------------------------------------
# updates whitelist — hallucinated ids must not overwrite fresh additions
# (2026-08-16 Mac 実機: existing=0 件なのに gemma3:4b が updates に mem_N を返し、
#  同一トランザクション内で先に採番された mem_1..N を上書きして 2 件消えた)
# ---------------------------------------------------------------------------

def _make_manager(tmp_path, monkeypatch, extraction_json, existing):
    monkeypatch.setattr(MemoryManager, "DISABLE_EMBEDDINGS", True)
    manager = MemoryManager(str(tmp_path / "mem.db"), CHAR)
    monkeypatch.setattr(manager, "_get_character_provider", lambda: "openai")
    monkeypatch.setattr(manager, "_get_related_existing_memories", lambda msgs: existing(manager))
    monkeypatch.setattr(manager, "_call_extraction_llm",
                        lambda msgs, existing: extraction_json)
    manager.add_message("user", "こんにちは")
    manager.add_message("assistant", "こんにちは!")
    return manager


def test_update_with_id_not_offered_to_model_becomes_addition(tmp_path, monkeypatch):
    # 既存記憶ゼロ(=モデルに見せた id は無い)のに updates が mem_1 を指す
    payload = json.dumps({
        "additions": [
            {"category": "user_fact", "content": "名前は稜"},
            {"category": "user_preference", "content": "パクチーが苦手"},
        ],
        "updates": [
            {"id": "mem_1", "content": "コーヒーは1日2杯まで", "category": "user_fact"},
        ],
    })
    mm = _make_manager(tmp_path, monkeypatch, payload, existing=lambda m: [])
    mm.extract_memories()

    memories = {it.key: it.value for it in mm.store.list((CHAR, "memories"))}
    # 件数 = additions + updates(振替) = 3。上書きなら 2 になる
    assert len(memories) == 3
    # 先に採番された mem_1 は addition のまま(上書きされていない)
    assert memories["mem_1"]["content"] == "名前は稜"
    # 幻覚 id の update は新規エントリとして残る
    assert "コーヒーは1日2杯まで" in {v["content"] for v in memories.values()}
    assert mm.store.get((CHAR, "meta"), "memory_count").value["count"] == 3


def test_update_with_offered_id_still_updates_in_place(tmp_path, monkeypatch):
    # 見せた id への update は従来どおり上書き(件数不変)
    payload = json.dumps({
        "additions": [],
        "updates": [
            {"id": "__SEED__", "content": "最近はコーヒーよりお茶を好む", "category": "user_preference"},
        ],
    })
    seeded = {}

    def existing(m):
        return [{"id": seeded["id"], "category": "user_preference", "content": "コーヒーが好き"}]

    monkeypatch.setattr(MemoryManager, "DISABLE_EMBEDDINGS", True)
    manager = MemoryManager(str(tmp_path / "mem.db"), CHAR)
    monkeypatch.setattr(manager, "_get_character_provider", lambda: "openai")
    res = manager.add_memory_manual("user_preference", "コーヒーが好き")
    assert res["success"]
    seeded["id"] = res["id"]
    monkeypatch.setattr(manager, "_get_related_existing_memories", existing)
    monkeypatch.setattr(manager, "_call_extraction_llm",
                        lambda msgs, ex: payload.replace("__SEED__", seeded["id"]))
    manager.add_message("user", "お茶にはまってる")
    manager.add_message("assistant", "いいね!")

    manager.extract_memories()

    memories = {it.key: it.value for it in manager.store.list((CHAR, "memories"))}
    assert len(memories) == 1
    assert memories[seeded["id"]]["content"] == "最近はコーヒーよりお茶を好む"
