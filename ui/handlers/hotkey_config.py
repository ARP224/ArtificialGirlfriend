"""
ui/handlers/hotkey_config.py

Voice Inputタブ「キーボードショートカット」の録音ホットキーエディタ
(修飾キーチェックボックス+キードロップダウン+保存)。

キー入力キャプチャ欄は IME/自ホットキー発火/OSショートカットの3層問題が
あるため採用しない(AG Client Addon 実踏 2026-07-16=チェックボックス化が
正解)。保存で settings_store へ永続化し、hotkey_handler.apply_config() が
リスナーを join 付きで再構築する=アプリ再起動不要で即時反映。
検証/組み立て/衝突判定の実体は backend.shared.hotkey_handler(真実源)。
"""

import logging

from backend.shared.hotkey_handler import (
    DEFAULT_HOTKEYS,
    VALID_HOTKEY_KEYS,
    get_hotkey_handler,
    hotkey_label,
    hotkeys_conflict,
    normalize_hotkey_entry,
)
from backend.shared.i18n import t
from backend.shared.settings_store import get_setting, update_setting

logger = logging.getLogger(__name__)


def key_choices():
    """キードロップダウンの候補 (label=大文字表示, value=正準小文字)。"""
    return [(k.upper(), k) for k in VALID_HOTKEY_KEYS]


def current_hotkey_values():
    """(start, stop) の正準化済み設定値(壊れていれば既定)。"""
    result = []
    for name in ('start', 'stop'):
        entry = normalize_hotkey_entry(get_setting('hotkeys', name, None))
        result.append(entry or dict(DEFAULT_HOTKEYS[name]))
    return result[0], result[1]


def save_hotkeys(start_ctrl, start_alt, start_shift, start_key,
                 stop_ctrl, stop_alt, stop_shift, stop_key) -> str:
    """検証→保存→リスナー再構築。返り値は結果表示 Markdown 文言。

    不正(修飾なし/衝突)は保存せずに理由を返す。保存失敗・再適用失敗も
    無言にしない(保存失敗は再起動後に巻き戻るため必ず可視化)。
    """
    start = {'ctrl': bool(start_ctrl), 'alt': bool(start_alt),
             'shift': bool(start_shift), 'key': (start_key or '').lower()}
    stop = {'ctrl': bool(stop_ctrl), 'alt': bool(stop_alt),
            'shift': bool(stop_shift), 'key': (stop_key or '').lower()}

    if (normalize_hotkey_entry(start) is None
            or normalize_hotkey_entry(stop) is None):
        return t('hdl.hotkey.need_modifier')

    if hotkeys_conflict(start, stop):
        return t('hdl.hotkey.conflict')

    ok = update_setting('hotkeys', 'start', start)
    ok = update_setting('hotkeys', 'stop', stop) and ok
    if not ok:
        return t('hdl.hotkey.save_failed')

    try:
        applied = get_hotkey_handler().apply_config()
    except Exception as e:
        logger.error(f"Hotkey re-apply failed: {e}", exc_info=True)
        applied = False
    if not applied:
        return t('hdl.hotkey.apply_failed')
    return t('hdl.hotkey.saved',
             start=hotkey_label(start), stop=hotkey_label(stop))
