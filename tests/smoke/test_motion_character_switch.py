"""
tests/smoke/test_motion_character_switch.py

表示中の MotionPNGPlayer がキャラ切替に追随する契約(稜裁定 2026-09-19)のスモーク。

- サーバーモード: 従来どおり「フォルダ名だけ」をリモートのプレイヤーへ送る(不変)。
- ローカルモード: 表示中なら、切替先の Motion フォルダが有効なとき character_changed を
  ローカルのプレイヤーへ送り、未設定/実体なしなら Disappear と同じ経路で閉じる。
  フォルダの有効性は Appear の可用性ゲートと同じ判定(resolve_character_folder + isdir)。
"""

from types import MethodType, SimpleNamespace

from backend.server.websocket_server import WebSocketManager
from backend.tools import motion_pngtuber_launcher


def _stub(server_mode, appeared, ws=object()):
    """notify_character_changed が触る面だけを持つ代役(実 WS サーバーは起動しない)。"""
    calls = {"sent": [], "closed": 0}
    stub = SimpleNamespace(
        server_mode=server_mode,
        _motion_pngtuber_appeared=appeared,
        _motion_pngtuber_ws=ws,
        send_to_motion_pngtuber_sync=lambda msg: calls["sent"].append(msg),
        close_motion_pngtuber_sync=lambda: calls.__setitem__("closed", calls["closed"] + 1),
    )
    stub._notify_character_changed_local = MethodType(
        WebSocketManager._notify_character_changed_local, stub)
    stub.notify = MethodType(WebSocketManager.notify_character_changed, stub)
    return stub, calls


def test_local_appeared_valid_folder_sends_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_pngtuber_launcher, "is_electron_running", lambda: False)
    folder = tmp_path / "Aya"
    folder.mkdir()
    stub, calls = _stub(server_mode=False, appeared=True)

    stub.notify("Aya", "Aya", motion_folder=str(folder))

    assert calls["closed"] == 0
    assert calls["sent"] == [{
        "action": "character_changed",
        "character_name": "Aya",
        "folder_name": str(folder),
    }]


def test_local_bare_name_resolves_under_asset_dir(tmp_path, monkeypatch):
    """設定値がフォルダ名だけのとき(通常)は <player>/Asset/<名前> に解決して送る。"""
    monkeypatch.setattr(motion_pngtuber_launcher, "is_electron_running", lambda: False)
    monkeypatch.setattr(motion_pngtuber_launcher, "ASSET_DIR", tmp_path)
    (tmp_path / "Hina").mkdir()
    stub, calls = _stub(server_mode=False, appeared=True)

    stub.notify("Hina", "Hina", motion_folder="Hina")

    assert calls["closed"] == 0
    assert calls["sent"][0]["folder_name"] == str(tmp_path / "Hina")


def test_local_appeared_missing_folder_closes_player(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_pngtuber_launcher, "is_electron_running", lambda: False)
    stub, calls = _stub(server_mode=False, appeared=True)

    stub.notify("Ghost", "NoSuch", motion_folder=str(tmp_path / "NoSuch"))

    assert calls["sent"] == []
    assert calls["closed"] == 1


def test_local_appeared_unset_folder_closes_player(monkeypatch):
    monkeypatch.setattr(motion_pngtuber_launcher, "is_electron_running", lambda: False)
    stub, calls = _stub(server_mode=False, appeared=True)

    stub.notify("NoMotion", "", motion_folder="")

    assert calls["sent"] == []
    assert calls["closed"] == 1


def test_local_not_appeared_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_pngtuber_launcher, "is_electron_running", lambda: False)
    stub, calls = _stub(server_mode=False, appeared=False)

    stub.notify("Ghost", "NoSuch", motion_folder=str(tmp_path / "NoSuch"))

    assert calls["sent"] == []
    assert calls["closed"] == 0


def test_local_player_not_connected_yet_neither_sends_nor_closes(tmp_path, monkeypatch):
    """Appear 直後で WS 未接続の短い間: 送れないが、閉じもしない(表示は前のキャラのまま)。"""
    monkeypatch.setattr(motion_pngtuber_launcher, "is_electron_running", lambda: True)
    folder = tmp_path / "Aya"
    folder.mkdir()
    stub, calls = _stub(server_mode=False, appeared=True, ws=None)

    stub.notify("Aya", "Aya", motion_folder=str(folder))

    assert calls["sent"] == []
    assert calls["closed"] == 0


def test_server_mode_unchanged_sends_folder_name_only(tmp_path):
    """サーバーモードは従来どおり: クライアントは自分の Asset を持つので名前だけ送る。"""
    stub, calls = _stub(server_mode=True, appeared=True)

    stub.notify("Aya", "Aya", motion_folder=str(tmp_path / "anything"))

    assert calls["closed"] == 0
    assert calls["sent"] == [{
        "action": "character_changed",
        "character_name": "Aya",
        "folder_name": "Aya",
    }]


def test_server_mode_not_appeared_does_nothing():
    stub, calls = _stub(server_mode=True, appeared=False)

    stub.notify("Aya", "Aya", motion_folder="Aya")

    assert calls["sent"] == []
    assert calls["closed"] == 0
