"""
tests/smoke/test_status_probe.py

Connectivity probe classification (backend/shared/api_settings.py).

The status indicator turns probe results into 🟢/🔴 lines, so the 4-state
classification is the contract: ok / auth_error / unreachable / no_key.
All network I/O is faked — a probe without an API key must make NO request
(this is also why the test suite never touches the network).
"""

import pytest
import requests

from backend.shared import api_settings


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._payload


def _settings_with_key(provider):
    return lambda: {provider: {"api_key": "sk-test"}}


def test_no_key_makes_no_request(monkeypatch):
    monkeypatch.setattr(api_settings, "load_api_settings", lambda: {})

    def _forbidden(*args, **kwargs):
        raise AssertionError("probe without a key must not touch the network")
    monkeypatch.setattr(api_settings.requests, "get", _forbidden)

    assert api_settings.probe_provider_api("openai")["state"] == "no_key"
    assert api_settings.probe_elevenlabs()["state"] == "no_key"


def test_ok_reports_model_count(monkeypatch):
    monkeypatch.setattr(api_settings, "load_api_settings",
                        _settings_with_key("openai"))
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}
    monkeypatch.setattr(api_settings.requests, "get",
                        lambda *a, **kw: _FakeResponse(200, payload))

    result = api_settings.probe_provider_api("openai")

    assert result == {"state": "ok", "model_count": 2}


@pytest.mark.parametrize("status_code,expected", [
    (401, "auth_error"),
    (403, "auth_error"),
    (500, "unreachable"),
])
def test_http_errors_classify(monkeypatch, status_code, expected):
    monkeypatch.setattr(api_settings, "load_api_settings",
                        _settings_with_key("anthropic"))
    monkeypatch.setattr(api_settings.requests, "get",
                        lambda *a, **kw: _FakeResponse(status_code))

    assert api_settings.probe_provider_api("anthropic")["state"] == expected


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectionError("refused"),
    requests.exceptions.Timeout("timed out"),
])
def test_network_errors_are_unreachable(monkeypatch, exc):
    monkeypatch.setattr(api_settings, "load_api_settings",
                        _settings_with_key("google"))

    def _raise(*args, **kwargs):
        raise exc
    monkeypatch.setattr(api_settings.requests, "get", _raise)

    assert api_settings.probe_provider_api("google")["state"] == "unreachable"


def test_unknown_provider_is_unreachable():
    # ollama has no models_url and must never be probed through this path
    assert api_settings.probe_provider_api("ollama")["state"] == "unreachable"


def test_probe_uses_short_timeout(monkeypatch):
    monkeypatch.setattr(api_settings, "load_api_settings",
                        _settings_with_key("openai"))
    seen = {}

    def _capture(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResponse(200, {"data": []})
    monkeypatch.setattr(api_settings.requests, "get", _capture)

    api_settings.probe_provider_api("openai")
    assert seen["timeout"] == api_settings.PROBE_TIMEOUT


def test_elevenlabs_auth_error(monkeypatch):
    monkeypatch.setattr(api_settings, "get_elevenlabs_api_key", lambda: "xi-test")
    monkeypatch.setattr(api_settings.requests, "get",
                        lambda *a, **kw: _FakeResponse(401))

    assert api_settings.probe_elevenlabs()["state"] == "auth_error"
