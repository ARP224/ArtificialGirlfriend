"""
backend/elyth_api.py

Thin HTTP client for the ELYTH Agent API v2.

Contract (ELYTH integration spec v6 §2 — internal design doc, not part of the repository):
  - Auth: `Authorization: Bearer <key>` (v1's x-api-key is dead).
  - Success responses are {"data": ...} envelopes — this client unwraps and
    returns the inner `data`.
  - Errors are {"error": {code, message, request_id, retryable,
    retry_after_seconds, violations}} — mapped to exception types below,
    with HTTP-status fallback when the body is not in that shape.
  - Idempotency-Key: generated once per logical operation (per _request call)
    and reused verbatim across the internal retry, so a retried write can
    never double-post.
  - Retry: at most one, only for retryable RATE_LIMITED / TEMPORARILY_
    UNAVAILABLE / network errors, honoring retry_after_seconds capped at
    RETRY_AFTER_CAP — a longer server-requested wait raises immediately
    instead of blocking the session thread.

Reference: Divedesign/elyth-agent-skills (GitHub) — skills/elyth/references/api-contract.md
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from backend.elyth.elyth_endpoints import (
    ELYTH_API_BASE_PATH, ELYTH_BASE_URL, ENDPOINTS,
)

logger = logging.getLogger(__name__)

# Longest server-requested wait we honor before retrying (seconds).
RETRY_AFTER_CAP = 60.0
# Default waits when the error carries no retry_after_seconds.
_DEFAULT_RATE_LIMIT_WAIT = 30.0
_DEFAULT_TRANSIENT_WAIT = 2.0

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class ElythAPIError(Exception):
    """Base exception for ELYTH API errors.

    Carries the structured-error fields when the server provided them:
    `code`, `request_id`, `retryable`, `retry_after_seconds`.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        request_id: str = "",
        retryable: bool = False,
        retry_after_seconds: Optional[float] = None,
    ):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class ElythRateLimitError(ElythAPIError):
    """RATE_LIMITED (or HTTP 429)."""
    pass


class ElythAuthError(ElythAPIError):
    """UNAUTHENTICATED (or HTTP 401) — invalid or expired API key."""
    pass


class ElythServerError(ElythAPIError):
    """TEMPORARILY_UNAVAILABLE / 5xx / network failure."""
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ElythAPIClient:
    """
    ELYTH Agent API v2 client.

    Raises ElythAPIError subtypes on failure; returns the unwrapped `data`
    dict on success. Aggregation/dispatch layers decide overall strategy.
    """

    def __init__(self, api_key: str, timeout: float = 15.0):
        self.api_key = api_key
        self.timeout = timeout

    # -- Posts ---------------------------------------------------------------

    def create_post(self, content: str) -> Dict[str, Any]:
        """POST /posts (Idempotency-Key)."""
        return self._request("create_post", body={"content": content})

    def create_reply(self, post_id: str, content: str) -> Dict[str, Any]:
        """POST /posts/{post_id}/replies (Idempotency-Key)."""
        return self._request("create_reply", path_params={"post_id": post_id},
                             body={"content": content})

    def get_post(self, post_id: str) -> Dict[str, Any]:
        """GET /posts/{post_id}"""
        return self._request("get_post", path_params={"post_id": post_id})

    def get_thread(self, post_id: str, limit: Optional[int] = None,
                   cursor: Optional[str] = None) -> Dict[str, Any]:
        """GET /posts/{post_id}/thread (replies oldest first, paginated)."""
        return self._request("get_thread", path_params={"post_id": post_id},
                             params=_page_params(limit, cursor))

    def get_timeline(self, limit: Optional[int] = None,
                     cursor: Optional[str] = None) -> Dict[str, Any]:
        """GET /timeline (root posts, newest first, paginated)."""
        return self._request("get_timeline", params=_page_params(limit, cursor))

    def get_my_posts(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """GET /me/posts"""
        return self._request("get_my_posts", params=_page_params(limit, None))

    def search_posts(self, hashtag: str, limit: Optional[int] = None,
                     cursor: Optional[str] = None) -> Dict[str, Any]:
        """GET /posts/search?hashtag=... (leading # tolerated server-side too)."""
        params = _page_params(limit, cursor)
        params["hashtag"] = hashtag.lstrip("#").strip()
        return self._request("search_posts", params=params)

    def like_post(self, post_id: str) -> Dict[str, Any]:
        """PUT /posts/{post_id}/like (sets liked state true; stateless)."""
        return self._request("like_post", path_params={"post_id": post_id})

    # -- Notifications -------------------------------------------------------

    def get_notifications(self, limit: Optional[int] = None,
                          type: Optional[str] = None,
                          prefix: Optional[str] = None,
                          cursor: Optional[str] = None) -> Dict[str, Any]:
        """GET /notifications (unread; `type` XOR `prefix` filter)."""
        params = _page_params(limit, cursor)
        if type:
            params["type"] = type
        elif prefix:
            params["prefix"] = prefix
        return self._request("get_notifications", params=params)

    def mark_notifications_read(self, notification_ids: List[str]) -> Dict[str, Any]:
        """POST /notifications/read (1-100 ids per call — caller chunks)."""
        return self._request("mark_notifications_read",
                             body={"notification_ids": notification_ids})

    # -- Profiles / relationships -------------------------------------------

    def get_me_profile(self) -> Dict[str, Any]:
        """GET /me/profile (identity check / own-handle source)."""
        return self._request("get_me_profile")

    def get_profile(self, profile_ref: str) -> Dict[str, Any]:
        """GET /profiles/{profile_ref} (UUID / handle / @handle)."""
        return self._request("get_profile", path_params={"profile_ref": profile_ref})

    def get_profile_posts(self, profile_ref: str,
                          limit: Optional[int] = None) -> Dict[str, Any]:
        """GET /profiles/{profile_ref}/posts"""
        return self._request("get_profile_posts",
                             path_params={"profile_ref": profile_ref},
                             params=_page_params(limit, None))

    def follow_aituber(self, profile_ref: str) -> Dict[str, Any]:
        """PUT /profiles/{profile_ref}/follow (sets follow state true)."""
        return self._request("follow_aituber",
                             path_params={"profile_ref": profile_ref})

    def get_relationships(self, kind: str, limit: Optional[int] = None,
                          cursor: Optional[str] = None) -> Dict[str, Any]:
        """GET /relationships/{kind} (followers / following / mutual)."""
        return self._request("get_relationships", path_params={"kind": kind},
                             params=_page_params(limit, cursor))

    # -- Aggregated snapshot -------------------------------------------------

    def get_information(self) -> Dict[str, Any]:
        """GET /information (fixed lightweight snapshot, no parameters)."""
        return self._request("get_information")

    # -- Internal ------------------------------------------------------------

    def _request(
        self,
        endpoint_name: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        path_params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute a v2 request; return the unwrapped `data` on success.

        Retries at most once for retryable failures, reusing the same
        Idempotency-Key so a retried write cannot double-apply.
        """
        ep = ENDPOINTS.get(endpoint_name)
        if not ep:
            raise ElythAPIError(f"Unknown endpoint: {endpoint_name}")

        method, path_template, needs_idem_key = ep
        path = path_template
        if path_params:
            for key, value in path_params.items():
                path = path.replace(f"{{{key}}}", str(value))

        url = f"{ELYTH_BASE_URL}{ELYTH_API_BASE_PATH}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if needs_idem_key:
            # One key per logical operation — reused verbatim by the retry.
            headers["Idempotency-Key"] = f"ag-{uuid.uuid4()}"

        last_error: Optional[ElythAPIError] = None
        for attempt in (1, 2):
            try:
                return self._do_request(method, url, path, headers, body, params)
            except (ElythRateLimitError, ElythServerError) as e:
                delay = _retry_delay(e)
                if attempt == 2 or delay is None:
                    raise
                logger.info(
                    f"[ELYTH] retryable failure on {method} {path} "
                    f"(code={e.code or 'n/a'} request_id={e.request_id or 'n/a'}), "
                    f"retrying in {delay:.0f}s"
                )
                last_error = e
                time.sleep(delay)
        raise last_error  # unreachable, satisfies type flow

    def _do_request(
        self,
        method: str,
        url: str,
        path: str,
        headers: Dict[str, str],
        body: Optional[Dict[str, Any]],
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        logger.debug(f"[ELYTH] {method} {url}")
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
                params=params,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise ElythServerError(f"Request timed out: {method} {path}",
                                   retryable=True)
        except requests.exceptions.ConnectionError as e:
            raise ElythServerError(f"Connection error: {e}", retryable=True)
        except requests.exceptions.RequestException as e:
            raise ElythAPIError(f"Request failed: {e}")

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            _raise_structured_error(payload["error"], response.status_code)

        # HTTP-status fallback for non-structured failures
        if response.status_code == 401:
            raise ElythAuthError("Invalid or expired ELYTH API key (401)",
                                 code="UNAUTHENTICATED")
        if response.status_code == 429:
            raise ElythRateLimitError("ELYTH rate limit exceeded (429)",
                                      code="RATE_LIMITED", retryable=True)
        if response.status_code >= 500:
            msg = response.text[:200] if response.text else "No details"
            raise ElythServerError(
                f"ELYTH server error ({response.status_code}): {msg}",
                retryable=True)
        if not response.ok:
            msg = response.text[:200] if response.text else "No details"
            raise ElythAPIError(f"ELYTH HTTP {response.status_code}: {msg}")

        if not isinstance(payload, dict) or "data" not in payload:
            raise ElythAPIError("Invalid JSON envelope in ELYTH response")
        return payload["data"]


def _raise_structured_error(err: Dict[str, Any], status_code: int) -> None:
    """Map a v2 structured error body to the exception hierarchy."""
    code = str(err.get("code") or "")
    message = str(err.get("message") or "")
    request_id = str(err.get("request_id") or "")
    retryable = bool(err.get("retryable"))
    retry_after = err.get("retry_after_seconds")
    retry_after = float(retry_after) if isinstance(retry_after, (int, float)) else None

    kwargs = dict(code=code, request_id=request_id, retryable=retryable,
                  retry_after_seconds=retry_after)
    text = f"{code}: {message}" if code else (message or f"HTTP {status_code}")

    # The structured code is authoritative — FEATURE_UNAVAILABLE arrives with
    # HTTP 503 (live-probed) and must NOT classify as a retryable server error.
    if code:
        if code == "UNAUTHENTICATED":
            raise ElythAuthError(text, **kwargs)
        if code == "RATE_LIMITED":
            raise ElythRateLimitError(text, **kwargs)
        if code == "TEMPORARILY_UNAVAILABLE":
            raise ElythServerError(text, **kwargs)
    elif status_code == 401:
        raise ElythAuthError(text, **kwargs)
    elif status_code == 429:
        raise ElythRateLimitError(text, **kwargs)
    elif status_code >= 500:
        raise ElythServerError(text, **kwargs)
    # VALIDATION_ERROR / FORBIDDEN / FEATURE_UNAVAILABLE /
    # IDEMPOTENCY_CONFLICT / unknown codes — non-retryable client errors.
    violations = err.get("violations")
    if violations:
        text = f"{text} violations={violations}"
    raise ElythAPIError(text, **kwargs)


def _retry_delay(e: ElythAPIError) -> Optional[float]:
    """Seconds to wait before the single retry, or None for 'do not retry'.

    A server-requested wait beyond RETRY_AFTER_CAP is treated as non-retryable
    so a session thread never blocks on a long hold.
    """
    if not e.retryable:
        return None
    if e.retry_after_seconds is not None:
        if e.retry_after_seconds > RETRY_AFTER_CAP:
            return None
        return max(0.0, e.retry_after_seconds)
    if isinstance(e, ElythRateLimitError):
        return _DEFAULT_RATE_LIMIT_WAIT
    return _DEFAULT_TRANSIENT_WAIT


def _page_params(limit: Optional[int], cursor: Optional[str]) -> Dict[str, Any]:
    """Build pagination query params (live-probed param name: `cursor`)."""
    params: Dict[str, Any] = {}
    if limit is not None:
        params["limit"] = max(1, min(50, int(limit)))
    if cursor:
        params["cursor"] = cursor
    return params
