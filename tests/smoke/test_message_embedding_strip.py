"""Smoke tests for stripping embeddings from extraction-processed messages.

対象: backend/memory/memory_manager.py の _strip_extracted_embeddings。
メッセージ埋め込みの唯一の読み手は抽出時の重心計算＝抽出済み分は死蔵データ
(1件あたり本文の約200倍)。抽出成功後に剥がし、embedding_stripped_up_to
watermark で進捗を記録する(冪等・エラーは抽出の成否に影響させない)。
"""

import json

import numpy as np
import pytest

from backend.memory.memory_manager import MemoryManager

CHAR = "test_char"
DIM = 768  # DEFAULT_EMBEDDING_SIZE

EXTRACTION_JSON = json.dumps({
    "additions": [{"category": "user_fact", "content": "テスト用の記憶断片"}],
    "updates": [],
})


@pytest.fixture
def mm(tmp_path, monkeypatch):
    manager = MemoryManager(str(tmp_path / "mem.db"), CHAR)
    monkeypatch.setattr(manager, "_get_embedding",
                        lambda text: np.full(DIM, 0.5, dtype=np.float32))
    monkeypatch.setattr(manager, "_get_character_provider", lambda: "openai")
    monkeypatch.setattr(manager, "_call_extraction_llm",
                        lambda msgs, existing: EXTRACTION_JSON)
    return manager


def _messages(mm):
    return {it.key: it.value for it in mm.store.list((CHAR, "messages"))}


def _watermark(mm):
    item = mm.store.get((CHAR, "meta"), "embedding_stripped_up_to")
    return item.value["index"] if item else None


def test_extraction_strips_embeddings_but_preserves_content(mm):
    mm.add_message("user", "こんにちは")
    mm.add_message("assistant", "こんにちは!")

    # 抽出前: 埋め込みが実際に付いている(前提の実弾確認)
    before = _messages(mm)
    assert all(len(v["embedding"]) == DIM for v in before.values())

    mm.extract_memories()

    after = _messages(mm)
    assert set(after.keys()) == {"msg_1", "msg_2"}
    for key, value in after.items():
        assert "embedding" not in value          # 剥がれている
        assert value["content"] == before[key]["content"]    # 本文は無傷
        assert value["role"] == before[key]["role"]
        assert value["timestamp"] == before[key]["timestamp"]
        assert value["images"] == before[key]["images"]
    assert _watermark(mm) == mm.get_message_count()
    # 長期記憶(抽出結果)は普通に生まれている
    assert len(mm.store.list((CHAR, "memories"))) == 1


def test_unprocessed_messages_keep_embeddings(mm):
    mm.add_message("user", "こんにちは")
    mm.add_message("assistant", "こんにちは!")
    mm.extract_memories()

    # 抽出後に増えた未処理分は、剥がしを直接呼んでも保持される
    mm.add_message("user", "追加の未処理メッセージ")
    mm._strip_extracted_embeddings()

    msgs = _messages(mm)
    assert "embedding" not in msgs["msg_1"]
    assert "embedding" not in msgs["msg_2"]
    assert len(msgs["msg_3"]["embedding"]) == DIM


def test_partial_extraction_strips_only_extracted_range(mm):
    for i in range(4):
        mm.add_message("user", f"メッセージ{i + 1}")

    # 分割抽出の前半だけ成功した状態を再現(last_extracted=2)
    mm.store.put((CHAR, "meta"), "last_extracted", {"index": 2})
    mm._strip_extracted_embeddings()

    msgs = _messages(mm)
    assert "embedding" not in msgs["msg_1"]
    assert "embedding" not in msgs["msg_2"]
    assert len(msgs["msg_3"]["embedding"]) == DIM   # 再試行に必要＝残る
    assert len(msgs["msg_4"]["embedding"]) == DIM
    assert _watermark(mm) == 2


def test_strip_is_idempotent(mm):
    mm.add_message("user", "こんにちは")
    mm.extract_memories()

    snapshot = _messages(mm)
    mm._strip_extracted_embeddings()   # 2回目は no-op
    assert _messages(mm) == snapshot
    assert _watermark(mm) == mm.get_message_count()


def test_clear_paths_reset_watermark(mm):
    mm.add_message("user", "こんにちは")
    mm.extract_memories()
    assert _watermark(mm) == 1

    mm.clear_short_term_only()
    assert _watermark(mm) == 0

    # 消去後の新メッセージ(msg_1 から振り直し)も次の抽出で剥がされる
    mm.add_message("user", "リセット後のメッセージ")
    mm.extract_memories()
    msgs = _messages(mm)
    assert "embedding" not in msgs["msg_1"]
    assert _watermark(mm) == 1

    mm.clear_all_memory()
    assert _watermark(mm) == 0


def test_strip_error_is_contained(mm, monkeypatch):
    mm.add_message("user", "こんにちは")
    mm.store.put((CHAR, "meta"), "last_extracted", {"index": 1})

    def failing_get(namespace, key):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(mm.store, "get", failing_get)
    mm._strip_extracted_embeddings()   # 例外が漏れない
    assert mm.extraction_failures == 0  # サーキットブレーカーに計上しない
