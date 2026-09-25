"""Utility Panel の無効表現の契約（稜裁定 2026-08-15）。

無効なボタンは「グレー(opacity 0.5)+disabled+tooltip」の1形式のみ。
横線(line-through)は廃止。従属無効(親トグルOFF)にも理由 tooltip を出し、
利用不能理由がある場合はそちらが tooltip に勝つ。
"""

import re

from backend.shared.i18n import t
from ui.utility_panel import _build_utility_panel_html


def _btn(html, elem_id):
    m = re.search(rf'<button id="{elem_id}"(.*?)>', html, re.S)
    assert m, elem_id
    return m.group(1)


def test_no_line_through_anywhere_even_when_everything_is_blocked():
    reasons = {f: "blocked" for f in (
        "notes", "image_generation", "screen_capture", "camera_capture",
        "ambient_camera", "deep_search", "elyth", "command_execution",
        "motion_appear")}
    html = _build_utility_panel_html(
        pc_status=False, camera_capture=False,
        availability=reasons, server_mode=True)
    assert "line-through" not in html
    # 全ゲート対象がグレー+disabled になっている
    for eid in ("appear-btn", "pc-status-toggle-btn", "screen-capture-toggle-btn",
                "command-execution-toggle-btn", "notes-toggle-btn",
                "image-gen-toggle-btn", "camera-capture-toggle-btn",
                "ambient-camera-toggle-btn", "elyth-toggle-btn",
                "deep-search-toggle-btn"):
        attrs = _btn(html, eid)
        assert "opacity: 0.5" in attrs and " disabled" in attrs, eid


def test_dependent_disable_gets_its_own_tooltip():
    html = _build_utility_panel_html(pc_status=False, camera_capture=False)
    sc = _btn(html, "screen-capture-toggle-btn")
    amb = _btn(html, "ambient-camera-toggle-btn")
    assert " disabled" in sc and t("utility.needs_pc_status") in sc
    assert " disabled" in amb and t("utility.needs_camera") in amb


def test_availability_reason_wins_over_dependent_tooltip():
    html = _build_utility_panel_html(
        pc_status=False, camera_capture=False,
        availability={"screen_capture": "no-vision", "ambient_camera": "no-vision"})
    sc = _btn(html, "screen-capture-toggle-btn")
    amb = _btn(html, "ambient-camera-toggle-btn")
    assert 'title="no-vision"' in sc and t("utility.needs_pc_status") not in sc
    assert 'title="no-vision"' in amb and t("utility.needs_camera") not in amb


def test_parent_on_and_available_has_no_tooltip_and_is_enabled():
    html = _build_utility_panel_html(pc_status=True, camera_capture=True)
    for eid in ("screen-capture-toggle-btn", "ambient-camera-toggle-btn"):
        attrs = _btn(html, eid)
        assert " disabled" not in attrs and "title=" not in attrs, eid
