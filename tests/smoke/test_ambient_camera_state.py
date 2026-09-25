"""Smoke tests for backend/shared/ambient_camera_state.py — the Live
Camera request/deliver/consume state machine (request_id matching, timeout
circuit breaker, provider lifecycle). Pure threading logic; no transport,
no LLM. fire_capture_request's transport hop is faked via sys.modules so
the heavy websocket_server module is never imported.
"""

import sys
import types

import pytest

from backend.shared import ambient_camera_state as acs
from backend.shared.ambient_camera_state import AmbientCameraState
from backend.shared.constants import AMBIENT_TIMEOUT_DEGRADE_THRESHOLD


@pytest.fixture()
def state():
    return AmbientCameraState()


def test_begin_without_provider_returns_none(state):
    assert state.begin_request() is None


def test_begin_returns_id_and_inflight_request_is_reused(state):
    state.set_provider_available(True)
    rid = state.begin_request()
    assert rid is not None
    # Double-fire while in flight: same id, no new claim
    assert state.begin_request() == rid


def test_deliver_and_consume_roundtrip(state, tmp_path):
    state.set_provider_available(True)
    rid = state.begin_request()
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpg")
    accepted, elapsed_ms = state.deliver_frame(rid, str(frame))
    assert accepted is True
    assert elapsed_ms >= 0
    assert state.consume_frame_if_pending(timeout=0.5) == str(frame)
    # Slot is single-use: nothing pending afterwards
    assert state.consume_frame_if_pending(timeout=0.01) is None


def test_stale_frame_rejected(state, tmp_path):
    state.set_provider_available(True)
    state.begin_request()
    accepted, _ = state.deliver_frame("amb-does-not-match", str(tmp_path / "x.jpg"))
    assert accepted is False


def test_consume_without_request_is_zero_cost(state):
    assert state.consume_frame_if_pending(timeout=5.0) is None  # returns instantly


def test_timeout_opens_breaker_and_reannounce_resets(state):
    state.set_provider_available(True)
    for i in range(AMBIENT_TIMEOUT_DEGRADE_THRESHOLD):
        rid = state.begin_request()
        assert rid is not None, f"breaker opened too early (round {i})"
        assert state.consume_frame_if_pending(timeout=0.01) is None
    # Breaker open: no more claims
    assert state.degraded is True
    assert state.begin_request() is None
    # Page re-announce resets the breaker
    state.set_provider_available(True)
    assert state.degraded is False
    assert state.begin_request() is not None


def test_cancel_request_releases_claim_without_timeout_penalty(state):
    state.set_provider_available(True)
    rid = state.begin_request()
    state.cancel_request(rid)
    assert state.consecutive_timeouts == 0
    assert state.consume_frame_if_pending(timeout=0.01) is None  # nothing pending
    assert state.begin_request() is not None  # claim was released


def test_provider_off_clears_inflight_request(state, tmp_path):
    state.set_provider_available(True)
    rid = state.begin_request()
    state.set_provider_available(False)
    accepted, _ = state.deliver_frame(rid, str(tmp_path / "x.jpg"))
    assert accepted is False
    assert state.begin_request() is None


def test_stranded_request_is_superseded_and_frame_dropped(state, tmp_path):
    """fire→turn-rejected strand: begin after MAX_AGE gets a fresh id and the
    old delivered frame file is deleted (stale scenery must not leak)."""
    state.set_provider_available(True)
    rid1 = state.begin_request()
    frame = tmp_path / "old.jpg"
    frame.write_bytes(b"old")
    state.deliver_frame(rid1, str(frame))
    # Age the request past the freshness bound
    state._request_started_at -= 31.0
    rid2 = state.begin_request()
    assert rid2 is not None and rid2 != rid1
    assert not frame.exists()


def test_consume_discards_aged_frame_without_timeout_penalty(state, tmp_path):
    state.set_provider_available(True)
    rid = state.begin_request()
    frame = tmp_path / "old.jpg"
    frame.write_bytes(b"old")
    state.deliver_frame(rid, str(frame))
    state._request_started_at -= 31.0
    assert state.consume_frame_if_pending(timeout=0.5) is None
    assert not frame.exists()
    assert state.consecutive_timeouts == 0  # camera answered; no breaker penalty
    assert state.degraded is False


def _fake_ws_module(sent_ids, send_result=True):
    manager = types.SimpleNamespace(
        send_ambient_capture_request_sync=lambda rid: (sent_ids.append(rid), send_result)[1]
    )
    return types.SimpleNamespace(get_websocket_manager=lambda: manager)


def _enable_ambient_feature(monkeypatch, enabled=True):
    """fire_capture_request gates on the ambient_camera feature toggle
    (call-time import of runtime_state.get_feature_status)."""
    import backend.shared.runtime_state as rts
    monkeypatch.setattr(rts, "get_feature_status",
                        lambda: {"ambient_camera_enabled": enabled})


def test_fire_capture_request_sends_via_transport(monkeypatch, state):
    monkeypatch.setattr(acs, "_ambient_camera_state", state)
    _enable_ambient_feature(monkeypatch)
    sent = []
    monkeypatch.setitem(sys.modules, "backend.server.websocket_server",
                        _fake_ws_module(sent, send_result=True))
    state.set_provider_available(True)
    rid = acs.fire_capture_request()
    assert rid is not None
    assert sent == [rid]


def test_fire_capture_request_send_failure_releases_claim(monkeypatch, state):
    monkeypatch.setattr(acs, "_ambient_camera_state", state)
    _enable_ambient_feature(monkeypatch)
    sent = []
    monkeypatch.setitem(sys.modules, "backend.server.websocket_server",
                        _fake_ws_module(sent, send_result=False))
    state.set_provider_available(True)
    assert acs.fire_capture_request() is None
    # Claim released: a later fire can claim again
    assert state.begin_request() is not None


def test_fire_capture_request_noop_without_provider(monkeypatch, state):
    monkeypatch.setattr(acs, "_ambient_camera_state", state)
    _enable_ambient_feature(monkeypatch)
    assert acs.fire_capture_request() is None


def test_fire_capture_request_noop_when_ambient_feature_off(monkeypatch, state):
    """Camera ON + Live Camera OFF = camera reserved for the AI tool: send paths
    must not fire (稜裁定 2026-07-16)."""
    monkeypatch.setattr(acs, "_ambient_camera_state", state)
    _enable_ambient_feature(monkeypatch, enabled=False)
    sent = []
    monkeypatch.setitem(sys.modules, "backend.server.websocket_server",
                        _fake_ws_module(sent, send_result=True))
    state.set_provider_available(True)
    assert acs.fire_capture_request() is None
    assert sent == []


def test_request_tool_frame_ignores_ambient_toggle(monkeypatch, state, tmp_path):
    """The AI tool path needs only a connected camera, not the auto-attach
    toggle. Frame delivered while waiting -> path returned."""
    monkeypatch.setattr(acs, "_ambient_camera_state", state)
    _enable_ambient_feature(monkeypatch, enabled=False)
    frame = tmp_path / "tool.jpg"
    frame.write_bytes(b"jpg")

    def _send_and_deliver(rid):
        state.deliver_frame(rid, str(frame))
        return True

    manager = types.SimpleNamespace(send_ambient_capture_request_sync=_send_and_deliver)
    monkeypatch.setitem(sys.modules, "backend.server.websocket_server",
                        types.SimpleNamespace(get_websocket_manager=lambda: manager))
    state.set_provider_available(True)
    assert acs.request_tool_frame(timeout=0.5) == str(frame)


def test_is_provider_available_reflects_degrade(state, monkeypatch):
    monkeypatch.setattr(acs, "_ambient_camera_state", state)
    assert acs.is_provider_available() is False
    state.set_provider_available(True)
    assert acs.is_provider_available() is True
    state.degraded = True
    assert acs.is_provider_available() is False
