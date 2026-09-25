"""
pages.py

Page definitions and helper functions for multi-page UI layout.
"""

import gradio as gr
from typing import Dict, Any
from backend.shared.i18n import available_languages, t
from backend.shared.settings_store import get_setting
from .status_checker import get_status_html
from .status_js import status_auto_hide_js


class Pages:
    """Constants for page identifiers."""
    CONVERSATION = "conversation"
    CHARACTER_SETTINGS = "character_settings"
    CONVERSATION_HISTORY = "conversation_history"
    SYSTEM_LOGS = "system_logs"
    SYSTEM_CONTROLS = "system_controls"


# Hide the Gradio footer: the built-with link, the API link, and the ⚙
# settings panel (whose theme/language controls translate only Gradio's own
# chrome — none of it applies to this app). Selectors follow the precedent in
# mobile_app.py, scoped under footer so chat-message links stay unaffected.
FOOTER_HIDE_CSS = """
    footer,
    .gradio-container footer,
    footer .built-with,
    footer .settings-toggle,
    footer button.settings,
    footer a[href*="api"],
    footer .api-link,
    footer .show-api { display: none !important; }
    """


def get_sidebar_css() -> str:
    """Get CSS styling for sidebar navigation."""
    return """
    /* Sidebar styling — uses Gradio CSS vars to support dark mode.
       background: transparent so the sidebar inherits the page bg
       (Gradio dark mode's --background-fill-secondary has a bluish tint
       that visually clashes with the surrounding page bg in our layout). */
    #sidebar {
        background-color: transparent;
        min-height: 100vh;
        padding: 20px 10px;
        transition: all 0.3s ease;
    }

    #sidebar.collapsed {
        width: 60px;
        padding: 20px 5px;
    }

    /* Navigation button styling */
    .nav-btn {
        width: 100%;
        text-align: left;
        padding: 12px 16px;
        margin: 4px 0;
        border: none;
        border-radius: 8px;
        background: transparent;
        font-size: 14px;
        cursor: pointer;
        transition: all 0.2s ease;
        color: var(--body-text-color);
    }

    .nav-btn:hover {
        background-color: var(--background-fill-primary);
        transform: translateX(2px);
    }

    .nav-btn.active {
        background-color: #007bff;
        color: white;
        font-weight: 500;
        box-shadow: 0 2px 4px rgba(0,123,255,0.2);
    }

    .nav-btn.active:hover {
        background-color: #0056b3;
    }

    /* Sidebar toggle button */
    #sidebar-toggle {
        position: absolute;
        top: 10px;
        right: -15px;
        width: 30px;
        height: 30px;
        border-radius: 50%;
        background-color: var(--background-fill-primary);
        border: 1px solid var(--border-color-primary);
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all 0.2s ease;
        z-index: 1000;
    }

    #sidebar-toggle:hover {
        background-color: var(--background-fill-secondary);
        transform: scale(1.1);
    }
    
    /* Talk Theme Panel Styling */
    #talk-theme-panel {
        margin-top: 20px;
        padding: 15px;
        background-color: var(--background-fill-secondary);
        border-radius: 8px;
    }

    .theme-panel-title {
        font-size: 14px;
        font-weight: 600;
        color: #b0b0b0;
        margin-bottom: 10px;
    }

    #current-theme-display {
        background-color: #232323;
        border: 1px solid #3a3a3a;
        color: #e0e0e0;
        font-size: 13px;
    }

    #new-theme-input {
        background-color: #232323;
        border: 1px solid #3a3a3a;
        color: #e0e0e0;
        font-size: 13px;
    }

    .theme-btn {
        font-size: 12px;
        padding: 6px 12px;
        margin: 2px;
        background-color: #6c757d !important;
        color: white !important;
        border: none !important;
    }

    .theme-btn:hover {
        background-color: #5a6268 !important;
    }

    #theme-error-display {
        font-size: 12px;
        margin-top: 5px;
        padding: 5px;
        background-color: #2e1a1a;
        border-radius: 4px;
    }

    /* Main content area */
    #main-content {
        padding: 20px;
        width: 100%;
    }

    /* Page title styling */
    .page-title {
        font-size: 24px;
        font-weight: 600;
        color: #e0e0e0;
        margin-bottom: 20px;
        padding-bottom: 10px;
        border-bottom: 2px solid #3a3a3a;
    }
    
    /* Tighten the gap between page title and first content.
       Conversation page (#page-conversation) is intentionally excluded. */
    #page-characters, #page-history, #page-logs, #page-system {
        gap: 4px !important;
    }
    #page-characters .page-title,
    #page-history .page-title,
    #page-logs .page-title,
    #page-system .page-title {
        margin-bottom: 4px !important;
    }

    /* Page content styling */
    #main-content > div {
        width: 100%;
        height: 100%;
    }
    
    /* Server Mode button: force blue (override default orange primary) */
    #switch-to-server-btn {
        background: #007bff !important;
        border-color: #007bff !important;
        color: white !important;
    }
    #switch-to-server-btn:hover {
        background: #0056b3 !important;
        border-color: #0056b3 !important;
    }

    /* Responsive design */
    @media (max-width: 768px) {
        #sidebar {
            position: fixed;
            left: -250px;
            width: 250px;
            z-index: 1000;
            box-shadow: 2px 0 5px rgba(0,0,0,0.1);
        }
        
        #sidebar.open {
            left: 0;
        }
        
        #sidebar-toggle {
            position: fixed;
            left: 10px;
            top: 10px;
        }
        
        #main-content {
            margin-left: 0;
        }
    }
    """


def create_conversation_page(app_state: Any) -> Dict[str, Any]:
    """Create the main conversation page with new layout structure.

    Args:
        app_state: Application state object

    Returns:
        Dictionary of all conversation page components
    """
    gr.Markdown(f"## {t('conv.title')}", elem_classes="page-title")
    
    components = {}
    
    # TOP SECTION: Character info and status (consolidated single row)
    with gr.Group() as top_section:
        # Title removed for more compact layout
        with gr.Row():
            # Character dropdown (balanced width)
            with gr.Column(scale=2, min_width=180):
                components['character_dropdown'] = gr.Dropdown(
                    label=t('conv.char_label'),
                    choices=[],
                    value=None,
                    interactive=True
                )
            
            # Character icon (larger presence)
            with gr.Column(scale=2, min_width=100):
                components['char_icon'] = gr.Image(
                    label="",
                    value=None,
                    height=90,
                    width=90,
                    interactive=False,
                    show_label=False
                )
            
            # Character info (slightly wider)
            with gr.Column(scale=3, min_width=200):
                components['char_name'] = gr.Markdown(t('conv.char_not_selected'))
                components['char_description'] = gr.Markdown(t('conv.char_select_hint'))
                components['model_info'] = gr.Markdown(t('charui.model_info_na'))
            
            # Connection status indicators (combined into single HTML for reduced flicker)
            with gr.Column(scale=2, min_width=180):
                components['status_display'] = gr.HTML(
                    value=get_status_html(),
                    elem_id="status-display"
                )
    
    # MIDDLE SECTION: Chat history
    with gr.Group() as middle_section:
        gr.Markdown(f"### {t('conv.history_title')}")
        components['chat_display'] = gr.HTML(
            label="",
            value=f"<div>{t('conv.chat_placeholder')}</div>",
            elem_id="chat-display"
        )
    
    # BOTTOM SECTION: Input controls (Voice/Text tabs)
    with gr.Group() as bottom_section:
        gr.Markdown(f"### {t('conv.io_title')}")
        
        # Tab container for Voice and Text input
        with gr.Tabs() as input_tabs:
            # Voice Input Tab
            with gr.Tab(t('conv.tab.voice_input')):
                # Main voice control row
                with gr.Row():
                    # Left side: Voice recording button and status
                    with gr.Column(scale=3):
                # Single unified recording button
                # 会話開始前は押せないボタンに見せない: voice-btn-ready(青CSS)を
                # 付けず interactive=False で灰色化。開始時に toggle_start_end が
                # クラス+interactive を付け直す(リロード時は load 同期が復元)。
                        components['voice_btn'] = gr.Button(
                    t('voicebtn.start_first'),
                    variant="secondary",
                    size="lg",
                    elem_id="voice-record-btn",
                    interactive=False
                )
                
                # Microphone status with animation indicator. Call-time
                # import: the HTML single source lives with the other mic
                # writers in ui/conversation/recording.py.
                        from .conversation.recording import initial_mic_status_html
                        components['mic_status'] = gr.HTML(
                    initial_mic_status_html(),
                    elem_id="mic-status-display"
                )
                
                # Recording time indicator (hidden by default)
                        components['recording_time'] = gr.HTML(
                    f"""<div class="recording-time" style="display: none;">
                        <span class="time-text">{t('rec.time', time='0:00')}</span>
                    </div>""",
                    elem_id="recording-time-display"
                )
                    
                    # Right side: Settings and controls
                    with gr.Column(scale=2):
                        # Audio settings accordion
                        with gr.Accordion(t('conv.audio_settings'), open=False) as audio_settings:
                            # Mic device selection. スタンドアロン=サーバー側
                            # sounddevice 録音にのみ効く=ローカルDD+🔄を表示。
                            # サーバーモード中の録音はブラウザマイク
                            # (MediaRecorder経路)のため、ローカルDDは出さず
                            # (無関係な設定の表示は誤解の元=稜指摘 2026-07-25)、
                            # JS 専有 div のブラウザ実デバイス名+🔄(掴み直し)に
                            # 置き換える。両セットとも常にビルドして visible で
                            # 切替(配線は app.py が無条件参照するため)。
                            from .handlers.audio_device import (
                                build_mic_device_choices, browser_mic_name_html)
                            _mic_choices, _mic_value = build_mic_device_choices()
                            _server_mode = bool(app_state.server_mode_enabled)
                            with gr.Row(visible=not _server_mode):
                                components['mic_device_dd'] = gr.Dropdown(
                            choices=_mic_choices,
                            value=_mic_value,
                            label=t('conv.mic_device'),
                            info=t('conv.mic_device_info'),
                            interactive=True,
                            scale=4,
                            elem_id="mic-device-dd"
                        )
                                components['mic_device_refresh_btn'] = gr.Button(
                            t('conv.mic_device_refresh'),
                            variant="secondary",
                            scale=1,
                            elem_id="mic-device-refresh-btn"
                        )
                            components['browser_mic_info'] = gr.Markdown(
                        t('conv.mic_device_info_server'),
                        visible=_server_mode,
                        elem_id="browser-mic-info"
                    )
                            with gr.Row(visible=_server_mode):
                                components['browser_mic_name'] = gr.HTML(
                            browser_mic_name_html(),
                            elem_id="browser-mic-name-wrap"
                        )
                                components['browser_mic_refresh_btn'] = gr.Button(
                            t('conv.mic_device_refresh'),
                            variant="secondary",
                            scale=0,
                            elem_id="browser-mic-refresh-btn"
                        )

                            gr.Markdown("---")  # Separator

                            # Beep sound toggle
                            components['beep_toggle'] = gr.Checkbox(
                        value=app_state.beep_enabled,
                        label=t('conv.beep_toggle'),
                        elem_id="beep-toggle"
                    )
                    
                            # Beep volume control
                            with gr.Row():
                                components['beep_volume_slider'] = gr.Slider(
                            minimum=0,
                            maximum=100,
                            value=app_state.beep_volume * 100,
                            step=5,
                            label=t('conv.beep_volume'),
                            info=t('conv.beep_volume_info'),
                            interactive=True,
                            elem_id="beep-volume-slider"
                        )
                                components['volume_label'] = gr.Markdown(
                            f"**{app_state.beep_volume * 100:.0f}%**",
                            elem_id="volume-label"
                        )
                    
                            # Test beep button
                            components['test_beep_btn'] = gr.Button(
                        t('conv.test_beeps'),
                        variant="secondary",
                        elem_id="test-beep-button"
                    )
                    
                            # Test result display
                            components['test_result'] = gr.Markdown(
                        "",
                        elem_id="test-result",
                        visible=False
                    )
                    
                            gr.Markdown("---")  # Separator

                            # STT engine selection (local Faster-whisper vs
                            # OpenAI transcription API) + API model choice.
                            _stt_engine_value = get_setting('audio', 'stt_engine', 'faster_whisper')
                            from backend.shared.api_settings import get_openai_stt_model_choices
                            from audio_input import VALID_MODEL_SIZES
                            _stt_model_choices = get_openai_stt_model_choices()
                            _stt_model_value = get_setting('audio', 'stt_api_model', 'whisper-1')
                            if _stt_model_value and _stt_model_value not in _stt_model_choices:
                                _stt_model_choices.append(_stt_model_value)
                            _stt_local_model_value = get_setting('audio', 'stt_local_model', 'turbo')
                            if _stt_local_model_value not in VALID_MODEL_SIZES:
                                _stt_local_model_value = 'turbo'
                            components['stt_engine_radio'] = gr.Radio(
                        choices=[
                            (t('conv.stt_engine_local'), "faster_whisper"),
                            (t('conv.stt_engine_api'), "openai"),
                        ],
                        value=_stt_engine_value,
                        label=t('conv.stt_engine'),
                        elem_id="stt-engine-radio"
                    )
                            components['stt_local_model_dd'] = gr.Dropdown(
                        choices=list(VALID_MODEL_SIZES),
                        value=_stt_local_model_value,
                        label=t('conv.stt_local_model'),
                        info=t('conv.stt_local_model_info'),
                        visible=(_stt_engine_value == "faster_whisper"),
                        interactive=True,
                        elem_id="stt-local-model-dd"
                    )
                            components['stt_api_model_dd'] = gr.Dropdown(
                        choices=_stt_model_choices,
                        value=_stt_model_value,
                        label=t('conv.stt_api_model'),
                        info=t('conv.stt_api_model_info'),
                        visible=(_stt_engine_value == "openai"),
                        interactive=True,
                        elem_id="stt-api-model-dd"
                    )
                            # STT ステータス行は JS 専有(書き手は WS の stt_model_status
                            # のみ・Gradio からは一切更新しない)。gr.HTML の素の div なので
                            # svelte が中身を再描画せず、JS の DOM 更新と競合しない。
                            # 常時 visible: 空 div は高さ0=見えない(visible=False の解除を
                            # JS からやるのは Gradio の隠し実装依存で不安定・実機で実証済み)。
                            components['stt_engine_status'] = gr.HTML(
                        "<div id='stt-engine-status-text'></div>",
                        elem_id="stt-engine-status"
                    )

                            gr.Markdown("---")  # Separator

                            # Mic info line (STT language, from switch_character).
                            # Key は mic_status_info: 'mic_status' はボタン下の HTML
                            # インジケータ(上で定義)が使用中。同キーだと後勝ちで
                            # インジケータが凍結し録音HTMLがここに流入していた(M8)。
                            components['mic_status_info'] = gr.Markdown(
                        t('conv.mic_default_info'),
                        elem_id="mic-status-info"
                    )
                
                        components['audio_settings'] = audio_settings
                        
                        # Keyboard shortcuts info + editor. キーは設定駆動
                        # (settings_store 'hotkeys')・現在値はエディタ自身が
                        # 表示するため説明文にキー列挙は持たない。
                        # 修飾キーはチェックボックス化(キャプチャ欄はIME/
                        # 自ホットキー発火/OSショートカットの3層問題=Addon実踏)。
                        with gr.Accordion(t('conv.shortcuts_title'), open=False) as shortcuts_info:
                            # サーバーモード: グローバルホットキー(pynput=AG本体
                            # PCのキーボード)はクライアントには効かない=エディタは
                            # 出さず「Client Addonで使える」案内に入れ替え
                            # (稜裁定 2026-07-25)。エディタ部品は常にビルドして
                            # visible で切替(配線は app.py が無条件参照)。
                            # Mac 3-4: Input Monitoring / Spaces-conflict notes.
                            from backend.shared.platform_caps import IS_MAC
                            gr.Markdown(t('conv.shortcuts_body_server'
                                          if _server_mode
                                          else ('conv.shortcuts_body_mac' if IS_MAC
                                                else 'conv.shortcuts_body')))
                            # darwin も編集可(2026-07-25 M1実測でキーイベント
                            # 表現を確定・DARWIN_VK 導入によりロック解除)
                            _hk_editable = True
                            _hk_visible = not _server_mode
                            from .handlers.hotkey_config import (
                                current_hotkey_values, key_choices)
                            _hk_start, _hk_stop = current_hotkey_values()
                            _hk_choices = key_choices()
                            with gr.Row(visible=_hk_visible):
                                components['hotkey_start_ctrl'] = gr.Checkbox(
                            value=_hk_start['ctrl'], label="Ctrl", scale=1,
                            interactive=_hk_editable,
                            elem_id="hotkey-start-ctrl")
                                components['hotkey_start_alt'] = gr.Checkbox(
                            value=_hk_start['alt'], label="Alt", scale=1,
                            interactive=_hk_editable,
                            elem_id="hotkey-start-alt")
                                components['hotkey_start_shift'] = gr.Checkbox(
                            value=_hk_start['shift'], label="Shift", scale=1,
                            interactive=_hk_editable,
                            elem_id="hotkey-start-shift")
                                components['hotkey_start_key'] = gr.Dropdown(
                            choices=_hk_choices, value=_hk_start['key'],
                            label=t('conv.hotkey_start'), scale=2,
                            interactive=_hk_editable, elem_id="hotkey-start-key")
                            with gr.Row(visible=_hk_visible):
                                components['hotkey_stop_ctrl'] = gr.Checkbox(
                            value=_hk_stop['ctrl'], label="Ctrl", scale=1,
                            interactive=_hk_editable,
                            elem_id="hotkey-stop-ctrl")
                                components['hotkey_stop_alt'] = gr.Checkbox(
                            value=_hk_stop['alt'], label="Alt", scale=1,
                            interactive=_hk_editable,
                            elem_id="hotkey-stop-alt")
                                components['hotkey_stop_shift'] = gr.Checkbox(
                            value=_hk_stop['shift'], label="Shift", scale=1,
                            interactive=_hk_editable,
                            elem_id="hotkey-stop-shift")
                                components['hotkey_stop_key'] = gr.Dropdown(
                            choices=_hk_choices, value=_hk_stop['key'],
                            label=t('conv.hotkey_stop'), scale=2,
                            interactive=_hk_editable, elem_id="hotkey-stop-key")
                            components['hotkey_save_btn'] = gr.Button(
                        t('conv.hotkey_save'),
                        variant="primary",
                        interactive=_hk_editable,
                        visible=_hk_visible,
                        elem_id="hotkey-save-btn")
                            components['hotkey_save_result'] = gr.Markdown(
                        "", visible=_hk_visible, elem_id="hotkey-save-result")

                        components['shortcuts_info'] = shortcuts_info
            
            # Text Input Tab
            with gr.Tab(t('conv.tab.text_input')):
                with gr.Row():
                    with gr.Column(scale=5):
                # 会話開始前はテキスト入力系も無効(グレー)。有効化と
                # プレースホルダ切替は toggle_start_end 後の
                # sync_text_controls_lock が担う(ビルド時=開始前の文言)。
                        components['text_input'] = gr.Textbox(
                            label=t('conv.text_label'),
                            placeholder=t('conv.text_placeholder_locked'),
                            lines=3,
                            max_lines=10,
                            interactive=False,
                            elem_id="text-input-box"
                        )
                    with gr.Column(scale=1):
                        components['send_btn'] = gr.Button(
                            t('conv.send'),
                            variant="primary",
                            size="lg",
                            interactive=False,
                            elem_id="send-text-btn"
                        )
                        components['clear_btn'] = gr.Button(
                            t('conv.clear'),
                            variant="secondary",
                            interactive=False,
                            elem_id="clear-text-btn"
                        )
                        components['attach_btn'] = gr.UploadButton(
                            t('conv.attach'),
                            # 画像は"image"包括指定ではなく明示5拡張子: JS側の
                            # 判定リスト(IMG_EXTS)と揃え、.heic等が選択できて
                            # 無言消滅する穴を選択段階で塞ぐ(稜裁定 2026-08-15)。
                            # PDF/DOCXは動作検証未了のため除外(同裁定)。
                            file_types=[
                                ".png", ".jpg", ".jpeg", ".gif", ".webp",
                                ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
                                ".log", ".ini", ".toml",
                                ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp",
                                ".h", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".bat", ".ps1",
                            ],
                            file_count="multiple",
                            variant="secondary",
                            interactive=False,
                            elem_id="attach-btn"
                        )

                # Attachment states (accumulated file paths / document info)
                components['images_state'] = gr.State([])
                components['documents_state'] = gr.State([])

                # Attachment status (shows count of attached images/documents)
                components['attach_status'] = gr.Markdown(
                    "",
                    elem_id="attach-status"
                )

                # Text input status
                components['text_status'] = gr.Markdown(
                    t('conv.text_ready'),
                    elem_id="text-status"
                )
            
            # Voice Output Tab
            with gr.Tab(t('conv.tab.voice_output')):
                with gr.Row():
                    # TTS Volume Control Section
                    with gr.Column(scale=1):
                        gr.Markdown(f"### {t('conv.voice_output_title')}")
                        
                        # TTS volume control and test button in same row
                        # (高さ揃え 稜依頼 2026-07-25: ボタンは音量パネルに対して
                        # 縦中央 — CSSは app.py の voice_output_css)
                        with gr.Row(equal_height=True, elem_classes="tts-volume-row"):
                            # Volume slider and label column
                            with gr.Column(scale=3):
                                components['tts_volume_slider'] = gr.Slider(
                                    minimum=0,
                                    maximum=100,
                                    value=app_state.tts_volume * 100,
                                    step=5,
                                    label=t('conv.tts_volume'),
                                    info=t('conv.tts_volume_info'),
                                    interactive=True,
                                    elem_id="tts-volume-slider"
                                )
                                components['tts_volume_label'] = gr.Markdown(
                                    f"**{app_state.tts_volume * 100:.0f}%**",
                                    elem_id="tts-volume-label"
                                )
                            
                            # Test button column
                            with gr.Column(scale=1, elem_id="tts-test-col"):
                                components['test_tts_btn'] = gr.Button(
                                    t('conv.test_voice'),
                                    variant="secondary",
                                    elem_id="test-tts-button"
                                )
                                # Test result display below button
                                components['tts_test_result'] = gr.Markdown(
                                    "",
                                    elem_id="tts-test-result",
                                    visible=False
                                )
            
            # Auto Prompt Tab
            with gr.Tab(t('conv.tab.auto_prompt')):
                # equal_height + タイマーボックス化(稜依頼 2026-07-25: 3領域の
                # 上下高さ不揃い解消+カウント表示を囲い付きのタイマー風に)
                # min_width明示: 既定320×3列は狭幅ウィンドウで3列目が折返す
                # (ヘッドレスChrome 1100px実測)。合計を抑えて1行を維持する。
                with gr.Row(equal_height=True, elem_classes="auto-prompt-row"):
                    # Enable/Disable checkbox
                    with gr.Column(scale=1, min_width=240):
                        components['auto_prompt_enabled'] = gr.Checkbox(
                            label=t('conv.auto_enable'),
                            value=app_state.auto_prompt_enabled,
                            info=t('conv.auto_enable_info')
                        )

                    # Timer duration slider
                    with gr.Column(scale=2):
                        components['auto_timer_duration'] = gr.Slider(
                            minimum=30,
                            maximum=600,
                            value=app_state.auto_prompt_timer_duration,
                            step=10,
                            label=t('conv.auto_duration'),
                            info=t('conv.auto_duration_info')
                        )

                    # Countdown display - 初期値を動的に設定。値の書き手は
                    # WS+JS(#auto-prompt-countdown p)のみ=囲いは外側で装飾し、
                    # JSの更新セレクタには触らない。
                    with gr.Column(scale=1, min_width=220, elem_id="auto-timer-box"):
                        gr.Markdown(
                            t('conv.auto_timer_title'),
                            elem_id="auto-timer-box-title"
                        )
                        initial_countdown_text = (
                            t('js.auto.inactive')
                            if app_state.auto_prompt_enabled
                            else t('js.auto.disabled')
                        )
                        components['auto_countdown_display'] = gr.Markdown(
                            initial_countdown_text,
                            elem_id="auto-prompt-countdown"
                        )
                
                # プロンプト本文の編集UI(旧「プロンプト設定」アコーディオン)は
                # 撤去(稜裁定 2026-07-19=編集不要)。本文は settings の
                # auto_prompt.prompt_ja/prompt_en が真実源のまま生きている。

            # Font Tab
            with gr.Tab(t('conv.tab.font')):
                with gr.Row():
                    with gr.Column(scale=3):
                        components['chat_font_slider'] = gr.Slider(
                            minimum=10,
                            maximum=24,
                            value=app_state.chat_font_size,
                            step=1,
                            label=t('conv.font_size'),
                            info=t('conv.font_size_info')
                        )
                        components['font_size_label'] = gr.Markdown(
                            t('conv.font_current', size=app_state.chat_font_size),
                            elem_id="font-size-label"
                        )
                    with gr.Column(scale=1):
                        components['font_status'] = gr.Markdown(
                            t('conv.font_hint'),
                            elem_id="font-status"
                        )

            # Refresh History Tab - Reset short-term conversation history
            with gr.Tab(t('conv.tab.refresh_history')):
                gr.Markdown(f"### {t('conv.refresh_title')}")
                gr.Markdown(t('conv.refresh_desc'))
                with gr.Row():
                    components['refresh_history_btn'] = gr.Button(
                        t('conv.refresh_btn'),
                        variant="secondary",
                        interactive=False,
                        elem_id="refresh-history-btn"
                    )
                components['refresh_history_feedback'] = gr.Markdown(
                    "",
                    elem_id="refresh-history-feedback"
                )

            # Tuning Tab - LLM parameter adjustment
            with gr.Tab(t('conv.tab.tuning')):
                from backend.shared.constants import TUNING_DEFAULTS

                gr.Markdown(f"### {t('conv.tuning_title')}")
                gr.Markdown(t('conv.tuning_desc'))

                # Store tuning input components
                tuning_inputs = {}

                # Temperature
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.temperature.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.temperature.range_text')} | {t('tuning.temperature.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.temperature.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['temperature'] = gr.Number(
                                value=TUNING_DEFAULTS['temperature'],
                                label="",
                                elem_id="tuning-temperature",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Top K
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.top_k.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.top_k.range_text')} | {t('tuning.top_k.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.top_k.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['top_k'] = gr.Number(
                                value=TUNING_DEFAULTS['top_k'],
                                label="",
                                elem_id="tuning-top_k",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Top P
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.top_p.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.top_p.range_text')} | {t('tuning.top_p.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.top_p.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['top_p'] = gr.Number(
                                value=TUNING_DEFAULTS['top_p'],
                                label="",
                                elem_id="tuning-top_p",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Min P
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.min_p.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.min_p.range_text')} | {t('tuning.min_p.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.min_p.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['min_p'] = gr.Number(
                                value=TUNING_DEFAULTS['min_p'],
                                label="",
                                elem_id="tuning-min_p",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Repeat Last N
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.repeat_last_n.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.repeat_last_n.range_text')} | {t('tuning.repeat_last_n.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.repeat_last_n.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['repeat_last_n'] = gr.Number(
                                value=TUNING_DEFAULTS['repeat_last_n'],
                                label="",
                                elem_id="tuning-repeat_last_n",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Repeat Penalty
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.repeat_penalty.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.repeat_penalty.range_text')} | {t('tuning.repeat_penalty.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.repeat_penalty.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['repeat_penalty'] = gr.Number(
                                value=TUNING_DEFAULTS['repeat_penalty'],
                                label="",
                                elem_id="tuning-repeat_penalty",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Presence Penalty
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.presence_penalty.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.presence_penalty.range_text')} | {t('tuning.presence_penalty.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.presence_penalty.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['presence_penalty'] = gr.Number(
                                value=TUNING_DEFAULTS['presence_penalty'],
                                label="",
                                elem_id="tuning-presence_penalty",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Frequency Penalty
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.frequency_penalty.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.frequency_penalty.range_text')} | {t('tuning.frequency_penalty.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.frequency_penalty.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['frequency_penalty'] = gr.Number(
                                value=TUNING_DEFAULTS['frequency_penalty'],
                                label="",
                                elem_id="tuning-frequency_penalty",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Num Predict
                with gr.Group(elem_classes=["tuning-param-group"]):
                    with gr.Row():
                        with gr.Column(scale=2):
                            gr.Markdown(f"**{t('tuning.num_predict.label')}**")
                            gr.Markdown(f"{t('tuning.range')}: {t('tuning.num_predict.range_text')} | {t('tuning.num_predict.hint')}", elem_classes=["tuning-hint"])
                            gr.Markdown(t('tuning.num_predict.description'), elem_classes=["tuning-description"])
                        with gr.Column(scale=1):
                            tuning_inputs['num_predict'] = gr.Number(
                                value=TUNING_DEFAULTS['num_predict'],
                                label="",
                                elem_id="tuning-num_predict",
                                elem_classes=["tuning-input"],
                                interactive=False
                            )

                # Status display（純通知欄。旧初期文 conv.tuning_select_char は
                # ページロード時の初期化イベントが即 "" で上書きするため一度も
                # 表示されない死文だった＝撤去・稜裁定 2026-08-15）
                components['tuning_status'] = gr.Markdown(
                    "",
                    elem_id="tuning-status"
                )

                # Buttons
                with gr.Row():
                    components['tuning_check_btn'] = gr.Button(
                        t('tuning.check'),
                        variant="secondary",
                        interactive=False,
                        elem_id="tuning-check-btn"
                    )
                    components['tuning_load_btn'] = gr.Button(
                        t('tuning.load'),
                        variant="primary",
                        interactive=False,
                        elem_id="tuning-load-btn"
                    )

                # Store tuning inputs in components
                components['tuning_inputs'] = tuning_inputs

                # Hidden state for original values (for change detection)
                components['tuning_original'] = gr.State(value={})

        components['input_tabs'] = input_tabs
        
        # Conversation control button (full width)
        components['start_end_btn'] = gr.Button(
            t('btn.start_conversation'),
            variant="primary",
            elem_id="conversation-toggle"
        )
            
        # Hidden components for state tracking
        components['is_recording'] = gr.State(value=False)
        components['recording_start_time'] = gr.State(value=None)
        components['transcribed_text'] = gr.State(value="")  # Store transcribed text for processing
        # auto_prompt_trigger削除 - WebSocket方式に移行
    
    components['top_section'] = top_section
    components['middle_section'] = middle_section
    components['bottom_section'] = bottom_section
    
    return components


def create_character_settings_page(app_state: Any) -> Dict[str, Any]:
    """Create character settings page with tab switching between create and edit modes."""
    gr.Markdown(f"## {t('char.title')}", elem_classes="page-title")

    # Import the UI creation functions
    from .components import create_character_creation_ui, create_character_edit_ui

    # Dictionary to store all components for event handling
    components = {}

    # Tab container for Create/Edit modes
    with gr.Tabs() as character_tabs:
        # API Settings Tab
        # 先頭に置く（稜裁定 2026-08-11）: APIキーと埋め込みモデルを
        # 先に設定しないとキャラクター自体を作れないため
        with gr.Tab(t('char.tab.api')):
            api_settings_components = create_api_settings_tab()
            components['api_settings'] = api_settings_components

        # Create Character Tab
        with gr.Tab(t('char.tab.create')):
            gr.Markdown(f"### {t('char.create_title')}")
            gr.Markdown(t('char.create_desc'))

            # Create character creation UI
            create_components = create_character_creation_ui()
            components['create'] = create_components

        # Edit Character Tab
        with gr.Tab(t('char.tab.edit')):
            gr.Markdown(f"### {t('char.edit_title')}")
            gr.Markdown(t('char.edit_desc'))

            # Create character edit UI
            edit_components = create_character_edit_ui()
            components['edit'] = edit_components

        # Tuning Defaults Tab
        with gr.Tab(t('char.tab.tuning_defaults')):
            tuning_defaults_components = create_tuning_defaults_tab()
            components['tuning_defaults'] = tuning_defaults_components

        # ELYTH / YouTube session control tabs — server mode では生成しない
        # (クライアント接続中は会話中扱いでセッションが走れない構造のため
        # サーバーモードでは扱わない=稜裁定 2026-07-31。両タブの配線は
        # タブ生成関数内で完結。唯一の例外が YouTube の担当キャラ DD で、
        # app.py がキャラ作成/編集/削除後の choices 更新先として参照する。
        # 非生成時は None ガードで従来の2つ組に落ちる=スキップしても
        # 配線は壊れない。History ページの閲覧タブは残す)。
        if not bool(getattr(app_state, "server_mode_enabled", False)):
            # ELYTH Control Tab
            with gr.Tab(t('char.tab.elyth')):
                elyth_control_components = create_elyth_control_tab()
                components['elyth_control'] = elyth_control_components

            # YouTube Comment Auto-Reply Tab (tab body lives in ui/handlers/youtube.py)
            with gr.Tab(t('char.tab.youtube')):
                from ui.handlers.youtube import create_youtube_control_tab
                youtube_control_components = create_youtube_control_tab()
                components['youtube_control'] = youtube_control_components

    components['tabs'] = character_tabs
    return components


def _get_elyth_status_html() -> str:
    """Generate ELYTH session status HTML with JS-updatable element IDs.

    The actual status is updated in real-time via WebSocket + JavaScript.
    This only provides the initial HTML skeleton.
    """
    return f'''<div id="elyth-status-container" style="padding:12px;background:#222;border-radius:8px;border-left:4px solid #888;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
            <span id="elyth-status-dot" style="width:10px;height:10px;border-radius:50%;background:#888;display:inline-block;"></span>
            <span id="elyth-status-label" style="font-weight:bold;font-size:14px;color:#888;">{t('elyth.initializing')}</span>
            <span id="elyth-status-timer" style="font-size:12px;color:#aaa;margin-left:auto;font-family:monospace;"></span>
        </div>
        <div id="elyth-status-detail" style="font-size:12px;color:#aaa;padding-left:18px;"></div>
        <div id="elyth-activity-log" style="font-size:11px;color:#9ca3af;padding-left:18px;margin-top:6px;max-height:120px;overflow-y:auto;display:none;border-top:1px solid #333;padding-top:4px;"></div>
    </div>'''


def create_elyth_control_tab() -> Dict[str, Any]:
    """Create the ELYTH Control tab for managing autonomous ELYTH sessions."""
    import gradio as gr

    components = {}

    # Tab title + description (最上部 — 稜指示 2026-07-19)
    gr.Markdown(f"### {t('char.tab.elyth')}")
    gr.Markdown(t('elyth.session_desc'))
    gr.Markdown("---")

    # Session Status Section
    gr.Markdown(f"### {t('elyth.session_status')}")
    with gr.Row():
        elyth_status_display = gr.HTML(
            value=_get_elyth_status_html(),
            elem_id="elyth-status-display"
        )
    components["status_display"] = elyth_status_display

    # Buttons rendered via raw HTML for WebSocket control
    # 縦ズレ根治: 英数字/日本語混在文言のベースライン差で箱ごと縦にズレるため、
    # Utility Panel実証済みパターン(各ボタンをdivで包み<style>+!importantでflex化
    # =インライン整列に参加させない)に統一(稜ヒント 2026-07-19)
    elyth_buttons = gr.HTML(
        value=f'''<style>
            #elyth-session-btn-row {{ display:flex !important; gap:8px !important; margin-top:4px !important; }}
            #elyth-session-btn-row > div {{ flex:0 0 auto !important; }}
            #elyth-session-btn-row button {{
                display:flex !important; align-items:center !important;
                justify-content:center !important; height:30px !important;
                padding:0 16px !important; font-size:12px !important;
                color:white !important; border:none !important;
                border-radius:4px !important; cursor:pointer;
            }}
        </style>
        <div id="elyth-session-btn-row">
            <div><button id="elyth-loop-btn"
                onclick="window._elythLoopBtnClick && window._elythLoopBtnClick()"
                style="background:#4caf50;">{t('elyth.auto_loop_on')}</button></div>
            <div><button id="elyth-session-btn"
                onclick="window._elythSessionBtnClick && window._elythSessionBtnClick()"
                style="background:#2196f3;">{t('elyth.start_session')}</button></div>
        </div>
        <div id="elyth-session-msg" style="display:none;margin-top:6px;font-size:12px;color:#f44336;"></div>''',
        elem_id="elyth-session-btn-container"
    )
    components["session_btn"] = elyth_buttons

    gr.Markdown("---")

    # Character Settings Section
    gr.Markdown(f"### {t('elyth.char_settings')}")
    gr.Markdown(t('elyth.char_settings_desc'))

    def _get_elyth_eligible_characters(prefetch_caps=False):
        """Get characters that have an ELYTH API key configured.

        各エントリの "blocked" はセッション実行可否(None=実行可/
        "no_tools"=確定非対応/"unknown"=判定不能。判定は
        backend.elyth.elyth_availability が真実源)。prefetch_caps=True は
        再取得ボタン用: capabilityを先読みして判定不能の解決導線にする
        (Ollama停止中はprefetchが静かに何もしない)。
        """
        import glob
        import json
        from pathlib import Path
        from backend.shared.constants import CHARACTER_CONFIGS_DIR
        from backend.elyth.elyth_availability import block_reason
        raw = []
        for f in sorted(glob.glob(str(CHARACTER_CONFIGS_DIR / "*.json"))):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except Exception:
                continue
            if cfg.get("elyth_api_key"):
                raw.append((cfg, Path(f).stem))
        if prefetch_caps:
            names = [cfg.get("model_name", "") for cfg, _ in raw
                     if cfg.get("model_provider", "ollama") == "ollama"
                     and cfg.get("model_name")]
            if names:
                try:
                    from backend.llm.ollama_capabilities import prefetch
                    prefetch(names)
                except Exception:
                    pass
        return [{
            "id": cfg.get("character_id", stem),
            "name": cfg.get("name", "Unknown"),
            "blocked": block_reason(cfg.get("model_provider", "ollama"),
                                    cfg.get("model_name", "")),
        } for cfg, stem in raw]

    def _build_char_list_html(available_chars, enabled_order,
                              empty_msg_key='elyth.no_chars_hint'):
        """Build HTML for character list with toggle buttons.

        blocked なキャラ(Ollama tools非対応/判定不能)はグレーアウト:
        ON/OFFボタンの代わりにバッジ・クリック無効・理由はtooltip
        (稜裁定 2026-08-15)。
        """
        if not available_chars:
            return f"<div style='padding:12px;color:#888;text-align:center;'>{t(empty_msg_key)}</div>"

        enabled_set = set(enabled_order)
        html = '<div id="elyth-char-list-container" style="font-family:sans-serif;">'
        for ch in available_chars:
            cid = ch["id"]
            name = ch["name"]
            safe_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            blocked = ch.get("blocked")
            if blocked:
                badge_key = ('elyth.char_unavailable' if blocked == 'no_tools'
                             else 'elyth.char_unknown')
                reason = t(f'avail.reason_ollama_{blocked}')
                safe_reason = (reason.replace("&", "&amp;").replace('"', "&quot;")
                               .replace("<", "&lt;").replace(">", "&gt;"))
                html += f'''<div title="{safe_reason}" style="display:flex;align-items:center;gap:8px;padding:6px 8px;margin:2px 0;border-radius:4px;background:rgba(255,255,255,0.03);opacity:0.5;cursor:not-allowed;">
                <span style="padding:3px 10px;font-size:11px;background:#455a64;color:#ccc;border-radius:3px;min-width:40px;text-align:center;">{t(badge_key)}</span>
                <span style="font-size:13px;">{safe_name}</span>
                <span style="font-size:10px;color:#888;margin-left:auto;">{cid[:8]}...</span>
            </div>'''
                continue
            is_enabled = cid in enabled_set
            bg = "#4caf50" if is_enabled else "#607d8b"
            label = "ON" if is_enabled else "OFF"
            html += f'''<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;margin:2px 0;border-radius:4px;background:rgba(255,255,255,0.03);cursor:pointer;" onclick="window._toggleElythChar && window._toggleElythChar('{cid}')">
                <button data-char-id="{cid}" style="padding:3px 10px;font-size:11px;background:{bg};color:white;border:none;border-radius:3px;cursor:pointer;min-width:40px;">{label}</button>
                <span style="font-size:13px;">{safe_name}</span>
                <span style="font-size:10px;color:#888;margin-left:auto;">{cid[:8]}...</span>
            </div>'''
        html += '</div>'
        return html

    _initial_chars = _get_elyth_eligible_characters()
    try:
        from ui.settings_manager import get_setting as _gs
        _initial_order = _gs("elyth", "character_order", [])
        _initial_order = [cid for cid in _initial_order if cid in {ch["id"] for ch in _initial_chars}]
    except Exception:
        _initial_order = []

    elyth_char_list = gr.HTML(value=_build_char_list_html(
        _initial_chars, _initial_order, empty_msg_key='elyth.no_chars'))
    components["char_list"] = elyth_char_list

    # Hidden state to track enabled character IDs and their order
    elyth_char_state = gr.State(value={"order": _initial_order, "available": _initial_chars})
    components["char_state"] = elyth_char_state

    # コンパクト幅+中央寄せ（全幅の細長ボタンは「セッション開始」と不揃い —
    # 稜指摘 2026-07-19。幅と中央寄せは app.py の #elyth-refresh-chars-btn CSS）
    with gr.Row():
        elyth_refresh_btn = gr.Button(t('elyth.refresh_chars'), size="sm", elem_id="elyth-refresh-chars-btn")

    def refresh_elyth_chars(state):
        from ui.settings_manager import get_setting
        from backend.elyth.elyth_availability import enforce_character_order
        available = _get_elyth_eligible_characters(prefetch_caps=True)
        # 確定非対応(tools無し)でONのままのキャラを強制OFF(稜裁定 2026-08-15。
        # 判定不能は温存=一時的な状態でON設定を破壊しない)
        enforce_character_order()
        order = get_setting("elyth", "character_order", [])
        # Filter order to only include available characters
        available_ids = {ch["id"] for ch in available}
        order = [cid for cid in order if cid in available_ids]
        new_state = {"order": order, "available": available}
        html = _build_char_list_html(available, order)
        return html, new_state

    elyth_refresh_btn.click(
        fn=refresh_elyth_chars,
        inputs=[elyth_char_state],
        outputs=[elyth_char_list, elyth_char_state],
    )

    gr.Markdown("---")

    # Global Settings Section
    gr.Markdown(f"### {t('elyth.schedule')}")
    # Initialize from saved settings so values persist across restarts
    # (off-time window removed in YE/J9 2026-07-11: the timer is pure awake
    # time — it runs whenever AG runs and pauses during conversations)
    try:
        from ui.settings_manager import get_setting
        interval_default = int(get_setting("elyth", "interval_seconds", 3600)) // 60
    except Exception:
        interval_default = 60
    with gr.Row():
        elyth_interval = gr.Number(
            label=t('elyth.interval'),
            value=interval_default,
            minimum=10,
            maximum=1440,
            precision=0,
        )
    components["interval"] = elyth_interval

    gr.Markdown("---")

    # Common Instructions Section
    gr.Markdown(f"### {t('elyth.common_instructions')}")
    gr.Markdown(t('elyth.common_instructions_desc'))

    try:
        from ui.settings_manager import get_setting
        from backend.shared.i18n import current_language
        from backend.shared.prompt_i18n import prompt_section
        current_instructions = get_setting(
            "elyth", "instructions", prompt_section("elyth_default_instructions", current_language()))
    except Exception:
        current_instructions = ""

    elyth_instructions = gr.Textbox(
        label=t('elyth.instructions_label'),
        value=current_instructions,
        lines=15,
        max_lines=30,
    )
    components["instructions"] = elyth_instructions

    # --- Prompt privacy settings (spec v6 §8, session mode only) ---
    gr.Markdown(f"### {t('elyth.prompt_settings')}")
    gr.Markdown(t('elyth.prompt_settings_desc'))

    try:
        from ui.settings_manager import get_setting as _get_elyth_setting
        _cur_include_notes = bool(_get_elyth_setting("elyth", "include_user_notes", False))
        _cur_include_rel = bool(_get_elyth_setting("elyth", "include_user_relationship", False))
        _cur_rag_mode = _get_elyth_setting("elyth", "memory_rag_mode", "activity_only")
    except Exception:
        _cur_include_notes, _cur_include_rel, _cur_rag_mode = False, False, "activity_only"

    _rag_choices = [
        (t('elyth.rag_off'), "off"),
        (t('elyth.rag_activity_only'), "activity_only"),
        (t('elyth.rag_all'), "all"),
    ]
    if _cur_rag_mode not in ("off", "activity_only", "all"):
        _cur_rag_mode = "activity_only"

    elyth_include_notes = gr.Checkbox(
        label=t('elyth.include_user_notes'), value=_cur_include_notes)
    elyth_include_rel = gr.Checkbox(
        label=t('elyth.include_user_relationship'), value=_cur_include_rel)
    elyth_rag_mode = gr.Dropdown(
        label=t('elyth.memory_rag_mode'),
        choices=_rag_choices, value=_cur_rag_mode)
    components["include_user_notes"] = elyth_include_notes
    components["include_user_relationship"] = elyth_include_rel
    components["memory_rag_mode"] = elyth_rag_mode

    elyth_save_btn = gr.Button(t('elyth.save'), variant="primary")
    elyth_save_status = gr.Markdown("", visible=False, elem_id="elyth-save-status")
    components["save_btn"] = elyth_save_btn
    components["save_status"] = elyth_save_status

    # Save handler (character order is saved immediately via toggle, so only
    # schedule + instructions + prompt-privacy settings here)
    def save_elyth_settings(interval, instructions, include_notes, include_rel, rag_mode):
        try:
            from ui.settings_manager import delete_setting, update_setting

            update_setting("elyth", "interval_seconds", int(interval) * 60)
            update_setting("elyth", "include_user_notes", bool(include_notes))
            update_setting("elyth", "include_user_relationship", bool(include_rel))
            update_setting("elyth", "memory_rag_mode",
                           rag_mode if rag_mode in ("off", "activity_only", "all")
                           else "activity_only")
            # 未編集デフォルト(いずれかの言語の原文と一致)は保存しない=キー削除。
            # 保存すると言語追従が止まる(旧・焼き込みバグの再発防止)。編集値のみ
            # 保存し、使用時は resolve_elyth_instructions がUI言語で解決する。
            from backend.shared.prompt_i18n import available_prompt_languages, prompt_section
            _defaults = {prompt_section("elyth_default_instructions", lang).strip()
                         for lang in available_prompt_languages()}
            if (instructions or "").strip() in _defaults:
                delete_setting("elyth", "instructions")
            else:
                update_setting("elyth", "instructions", instructions)

            # Reload settings in session manager
            try:
                from backend.elyth.elyth_session_manager import get_elyth_session_manager
                get_elyth_session_manager().reload_settings()
            except Exception:
                pass

            return gr.update(value=t('elyth.saved'), visible=True)
        except Exception as e:
            return gr.update(value=t('elyth.save_error', error=e), visible=True)

    elyth_save_btn.click(
        fn=save_elyth_settings,
        inputs=[elyth_interval, elyth_instructions,
                elyth_include_notes, elyth_include_rel, elyth_rag_mode],
        outputs=[elyth_save_status],
    ).then(
        fn=lambda: None, inputs=[], outputs=[],
        js=status_auto_hide_js('elyth-save-status'), queue=False
    )

    # Stop/Start is handled via WebSocket + JavaScript (no Gradio callback needed)

    # Load current settings
    # Load Settings button to refresh UI from saved values
    elyth_load_btn = gr.Button(t('elyth.reload'), variant="secondary", size="sm")
    components["load_btn"] = elyth_load_btn

    def load_current_settings():
        try:
            from ui.settings_manager import get_setting
            from backend.shared.i18n import current_language
            from backend.shared.prompt_i18n import prompt_section
            interval = get_setting("elyth", "interval_seconds", 3600) // 60
            instructions = get_setting(
                "elyth", "instructions", prompt_section("elyth_default_instructions", current_language()))
            include_notes = bool(get_setting("elyth", "include_user_notes", False))
            include_rel = bool(get_setting("elyth", "include_user_relationship", False))
            rag_mode = get_setting("elyth", "memory_rag_mode", "activity_only")
            if rag_mode not in ("off", "activity_only", "all"):
                rag_mode = "activity_only"
            return (
                interval,
                instructions,
                include_notes,
                include_rel,
                rag_mode,
            )
        except Exception:
            return (60, "", False, False, "activity_only")

    elyth_load_btn.click(
        fn=load_current_settings,
        inputs=[],
        outputs=[elyth_interval, elyth_instructions,
                 elyth_include_notes, elyth_include_rel, elyth_rag_mode],
    )

    return components


def create_api_settings_tab() -> Dict[str, Any]:
    """Create the API Settings tab for managing external LLM API keys and models."""
    from backend.shared.api_settings import load_api_settings, PROVIDER_CONFIG

    gr.Markdown(f"### {t('api.title')}")
    gr.Markdown(t('api.page_desc'))
    gr.Markdown("---")

    settings = load_api_settings()
    components = {}

    # --- External LLM providers ---
    gr.Markdown(f"### {t('api.llm_title')}")
    gr.Markdown(t('api.desc'))

    # --- ChatGPT (OpenAI) ---
    with gr.Accordion("ChatGPT (OpenAI)", open=True):
        with gr.Row():
            openai_key = gr.Textbox(
                label=t('api.key'),
                value=settings.get("openai", {}).get("api_key", ""),
                type="password",
                scale=4
            )
            openai_save_btn = gr.Button(t('common.save'), scale=1)
        openai_web_search = gr.Checkbox(
            label=t('api.web_search'),
            value=settings.get("openai", {}).get("web_search_enabled", False)
        )
        with gr.Row():
            openai_models = gr.Dropdown(
                label=t('api.models'),
                choices=settings.get("openai", {}).get("available_models", []),
                interactive=True,
                scale=4
            )
            openai_refresh_btn = gr.Button(t('common.refresh'), scale=1)
        openai_status = gr.Markdown("", visible=False, elem_id="openai-status")

    components['openai_key'] = openai_key
    components['openai_save_btn'] = openai_save_btn
    components['openai_web_search'] = openai_web_search
    components['openai_models'] = openai_models
    components['openai_refresh_btn'] = openai_refresh_btn
    components['openai_status'] = openai_status

    # --- Claude (Anthropic) ---
    with gr.Accordion("Claude (Anthropic)", open=True):
        with gr.Row():
            anthropic_key = gr.Textbox(
                label=t('api.key'),
                value=settings.get("anthropic", {}).get("api_key", ""),
                type="password",
                scale=4
            )
            anthropic_save_btn = gr.Button(t('common.save'), scale=1)
        anthropic_web_search = gr.Checkbox(
            label=t('api.web_search'),
            value=settings.get("anthropic", {}).get("web_search_enabled", False)
        )
        with gr.Row():
            anthropic_models = gr.Dropdown(
                label=t('api.models'),
                choices=settings.get("anthropic", {}).get("available_models", []),
                interactive=True,
                scale=4
            )
            anthropic_refresh_btn = gr.Button(t('common.refresh'), scale=1)
        anthropic_status = gr.Markdown("", visible=False, elem_id="anthropic-status")

    components['anthropic_key'] = anthropic_key
    components['anthropic_save_btn'] = anthropic_save_btn
    components['anthropic_web_search'] = anthropic_web_search
    components['anthropic_models'] = anthropic_models
    components['anthropic_refresh_btn'] = anthropic_refresh_btn
    components['anthropic_status'] = anthropic_status

    # --- Grok (xAI) ---
    with gr.Accordion("Grok (xAI)", open=True):
        with gr.Row():
            xai_key = gr.Textbox(
                label=t('api.key'),
                value=settings.get("xai", {}).get("api_key", ""),
                type="password",
                scale=4
            )
            xai_save_btn = gr.Button(t('common.save'), scale=1)
        xai_web_search = gr.Checkbox(
            label=t('api.web_search'),
            value=settings.get("xai", {}).get("web_search_enabled", False)
        )
        xai_x_search = gr.Checkbox(
            label=t('api.x_search'),
            value=settings.get("xai", {}).get("x_search_enabled", False)
        )
        with gr.Row():
            xai_models = gr.Dropdown(
                label=t('api.models'),
                choices=settings.get("xai", {}).get("available_models", []),
                interactive=True,
                scale=4
            )
            xai_refresh_btn = gr.Button(t('common.refresh'), scale=1)
        xai_status = gr.Markdown("", visible=False, elem_id="xai-status")

    components['xai_key'] = xai_key
    components['xai_save_btn'] = xai_save_btn
    components['xai_web_search'] = xai_web_search
    components['xai_x_search'] = xai_x_search
    components['xai_models'] = xai_models
    components['xai_refresh_btn'] = xai_refresh_btn
    components['xai_status'] = xai_status

    # --- Gemini (Google) ---
    with gr.Accordion("Gemini (Google)", open=True):
        with gr.Row():
            google_key = gr.Textbox(
                label=t('api.key'),
                value=settings.get("google", {}).get("api_key", ""),
                type="password",
                scale=4
            )
            google_save_btn = gr.Button(t('common.save'), scale=1)
        google_web_search = gr.Checkbox(
            label=t('api.web_search'),
            value=settings.get("google", {}).get("web_search_enabled", False)
        )
        with gr.Row():
            google_models = gr.Dropdown(
                label=t('api.models'),
                choices=settings.get("google", {}).get("available_models", []),
                interactive=True,
                scale=4
            )
            google_refresh_btn = gr.Button(t('common.refresh'), scale=1)
        google_status = gr.Markdown("", visible=False, elem_id="google-status")

    components['google_key'] = google_key
    components['google_save_btn'] = google_save_btn
    components['google_web_search'] = google_web_search
    components['google_models'] = google_models
    components['google_refresh_btn'] = google_refresh_btn
    components['google_status'] = google_status

    # --- ElevenLabs (TTS) ---
    from backend.shared.api_settings import (
        get_elevenlabs_voice_display_choices, get_elevenlabs_model_choices,
        get_elevenlabs_model_id,
    )
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.tts_title')}")
    gr.Markdown(t('api.eleven_desc'))
    with gr.Accordion("ElevenLabs (TTS)", open=True):
        with gr.Row():
            eleven_key = gr.Textbox(
                label=t('api.key'),
                value=settings.get("elevenlabs", {}).get("api_key", ""),
                type="password",
                scale=4
            )
            eleven_save_btn = gr.Button(t('common.save'), scale=1)
        with gr.Row():
            eleven_voices_dd = gr.Dropdown(
                label=t('api.eleven_voices_label'),
                choices=get_elevenlabs_voice_display_choices(),
                interactive=True,
                scale=4
            )
            eleven_voices_refresh_btn = gr.Button(t('common.refresh'), scale=1)
        eleven_model_choices = get_elevenlabs_model_choices()
        current_eleven_model = get_elevenlabs_model_id()
        if current_eleven_model and current_eleven_model not in [v for _, v in eleven_model_choices]:
            # Keep the saved model visible before the first refresh (same
            # fallback pattern as the embedding dropdown below).
            eleven_model_choices.append(
                (t('api.current_setting', model=current_eleven_model), current_eleven_model))
        with gr.Row():
            eleven_model_dd = gr.Dropdown(
                label=t('api.eleven_model_label'),
                choices=eleven_model_choices,
                value=current_eleven_model,
                interactive=True,
                scale=3
            )
            eleven_models_refresh_btn = gr.Button(t('common.refresh'), scale=1)
            eleven_model_save_btn = gr.Button(t('common.save'), scale=1)
        eleven_status = gr.Markdown("", visible=False, elem_id="eleven-status")

    components['eleven_key'] = eleven_key
    components['eleven_save_btn'] = eleven_save_btn
    components['eleven_voices_dd'] = eleven_voices_dd
    components['eleven_voices_refresh_btn'] = eleven_voices_refresh_btn
    components['eleven_model_dd'] = eleven_model_dd
    components['eleven_models_refresh_btn'] = eleven_models_refresh_btn
    components['eleven_model_save_btn'] = eleven_model_save_btn
    components['eleven_status'] = eleven_status

    # --- Google Maps API Key ---
    # (他のAPIキー入力ブロックの並びに置く: 稜裁定 2026-07-22。
    #  モデル選択系[埋め込み/画像生成]より上)
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.maps_title')}")
    gr.Markdown(t('api.maps_desc'))

    from backend.shared.api_settings import get_google_maps_api_key
    with gr.Row():
        google_maps_key = gr.Textbox(
            label=t('api.maps_label'),
            value=get_google_maps_api_key(),
            type="password",
            scale=4
        )
        google_maps_save_btn = gr.Button(t('common.save'), scale=1)
    google_maps_status = gr.Markdown("", visible=False, elem_id="google-maps-status")

    components['google_maps_key'] = google_maps_key
    components['google_maps_save_btn'] = google_maps_save_btn
    components['google_maps_status'] = google_maps_status

    # --- Embedding Model ---
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.embedding_title')}")
    gr.Markdown(t('api.embedding_desc'))

    # Embedding-capable models only (ST6 §7-4 sequel): the unfiltered list let
    # a chat model be picked here, which Ollama silently "embeds" with garbage
    # quality. The character-LLM dropdown keeps get_all_available_models.
    from backend.shared.api_settings import get_embedding_model_choices
    all_models = get_embedding_model_choices()
    # 未設定はNone(デフォルト補填廃止 2026-07-25)=ドロップダウンは未選択表示。
    # 会話開始時のガードが設定を促す。
    current_embedding = settings.get("embedding_model") or None
    if current_embedding and current_embedding not in [v for _, v in all_models]:
        # Keep a previously saved model visible even if the filter would hide
        # it (or its provider is offline), so the current setting stays shown.
        all_models.append((t('api.current_setting', model=current_embedding), current_embedding))

    with gr.Row():
        embedding_model_dropdown = gr.Dropdown(
            label=t('api.embedding_label'),
            choices=all_models,
            value=current_embedding,
            interactive=True,
            scale=4
        )
        embedding_save_btn = gr.Button(t('common.save'), scale=1)
    embedding_status = gr.Markdown("", visible=False, elem_id="embedding-status")

    components['embedding_model_dropdown'] = embedding_model_dropdown
    components['embedding_save_btn'] = embedding_save_btn
    components['embedding_status'] = embedding_status

    # --- Image Generation Model ---
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.imagen_title')}")
    gr.Markdown(t('api.imagen_desc'))

    from backend.shared.api_settings import get_imagen_models, get_image_generation_model
    imagen_choices = get_imagen_models()
    _, current_ig_model = get_image_generation_model()
    current_ig_value = f"google::{current_ig_model}" if current_ig_model else ""

    with gr.Row():
        image_gen_model_dropdown = gr.Dropdown(
            label=t('api.imagen_label'),
            choices=imagen_choices,
            value=current_ig_value if current_ig_value else None,
            interactive=True,
            scale=4
        )
        image_gen_save_btn = gr.Button(t('common.save'), scale=1)
    image_gen_status = gr.Markdown("", visible=False, elem_id="image-gen-status")

    components['image_gen_model_dropdown'] = image_gen_model_dropdown
    components['image_gen_save_btn'] = image_gen_save_btn
    components['image_gen_status'] = image_gen_status

    # --- Ollama Context Size (num_ctx) ---
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.ollama_ctx_title')}")
    gr.Markdown(t('api.ollama_ctx_desc'))

    from backend.shared.api_settings import (
        OLLAMA_NUM_CTX_CHOICES, get_ollama_num_ctx)
    current_num_ctx = get_ollama_num_ctx()
    ctx_choices = [(f"{v:,}", v) for v in OLLAMA_NUM_CTX_CHOICES]
    if current_num_ctx not in OLLAMA_NUM_CTX_CHOICES:
        # 設定ファイル手編集値はプリセット外でも温存表示する
        ctx_choices.append((f"{current_num_ctx:,}", current_num_ctx))

    with gr.Row():
        ollama_ctx_dropdown = gr.Dropdown(
            label=t('api.ollama_ctx_label'),
            choices=ctx_choices,
            value=current_num_ctx,
            interactive=True,
            scale=4
        )
        ollama_ctx_save_btn = gr.Button(t('common.save'), scale=1)
    ollama_ctx_status = gr.Markdown("", visible=False, elem_id="ollama-ctx-status")

    components['ollama_ctx_dropdown'] = ollama_ctx_dropdown
    components['ollama_ctx_save_btn'] = ollama_ctx_save_btn
    components['ollama_ctx_status'] = ollama_ctx_status

    # --- Web Search Blacklist ---
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.ws_blacklist_title')}")
    gr.Markdown(t('api.ws_blacklist_desc'))

    from backend.shared.api_settings import get_web_search_blacklist, decode_model_value, get_provider_display_name
    blacklist = get_web_search_blacklist()
    blacklist_choices = []
    for encoded in blacklist:
        provider, model_name = decode_model_value(encoded)
        display = get_provider_display_name(provider)
        blacklist_choices.append((f"{model_name} ({display})", encoded))

    blacklist_display = gr.CheckboxGroup(
        choices=blacklist_choices,
        label=t('api.blacklisted_models'),
        value=[],
    )
    with gr.Row():
        blacklist_refresh_btn = gr.Button(t('common.refresh'))
        blacklist_remove_btn = gr.Button(t('api.remove_selected'))

    components['blacklist_display'] = blacklist_display
    components['blacklist_refresh_btn'] = blacklist_refresh_btn
    components['blacklist_remove_btn'] = blacklist_remove_btn

    # --- Image Input Blacklist ---
    gr.Markdown("---")
    gr.Markdown(f"### {t('api.img_blacklist_title')}")
    gr.Markdown(t('api.img_blacklist_desc'))

    from backend.shared.api_settings import get_image_blacklist
    img_blacklist = get_image_blacklist()
    img_blacklist_choices = []
    for encoded in img_blacklist:
        provider, model_name = decode_model_value(encoded)
        display = get_provider_display_name(provider)
        img_blacklist_choices.append((f"{model_name} ({display})", encoded))

    img_blacklist_display = gr.CheckboxGroup(
        choices=img_blacklist_choices,
        label=t('api.blacklisted_models'),
        value=[],
    )
    with gr.Row():
        img_blacklist_refresh_btn = gr.Button(t('common.refresh'))
        img_blacklist_remove_btn = gr.Button(t('api.remove_selected'))

    components['img_blacklist_display'] = img_blacklist_display
    components['img_blacklist_refresh_btn'] = img_blacklist_refresh_btn
    components['img_blacklist_remove_btn'] = img_blacklist_remove_btn

    return components


def create_tuning_defaults_tab() -> Dict[str, Any]:
    """Create the Tuning Defaults tab for editing default tuning parameters."""
    from backend.shared.constants import get_tuning_defaults

    gr.Markdown(f"### {t('tdefaults.title')}")
    gr.Markdown(t('tdefaults.desc'))

    # Get current defaults
    defaults = get_tuning_defaults()

    # Store tuning input components
    tuning_inputs = {}

    # Temperature
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.temperature.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.temperature.range_text')} | {t('tuning.temperature.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.temperature.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['temperature'] = gr.Number(
                    value=defaults['temperature'],
                    label="",
                    elem_id="defaults-tuning-temperature",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Top K
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.top_k.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.top_k.range_text')} | {t('tuning.top_k.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.top_k.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['top_k'] = gr.Number(
                    value=defaults['top_k'],
                    label="",
                    elem_id="defaults-tuning-top_k",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Top P
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.top_p.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.top_p.range_text')} | {t('tuning.top_p.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.top_p.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['top_p'] = gr.Number(
                    value=defaults['top_p'],
                    label="",
                    elem_id="defaults-tuning-top_p",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Min P
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.min_p.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.min_p.range_text')} | {t('tuning.min_p.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.min_p.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['min_p'] = gr.Number(
                    value=defaults['min_p'],
                    label="",
                    elem_id="defaults-tuning-min_p",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Repeat Last N
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.repeat_last_n.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.repeat_last_n.range_text')} | {t('tuning.repeat_last_n.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.repeat_last_n.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['repeat_last_n'] = gr.Number(
                    value=defaults['repeat_last_n'],
                    label="",
                    elem_id="defaults-tuning-repeat_last_n",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Repeat Penalty
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.repeat_penalty.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.repeat_penalty.range_text')} | {t('tuning.repeat_penalty.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.repeat_penalty.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['repeat_penalty'] = gr.Number(
                    value=defaults['repeat_penalty'],
                    label="",
                    elem_id="defaults-tuning-repeat_penalty",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Presence Penalty
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.presence_penalty.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.presence_penalty.range_text')} | {t('tuning.presence_penalty.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.presence_penalty.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['presence_penalty'] = gr.Number(
                    value=defaults['presence_penalty'],
                    label="",
                    elem_id="defaults-tuning-presence_penalty",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Frequency Penalty
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.frequency_penalty.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.frequency_penalty.range_text')} | {t('tuning.frequency_penalty.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.frequency_penalty.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['frequency_penalty'] = gr.Number(
                    value=defaults['frequency_penalty'],
                    label="",
                    elem_id="defaults-tuning-frequency_penalty",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Num Predict
    with gr.Group(elem_classes=["tuning-param-group"]):
        with gr.Row():
            with gr.Column(scale=2):
                gr.Markdown(f"**{t('tuning.num_predict.label')}**")
                gr.Markdown(f"{t('tuning.range')}: {t('tuning.num_predict.range_text')} | {t('tuning.num_predict.hint')}", elem_classes=["tuning-hint"])
                gr.Markdown(t('tuning.num_predict.description'), elem_classes=["tuning-description"])
            with gr.Column(scale=1):
                tuning_inputs['num_predict'] = gr.Number(
                    value=defaults['num_predict'],
                    label="",
                    elem_id="defaults-tuning-num_predict",
                    elem_classes=["tuning-input"],
                    interactive=True
                )

    # Status display
    status = gr.Markdown(
        "",
        elem_id="defaults-tuning-status"
    )

    # Buttons
    with gr.Row():
        check_btn = gr.Button(
            t('tuning.check'),
            variant="secondary",
            elem_id="defaults-tuning-check-btn"
        )
        load_btn = gr.Button(
            t('tuning.load'),
            variant="primary",
            elem_id="defaults-tuning-load-btn"
        )

    # Hidden state for original values (for change detection)
    original_state = gr.State(value=defaults)

    # ============================================
    # Apply to All Characters Section
    # ============================================
    gr.Markdown("---")
    gr.Markdown(f"### {t('tdefaults.apply_all_title')}")
    gr.Markdown(t('tdefaults.apply_all_warning'))

    # Apply to All button
    apply_all_btn = gr.Button(
        t('tdefaults.apply_all_btn'),
        variant="secondary",
        elem_id="defaults-apply-all-btn"
    )

    # Confirmation dialog (initially hidden)
    with gr.Group(visible=False) as apply_all_confirm_box:
        apply_all_confirm_text = gr.Markdown(
            t('tdefaults.apply_all_confirm')
        )
        with gr.Row():
            apply_all_confirm_yes = gr.Button(t('tdefaults.confirm_yes'), variant="stop")
            apply_all_confirm_no = gr.Button(t('common.cancel'))

    # Status display for apply all
    apply_all_status = gr.Markdown(
        "",
        elem_id="defaults-apply-all-status"
    )

    return {
        'tuning_inputs': tuning_inputs,
        'status': status,
        'check_btn': check_btn,
        'load_btn': load_btn,
        'original': original_state,
        'apply_all_btn': apply_all_btn,
        'apply_all_confirm_box': apply_all_confirm_box,
        'apply_all_confirm_text': apply_all_confirm_text,
        'apply_all_confirm_yes': apply_all_confirm_yes,
        'apply_all_confirm_no': apply_all_confirm_no,
        'apply_all_status': apply_all_status
    }


def create_conversation_history_page(app_state: Any) -> Dict[str, Any]:
    """Create conversation history viewer page."""
    gr.Markdown(f"## {t('history.title')}", elem_classes="page-title")
    
    components = {}
    
    with gr.Row():
        with gr.Column(scale=1):
            # Character selection
            components['character_dropdown'] = gr.Dropdown(
                label=t('history.select_char'),
                choices=[],
                value=None,
                interactive=True,
                elem_id="history-character-select"
            )
            
            # Stats display
            components['memory_stats'] = gr.Markdown(
                t('history.stats_hint'),
                elem_id="history-stats"
            )
            
            # Refresh button
            components['refresh_btn'] = gr.Button(
                t('common.refresh_btn'),
                variant="secondary"
            )
        
        with gr.Column(scale=3):
            with gr.Tabs() as tabs:
                # Short-term memory tab
                with gr.Tab(t('history.tab.recent')):
                    gr.Markdown(t('history.desc_recent'))
                    components['countdown_display'] = gr.Markdown("")
                    components['short_term_display'] = gr.HTML(
                        value=f"<div class='history-empty'>{t('history.empty_recent')}</div>",
                        elem_id="history-short-term"
                    )

                # Notes tab (conversation)
                with gr.Tab(t('history.tab.notes')):
                    gr.Markdown(t('history.desc_notes'))
                    components['notes_display'] = gr.HTML(
                        value=f"<div class='history-empty'>{t('history.empty_notes')}</div>",
                        elem_id="history-notes"
                    )

                # Long-term memory tab
                with gr.Tab(t('history.tab.memory')):
                    gr.Markdown(t('history.desc_memory'))
                    # Main display
                    components['long_term_display'] = gr.HTML(
                        value=f"<div class='history-empty'>{t('history.empty_memory')}</div>",
                        elem_id="history-long-term"
                    )

                    # Add memory section
                    with gr.Accordion(t('history.add_memory'), open=False):
                        with gr.Row():
                            components['memory_category'] = gr.Dropdown(
                                label=t('history.category'),
                                choices=[
                                    (t('history.cat_user_fact'), "user_fact"),
                                    (t('history.cat_user_preference'), "user_preference"),
                                    (t('history.cat_character_relationship'), "character_relationship"),
                                    (t('history.cat_shared_experience'), "shared_experience"),
                                    (t('history.cat_user_opinion'), "user_opinion"),
                                ],
                                value="user_fact",
                                interactive=True,
                                scale=1
                            )
                            components['memory_content_input'] = gr.Textbox(
                                label=t('history.content'),
                                placeholder=t('history.content_placeholder'),
                                lines=2,
                                interactive=True,
                                scale=3
                            )
                        with gr.Row():
                            components['memory_add_btn'] = gr.Button(
                                t('history.add_btn'),
                                variant="primary",
                                scale=1
                            )
                            components['memory_action_status'] = gr.Markdown(
                                "",
                                elem_id="memory-action-status"
                            )

                    # Hidden components for memory actions (JS → Gradio bridge)
                    with gr.Row(visible=False):
                        components['memory_action_trigger'] = gr.Textbox(
                            elem_id="memory-action-trigger",
                            visible=False
                        )
                        components['memory_action_btn'] = gr.Button(
                            "Memory Action",
                            elem_id="memory-action-btn",
                            visible=False
                        )
    
                # ELYTH Notes tab
                with gr.Tab(t('history.tab.elyth_notes')):
                    gr.Markdown(t('history.desc_elyth_notes'))
                    components['elyth_notes_display'] = gr.HTML(
                        value=f"<div class='history-empty'>{t('history.empty_elyth_notes')}</div>",
                        elem_id="history-elyth-notes"
                    )

                # ELYTH Session Logs tab
                with gr.Tab(t('history.tab.elyth_sessions')):
                    gr.Markdown(t('history.desc_elyth_sessions'))
                    components['elyth_sessions_display'] = gr.HTML(
                        value=f"<div class='history-empty'>{t('history.empty_elyth_sessions')}</div>",
                        elem_id="history-elyth-sessions"
                    )

                # YouTube reply sessions tab (beta) — visible only while the
                # character configured as youtube.character_id is selected
                with gr.Tab(t('history.tab.youtube'), visible=False) as youtube_tab:
                    # Beta banner (稜依頼 2026-07-25): dry-run 検証止まりのため
                    # タブ内最上部で目立たせる。色はダーク/ライト両テーマで沈まない
                    # 半透明アンバー。
                    gr.HTML(
                        "<div style='border:1px solid rgba(217,119,6,.65);"
                        "background:rgba(217,119,6,.14);border-radius:8px;"
                        "padding:10px 14px;margin:4px 0 8px 0;'>"
                        f"<strong style='color:#d97706;'>{t('youtube.beta_title')}</strong>"
                        f"<span style='margin-left:8px;'>{t('youtube.beta_body')}</span>"
                        "</div>"
                    )
                    gr.Markdown(t('history.desc_youtube'))
                    components['youtube_sessions_display'] = gr.HTML(
                        value=f"<div class='history-empty'>{t('history.empty_youtube')}</div>",
                        elem_id="history-youtube-sessions"
                    )
                components['youtube_tab'] = youtube_tab

    # Hidden state
    components['current_character'] = gr.State(None)
    
    return components


def create_system_logs_page(app_state: Any, server_mode_enabled: bool = False) -> Dict[str, Any]:
    """Create system logs page with tabs for system logs and prompt log."""
    from .components import (
        create_log_panel, update_log_view, update_prompt_view,
        update_prompt_token_view,
    )

    gr.Markdown(f"## {t('logs.title')}", elem_classes="page-title")

    # Create components dict that will contain all components
    all_components = {}

    with gr.Tabs() as log_tabs:
        # System Logs Tab (preserve existing functionality exactly)
        with gr.Tab(t('logs.tab.system')):
            if server_mode_enabled:
                gr.Markdown(t('logs.desc_server'))
            else:
                gr.Markdown(t('logs.desc_local'))
            
            # Use existing create_log_panel function
            log_container, log_components = create_log_panel()
            log_container.visible = True
            log_components["log_textbox"].value = update_log_view()
            
            # Merge log_components into all_components (preserves existing names)
            all_components.update(log_components)
        
        # Prompt Log Tab (new functionality)
        with gr.Tab(t('logs.tab.prompt')):
            with gr.Column():
                gr.Markdown(f"### {t('logs.prompt_title')}")
                gr.Markdown(t('logs.prompt_desc'))
                if server_mode_enabled:
                    # サーバーモードはタイマー停止=手動更新(システムログタブと同じ注意書き)
                    gr.Markdown(t('logs.manual_refresh_note'))
                
                with gr.Row():
                    gr.Markdown("")  # Spacer
                    prompt_refresh_btn = gr.Button(t('common.refresh_btn'), size="sm", scale=0)

                # 本文の上のトークン数情報行（実測/推定の1本表示・2026-08-14）。
                # 書き手はWS→JS（ws_client_js 'prompt_token_info'）のみ=JS専有
                # 素div。Gradioが触るのは初期値だけ（gr.Timer更新の可視要素は
                # ちらつく既知問題のためタイマー/ボタン出力に載せない）
                gr.HTML(
                    value=('<div id="prompt-token-info" style="min-height:1.4em;">'
                           f'{update_prompt_token_view()}</div>'),
                )

                prompt_textbox = gr.Textbox(
                    label="",
                    value=update_prompt_view(),
                    lines=30,
                    max_lines=50,
                    interactive=False,
                    elem_id="prompt-log-panel",
                    show_copy_button=True
                )
                
                # Add prompt components with unique names
                all_components["prompt_textbox"] = prompt_textbox
                all_components["prompt_refresh_btn"] = prompt_refresh_btn

        # Command Logs Tab — サーバーモードではコマンド実行自体が抑止される
        # (BackendState.server_mode)ためタブごと出さない。下流の配線(app.pyの
        # タイマー/更新ボタン)は cmd_log_* の存在チェック付きなので波及なし
        if not server_mode_enabled:
            with gr.Tab(t('logs.tab.command')):
                with gr.Column():
                    gr.Markdown(f"### {t('logs.command_title')}")
                    gr.Markdown(t('logs.command_desc'))

                    with gr.Row():
                        gr.Markdown("")  # Spacer
                        cmd_log_refresh_btn = gr.Button(t('common.refresh_btn'), size="sm", scale=0)

                    cmd_log_textbox = gr.Textbox(
                        label="",
                        value=t('logs.command_empty'),
                        lines=25,
                        max_lines=50,
                        interactive=False,
                        elem_id="command-log-panel",
                        show_copy_button=True
                    )

                    all_components["cmd_log_textbox"] = cmd_log_textbox
                    all_components["cmd_log_refresh_btn"] = cmd_log_refresh_btn

    all_components["log_tabs"] = log_tabs
    return all_components


def create_system_controls_page(app_state: Any, server_mode_enabled: bool = False) -> Dict[str, Any]:
    """Create system controls page and return button components.

    Args:
        app_state: Application state instance
        server_mode_enabled: Whether server mode is active

    Returns:
        Dict[str, Any]: Dictionary containing button components
    """
    gr.Markdown(f"## {t('system.title')}", elem_classes="page-title")

    if server_mode_enabled:
        # Server mode ON: remote desktop view — minimal controls
        with gr.Column():
            gr.Markdown(t('system.server_admin_note'))
            # size="sm" だと細すぎる（稜指摘 2026-07-19）→ 通常サイズ
            disconnect_btn = gr.Button(
                t('system.disconnect'),
                variant="stop",
            )
            status_text = gr.HTML(
                value=f"""<div style='text-align: center; padding: 15px; background-color: #4CAF50; color: white; border-radius: 8px; font-size: 16px;'>
                    <strong>{t('system.status_running_server')}</strong>
                </div>"""
            )

        return {
            'restart_btn': gr.Button(visible=False),
            'exit_btn': gr.Button(visible=False),
            'switch_to_server_btn': gr.Button(visible=False),
            'remote_switch_toggle': gr.Checkbox(visible=False),
            'remote_switch_status': gr.HTML(visible=False),
            'startup_toggle': gr.Checkbox(visible=False),
            'chrome_profile_dropdown': gr.Dropdown(visible=False),
            'language_dropdown': gr.Dropdown(visible=False),
            'disconnect_btn': disconnect_btn,
            'status_text': status_text,
        }

    # Server mode OFF: local mode — full controls
    with gr.Row():
        with gr.Column():
            # macOS: TCC permission warning (Mac 3-3). Only built on mac —
            # Windows page structure stays untouched. Callable value =
            # re-checked on every page load (F5 after granting in System
            # Settings shows the new state; restart still required for the
            # features themselves).
            from backend.shared.platform_caps import IS_MAC
            if IS_MAC:
                from backend.shared.mac_permissions import warn_permissions

                def _mac_perm_warning() -> str:
                    missing = warn_permissions()
                    if not missing:
                        return ''
                    names = ' / '.join(
                        t(f'system.mac_perm_{name}') for name in missing)
                    return t('system.mac_perm_warning', perms=names)

                gr.Markdown(_mac_perm_warning, elem_classes="helper-text")

            # Control-API bind failure warning (both OS). Normally the
            # backend self-heals a taken port (fallback + config
            # write-back); this only shows when port..port+10 were ALL
            # taken. Callable value = re-checked per page load.
            from backend.server.control_api import get_control_api_status

            def _control_api_warning() -> str:
                status = get_control_api_status()
                if status['requested'] is None or status['port'] is not None:
                    return ''
                from backend.shared.launch_config import LAUNCH_CONFIG_FILE
                return t('system.control_api_failed',
                         port=status['requested'],
                         config_path=str(LAUNCH_CONFIG_FILE))

            gr.Markdown(_control_api_warning, elem_classes="helper-text")

            gr.Markdown(f"### {t('system.app_controls')}")

            with gr.Row():
                restart_btn = gr.Button(
                    t('system.restart'),
                    variant="secondary",
                    scale=1
                )

                exit_btn = gr.Button(
                    t('system.exit'),
                    variant="stop",
                    scale=1
                )

            # フールプルーフ: Tailscale未インストールならサーバーモード系の
            # 入口をグレーアウト(理由はヘルパーテキストに前置)。インストール
            # 済みで未起動は従来どおり押下時エラーが案内(稜裁定 2026-07-25)。
            # 判定はビルド時と記憶タスク状態イベント時(check_extraction_status が
            # 復帰値に同じゲートを保持)に再評価。
            from backend.server.tailscale import is_tailscale_installed
            _ts_installed = is_tailscale_installed()
            switch_to_server_btn = gr.Button(
                t('system.server_mode'),
                variant="primary",
                elem_id="switch-to-server-btn",
                interactive=_ts_installed,
            )
            _server_mode_help = t('system.server_mode_help')
            if not _ts_installed:
                _server_mode_help = (
                    f"⚠️ {t('system.server_mode_needs_tailscale')}\n\n"
                    + _server_mode_help)
            gr.Markdown(
                _server_mode_help,
                elem_classes="helper-text",
            )

            # Remote mode-switch opt-in: while ON (local mode), a switch-only
            # HTTPS page on the Tailscale IP lets a remote device trigger the
            # switch to server mode (backend/server/remote_switch_listener.py).
            # Live config read on render — no mirror state.
            # 未インストール時はOFF→ONを封じる(ONで残っている場合はOFF操作を
            # 許すため interactive を維持=戻れない罠を作らない)。
            from backend.shared.launch_config import get_launch_config_value
            _remote_enabled_now = bool(get_launch_config_value(
                'server_mode', 'remote_switch_enabled', False))
            remote_switch_toggle = gr.Checkbox(
                label=t('system.remote_switch_toggle'),
                value=lambda: bool(get_launch_config_value(
                    'server_mode', 'remote_switch_enabled', False)),
                elem_id="remote-switch-toggle",
                interactive=_ts_installed or _remote_enabled_now,
            )
            # Tailscale未インストール警告はサーバーモードボタン側の1箇所のみ
            # (同一画面での二重表示を解消=稜指摘2026-07-30)。チェックボックスの
            # グレー化自体は interactive で維持。
            gr.Markdown(
                t('system.remote_switch_help'),
                elem_classes="helper-text",
            )
            # Live listener state (waiting for Tailscale / listening URL /
            # cert failure). JS専有: 書き手はWSの remote_switch_status のみ
            # (素のdiv=svelteが中身を再描画せずJSのDOM更新と競合しない・
            # stt-engine-status-text と同形)。初期値だけはページロード時に
            # サーバー側で描画(callable)=F5・再訪をカバー。outputsに載せない。
            from ui.handlers.remote_switch import remote_switch_status_html
            remote_switch_status = gr.HTML(
                remote_switch_status_html,
                elem_id="remote-switch-status",
            )

            # ST-D: Windows startup registration (live registry read on render)
            from launcher.startup_registry import is_startup_enabled
            startup_toggle = gr.Checkbox(
                label=t('system.startup_toggle'),
                value=is_startup_enabled,
                elem_id="startup-toggle",
            )
            gr.Markdown(
                t('system.startup_help'),
                elem_classes="helper-text",
            )

            # Chrome profile for the app window (Mac Phase 2, ruling F).
            # Listed by display name, saved by folder name. Hidden when
            # Local State is unreadable or only one profile exists (= no
            # meaningful choice, current behavior stays). ui->launcher
            # import direction is the startup_registry precedent.
            from launcher.chrome_profiles import list_chrome_profiles
            from ui.handlers.chrome_profile import current_chrome_profile
            chrome_profiles = list_chrome_profiles()
            chrome_profile_choices = (
                [(t('system.chrome_profile_unspecified'), '')]
                + [(p['display_name'], p['folder']) for p in chrome_profiles]
            )
            _valid_profile_values = frozenset(v for _, v in chrome_profile_choices)

            def _chrome_profile_value() -> str:
                # Saved folder may have been deleted in Chrome since —
                # fall back to unspecified (language_dropdown precedent).
                current = current_chrome_profile()
                return current if current in _valid_profile_values else ''

            chrome_profile_dropdown = gr.Dropdown(
                choices=chrome_profile_choices,
                value=_chrome_profile_value,  # callable = live read on page load
                label=t('system.chrome_profile_label'),
                elem_id="chrome-profile-dropdown",
                visible=bool(chrome_profiles),
            )
            gr.Markdown(
                t('system.chrome_profile_help'),
                elem_classes="helper-text",
                visible=bool(chrome_profiles),
            )

            gr.Markdown(f"### {t('system.language_title')}")
            # Dropdown choices come from the catalogs in locales/ (native
            # display names), plus 'auto' = follow the OS UI language.
            language_choices = [(t('system.language_auto'), 'auto')] + [
                (name, code) for code, name in available_languages()
            ]
            saved_language = get_setting('display', 'language', 'auto')
            if saved_language not in [code for _, code in language_choices]:
                saved_language = 'auto'
            language_dropdown = gr.Dropdown(
                choices=language_choices,
                value=saved_language,
                label=t('system.language_label'),
                elem_id="ui-language-dropdown",
            )
            gr.Markdown(
                t('system.language_restart_note'),
                elem_classes="helper-text",
            )

            gr.Markdown(t('system.warning'))

        with gr.Column():
            gr.Markdown(f"### {t('system.info_title')}")
            gr.Markdown(f"**{t('system.status_label')}**")
            status_text = gr.HTML(
                elem_id="system-status-text",
                value=f"""<div style='text-align: center; padding: 15px; background-color: #4CAF50; color: white; border-radius: 8px; font-size: 16px;'>
                    <strong>{t('system.status_running')}</strong>
                </div>"""
            )

    return {
        'restart_btn': restart_btn,
        'exit_btn': exit_btn,
        'switch_to_server_btn': switch_to_server_btn,
        'remote_switch_toggle': remote_switch_toggle,
        'remote_switch_status': remote_switch_status,
        'startup_toggle': startup_toggle,
        'chrome_profile_dropdown': chrome_profile_dropdown,
        'language_dropdown': language_dropdown,
        'disconnect_btn': gr.Button(visible=False),
        'status_text': status_text,
    }