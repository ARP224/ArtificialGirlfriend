"""
tests/smoke/test_remote_switch_listener.py

リモートモード切替リスナーの契約を固定する:

HTTP面:
  - GET は全パスで切替ページ+識別ヘッダ X-AG-Switch-Page (ブックマーク1本設計)
  - POST /switch は CSRF ガードヘッダ必須(なし=403・コールバック不発火)
  - POST /switch はヘッダありで注入コールバックの結果をそのまま返す

ライフサイクル面(2026-07-20 実機で発覚した表示固着バグの再発防止):
  - Tailscale断を watchdog が検知して waiting_tailscale へ落ち、復帰で再bindする
  - stop→start の素早い切り替えでも新世代が確実に待受に到達する(世代方式)
  - 状態遷移のたびに注入した on_status_change が発火する

本番のSSL/Tailscale/証明書は実機受け入れで確認する(平文HTTPで検証。
ssl_context=None / _build_ssl_context のmonkeypatchがテスト用の継ぎ目)。
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from backend.server.remote_switch_listener import _build_server


@pytest.fixture
def switch_server():
    calls = []

    def fake_switch_cb():
        calls.append(1)
        return {"accepted": True, "reason": ""}

    server = _build_server('127.0.0.1', 0, fake_switch_cb, ssl_context=None)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{port}', calls
    server.shutdown()
    server.server_close()


def test_get_serves_switch_page_on_any_path(switch_server):
    base, _calls = switch_server
    for path in ('/', '/mobile', '/admin/anything'):
        with urllib.request.urlopen(f'{base}{path}', timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get('X-AG-Switch-Page') == '1', path
            body = resp.read().decode('utf-8')
            assert 'switch-btn' in body, path


def test_get_renders_disabled_button_with_reason_while_busy():
    # 記憶タスク中(busy_cb が理由文を返す)は開いた時点でボタン disabled+理由表示。
    # 忙しくない(None)なら従来どおり押せる(2026-08-16 稜裁定)
    busy = {"reason": "memory tasks running <b>now</b>"}

    server = _build_server('127.0.0.1', 0, lambda: {"accepted": True, "reason": ""},
                           ssl_context=None, busy_cb=lambda: busy["reason"])
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as resp:
            body = resp.read().decode('utf-8')
        assert '<button id="switch-btn" disabled>' in body
        assert 'memory tasks running &lt;b&gt;now&lt;/b&gt;' in body  # HTML エスケープ済み

        busy["reason"] = None
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as resp:
            body = resp.read().decode('utf-8')
        assert '<button id="switch-btn">' in body
        assert '<div id="status"></div>' in body
    finally:
        server.shutdown()
        server.server_close()


def test_post_without_csrf_header_is_rejected(switch_server):
    base, calls = switch_server
    req = urllib.request.Request(f'{base}/switch', method='POST')
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)
    assert excinfo.value.code == 403
    assert calls == [], "callback fired despite missing CSRF header"


def test_post_with_csrf_header_invokes_callback(switch_server):
    base, calls = switch_server
    req = urllib.request.Request(
        f'{base}/switch', method='POST',
        headers={'X-AG-Remote-Switch': '1'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode('utf-8'))
    assert data == {"accepted": True, "reason": ""}
    assert calls == [1]


def test_post_unknown_path_is_404(switch_server):
    base, calls = switch_server
    req = urllib.request.Request(
        f'{base}/other', method='POST',
        headers={'X-AG-Remote-Switch': '1'})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)
    assert excinfo.value.code == 404
    assert calls == []


# -- lifecycle (watchdog / generation) ---------------------------------------

def _wait_for(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def lifecycle_listener(monkeypatch):
    from backend.server import remote_switch_listener as mod
    from backend.server import tailscale
    monkeypatch.setattr(mod, 'RETRY_INTERVAL_SECONDS', 0.05)
    available = {'up': True}
    monkeypatch.setattr(tailscale, 'check_tailscale_available',
                        lambda: available['up'])
    monkeypatch.setattr(tailscale, 'get_tailscale_ip', lambda: '127.0.0.1')
    monkeypatch.setattr(tailscale, 'get_tailscale_hostname',
                        lambda: 'test.example.ts.net')
    monkeypatch.setattr(tailscale, 'ensure_valid_cert',
                        lambda: ('cert.pem', 'key.pem'))
    monkeypatch.setattr(mod, '_build_ssl_context', lambda c, k: None)

    listener = mod.RemoteSwitchListener()
    statuses = []
    listener.configure(lambda: {"accepted": True}, 0,
                       on_status_change=lambda: statuses.append(listener.status))
    yield listener, available, statuses
    listener.stop()


def test_ts_outage_and_recovery(lifecycle_listener):
    listener, available, statuses = lifecycle_listener
    listener.start()
    assert _wait_for(lambda: listener.status == 'listening'), statuses
    assert listener.listen_url.startswith('https://test.example.ts.net')

    available['up'] = False
    assert _wait_for(lambda: listener.status == 'waiting_tailscale'), statuses
    assert listener._server is None, "server not torn down on Tailscale loss"

    available['up'] = True
    assert _wait_for(lambda: listener.status == 'listening'), statuses


def test_stop_then_immediate_start_reaches_listening(lifecycle_listener):
    listener, _available, statuses = lifecycle_listener
    listener.start()
    assert _wait_for(lambda: listener.status == 'listening'), statuses
    listener.stop()
    assert listener.status == 'stopped'
    listener.start()  # 旧世代の巻き取り中でも新世代が必ず立ち上がること
    assert _wait_for(lambda: listener.status == 'listening'), statuses


def test_waiting_state_fires_status_callback(lifecycle_listener):
    listener, available, statuses = lifecycle_listener
    available['up'] = False
    listener.start()
    assert _wait_for(lambda: 'waiting_tailscale' in statuses), statuses
