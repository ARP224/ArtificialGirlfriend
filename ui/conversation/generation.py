"""
ui/conversation/generation.py

AI応答生成の中核責務。バリデーション・backend 呼び出し・応答後の
TTS/タイマー/UI通知のオーケストレーション(handle_generate_reply)を持つ。
"""

import logging
import traceback
from typing import Optional

from ..state import app_state, show_warning_popup
from ..constants import ERROR_MESSAGE_PREFIX
from ..error_handler import handle_llm_operation, user_error
from backend.shared.i18n import t
from backend.shared.timing_logger import end_turn

import backend

from .auto_prompt import start_auto_prompt_timer
from .notify import notify_ui_update
from .tts_playback import play_tts_response, _play_tts_for_step, handle_tts_error

logger = logging.getLogger(__name__)


@handle_llm_operation("AI reply generation")
def _generate_ai_reply_wrapper(user_text: str, character_id: str, images: list = None,
                               documents: list = None):
    """Generate AI reply with specialized LLM error handling."""
    result = backend.generate_reply(user_text, character_id, images=images, documents=documents)

    # Handle structured response with error checking
    if isinstance(result, dict):
        if not result.get("success", False):
            error_type = result.get("error_type", "UNKNOWN")
            error_msg = result.get("error", "Unknown error during generation")

            if error_type == "RESOURCE_LIMIT":
                raise RuntimeError(user_error('err.low_resources'))
            elif error_type == "VALIDATION":
                raise RuntimeError(user_error('err.input', error=error_msg))
            elif error_type == "SERVICE":
                raise RuntimeError(user_error('err.service_unavailable'))
            elif error_type == "TIMEOUT":
                # backend は TIMEOUT を返すのに UI 側にマッピングが無く、
                # err.generic(英語原文込み)に落ちていた(稜裁定 2026-08-02)
                raise RuntimeError(user_error('err.generation_timeout'))
            else:
                raise RuntimeError(user_error('err.generic', error=error_msg))

        # Extract response if successful
        ai_reply = result.get("response", "")
        if not ai_reply:
            raise RuntimeError(user_error('err.empty_response'))

        # Check for command execution steps
        steps = result.get("steps")
        if steps:
            app_state._pending_command_steps = steps
    else:
        # Legacy string response
        ai_reply = result

    return ai_reply


def validate_active_character() -> Optional[str]:
    """
    Validate that an active character is selected.
    
    Returns:
        Optional[str]: Character ID if valid, None if not
    """
    if not app_state.active_character_id:
        app_state.add_log_message("warning", "No active character selected. Cannot generate reply.")
        return None
    return app_state.active_character_id


def generate_ai_response(user_text: str, character_id: str, images: list = None,
                         documents: list = None) -> str:
    """
    Generate AI response for user text.

    Args:
        user_text: The user's input text
        character_id: The active character ID
        images: Optional list of image file paths attached to this message
        documents: Optional list of document dicts attached to this message

    Returns:
        str: The AI's response or error message
    """
    app_state.add_log_message("info", "Generating AI response...")

    try:
        # Check if backend has required function
        if not hasattr(backend, 'generate_reply'):
            # user_error 形式で返す: 素の文字列だと is_error_response をすり抜けて
            # TTS がエラー文を読み上げてしまう(M9 の残穴)
            raise RuntimeError(user_error('err.backend_not_initialized'))

        # Generate AI reply using wrapper with LLM-specific error handling
        return _generate_ai_reply_wrapper(user_text, character_id, images=images, documents=documents)
    except RuntimeError as e:
        # wrapper 側(handle_llm_operation)が ERROR ログ済み。ここで error で
        # 重ねるとトーストが1障害2枚になるため warning 止まり(稜裁定
        # 2026-08-02)。ユーザーへは戻り値がステータス行で届く。
        app_state.add_log_message("warning", f"AI reply error: {e}")
        return str(e)  # Error messages are already ERROR_MESSAGE_PREFIX-marked
    except Exception as e:
        app_state.add_log_message("error", f"Unexpected error generating AI reply: {e}")
        return user_error('err.reply_failed', error=str(e))


def is_error_response(response: str) -> bool:
    """
    Check if the AI response is an error message.
    
    Args:
        response: The AI response text
        
    Returns:
        bool: True if response is an error
    """
    if not isinstance(response, str):
        return False

    # user_error() が付ける言語非依存マーカーで判定する
    return response.startswith(ERROR_MESSAGE_PREFIX)


def handle_generate_reply(user_text: str, images: list = None,
                          documents: list = None) -> str:
    """
    Generate AI reply for user message.
    Updates chat with AI response and triggers TTS playback.

    Args:
        user_text: The transcribed user text
        images: Optional list of image file paths attached to this message
        documents: Optional list of document dicts attached to this message

    Returns:
        str: The generated AI reply
    """
    import uuid

    # 会話が既に終了していたら生成を始めない(稜裁定 2026-08-21)。
    # is_generatingが立つ前の隙間(テキスト送信直後〜生成開始・音声の
    # 文字起こし中)にEndが通った場合の受け皿。ここで止めればターンは
    # 始まらず、終了済み会話へのコミットも終了後のTTSも起きない。
    if not app_state.conversation_started:
        app_state.add_log_message("info", "Generation aborted: conversation ended before turn start")
        return user_error('err.conversation_ended')

    # Validate character selection
    char_id = validate_active_character()
    if not char_id:
        return "No active character selected."

    # Create unique request ID
    request_id = str(uuid.uuid4())

    # Check if there's already a response being generated
    if app_state.current_request_id and app_state.response_generating:
        app_state.add_log_message("info", "Cancelling previous response generation")
        # Note: Backend would need to support request cancellation for full implementation

    app_state.current_request_id = request_id

    try:
        # Set the generating flag to show a loading indicator in the UI (if not already set)
        if not app_state.response_generating:
            app_state.response_generating = True

        # Generate AI response
        ai_reply = generate_ai_response(user_text, char_id, images=images, documents=documents)
        
        # Check if this request is still current (hasn't been superseded)
        if app_state.current_request_id != request_id:
            app_state.add_log_message("info", f"Discarding superseded response for request {request_id}")
            # 注: 旧来この文言は ERROR_PREFIXES に含まれず「通常応答」として扱われて
            # いた。挙動維持のため user_error にはしない(t のみ)。
            return t('gen.cancelled')
        
        # Check if response is an error
        if is_error_response(ai_reply):
            return ai_reply

        # Add to chat history with character name
        char_name = "AI"
        if app_state.active_character_id:
            try:
                config_response = backend.load_character_config(app_state.active_character_id)
                if isinstance(config_response, dict) and 'result' in config_response:
                    char_config = config_response.get('result', {})
                else:
                    char_config = config_response
                char_name = char_config.get('name', 'AI')
            except:
                pass
        
        # Determine speechless mode once
        from backend.backend import _backend_state
        speechless = (_backend_state.speechless_enabled if _backend_state else False) if app_state.audio_output_available else True

        # Process command execution steps with sequential TTS per ai_text
        if hasattr(app_state, '_pending_command_steps') and app_state._pending_command_steps:
            import html as html_mod
            for step in app_state._pending_command_steps:
                if step.get("type") == "ai_text":
                    content = step["content"]
                    if step.get("pre_displayed"):
                        # Already shown + TTS'd via WS before approval;
                        # just add to chat_history for persistence (no TTS/notify)
                        app_state.append_chat_message(char_name, content, is_ai=True)
                        continue
                    # Play TTS FIRST (blocks until done), then show text
                    if not speechless:
                        try:
                            _play_tts_for_step(content)
                        except Exception as e:
                            logger.error(f"TTS step error: {e}")
                    app_state.append_chat_message(char_name, content, is_ai=True)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="command_step")
                elif step.get("type") == "command":
                    # 整形表は command_format に一本化(リロード経路 history.py と共有)
                    from .command_format import render_command_step_html
                    cmd_html = render_command_step_html(
                        step.get("status", ""),
                        step.get("command", ""),
                        step.get("reason", ""),
                        step.get("result", ""),
                    )
                    if step.get("pre_displayed"):
                        # Already shown via WS; just add to chat_history for persistence
                        app_state.append_chat_message("COMMAND", cmd_html, is_ai=False)
                        continue
                    app_state.append_chat_message("COMMAND", cmd_html, is_ai=False)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="command_step")
                elif step.get("type") == "talk_theme":
                    # 整形は theme_format に一本化(リロード経路 history.py と共有)
                    from .theme_format import render_talk_theme_html
                    action = step.get("action", "")
                    theme = step.get("theme", "")
                    theme_html = render_talk_theme_html(
                        "set" if action == "set_talk_theme" else "clear", theme)
                    if step.get("pre_displayed"):
                        app_state.append_chat_message("TALK_THEME", theme_html, is_ai=False)
                        continue
                    app_state.append_chat_message("TALK_THEME", theme_html, is_ai=False)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="theme_step")
                elif step.get("type") == "image_gen":
                    prompt_text = step.get("prompt", "")
                    status = step.get("status", "error")
                    full_path = step.get("image_path", "")

                    # Store lightweight text; images rendered from paths in get_chat_history()
                    if status == "success":
                        ig_text = f"IMAGE_GEN_SUCCESS:{prompt_text}"
                        ig_images = [full_path] if full_path else []
                    else:
                        result_text = step.get("result", "")
                        ig_text = f"IMAGE_GEN_FAILED:{result_text}"
                        ig_images = []

                    if step.get("pre_displayed"):
                        app_state.append_chat_message("IMAGE_GEN", ig_text, is_ai=False, images=ig_images)
                        continue
                    app_state.append_chat_message("IMAGE_GEN", ig_text, is_ai=False, images=ig_images)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="image_gen_step")
                elif step.get("type") == "camera_capture":
                    reason = step.get("reason", "")
                    status = step.get("status", "error")
                    image_path = step.get("image_path", "")

                    if status == "success":
                        cam_text = f"CAMERA_SUCCESS:{reason}"
                        cam_images = [image_path] if image_path else []
                    else:
                        result_text = step.get("result", "")
                        cam_text = f"CAMERA_FAILED:{result_text}"
                        cam_images = []

                    if step.get("pre_displayed"):
                        app_state.append_chat_message("CAMERA_CAPTURE", cam_text, is_ai=False, images=cam_images)
                        continue
                    app_state.append_chat_message("CAMERA_CAPTURE", cam_text, is_ai=False, images=cam_images)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="camera_capture_step")
                elif step.get("type") == "deep_search":
                    tool_name = step.get("tool", "")
                    status = step.get("status", "error")
                    status_icon = {"success": "🔍", "rate_limited": "⏳"}.get(status, "❌")
                    if tool_name == "search_web":
                        label = t('gen.web_search') if status == "success" else t('gen.web_search_failed')
                        detail = html_mod.escape(step.get("query", ""))
                    else:
                        label = t('gen.page_read') if status == "success" else t('gen.page_read_failed')
                        detail = html_mod.escape(step.get("url", ""))
                    ds_html = f'<div class="ds-header">{status_icon} {label}</div>'
                    if detail:
                        ds_html += f'<div class="ds-detail">{detail}</div>'
                    # Add result details (hit URLs for search, page title for read)
                    hit_urls = step.get("hit_urls", [])
                    if hit_urls:
                        url_items = ''.join(f'<div class="ds-url">{html_mod.escape(u)}</div>' for u in hit_urls)
                        ds_html += f'<details class="ds-results"><summary>{t("gen.results", count=len(hit_urls))}</summary>{url_items}</details>'
                    page_title = step.get("page_title", "")
                    if page_title:
                        ds_html += f'<div class="ds-detail">{html_mod.escape(page_title)}</div>'
                    if step.get("pre_displayed"):
                        app_state.append_chat_message("DEEP_SEARCH", ds_html, is_ai=False)
                        continue
                    app_state.append_chat_message("DEEP_SEARCH", ds_html, is_ai=False)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="deep_search_step")
                elif step.get("type") == "map_search":
                    tool_name = step.get("tool", "")
                    status = step.get("status", "error")
                    ok = status == "success"
                    icon = "🗺" if ok else ("⏳" if status == "rate_limited" else "❌")
                    if tool_name == "search_places":
                        label = t('gen.place_search') if ok else t('gen.place_search_failed')
                        detail = html_mod.escape(step.get("query", ""))
                    elif tool_name == "get_place_details":
                        label = t('gen.place_details') if ok else t('gen.place_details_failed')
                        detail = ""
                    elif tool_name == "get_directions":
                        label = t('gen.directions') if ok else t('gen.directions_failed')
                        mode = step.get("mode", "")
                        mode_icon = {"walking": "🚶", "driving": "🚗", "transit": "🚃"}.get(mode, "")
                        label = f"{label} {mode_icon}"
                        detail = ""
                    else:
                        label = t('gen.map_search')
                        detail = ""
                    ms_html = f'<div class="ds-header">{icon} {label}</div>'
                    if detail:
                        ms_html += f'<div class="ds-detail">{detail}</div>'
                    if step.get("pre_displayed"):
                        app_state.append_chat_message("MAP_SEARCH", ms_html, is_ai=False)
                        continue
                    app_state.append_chat_message("MAP_SEARCH", ms_html, is_ai=False)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="map_search_step")
                elif step.get("type") == "elyth":
                    tool_name = step.get("tool", "")
                    status = step.get("status", "error")
                    icon = "📡" if status == "success" else "❌"
                    tool_labels = {
                        "create_post": t('gen.elyth_post'),
                        "create_reply": t('gen.elyth_reply'),
                        "like_post": t('gen.elyth_like'),
                        "follow_vtuber": t('gen.elyth_follow'),
                    }
                    label = tool_labels.get(tool_name, f"ELYTH {tool_name}")
                    if status != "success":
                        label += t('gen.failed_suffix')
                    el_html = f'<div class="ds-header">{icon} {label}</div>'
                    content = step.get("content", "")
                    if content:
                        el_html += f'<div class="ds-detail">{html_mod.escape(content[:200])}</div>'
                    if step.get("pre_displayed"):
                        app_state.append_chat_message("ELYTH", el_html, is_ai=False)
                        continue
                    app_state.append_chat_message("ELYTH", el_html, is_ai=False)
                    app_state._chat_history_version += 1
                    notify_ui_update(reason="elyth_step")
            del app_state._pending_command_steps
            # Turn management (done once after all steps)
            end_turn()
            start_auto_prompt_timer()
        else:
            # Normal flow (no command steps)
            app_state.append_chat_message(char_name, ai_reply, is_ai=True)

            # Play TTS response if audio output is available
            if not speechless:
                try:
                    play_tts_response(ai_reply)
                except ModuleNotFoundError as e:
                    app_state.add_log_message("error", f"TTS module missing dependency: {e}")
                    show_warning_popup(t('tts.error_title'), t('tts.missing_dep', error=e))
                except ImportError as e:
                    app_state.add_log_message("error", f"TTS import error: {e}")
                    show_warning_popup(t('tts.error_title'), t('tts.import_error', error=e))
                except Exception as tts_error:
                    handle_tts_error(tts_error)
            else:
                end_turn()
                start_auto_prompt_timer()

        # Clear pending steps if somehow still set
        if hasattr(app_state, '_pending_command_steps'):
            del app_state._pending_command_steps

        app_state.response_generating = False
        return ai_reply

    except Exception as e:
        app_state.add_log_message("error", f"Unexpected error in generate_reply: {e}")
        error_details = traceback.format_exc()
        # トレース全文は debug(WSトーストに断片が出るのを防ぐ)
        logger.debug(f"Unexpected error details: {error_details}")
        app_state.response_generating = False
        # Clear request ID on error
        if app_state.current_request_id == request_id:
            app_state.current_request_id = None
        return user_error('err.reply_failed_logs')
    finally:
        # Always clear the request ID if it's still ours
        if app_state.current_request_id == request_id:
            app_state.current_request_id = None
        # Always discard pending command steps: the superseded/error early
        # returns above skip the normal cleanup, so steps set during this turn
        # would otherwise be replayed on the next turn (L9).
        if hasattr(app_state, '_pending_command_steps'):
            del app_state._pending_command_steps
