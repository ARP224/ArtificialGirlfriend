"""
ui/conversation/history.py

会話履歴の非同期ロード責務。DB からの履歴読込・チャットキャッシュ再構築・
ロード完了待ちを持つ。
"""

import threading

from ..state import app_state
from ..constants import MAX_CHAT_HISTORY_SIZE
from backend.shared.i18n import t

import backend

from .notify import notify_ui_update


def is_auto_prompt_user_message(msg: dict, auto_prompt_texts) -> bool:
    """AutoPromptの固定メッセージ(userロールで保存)かどうかを判定する。

    会話ページの履歴ロードから隠すための判定(ライブ表示は元々固定文を出さない
    =リロード後も同じ見え方に揃える。Historyページは全量表示のまま=稜裁定
    2026-08-21)。正は保存時の metadata 印。内容一致は印が付く前に保存された
    過去行の救済で、固定文が設定で編集されると旧文面の行は一致しなくなる
    (直近20件の窓から自然に流れるので許容)。空文字は不一致扱い(固定文が
    空に編集されていた場合に通常の空メッセージを誤って消さない)。

    Args:
        msg: backend.get_conversation_history の1メッセージdict
        auto_prompt_texts: 現在設定中の固定文(strip済み・空除去済み)の集合
    """
    if msg.get("role") != "user":
        return False
    if msg.get("metadata", {}).get("is_auto_prompt"):
        return True
    content = str(msg.get("content", "")).strip()
    return bool(content) and content in auto_prompt_texts


def current_auto_prompt_texts() -> set:
    """現在設定中のAutoPrompt固定文(ja/en)をstrip・空除去して返す。"""
    return {
        (app_state.auto_prompt_ja or "").strip(),
        (app_state.auto_prompt_en or "").strip(),
    } - {""}


def load_conversation_history(character_id: str, limit: int = 20) -> None:
    """
    Load conversation history for a character and add to chat display.
    Directly populates app_state.chat_history with the loaded messages.
    
    Args:
        character_id: The character ID to load history for
        limit: Maximum number of messages to load (default 20)
    """
    def _load_history_thread():
        """Background thread to load history without blocking UI"""
        try:
            # Set loading state
            app_state.is_loading_history = True
            app_state.history_load_error = None
            app_state.add_log_message("debug", f"Loading conversation history for {character_id}")
            
            # Call backend to get history
            result = backend.get_conversation_history(character_id, limit=limit)

            # success=True with an empty history is a valid "no messages" state
            # (e.g. right after End Conversation), NOT an error — treat it as a
            # clean clear instead of falling through to the error branch below.
            if result.get("success", False) and not result.get("history"):
                app_state.chat_history.clear()
                app_state.history_load_error = None
                app_state._chat_history_version += 1
                app_state.add_log_message("info", "No messages in conversation history")
            elif result.get("success", False) and result.get("history"):
                # Process messages into UI format
                loaded_messages = []
                _pending_location_flag = False  # Track location update → next user message
                _auto_prompt_texts = current_auto_prompt_texts()
                for msg in result["history"]:
                    # AutoPromptの固定文はライブ表示と同様に隠す(稜裁定 2026-08-21)
                    if is_auto_prompt_user_message(msg, _auto_prompt_texts):
                        continue

                    # Convert role to speaker name
                    role = msg.get("role", "user")

                    # Convert tool_record messages to FC display
                    if msg.get("metadata", {}).get("is_tool_message"):
                        import html as _html_mod
                        import re as _re
                        content = msg.get("content", "")
                        timestamp = msg.get("timestamp", None)

                        # --- Command ---
                        # Format: [Command: {cmd}] Reason: {reason} | Status: {status} | Result: {result}
                        m = _re.match(
                            r'\[Command:\s*(.+?)\]\s*Reason:\s*(.*?)\s*\|\s*Status:\s*(\w+)\s*\|\s*Result:\s*(.*)',
                            content, _re.DOTALL
                        )
                        if m:
                            cmd, reason, status, result_text = m.group(1), m.group(2), m.group(3), m.group(4).strip()
                            # 整形表は command_format に一本化(ライブ経路 generation.py と共有)。
                            # 旧実装は "interrupted" を欠き、リロード後に "❓" になっていた。
                            from .command_format import render_command_step_html
                            cmd_html = render_command_step_html(status, cmd, reason, result_text)
                            loaded_messages.append(("COMMAND", cmd_html, False, timestamp, [], []))
                            continue

                        # --- Camera Capture ---
                        # Format: [Camera: {status}] Reason: {reason}
                        m = _re.match(
                            r'\[Camera:\s*(\w+)\]\s*Reason:\s*(.*)',
                            content, _re.DOTALL
                        )
                        if m:
                            status, reason = m.group(1), m.group(2).strip()
                            images = msg.get("images", [])
                            if status == "success":
                                cam_text = f"CAMERA_SUCCESS:{reason}"
                            else:
                                cam_text = f"CAMERA_FAILED:{reason}"
                            loaded_messages.append(("CAMERA_CAPTURE", cam_text, False, timestamp, images, []))
                            continue

                        # --- Image Generation ---
                        # Format: [ImageGen: {status}] Prompt: {prompt}
                        m = _re.match(
                            r'\[ImageGen:\s*(\w+)\]\s*Prompt:\s*(.*)',
                            content, _re.DOTALL
                        )
                        if m:
                            status, prompt = m.group(1), m.group(2).strip()
                            images = msg.get("images", [])
                            if status == "success" and not images:
                                # 生成画像はメモリ上、同ターンの直前のAIテキスト
                                # メッセージに添付されている(conversation_manager
                                # の generated_image_paths=プロンプトのvision再投入
                                # 用の設計で、ImageGenレコード自身は images 空)。
                                # 表示の正位置は生成ブロック内=ライブ表示と同じ形
                                # になるよう、ここで表示用に移し替える。
                                from backend.shared.image_storage import is_generated_image
                                for i in range(len(loaded_messages) - 1, -1, -1):
                                    p_speaker, p_content, p_is_ai, p_ts, p_imgs, p_docs = loaded_messages[i]
                                    if p_speaker == "User":
                                        break  # 前のターンまでは遡らない
                                    gen_imgs = [p for p in (p_imgs or [])
                                                if is_generated_image(str(p))]
                                    if gen_imgs:
                                        images = gen_imgs
                                        loaded_messages[i] = (
                                            p_speaker, p_content, p_is_ai, p_ts,
                                            [p for p in p_imgs if p not in gen_imgs],
                                            p_docs)
                                        break
                            if status == "success":
                                img_text = f"IMAGE_GEN_SUCCESS:{prompt}"
                            else:
                                img_text = f"IMAGE_GEN_FAILED:{prompt}"
                            loaded_messages.append(("IMAGE_GEN", img_text, False, timestamp, images, []))
                            continue

                        # --- Deep Search ---
                        # Format: [DeepSearch: search_web] Query: {query}
                        # Format: [DeepSearch: read_webpage] URL: {url}
                        m = _re.match(
                            r'\[DeepSearch:\s*(\w+)\]\s*(?:Query|URL):\s*(.*)',
                            content, _re.DOTALL
                        )
                        if m:
                            tool_name, detail = m.group(1), m.group(2).strip()
                            status_icon = "🔍"
                            if tool_name == "search_web":
                                label = t('gen.web_search')
                                ds_html = f'<div class="ds-header">{status_icon} {label}</div>'
                                if detail:
                                    ds_html += f'<div class="ds-detail">{_html_mod.escape(detail)}</div>'
                            else:
                                label = t('gen.page_read')
                                ds_html = f'<div class="ds-header">{status_icon} {label}</div>'
                                if detail:
                                    ds_html += f'<div class="ds-detail">{_html_mod.escape(detail)}</div>'
                            loaded_messages.append(("DEEP_SEARCH", ds_html, False, timestamp, [], []))
                            continue

                        # Location update message → flag for next user message indicator
                        # (content match covers legacy records saved before tool_type existed)
                        if (msg.get("metadata", {}).get("tool_type") == "location_update"
                                or "位置情報更新" in content):
                            _pending_location_flag = True
                            continue

                        # --- Map Search ---
                        # Format: [MapSearch: search_places] Query: {query}\n{results}
                        # Format: [MapSearch: get_place_details] Place: {place_id}\n{results}
                        # Format: [MapSearch: get_directions] Place: {place_id} Mode: {mode}\n{results}
                        m = _re.match(
                            r'\[MapSearch:\s*(\w+)\]',
                            content
                        )
                        if m:
                            ms_tool = m.group(1)
                            ms_icon = "🗺"
                            if ms_tool == "search_places":
                                ms_label = t('gen.place_search')
                                ms_detail_match = _re.search(r'Query:\s*(.+?)(?:\n|$)', content)
                                ms_detail = _html_mod.escape(ms_detail_match.group(1).strip()) if ms_detail_match else ""
                            elif ms_tool == "get_place_details":
                                ms_label = t('gen.place_details')
                                ms_detail = ""
                            elif ms_tool == "get_directions":
                                ms_label = t('gen.directions')
                                mode_match = _re.search(r'Mode:\s*(\w+)', content)
                                mode_val = mode_match.group(1) if mode_match else ""
                                mode_icon = {"walking": "🚶", "driving": "🚗", "transit": "🚃"}.get(mode_val, "")
                                ms_label = f"{t('gen.directions')} {mode_icon}"
                                ms_detail = ""
                            else:
                                ms_label = t('gen.map_search')
                                ms_detail = ""
                            ms_html = f'<div class="ds-header">{ms_icon} {ms_label}</div>'
                            if ms_detail:
                                ms_html += f'<div class="ds-detail">{ms_detail}</div>'
                            loaded_messages.append(("MAP_SEARCH", ms_html, False, timestamp, [], []))
                            continue

                        # --- ELYTH ---
                        # Format: [ELYTH: {tool}] Status: {status} | Content: {content}
                        m = _re.match(
                            r'\[ELYTH:\s*(\w+)\]\s*Status:\s*(\w+)\s*\|\s*Content:\s*(.*)',
                            content, _re.DOTALL
                        )
                        if m:
                            el_tool, el_status, el_content = m.group(1), m.group(2), m.group(3).strip()
                            el_icon = "📡" if el_status == "success" else "❌"
                            tool_labels = {
                                "create_post": t('gen.elyth_post'),
                                "create_reply": t('gen.elyth_reply'),
                                "like_post": t('gen.elyth_like'),
                                "follow_vtuber": t('gen.elyth_follow'),
                            }
                            el_label = tool_labels.get(el_tool, f"ELYTH {el_tool}")
                            if el_status != "success":
                                el_label += t('gen.failed_suffix')
                            el_html = f'<div class="ds-header">{el_icon} {el_label}</div>'
                            if el_content:
                                el_html += f'<div class="ds-detail">{_html_mod.escape(el_content[:200])}</div>'
                            loaded_messages.append(("ELYTH", el_html, False, timestamp, [], []))
                            continue

                        # --- Talk Theme (AI tool) ---
                        # Format: [TalkTheme: set] Theme: {theme}
                        # Format: [TalkTheme: clear]
                        m = _re.match(
                            r'\[TalkTheme:\s*(set|clear)\](?:\s*Theme:\s*(.*))?',
                            content, _re.DOTALL
                        )
                        if m:
                            # 整形は theme_format に一本化(ライブ経路 generation.py と共有)
                            from .theme_format import render_talk_theme_html
                            tt_action, tt_theme = m.group(1), (m.group(2) or "").strip()
                            tt_html = render_talk_theme_html(tt_action, tt_theme)
                            loaded_messages.append(("TALK_THEME", tt_html, False, timestamp, [], []))
                            continue

                        # Other tool messages (Note) - skip silently
                        # (Noteはライブ表示も存在しない=リロードで出さないのが対称)
                        continue

                    # --- User theme change (theme_feedback) → same card as live path ---
                    # character_manager.update_talk_theme が生成する固定2書式(日/英)のみ
                    # 変換。パース不能(旧版のキャラ名主語形式等)は従来どおり下の
                    # role=="system" 分岐へ落として灰色SYSTEM行で表示する。
                    if msg.get("metadata", {}).get("type") == "theme_feedback":
                        import re as _tf_re
                        from .theme_format import render_talk_theme_html
                        tf_content = msg.get("content", "")
                        tf_timestamp = msg.get("timestamp", None)
                        tf_m = (
                            _tf_re.match(r'^（ユーザーが、トークテーマとして「(.*)」を設定しました。）$',
                                         tf_content, _tf_re.DOTALL)
                            or _tf_re.match(r'^\(User has set the talk theme to "(.*)"\.\)$',
                                            tf_content, _tf_re.DOTALL)
                        )
                        if tf_m:
                            tf_html = render_talk_theme_html("set", tf_m.group(1), by_user=True)
                            loaded_messages.append(("TALK_THEME_USER", tf_html, False, tf_timestamp, [], []))
                            continue
                        if tf_content in ("（ユーザーが、トークテーマをクリアしました。）",
                                          "(User has cleared the talk theme.)"):
                            tf_html = render_talk_theme_html("clear", by_user=True)
                            loaded_messages.append(("TALK_THEME_USER", tf_html, False, tf_timestamp, [], []))
                            continue

                    if role == "system":
                        # System messages (like feedback) - use special marker
                        speaker = "SYSTEM"
                        is_ai = False  # Not AI, not user - will be handled specially
                    elif role == "assistant":
                        # Try to get character name from cached config if available
                        speaker = "AI"  # Default
                        if hasattr(app_state, '_character_config_cache') and character_id in app_state._character_config_cache:
                            speaker = app_state._character_config_cache[character_id].get("name", "AI")
                        else:
                            try:
                                # load_character_config は @standardize_response 済み
                                # ({success, result})。result を剥がさず config.get("name")
                                # を直読みすると常に None→speaker="AI" 固定+キャッシュ永久空。
                                config_response = backend.load_character_config(character_id)
                                if isinstance(config_response, dict) and 'result' in config_response:
                                    config = config_response.get('result', {})
                                else:
                                    config = config_response
                                if config and config.get("name"):
                                    speaker = config["name"]
                                    # Cache for future use
                                    if not hasattr(app_state, '_character_config_cache'):
                                        app_state._character_config_cache = {}
                                    app_state._character_config_cache[character_id] = config
                            except Exception:
                                pass
                        is_ai = True
                    else:
                        speaker = "User"
                        is_ai = False

                    # Extract content, timestamp, images, and document filenames
                    content = msg.get("content", "")
                    # Backend provides ISO format timestamps
                    timestamp = msg.get("timestamp", None)
                    images = msg.get("images", [])
                    # Extract document filenames for UI display
                    doc_filenames = [
                        doc.get("filename", "") for doc in msg.get("documents", [])
                        if doc.get("filename")
                    ]

                    # Apply pending location flag to user messages
                    if not is_ai and _pending_location_flag:
                        doc_filenames.append("__location_sent__")
                        _pending_location_flag = False

                    if content:  # Only add non-empty messages
                        loaded_messages.append((speaker, content, is_ai, timestamp, images, doc_filenames))

                # Add loaded messages to chat history
                if loaded_messages:
                    # Clear any existing messages first
                    app_state.chat_history.clear()
                    # Add all loaded messages using append_chat_message to ensure proper trimming
                    for speaker, content, is_ai, timestamp, images, documents in loaded_messages:
                        # Temporarily add to chat_history directly to preserve original timestamp
                        app_state.chat_history.append((speaker, content, is_ai, timestamp, images, documents))
                    
                    # Apply trimming if needed
                    if len(app_state.chat_history) > MAX_CHAT_HISTORY_SIZE:
                        app_state.chat_history = app_state.chat_history[-MAX_CHAT_HISTORY_SIZE:]
                        app_state.add_log_message("info", f"Loaded history trimmed to {MAX_CHAT_HISTORY_SIZE} messages")
                    else:
                        app_state.add_log_message("info", f"Loaded {len(loaded_messages)} messages into chat history")
                    
                    # Force UI update
                    app_state._chat_history_version += 1
                else:
                    # New character has no history — clear the old one
                    app_state.chat_history.clear()
                    app_state._chat_history_version += 1
                    app_state.add_log_message("info", "No messages found in conversation history")

            elif result.get("warnings"):
                # Handle warnings (e.g., no history found) — still clear old history
                app_state.chat_history.clear()
                app_state._chat_history_version += 1
                app_state.add_log_message("info", f"No conversation history found for {character_id}")
            else:
                # Handle errors — still clear to avoid showing wrong character's history
                app_state.chat_history.clear()
                app_state._chat_history_version += 1
                error_msg = result.get("error", "Unknown error loading history")
                app_state.history_load_error = error_msg
                app_state.add_log_message("warning", f"Failed to load history: {error_msg}")
                
        except Exception as e:
            app_state.history_load_error = str(e)
            app_state.add_log_message("error", f"Error loading conversation history: {e}")
        finally:
            # Clear loading state
            app_state.is_loading_history = False
            # Force final UI update
            app_state._chat_history_version += 1
            # Notify all WS clients (companion needs this to refresh from "Loading..." state)
            notify_ui_update(reason="history_load_complete")
    
    # Start loading in background thread
    history_thread = threading.Thread(target=_load_history_thread, daemon=True)
    history_thread.start()
    
    # Don't wait for completion - let UI remain responsive
    app_state.add_log_message("debug", "Started background history loading")


def wait_for_history_load() -> None:
    """
    Wait for conversation history to finish loading.
    This is used to update UI after history load completes.
    
    Returns:
        None
    """
    import time
    max_wait = 5  # Maximum 5 seconds wait
    start_time = time.time()
    
    # Check if loading is in progress
    while app_state.is_loading_history:
        if time.time() - start_time > max_wait:
            app_state.add_log_message("warning", "History loading timeout - forcing completion")
            app_state.is_loading_history = False
            break
        time.sleep(0.1)  # Check every 100ms
    
    # Force a version update to refresh UI
    app_state._chat_history_version += 1
