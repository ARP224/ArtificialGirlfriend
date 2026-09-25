"""Smoke tests for pruning dead attachment references.

対象: backend/memory/memory_manager.py の prune_missing_attachment_refs。
ファイルを消す側(添付上限クリーンアップ・会話終了時のカメラ全消し)が
メッセージに残る参照も一緒に畳む(稜裁定 2026-08-20)。従来は「ファイル
だけ消して参照を残す」設計で、101個目の添付から壊れた参照が量産される
仕込みだった。

- 実在しないファイルへの画像参照だけが除去され、実在分は残る
- 文書は file_path キーだけ落ち、text/filename は温存
- 他フィールド(embedding 含む)は無傷・変更のないメッセージは書き戻さない
- 冪等・エラーは内部で握って呼出元に漏れない
"""

import numpy as np
import pytest

from backend.memory.memory_manager import MemoryManager

CHAR = "test_char"
DIM = 768  # DEFAULT_EMBEDDING_SIZE


@pytest.fixture
def mm(tmp_path, monkeypatch):
    manager = MemoryManager(str(tmp_path / "mem.db"), CHAR)
    monkeypatch.setattr(manager, "_get_embedding",
                        lambda text: np.full(DIM, 0.5, dtype=np.float32))
    monkeypatch.setattr(manager, "_get_character_provider", lambda: "openai")
    return manager


def _stored(mm, key):
    return mm.store.get((CHAR, "messages"), key).value


def _make_file(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def test_prunes_only_dead_image_refs(mm, tmp_path):
    alive = _make_file(tmp_path, "alive.jpg")
    dead = _make_file(tmp_path, "dead.jpg")
    mm.add_message("user", "画像2枚", images=[alive, dead])

    (tmp_path / "dead.jpg").unlink()
    changed = mm.prune_missing_attachment_refs()

    assert changed == 1
    stored = _stored(mm, "msg_1")
    assert stored["images"] == [alive]  # tmp_path はリポジトリ外＝素通し形
    # 他フィールドは無傷(embedding 含む)
    assert stored["content"] == "画像2枚"
    assert len(stored["embedding"]) == DIM


def test_prunes_dead_document_file_path_but_keeps_text(mm, tmp_path):
    doc_file = _make_file(tmp_path, "doc.txt")
    mm.add_message("user", "文書つき", documents=[
        {"filename": "doc.txt", "text": "本文", "char_count": 2, "file_path": doc_file}
    ])

    (tmp_path / "doc.txt").unlink()
    changed = mm.prune_missing_attachment_refs()

    assert changed == 1
    doc = _stored(mm, "msg_1")["documents"][0]
    assert "file_path" not in doc
    assert doc["text"] == "本文"
    assert doc["filename"] == "doc.txt"


def test_untouched_messages_are_not_rewritten(mm, tmp_path):
    alive = _make_file(tmp_path, "alive.jpg")
    mm.add_message("user", "生きてる参照", images=[alive])
    mm.add_message("assistant", "添付なし")

    snapshot = {k: _stored(mm, k) for k in ("msg_1", "msg_2")}
    changed = mm.prune_missing_attachment_refs()

    assert changed == 0
    assert {k: _stored(mm, k) for k in ("msg_1", "msg_2")} == snapshot


def test_prune_is_idempotent(mm, tmp_path):
    dead = _make_file(tmp_path, "dead.jpg")
    mm.add_message("user", "死ぬ参照", images=[dead])
    (tmp_path / "dead.jpg").unlink()

    assert mm.prune_missing_attachment_refs() == 1
    snapshot = _stored(mm, "msg_1")
    assert mm.prune_missing_attachment_refs() == 0  # 2回目は no-op
    assert _stored(mm, "msg_1") == snapshot


def test_prune_error_is_contained(mm, tmp_path, monkeypatch):
    dead = _make_file(tmp_path, "dead.jpg")
    mm.add_message("user", "エラー封じ込め", images=[dead])
    (tmp_path / "dead.jpg").unlink()

    def failing_get(namespace, key):
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(mm.store, "get", failing_get)
    assert mm.prune_missing_attachment_refs() == 0  # 例外が漏れない
