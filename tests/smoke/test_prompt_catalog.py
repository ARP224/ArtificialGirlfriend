"""
tests/smoke/test_prompt_catalog.py

LLM向けプロンプトカタログ (locales/prompts/) の翻訳完全性ガード。
実行時は言語fallback(指定→en→ja)で欠落キーが黙って他言語になるため、
翻訳漏れが実際に落ちるのはこのテスト。

UI版(test_i18n_locales.py)と違い、プロンプトは str.replace 置換方式で
literal brace ({{"additions": []}} 等) を含むため、プレースホルダ検査は
string.Formatter ではなく regex (\\{[a-z_]+\\}) で行う。

byte契約ガード: セクション txt は LF のみ・末尾改行ちょうど1個
(ローダーが1個strip する生成規約。CRLF 混入や末尾改行の増減は
LLMに届く文字列を変える = .gitattributes と二重の防壁)。

文字衛生ガード: 非CJK言語(en)の資材にCJK文字を混入させない。en プロンプト
中の 【】 等は英語キャラの文脈を日本語側へ引き、「同じ言語で書く」系指示や
生成言語そのものを倒す実害があった(サブOS実機 2026-08-02 日本語ノート事故)。
"""

import json
import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / 'locales' / 'prompts'
LANGUAGES = ('ja', 'en')  # ja が原文・en が翻訳。言語追加時はここに足す
NON_CJK_LANGUAGES = ('en',)  # CJK混入を禁じる言語。CJK圏以外の言語追加時はここにも足す

_PLACEHOLDER_RE = re.compile(r'\{([a-z_]+)\}')
# ひらがな/カタカナ(拡張含む)/漢字(拡張A含む)/CJK記号・全角形。em dash や
# curly quote 等の正当な非ASCIIは対象外(ASCII縛りにはしない)
_CJK_RE = re.compile(
    r'[　-〿぀-ゟ゠-ヿㇰ-ㇿ'
    r'㐀-䶿一-鿿＀-￯]')


def _placeholders(text: str) -> set:
    return set(_PLACEHOLDER_RE.findall(text))


def _load_catalog(lang: str) -> dict:
    return json.loads((PROMPTS_DIR / f'{lang}.json').read_text(encoding='utf-8-sig'))


def test_catalogs_share_keyset():
    keysets = {lang: {k for k in _load_catalog(lang) if not k.startswith('_')}
               for lang in LANGUAGES}
    reference = keysets[LANGUAGES[0]]
    for lang, keys in keysets.items():
        assert keys == reference, (
            f"{lang}.json: missing {sorted(reference - keys)}, extra {sorted(keys - reference)}"
        )


def test_catalog_placeholders_match():
    catalogs = {lang: _load_catalog(lang) for lang in LANGUAGES}
    reference = catalogs[LANGUAGES[0]]
    for lang, catalog in catalogs.items():
        for key, ref_text in reference.items():
            if key.startswith('_') or key not in catalog:
                continue
            assert _placeholders(catalog[key]) == _placeholders(ref_text), (
                f"{lang}.json: placeholder mismatch for key '{key}'"
            )


def test_sections_share_fileset():
    filesets = {lang: {p.name for p in (PROMPTS_DIR / lang).glob('*.txt')}
                for lang in LANGUAGES}
    reference = filesets[LANGUAGES[0]]
    for lang, names in filesets.items():
        assert names == reference, (
            f"prompts/{lang}/: missing {sorted(reference - names)}, extra {sorted(names - reference)}"
        )


def test_section_placeholders_match():
    reference_dir = PROMPTS_DIR / LANGUAGES[0]
    for ref_path in sorted(reference_dir.glob('*.txt')):
        ref_ph = _placeholders(ref_path.read_text(encoding='utf-8-sig'))
        for lang in LANGUAGES[1:]:
            other = PROMPTS_DIR / lang / ref_path.name
            if not other.exists():
                continue  # fileset test fails on this already
            assert _placeholders(other.read_text(encoding='utf-8-sig')) == ref_ph, (
                f"prompts/{lang}/{ref_path.name}: placeholder mismatch"
            )


def test_tool_description_catalog_matches_code():
    """コード内ツール定義の日本語descriptionと ja.json の tool.* キーの同期ガード。

    ツール定義は「構造=コード・文言=カタログ」(localize_tools)。コード側の
    description はカタログ生成の原文だが localize で常に上書きされるため、
    片方だけ編集すると黙ってズレる。ここで両者の一致を強制する。
    """
    from backend.tools import camera_capture, command_executor, deep_search_tools, \
        image_generator, map_search_tools, talk_theme_tools
    from backend.memory import note_manager
    from backend.elyth import elyth_tools

    raw_tools = (
        camera_capture._CAMERA_TOOLS
        + [command_executor.EXECUTE_COMMAND_TOOL]
        + deep_search_tools._DEEP_SEARCH_TOOLS
        + image_generator._IMAGE_GEN_TOOLS
        + map_search_tools._MAP_SEARCH_TOOLS
        + note_manager._NOTE_TOOLS
        + talk_theme_tools._TALK_THEME_TOOLS
        + elyth_tools._ELYTH_API_TOOLS
        + elyth_tools._ELYTH_NOTE_TOOLS
    )

    def walk(schema, prefix, out):
        for pname, ps in (schema.get('properties') or {}).items():
            if 'description' in ps:
                out[f'{prefix}.{pname}'] = ps['description']
            walk(ps, f'{prefix}.{pname}', out)
            items = ps.get('items')
            if isinstance(items, dict):
                if 'description' in items:
                    out[f'{prefix}.{pname}.items'] = items['description']
                walk(items, f'{prefix}.{pname}.items', out)

    code_keys = {}
    for t in raw_tools:
        code_keys[f"tool.{t['name']}.description"] = t['description']
        walk(t['parameters'], f"tool.{t['name']}.param", code_keys)

    ja = _load_catalog('ja')
    ja_tool_keys = {k: v for k, v in ja.items() if k.startswith('tool.')}
    assert code_keys == ja_tool_keys, (
        f"code-only: {sorted(set(code_keys) - set(ja_tool_keys))}, "
        f"catalog-only: {sorted(set(ja_tool_keys) - set(code_keys))}, "
        f"value-drift: {sorted(k for k in set(code_keys) & set(ja_tool_keys) if code_keys[k] != ja_tool_keys[k])}"
    )


def test_sections_are_lf_with_single_trailing_newline():
    for lang in LANGUAGES:
        for path in sorted((PROMPTS_DIR / lang).glob('*.txt')):
            raw = path.read_bytes()
            assert b'\r' not in raw, f"{path}: CRLF detected (byte contract requires LF)"
            assert raw.endswith(b'\n') and not raw.endswith(b'\n\n'), (
                f"{path}: must end with exactly one trailing newline (loader strips one)"
            )


def test_non_cjk_prompts_have_no_cjk():
    """非CJK言語(en)のLLM到達資材にCJK文字が無いことのガード。

    en資材の 【】 見出し等は毎ターン system に入り「文脈の言語多数決」を
    日本語側へ引く(2026-08-02 ASCII化)。カタログ値とセクション txt の両方を検査。
    """
    for lang in NON_CJK_LANGUAGES:
        for key, value in _load_catalog(lang).items():
            if key.startswith('_'):
                continue
            m = _CJK_RE.search(value)
            assert not m, f"{lang}.json: CJK character {m.group()!r} in key '{key}'"
        for path in sorted((PROMPTS_DIR / lang).glob('*.txt')):
            m = _CJK_RE.search(path.read_text(encoding='utf-8-sig'))
            assert not m, f"{path}: CJK character {m.group()!r} detected"
