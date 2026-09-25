"""
tests/smoke/test_elyth_aggregates.py

Aggregation-layer tests (spec v6 §5-6) driven by the live-probed fixtures in
tests/fixtures/elyth_v2/ plus synthetic items for what the test account
cannot produce (dm.messages_received, account.restriction_changed, long
threads). Covers: AG stable format, the notification whitelist, resolution
budgets + dedupe/cap suppression + auto-mark-read, the blocked-surface strip
(image_credits / capabilities / images[]), thread windowing over oldest-first
pagination, and graceful degrade of enrichment failures.
"""

import json
from pathlib import Path

import pytest

import backend.elyth.elyth_aggregates as agg
from backend.elyth.elyth_api import ElythAPIError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "elyth_v2"


def _fx(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"]


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------

class FakeClient:
    """Records calls; serves configured data per method."""

    def __init__(self, **data):
        self.data = data
        self.calls = []
        self.marked_read = []
        # post resolution: post_id -> thread_id override
        self.thread_map = data.get("thread_map", {})
        self.fail = set(data.get("fail", ()))  # method names that raise

    def _serve(self, method, default=None):
        self.calls.append(method)
        if method in self.fail:
            raise ElythAPIError(f"{method} failed")
        return self.data.get(method, default)

    def get_notifications(self, **kw):
        return self._serve("get_notifications", {"items": []})

    def get_information(self):
        return self._serve("get_information", {})

    def get_post(self, post_id):
        self.calls.append("get_post")
        if "get_post" in self.fail:
            raise ElythAPIError("get_post failed")
        return {"post": {
            "id": post_id,
            "thread_id": self.thread_map.get(post_id, f"thread-{post_id}"),
            "author": {"id": "a1", "display_name": "相手", "handle": "other"},
            "content": f"full text of {post_id}",
            "engagement": {"reply_count": 0, "like_count": 0, "liked_by_me": False},
            "images": [], "image_generation": None,
            "kind": "reply", "reply_to_id": "root",
            "created_at": "2026-08-14T23:00:00+00:00",
        }}

    def mark_notifications_read(self, notification_ids):
        self.calls.append("mark_notifications_read")
        self.marked_read.append(list(notification_ids))
        return {"received_count": len(notification_ids)}

    def get_timeline(self, limit=None, cursor=None):
        return self._serve("get_timeline", {"items": [], "page": {}})

    def get_thread(self, post_id, limit=None, cursor=None):
        self.calls.append("get_thread")
        pages = self.data["thread_pages"]
        idx = 0 if cursor is None else int(cursor)
        return pages[idx]

    def get_my_posts(self, limit=None):
        return self._serve("get_my_posts", {"items": [], "page": {}})

    def get_profile(self, ref):
        return self._serve("get_profile", {})

    def get_profile_posts(self, ref, limit=None):
        return self._serve("get_profile_posts", {"items": [], "page": {}})

    def search_posts(self, hashtag, limit=None, cursor=None):
        return self._serve("search_posts", {"items": [], "page": {}})

    def get_relationships(self, kind, limit=None, cursor=None):
        return self._serve("get_relationships", {"items": [], "page": {}})


@pytest.fixture
def no_state(monkeypatch):
    """Isolate from real thread-state files."""
    monkeypatch.setattr(agg, "get_sibling_handles", lambda cid: set())
    monkeypatch.setattr(agg, "get_thread_counts", lambda cid: {})


def _notif(nid, ntype, created="2026-08-10T00:00:00+00:00", **extra):
    n = {"id": nid, "type": ntype, "created_at": created,
         "actor": {"type": "aituber", "id": f"actor-{nid}",
                   "display_name": f"表示名{nid}", "handle": f"handle-{nid}"},
         "resource": {"type": "post", "id": f"post-{nid}"},
         "preview": {"text": "prev", "truncated": True}}
    n.update(extra)
    return n


# ---------------------------------------------------------------------------
# normalize_post
# ---------------------------------------------------------------------------

def test_normalize_post_full_and_image_strip():
    post = _fx("post_single.json")["post"]
    ag_post = agg.normalize_post(post)
    assert ag_post["id"] == post["id"]
    assert ag_post["thread_id"] == post["thread_id"]
    assert ag_post["author_handle"] == "nectalica"
    assert ag_post["content"] == post["content"]
    assert ag_post["has_image"] is False
    assert "images" not in ag_post and "image_generation" not in ag_post
    # local-time format, not raw ISO/UTC
    assert "T" not in ag_post["created_at"] and "+00:00" not in ag_post["created_at"]

    with_image = dict(post, images=[{"url": "https://x/y.png"}])
    assert agg.normalize_post(with_image)["has_image"] is True


def test_normalize_post_preview_content_dict():
    item = _fx("information.json")["timeline"]["items"][0]
    ag_post = agg.normalize_post(item)
    assert ag_post["content"] == item["content"]["text"]
    assert ag_post["truncated"] is True


# ---------------------------------------------------------------------------
# get_notifications aggregate
# ---------------------------------------------------------------------------

def test_notifications_whitelist_blocks_dm_image_account_unknown(no_state):
    items = [
        _notif("n1", "post.reply_received"),
        _notif("n2", "dm.messages_received"),
        _notif("n3", "image.generation_ready"),
        _notif("n4", "account.restriction_changed"),
        _notif("n5", "some.future_type"),
        _notif("n6", "relationship.follow_started"),
    ]
    client = FakeClient(get_notifications={"items": items},
                        get_information=_fx("information.json"))
    result, suppressed = agg.build_notifications_result(client, "char1", 3)
    shown_ids = {n["notification_id"] for n in result["notifications"]}
    assert shown_ids == {"n1", "n6"}
    # Blocked types never marked read — the owner still sees DMs on the web UI
    assert suppressed == [] and client.marked_read == []
    text = json.dumps(result, ensure_ascii=False)
    assert "dm." not in text and "image." not in text and "account." not in text


def test_notifications_reply_resolution_and_metrics_strip(no_state):
    items = [_notif("n1", "post.reply_received")]
    client = FakeClient(get_notifications={"items": items},
                        get_information=_fx("information.json"))
    result, _ = agg.build_notifications_result(client, "char1", 3)
    n = result["notifications"][0]
    assert n["notification_type"] == "reply"
    assert n["post_id"] == "post-n1"
    assert n["post_thread_id"] == "thread-post-n1"
    assert n["post_content"] == "full text of post-n1"
    # author comes from the resolved post (canonical), not the notification actor
    assert n["post_author_handle"] == "other"
    # metrics: glyph stays, image_credits stripped
    assert result["my_metrics"]["glyph_balance"] == 746
    assert "image_credits" not in result["my_metrics"]
    # capabilities never reach the LLM view
    assert "capabilities" not in json.dumps(result)


def test_notifications_dedupe_cap_and_automark(no_state, monkeypatch):
    monkeypatch.setattr(agg, "get_thread_counts",
                        lambda cid: {"capped-thread": 3})
    items = [
        _notif("new", "post.reply_received", created="2026-08-14T00:00:00+00:00"),
        _notif("old", "post.reply_received", created="2026-08-13T00:00:00+00:00"),
        _notif("cap", "post.reply_received", created="2026-08-12T00:00:00+00:00"),
    ]
    client = FakeClient(
        get_notifications={"items": items},
        get_information=_fx("information.json"),
        thread_map={"post-new": "shared-thread", "post-old": "shared-thread",
                    "post-cap": "capped-thread"},
    )
    result, suppressed = agg.build_notifications_result(client, "char1", 3)
    shown = {n["notification_id"] for n in result["notifications"]}
    assert shown == {"new"}                # newest per thread wins; capped dropped
    assert set(suppressed) == {"old", "cap"}
    assert client.marked_read == [["old", "cap"]]  # auto-marked read


def test_notifications_sibling_exempt_from_cap(no_state, monkeypatch):
    monkeypatch.setattr(agg, "get_thread_counts",
                        lambda cid: {"capped-thread": 99})
    monkeypatch.setattr(agg, "get_sibling_handles", lambda cid: {"handle-sib"})
    n = _notif("sib", "post.reply_received")
    n["actor"]["handle"] = "handle-sib"
    client = FakeClient(get_notifications={"items": [n]},
                        get_information=_fx("information.json"),
                        thread_map={"post-sib": "capped-thread"})
    result, suppressed = agg.build_notifications_result(client, "char1", 3)
    assert [x["notification_id"] for x in result["notifications"]] == ["sib"]
    assert suppressed == []


def test_notifications_adoption_budget_and_backlog(no_state):
    items = [_notif(f"n{i}", "post.reply_received",
                    created=f"2026-08-{14 - i // 10:02d}T{i % 10:02d}:00:00+00:00")
             for i in range(15)]
    info = _fx("information.json")  # counts_by_type: reply=76
    client = FakeClient(get_notifications={"items": items},
                        get_information=info)
    result, suppressed = agg.build_notifications_result(client, "char1", 3)
    replies = [n for n in result["notifications"]
               if n["notification_type"] == "reply"]
    assert len(replies) == agg.MAX_ADOPTED_REPLIES  # 10
    assert suppressed == []
    assert result["unread_reply_backlog"] == 76 - 10  # honest remaining work


def test_notifications_announcement_cap(no_state):
    fixture_items = _fx("notifications.json")["items"]  # 5 announcements + replies
    client = FakeClient(get_notifications={"items": fixture_items},
                        get_information=_fx("information.json"))
    result, _ = agg.build_notifications_result(client, "char1", 3)
    anns = [n for n in result["notifications"]
            if n["notification_type"] == "announcement"]
    assert len(anns) == agg.MAX_ANNOUNCEMENTS
    assert result["announcements_omitted"] == 2
    assert anns[0]["title"] and anns[0]["summary"]


def test_notifications_information_failure_degrades(no_state):
    client = FakeClient(get_notifications={"items": [_notif("n1", "post.reply_received")]},
                        fail={"get_information"})
    result, _ = agg.build_notifications_result(client, "char1", 3)
    assert result["notifications"][0]["post_content"]
    assert "my_metrics" not in result
    assert "my_metrics_unavailable" in result["warnings"]


# ---------------------------------------------------------------------------
# get_timeline aggregate
# ---------------------------------------------------------------------------

def test_timeline_full_text_plus_information_extras(no_state):
    client = FakeClient(get_timeline=_fx("timeline.json"),
                        get_information=_fx("information.json"))
    result = agg.build_timeline_result(client, 3)
    assert len(result["timeline"]) == 3
    assert result["timeline"][0]["content"].endswith("🌿")  # full text, not preview
    assert result["timeline"][0]["thread_id"]
    assert result["post_count_last_hour"] == 7
    assert len(result["trends"]["posts"]) == 5
    assert result["trends"]["posts"][0]["truncated"] is True
    assert result["today_topic"] is None
    assert result["service_status"] == "operational"
    text = json.dumps(result)
    assert "capabilities" not in text and "image_credits" not in text


def test_timeline_information_failure_degrades(no_state):
    client = FakeClient(get_timeline=_fx("timeline.json"),
                        fail={"get_information"})
    result = agg.build_timeline_result(client, 3)
    assert len(result["timeline"]) == 3
    assert result["warnings"] == ["information_unavailable"]


# ---------------------------------------------------------------------------
# get_thread windowing
# ---------------------------------------------------------------------------

def _thread_page(root, replies, has_more, next_cursor):
    return {"thread": {"root": root, "replies": replies},
            "page": {"has_more": has_more, "next_cursor": next_cursor}}


def _reply(i):
    return {"id": f"r{i}", "thread_id": "t", "content": f"reply {i}",
            "author": {"handle": f"h{i}"}, "engagement": {},
            "created_at": "2026-08-14T00:00:00+00:00"}


def test_thread_single_page_matches_fixture(no_state):
    client = FakeClient(thread_pages=[_fx("post_thread.json")])
    result = agg.build_thread_result(client, "b5421e6a")
    assert len(result["posts"]) == 3  # root + 2 replies
    assert result["posts"][0]["author_handle"] == "nectalica"
    assert result["omitted_count"] == 0
    assert "tail_may_be_missing" not in result


def test_thread_windowing_keeps_root_plus_latest_5(no_state):
    root = _reply("root")
    pages = [
        _thread_page(root, [_reply(i) for i in range(50)], True, "1"),
        _thread_page(None, [_reply(50 + i) for i in range(8)], False, None),
    ]
    client = FakeClient(thread_pages=pages)
    result = agg.build_thread_result(client, "p")
    assert len(result["posts"]) == 1 + agg.THREAD_TAIL_KEEP
    assert result["posts"][0]["id"] == "rroot"
    assert [p["id"] for p in result["posts"][1:]] == ["r53", "r54", "r55", "r56", "r57"]
    assert result["omitted_count"] == 58 - agg.THREAD_TAIL_KEEP


def test_thread_walk_limit_flags_missing_tail(no_state):
    pages = [_thread_page({"id": "root", "content": "r"} if i == 0 else None,
                          [_reply(i)], True, str(i + 1))
             for i in range(agg.THREAD_PAGE_WALK_LIMIT + 5)]
    client = FakeClient(thread_pages=pages)
    result = agg.build_thread_result(client, "p")
    assert result["tail_may_be_missing"] is True
    assert client.calls.count("get_thread") == agg.THREAD_PAGE_WALK_LIMIT


# ---------------------------------------------------------------------------
# Other aggregates
# ---------------------------------------------------------------------------

def test_aituber_recomposes_profile_and_posts(no_state):
    client = FakeClient(get_profile=_fx("profile.json"),
                        get_profile_posts=_fx("profile_posts.json"))
    result = agg.build_aituber_result(client, "nectalica")
    prof = result["profile"]
    assert prof["handle"] == "nectalica"
    assert prof["follower_count"] == 42
    assert prof["following"] is True and prof["mutual"] is False
    assert prof["is_live"] is False
    assert result["posts"] and result["posts"][0]["content"]


def test_aituber_posts_failure_degrades(no_state):
    client = FakeClient(get_profile=_fx("profile.json"),
                        fail={"get_profile_posts"})
    result = agg.build_aituber_result(client, "nectalica")
    assert result["profile"]["handle"] == "nectalica"
    assert result["posts"] == []
    assert result["warnings"] == ["posts_unavailable"]


def test_relationships_shape(no_state):
    client = FakeClient(get_relationships=_fx("relationships_followers.json"))
    result = agg.build_relationships_result(client, "followers")
    assert result["kind"] == "followers"
    entry = result["aitubers"][0]
    assert entry["handle"] == "ruina"
    assert entry["follows_me"] is True and entry["following"] is False
    assert result["has_more"] is False


def test_search_result_shape(no_state):
    client = FakeClient(search_posts=_fx("timeline.json"))
    result = agg.build_search_result(client, "#流星群")
    assert result["hashtag"] == "流星群"
    assert len(result["posts"]) == 3


def test_mark_read_chunked_splits_at_100(no_state):
    client = FakeClient()
    ids = [f"id{i}" for i in range(250)]
    result = agg.mark_read_chunked(client, ids)
    assert [len(c) for c in client.marked_read] == [100, 100, 50]
    assert result["received_count"] == 250


def test_shape_write_result_normalizes_post():
    data = _fx("post_single.json")
    shaped = agg.shape_write_result(data)
    assert "images" not in shaped["post"]
    assert shaped["post"]["author_handle"] == "nectalica"
