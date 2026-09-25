"""
tests/smoke/test_i18n_locales.py

Locale catalogs must stay translation-complete. At runtime a missing key
silently falls back to English (backend.shared.i18n.t), so this test is where
translation gaps actually fail. en.json is the reference catalog: every other
locales/*.json must carry the same key set, and each value's {placeholders}
must match the reference (a renamed placeholder would make str.format fail
softly and ship the raw template).
"""

import json
import string
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parents[2] / 'locales'
REFERENCE = 'en.json'


def _load(path: Path) -> dict:
    # utf-8-sig: tolerate a BOM from hand edits (same as the runtime loader)
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _placeholders(text: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _other_catalogs():
    return sorted(p for p in LOCALES_DIR.glob('*.json') if p.name != REFERENCE)


def test_locales_exist():
    assert (LOCALES_DIR / REFERENCE).exists(), f"reference catalog {REFERENCE} missing"
    assert _other_catalogs(), "no translated catalogs found next to en.json"


def test_catalogs_share_reference_keyset():
    reference_keys = set(_load(LOCALES_DIR / REFERENCE))
    for path in _other_catalogs():
        catalog_keys = set(_load(path))
        missing = reference_keys - catalog_keys
        extra = catalog_keys - reference_keys
        assert not missing and not extra, (
            f"{path.name}: missing keys {sorted(missing)}, extra keys {sorted(extra)}"
        )


def test_placeholders_match_reference():
    reference = _load(LOCALES_DIR / REFERENCE)
    for path in _other_catalogs():
        catalog = _load(path)
        for key, ref_text in reference.items():
            if key.startswith('_') or key not in catalog:
                continue  # meta keys differ by design; missing keys fail above
            assert _placeholders(catalog[key]) == _placeholders(ref_text), (
                f"{path.name}: placeholder mismatch for key '{key}'"
            )
