"""
tests/smoke/test_elyth_prompt_settings.py

ELYTH prompt-privacy settings (spec v6 §8, 稜裁定 2026-08-15): what
user-derived context enters ELYTH sessions. Verifies the safe defaults
(notes OFF / relationship OFF / RAG = activity_only), the session-prompt
gating, and the per-mode categories passed to multi_query_search — the
J5-consistency fix (third-party-writable query text must not fish
user-conversation memories onto a public surface by default).
"""

import json
from types import SimpleNamespace

import backend.elyth.elyth_session_manager as sm
from backend.elyth.elyth_session_manager import (
    ELYTH_ACTIVITY_MEMORY_CATEGORIES, ELYTHSessionManager,
    get_elyth_prompt_settings,
)


def _mgr():
    mgr = ELYTHSessionManager.__new__(ELYTHSessionManager)  # no scheduler
    return mgr


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------

def test_defaults_are_safe_side(monkeypatch):
    import backend.shared.settings_store as store
    monkeypatch.setattr(store, "get_setting",
                        lambda section, key, default=None: default)
    s = get_elyth_prompt_settings()
    assert s == {"include_user_notes": False,
                 "include_user_relationship": False,
                 "memory_rag_mode": "activity_only"}


def test_invalid_rag_mode_falls_back(monkeypatch):
    import backend.shared.settings_store as store
    monkeypatch.setattr(store, "get_setting",
                        lambda section, key, default=None:
                        "banana" if key == "memory_rag_mode" else default)
    assert get_elyth_prompt_settings()["memory_rag_mode"] == "activity_only"


def test_settings_store_failure_degrades_to_defaults(monkeypatch):
    import backend.shared.settings_store as store

    def boom(*a, **k):
        raise RuntimeError("store broken")

    monkeypatch.setattr(store, "get_setting", boom)
    s = get_elyth_prompt_settings()
    assert s["include_user_notes"] is False
    assert s["memory_rag_mode"] == "activity_only"


# ---------------------------------------------------------------------------
# Session prompt gating
# ---------------------------------------------------------------------------

def _patch_user_context(monkeypatch):
    import backend.memory.note_manager as note_manager
    import backend.memory.relationship_manager as relationship_manager
    monkeypatch.setattr(note_manager, "load_note",
                        lambda cid: ["ユーザーの秘密のメモ"])
    monkeypatch.setattr(relationship_manager, "build_relationship_prompt",
                        lambda cid, lang: "ユーザーとの関係性データ")


def _settings(notes=False, rel=False, rag="activity_only"):
    return {"include_user_notes": notes, "include_user_relationship": rel,
            "memory_rag_mode": rag}


def test_prompt_excludes_user_context_by_default(monkeypatch):
    _patch_user_context(monkeypatch)
    monkeypatch.setattr(sm, "get_elyth_prompt_settings", lambda: _settings())
    system_msg, _ = _mgr()._build_elyth_prompt(
        "no-such-char", {"elyth_system_prompt": "SNSでの人格"}, "anthropic")
    assert "ユーザーの秘密のメモ" not in system_msg
    assert "ユーザーとの関係性データ" not in system_msg
    assert "<notes>" not in system_msg
    assert "<relationship>" not in system_msg
    assert "SNSでの人格" in system_msg  # the prompt itself still builds


def test_prompt_includes_user_context_when_opted_in(monkeypatch):
    _patch_user_context(monkeypatch)
    monkeypatch.setattr(sm, "get_elyth_prompt_settings",
                        lambda: _settings(notes=True, rel=True, rag="all"))
    system_msg, _ = _mgr()._build_elyth_prompt(
        "no-such-char", {"elyth_system_prompt": "SNSでの人格"}, "anthropic")
    assert "ユーザーの秘密のメモ" in system_msg
    assert "ユーザーとの関係性データ" in system_msg


# ---------------------------------------------------------------------------
# RAG injection modes
# ---------------------------------------------------------------------------

class _FakeMM:
    def __init__(self):
        self.calls = []

    def multi_query_search(self, queries, token_budget, categories=None):
        self.calls.append({"queries": queries, "categories": categories})
        return ["[elyth] 先週ネクタリカと流星群の話をした"]


def _mgr_with_mm():
    mgr = _mgr()
    mm = _FakeMM()
    mgr.state = SimpleNamespace(memory_managers={"c1": mm})
    return mgr, mm


RESULT = json.dumps({"posts": [{"content": "流星群を見たよ"}]}, ensure_ascii=False)


def test_rag_activity_only_passes_categories(monkeypatch):
    mgr, mm = _mgr_with_mm()
    monkeypatch.setattr(sm, "get_elyth_prompt_settings",
                        lambda: _settings(rag="activity_only"))
    out = mgr._inject_rag("c1", "get_thread", RESULT, "ja")
    assert mm.calls[0]["categories"] == ELYTH_ACTIVITY_MEMORY_CATEGORIES
    assert "流星群の話をした" in out  # injected


def test_rag_all_passes_no_category_filter(monkeypatch):
    mgr, mm = _mgr_with_mm()
    monkeypatch.setattr(sm, "get_elyth_prompt_settings",
                        lambda: _settings(rag="all"))
    mgr._inject_rag("c1", "get_thread", RESULT, "ja")
    assert mm.calls[0]["categories"] is None


def test_rag_off_skips_search_entirely(monkeypatch):
    mgr, mm = _mgr_with_mm()
    monkeypatch.setattr(sm, "get_elyth_prompt_settings",
                        lambda: _settings(rag="off"))
    out = mgr._inject_rag("c1", "get_thread", RESULT, "ja")
    assert mm.calls == []          # no retrieval at all
    assert out == RESULT           # tool result untouched
