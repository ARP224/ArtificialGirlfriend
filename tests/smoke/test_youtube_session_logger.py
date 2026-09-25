"""Smoke tests for backend/youtube/youtube_session_logger.py — the v2
last_run + replied-session FIFO format (spec §6: 実行ログはlogs/側・
0コメント=ノーセッション扱い・FIFO 3件 — 稜裁定 2026-07-20).

The logger takes output_path directly, so everything runs against tmp_path;
no network, no LLM, no product-code bending.
"""

import json

from backend.youtube.youtube_session_logger import (
    MAX_SESSIONS_KEPT,
    YouTubeSessionLogger,
    load_session_history,
)


def _run_session(path, n_comments, end_reason="completed", dry_run=False):
    slog = YouTubeSessionLogger(output_path=path)
    slog.start_session("char-1", "紗羅", dry_run, "UCtarget")
    for i in range(n_comments):
        slog.log_comment(
            {"comment_id": f"c{i}", "video_id": "vid1",
             "author_name": "viewer", "text": f"hi {i}"},
            "generated", reply_text=f"reply {i}")
    slog.end_session(end_reason)
    return slog


def test_fresh_session_writes_last_run_only(tmp_path):
    path = tmp_path / "log.json"
    slog = YouTubeSessionLogger(output_path=path)
    slog.start_session("char-1", "紗羅", True, "UCtarget")
    doc = load_session_history(path)
    assert doc["last_run"]["character_id"] == "char-1"
    assert doc["last_run"]["session_id"]
    assert doc["sessions"] == []


def test_zero_comment_session_not_promoted(tmp_path):
    path = tmp_path / "log.json"
    _run_session(path, n_comments=0, end_reason="no_new_comments")
    doc = load_session_history(path)
    assert doc["last_run"]["end_reason"] == "no_new_comments"
    assert doc["sessions"] == []  # ノーセッション扱い


def test_replied_session_promoted_and_fifo_trims(tmp_path):
    path = tmp_path / "log.json"
    ids = []
    for i in range(MAX_SESSIONS_KEPT + 1):
        slog = _run_session(path, n_comments=1)
        ids.append(slog._data["session_id"])
    doc = load_session_history(path)
    kept = [s["session_id"] for s in doc["sessions"]]
    assert len(kept) == MAX_SESSIONS_KEPT
    assert kept == ids[1:]  # oldest dropped, order preserved
    assert doc["last_run"]["session_id"] == ids[-1]


def test_zero_comment_run_keeps_previous_replied_sessions(tmp_path):
    path = tmp_path / "log.json"
    replied = _run_session(path, n_comments=2)._data["session_id"]
    _run_session(path, n_comments=0, end_reason="no_new_comments")
    doc = load_session_history(path)
    assert [s["session_id"] for s in doc["sessions"]] == [replied]
    assert doc["last_run"]["end_reason"] == "no_new_comments"


def test_crashed_replied_run_rescued_on_next_start(tmp_path):
    path = tmp_path / "log.json"
    crashed = YouTubeSessionLogger(output_path=path)
    crashed.start_session("char-1", "紗羅", False, "UCtarget")
    crashed.log_comment({"comment_id": "c0", "video_id": "vid1",
                         "author_name": "viewer", "text": "hi"},
                        "generated", reply_text="reply")
    crashed_id = crashed._data["session_id"]
    # no end_session — process death; next run must rescue it
    nxt = YouTubeSessionLogger(output_path=path)
    nxt.start_session("char-1", "紗羅", False, "UCtarget")
    doc = load_session_history(path)
    rescued = [s for s in doc["sessions"] if s["session_id"] == crashed_id]
    assert len(rescued) == 1
    assert rescued[0]["end_reason"] == "crash"
    assert rescued[0]["comments"][0]["reply_text"] == "reply"


def test_crashed_zero_comment_run_not_rescued(tmp_path):
    path = tmp_path / "log.json"
    crashed = YouTubeSessionLogger(output_path=path)
    crashed.start_session("char-1", "紗羅", False, "UCtarget")
    nxt = YouTubeSessionLogger(output_path=path)
    nxt.start_session("char-1", "紗羅", False, "UCtarget")
    assert load_session_history(path)["sessions"] == []


def test_ended_session_not_promoted_twice(tmp_path):
    path = tmp_path / "log.json"
    ended = _run_session(path, n_comments=1)._data["session_id"]
    # end_session promoted it; the next start must not duplicate it
    nxt = YouTubeSessionLogger(output_path=path)
    nxt.start_session("char-1", "紗羅", False, "UCtarget")
    doc = load_session_history(path)
    assert [s["session_id"] for s in doc["sessions"]] == [ended]


def test_v1_format_migrates_on_load_and_start(tmp_path):
    path = tmp_path / "log.json"
    v1 = {
        "started_at": "2026-07-12T02:10:32",
        "character_id": "char-1", "character_name": "紗羅",
        "target_channel_id": "UCtarget", "dry_run": True,
        "events": [], "ended_at": "2026-07-12T02:11:00",
        "end_reason": "dry_run_completed",
        "comments": [{"comment_id": "c0", "comment_text": "hi",
                      "reply_text": "reply", "status": "dry_run"}],
    }
    path.write_text(json.dumps(v1), encoding="utf-8")
    doc = load_session_history(path)
    assert doc["last_run"]["started_at"] == "2026-07-12T02:10:32"
    assert len(doc["sessions"]) == 1  # replied v1 session enters the FIFO
    # a new run keeps the migrated session exactly once (keyed by started_at)
    nxt = YouTubeSessionLogger(output_path=path)
    nxt.start_session("char-1", "紗羅", False, "UCtarget")
    doc = load_session_history(path)
    assert [s.get("started_at") for s in doc["sessions"]] == ["2026-07-12T02:10:32"]


def test_v1_zero_comment_file_migrates_to_empty_fifo(tmp_path):
    path = tmp_path / "log.json"
    v1 = {"started_at": "2026-07-12T02:10:32", "events": [], "comments": [],
          "ended_at": "2026-07-12T02:11:00", "end_reason": "no_new_comments"}
    path.write_text(json.dumps(v1), encoding="utf-8")
    doc = load_session_history(path)
    assert doc["last_run"]["started_at"] == "2026-07-12T02:10:32"
    assert doc["sessions"] == []


def test_missing_and_corrupt_files_tolerated(tmp_path):
    missing = load_session_history(tmp_path / "nope.json")
    assert missing == {"last_run": None, "sessions": []}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_session_history(bad) == {"last_run": None, "sessions": []}
    # a corrupt file must not stop a new session from starting
    slog = YouTubeSessionLogger(output_path=bad)
    slog.start_session("char-1", "紗羅", False, "UCtarget")
    assert load_session_history(bad)["last_run"]["character_id"] == "char-1"
