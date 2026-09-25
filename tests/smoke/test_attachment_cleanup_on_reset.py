"""Smoke tests: 履歴クリア/会話リセットで添付の実体ファイルも消える。

対象: backend/conversation_manager.py の remove_attachment_files と
reset_conversation への配線。メッセージ(=参照側)を全消しする経路が
実体ファイルを残すと、到達不能の孤児が永久に溜まる(旧 test4 の 17MB が
前例。稜裁定 2026-08-20=参照を捨てたら実体も捨てる)。
reset_short_term_history(backend.py) 側の配線は実機ゲートで確認する。
"""

import threading

import backend.conversation_manager as cmod
from backend.conversation_manager import ConversationManager, remove_attachment_files

CID = "11111111-2222-3333-4444-555555555555"


def _seed_attachments(root):
    d = root / CID / "images"
    d.mkdir(parents=True)
    (d / "a.jpg").write_bytes(b"jpg")
    (root / CID / "documents").mkdir()
    (root / CID / "documents" / "a.txt").write_text("doc")


def test_remove_attachment_files_deletes_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(cmod, "ATTACHMENTS_DIR", tmp_path)
    _seed_attachments(tmp_path)

    remove_attachment_files(CID)

    assert not (tmp_path / CID).exists()


def test_remove_attachment_files_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(cmod, "ATTACHMENTS_DIR", tmp_path)
    remove_attachment_files(CID)  # 例外を出さない
    assert not (tmp_path / CID).exists()


class _FakeState:
    def __init__(self):
        self.memory_managers = {}
        self.memory_lock = threading.Lock()
        self.short_term_buffer = {CID: [{"role": "user", "content": "x"}]}
        self.message_count_cache = {CID: 1}


def test_reset_conversation_removes_attachment_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cmod, "ATTACHMENTS_DIR", tmp_path)
    _seed_attachments(tmp_path)
    db_file = tmp_path / f"{CID}.db"
    db_file.write_bytes(b"")

    manager = ConversationManager(
        _FakeState(),
        character_config_loader=lambda cid: {
            "success": True, "result": {"db_file_path": str(db_file)}},
        character_activator=lambda cid: {"success": True},
        find_config_file_func=lambda cid: f"{cid}.json",
    )
    monkeypatch.setattr(manager, "start_conversation", lambda cid: {"success": True})

    result = manager.reset_conversation(CID)

    assert result["success"]
    assert not (tmp_path / CID).exists()   # 添付フォルダも消えた
    assert not db_file.exists()            # 従来どおり .db も消えている
