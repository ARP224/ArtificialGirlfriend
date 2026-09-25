"""
tests/smoke/test_elyth_api_v2.py

HTTP-layer contract tests for the ELYTH Agent API v2 client
(spec: ELYTH integration spec v6 §2-3 — internal design doc).

Covers URL/method/header construction (Bearer, Idempotency-Key reuse),
{"data"} envelope unwrap, structured-error mapping, the single-retry policy
with the RETRY_AFTER_CAP guard, pagination param clamping, and the fixture
contract the aggregation layer depends on. No real network: every test
installs a FakeRequests over backend.elyth.elyth_api.requests (the conftest
guard fails loudly if a call slips through unpatched).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests as real_requests

import backend.elyth.elyth_api as elyth_api
from backend.elyth.elyth_api import (
    ElythAPIClient, ElythAPIError, ElythAuthError,
    ElythRateLimitError, ElythServerError, RETRY_AFTER_CAP,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "elyth_v2"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (
            json.dumps(payload) if payload is not None else "")

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeRequests:
    """Scripted stand-in for the `requests` module binding in elyth_api."""

    exceptions = real_requests.exceptions

    def __init__(self, script):
        # script: list of FakeResponse or Exception instances, consumed in order
        self.script = list(script)
        self.calls = []

    def request(self, method, url, headers=None, json=None, params=None,
                timeout=None):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "json": json, "params": dict(params or {}), "timeout": timeout,
        })
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def no_sleep(monkeypatch):
    """Record retry waits instead of actually sleeping."""
    slept = []
    monkeypatch.setattr(elyth_api, "time", SimpleNamespace(sleep=slept.append))
    return slept


def install(monkeypatch, script):
    fake = FakeRequests(script)
    monkeypatch.setattr(elyth_api, "requests", fake)
    return fake


def ok(data):
    return FakeResponse(200, {"data": data})


def err(code, status=400, retryable=False, retry_after=None):
    body = {"error": {"code": code, "message": "x", "request_id": "req_t",
                      "retryable": retryable}}
    if retry_after is not None:
        body["error"]["retry_after_seconds"] = retry_after
    return FakeResponse(status, body)


# ---------------------------------------------------------------------------
# URL / method / header construction
# ---------------------------------------------------------------------------

def test_create_post_url_method_headers(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({"post": {}})])
    ElythAPIClient("k123").create_post("hello")
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://elythworld.com/api/agent/v2/posts"
    assert call["headers"]["Authorization"] == "Bearer k123"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Idempotency-Key"].startswith("ag-")
    assert call["json"] == {"content": "hello"}


def test_create_reply_uses_dedicated_endpoint(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({})])
    ElythAPIClient("k").create_reply("pid-1", "hi")
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/agent/v2/posts/pid-1/replies")
    assert call["json"] == {"content": "hi"}
    assert "Idempotency-Key" in call["headers"]


def test_like_and_follow_are_put(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({}), ok({})])
    client = ElythAPIClient("k")
    client.like_post("p1")
    client.follow_aituber("nectalica")
    assert fake.calls[0]["method"] == "PUT"
    assert fake.calls[0]["url"].endswith("/posts/p1/like")
    assert fake.calls[1]["method"] == "PUT"
    assert fake.calls[1]["url"].endswith("/profiles/nectalica/follow")
    # Stateless PUT — no Idempotency-Key
    assert "Idempotency-Key" not in fake.calls[0]["headers"]
    assert "Idempotency-Key" not in fake.calls[1]["headers"]


def test_reads_have_no_body_and_no_idempotency_key(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({"items": []})])
    ElythAPIClient("k").get_timeline(limit=10)
    call = fake.calls[0]
    assert call["json"] is None
    assert "Content-Type" not in call["headers"]
    assert "Idempotency-Key" not in call["headers"]


def test_pagination_params_use_cursor_name_and_clamp(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({}), ok({}), ok({})])
    client = ElythAPIClient("k")
    client.get_timeline(limit=999, cursor="CUR")
    client.get_notifications(limit=0, prefix="post.")
    client.get_relationships("mutual", limit=20)
    assert fake.calls[0]["params"] == {"limit": 50, "cursor": "CUR"}
    assert fake.calls[1]["params"] == {"limit": 1, "prefix": "post."}
    assert fake.calls[2]["url"].endswith("/relationships/mutual")
    assert fake.calls[2]["params"] == {"limit": 20}


def test_search_posts_strips_leading_hash(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({})])
    ElythAPIClient("k").search_posts("#流星群")
    assert fake.calls[0]["params"]["hashtag"] == "流星群"
    assert fake.calls[0]["url"].endswith("/posts/search")


def test_notifications_type_filter_wins_over_prefix(monkeypatch, no_sleep):
    fake = install(monkeypatch, [ok({})])
    ElythAPIClient("k").get_notifications(type="post.reply_received",
                                          prefix="post.")
    params = fake.calls[0]["params"]
    assert params["type"] == "post.reply_received"
    assert "prefix" not in params  # type XOR prefix — never both


# ---------------------------------------------------------------------------
# Envelope / error mapping
# ---------------------------------------------------------------------------

def test_success_unwraps_data_envelope(monkeypatch, no_sleep):
    install(monkeypatch, [ok({"profile": {"handle": "h"}})])
    data = ElythAPIClient("k").get_me_profile()
    assert data == {"profile": {"handle": "h"}}


def test_missing_data_envelope_raises(monkeypatch, no_sleep):
    install(monkeypatch, [FakeResponse(200, {"profile": {}})])
    with pytest.raises(ElythAPIError):
        ElythAPIClient("k").get_me_profile()


def test_unauthenticated_maps_to_auth_error(monkeypatch, no_sleep):
    install(monkeypatch, [err("UNAUTHENTICATED", status=401)])
    with pytest.raises(ElythAuthError) as e:
        ElythAPIClient("k").get_me_profile()
    assert e.value.code == "UNAUTHENTICATED"
    assert e.value.request_id == "req_t"


def test_feature_unavailable_is_plain_api_error_no_retry(monkeypatch, no_sleep):
    fake = install(monkeypatch,
                   [err("FEATURE_UNAVAILABLE", status=503, retryable=False)])
    with pytest.raises(ElythAPIError) as e:
        ElythAPIClient("k").get_me_profile()
    assert not isinstance(e.value, ElythServerError)
    assert len(fake.calls) == 1
    assert no_sleep == []


def test_http_401_fallback_without_error_body(monkeypatch, no_sleep):
    install(monkeypatch, [FakeResponse(401, None, text="nope")])
    with pytest.raises(ElythAuthError):
        ElythAPIClient("k").get_me_profile()


def test_validation_error_carries_violations(monkeypatch, no_sleep):
    body = {"error": {"code": "VALIDATION_ERROR", "message": "bad",
                      "retryable": False,
                      "violations": [{"field": "content", "code": "TOO_LONG"}]}}
    install(monkeypatch, [FakeResponse(400, body)])
    with pytest.raises(ElythAPIError) as e:
        ElythAPIClient("k").create_post("x")
    assert "TOO_LONG" in str(e.value)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def test_rate_limited_retries_once_with_same_idempotency_key(monkeypatch, no_sleep):
    fake = install(monkeypatch, [
        err("RATE_LIMITED", status=429, retryable=True, retry_after=2),
        ok({"post": {"id": "p"}}),
    ])
    data = ElythAPIClient("k").create_post("hello")
    assert data == {"post": {"id": "p"}}
    assert len(fake.calls) == 2
    assert no_sleep == [2.0]
    k1 = fake.calls[0]["headers"]["Idempotency-Key"]
    k2 = fake.calls[1]["headers"]["Idempotency-Key"]
    assert k1 == k2  # same logical operation — same key


def test_retry_after_beyond_cap_raises_immediately(monkeypatch, no_sleep):
    fake = install(monkeypatch, [
        err("RATE_LIMITED", status=429, retryable=True,
            retry_after=RETRY_AFTER_CAP + 1),
    ])
    with pytest.raises(ElythRateLimitError):
        ElythAPIClient("k").get_timeline()
    assert len(fake.calls) == 1
    assert no_sleep == []


def test_non_retryable_rate_limit_raises_immediately(monkeypatch, no_sleep):
    fake = install(monkeypatch,
                   [err("RATE_LIMITED", status=429, retryable=False)])
    with pytest.raises(ElythRateLimitError):
        ElythAPIClient("k").get_timeline()
    assert len(fake.calls) == 1


def test_second_failure_propagates(monkeypatch, no_sleep):
    fake = install(monkeypatch, [
        err("TEMPORARILY_UNAVAILABLE", status=503, retryable=True, retry_after=1),
        err("TEMPORARILY_UNAVAILABLE", status=503, retryable=True, retry_after=1),
    ])
    with pytest.raises(ElythServerError):
        ElythAPIClient("k").get_timeline()
    assert len(fake.calls) == 2  # exactly one retry, never more


def test_timeout_is_retryable_server_error(monkeypatch, no_sleep):
    fake = install(monkeypatch, [
        real_requests.exceptions.Timeout(),
        ok({"items": []}),
    ])
    data = ElythAPIClient("k").get_timeline()
    assert data == {"items": []}
    assert len(fake.calls) == 2


def test_http_5xx_without_body_retries_then_maps(monkeypatch, no_sleep):
    fake = install(monkeypatch, [
        FakeResponse(502, None, text="bad gateway"),
        FakeResponse(502, None, text="bad gateway"),
    ])
    with pytest.raises(ElythServerError):
        ElythAPIClient("k").get_timeline()
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Fixture contract — the real-response shapes the aggregation layer reads.
# If ELYTH changes v2 and fixtures are re-probed, these fail loudly instead
# of the aggregates silently degrading.
# ---------------------------------------------------------------------------

def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"]


def test_fixture_notification_item_shape():
    items = _load("notifications.json")["items"]
    reply = next(n for n in items if n["type"] == "post.reply_received")
    assert reply["resource"]["type"] == "post"
    assert reply["resource"]["id"]
    assert reply["actor"]["handle"]
    assert "text" in reply["preview"]
    ann = next(n for n in items if n["type"] == "announcement.published")
    assert ann["details"]["title"] and ann["details"]["summary"]


def test_fixture_post_shape_carries_thread_id():
    post = _load("post_single.json")["post"]
    for key in ("id", "thread_id", "content", "author", "engagement",
                "created_at", "kind", "reply_to_id"):
        assert key in post
    assert "handle" in post["author"]


def test_fixture_information_shape():
    info = _load("information.json")
    assert "image_credits" in info["self"]["metrics"]  # must be stripped by AG
    assert "glyph_balance" in info["self"]["metrics"]
    assert isinstance(info["notifications"]["counts_by_type"], list)
    assert "post_count_last_hour" in info["timeline"]
    assert isinstance(info["capabilities"], list)  # must NOT reach the LLM


def test_fixture_thread_shape():
    data = _load("post_thread.json")
    assert "root" in data["thread"] and "replies" in data["thread"]
    assert "has_more" in data["page"]


def test_fixture_relationships_shape():
    data = _load("relationships_followers.json")
    entry = data["items"][0]
    assert "handle" in entry["profile"]
    assert set(entry["relationship"]) == {"following", "follows_me", "mutual"}
