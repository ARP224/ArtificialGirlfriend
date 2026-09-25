"""チューニングUIのヒント「目安: X」/"Typical: X" と配布既定値の一致契約。

num_predict で既定200とヒント「目安800」が初期実装から乖離していた
（2026-08-15 に 800 へ統一）。同じ乖離を別パラメータで再発させないための
契約: 9パラメータ×2言語のヒント末尾の数値 = TUNING_HARDCODED_DEFAULTS。
"""

import json
import re
from pathlib import Path

import pytest

from backend.shared.constants import TUNING_HARDCODED_DEFAULTS

_LOCALES = Path(__file__).resolve().parents[2] / "locales"
_PATTERNS = {
    "ja": re.compile(r"目安:\s*(-?[0-9.]+)\s*$"),
    "en": re.compile(r"Typical:\s*(-?[0-9.]+)\s*$"),
}


@pytest.mark.parametrize("lang", sorted(_PATTERNS))
@pytest.mark.parametrize("param", sorted(TUNING_HARDCODED_DEFAULTS))
def test_hint_typical_value_matches_default(lang, param):
    catalog = json.loads((_LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    hint = catalog[f"tuning.{param}.hint"]
    m = _PATTERNS[lang].search(hint)
    assert m, f"{lang}: tuning.{param}.hint に目安値が無い: {hint!r}"
    assert float(m.group(1)) == float(TUNING_HARDCODED_DEFAULTS[param]), (
        f"{lang}: tuning.{param}.hint の目安 {m.group(1)} != 既定 "
        f"{TUNING_HARDCODED_DEFAULTS[param]}")
