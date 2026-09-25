"""Smoke tests for backend/youtube/youtube_session_manager.py — the posting
worker state machine (spec §4 投稿フェーズ・二重投稿防止) and the generation
phase pipeline (spec §4 生成フェーズ), with all network/LLM seams faked."""

import threading
from types import SimpleNamespace

import pytest

from backend.shared.youtube_state import YouTubeState
from backend.youtube import youtube_session_manager as ysm_mod
from backend.youtube import youtube_store as store
from backend.youtube.youtube_api import (
    AuthRequiredError, PermanentAPIError, QuotaExceededError, TransientAPIError,
)
from backend.youtube.youtube_session_manager import YouTubeSessionManager

OWN_ID = "UCown0000000000000000000"


@pytest.fixture(autouse=True)
def _redirect_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(store, "VIDEO_CONTEXT_FILE", tmp_path / "video_context.json")
    monkeypatch.setattr(store, "SESSION_STATE_FILE", tmp_path / "session_state.json")
    monkeypatch.setattr(store, "POST_QUEUE_FILE", tmp_path / "post_queue.json")


@pytest.fixture(autouse=True)
def _fast_cooldown(monkeypatch):
    monkeypatch.setattr(ysm_mod.random, "uniform", lambda a, b: 0.01)


@pytest.fixture
def settings():
    return {
        "enabled": True, "character_id": "char1",
        "target_channel_id": "UCtarget",
        "interval_seconds": 3600, "cooldown_min": 1, "cooldown_max": 1,
        "daily_post_limit": 30, "rules": "", "dry_run": False,
    }


@pytest.fixture
def manager(monkeypatch, settings):
    state = SimpleNamespace(
        server_mode=False, conversation_active=False,
        elyth_session_active=False, memory_managers={},
        queue_manager=None,
    )
    mgr = YouTubeSessionManager.__new__(YouTubeSessionManager)
    mgr.state = state
    mgr.yt = YouTubeState()  # fresh, not the process singleton
    mgr._stop_event = threading.Event()
    mgr._scheduler_thread = None
    mgr._worker_thread = None
    mgr._timer_elapsed = 0.0
    mgr._last_tick = 0.0
    mgr._waiting_reason = ""
    mgr._last_session_summary = ""
    mgr._last_comment_activity = None
    mgr._session_done = 0
    mgr._session_total = 0
    mgr._settings = dict(settings)
    monkeypatch.setattr(YouTubeSessionManager, "_load_settings",
                        lambda self: dict(settings))
    monkeypatch.setattr(YouTubeSessionManager, "_broadcast_status_update",
                        lambda self, event="status": None)
    monkeypatch.setattr(ysm_mod.auth, "get_authorized_channel",
                        lambda: {"channel_id": OWN_ID, "channel_title": "Sara"})
    return mgr


def _comment(cid, published_at="2026-07-11T01:00:00Z"):
    return {
        "comment_id": cid, "video_id": "vid1",
        "author_channel_id": "UCviewer", "author_name": "viewer",
        "text": f"comment {cid}", "published_at": published_at,
    }


def _enqueue_generated(cid, reply="返信です"):
    store.append_queue_item(store.make_queue_item(_comment(cid), reply))


# ---------------------------------------------------------------------------
# Worker: main posting loop (spec §4-8 エラー3分類)
# ---------------------------------------------------------------------------

def test_worker_posts_and_confirms(manager, monkeypatch, settings):
    _enqueue_generated("c1")
    posted = []
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply",
                        lambda cid, text: posted.append((cid, text)) or {})
    end = manager._run_worker(settings)
    assert end == "queue_empty"
    assert posted == [("c1", "返信です")]
    assert store.load_queue() == []                      # posted → 掃除済み
    history = store.load_history()
    assert len(history) == 1 and history[0]["comment_id"] == "c1"
    state = store.load_session_state()
    assert store.get_daily_count(state) == 1
    assert state["posts_since_extraction"] == 1
    assert "c1" in state["processed_ids"]                # 冪等台帳追加


def test_worker_quota_exceeded_reverts_and_stops(manager, monkeypatch, settings):
    _enqueue_generated("c1")

    def _raise(cid, text):
        raise QuotaExceededError("quota", reason="quotaExceeded")
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply", _raise)
    end = manager._run_worker(settings)
    assert end == "quota_exceeded"
    items = store.load_queue()
    assert items[0]["status"] == "generated"             # 戻してキュー保持
    assert store.load_history() == []


def test_worker_permanent_error_skips_without_blocking(manager, monkeypatch, settings):
    _enqueue_generated("c1")
    _enqueue_generated("c2")
    calls = []

    def _insert(cid, text):
        calls.append(cid)
        if cid == "c1":
            raise PermanentAPIError("gone", reason="commentNotFound")
        return {}
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply", _insert)
    end = manager._run_worker(settings)
    assert end == "queue_empty"
    assert calls == ["c1", "c2"]                         # キューを詰まらせない
    state = store.load_session_state()
    assert "c1" in state["processed_ids"] and "c2" in state["processed_ids"]
    assert [h["comment_id"] for h in store.load_history()] == ["c2"]


def test_worker_transient_retry_exhaustion(manager, monkeypatch, settings):
    _enqueue_generated("c1")

    def _raise(cid, text):
        raise TransientAPIError("network")
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply", _raise)
    end = manager._run_worker(settings)
    assert end == "queue_empty"
    assert store.load_queue() == []                      # retry_exhausted で掃除
    assert "c1" in store.load_session_state()["processed_ids"]
    assert store.load_history() == []


def test_worker_auth_error_keeps_posting_status(manager, monkeypatch, settings):
    _enqueue_generated("c1")

    def _raise(cid, text):
        raise AuthRequiredError("invalid_grant", reason="invalid_grant")
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply", _raise)
    end = manager._run_worker(settings)
    assert end == "auth_error"
    assert manager.yt.auth_error is True
    # posting のまま保持 → 次回ワーカーの回収機構が実測検証する
    assert store.load_queue()[0]["status"] == "posting"


def test_worker_daily_limit_stops_before_posting(manager, monkeypatch, settings):
    _enqueue_generated("c1")
    state = store.load_session_state()
    for _ in range(settings["daily_post_limit"]):
        store.increment_daily_count(state)
    store.save_session_state(state)
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply",
                        lambda cid, text: pytest.fail("must not post"))
    end = manager._run_worker(settings)
    assert end == "daily_limit"
    assert store.load_queue()[0]["status"] == "generated"


# ---------------------------------------------------------------------------
# Worker: posting-orphan recovery (spec §4-7 二重投稿防止)
# ---------------------------------------------------------------------------

def test_recovery_confirms_posted_orphan(manager, monkeypatch, settings):
    _enqueue_generated("c1")
    store.update_queue_item("c1", status="posting")
    monkeypatch.setattr(ysm_mod.youtube_api, "list_reply_author_channel_ids",
                        lambda cid: ["UCother", OWN_ID])
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply",
                        lambda cid, text: pytest.fail("must not re-post"))
    end = manager._run_worker(settings)
    assert end == "queue_empty"
    assert [h["comment_id"] for h in store.load_history()] == ["c1"]
    assert store.load_queue() == []


def test_recovery_reverts_unposted_orphan_and_posts(manager, monkeypatch, settings):
    _enqueue_generated("c1")
    store.update_queue_item("c1", status="posting")
    monkeypatch.setattr(ysm_mod.youtube_api, "list_reply_author_channel_ids",
                        lambda cid: [])                   # 返信なし → 未投稿
    posted = []
    monkeypatch.setattr(ysm_mod.youtube_api, "insert_reply",
                        lambda cid, text: posted.append(cid) or {})
    end = manager._run_worker(settings)
    assert end == "queue_empty"
    assert posted == ["c1"]                               # 通常の投稿順で再投稿


def test_recovery_verification_failure_keeps_posting(manager, monkeypatch, settings):
    _enqueue_generated("c1")
    store.update_queue_item("c1", status="posting")

    def _raise(cid):
        raise TransientAPIError("network")
    monkeypatch.setattr(ysm_mod.youtube_api, "list_reply_author_channel_ids", _raise)
    end = manager._run_worker(settings)
    assert end == "verify_failed"
    # 「確認できなかった→再投稿」に倒さない: posting のまま保持 (spec §4)
    assert store.load_queue()[0]["status"] == "posting"


# ---------------------------------------------------------------------------
# Generation phase (spec §4 生成フェーズ)
# ---------------------------------------------------------------------------

class _FakeReplyLLM:
    def __init__(self, replies):
        self._replies = list(replies)

    def invoke(self, messages):
        return SimpleNamespace(content=self._replies.pop(0), usage_metadata=None)


class _NoLog:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _prep_generation(manager, monkeypatch, comments, replies,
                     token_ok=True):
    monkeypatch.setattr(ysm_mod.auth, "get_access_token",
                        lambda force_refresh=False: (
                            {"success": True, "token": "t"} if token_ok else
                            {"success": False, "error": "invalid_grant",
                             "invalid_grant": True}))
    monkeypatch.setattr(ysm_mod.youtube_api, "fetch_latest_comments",
                        lambda ch, mr, mv: list(comments))
    monkeypatch.setattr(ysm_mod.youtube_api, "fetch_video_info",
                        lambda vid: {"title": "T", "description": "D"})
    monkeypatch.setattr(YouTubeSessionManager, "_load_character_config",
                        lambda self, cid: {"character_id": cid, "name": "Sara",
                                           "youtube_system_prompt": "私はサラ。"})
    monkeypatch.setattr(YouTubeSessionManager, "_create_llm",
                        lambda self, config: _FakeReplyLLM(replies))
    monkeypatch.setattr(YouTubeSessionManager, "_start_worker",
                        lambda self, s: True)


def test_generation_initial_baseline_no_replies(manager, monkeypatch, settings):
    comments = [_comment("c1", "2026-07-11T01:00:00Z"),
                _comment("c2", "2026-07-11T02:00:00Z")]
    _prep_generation(manager, monkeypatch, comments, [])
    end = manager._run_generation(settings, _NoLog())
    assert end == "initial_baseline"
    state = store.load_session_state()
    assert state["baseline"] == "2026-07-11T02:00:00Z"
    assert store.load_queue() == []                       # 返信せず終了


def test_generation_queues_replies_and_ledgers(manager, monkeypatch, settings):
    state = store.load_session_state()
    state["baseline"] = "2026-07-10T00:00:00Z"
    store.save_session_state(state)
    comments = [_comment("c1"), _comment("c2", "2026-07-11T02:00:00Z")]
    _prep_generation(manager, monkeypatch, comments, ["返信1", "{{DENY}}"])
    end = manager._run_generation(settings, _NoLog())
    assert end == "completed"
    queue = store.load_queue()
    assert len(queue) == 1 and queue[0]["status"] == "generated"
    state = store.load_session_state()
    # deny分もIDは台帳へ・基準点は新規判定該当全件の最新へ (spec §4-6)
    assert "c1" in state["processed_ids"] and "c2" in state["processed_ids"]
    assert state["baseline"] == "2026-07-11T02:00:00Z"
    assert state["session_count"] == 1


def test_generation_dry_run_persists_nothing(manager, monkeypatch, settings):
    state = store.load_session_state()
    state["baseline"] = "2026-07-10T00:00:00Z"
    store.save_session_state(state)
    settings = dict(settings, dry_run=True)
    _prep_generation(manager, monkeypatch, [_comment("c1")], ["返信1"])
    end = manager._run_generation(settings, _NoLog())
    assert end == "dry_run_completed"
    assert store.load_queue() == []
    state = store.load_session_state()
    assert state["processed_ids"] == []                   # 何度でも再実行できる
    assert state["baseline"] == "2026-07-10T00:00:00Z"


def test_generation_transient_fetch_interrupts_without_state_change(
        manager, monkeypatch, settings):
    state = store.load_session_state()
    state["baseline"] = "2026-07-10T00:00:00Z"
    store.save_session_state(state)
    _prep_generation(manager, monkeypatch, [], [])

    def _raise(ch, mr, mv):
        raise TransientAPIError("network")
    monkeypatch.setattr(ysm_mod.youtube_api, "fetch_latest_comments", _raise)
    end = manager._run_generation(settings, _NoLog())
    assert end == "interrupted"
    assert store.load_session_state()["baseline"] == "2026-07-10T00:00:00Z"


def test_generation_auth_error_transition(manager, monkeypatch, settings):
    _prep_generation(manager, monkeypatch, [], [], token_ok=False)
    end = manager._run_generation(settings, _NoLog())
    assert end == "auth_error"
    assert manager.yt.auth_error is True


def test_generation_drains_existing_queue_first(manager, monkeypatch, settings):
    _enqueue_generated("c9")
    started = []
    monkeypatch.setattr(YouTubeSessionManager, "_start_worker",
                        lambda self, s: started.append(True) or True)
    monkeypatch.setattr(ysm_mod.auth, "get_access_token",
                        lambda force_refresh=False: {"success": True, "token": "t"})
    monkeypatch.setattr(YouTubeSessionManager, "_load_character_config",
                        lambda self, cid: {"character_id": cid, "name": "Sara"})
    monkeypatch.setattr(
        ysm_mod.youtube_api, "fetch_latest_comments",
        lambda *a: pytest.fail("must not fetch while queue has residue"))
    end = manager._run_generation(settings, _NoLog())
    assert end == "drain_queue"
    assert started == [True]


# ---------------------------------------------------------------------------
# Daily limit blocks BEFORE fetch/generation (稜指示 2026-08-11)
# ---------------------------------------------------------------------------

def _reach_daily_limit(settings):
    state = store.load_session_state()
    for _ in range(settings["daily_post_limit"]):
        store.increment_daily_count(state)
    store.save_session_state(state)


def test_generation_daily_limit_skips_fetch(manager, monkeypatch, settings):
    """上限到達日は取得も生成もしない: 投稿できないのに fetch＋最大10回の
    LLM 呼び出しを空撃ちしていた(旧挙動)ことの再発防止。"""
    _reach_daily_limit(settings)
    _prep_generation(manager, monkeypatch, [_comment("c1")], ["返信1"])
    monkeypatch.setattr(
        ysm_mod.youtube_api, "fetch_latest_comments",
        lambda *a: pytest.fail("must not fetch when the daily limit is reached"))
    monkeypatch.setattr(
        YouTubeSessionManager, "_create_llm",
        lambda self, config: pytest.fail("must not call the LLM when limited"))
    end = manager._run_generation(settings, _NoLog())
    assert end == "daily_limit"
    assert store.load_queue() == []
    # 基準点も台帳も進めない=翌日以降に判定し直す
    assert store.load_session_state()["baseline"] is None


def test_blocked_reason_daily_limit(manager, settings):
    """タイマー満了時も手動開始も、上限到達日はセッション自体を作らない。"""
    assert manager._blocked_reason() == ""
    _reach_daily_limit(settings)
    assert manager._blocked_reason() == "daily_limit"
    assert manager.start_session_manually() == {"success": False,
                                                "error": "daily_limit"}
    # セッションは1つも起動していない(enqueue されたら queue_manager=None で落ちる)
    assert manager.yt.session_active is False


# ---------------------------------------------------------------------------
# Extraction response parser
# ---------------------------------------------------------------------------

def test_parse_extraction_response_variants():
    parse = YouTubeSessionManager._parse_extraction_response
    assert parse('{"memories": ["a", "b"]}') == ["a", "b"]
    assert parse('```json\n{"memories": []}\n```') == []
    assert parse('前置き {"memories": ["x"]} 後置き') == ["x"]
    assert parse("garbage") is None
