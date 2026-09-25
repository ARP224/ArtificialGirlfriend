"""
backend/youtube/youtube_prompt.py

Dedicated prompt builder for YouTube comment replies (spec §5) — like ELYTH,
core's prompt_builder is NOT used; sections are assembled with XML-like tags.

Section order (spec §5):
    <youtube_system_prompt>  character's youtube_system_prompt field
                             (NO current time — J6: not a realtime conversation)
    <youtube_instructions>   fixed catalog text (plain-text replies, concise,
                             deny convention)
    <injection_guard>        fixed, user-uneditable anti-injection text
    <rules>                  user-configured rules (settings youtube.rules)
    <video_context>          video title + FULL description (J7: no truncation;
                             YouTube caps descriptions at 5000 chars anyway)
    <video_history>          last <=5 posted exchanges on the same video
    <user_history>           last <=5 posted exchanges with the same commenter
                             (matched by channel id, never display name)
    <target_comment>         the comment being replied to, including its
                             publishedAt (J6: gives the model a sense of
                             "this comment is 3 days old") — sent as the
                             user message together with the reply request.

Long-term memory is NEVER injected (J5 裁定: 検索クエリ=第三者が書ける
コメント本文なので、私的記憶の狙い撃ち検索→全世界公開への漏洩経路になる).

Deny convention (spec §5): the model is told to output the frozen marker
``{{DENY}}`` (verbatim, never localized) and nothing else when it declines.
Judgement is case-insensitive substring match, plus the bare word "deny" as
an obvious-variant safety net. False-deny (a commenter writing {{DENY}}) is
fail-safe — the comment simply gets no reply.
"""

import logging
from typing import Any, Dict, List

from backend.shared.prompt_i18n import prompt_section, prompt_text

logger = logging.getLogger(__name__)

# Frozen marker — verbatim contract, never localized ({{...}} passes through
# prompt_i18n's str.replace untouched; same class as the memory-extraction
# {{"additions": []}} literals).
DENY_MARKER = "{{DENY}}"


def is_deny(output: str) -> bool:
    """True if the generated output means 'do not reply' (spec §5 deny規約).

    Case-insensitive containment of {{deny}} anywhere in the output, plus
    the bare single word 'deny' as an obvious misspelling of the contract.
    Empty output is NOT deny — the caller treats it as generation_failed.
    """
    if not output or not output.strip():
        return False
    low = output.strip().lower()
    return "{{deny}}" in low or low == "deny"


def _format_history(entries: List[Dict[str, Any]], language: str) -> str:
    lines = []
    for e in entries:
        lines.append(prompt_text(
            "youtube.history_entry", language,
            author=e.get("author_name", ""),
            comment=e.get("comment_text", ""),
            reply=e.get("reply_text", ""),
        ))
    return "\n".join(lines)


def build_reply_messages(
    config: Dict[str, Any],
    comment: Dict[str, str],
    video_context: Dict[str, str],
    video_hist: List[Dict[str, Any]],
    user_hist: List[Dict[str, Any]],
    rules: str,
    language: str,
) -> List[Dict[str, str]]:
    """Build the [system, user] messages for one comment → one reply."""
    parts = []
    parts.append(
        "<youtube_system_prompt>\n"
        f"{config.get('youtube_system_prompt', '')}\n"
        "</youtube_system_prompt>"
    )
    parts.append(
        "<youtube_instructions>\n"
        f"{prompt_section('youtube_instructions', language)}\n"
        "</youtube_instructions>"
    )
    parts.append(
        "<injection_guard>\n"
        f"{prompt_section('youtube_injection_guard', language)}\n"
        "</injection_guard>"
    )
    if rules and rules.strip():
        parts.append(f"<rules>\n{rules.strip()}\n</rules>")
    if video_context:
        ctx_text = prompt_text(
            "youtube.video_context", language,
            title=video_context.get("title", ""),
            description=video_context.get("description", ""),
        )
        parts.append(f"<video_context>\n{ctx_text}\n</video_context>")
    if video_hist:
        parts.append(
            f"<video_history>\n{_format_history(video_hist, language)}\n</video_history>"
        )
    if user_hist:
        parts.append(
            f"<user_history>\n{_format_history(user_hist, language)}\n</user_history>"
        )

    system_msg = "\n\n".join(parts)

    target = prompt_text(
        "youtube.target_comment", language,
        author=comment.get("author_name", ""),
        published_at=comment.get("published_at", ""),
        text=comment.get("text", ""),
    )
    user_msg = (
        f"<target_comment>\n{target}\n</target_comment>\n\n"
        f"{prompt_text('youtube.reply_request', language)}"
    )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]
