"""
backend/shared/prompt_i18n.py

Prompt-language resolution and text catalog for LLM-bound fixed texts
(shared/foundation layer).

Unlike the UI language (``display.language``, app-wide, resolved once per
process in ``i18n.py``), the prompt language is a per-character value derived
from the character's STT setting (``faster_whisper_config.language``) and is
resolved per call. Every module that assembles LLM-bound text must obtain the
language through :func:`get_prompt_language` — reading the config key directly
is what produced the historical ja/en default drift.

Catalog layout (``<repo>/locales/prompts/``):

- ``<lang>/<name>.txt`` — long fixed sections, one file per section, loaded by
  :func:`prompt_section`. The file's single trailing newline is a file
  terminator, not content, and is stripped on load.
- ``<lang>.json`` — flat dot-keyed short strings, loaded by
  :func:`prompt_text`.

Both APIs take an explicit ``language`` and fall back ``language → en → ja``.
Placeholders use ``{name}`` tokens substituted via ``str.replace`` (NOT
``str.format``): several prompts contain literal braces — e.g. the memory
extraction prompt sends ``{{"additions": []}}`` verbatim to the LLM — and
format-escaping them would silently change the byte contract.

These texts are part of the model-facing byte contract (pinned by
tests/golden). Editing a ja file is a behavior change, not a translation
touch-up. ``.gitattributes`` pins the catalog to LF for the same reason.

Like settings_store/i18n, this module depends only on the standard library and
other shared-leaf modules, so any layer may depend on it downward.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# locales/prompts/ sits at the repo root, two levels above backend/shared/.
PROMPTS_DIR = Path(__file__).resolve().parents[2] / 'locales' / 'prompts'

_FALLBACK_ORDER = ('en', 'ja')

_sections: Dict[Tuple[str, str], Optional[str]] = {}
_catalogs: Dict[str, Dict[str, str]] = {}

# Unified default when a character config carries no STT language.
# (All shipped character configs set the key explicitly, so this only
# governs genuinely unset configs — unified to English for publication.)
DEFAULT_PROMPT_LANGUAGE = 'en'


def get_prompt_language(config: Dict[str, Any]) -> str:
    """Return the prompt language code (e.g. 'ja', 'en') for a character config.

    Single source of truth for LLM-bound text language. Accepts a raw
    character-config dict (already unwrapped from the ``{success, result}``
    response form) and tolerates ``None``.
    """
    if not isinstance(config, dict):
        return DEFAULT_PROMPT_LANGUAGE
    return config.get('faster_whisper_config', {}).get('language', DEFAULT_PROMPT_LANGUAGE)


def _language_chain(language: str):
    """Yield lookup languages: requested first, then en, then ja (deduped)."""
    yield language
    for lang in _FALLBACK_ORDER:
        if lang != language:
            yield lang


def _apply_replacements(text: str, replacements: Dict[str, Any]) -> str:
    for key, value in replacements.items():
        text = text.replace('{' + key + '}', str(value))
    return text


def _load_section(language: str, name: str) -> Optional[str]:
    key = (language, name)
    if key not in _sections:
        path = PROMPTS_DIR / language / f'{name}.txt'
        try:
            raw = path.read_text(encoding='utf-8-sig')
            # 末尾の改行1個はファイル終端であって本文ではない(生成規約)
            _sections[key] = raw[:-1] if raw.endswith('\n') else raw
        except FileNotFoundError:
            _sections[key] = None
        except Exception as e:
            logger.error(f"prompt_i18n: failed to load section {path}: {e}")
            _sections[key] = None
    return _sections[key]


def _load_catalog(language: str) -> Dict[str, str]:
    if language not in _catalogs:
        path = PROMPTS_DIR / f'{language}.json'
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                _catalogs[language] = json.load(f)
        except FileNotFoundError:
            _catalogs[language] = {}
        except Exception as e:
            logger.error(f"prompt_i18n: failed to load catalog {path}: {e}")
            _catalogs[language] = {}
    return _catalogs[language]


def available_prompt_languages() -> list:
    """Return language codes that have a section directory under locales/prompts/."""
    try:
        return sorted(p.name for p in PROMPTS_DIR.iterdir() if p.is_dir())
    except OSError:
        return list(_FALLBACK_ORDER)


def prompt_section(name: str, language: str, **replacements: Any) -> str:
    """Return a long fixed prompt section from ``locales/prompts/<lang>/<name>.txt``.

    ``{key}`` tokens are substituted with the given keyword values via
    ``str.replace`` (literal braces in the text pass through untouched).
    A section missing in every language returns '' — a conversation must not
    crash on a catalog gap, and golden/smoke tests are where gaps fail.
    """
    for lang in _language_chain(language):
        text = _load_section(lang, name)
        if text is not None:
            return _apply_replacements(text, replacements) if replacements else text
    logger.error(f"prompt_i18n: section '{name}' missing in all languages")
    return ''


def prompt_text(key: str, language: str, **replacements: Any) -> str:
    """Return a short LLM-bound string from ``locales/prompts/<lang>.json``.

    Same substitution and fallback rules as :func:`prompt_section`; a key
    missing in every language returns the key itself (visible in prompt logs,
    never a crash).
    """
    for lang in _language_chain(language):
        value = _load_catalog(lang).get(key)
        if value is not None:
            return _apply_replacements(value, replacements) if replacements else value
    logger.error(f"prompt_i18n: key '{key}' missing in all languages")
    return key
