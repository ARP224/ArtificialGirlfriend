"""
backend/elyth_aggregates.py

AG stable-format aggregation layer over the ELYTH Agent API v2
(ELYTH integration spec v6 §5-6 — internal design doc, not part of the repository).

v2 split the v1 all-in-one endpoints, so one AG tool call = several HTTP
calls assembled here. The LLM and every downstream consumer (per-post RAG
extraction, thread-state accounting) read ONLY the AG format this module
emits — raw v2 JSON never passes through. Key names deliberately match the
v1 vocabulary (`notification_type: "reply"`, `post_thread_id`, `trends.posts`,
...) so the persisted thread state and the extraction code keep their
semantics across the migration.

Blocked-surface enforcement (稜裁定 2026-08-15): DM / Field / image posts are
gone at the tool table, and this layer additionally strips every field that
would advertise them — `image_credits`, `capabilities`, `images[]` (reduced
to a `has_image` bool), and all non-whitelisted notification types.
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.elyth.elyth_api import ElythAPIClient, ElythAPIError, ElythAuthError
from backend.elyth.elyth_thread_state import get_sibling_handles, get_thread_counts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Budgets (spec v6 §5.2 / §5.4)
# ---------------------------------------------------------------------------

MAX_ADOPTED_REPLIES = 10        # reply/mention notifications shown with full context
MAX_POST_RESOLUTIONS = 20       # GET /posts/{id} calls per get_notifications
MAX_ANNOUNCEMENTS = 3           # newest announcements shown per call
AGGREGATE_DEADLINE_SECONDS = 45.0
THREAD_PAGE_WALK_LIMIT = 10     # thread pages (oldest-first) walked per get_thread
THREAD_TAIL_KEEP = 5            # v1 parity: root + latest 5 replies
NOTIFICATIONS_FETCH_LIMIT = 50
MARK_READ_CHUNK = 100           # server-side cap per /notifications/read call

# v2 type -> AG-internal notification_type. Anything not listed here is
# invisible to the LLM (whitelist — unknown future types fail safe).
NOTIFICATION_TYPE_MAP = {
    "post.reply_received": "reply",
    "post.mention_received": "mention",
    "relationship.follow_started": "follow",
    "relationship.follow_ended": "follow_ended",
    "relationship.mutual_started": "mutual",
    "relationship.mutual_ended": "mutual_ended",
    "announcement.published": "announcement",
    "event.started": "event_started",
    "event.ended": "event_ended",
}
_THREADED_INTERNAL_TYPES = ("reply", "mention")


# ---------------------------------------------------------------------------
# Normalisation primitives
# ---------------------------------------------------------------------------

def to_local_time(iso_str: Any) -> str:
    """ISO8601 (UTC) -> local `YYYY-MM-DD HH:MM`, so tool results agree with
    the session prompt's local clock. Unparseable input passes through."""
    if not isinstance(iso_str, str) or not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_str


def _content_text(post: Dict[str, Any]) -> Tuple[str, bool]:
    """Post content as (text, truncated). /information previews use
    {"text", "truncated"} dicts; full endpoints use plain strings."""
    c = post.get("content")
    if isinstance(c, dict):
        return str(c.get("text") or ""), bool(c.get("truncated"))
    return str(c or ""), False


def normalize_post(post: Dict[str, Any]) -> Dict[str, Any]:
    """v2 Post -> AG post (spec v6 §5.1). Single choke point for stripping
    image payloads: `images[]` / `image_generation` become one `has_image`
    bool (the character cannot perceive image content — instructions say so).
    """
    if not isinstance(post, dict):
        return {}
    author = post.get("author") or {}
    engagement = post.get("engagement") or {}
    text, truncated = _content_text(post)
    out: Dict[str, Any] = {
        "id": post.get("id"),
        "thread_id": post.get("thread_id"),
        "author_id": author.get("id"),
        "author_name": author.get("display_name"),
        "author_handle": author.get("handle"),
        "content": text,
        "reply_count": engagement.get("reply_count"),
        "like_count": engagement.get("like_count"),
        "liked_by_me": engagement.get("liked_by_me"),
        "has_image": bool(post.get("images")) or bool(post.get("has_image")),
        "created_at": to_local_time(post.get("created_at")),
    }
    if truncated:
        out["truncated"] = True
    return out


def _normalize_page_posts(data: Dict[str, Any]) -> Tuple[List[Dict], bool]:
    items = data.get("items") or []
    page = data.get("page") or {}
    return ([normalize_post(p) for p in items if isinstance(p, dict)],
            bool(page.get("has_more")))


def shape_write_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a create/like response: normalize the returned post if present."""
    if isinstance(data, dict) and isinstance(data.get("post"), dict):
        return {"post": normalize_post(data["post"])}
    return data


# ---------------------------------------------------------------------------
# get_notifications (spec v6 §5.2)
# ---------------------------------------------------------------------------

def build_notifications_result(
    client: ElythAPIClient,
    character_id: str,
    reply_cap: int,
) -> Tuple[Dict[str, Any], List[str]]:
    """Assemble the AG notifications view.

    Returns (result_dict, suppressed_notification_ids). Suppressed ids are
    ALSO auto-marked read here (best effort); they are returned so the caller
    can log/trace. HTTP budget: 1 (notifications) + 1 (information) +
    <= MAX_POST_RESOLUTIONS (post resolution) < the 60/min rate limit.
    """
    deadline = time.monotonic() + AGGREGATE_DEADLINE_SECONDS
    warnings: List[str] = []

    raw = client.get_notifications(limit=NOTIFICATIONS_FETCH_LIMIT)
    items = [n for n in (raw.get("items") or []) if isinstance(n, dict)]

    # --- whitelist -----------------------------------------------------------
    visible: List[Dict[str, Any]] = []
    excluded: Dict[str, int] = {}
    for n in items:
        v2_type = str(n.get("type") or "")
        internal = NOTIFICATION_TYPE_MAP.get(v2_type)
        if internal is None:
            excluded[v2_type] = excluded.get(v2_type, 0) + 1
            if v2_type == "account.restriction_changed":
                logger.warning(
                    f"[ELYTH] account restriction changed for {character_id} "
                    f"(notification {n.get('id')}) — not shown to the LLM")
            continue
        visible.append({"v2": n, "internal": internal})
    if excluded:
        logger.info(f"[ELYTH] notifications excluded by whitelist: {excluded}")

    # --- metrics + per-type unread counts (enrichment; degrade on failure) ---
    metrics: Optional[Dict[str, Any]] = None
    unread_by_type: Dict[str, int] = {}
    try:
        info = client.get_information()
        self_metrics = ((info.get("self") or {}).get("metrics")) or {}
        metrics = {k: v for k, v in self_metrics.items() if k != "image_credits"}
        for row in ((info.get("notifications") or {}).get("counts_by_type")) or []:
            if isinstance(row, dict) and row.get("type"):
                unread_by_type[str(row["type"])] = int(row.get("count") or 0)
    except ElythAuthError:
        raise
    except ElythAPIError as e:
        logger.warning(f"[ELYTH] information enrichment failed: {e}")
        warnings.append("my_metrics_unavailable")

    # --- reply/mention resolution with suppression (newest first) ------------
    threaded = [v for v in visible if v["internal"] in _THREADED_INTERNAL_TYPES]
    others = [v for v in visible if v["internal"] not in _THREADED_INTERNAL_TYPES]
    threaded.sort(key=lambda v: str(v["v2"].get("created_at") or ""), reverse=True)

    sibling_handles = get_sibling_handles(character_id)
    counts = get_thread_counts(character_id)

    adopted: List[Dict[str, Any]] = []
    suppressed_ids: List[str] = []
    seen_threads: set = set()
    resolutions = 0
    unresolved = 0

    for v in threaded:
        n = v["v2"]
        if (len(adopted) >= MAX_ADOPTED_REPLIES
                or resolutions >= MAX_POST_RESOLUTIONS
                or time.monotonic() > deadline):
            unresolved += 1
            continue
        actor = n.get("actor") or {}
        resource = n.get("resource") or {}
        post_id = resource.get("id")
        if not post_id:
            unresolved += 1
            continue
        try:
            resolutions += 1
            post = client.get_post(str(post_id)).get("post") or {}
        except ElythAuthError:
            raise
        except ElythAPIError as e:
            logger.warning(f"[ELYTH] post resolution failed for {post_id}: {e}")
            unresolved += 1
            continue

        tid = post.get("thread_id")
        is_sibling = actor.get("handle") in sibling_handles
        if tid and not is_sibling:
            if counts.get(tid, 0) >= reply_cap:
                _append_id(suppressed_ids, n)
                continue
            if tid in seen_threads:  # dedupe: newest per thread wins
                _append_id(suppressed_ids, n)
                continue
            seen_threads.add(tid)
        ag_post = normalize_post(post)
        adopted.append({
            "notification_id": n.get("id"),
            "notification_type": v["internal"],
            "notification_created_at": to_local_time(n.get("created_at")),
            "post_id": ag_post.get("id"),
            "post_thread_id": ag_post.get("thread_id"),
            "post_author_id": ag_post.get("author_id"),
            "post_author_name": ag_post.get("author_name"),
            "post_author_handle": ag_post.get("author_handle"),
            "post_content": ag_post.get("content"),
            **({"post_has_image": True} if ag_post.get("has_image") else {}),
        })

    # --- non-threaded types ---------------------------------------------------
    shaped_others: List[Dict[str, Any]] = []
    announcements_shown = 0
    announcements_omitted = 0
    for v in others:
        n = v["v2"]
        internal = v["internal"]
        base = {
            "notification_id": n.get("id"),
            "notification_type": internal,
            "notification_created_at": to_local_time(n.get("created_at")),
        }
        if internal == "announcement":
            if announcements_shown >= MAX_ANNOUNCEMENTS:
                announcements_omitted += 1
                continue
            announcements_shown += 1
            details = n.get("details") or {}
            base["title"] = details.get("title")
            base["summary"] = details.get("summary")
        elif internal in ("event_started", "event_ended"):
            details = n.get("details")
            if isinstance(details, dict):
                base["details"] = details
        else:  # follow / mutual family — actor is the counterpart
            actor = n.get("actor") or {}
            base["aituber_id"] = actor.get("id")
            base["aituber_name"] = actor.get("display_name")
            base["aituber_handle"] = actor.get("handle")
        shaped_others.append(base)

    # --- backlog (honest count of unshown reply/mention work) ----------------
    total_threaded_unread = (
        unread_by_type.get("post.reply_received", 0)
        + unread_by_type.get("post.mention_received", 0)
    )
    if total_threaded_unread:
        backlog = max(0, total_threaded_unread - len(adopted) - len(suppressed_ids))
    else:
        backlog = unresolved

    # --- auto-mark suppressed (best effort, v1 parity) ------------------------
    if suppressed_ids:
        try:
            mark_read_chunked(client, suppressed_ids)
        except ElythAPIError as e:
            logger.warning(f"[ELYTH] auto mark-read of suppressed failed: {e}")

    result: Dict[str, Any] = {
        "notifications": adopted + shaped_others,
        "unread_reply_backlog": backlog,
    }
    if announcements_omitted:
        result["announcements_omitted"] = announcements_omitted
    if metrics is not None:
        result["my_metrics"] = metrics
    if warnings:
        result["warnings"] = warnings
    return result, suppressed_ids


def _append_id(ids: List[str], notification: Dict[str, Any]) -> None:
    nid = notification.get("id")
    if nid:
        ids.append(str(nid))


def mark_read_chunked(client: ElythAPIClient, ids: List[str]) -> Dict[str, Any]:
    """POST /notifications/read in server-cap chunks; sums received_count."""
    total = 0
    for i in range(0, len(ids), MARK_READ_CHUNK):
        chunk = ids[i:i + MARK_READ_CHUNK]
        data = client.mark_notifications_read(chunk)
        total += int((data or {}).get("received_count") or 0)
    return {"received_count": total}


# ---------------------------------------------------------------------------
# get_timeline (spec v6 §5.3)
# ---------------------------------------------------------------------------

def build_timeline_result(
    client: ElythAPIClient,
    timeline_limit: Optional[int],
) -> Dict[str, Any]:
    """GET /timeline (full text) + /information extras, in the v1 shape the
    RAG extractor and thread-map scanner already read."""
    tl_data = client.get_timeline(limit=timeline_limit or 10)
    timeline, _ = _normalize_page_posts(tl_data)
    result: Dict[str, Any] = {"timeline": timeline}

    try:
        info = client.get_information()
    except ElythAuthError:
        raise
    except ElythAPIError as e:
        logger.warning(f"[ELYTH] information enrichment failed: {e}")
        result["warnings"] = ["information_unavailable"]
        return result

    info_tl = info.get("timeline") or {}
    if "post_count_last_hour" in info_tl:
        result["post_count_last_hour"] = info_tl.get("post_count_last_hour")

    trending = info.get("trending") or {}
    result["trends"] = {"posts": [
        normalize_post(p) for p in (trending.get("items") or [])
        if isinstance(p, dict)
    ]}

    newcomers = []
    for entry in ((info.get("newcomers") or {}).get("items")) or []:
        if isinstance(entry, dict):
            prof = entry.get("profile") if isinstance(entry.get("profile"), dict) else entry
            newcomers.append({
                "aituber_id": prof.get("id"),
                "name": prof.get("display_name"),
                "handle": prof.get("handle"),
            })
    result["newcomers"] = newcomers

    result["today_topic"] = _shape_topic(info.get("today_topic"))
    result["current_event"] = info.get("current_event")
    result["service_status"] = info.get("service_status")
    return result


def _shape_topic(topic: Any) -> Optional[Dict[str, str]]:
    """today_topic -> {"title", "description"} (v1 extractor keys)."""
    if not isinstance(topic, dict):
        return None
    title = topic.get("title") or topic.get("name") or ""
    description = topic.get("description") or topic.get("body") or ""
    if not (title or description):
        return None
    return {"title": str(title), "description": str(description)}


# ---------------------------------------------------------------------------
# get_thread — v1-parity windowing over oldest-first pagination (spec §5.4)
# ---------------------------------------------------------------------------

def build_thread_result(client: ElythAPIClient, post_id: str) -> Dict[str, Any]:
    """Root + latest THREAD_TAIL_KEEP replies. v1's server did this trimming;
    v2 returns everything oldest-first, so we walk pages (bounded) and window
    client-side. If the walk hits its bound with more pages remaining, the
    true tail is unreachable — flagged honestly via `tail_may_be_missing`."""
    deadline = time.monotonic() + AGGREGATE_DEADLINE_SECONDS
    root: Optional[Dict[str, Any]] = None
    replies: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    tail_may_be_missing = False

    for _page in range(THREAD_PAGE_WALK_LIMIT):
        data = client.get_thread(post_id, limit=50, cursor=cursor)
        thread = data.get("thread") or {}
        if root is None and isinstance(thread.get("root"), dict):
            root = thread["root"]
        replies.extend(r for r in (thread.get("replies") or []) if isinstance(r, dict))
        page = data.get("page") or {}
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if not cursor or time.monotonic() > deadline:
            tail_may_be_missing = True
            break
    else:
        tail_may_be_missing = True

    omitted = max(0, len(replies) - THREAD_TAIL_KEEP)
    tail = replies[-THREAD_TAIL_KEEP:]
    posts = ([normalize_post(root)] if root else []) + [normalize_post(r) for r in tail]
    result: Dict[str, Any] = {"posts": posts, "omitted_count": omitted}
    if tail_may_be_missing:
        result["tail_may_be_missing"] = True
    return result


# ---------------------------------------------------------------------------
# Simple aggregates (spec §5.4)
# ---------------------------------------------------------------------------

def build_my_posts_result(client: ElythAPIClient, limit: int) -> Dict[str, Any]:
    posts, _ = _normalize_page_posts(client.get_my_posts(limit=limit))
    return {"posts": posts}


def build_aituber_result(client: ElythAPIClient, profile_ref: str) -> Dict[str, Any]:
    """v1 parity: profile + recent posts in one result (v2 split them)."""
    prof_data = client.get_profile(profile_ref)
    prof = prof_data.get("profile") or {}
    stats = prof.get("stats") or {}
    relationship = prof.get("relationship") or {}
    live = prof.get("live") or {}
    result: Dict[str, Any] = {"profile": {
        "id": prof.get("id"),
        "display_name": prof.get("display_name"),
        "handle": prof.get("handle"),
        "bio": prof.get("bio"),
        "follower_count": stats.get("follower_count"),
        "following_count": stats.get("following_count"),
        "post_count": stats.get("post_count"),
        "following": relationship.get("following"),
        "follows_me": relationship.get("follows_me"),
        "mutual": relationship.get("mutual"),
        "is_live": live.get("is_live"),
    }}
    try:
        posts, _ = _normalize_page_posts(
            client.get_profile_posts(profile_ref, limit=10))
        result["posts"] = posts
    except ElythAuthError:
        raise
    except ElythAPIError as e:
        logger.warning(f"[ELYTH] profile posts enrichment failed: {e}")
        result["posts"] = []
        result["warnings"] = ["posts_unavailable"]
    return result


def build_search_result(client: ElythAPIClient, hashtag: str) -> Dict[str, Any]:
    posts, has_more = _normalize_page_posts(
        client.search_posts(hashtag, limit=10))
    return {"hashtag": hashtag.lstrip("#").strip(), "posts": posts,
            "has_more": has_more}


VALID_RELATIONSHIP_KINDS = ("followers", "following", "mutual")


def build_relationships_result(client: ElythAPIClient, kind: str) -> Dict[str, Any]:
    entries, has_more = [], False
    data = client.get_relationships(kind, limit=50)
    page = data.get("page") or {}
    has_more = bool(page.get("has_more"))
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        prof = entry.get("profile") or {}
        rel = entry.get("relationship") or {}
        entries.append({
            "aituber_id": prof.get("id"),
            "name": prof.get("display_name"),
            "handle": prof.get("handle"),
            "follower_count": prof.get("follower_count"),
            "following": rel.get("following"),
            "follows_me": rel.get("follows_me"),
            "mutual": rel.get("mutual"),
        })
    return {"kind": kind, "aitubers": entries, "has_more": has_more}
