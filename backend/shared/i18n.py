"""
backend/shared/i18n.py

UI-string translation core for Artificial Girlfriend (shared/foundation layer).

Flat dot-keyed JSON catalogs live in ``<repo>/locales/<lang>.json`` (en.json is
the reference catalog). Adding a language = adding one JSON file with the same
key set; the System page dropdown discovers it via ``available_languages()``.
Each catalog carries its own native display name under the ``_language_name``
key.

The active language is resolved once per process: the persisted setting
``display.language`` wins, and ``'auto'`` (the default) falls back to the OS
UI language. Gradio labels are fixed at Blocks build time, so a language
change takes effect on the next application restart.

Missing keys fall back to English, then to the key string itself — a
translation gap must never break the UI (tests/smoke/test_i18n_locales.py is
where gaps actually fail).

Like settings_store, this module depends only on the standard library and
other shared-leaf modules, so any layer may depend on it downward. The tray
launcher is an independent process that must not import backend; it reads the
same JSON files with its own small loader.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backend.shared.settings_store import get_setting

logger = logging.getLogger(__name__)

# locales/ sits at the repo root, two levels above backend/shared/.
LOCALES_DIR = Path(__file__).resolve().parents[2] / 'locales'

DEFAULT_LANGUAGE = 'en'

_language: Optional[str] = None
_catalogs: Dict[str, Dict[str, str]] = {}


def _load_catalog(lang: str) -> Dict[str, str]:
    """Load (and cache) one locale catalog; an unreadable file yields {}."""
    if lang not in _catalogs:
        path = LOCALES_DIR / f'{lang}.json'
        try:
            # utf-8-sig: tolerate a BOM from hand edits (see launch_config.py)
            with open(path, 'r', encoding='utf-8-sig') as f:
                _catalogs[lang] = json.load(f)
        except Exception as e:
            logger.error(f"i18n: failed to load catalog {path}: {e}")
            _catalogs[lang] = {}
    return _catalogs[lang]


def detect_os_language() -> str:
    """Return the OS UI language if a catalog for it exists, else English."""
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
        logger.warning(f"i18n: OS language detection failed: {e}")
    return DEFAULT_LANGUAGE


def resolve_language() -> str:
    """Resolve the UI language: persisted setting, or OS language on 'auto'."""
    setting = get_setting('display', 'language', 'auto')
    if setting and setting != 'auto':
        if (LOCALES_DIR / f'{setting}.json').exists():
            return setting
        logger.warning(f"i18n: no catalog for configured language '{setting}', using auto detection")
    return detect_os_language()


def current_language() -> str:
    """The active language code, resolved once per process."""
    global _language
    if _language is None:
        _language = resolve_language()
        logger.info(f"i18n: UI language resolved to '{_language}'")
    return _language


def t(key: str, **kwargs) -> str:
    """Translate a UI string key in the active language.

    Placeholders use str.format names: t('x.y', count=3) for "{count} items".
    """
    text = _load_catalog(current_language()).get(key)
    if text is None:
        text = _load_catalog(DEFAULT_LANGUAGE).get(key)
        if text is None:
            logger.warning(f"i18n: missing key '{key}'")
            return key
        logger.warning(f"i18n: key '{key}' missing in '{current_language()}', fell back to '{DEFAULT_LANGUAGE}'")
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError) as e:
            logger.warning(f"i18n: bad placeholder for key '{key}': {e}")
    return text


def available_languages() -> List[Tuple[str, str]]:
    """All catalogs in locales/ as (code, native display name) pairs."""
    langs = []
    try:
        for path in sorted(LOCALES_DIR.glob('*.json')):
            code = path.stem
            langs.append((code, _load_catalog(code).get('_language_name', code)))
    except Exception as e:
        logger.error(f"i18n: failed to scan {LOCALES_DIR}: {e}")
    return langs
