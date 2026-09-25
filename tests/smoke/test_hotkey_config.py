"""
tests/smoke/test_hotkey_config.py

録音ホットキーの設定駆動組み立て(backend.shared.hotkey_handler)の検証。
pynput 実物の Key/KeyCode を使うが、組み立ては純関数でありリスナーは
一切起動しない。
"""

import pytest

pytest.importorskip("pynput")
from pynput.keyboard import Key, KeyCode  # noqa: E402

from backend.shared.hotkey_handler import (  # noqa: E402
    HotkeyAction,
    HotkeyHandler,
    build_hotkey_configs,
    hotkey_label,
    hotkeys_conflict,
    normalize_hotkey_entry,
)


def _combos(configs, action):
    return [c.key_combination for c in configs if c.action == action]


class TestBuildHotkeyConfigs:
    def test_default_config_matches_legacy_windows_combos(self):
        """設定なし→旧ハードコード(Ctrl+1/0 の vk+char 二重登録)と同値。"""
        configs = build_hotkey_configs(None, is_mac=False)
        assert len(configs) == 4
        starts = _combos(configs, HotkeyAction.START_RECORDING)
        stops = _combos(configs, HotkeyAction.STOP_RECORDING)
        assert {Key.ctrl, KeyCode.from_vk(49)} in starts
        assert {Key.ctrl, KeyCode.from_char('1')} in starts
        assert {Key.ctrl, KeyCode.from_vk(48)} in stops
        assert {Key.ctrl, KeyCode.from_char('0')} in stops

    def test_letter_combo_builds_vk_and_char_variants(self):
        cfg = {'start': {'ctrl': True, 'alt': False, 'shift': True, 'key': 'r'},
               'stop': {'ctrl': True, 'alt': False, 'shift': False, 'key': '0'}}
        configs = build_hotkey_configs(cfg, is_mac=False)
        starts = _combos(configs, HotkeyAction.START_RECORDING)
        assert {Key.ctrl, Key.shift, KeyCode.from_vk(82)} in starts
        assert {Key.ctrl, Key.shift, KeyCode.from_char('r')} in starts

    def test_function_key_is_single_variant(self):
        cfg = {'start': {'ctrl': True, 'alt': False, 'shift': False, 'key': 'f5'},
               'stop': {'ctrl': True, 'alt': False, 'shift': False, 'key': 'f6'}}
        configs = build_hotkey_configs(cfg, is_mac=False)
        starts = _combos(configs, HotkeyAction.START_RECORDING)
        assert starts == [{Key.ctrl, Key.f5}]

    def test_invalid_entries_fall_back_to_defaults(self):
        """壊れた設定でもホットキーを失わない(既定 Ctrl+1/0 へ劣化)。"""
        cfg = {'start': {'ctrl': False, 'alt': False, 'shift': False, 'key': '5'},
               'stop': 'garbage'}
        configs = build_hotkey_configs(cfg, is_mac=False)
        starts = _combos(configs, HotkeyAction.START_RECORDING)
        stops = _combos(configs, HotkeyAction.STOP_RECORDING)
        assert {Key.ctrl, KeyCode.from_vk(49)} in starts
        assert {Key.ctrl, KeyCode.from_vk(48)} in stops

    def test_mac_builds_darwin_vk_and_char_variants(self):
        """darwin は DARWIN_VK+char の二重登録(M1実測: 1=18 / 0=29)。"""
        configs = build_hotkey_configs(None, is_mac=True)
        assert len(configs) == 4
        starts = _combos(configs, HotkeyAction.START_RECORDING)
        stops = _combos(configs, HotkeyAction.STOP_RECORDING)
        assert {Key.ctrl, KeyCode.from_vk(18)} in starts
        assert {Key.ctrl, KeyCode.from_char('1')} in starts
        assert {Key.ctrl, KeyCode.from_vk(29)} in stops
        assert {Key.ctrl, KeyCode.from_char('0')} in stops

    def test_mac_letter_uses_measured_darwin_vk(self):
        """Option+R は char が '®' に化けても vk=15 で照合できる(M1実測)。"""
        cfg = {'start': {'ctrl': False, 'alt': True, 'shift': False, 'key': 'r'},
               'stop': {'ctrl': True, 'alt': False, 'shift': False, 'key': '0'}}
        configs = build_hotkey_configs(cfg, is_mac=True)
        starts = _combos(configs, HotkeyAction.START_RECORDING)
        assert {Key.alt, KeyCode.from_vk(15)} in starts

    def test_darwin_vk_table_matches_m1_measurements(self):
        """DARWIN_VK の実測4点照合(2026-07-25 M1・USkeyboard)。"""
        from backend.shared.hotkey_handler import DARWIN_VK
        assert (DARWIN_VK['r'], DARWIN_VK['1'],
                DARWIN_VK['0'], DARWIN_VK['2']) == (15, 18, 29, 19)


class TestNormalizeHotkeyEntry:
    def test_normalizes_uppercase_key(self):
        entry = normalize_hotkey_entry(
            {'ctrl': True, 'alt': 0, 'shift': None, 'key': 'R'})
        assert entry == {'ctrl': True, 'alt': False, 'shift': False, 'key': 'r'}

    def test_rejects_bare_key_without_modifier(self):
        assert normalize_hotkey_entry(
            {'ctrl': False, 'alt': False, 'shift': False, 'key': '1'}) is None

    def test_rejects_unknown_key_and_non_dict(self):
        assert normalize_hotkey_entry(
            {'ctrl': True, 'alt': False, 'shift': False, 'key': 'esc'}) is None
        assert normalize_hotkey_entry(None) is None
        assert normalize_hotkey_entry("Ctrl+1") is None


class TestConflictAndLabel:
    def test_label(self):
        assert hotkey_label(
            {'ctrl': True, 'alt': False, 'shift': True, 'key': 'r'}) == 'Ctrl+Shift+R'

    def test_identical_combos_conflict(self):
        a = {'ctrl': True, 'alt': False, 'shift': False, 'key': '1'}
        assert hotkeys_conflict(a, dict(a)) is True

    def test_subset_modifiers_conflict(self):
        """Ctrl+1 と Ctrl+Shift+1 は「必要キー⊆押下集合」で両方発火する。"""
        a = {'ctrl': True, 'alt': False, 'shift': False, 'key': '1'}
        b = {'ctrl': True, 'alt': False, 'shift': True, 'key': '1'}
        assert hotkeys_conflict(a, b) is True

    def test_different_key_or_disjoint_modifiers_ok(self):
        a = {'ctrl': True, 'alt': False, 'shift': False, 'key': '1'}
        b = {'ctrl': True, 'alt': False, 'shift': False, 'key': '0'}
        assert hotkeys_conflict(a, b) is False
        c = {'ctrl': False, 'alt': True, 'shift': True, 'key': '1'}
        assert hotkeys_conflict(a, c) is False


class TestMacSettingsDriven:
    def test_build_honors_settings_on_mac(self, monkeypatch):
        """darwin も設定駆動(2026-07-25 M1実測で3層ロック解除済み)。"""
        import backend.shared.hotkey_handler as hh
        import backend.shared.settings_store as ss
        monkeypatch.setattr(hh, 'IS_MAC', True)
        monkeypatch.setattr(
            ss, 'get_setting',
            lambda cat, key, default=None:
                {'ctrl': True, 'alt': False, 'shift': False, 'key': 'r'})
        handler = hh.HotkeyHandler()
        combos = [c.key_combination for c in handler.hotkeys]
        assert {Key.ctrl, KeyCode.from_vk(15)} in combos  # r = kVK_ANSI_R
        assert {Key.ctrl, KeyCode.from_char('r')} in combos


class TestControlCharTranslation:
    def test_ctrl_letter_control_char_translates_back(self):
        """Ctrl+R は '\\x12' で届く → 'r' の KeyCode へ翻訳される。"""
        key = HotkeyHandler._translate_control_char(KeyCode.from_char('\x12'))
        assert key == KeyCode.from_char('r')

    def test_non_letter_control_char_is_dropped(self):
        assert HotkeyHandler._translate_control_char(
            KeyCode.from_char('\x1b')) is None

    def test_plain_keys_pass_through(self):
        plain = KeyCode.from_char('a')
        assert HotkeyHandler._translate_control_char(plain) == plain
        assert HotkeyHandler._translate_control_char(Key.ctrl) == Key.ctrl
