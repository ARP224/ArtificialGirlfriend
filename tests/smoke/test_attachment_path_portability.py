"""Smoke tests for attachment path portability (repo-relative storage).

対象: backend/memory/memory_manager.py の add_message / get_recent_messages。
添付パスは保存時に repo 相対化・読出時に絶対化する(キャラ設定の
_map_config_paths と同じ正準形)。過去 DB のパスがインストールフォルダの
移動/改名で全滅した事故(2026-08-19 実測 102/107)の恒久修正。

- リポジトリ内の絶対パス → 保存形は repo 相対 POSIX → 読出形は絶対(往復)
- リポジトリ外の絶対パスは素通し(resolve_data_path / to_repo_relative の仕様)
- 呼出元が渡した list / dict は書き換えない(コピー方式)
"""

from pathlib import Path

import numpy as np
import pytest

from backend.memory.memory_manager import MemoryManager
from backend.shared.constants import BASE_DIR

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


def test_in_repo_paths_round_trip(mm):
    abs_img = str(BASE_DIR / "character_data" / "attachments" / CHAR / "images" / "a.jpg")
    abs_doc = str(BASE_DIR / "character_data" / "attachments" / CHAR / "documents" / "d.txt")
    doc = {"filename": "d.txt", "text": "本文", "char_count": 2, "file_path": abs_doc}

    result = mm.add_message("user", "画像つき", images=[abs_img], documents=[doc])
    assert result["success"]

    # 保存形: repo 相対の POSIX (可搬)
    stored = _stored(mm, "msg_1")
    assert stored["images"] == ["character_data/attachments/test_char/images/a.jpg"]
    assert stored["documents"][0]["file_path"] == \
        "character_data/attachments/test_char/documents/d.txt"

    # 読出形: 絶対 (消費者は従来どおりの形を見る)
    msg = mm.get_recent_messages()[0]
    assert Path(msg["images"][0]) == Path(abs_img)
    assert Path(msg["documents"][0]["file_path"]) == Path(abs_doc)
    # 本文フィールドは無傷
    assert msg["documents"][0]["text"] == "本文"


def test_out_of_repo_absolute_passes_through(mm, tmp_path):
    outside = str(tmp_path / "outside.jpg")  # tmp_path はリポジトリ外
    mm.add_message("user", "外部パス", images=[outside])

    assert _stored(mm, "msg_1")["images"] == [outside]
    assert mm.get_recent_messages()[0]["images"] == [outside]


def test_caller_inputs_are_not_mutated(mm):
    abs_img = str(BASE_DIR / "character_data" / "x.jpg")
    images = [abs_img]
    doc = {"filename": "d", "text": "t", "char_count": 1,
           "file_path": str(BASE_DIR / "character_data" / "d.txt")}
    documents = [doc]

    mm.add_message("user", "ミューテーション確認", images=images, documents=documents)

    assert images == [abs_img]
    assert doc["file_path"] == str(BASE_DIR / "character_data" / "d.txt")


def test_no_attachments_unchanged(mm):
    mm.add_message("user", "添付なし")
    stored = _stored(mm, "msg_1")
    assert stored["images"] == []
    assert stored["documents"] == []
    assert mm.get_recent_messages()[0]["images"] == []
