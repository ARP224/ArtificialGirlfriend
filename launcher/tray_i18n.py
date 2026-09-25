"""
launcher/tray_i18n.py

トレイランチャー用の最小 i18n ローダー（標準ライブラリのみ・backend 非依存）。

トレイは backend/ui を import しない独立プロセスのため、backend/shared/i18n.py と
同じ locales/<lang>.json と user_settings.json (display.language) を自前で読む。
解決規則は本家と同一: 設定値(≠'auto'かつカタログあり) → OS の UI 言語 → 'en'。
キー欠落は en → キー名そのもの、へフォールバックし表示を壊さない。
言語はプロセス起動時に一度だけ解決される（切替の反映は AG 本体と同じく再起動後）。
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# locales/ はリポジトリ直下 = launcher/ の一つ上
LOCALES_DIR = Path(__file__).resolve().parents[1] / 'locales'

DEFAULT_LANGUAGE = 'en'

_language: Optional[str] = None
_catalogs: Dict[str, Dict[str, str]] = {}


def _settings_file() -> Path:
    # backend/shared/settings_store.py の get_settings_dir() と同じ場所を参照
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', '.')) / 'ArtificialGirlfriend' / 'user_settings.json'
    return Path.home() / '.config' / 'ArtificialGirlfriend' / 'user_settings.json'


def _load_catalog(lang: str) -> Dict[str, str]:
    if lang not in _catalogs:
        path = LOCALES_DIR / f'{lang}.json'
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                _catalogs[lang] = json.load(f)
        except Exception as e:
            logger.error(f"tray_i18n: failed to load catalog {path}: {e}")
            _catalogs[lang] = {}
    return _catalogs[lang]


def _detect_os_language() -> str:
    try:
        import locale
        if os.name == 'nt':
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            loc = locale.windows_locale.get(langid, '')
        else:
            loc = locale.getlocale()[0] or os.environ.get('LANG', '')
        code = str(loc).replace('-', '_').split('_')[0].lower()
        if code and (LOCALES_DIR / f'{code}.json').exists():
            return code
    except Exception as e:
        logger.warning(f"tray_i18n: OS language detection failed: {e}")
    return DEFAULT_LANGUAGE


def _resolve_language() -> str:
    setting = 'auto'
    try:
        path = _settings_file()
        if path.exists():
            with open(path, 'r', encoding='utf-8-sig') as f:
                setting = json.load(f).get('display', {}).get('language', 'auto')
    except Exception as e:
        logger.warning(f"tray_i18n: failed to read settings: {e}")
    if setting and setting != 'auto' and (LOCALES_DIR / f'{setting}.json').exists():
        return setting
    return _detect_os_language()


def current_language() -> str:
    global _language
    if _language is None:
        _language = _resolve_language()
        logger.info(f"tray_i18n: language resolved to '{_language}'")
    return _language


def t(key: str, **kwargs) -> str:
    """トレイ用翻訳。backend/shared/i18n.t と同じ規約（{name} プレースホルダ）。"""
    text = _load_catalog(current_language()).get(key)
    if text is None:
        text = _load_catalog(DEFAULT_LANGUAGE).get(key)
        if text is None:
            logger.warning(f"tray_i18n: missing key '{key}'")
            return key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError) as e:
            logger.warning(f"tray_i18n: bad placeholder for key '{key}': {e}")
    return text
