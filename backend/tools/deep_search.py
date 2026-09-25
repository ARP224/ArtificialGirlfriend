"""
backend/deep_search.py

Web search and webpage text extraction for Deep Search Function Calling.
Uses DuckDuckGo for search and trafilatura for page text extraction.
Ollama: available on tools-capable models (2026-08-11).
"""

import ipaddress
import logging
import re
import socket
from typing import Dict, Any, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MAX_HTML_SIZE = 5 * 1024 * 1024  # 5MB
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_CHARS = 8000
SEARCH_TIMEOUT = 10   # seconds
PAGE_GET_TIMEOUT = 15  # seconds
PAGE_HEAD_TIMEOUT = 10  # seconds
MAX_REDIRECTS = 5


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def validate_url(url: str, language: str) -> Tuple[bool, str]:
    """Validate URL safety. Returns (is_valid, error_message).

    SSRF guard: read_webpage は LLM ツールで、URL は Web検索結果由来=
    プロンプトインジェクションで汚染されうる。ホスト名を解決し、解決先IPが
    ループバック/プライベート/リンクローカル(169.254.169.254 のメタデータ
    エンドポイント含む)なら拒否して内部サービスの読み出しを防ぐ。
    """
    from backend.shared.prompt_i18n import prompt_text
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False, prompt_text("fmt.deep.url_bad_scheme", language, scheme=parsed.scheme)

    if not parsed.hostname:
        return False, prompt_text("fmt.deep.url_invalid", language)

    hostname = parsed.hostname
    try:
        # 全解決先を検査(DNS が複数レコードを返す場合の rebinding 的抜けを塞ぐ)
        addrinfos = socket.getaddrinfo(hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        return False, prompt_text("fmt.deep.url_resolve_failed", language, error=e)

    for info in addrinfos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, prompt_text("fmt.deep.url_bad_ip", language)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, prompt_text("fmt.deep.url_internal_blocked", language)

    return True, ""


class RedirectBlockedError(Exception):
    """リダイレクト先が validate_url に落ちた（メッセージは validate_url のものをそのまま持つ）。"""


def _request_following_redirects(method: str, url: str, timeout: float, headers: dict, language: str):
    """requests の自動リダイレクト追跡は各ホップを再検証しない=公開ホストが
    内部IPへ 302 するだけで初回の validate_url が素通りする。ここで手動追跡し、
    ホップ毎に validate_url を通す(初回URLは呼び出し元が検証済み)。
    """
    import requests as req_lib

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response = req_lib.request(
            method, current, timeout=timeout, headers=headers, allow_redirects=False,
        )
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        response.close()
        current = urljoin(current, location)
        is_valid, err_msg = validate_url(current, language)
        if not is_valid:
            raise RedirectBlockedError(err_msg)
    raise req_lib.TooManyRedirects(f"Exceeded {MAX_REDIRECTS} redirects")


# ---------------------------------------------------------------------------
# Text truncation helper
# ---------------------------------------------------------------------------

def _truncate_text(text: str, max_chars: int, language: str) -> str:
    """Truncate text at sentence boundary, appending marker if truncated."""
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]

    # Try to cut at the last sentence boundary
    for sep in ("。", ".\n", "\n\n", "\n", ".", "、"):
        last_pos = truncated.rfind(sep)
        if last_pos > max_chars * 0.5:
            truncated = truncated[: last_pos + len(sep)]
            break

    from backend.shared.prompt_i18n import prompt_text
    return truncated.rstrip() + "\n\n" + prompt_text("fmt.deep.truncated", language)


# ---------------------------------------------------------------------------
# Web search (DuckDuckGo)
# ---------------------------------------------------------------------------

# ddgs の検索リージョン。プロンプト言語に連動(未知の言語は英語圏へ)
_SEARCH_REGIONS = {"ja": "jp-jp", "en": "us-en"}


def search_web(query: str, max_results: int = DEFAULT_MAX_RESULTS, *, language: str) -> Dict[str, Any]:
    """Execute a DuckDuckGo web search.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: Prompt language (result labels + search region).

    Returns:
        {"success": True, "result": "formatted results"} or
        {"success": False, "error": "error message"}
    """
    from backend.shared.prompt_i18n import prompt_text

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.error("[DeepSearch] ddgs is not installed")
            return {
                "success": False,
                "error": prompt_text("fmt.deep.ddgs_missing", language),
            }

    if not query or not query.strip():
        return {"success": False, "error": prompt_text("fmt.deep.empty_query", language)}

    region = _SEARCH_REGIONS.get(language, "us-en")
    try:
        ddgs = DDGS(timeout=SEARCH_TIMEOUT)
        results = list(ddgs.text(
            query.strip(),
            max_results=max_results,
            region=region,
            backend="api",
        ))

        if not results:
            # Fallback: try "html" backend if "api" returns nothing
            logger.info("[DeepSearch] Empty results from api backend, retrying with html backend")
            results = list(ddgs.text(
                query.strip(),
                max_results=max_results,
                region=region,
                backend="html",
            ))

        if not results:
            return {"success": False, "error": prompt_text("fmt.deep.no_results", language)}

        # Format results
        lines = [prompt_text("fmt.deep.results_header", language)]
        for i, r in enumerate(results, 1):
            title = r.get("title", prompt_text("fmt.deep.no_title", language))
            href = r.get("href", "")
            body = r.get("body", "")
            lines.append(f"\n{i}. [{title}]({href})")
            if body:
                lines.append(f"   {body}")

        return {"success": True, "result": "\n".join(lines)}

    except Exception as e:
        err_str = str(e).lower()
        if "ratelimit" in err_str or "429" in err_str:
            logger.warning(f"[DeepSearch] Rate limited: {e}")
            return {
                "success": False,
                "error": prompt_text("fmt.deep.rate_limited", language),
            }
        if "timeout" in err_str:
            logger.warning(f"[DeepSearch] Search timeout: {e}")
            return {"success": False, "error": prompt_text("fmt.deep.search_timeout", language)}

        logger.error(f"[DeepSearch] Search error: {e}")
        return {
            "success": False,
            "error": prompt_text("fmt.deep.search_failed", language),
        }


# ---------------------------------------------------------------------------
# Webpage text extraction
# ---------------------------------------------------------------------------

def read_webpage(url: str, max_chars: int = DEFAULT_MAX_CHARS, *, language: str) -> Dict[str, Any]:
    """Fetch a webpage and extract its main text content.

    Args:
        url: URL to fetch.
        max_chars: Maximum characters to return.
        language: Prompt language for result labels / error strings.

    Returns:
        {"success": True, "result": "formatted text"} or
        {"success": False, "error": "error message"}
    """
    import requests as req_lib
    from backend.shared.prompt_i18n import prompt_text

    try:
        import trafilatura
    except ImportError:
        logger.error("[DeepSearch] trafilatura is not installed")
        return {
            "success": False,
            "error": prompt_text("fmt.deep.trafilatura_missing", language),
        }

    # --- URL validation ---
    is_valid, err_msg = validate_url(url, language)
    if not is_valid:
        return {"success": False, "error": err_msg}

    headers = {"User-Agent": USER_AGENT}

    # --- HEAD request for Content-Type pre-check ---
    try:
        head_resp = _request_following_redirects("HEAD", url, PAGE_HEAD_TIMEOUT, headers, language)
        content_type = head_resp.headers.get("Content-Type", "")
        if content_type and "text/html" not in content_type and "text/plain" not in content_type:
            return {"success": False, "error": prompt_text("fmt.deep.unsupported_format", language)}
    except RedirectBlockedError as e:
        return {"success": False, "error": str(e)}
    except req_lib.RequestException:
        pass  # HEAD failure is acceptable; continue with GET

    # --- GET request ---
    try:
        response = _request_following_redirects("GET", url, PAGE_GET_TIMEOUT, headers, language)
        response.raise_for_status()
    except RedirectBlockedError as e:
        return {"success": False, "error": str(e)}
    except req_lib.exceptions.Timeout:
        logger.warning(f"[DeepSearch] Page timeout: {url}")
        return {"success": False, "error": prompt_text("fmt.deep.page_timeout", language)}
    except req_lib.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 0
        if status_code == 404:
            return {"success": False, "error": prompt_text("fmt.deep.page_not_found", language)}
        if status_code == 403:
            return {"success": False, "error": prompt_text("fmt.deep.access_denied", language)}
        logger.warning(f"[DeepSearch] HTTP error {status_code}: {url}")
        return {"success": False, "error": prompt_text("fmt.deep.fetch_failed", language, status=status_code)}
    except req_lib.exceptions.ConnectionError:
        logger.warning(f"[DeepSearch] Connection error: {url}")
        return {"success": False, "error": prompt_text("fmt.deep.connect_failed", language)}
    except req_lib.RequestException as e:
        logger.error(f"[DeepSearch] Request error: {e}")
        return {"success": False, "error": prompt_text("fmt.deep.fetch_error", language)}

    # --- Content-Type re-check on GET response ---
    content_type = response.headers.get("Content-Type", "")
    if content_type and "text/html" not in content_type and "text/plain" not in content_type:
        return {"success": False, "error": prompt_text("fmt.deep.unsupported_format", language)}

    # --- Size check ---
    html = response.text
    if len(html.encode("utf-8", errors="replace")) > MAX_HTML_SIZE:
        return {"success": False, "error": prompt_text("fmt.deep.page_too_large", language)}

    # --- Text extraction ---
    text = trafilatura.extract(html, include_comments=False, include_tables=True)
    if not text or not text.strip():
        return {"success": False, "error": prompt_text("fmt.deep.extract_failed", language)}

    # --- Extract title from HTML ---
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = title_match.group(1).strip()

    # --- Truncate ---
    text = _truncate_text(text.strip(), max_chars, language)

    # --- Format result ---
    result_parts = []
    if title:
        result_parts.append(prompt_text("fmt.deep.page_title", language, title=title))
    result_parts.append(f"URL: {url}")
    result_parts.append(prompt_text("fmt.deep.body", language, text=text))

    return {"success": True, "result": "\n".join(result_parts)}
