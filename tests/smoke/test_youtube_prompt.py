"""Smoke tests for backend/youtube/youtube_prompt.py — prompt assembly
(spec §5 の構成契約) and the deny convention."""

from backend.youtube import youtube_prompt as yp


def _messages(language="ja", rules="", video_hist=None, user_hist=None,
              description="長い概要欄 " * 400):
    config = {"youtube_system_prompt": "私はサラ。"}
    comment = {
        "comment_id": "c1",
        "author_name": "視聴者A",
        "published_at": "2026-07-08T12:00:00Z",
        "text": "面白かった！",
    }
    return yp.build_reply_messages(
        config, comment,
        video_context={"title": "動画タイトル", "description": description},
        video_hist=video_hist or [],
        user_hist=user_hist or [],
        rules=rules,
        language=language,
    )


def test_prompt_sections_and_contracts():
    msgs = _messages(rules="政治の話題には返信しない")
    assert [m["role"] for m in msgs] == ["system", "user"]
    system, user = msgs[0]["content"], msgs[1]["content"]

    # Section presence + order (spec §5)
    for tag in ("<youtube_system_prompt>", "<youtube_instructions>",
                "<injection_guard>", "<rules>", "<video_context>"):
        assert tag in system
    assert system.index("<youtube_instructions>") < system.index("<injection_guard>")
    assert system.index("<injection_guard>") < system.index("<rules>")

    # J5: long-term memory is never injected
    assert "<long_term_memory>" not in system
    # J6: no current-time line; the TARGET COMMENT's publishedAt is injected
    assert "2026-07-08T12:00:00Z" in user
    assert "<target_comment>" in user
    # J7: full description, no truncation
    assert system.count("長い概要欄") == 400
    # Deny marker reaches the model verbatim (frozen, not localized)
    assert "{{DENY}}" in system or "{{DENY}}" in user


def test_prompt_empty_sections_omitted():
    msgs = _messages()
    system = msgs[0]["content"]
    assert "<rules>" not in system
    assert "<video_history>" not in system
    assert "<user_history>" not in system


def test_prompt_history_sections():
    hist = [{"author_name": "A", "comment_text": "こんにちは", "reply_text": "やあ！"}]
    msgs = _messages(video_hist=hist, user_hist=hist)
    system = msgs[0]["content"]
    assert "<video_history>" in system and "<user_history>" in system
    assert "こんにちは" in system and "やあ！" in system


def test_deny_judgement():
    assert yp.is_deny("{{DENY}}")
    assert yp.is_deny("  {{deny}}  ")
    assert yp.is_deny("すみません、{{DENY}}")   # 含めばdeny
    assert yp.is_deny("DENY")                   # 明らかな表記揺れの保険
    assert not yp.is_deny("")                    # 空はgeneration_failed側
    assert not yp.is_deny("これは普通の返信です。denyという単語を含む文。")
    assert not yp.is_deny("面白かったです！")
