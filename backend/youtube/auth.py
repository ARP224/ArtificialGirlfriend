"""
backend/youtube/auth.py

OAuth 2.0 authorization and token management for the YouTube comment
auto-reply feature (spec §2 — internal design doc, see backend/youtube/__init__.py).

Responsibilities:
- Import the user-supplied client_secret.json into character_data/youtube/
  (pull-in copy; no .example template is shipped — spec §0).
- Run the loopback authorization. The browser is NOT auto-opened (multi-
  profile Chrome: auto-open lands in the wrong profile — 2026-07-12); the
  authorization URL is returned/printed and the user opens it in the
  browser/profile logged into the reply channel. With a brand-channel
  account the user picks WHICH channel to authorize there — that choice
  decides the posting channel.
- Persist token.json (refresh + access token, plus the authorized channel's
  id/name fetched right after authorization via channels.list(mine=true)).
- Hand out a valid access token, refreshing transparently. The two failure
  classes are kept apart (spec §2): an expired access token (~1h) is a
  routine refresh, while invalid_grant (refresh token revoked/expired,
  e.g. the 7-day Testing-mode expiry) means re-authorization is required
  and is surfaced as ``invalid_grant: True`` so the caller can enter the
  auth_error state.

API calls themselves live in youtube_api.py (requests direct-hit, same
style as map_search.py); this module only manages credentials.
"""

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from backend.shared.constants import YOUTUBE_DIR

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

CLIENT_SECRET_FILE = YOUTUBE_DIR / "client_secret.json"
TOKEN_FILE = YOUTUBE_DIR / "token.json"

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
API_TIMEOUT = 15  # seconds

# Serializes refresh + token.json writes across threads (scheduler thread,
# generation task in the LLM queue, and the posting worker can all need a
# token at the same time).
_token_lock = threading.RLock()


# ---------------------------------------------------------------------------
# client_secret.json
# ---------------------------------------------------------------------------

def has_client_secret() -> bool:
    return CLIENT_SECRET_FILE.exists()


def has_token() -> bool:
    return TOKEN_FILE.exists()


def import_client_secret(src_path: str) -> Dict[str, Any]:
    """Copy a user-supplied client_secret.json into character_data/youtube/.

    Validates that the file is JSON of the "installed" (desktop) application
    type before copying, so a wrong file fails here instead of mid-flow.
    """
    src = Path(src_path)
    if not src.is_file():
        return {"success": False, "error": f"File not found: {src_path}"}
    try:
        with open(src, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return {"success": False, "error": f"Not a valid JSON file: {e}"}
    if "installed" not in data:
        return {
            "success": False,
            "error": (
                "client_secret.json must be of application type "
                "'Desktop app' (missing 'installed' key)."
            ),
        }
    try:
        YOUTUBE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(CLIENT_SECRET_FILE))
    except Exception as e:
        return {"success": False, "error": f"Failed to copy: {e}"}
    logger.info(f"[YouTube] Imported client_secret.json from {src}")
    return {"success": True}


# ---------------------------------------------------------------------------
# token.json persistence
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(str(tmp), str(path))


def _load_token_info() -> Optional[Dict[str, Any]]:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"[YouTube] Failed to read token.json: {e}")
        return None


def _save_credentials(creds, channel_id: Optional[str] = None,
                      channel_title: Optional[str] = None) -> None:
    """Persist credentials to token.json, keeping the stored channel info
    unless new values are given (refresh writes must not drop it)."""
    with _token_lock:
        info = json.loads(creds.to_json())
        prev = _load_token_info() or {}
        info["channel_id"] = channel_id if channel_id is not None else prev.get("channel_id")
        info["channel_title"] = (
            channel_title if channel_title is not None else prev.get("channel_title")
        )
        _atomic_write_json(TOKEN_FILE, info)


def get_authorized_channel() -> Optional[Dict[str, str]]:
    """Return {'channel_id', 'channel_title'} of the authorized channel, or None."""
    info = _load_token_info()
    if not info or not info.get("channel_id"):
        return None
    return {
        "channel_id": info["channel_id"],
        "channel_title": info.get("channel_title", ""),
    }


def token_file_mtime() -> Optional[float]:
    """mtime of token.json, or None if absent. Used by the auth_error
    recovery watch (spec §2: a CLI re-authorization in another process is
    detected via mtime and the scheduler self-recovers without a restart)."""
    try:
        return TOKEN_FILE.stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _load_credentials():
    """Build google Credentials from token.json (None if absent/corrupt)."""
    info = _load_token_info()
    if not info or not info.get("refresh_token"):
        return None
    try:
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_info(info, SCOPES)
    except Exception as e:
        logger.error(f"[YouTube] Failed to load credentials: {e}")
        return None


def _is_invalid_grant(exc: Exception) -> bool:
    return "invalid_grant" in str(exc).lower()


def get_access_token(force_refresh: bool = False) -> Dict[str, Any]:
    """Return a valid access token, refreshing if needed.

    Returns:
        {"success": True, "token": str}
        {"success": False, "error": str, "invalid_grant": bool}
        invalid_grant=True means re-authorization is required (auth_error
        transition); False means a transient failure (network etc.).
    """
    with _token_lock:
        creds = _load_credentials()
        if creds is None:
            return {
                "success": False,
                "error": "Not authorized (token.json missing or unreadable)",
                "invalid_grant": True,
            }
        if force_refresh or not creds.valid:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
            except Exception as e:
                if _is_invalid_grant(e):
                    logger.warning(f"[YouTube] Refresh token invalid (re-auth needed): {e}")
                    return {"success": False, "error": str(e), "invalid_grant": True}
                logger.warning(f"[YouTube] Token refresh failed (transient): {e}")
                return {"success": False, "error": str(e), "invalid_grant": False}
            # Spec §2: refreshed credentials are written back immediately.
            try:
                _save_credentials(creds)
            except Exception as e:
                logger.error(f"[YouTube] Failed to persist refreshed token: {e}")
        return {"success": True, "token": creds.token}


def verify_token() -> Dict[str, Any]:
    """Re-validate stored credentials by forcing a refresh.

    Used by the auth_error recovery watch after token.json changed on disk.
    """
    return get_access_token(force_refresh=True)


# ---------------------------------------------------------------------------
# Authorization flow (stage A: CLI wrapper get_token.py / stage B: UI)
#
# 2026-07-12 change (稜フィードバック): the browser is NEVER auto-opened.
# Auto-open lands in the OS-default browser profile, which is the wrong one
# for multi-profile Chrome setups (the reply channel lives in a different
# profile than the one AG's UI runs in). Instead, begin_authorization_flow()
# starts a one-shot loopback server and RETURNS the authorization URL; the
# user opens it in whichever browser/profile they want. Restartable: calling
# begin again cancels the previous pending flow (the old blocking design left
# a dead server behind and made the second button press a no-op).
# ---------------------------------------------------------------------------

AUTH_FLOW_TIMEOUT = 600  # seconds to wait for the redirect before giving up


class _PendingAuth:
    """One in-flight loopback authorization attempt."""

    def __init__(self):
        self.status = "pending"  # pending | success | error | cancelled
        self.error = ""
        self.channel_id = ""
        self.channel_title = ""
        self.auth_url = ""
        self.cancelled = False


_pending_auth: Optional[_PendingAuth] = None
_pending_auth_lock = threading.Lock()


def _fetch_own_channel(access_token: str) -> Dict[str, str]:
    """channels.list(mine=true) → {'channel_id', 'channel_title'} (best effort)."""
    channel = {"channel_id": "", "channel_title": ""}
    try:
        resp = requests.get(
            CHANNELS_URL,
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if items:
            channel["channel_id"] = items[0].get("id", "")
            channel["channel_title"] = items[0].get("snippet", {}).get("title", "")
    except Exception as e:
        # Non-fatal: the token itself is good; channel info can be
        # re-fetched later. But log loudly since UI relies on it.
        logger.error(f"[YouTube] Failed to fetch authorized channel: {e}")
    return channel


def begin_authorization_flow() -> Dict[str, Any]:
    """Start the loopback flow WITHOUT opening a browser.

    Returns {"success": True, "auth_url": str} immediately; the redirect is
    awaited on a daemon thread (AUTH_FLOW_TIMEOUT ceiling). The user opens
    auth_url in the browser/profile that is logged into the reply channel —
    on the Google side they pick the account AND, for brand-channel
    accounts, which channel to authorize; that channel becomes the poster.

    Any previous pending attempt is cancelled first, so the button can be
    pressed repeatedly without stranding a dead flow.
    """
    global _pending_auth
    if not has_client_secret():
        return {
            "success": False,
            "error": f"client_secret.json not found at {CLIENT_SECRET_FILE}",
        }
    with _pending_auth_lock:
        # Cancel a previous pending attempt (its thread notices via the flag
        # within the server-poll interval and shuts the old server down).
        if _pending_auth is not None and _pending_auth.status == "pending":
            _pending_auth.cancelled = True

        try:
            import http.server

            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET_FILE), SCOPES
            )

            captured: Dict[str, str] = {}

            class _RedirectHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802 (http.server API)
                    captured["path"] = self.path
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        "認可が完了しました。このタブを閉じて Artificial Girlfriend "
                        "の画面に戻ってください。 / Authorization complete. You can "
                        "close this tab and return to Artificial Girlfriend."
                        .encode("utf-8")
                    )

                def log_message(self, *args):  # silence request logging
                    pass

            server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
            server.timeout = 2  # handle_request() poll interval (cancel latency)
            port = server.server_address[1]
            flow.redirect_uri = f"http://localhost:{port}/"
            # access_type=offline is google_auth_oauthlib's default → refresh token
            auth_url, _ = flow.authorization_url()
        except Exception as e:
            logger.error(f"[YouTube] Failed to start authorization flow: {e}")
            return {"success": False, "error": f"Failed to start flow: {e}"}

        pending = _PendingAuth()
        pending.auth_url = auth_url
        _pending_auth = pending

    def _wait_for_redirect():
        deadline = time.time() + AUTH_FLOW_TIMEOUT
        try:
            while (
                "path" not in captured
                and not pending.cancelled
                and time.time() < deadline
            ):
                server.handle_request()  # returns every server.timeout seconds
            if pending.cancelled:
                pending.status = "cancelled"
                return
            if "path" not in captured:
                pending.status = "error"
                pending.error = "Timed out waiting for authorization"
                return
            # oauthlib rejects http:// responses; loopback is exempted by
            # presenting the redirect as https (same trick run_local_server uses).
            authorization_response = f"https://localhost:{port}{captured['path']}"
            flow.fetch_token(authorization_response=authorization_response)
            creds = flow.credentials
            channel = _fetch_own_channel(creds.token)
            _save_credentials(
                creds,
                channel_id=channel["channel_id"],
                channel_title=channel["channel_title"],
            )
            pending.channel_id = channel["channel_id"]
            pending.channel_title = channel["channel_title"]
            pending.status = "success"
            logger.info(
                f"[YouTube] Authorized as channel "
                f"'{pending.channel_title}' ({pending.channel_id})"
            )
        except Exception as e:
            logger.error(f"[YouTube] Authorization flow failed: {e}")
            pending.status = "error"
            pending.error = str(e)
        finally:
            try:
                server.server_close()
            except Exception:
                pass

    threading.Thread(
        target=_wait_for_redirect, name="youtube-oauth-wait", daemon=True,
    ).start()
    return {"success": True, "auth_url": auth_url}


def get_authorization_status() -> Dict[str, Any]:
    """State of the most recent begin_authorization_flow() attempt.

    {"status": "idle" | "pending" | "success" | "error" | "cancelled", ...}
    """
    pending = _pending_auth
    if pending is None:
        return {"status": "idle"}
    return {
        "status": pending.status,
        "auth_url": pending.auth_url,
        "error": pending.error,
        "channel_id": pending.channel_id,
        "channel_title": pending.channel_title,
    }


def cancel_authorization() -> None:
    """Cancel a pending authorization attempt (no-op otherwise)."""
    pending = _pending_auth
    if pending is not None and pending.status == "pending":
        pending.cancelled = True


def run_authorization_flow(url_callback=None,
                           timeout: float = AUTH_FLOW_TIMEOUT) -> Dict[str, Any]:
    """Blocking wrapper for the CLI (stage A): begin the flow, hand the URL
    to url_callback (e.g. print), then wait for the outcome."""
    begin = begin_authorization_flow()
    if not begin["success"]:
        return begin
    if url_callback:
        url_callback(begin["auth_url"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = get_authorization_status()
        if status["status"] == "success":
            return {
                "success": True,
                "channel_id": status["channel_id"],
                "channel_title": status["channel_title"],
            }
        if status["status"] in ("error", "cancelled"):
            return {"success": False,
                    "error": status.get("error") or status["status"]}
        time.sleep(1.0)
    cancel_authorization()
    return {"success": False, "error": "Timed out waiting for authorization"}
