"""Prompt assembly golden.

Each scenario drives `ConversationManager._build_prompt_messages` with the clock
frozen, memory mocked, leaf providers patched to fixed values, and a fixed
character config, then snapshots the returned `messages`.

Author discipline (anti-"嘘の緑"): after each scenario first goes green, OPEN the
snapshot under tests/golden/snapshots/ and confirm the section order/content is
the real prompt, the time/memory are frozen, and the fallback (conversation_
manager.py:2874) was NOT taken (it would collapse to a single bare system msg).
"""

from __future__ import annotations

import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))  # make _snapshot importable
from _snapshot import assert_golden, render_messages  # noqa: E402

_FIXT = Path(__file__).parent.parent / "fixtures" / "characters"


def _load(name: str) -> dict:
    return json.loads((_FIXT / name).read_text(encoding="utf-8"))


# --- Scenario 1: ollama, minimal (flags all off, no history) ----------------
def test_s1_01_ollama_minimal(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_ollama_minimal.json")
    patch_leaves()
    state = make_state()
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("nox", user_text="", config=config)

    assert_golden("s1_01_ollama_minimal", render_messages(messages))


# --- Scenario 2: ollama, talk_theme + 20 history + long-term memory ---------
def test_s1_02_ollama_theme_history(
    frozen_time, make_state, make_cm, patch_leaves, fake_memory_manager
):
    config = _load("s1_ollama_theme_history.json")
    patch_leaves()
    mm = fake_memory_manager(
        memories=[
            {"category": "好み", "content": "ユーザーはSF映画が好き。", "id": "m1"},
            {"category": "事実", "content": "ユーザーの名前はリョウ。", "id": "m2"},
        ],
        messages=[
            {"role": "user", "content": "こんばんは"},
            {"role": "assistant", "content": "こんばんは、今日はどんな一日だった？"},
            {"role": "user", "content": "映画を観たよ"},
        ],
    )
    state = make_state(talk_theme_enabled=True, memory_managers={"nox": mm})
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages(
        "nox", user_text="おすすめのSF映画ある？", config=config
    )

    assert_golden("s1_02_ollama_theme_history", render_messages(messages))


# --- Scenario 3: api(anthropic), minimal (connection_info + relationship) ----
def test_s1_03_api_minimal(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_api_minimal.json")
    patch_leaves()
    state = make_state()  # server_mode False -> fixed connection_info string
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_03_api_minimal", render_messages(messages))


# --- Scenario 4: api, command+image+camera+deep_search+map all on -----------
def test_s1_04_api_all_tools(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_api_all_tools.json")
    patch_leaves(
        has_valid_location=True,  # map-tools gate
        google_maps_api_key="test-maps-key",
        image_generation_model=("google", "imagen-test"),
        api_settings={"google": {"api_key": "test-key"}},
    )
    state = make_state(
        command_execution_enabled=True,
        image_generation_enabled=True,
        camera_capture_enabled=True,
        deep_search_enabled=True,
    )
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_04_api_all_tools", render_messages(messages))


# --- Scenario 5: api, note + location + pc_status + screen_capture -----------
def test_s1_05_api_note_location(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_api_note_location.json")
    patch_leaves(location="現在地: テスト市・固定（2026-06-20）")
    state = make_state(
        notes_enabled=True,
        pc_status_enabled=True,
        screen_capture_enabled=True,
    )
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_05_api_note_location", render_messages(messages))


# --- Scenario 6: api, ELYTH mode --------------------------------------------
def test_s1_06_api_elyth(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_api_elyth.json")
    patch_leaves()
    state = make_state(elyth_enabled=True)
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_06_api_elyth", render_messages(messages))


# --- Scenario 7: api, image/document attachments (trim + map dedup) ----------
# Exercises three deterministic transforms in _build_prompt_messages:
#   * MAX_IMAGES_IN_PROMPT (10): 11 image refs -> newest 10 kept (2848).
#   * MAX_DOCUMENTS_IN_PROMPT (10): 11 docs -> oldest 1 dropped, rest embedded
#     into message content (2786). Counts (char_count small) so the COUNT limit
#     fires, not the char limit -> snapshot stays small and honest.
#   * map_search_result dedup: older result -> cleared notice, newest -> full
#     notice (2545).
_IMG = str((Path(__file__).parent.parent / "fixtures" / "attachments" / "s1_img.png").resolve())


def test_s1_07_api_attachments(
    frozen_time, make_state, make_cm, patch_leaves, fake_memory_manager
):
    config = _load("s1_api_attachments.json")
    patch_leaves()
    docs = [
        {"filename": f"doc{i:02d}.txt", "text": f"本文{i:02d}", "char_count": 10}
        for i in range(11)  # 11 > MAX_DOCUMENTS_IN_PROMPT(10) -> oldest dropped
    ]
    mm = fake_memory_manager(
        messages=[
            {"role": "user", "content": "古いMap検索",
             "metadata": {"tool_type": "map_search_result"}},
            {"role": "user", "content": "資料を見て", "documents": docs},
            {"role": "user", "content": "写真たくさん", "images": [_IMG] * 11},
            {"role": "user", "content": "新しいMap検索",
             "metadata": {"tool_type": "map_search_result"}},
        ],
    )
    state = make_state(memory_managers={"lumina": mm})
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="添付を見て", config=config)

    assert_golden("s1_07_api_attachments", render_messages(messages))


# =============================================================================
# English scenarios (プロンプトi18n Phase 3): language:"en" fixtures drive the
# same assembly through the en prompt catalog. Mirrors of s1_04/02/05/06.
# =============================================================================


# --- Scenario 8: EN api, command+image+camera+deep_search+map all on ---------
def test_s1_08_en_api_all_tools(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_en_api_all_tools.json")
    patch_leaves(
        has_valid_location=True,
        google_maps_api_key="test-maps-key",
        image_generation_model=("google", "imagen-test"),
        api_settings={"google": {"api_key": "test-key"}},
    )
    state = make_state(
        command_execution_enabled=True,
        image_generation_enabled=True,
        camera_capture_enabled=True,
        deep_search_enabled=True,
    )
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_08_en_api_all_tools", render_messages(messages))


# --- Scenario 9: EN ollama, talk_theme + history + long-term memory ----------
def test_s1_09_en_ollama_theme_history(
    frozen_time, make_state, make_cm, patch_leaves, fake_memory_manager
):
    config = _load("s1_en_ollama_theme_history.json")
    patch_leaves()
    mm = fake_memory_manager(
        memories=[
            {"category": "preference", "content": "The user likes sci-fi movies.", "id": "m1"},
            {"category": "fact", "content": "The user's name is Ryo.", "id": "m2"},
        ],
        messages=[
            {"role": "user", "content": "Good evening"},
            {"role": "assistant", "content": "Good evening! How was your day?"},
            {"role": "user", "content": "I watched a movie"},
        ],
    )
    state = make_state(talk_theme_enabled=True, memory_managers={"nox": mm})
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages(
        "nox", user_text="Any sci-fi movie recommendations?", config=config
    )

    assert_golden("s1_09_en_ollama_theme_history", render_messages(messages))


# --- Scenario 10: EN api, note + location + pc_status + screen_capture -------
def test_s1_10_en_api_note_location(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_en_api_note_location.json")
    patch_leaves(location="Current location: Test City, fixed (2026-06-20)")
    state = make_state(
        notes_enabled=True,
        pc_status_enabled=True,
        screen_capture_enabled=True,
    )
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_10_en_api_note_location", render_messages(messages))


# --- Scenario 11: EN api, ELYTH mode ------------------------------------------
def test_s1_11_en_api_elyth(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_en_api_elyth.json")
    patch_leaves()
    state = make_state(elyth_enabled=True)
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("lumina", user_text="", config=config)

    assert_golden("s1_11_en_api_elyth", render_messages(messages))


# --- Scenario 12: ollama, command toggle ON -> gate keeps instructions out ---
# capability判定不能(未照会・旧Ollama)のモデルはfail-closed(2026-08-11 稜裁定
# =per-model 2軸化。旧: プロバイダ一律ゲート 2026-08-02)なので、トグルONでも
# <command_instructions> は落ちる。Snapshot must stay identical to the
# minimal ollama prompt.
def test_s1_12_ollama_command_gated(frozen_time, make_state, make_cm, patch_leaves):
    config = _load("s1_ollama_minimal.json")
    patch_leaves()
    state = make_state(command_execution_enabled=True)
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("nox", user_text="", config=config)

    assert "<command_instructions>" not in render_messages(messages)
    assert_golden("s1_12_ollama_command_gated", render_messages(messages))


# --- Scenario 13: ollama, tools対応モデル -> tools軸の機能が開く (C5) --------
# 2026-08-11 稜裁定(per-model 2軸)の新経路: capabilityにtoolsを持つOllama
# モデルは command_instructions / notes / FC版talk_theme がAPIプロバイダと
# 同様にプロンプトへ載る。visionは無いモデルなので画像系ツールは閉じたまま。
def test_s1_13_ollama_tools_capable(
    frozen_time, make_state, make_cm, patch_leaves, fake_ollama_caps
):
    config = _load("s1_ollama_tools_capable.json")
    patch_leaves()
    fake_ollama_caps("qwen-tools-test:latest", tools=True, vision=False)
    state = make_state(
        command_execution_enabled=True,
        notes_enabled=True,
        talk_theme_enabled=True,
    )
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("nox", user_text="", config=config)

    rendered = render_messages(messages)
    assert "<command_instructions>" in rendered
    assert "<notes>" in rendered
    assert_golden("s1_13_ollama_tools_capable", rendered)


# --- Scenario 14: ollama, document history -> embedded (C7) ------------------
# ドキュメントはテキスト化されるため capability 非依存で全Ollamaモデルに
# 載る(2026-08-11 稜裁定=C7)。旧実装は履歴から全文書を剥がしていた。
# モデルは capability 未登録(判定不能)のまま=文書がツール軸と無関係である
# ことも同時に固定する。
def test_s1_14_ollama_documents(
    frozen_time, make_state, make_cm, patch_leaves, fake_memory_manager
):
    config = _load("s1_ollama_minimal.json")
    patch_leaves()
    mm = fake_memory_manager(
        messages=[
            {"role": "user", "content": "資料を見て",
             "documents": [{"filename": "doc01.txt", "text": "本文01", "char_count": 10}]},
            {"role": "assistant", "content": "読んだよ"},
        ],
    )
    state = make_state(memory_managers={"nox": mm})
    cm = make_cm(state, config)

    messages = cm._build_prompt_messages("nox", user_text="どう思う？", config=config)

    rendered = render_messages(messages)
    assert "<attached_document" in rendered
    assert_golden("s1_14_ollama_documents", rendered)
