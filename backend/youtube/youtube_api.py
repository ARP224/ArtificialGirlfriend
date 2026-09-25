"""
backend/youtube/youtube_api.py

YouTube Data API v3 client for the comment auto-reply feature — requests
direct-hit (same style as backend/tools/map_search.py), with tokens supplied
by backend/youtube/auth.py.

Error model (spec §4-8 / §2): every call raises one of the typed errors
below instead of returning error dicts, so the session/worker layers can
classify without string matching:

- AuthRequiredError   … invalid_grant / no token → auth_error transition
- QuotaExceededError  … daily quota exhausted → interrupt, resume next day
- PermanentAPIError   … 404 / commentsDisabled etc. → skip the item, never retry
- TransientAPIError   … network / 5xx → retry via the caller's retry rules

401 (access token expired, a routine ~1h event) is handled INSIDE the
request core: force-refresh once and retry; only a refresh failing with
invalid_grant escalates to AuthRequiredError (spec §2 の2分類).
"""

import logging
import re
from typing import Any, Dict, List, Optional

import requests

from backend.youtube import auth

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
API_TIMEOUT = 15  # seconds

# 403 reasons that mean "quota", as opposed to permanent permission errors.
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"})


class YouTubeAPIError(Exception):
    """Base class. `reason` carries the API error reason when known."""

    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class AuthRequiredError(YouTubeAPIError):
    """Re-authorization required (invalid_grant / no token)."""


class QuotaExceededError(YouTubeAPIError):
    """Daily API quota exhausted — interrupt and resume in a later session."""


class PermanentAPIError(YouTubeAPIError):
    """Will not succeed on retry (deleted video/comment, comments disabled...)."""


class TransientAPIError(YouTubeAPIError):
    """Network / 5xx — may succeed on retry."""


# ---------------------------------------------------------------------------
# Request core
# ---------------------------------------------------------------------------

def _error_reason(resp: requests.Response) -> str:
    try:
        errors = resp.json().get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason", "")
    except Exception:
        pass
    return ""


def _get_token(force_refresh: bool = False) -> str:
    tok = auth.get_access_token(force_refresh=force_refresh)
    if not tok["success"]:
        if tok.get("invalid_grant"):
            raise AuthRequiredError(tok["error"], reason="invalid_grant")
        raise TransientAPIError(f"Token refresh failed: {tok['error']}")
    return tok["token"]


def _request(method: str, endpoint: str, *, params: Optional[Dict[str, Any]] = None,
             json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One authorized API call with the 401→refresh-once→retry rule."""
    url = f"{API_BASE}/{endpoint}"
    token = _get_token()
    for attempt in (1, 2):
        try:
            resp = requests.request(
                method, url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=API_TIMEOUT,
            )
        except requests.RequestException as e:
            raise TransientAPIError(f"Network error: {e}") from e

        if resp.status_code == 401 and attempt == 1:
            # Routine access-token expiry — refresh and retry once. Only an
            # invalid_grant inside _get_token escalates to AuthRequiredError.
            token = _get_token(force_refresh=True)
            continue

        return _classify_response(resp)
    raise TransientAPIError("Unreachable retry state")  # pragma: no cover


def _classify_response(resp: requests.Response) -> Dict[str, Any]:
    code = resp.status_code
    if 200 <= code < 300:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise TransientAPIError(f"Invalid JSON response: {e}") from e

    reason = _error_reason(resp)
    message = f"HTTP {code} ({reason or 'no reason'}): {resp.text[:300]}"
    if code == 403 and reason in _QUOTA_REASONS:
        raise QuotaExceededError(message, reason=reason)
    if code == 401:
        raise AuthRequiredError(message, reason=reason)
    if code in (400, 403, 404):
        # 400 = bad/removed parameter (e.g. the deprecated
        # allThreadsRelatedToChannelId), 403 non-quota = permission
        # (commentsDisabled...), 404 = gone. None recover on retry.
        raise PermanentAPIError(message, reason=reason)
    if code >= 500:
        raise TransientAPIError(message, reason=reason)
    raise TransientAPIError(message, reason=reason)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

# 対象チャンネルの入力形式は @ハンドルのみ(稜裁定 2026-08-11: チャンネルID/URL
# には対応しない)。YouTube公式のハンドル文字種(英数字 . - _ / 3〜30字)まで焼き
# 込むと、将来 YouTube 側が規則を変えたとき正当なハンドルを弾いて回避手段が
# なくなるため、「@必須＋URL/空白の混入を拒否」だけの緩い判定にする(綴り違いは
# API の channelNotFound で明確に落ちる)。UI 側の事前検証もこの定数を使う
# ＝形式規則の真実源はここ1箇所。
HANDLE_RE = re.compile(r"^@[^\s/?&#]+$")


def resolve_channel(query: str) -> Dict[str, str]:
    """Resolve an '@handle' to {'channel_id', 'title'}.

    Only the '@handle' form is accepted (spec §7 originally allowed
    handle/ID/URL; narrowed to handle-only by 稜裁定 2026-08-11). Raises
    PermanentAPIError for any other shape and when the handle does not exist.
    """
    q = (query or "").strip()
    if not q:
        raise PermanentAPIError("Empty channel query")
    if not HANDLE_RE.match(q):
        raise PermanentAPIError(f"Not an @handle: {query}", reason="invalidHandle")

    params = {"part": "snippet", "forHandle": q.lstrip("@")}

    data = _request("GET", "channels", params=params)
    items = data.get("items", [])
    if not items:
        raise PermanentAPIError(f"Channel not found: {query}", reason="channelNotFound")
    return {
        "channel_id": items[0]["id"],
        "title": items[0].get("snippet", {}).get("title", ""),
    }


def get_uploads_playlist_id(channel_id: str) -> str:
    """Return the channel's uploads playlist id (for the fallback fetch)."""
    data = _request("GET", "channels", params={
        "part": "contentDetails", "id": channel_id,
    })
    items = data.get("items", [])
    if not items:
        raise PermanentAPIError(f"Channel not found: {channel_id}",
                                reason="channelNotFound")
    try:
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except KeyError as e:
        raise PermanentAPIError(f"No uploads playlist for {channel_id}") from e


def list_recent_video_ids(channel_id: str, max_videos: int = 10) -> List[str]:
    """Latest uploaded video ids (newest first) via playlistItems.list."""
    playlist_id = get_uploads_playlist_id(channel_id)
    data = _request("GET", "playlistItems", params={
        "part": "contentDetails",
        "playlistId": playlist_id,
        "maxResults": max(1, min(int(max_videos), 50)),
    })
    return [
        it["contentDetails"]["videoId"]
        for it in data.get("items", [])
        if it.get("contentDetails", {}).get("videoId")
    ]


# ---------------------------------------------------------------------------
# Comment threads (fetch)
# ---------------------------------------------------------------------------

def _normalize_thread(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """commentThreads.list item → flat comment dict (top-level only)."""
    try:
        thread_snippet = item["snippet"]
        tlc = thread_snippet["topLevelComment"]
        cs = tlc["snippet"]
        return {
            "comment_id": tlc["id"],
            "video_id": thread_snippet.get("videoId") or cs.get("videoId", ""),
            "author_channel_id": (cs.get("authorChannelId") or {}).get("value", ""),
            "author_name": cs.get("authorDisplayName", ""),
            "text": cs.get("textOriginal") or cs.get("textDisplay", ""),
            "published_at": cs.get("publishedAt", ""),
        }
    except (KeyError, TypeError):
        logger.warning(f"[YouTube] Unparseable comment thread item: {item.get('id')}")
        return None


def fetch_channel_comments(channel_id: str, max_results: int = 50) -> List[Dict[str, str]]:
    """Primary fetch (spec §4 一次案): one call for the channel's latest
    top-level comments across all videos.

    `allThreadsRelatedToChannelId` is suspected deprecated — if the API
    rejects it (400/PermanentAPIError), the caller falls back to
    fetch_comments_via_videos (予備案).
    """
    data = _request("GET", "commentThreads", params={
        "part": "snippet",
        "allThreadsRelatedToChannelId": channel_id,
        "order": "time",
        "maxResults": max(1, min(int(max_results), 100)),
        "textFormat": "plainText",
    })
    comments = [_normalize_thread(it) for it in data.get("items", [])]
    return [c for c in comments if c]


def fetch_video_comments(video_id: str, max_results: int = 50) -> List[Dict[str, str]]:
    """Top-level comments of one video, newest first.

    commentsDisabled arrives as PermanentAPIError — the caller treats that
    video as having no fetchable comments (skip, don't interrupt).
    """
    data = _request("GET", "commentThreads", params={
        "part": "snippet",
        "videoId": video_id,
        "order": "time",
        "maxResults": max(1, min(int(max_results), 100)),
        "textFormat": "plainText",
    })
    comments = [_normalize_thread(it) for it in data.get("items", [])]
    return [c for c in comments if c]


def fetch_comments_via_videos(channel_id: str, max_videos: int = 10,
                              max_results: int = 50) -> List[Dict[str, str]]:
    """Fallback fetch (spec §4 予備案): enumerate the latest N uploads and
    collect each video's top-level comments, merged newest-first and capped
    at max_results. Videos with comments disabled are skipped silently."""
    merged: List[Dict[str, str]] = []
    for video_id in list_recent_video_ids(channel_id, max_videos):
        try:
            merged.extend(fetch_video_comments(video_id, max_results))
        except PermanentAPIError as e:
            logger.info(f"[YouTube] Skipping video {video_id}: {e.reason or e}")
    merged.sort(key=lambda c: c["published_at"], reverse=True)
    return merged[:max_results]


def fetch_latest_comments(channel_id: str, max_results: int = 50,
                          max_videos: int = 10) -> List[Dict[str, str]]:
    """Primary fetch with automatic fallback (Y2 前提ゲートの機械化).

    If allThreadsRelatedToChannelId is rejected as a bad request (the
    deprecation case), switch to the per-video fallback. Quota / transient /
    auth errors propagate unchanged.
    """
    try:
        return fetch_channel_comments(channel_id, max_results)
    except PermanentAPIError as e:
        logger.warning(
            "[YouTube] allThreadsRelatedToChannelId rejected "
            f"({e.reason or e}); falling back to per-video fetch"
        )
        return fetch_comments_via_videos(channel_id, max_videos, max_results)


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

def fetch_video_info(video_id: str) -> Dict[str, str]:
    """{'title', 'description'} via videos.list. A deleted/private video
    returns an empty items list → PermanentAPIError(videoNotFound)."""
    data = _request("GET", "videos", params={"part": "snippet", "id": video_id})
    items = data.get("items", [])
    if not items:
        raise PermanentAPIError(f"Video not found: {video_id}", reason="videoNotFound")
    snippet = items[0].get("snippet", {})
    return {
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
    }


# ---------------------------------------------------------------------------
# Replies (post + verification)
# ---------------------------------------------------------------------------

def insert_reply(parent_comment_id: str, text: str) -> Dict[str, Any]:
    """Post a reply to a top-level comment (comments.insert, 50 units)."""
    return _request("POST", "comments",
                    params={"part": "snippet"},
                    json_body={
                        "snippet": {
                            "parentId": parent_comment_id,
                            "textOriginal": text,
                        }
                    })


def list_reply_author_channel_ids(parent_comment_id: str) -> List[str]:
    """All reply authors' channel ids for a top-level comment, walking every
    page (spec §4 二重投稿防止: 1ページに収まらない大量返信でも見逃さない)."""
    authors: List[str] = []
    page_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {
            "part": "snippet",
            "parentId": parent_comment_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token
        data = _request("GET", "comments", params=params)
        for it in data.get("items", []):
            cid = (it.get("snippet", {}).get("authorChannelId") or {}).get("value", "")
            if cid:
                authors.append(cid)
        page_token = data.get("nextPageToken")
        if not page_token:
            return authors
