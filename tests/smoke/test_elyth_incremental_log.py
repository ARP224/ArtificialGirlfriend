"""Smoke tests for ELYTH session-log incremental persistence (upsert) and
pending-based thread-reply accounting — the hard-kill resilience seams.

対象: backend/elyth/elyth_memory.py の save_session_log(upsert) と
backend/elyth/elyth_thread_state.py の add_pending/fold。cap 判定
(_load_thread_counts) が pending を絶対に数えないことが要点。
"""

import json

import pytest

from backend.elyth import elyth_memory as em
from backend.elyth import elyth_thread_state as ets

CHAR = "test_char"


@pytest.fixture(autouse=True)
def _redirect_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(em, "ELYTH_SESSION_DIR", tmp_path / "elyth_sessions")
    monkeypatch.setattr(ets, "ELYTH_THREAD_STATE_DIR", tmp_path / "elyth_thread_state")


def _load_sessions():
    path = em.ELYTH_SESSION_DIR / f"{CHAR}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["sessions"]


# ---------------------------------------------------------------------------
# Session log: upsert
# ---------------------------------------------------------------------------

def test_upsert_replaces_same_session_across_saves():
    session = em.create_session_data(CHAR)
    em.add_turn_to_session(session, "turn1")
    em.save_session_log(CHAR, session)
    em.add_turn_to_session(session, "turn2")
    em.save_session_log(CHAR, session)

    sessions = _load_sessions()
    assert len(sessions) == 1
    assert len(sessions[0]["turns"]) == 2


def test_new_session_appends_and_final_save_updates_end_reason():
    s1 = em.create_session_data(CHAR)
    em.save_session_log(CHAR, s1)
    s2 = em.create_session_data(CHAR)
    em.add_turn_to_session(s2, "s2-turn")
    em.save_session_log(CHAR, s2)

    sessions = _load_sessions()
    assert len(sessions) == 2
    # 進行中保存の初期ラベルは interrupted(=killされた回にだけ残る値)
    assert sessions[1]["end_reason"] == "interrupted"

    s2["end_reason"] = "natural"
    em.save_session_log(CHAR, s2)
    sessions = _load_sessions()
    assert len(sessions) == 2
    assert sessions[1]["end_reason"] == "natural"


def test_legacy_sessions_without_session_id_are_never_replaced():
    legacy = {"timestamp": "2026-01-01T00:00:00", "character_id": CHAR,
              "end_reason": "natural", "turns": []}
    em.save_session_log(CHAR, dict(legacy))
    em.save_session_log(CHAR, dict(legacy))  # 旧形式同士も置換しない=append
    new = em.create_session_data(CHAR)
    em.save_session_log(CHAR, new)
    em.save_session_log(CHAR, new)  # 新形式は upsert

    sessions = _load_sessions()
    assert len(sessions) == 3


def test_fifo_trim_keeps_last_sessions():
    for _ in range(em.MAX_SESSIONS_KEPT + 2):
        em.save_session_log(CHAR, em.create_session_data(CHAR))
    assert len(_load_sessions()) == em.MAX_SESSIONS_KEPT


def test_partial_session_loads_cleanly():
    session = em.create_session_data(CHAR)
    em.add_turn_to_session(session, "partial", [{"id": "t1", "name": "create_post",
                                                 "arguments": {}}],
                           [{"id": "t1", "name": "create_post", "content": "{}"}])
    em.save_session_log(CHAR, session)  # kill想定: end_reason=interrupted のまま

    loaded = em.load_session_logs(CHAR)
    assert len(loaded) == 1
    assert loaded[0]["end_reason"] == "interrupted"
    msgs = em.convert_session_to_messages(loaded[0], "openai", language="ja")
    assert msgs  # 変換もクラッシュしない


# ---------------------------------------------------------------------------
# Thread accounting: pending / fold
# ---------------------------------------------------------------------------

def test_pending_does_not_affect_cap_counts():
    ets.add_pending_thread_reply(CHAR, "th1")
    assert ets._load_thread_counts(CHAR) == {}  # cap判定値は不変
    assert ets._load_pending(CHAR) == ["th1"]


def test_fold_moves_pending_into_counts_once():
    ets.add_pending_thread_reply(CHAR, "th1")
    ets.add_pending_thread_reply(CHAR, "th1")  # 同一tidの再追加は no-op
    ets.add_pending_thread_reply(CHAR, "th2")
    ets.fold_pending_thread_replies(CHAR)

    assert ets._load_thread_counts(CHAR) == {"th1": 1, "th2": 1}
    assert ets._load_pending(CHAR) == []

    ets.fold_pending_thread_replies(CHAR)  # 空pendingの再foldは no-op
    assert ets._load_thread_counts(CHAR) == {"th1": 1, "th2": 1}


def test_crash_leftover_folded_at_next_session_start_without_double_count():
    # セッション1: リプライ後にハードkill(=末尾foldなし)
    ets.add_pending_thread_reply(CHAR, "th1")
    # セッション2開始: 残骸fold → 通常のリプライ → 末尾fold
    ets.fold_pending_thread_replies(CHAR)
    ets.add_pending_thread_reply(CHAR, "th1")
    ets.fold_pending_thread_replies(CHAR)

    # 2セッション分ちょうど: 二重加算なし
    assert ets._load_thread_counts(CHAR) == {"th1": 2}


def test_add_pending_preserves_existing_counts():
    ets.add_pending_thread_reply(CHAR, "th1")
    ets.fold_pending_thread_replies(CHAR)
    ets.add_pending_thread_reply(CHAR, "th2")  # 書き込みが counts を消さないこと
    assert ets._load_thread_counts(CHAR) == {"th1": 1}
    assert ets._load_pending(CHAR) == ["th2"]
