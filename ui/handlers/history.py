"""
ui/handlers/history.py

Short-term conversation-history reset handler (SL3 memory; long-term memory
is preserved). Extracted verbatim from ui/app.py (B11d) as a self-contained
UI handler leaf (registry-seam home: ui/handlers/<feature>.py). Only
mechanical change: relative-import depth .components -> ..components.
"""

import logging

import gradio as gr

import backend
from backend.shared.i18n import t
from ..state import app_state

logger = logging.getLogger(__name__)


def handle_refresh_history():
    """
    Handle the refresh history button click.
    Resets short-term conversation history while preserving long-term memory.

    Returns:
        Tuple of (feedback_message, chat_html_update)
    """
    from ..components import get_chat_history

    # Check if character is selected
    if not app_state.active_character_id:
        return (
            f"""<div style='color: #ffab91; padding: 10px; border-radius: 5px; background-color: #2e2a1a;'>
                {t('hdl.history.no_char_selected')}
            </div>""",
            gr.update()
        )

    # Double-check memory-task status (extraction / relationship update)
    if backend.is_memory_task_running():
        return (
            f"""<div style='color: #ffe082; padding: 10px; border-radius: 5px; background-color: #2e2a1a;'>
                {t('hdl.history.reset_blocked')}
            </div>""",
            gr.update()
        )

    try:
        # Call backend to reset short-term history
        result = backend.reset_short_term_history(app_state.active_character_id)

        if result.get("success"):
            # Clear UI chat history
            app_state.clear_chat_history()

            # Log the action
            app_state.add_log_message("info", "Short-term conversation history has been reset")

            return (
                f"""<div style='color: #81c784; padding: 10px; border-radius: 5px; background-color: #1a2e1a;'>
                    {t('hdl.history.reset_done')}
                </div>""",
                get_chat_history()
            )
        else:
            error_msg = result.get("error", "Unknown error")
            app_state.add_log_message("error", f"Failed to reset history: {error_msg}")
            return (
                f"""<div style='color: #ef9a9a; padding: 10px; border-radius: 5px; background-color: #2e1a1a;'>
                    {t('hdl.history.reset_failed', error=error_msg)}
                </div>""",
                gr.update()
            )

    except Exception as e:
        # ERROR は1本(1障害1トースト・稜裁定 2026-08-02)
        app_state.add_log_message("error", f"Error resetting history: {str(e)}")
        return (
            f"""<div style='color: #ef9a9a; padding: 10px; border-radius: 5px; background-color: #2e1a1a;'>
                {t('hdl.history.reset_error', error=str(e))}
            </div>""",
            gr.update()
        )


def _format_elyth_sessions_html(character_id):
    """Format ELYTH session logs as HTML for the History page."""
    from backend.elyth.elyth_memory import load_session_logs
    sessions = load_session_logs(character_id, limit=5)
    if not sessions:
        return f'<div class="history-empty">{t("hdl.history.no_elyth_sessions")}</div>'

    html = '<div style="font-family:sans-serif;padding:4px;">'
    reason_colors = {
        "natural": "#4caf50", "max_turns": "#ff9800",
        "cutoff": "#2196f3", "manual_stop": "#9c27b0",
        "api_error": "#f44336", "auth_error": "#f44336",
    }
    for session in reversed(sessions):
        ts = session.get("timestamp", "?")[:19]
        reason = session.get("end_reason", "?")
        turns = session.get("turns", [])
        color = reason_colors.get(reason, "#666")

        html += '<div style="border:1px solid #444;border-radius:8px;margin:8px 0;padding:12px;background:rgba(255,255,255,0.02);">'
        html += '<div style="display:flex;justify-content:space-between;margin-bottom:8px;">'
        html += f'<b style="font-size:13px;">{ts}</b>'
        html += f'<span style="color:{color};font-weight:bold;font-size:12px;">{reason}</span></div>'

        for turn in turns:
            content = turn.get("content", "")
            if content:
                safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                html += f'<div style="padding:4px 8px;margin:2px 0;font-size:12px;color:#ccc;border-left:2px solid #666;background:rgba(255,255,255,0.03);">💭 {safe}</div>'
            for tc in turn.get("metadata", {}).get("elyth_tool_calls", []):
                name = tc.get("name", "?")
                html += f'<div style="padding:2px 8px;font-size:11px;color:#888;">🔧 {name}</div>'

        html += '</div>'
    html += '</div>'
    return html


def _resolve_youtube_reply_status(comment, posted_ids, queued):
    """3値突合: posted の真実源は history.json・待機中は投稿キュー・どちらにも
    無い generated は投稿されず終了(ワーカー側スキップの理由は永続されず復元不能)。"""
    status = comment.get("status", "?")
    if status == "dry_run":
        return "🧪 dry_run", "#2196f3"
    if status == "skipped":
        reason = comment.get("skip_reason", "")
        label = f"⏭ skipped: {reason}" if reason else "⏭ skipped"
        return label, "#ff9800"
    cid = comment.get("comment_id", "")
    if cid in posted_ids:
        return "✅ posted", "#4caf50"
    if cid in queued:
        return f"⏳ in queue ({queued[cid]})", "#9e9e9e"
    return "⚠ not posted", "#f44336"


def _format_youtube_sessions_html():
    """Format YouTube reply-session logs as HTML for the History page."""
    from backend.youtube import youtube_store as store
    from backend.youtube.youtube_session_logger import load_session_history

    def key(session):
        return session.get("session_id") or session.get("started_at") or ""

    doc = load_session_history()
    sessions = doc.get("sessions") or []
    last_run = doc.get("last_run")
    if not sessions and not last_run:
        return f'<div class="history-empty">{t("hdl.history.no_youtube_sessions")}</div>'

    posted_ids = {h.get("comment_id") for h in store.load_history()}
    queued = {q.get("comment_id"): q.get("status", "") for q in store.load_queue()}

    def esc(text):
        return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    reason_colors = {
        "completed": "#4caf50", "dry_run_completed": "#2196f3",
        "no_new_comments": "#9e9e9e", "daily_limit": "#ff9800",
        "crash": "#f44336", "exception": "#f44336", "auth_error": "#f44336",
    }

    html = '<div style="font-family:sans-serif;padding:4px;">'
    # FIFO外の最新実行(0コメント等)は1行だけ添える — ベータ中の生存確認用
    if last_run and key(last_run) not in {key(s) for s in sessions}:
        ts = (last_run.get("started_at") or "?")[:19]
        reason = last_run.get("end_reason") or "running"
        html += (f'<div style="font-size:12px;color:#888;margin:4px 0;">'
                 f'{esc(t("hdl.history.youtube_last_run", ts=ts, reason=reason))}</div>')

    for session in reversed(sessions):
        ts = (session.get("started_at") or "?")[:19]
        reason = session.get("end_reason") or "?"
        color = reason_colors.get(reason, "#666")
        badge = ('<span style="color:#2196f3;font-size:11px;border:1px solid #2196f3;'
                 'border-radius:4px;padding:0 4px;margin-right:6px;">🧪 dry run</span>'
                 if session.get("dry_run") else "")
        html += '<div style="border:1px solid #444;border-radius:8px;margin:8px 0;padding:12px;background:rgba(255,255,255,0.02);">'
        html += '<div style="display:flex;justify-content:space-between;margin-bottom:8px;">'
        html += (f'<b style="font-size:13px;">{ts} <span style="color:#888;font-weight:normal;">'
                 f'{esc(session.get("character_name", ""))}</span></b>')
        html += f'<span>{badge}<span style="color:{color};font-weight:bold;font-size:12px;">{esc(reason)}</span></span></div>'

        for c in session.get("comments", []):
            status_label, status_color = _resolve_youtube_reply_status(c, posted_ids, queued)
            video_id = c.get("video_id", "")
            ctx = store.get_video_context(video_id) or {}
            title = ctx.get("title") or video_id or "?"
            html += '<div style="padding:6px 8px;margin:6px 0;border-left:2px solid #666;background:rgba(255,255,255,0.03);font-size:12px;">'
            html += f'<div style="color:#888;font-size:11px;">🎬 {esc(title)}</div>'
            html += (f'<div style="color:#ccc;margin-top:2px;white-space:pre-wrap;">💬 '
                     f'<b>{esc(c.get("author_name", "?"))}</b>: {esc(c.get("comment_text", ""))}</div>')
            reply = c.get("reply_text", "")
            if reply:
                html += f'<div style="color:#e0e0e0;margin-top:4px;padding-left:14px;white-space:pre-wrap;">↳ {esc(reply)}</div>'
            html += f'<div style="color:{status_color};font-size:11px;margin-top:4px;">{esc(status_label)}</div>'
            html += '</div>'
        html += '</div>'
    html += '</div>'
    return html


def _youtube_tab_updates(character_id):
    """Tab visibility + content for the YouTube reply-session tab: shown only
    while the character configured as youtube.character_id is selected."""
    try:
        from backend.shared.settings_store import get_setting
        yt_char = get_setting("youtube", "character_id", "") or ""
    except Exception as e:
        logger.error(f"Error reading youtube.character_id: {e}")
        yt_char = ""
    if not character_id or character_id != yt_char:
        return gr.update(visible=False), gr.update()
    try:
        html = _format_youtube_sessions_html()
    except Exception as e:
        logger.error(f"Error formatting YouTube sessions: {e}")
        html = f'<div class="error-message">{t("hdl.history.load_youtube_sessions_error", error=str(e))}</div>'
    return gr.update(visible=True), gr.update(value=html)


def on_history_character_select(character_id):
    """Handle character selection in history page"""
    from ..components import format_history_messages, format_memory_entries

    yt_tab_update, yt_sessions_update = _youtube_tab_updates(character_id)

    if not character_id:
        empty_msg = t('hdl.history.select_char_hint')
        empty_html = f'<div class="history-empty">{empty_msg}</div>'
        return [
            gr.update(value=empty_msg),
            gr.update(value=""),
            gr.update(value=empty_html),
            gr.update(value=empty_html),
            gr.update(value=empty_html),
            gr.update(value=empty_html),
            gr.update(value=empty_html),
            yt_tab_update,
            yt_sessions_update,
            None
        ]
    
    # Get memory data
    result = backend.get_character_memory_data(character_id)
    
    # Debug log the result structure
    logger.debug(f"Memory data result type: {type(result)}")
    logger.debug(f"Memory data result: {result}")
    
    if not result.get('success'):
        error_msg = result.get('error', 'Unknown error')
        error_html = f'<div class="error-message">❌ {error_msg}</div>'
        return [
            gr.update(value=t('common.error_with', error=error_msg)),
            gr.update(value=""),
            gr.update(value=error_html),
            gr.update(value=error_html),
            gr.update(value=error_html),
            gr.update(value=error_html),
            gr.update(value=error_html),
            yt_tab_update,
            yt_sessions_update,
            character_id
        ]
    
    data = result.get('result', {})
    stats = data.get('stats', {})
    
    # Debug log the data structure
    logger.debug(f"Data type: {type(data)}")
    logger.debug(f"Stats: {stats}")
    logger.debug(f"Short term type: {type(data.get('short_term', []))}")
    logger.debug(f"Long term type: {type(data.get('long_term', []))}")
    
    # Format stats
    stats_md = t(
        'hdl.history.stats',
        total=stats.get('total_messages', 0),
        memories=stats.get('total_memories', 0),
        unprocessed=stats.get('unprocessed', 0),
    )

    # Format countdown
    countdown_md = t('hdl.history.countdown', count=data.get('countdown', 0))
    
    # Format displays with error handling
    try:
        short_html = format_history_messages(
            data.get('short_term', []), data.get('countdown', 0),
            unprocessed=stats.get('unprocessed', 0))
    except Exception as e:
        logger.error(f"Error formatting short-term messages: {e}")
        # 生データ全量は debug: error だとWSトースト本文にダンプが出る
        logger.debug(f"Short-term data: {data.get('short_term', [])}")
        short_html = f'<div class="error-message">{t("hdl.history.format_messages_error", error=str(e))}</div>'
    
    try:
        long_html = format_memory_entries(data.get('long_term', []))
    except Exception as e:
        logger.error(f"Error formatting long-term memories: {e}")
        # 生データ全量は debug(short-term 側と同じ理由)
        logger.debug(f"Long-term data: {data.get('long_term', [])}")
        long_html = f'<div class="error-message">{t("hdl.history.format_memories_error", error=str(e))}</div>'
    
    # Format notes display
    try:
        from backend.memory.note_manager import get_note_display_html
        notes_html = get_note_display_html(character_id)
    except Exception as e:
        logger.error(f"Error formatting notes: {e}")
        notes_html = f'<div class="error-message">{t("hdl.history.load_notes_error", error=str(e))}</div>'

    # Format ELYTH notes
    try:
        from backend.elyth.elyth_note_manager import get_elyth_note_display_html
        elyth_notes_html = get_elyth_note_display_html(character_id)
    except Exception as e:
        logger.error(f"Error formatting ELYTH notes: {e}")
        elyth_notes_html = f'<div class="error-message">{t("hdl.history.load_elyth_notes_error", error=str(e))}</div>'

    # Format ELYTH session logs
    try:
        elyth_sessions_html = _format_elyth_sessions_html(character_id)
    except Exception as e:
        logger.error(f"Error formatting ELYTH sessions: {e}")
        elyth_sessions_html = f'<div class="error-message">{t("hdl.history.load_elyth_sessions_error", error=str(e))}</div>'

    return [
        gr.update(value=stats_md),
        gr.update(value=countdown_md),
        gr.update(value=short_html),
        gr.update(value=long_html),
        gr.update(value=notes_html),
        gr.update(value=elyth_notes_html),
        gr.update(value=elyth_sessions_html),
        yt_tab_update,
        yt_sessions_update,
        character_id
    ]


def handle_add_memory(category, content, character_id):
    """Handle manual memory addition."""
    if not character_id:
        return gr.update(), t('hdl.history.add_no_char'), gr.update()
    if not content or not content.strip():
        return gr.update(), t('hdl.history.add_empty'), gr.update()

    result = backend.add_memory(character_id, category, content)
    if result.get('success'):
        # Refresh display
        updates = on_history_character_select(character_id)
        return gr.update(value=""), t('hdl.history.memory_added'), updates[3]
    else:
        error = result.get('error', 'Unknown error')
        return gr.update(), t('common.error_with', error=error), gr.update()


def handle_memory_action(action_str, character_id):
    """Handle pin/edit/delete actions from JS bridge."""
    if not action_str or not character_id:
        return gr.update(), ""

    parts = action_str.split('::', 2)
    action = parts[0] if len(parts) > 0 else ""
    mem_id = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    if not action or not mem_id:
        return gr.update(), ""

    result = {"success": False, "error": "Unknown action"}

    if action == "pin":
        pinned = extra.lower() == "true"
        result = backend.pin_memory(character_id, mem_id, pinned)
    elif action == "delete":
        result = backend.delete_memory(character_id, mem_id)
    elif action == "edit":
        if extra:
            result = backend.edit_memory(character_id, mem_id, extra)
        else:
            result = {"success": False, "error": "No content provided"}

    if result.get('success'):
        updates = on_history_character_select(character_id)
        return updates[3], ""  # long_term_display
    else:
        error = result.get('error', 'Unknown error')
        return gr.update(), t('common.error_with', error=error)
