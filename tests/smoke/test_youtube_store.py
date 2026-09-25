"""Smoke tests for backend/youtube/youtube_store.py — the pure persistence
and judgement logic of the YouTube comment auto-reply feature (spec §4/§6).

All file paths are redirected to tmp_path via monkeypatching the module
constants; no network, no LLM, no product-code bending.
"""

import pytest

from backend.youtube import youtube_store as store


@pytest.fixture(autouse=True)
def _redirect_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(store, "VIDEO_CONTEXT_FILE", tmp_path / "video_context.json")
    monkeypatch.setattr(store, "SESSION_STATE_FILE", tmp_path / "session_state.json")
    monkeypatch.setattr(store, "POST_QUEUE_FILE", tmp_path / "post_queue.json")


def _comment(cid, published_at, author="UCviewer000000000000000x", text="hi"):
    return {
        "comment_id": cid,
        "video_id": "vid1",
        "author_channel_id": author,
        "author_name": "viewer",
        "text": text,
        "published_at": published_at,
    }


# ---------------------------------------------------------------------------
# session_state: daily count read-time normalization
# ---------------------------------------------------------------------------

def test_daily_count_normalizes_on_stale_date():
    state = store.load_session_state()
    state["daily"] = {"date": "2000-01-01", "count": 30}
    # Stored date != today → treated as 0 (deadlock-free reset, spec §4 v5)
    assert store.get_daily_count(state) == 0
    store.increment_daily_count(state)
    assert store.get_daily_count(state) == 1
    store.increment_daily_count(state)
    assert store.get_daily_count(state) == 2


def test_processed_ids_fifo_cap():
    state = store.load_session_state()
    store.register_processed_ids(state, [f"c{i}" for i in range(store.PROCESSED_IDS_MAX + 20)])
    assert len(state["processed_ids"]) == store.PROCESSED_IDS_MAX
    # Oldest dropped, newest kept
    assert "c0" not in state["processed_ids"]
    assert f"c{store.PROCESSED_IDS_MAX + 19}" in state["processed_ids"]
    # Idempotent re-add
    store.register_processed_ids(state, [f"c{store.PROCESSED_IDS_MAX + 19}"])
    assert len(state["processed_ids"]) == store.PROCESSED_IDS_MAX


# ---------------------------------------------------------------------------
# Newness judgement (spec §4-3/4)
# ---------------------------------------------------------------------------

def test_filter_candidates_excludes_own_and_processed_and_old():
    state = store.load_session_state()
    state["baseline"] = "2026-07-10T00:00:00Z"
    state["processed_ids"] = ["seen"]
    own = "UCown0000000000000000000"
    comments = [
        _comment("new1", "2026-07-10T00:00:00Z"),          # same second as baseline → included (>=)
        _comment("new2", "2026-07-11T09:00:00Z"),          # newer → included
        _comment("old1", "2026-07-09T23:59:59Z"),          # older → excluded
        _comment("seen", "2026-07-11T10:00:00Z"),          # ledgered → excluded
        _comment("mine", "2026-07-11T10:00:00Z", author=own),  # own → excluded
    ]
    result = store.filter_candidates(comments, state, own)
    assert sorted(c["comment_id"] for c in result) == ["new1", "new2"]


def test_initial_baseline_ledgers_same_second_and_sets_boundary():
    state = store.load_session_state()
    comments = [
        _comment("a", "2026-07-11T08:00:00Z"),
        _comment("b", "2026-07-11T09:30:00Z"),
        _comment("c", "2026-07-11T09:30:00Z"),  # same second as newest
    ]
    store.record_initial_baseline(state, comments)
    assert state["baseline"] == "2026-07-11T09:30:00Z"
    # Both same-second comments ledgered so the >= judgement can't re-capture them
    assert set(state["processed_ids"]) == {"b", "c"}
    # Next session: only strictly-newer or未台帳 comments qualify
    nxt = store.filter_candidates(comments, state, "UCown")
    assert [c["comment_id"] for c in nxt] == []


def test_finalize_baseline_covers_nonselected():
    state = store.load_session_state()
    state["baseline"] = "2026-07-10T00:00:00Z"
    candidates = [
        _comment("s1", "2026-07-11T01:00:00Z"),
        _comment("n1", "2026-07-11T02:00:00Z"),  # not selected → 永久スキップ
    ]
    store.finalize_baseline(state, candidates)
    assert state["baseline"] == "2026-07-11T02:00:00Z"
    assert set(state["processed_ids"]) == {"s1", "n1"}


def test_select_targets_caps_at_ten():
    candidates = [_comment(f"c{i}", "2026-07-11T00:00:00Z") for i in range(25)]
    selected = store.select_targets(candidates)
    assert len(selected) == store.SELECTION_CAP
    assert len({c["comment_id"] for c in selected}) == store.SELECTION_CAP
    few = store.select_targets(candidates[:3])
    assert len(few) == 3


# ---------------------------------------------------------------------------
# Post queue state machine
# ---------------------------------------------------------------------------

def test_queue_roundtrip_and_status_transitions():
    item = store.make_queue_item(_comment("c1", "2026-07-11T00:00:00Z"), "こんにちは！")
    assert item["status"] == "generated"
    store.append_queue_item(item)

    store.update_queue_item("c1", status="posting")
    assert store.pending_queue_items(["posting"])[0]["comment_id"] == "c1"

    store.update_queue_item("c1", status="generated", retry_count=1)
    pending = store.pending_queue_items()
    assert pending[0]["retry_count"] == 1

    store.remove_queue_item("c1")
    assert store.load_queue() == []


# ---------------------------------------------------------------------------
# History (posted のみ・FIFO・チャンネルID照合)
# ---------------------------------------------------------------------------

def test_history_video_and_user_lookup():
    for i in range(7):
        store.append_history({
            "video_id": "vidA" if i % 2 == 0 else "vidB",
            "author_channel_id": "UCu1",
            "author_name": f"name{i}",  # display name changes; id stays
            "comment_id": f"c{i}",
            "comment_text": f"comment {i}",
            "reply_text": f"reply {i}",
        })
    vh = store.video_history("vidA", limit=2)
    assert [h["comment_id"] for h in vh] == ["c4", "c6"]
    uh = store.user_history("UCu1", limit=3)
    assert [h["comment_id"] for h in uh] == ["c4", "c5", "c6"]
    assert store.user_history("", limit=3) == []


def test_video_context_cache():
    assert store.get_video_context("v1") is None
    store.set_video_context("v1", "title", "desc")
    assert store.get_video_context("v1") == {"title": "title", "description": "desc"}
