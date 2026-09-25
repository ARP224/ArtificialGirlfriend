"""
tests/smoke/test_launch_config_remote_switch.py

server_mode.remote_switch_enabled (リモートモード切替のオプトイン) は
既存の launch_config.json に存在しないキーなので、deep merge が
デフォルト False を補完すること・update が永続化されることを固定する。
"""

import json

from backend.shared import launch_config


def test_missing_key_merges_to_false(tmp_path, monkeypatch):
    cfg_file = tmp_path / 'launch_config.json'
    # 既存ユーザーのファイル: remote_switch_enabled を知らない世代
    cfg_file.write_text(json.dumps({
        "server_mode": {"enabled": True, "session_timeout_minutes": 30},
    }), encoding='utf-8')
    monkeypatch.setattr(launch_config, 'LAUNCH_CONFIG_FILE', cfg_file)

    config = launch_config.load_launch_config()
    assert config['server_mode']['remote_switch_enabled'] is False
    # 既存値は上書きされない
    assert config['server_mode']['enabled'] is True
    assert config['server_mode']['session_timeout_minutes'] == 30


def test_update_persists_flag(tmp_path, monkeypatch):
    cfg_file = tmp_path / 'launch_config.json'
    monkeypatch.setattr(launch_config, 'LAUNCH_CONFIG_FILE', cfg_file)

    assert launch_config.update_launch_config_value(
        'server_mode', 'remote_switch_enabled', True)
    assert launch_config.get_launch_config_value(
        'server_mode', 'remote_switch_enabled', False) is True

    assert launch_config.update_launch_config_value(
        'server_mode', 'remote_switch_enabled', False)
    assert launch_config.get_launch_config_value(
        'server_mode', 'remote_switch_enabled', True) is False
