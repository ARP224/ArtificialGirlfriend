"""
ui/mobile_app.py

Mobile Gradio app mounted at /mobile in server mode.
Provides a simplified text-only chat interface optimized for iPhone/iPad.
"""

import logging

import gradio as gr

from .state import app_state
from .components import CHAT_CSS_BODY, get_chat_history, create_log_panel, update_log_view, update_prompt_view
from .character_ui import refresh_char_list, switch_character
from .error_handler import error_status_text
from .conversation import (
    handle_text_input, start_text_generation, process_text_generation,
    toggle_start_end, update_tts_volume, test_tts_voice,
    wait_for_history_load,
)
from .conversation_functions import update_talk_theme_click, clear_talk_theme_click
from .local_fonts import LOCAL_FONTS_CSS
from .license_notice import license_notice_html
from .status_js import status_auto_hide_js
from .handlers.attachments import notify_attach_rejected as _notify_attach_rejected

import backend

from backend.shared.i18n import t

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Simple scroll JS (standalone, no import from app.py to avoid circular deps)
# ---------------------------------------------------------------------------
MOBILE_SCROLL_JS = """
() => {
    const chatDisplay = document.getElementById('chat-display');
    if (chatDisplay) {
        setTimeout(() => {
            const max = chatDisplay.scrollHeight - chatDisplay.clientHeight;
            chatDisplay.scrollTop = max > 0 ? max - 1 : 0;
        }, 100);
    }
}
"""

# テキスト送信ステータス: モバイルは既定CSSで非表示(成功/進行系は出さない)。
# エラー(❌始まり)のときだけチェーン末尾JSが約5秒間だけ表示する
# (稜裁定 2026-08-02: モバイルのエラー表示=この欄+システムログページ)。
# 「エラー時のみ表示」という別挙動のため ui/status_js.py とは共通化せず
# インライン定義のまま(単純な自動消去は status_js の import で足りる)。
MOBILE_STATUS_RESET_JS = """
() => {
    const el = document.getElementById('mobile-text-status');
    if (!el) return;
    if (window._statusHideTimers) clearTimeout(window._statusHideTimers['mobile-text-status']);
    el.style.removeProperty('display');  // 既定CSS(display:none)に戻す
}
"""

MOBILE_STATUS_ERROR_SHOW_JS = """
() => {
    // svelteのMarkdown反映を待ってから内容を判定する(SCROLL_JSと同じ100ms)
    setTimeout(() => {
        const el = document.getElementById('mobile-text-status');
        if (!el) return;
        window._statusHideTimers = window._statusHideTimers || {};
        clearTimeout(window._statusHideTimers['mobile-text-status']);
        // ❌はUI側が生成する表示マーカー(error_status_text / gen.error)。
        // 判定はこの表示層のみ=エラー分類ロジックには使わない
        const isError = (el.textContent || '').trim().startsWith('❌');
        if (!isError) { el.style.removeProperty('display'); return; }
        el.style.display = 'block';
        window._statusHideTimers['mobile-text-status'] = setTimeout(() => {
            el.style.removeProperty('display');
        }, 5000);
    }, 100);
}
"""


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
def get_mobile_css() -> str:
    """Generate CSS for mobile-optimized layout."""
    return """
    /* === Mobile global resets === */
    .gradio-container { padding: 0 !important; max-width: 100% !important; }
    .main { padding: 0 !important; }

    /* === Header: bypass Gradio Row flex entirely, use position:fixed === */
    /* Collapse the Row — children are all position:fixed */
    #mobile-header-row {
        height: 0 !important; min-height: 0 !important;
        padding: 0 !important; margin: 0 !important;
        overflow: visible !important;
        background: transparent !important;
        border: none !important;
        gap: 0 !important;
    }
    #mobile-header-row > div { overflow: visible !important; }
    /* Header background bar (pseudo-element) */
    #mobile-header-row::before {
        content: '';
        position: fixed;
        top: 0; left: 0; right: 0;
        height: 52px;
        background: #1a1a2e;
        border-bottom: 1px solid #333;
        z-index: 999;
        pointer-events: none;
    }

    /* Hamburger button — fixed top-left */
    #mobile-hamburger-btn {
        position: fixed !important;
        top: 4px !important; left: 8px !important;
        z-index: 1001 !important;
        min-width: 44px !important; max-width: 44px !important;
        min-height: 44px !important; max-height: 44px !important;
        font-size: 22px !important;
        padding: 0 !important; border-radius: 8px !important;
        background: transparent !important; border: none !important;
        color: #fff !important;
    }

    /* Connection status — fixed top-right */
    #mobile-connection-status {
        position: fixed !important;
        top: 4px !important; right: 8px !important;
        z-index: 1001 !important;
        width: 36px !important; height: 44px !important;
        display: flex !important; align-items: center !important; justify-content: center !important;
    }
    #mobile-connection-status p { margin: 0 !important; font-size: 18px !important; }

    /* Character dropdown — fixed, fills space between ☰ and 🔴 */
    #mobile-char-select-container {
        position: fixed !important;
        top: 8px !important;
        left: 58px !important;   /* 8 + 44 + 6 */
        right: 50px !important;  /* 8 + 36 + 6 */
        height: 36px !important;
        z-index: 1001 !important;
        padding: 0 !important;
        width: auto !important;
        overflow: hidden !important;
    }
    #mobile-char-select-container * { margin: 0 !important; padding: 0 !important; }
    #mobile-char-select-container .prose { height: 100% !important; }
    #mobile-char-select {
        width: 100%;
        height: 36px !important;
        font-size: 14px !important;
        padding: 0 30px 0 10px !important;
        border: 1px solid #444;
        border-radius: 6px;
        background: #1e1e35;
        color: #fff;
        text-align: center;
        text-align-last: center;  /* Centers selected text in <select> */
        -webkit-appearance: none;
        appearance: none;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%23aaa' d='M6 8L0 0h12z'/%3E%3C/svg%3E");
        background-repeat: no-repeat;
        background-position: right 8px center;
        box-sizing: border-box;
    }
    #mobile-char-select:focus {
        outline: none;
        border-color: #4a90d9;
    }

    /* Scrollable chat area (below header, above input) */
    #chat-display {
        position: fixed !important;
        top: 56px !important;
        bottom: 64px !important;  /* fallback */
        bottom: calc(64px + env(safe-area-inset-bottom, 0px)) !important;
        left: 0 !important; right: 0 !important;
        overflow-y: auto !important;
        -webkit-overflow-scrolling: touch;
        padding: 12px !important;
        background: #0f0f1a !important;
        height: auto !important;
        max-height: none !important;
    }

    /* Fixed bottom input dock: 入力行+添付ステータスを縦積みの通常フローで
       持つ固定コンテナ(コンパニオンバーと同型)。座標指定の位置合わせをしない */
    #mobile-input-dock {
        position: fixed;
        bottom: 0; left: 0; right: 0;
        z-index: 1000;
        background: #1a1a2e;
        padding: 8px 8px calc(8px + env(safe-area-inset-bottom));
        border-top: 1px solid #333;
        gap: 0 !important;
    }
    #mobile-input-row { padding: 0 !important; }
    #mobile-input-row .wrap { gap: 6px !important; }

    /* Input bar buttons */
    #mobile-attach-btn {
        min-width: 44px !important; max-width: 44px !important;
        min-height: 44px !important;
        padding: 0 !important; font-size: 20px !important;
    }
    /* Real UploadButton stays hidden — the visible 📎 opens the attach menu,
       whose "attach file" item clicks this programmatically */
    #mobile-attach-upload { display: none !important; }

    /* 📎 action menu (file attach / location send) — anchored above the input bar */
    #mobile-attach-menu {
        display: none;
        position: fixed;
        left: 8px;
        bottom: calc(64px + env(safe-area-inset-bottom, 0px));
        z-index: 1002;
        background: #1e1e35;
        border: 1px solid #444;
        border-radius: 10px;
        overflow: hidden;
        box-shadow: 0 4px 16px rgba(0,0,0,0.5);
    }
    #mobile-attach-menu.open { display: block; }
    .mobile-attach-menu-item {
        display: block;
        width: 100%;
        padding: 12px 18px;
        background: transparent;
        border: none;
        color: #e0e0e0;
        font-size: 15px;
        text-align: left;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
    }
    .mobile-attach-menu-item:active { background: #2a2a4a; }
    .mobile-attach-menu-item + .mobile-attach-menu-item { border-top: 1px solid #333; }
    #mobile-send-btn {
        min-width: 44px !important; max-width: 44px !important;
        min-height: 44px !important;
        padding: 0 !important; font-size: 22px !important;
        background: #4a90d9 !important;
        color: #fff !important;
        border: none !important;
        border-radius: 50% !important;
    }
    /* Native textarea for text input (iOS compatible) */
    #mobile-native-input {
        flex: 1;
        width: 100%;
        height: 38px;
        min-height: 38px;
        max-height: 100px;
        font-size: 16px !important;  /* Prevent iOS zoom */
        padding: 8px 12px;
        border: 1px solid #444;
        border-radius: 6px;
        background: #1a1a2e;
        color: #e0e0e0;
        resize: none;
        outline: none;
        font-family: inherit;
        line-height: 1.4;
        -webkit-appearance: none;
        appearance: none;
        box-sizing: border-box;
        overflow: hidden;
    }
    #mobile-native-input:focus {
        border-color: #4a90d9;
    }
    /* 会話未開始/生成中ロックの視覚フィードバック */
    #mobile-native-input:disabled { opacity: 0.55; }
    #mobile-send-btn:disabled { opacity: 0.45 !important; }
    #mobile-native-input::placeholder {
        color: #666;
    }
    #mobile-text-input-container { flex: 1 !important; padding: 0 !important; }
    #mobile-text-input-container .prose { margin: 0 !important; padding: 0 !important; }

    /* Hidden textbox for text input JS→Python bridge */
    #mobile-text-hidden {
        position: absolute !important;
        width: 0 !important; height: 0 !important;
        overflow: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
    }

    /* Attach status — JS専有の素div。dock内の通常フローで入力行の下に出る */
    #mobile-attach-status {
        margin: 4px 4px 0 !important;
        font-size: 12px !important;
        color: #888 !important;
        text-align: center;
    }
    #mobile-attach-status:empty { display: none !important; }
    #mobile-attach-status-wrap { padding: 0 !important; margin: 0 !important; min-height: 0 !important; }
    #mobile-attach-status-wrap .prose { margin: 0 !important; padding: 0 !important; }
    /* 空のときはGradioラッパーごと畳む(素のblockが約20pxの高さを持つため) */
    #mobile-attach-status-wrap:has(#mobile-attach-status:empty) { display: none !important; }
    /* Text status: 成功/進行系はモバイルでは出さない(チャット側で生成状態が
       分かる)が、エラー(❌)だけはチェーン末尾のJSが一時表示する
       (稜裁定 2026-08-02: モバイルのエラー表示はこの欄+システムログ)。
       表示位置は showNotification と同じ入力ドック直上に固定
       (稜裁定 2026-08-03: 通常フローだとチャット領域の中に浮いて出る)。
       display は JS の inline style が制御するため !important 禁止 */
    #mobile-text-status {
        display: none;
        position: fixed !important;
        bottom: 120px; left: 10px; right: 10px;
        z-index: 9999;
        background: #4a1f1f;
        border-left: 4px solid #f44336;
        border-radius: 8px;
        padding: 10px 14px;
        text-align: center;
    }
    #mobile-text-status .prose, #mobile-text-status p { margin: 0 !important; }

    /* === Hamburger menu === */
    #mobile-hamburger-menu {
        position: fixed !important;
        top: 0; left: 0; bottom: 0;
        width: 280px !important;
        max-width: 80vw !important;
        z-index: 2000;
        background: #16213e !important;
        transform: translateX(-100%);
        transition: transform 0.3s ease;
        overflow-y: auto;
        padding: 16px !important;
        border-right: 1px solid #444;
    }
    #mobile-hamburger-menu.open {
        transform: translateX(0);
    }
    /* Fix Gradio inner containers clipping menu content.
       Safe because all visible form inputs in the menu are native HTML
       (no gr.Textbox visible — theme_hidden is CSS-hidden). */
    #mobile-hamburger-menu > div,
    #mobile-hamburger-menu .form,
    #mobile-hamburger-menu .block,
    #mobile-hamburger-menu .contain {
        overflow: visible !important;
        max-height: none !important;
    }

    /* Menu backdrop */
    #mobile-menu-backdrop {
        position: fixed; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(0,0,0,0.5);
        z-index: 1999;
        display: none;
    }
    #mobile-menu-backdrop.open { display: block; }

    /* Navigation buttons in menu */
    .mobile-nav-btn {
        width: 100% !important;
        text-align: left !important;
        padding: 12px 16px !important;
        min-height: 44px !important;
        border: none !important;
        border-radius: 8px !important;
        background: transparent !important;
        color: #ccc !important;
        font-size: 16px !important;
        margin-bottom: 4px !important;
    }
    .mobile-nav-btn.active {
        background: #1a3a5c !important;
        color: #fff !important;
    }

    /* Start/End button in menu */
    #mobile-start-end-btn {
        width: 100% !important;
        min-height: 44px !important;
        margin: 12px 0 !important;
        font-size: 16px !important;
    }

    /* Utility panel (feature toggle buttons) — 2列グリッドでデスクトップの
       サイドバーと同じ整列にする (稜指示 2026-07-19: バラバラ配置を整える) */
    #mobile-utility-panel {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px;
        margin: 12px 0;
    }
    #mobile-utility-panel button {
        width: 100%;
        box-sizing: border-box;
        padding: 8px 4px;
        margin: 0;
        border: 1px solid #555;
        border-radius: 6px;
        background: #607d8b;
        color: #fff;
        font-size: 13px;
        cursor: pointer;
        min-height: 36px;
    }

    /* Theme section in menu — native textarea */
    #mobile-theme-textarea {
        width: 100%;
        min-height: 44px;
        font-size: 14px !important;
        padding: 8px 10px;
        border: 1px solid #444;
        border-radius: 6px;
        background: #1e1e35;
        color: #e0e0e0;
        resize: none;
        outline: none;
        font-family: inherit;
        line-height: 1.4;
        box-sizing: border-box;
        -webkit-appearance: none;
    }
    #mobile-theme-textarea:focus { border-color: #4a90d9; }
    #mobile-theme-textarea::placeholder { color: #666; }
    #mobile-theme-textarea-container { padding: 0 !important; margin: 4px 0 8px !important; }
    #mobile-theme-textarea-container .prose { margin: 0 !important; padding: 0 !important; }
    /* Hidden bridge for theme input */
    #mobile-theme-hidden {
        position: absolute !important;
        width: 0 !important; height: 0 !important;
        overflow: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
    }
    /* Current theme display box */
    #mobile-current-theme {
        background: #1e1e35;
        border: 1px solid #444;
        border-radius: 6px;
        padding: 8px 10px;
        margin: 4px 0 8px;
        min-height: 32px;
        color: #ccc;
        font-size: 14px;
    }
    #mobile-current-theme p { margin: 0 !important; }
    .mobile-label p {
        margin: 2px 0 !important;
        font-size: 13px !important;
        color: #8ab4f8 !important;
    }

    /* Pages */
    .mobile-page { padding-top: 56px; padding-bottom: 120px; }
    #mobile-page-logs, #mobile-page-system {
        position: fixed;
        top: 56px; bottom: 0; left: 0; right: 0;
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
        padding: 12px;
        background: #0f0f1a;
    }
    /* Force Gradio inner containers to not clip page content */
    #mobile-page-system > div,
    #mobile-page-system .form,
    #mobile-page-system .block,
    #mobile-page-system .contain,
    #mobile-page-logs > div,
    #mobile-page-logs .form,
    #mobile-page-logs .block,
    #mobile-page-logs .contain {
        overflow: visible !important;
        max-height: none !important;
    }

    /* System page controls */
    .mobile-system-section {
        padding: 12px 0;
        border-bottom: 1px solid #333;
    }
    .mobile-system-section h3 { margin: 0 0 8px; font-size: 16px; }

    /* Mobile chat: icon above bubble so text gets full width */
    .ai-message {
        flex-direction: column !important;
        align-items: flex-start !important;
        margin-right: 8px !important;
    }
    .char-icon {
        width: 32px !important;
        height: 32px !important;
        margin-right: 0 !important;
        margin-bottom: 0 !important;
    }
    /* Character name displayed next to icon */
    .ai-icon-row {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-right: 0 !important;
        margin-bottom: 4px;
    }
    .char-name {
        display: inline !important;
        font-size: 13px;
        color: #8ab4f8;
        font-weight: 500;
    }
    .ai-bubble {
        max-width: 100% !important;
        background-color: #082c41 !important;
        color: #e0e0e0 !important;
    }
    .message-content {
        width: 100% !important;
    }
    /* User messages: reduce left margin for more width */
    .user-message {
        margin-left: 10% !important;
    }
    .user-bubble {
        max-width: 95% !important;
        background-color: #305115 !important;
        color: #e0e0e0 !important;
    }
    /* Timestamp: adjust for mobile layout */
    .user-message .message-timestamp {
        padding-right: 16px !important;
    }
    /* Generating spinner row: icon above */
    .generating-message {
        flex-direction: column !important;
        align-items: flex-start !important;
        margin-right: 8px !important;
    }
    /* Speaking indicator: no left offset needed */
    .speaking-indicator {
        margin-left: 0 !important;
    }

    /* Hide Gradio mobile queue warning toast (iPhone only, not relevant for our WS-based UI) */
    .toast-wrap { display: none !important; }

    /* Hide Gradio footer ("Built with Gradio", "APIを介して使用", settings) */
    footer,
    .gradio-container footer,
    .built-with,
    .settings-toggle,
    button.settings,
    a[href*="api"],
    .api-link,
    .show-api { display: none !important; }


    /* Touch targets */
    button { min-height: 44px; }
    input, select, textarea { font-size: 16px !important; }

    /* Hidden triggers */
    #mobile-hidden-triggers { display: none !important; }

    /* Hidden textbox for JS→Python bridge (CSS hidden, NOT visible=False) */
    #mobile-char-hidden {
        position: absolute !important;
        width: 0 !important; height: 0 !important;
        overflow: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
    }

    /* --- Companion Mode --- */

    /* Companion mode = conversation page only. Companion is subordinate to
       the PC UI — no conversation start/end or management from companion
       (稜裁定 2026-07-16). Hide navigation and the standalone input bar;
       the companion camera bar (text row + attach buttons) is the input surface.
       NOTE: Gradio 5 prefix_css emits every css= rule twice (verbatim +
       ".gradio-container .contain"-scoped copy), so these body.companion-mode
       rules DO apply — they are not dead.
       (#mobile-text-hidden excluded: its own CSS hides it, but Svelte binding must stay active) */
    body.companion-mode #mobile-hamburger-btn,
    body.companion-mode #mobile-hamburger-menu,
    body.companion-mode #mobile-menu-backdrop,
    body.companion-mode #mobile-input-dock,
    body.companion-mode #mobile-text-status {
        display: none !important;
    }

    /* Disable character dropdown in companion mode
       (JS also sets disabled attribute for iOS Safari) */
    body.companion-mode #mobile-char-select-container {
        pointer-events: none !important;
        opacity: 0.6;
    }
    body.companion-mode #mobile-char-select {
        pointer-events: none !important;
        -webkit-appearance: none;
        opacity: 0.6;
    }

    /* Companion camera bar */
    #companion-camera-bar {
        display: none;
        position: fixed;
        bottom: 0; left: 0; right: 0;
        z-index: 1000;
        background: #1a1a2e;
        padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0px));
        border-top: 1px solid #333;
        text-align: center;
    }
    body.companion-mode #companion-camera-bar {
        display: block !important;
    }
    .companion-btn-row {
        display: flex;
        justify-content: center;
        gap: 24px;
    }
    .companion-btn {
        width: 56px; height: 56px;
        border-radius: 50%;
        border: 2px solid #555;
        background: #2a2a4a;
        font-size: 24px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #fff;
        -webkit-tap-highlight-color: transparent;
    }
    .companion-btn:active {
        background: #4a4a6a;
        transform: scale(0.95);
    }
    #companion-attach-status {
        margin-top: 8px;
        font-size: 13px;
        color: #aaa;
        min-height: 18px;
    }

    /* Companion text input row */
    #companion-text-row {
        display: flex;
        gap: 8px;
        margin-bottom: 8px;
    }
    #companion-text-input {
        flex: 1;
        height: 38px;
        min-height: 38px;
        max-height: 100px;
        font-size: 16px !important;
        padding: 8px 12px;
        border: 1px solid #444;
        border-radius: 6px;
        background: #1a1a2e;
        color: #e0e0e0;
        resize: none;
        outline: none;
        font-family: inherit;
        line-height: 1.4;
        -webkit-appearance: none;
        appearance: none;
        box-sizing: border-box;
        overflow: hidden;
    }
    #companion-text-input:focus {
        border-color: #4a90d9;
    }
    #companion-text-input::placeholder {
        color: #666;
    }
    #companion-text-input:disabled {
        background: #111;
        color: #555;
        cursor: not-allowed;
    }
    #companion-send-btn {
        width: 44px;
        height: 38px;
        border-radius: 50%;
        border: none;
        background: #4a90d9;
        color: #fff;
        font-size: 20px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        -webkit-tap-highlight-color: transparent;
    }
    #companion-send-btn:active {
        background: #3a7ac9;
        transform: scale(0.95);
    }
    #companion-send-btn:disabled {
        background: #555;
        color: #888;
        cursor: not-allowed;
    }

    /* Companion mode chat-display layout is set via JS inline styles
       (CSS rules are broken by Gradio's selector scoping — see companion init in WS handler) */

    """


# ---------------------------------------------------------------------------
# WebSocket JS
# ---------------------------------------------------------------------------
def create_mobile_ws_js() -> str:
    """Generate JavaScript for mobile WebSocket client.

    Standalone definition (same pattern as admin_app.py).
    Does NOT import from app.py to avoid circular imports and allow
    mobile-specific customisation.
    """
    js = """
    async () => {
        if (window.wsManager) return;  // 値を返さない(0ad9bee と同じ理由)

        window.isMobileUI = true;

        // WebSocket URL: /ws on same host
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        const wsUrl = protocol + '//' + host + '/ws';

        // ---- WS Manager (Phase 2C: backoff reconnect + send queue + overlay,
        //                  Phase 3D: session_token + last_seq for resume) ----
        window.wsManager = {
            ws: null,
            sendQueue: [],
            reconnectAttempt: 0,
            reconnectStartedAt: null,
            reconnectTimer: null,
            backoffSchedule: [1000, 2000, 4000, 8000, 16000],
            giveUpAfterMs: 5 * 60 * 1000,
            queueMaxSize: 50,
            closeIntentional: false,
            // Set when the server closed us with a no-reconnect code (4001-4003).
            // Blocks the pageshow/visibilitychange auto-reconnect below — an
            // admin-kicked or timed-out page must stay dead until manual reload.
            noReconnect: false,
            // Phase 3D state — JS memory only (refresh = new session, by design)
            sessionToken: null,
            lastSeq: 0,

            connect: function() {
                this.closeIntentional = false;
                console.log('[WS] Connecting to', wsUrl);
                try {
                    this.ws = new WebSocket(wsUrl);

                    this.ws.onopen = () => {
                        console.log('[WS] Connected');
                        this.reconnectAttempt = 0;
                        this.reconnectStartedAt = null;
                        clearTimeout(this.reconnectTimer);
                        this.hideOverlay();
                        // identify (NOT queued — must arrive within server's 10s timeout).
                        // Phase 3D: include sessionToken + lastSeq so server can resume.
                        this.ws.send(JSON.stringify({
                            type: 'identify',
                            client_type: 'mobile',
                            session_token: this.sessionToken,
                            last_seq: this.sessionToken ? this.lastSeq : null
                        }));
                        console.log('[WS] Identify sent: mobile, token=' +
                                    (this.sessionToken ? this.sessionToken.substring(0,8) : 'null') +
                                    ', last_seq=' + (this.sessionToken ? this.lastSeq : 'null'));
                        this.flushQueue();
                    };

                    this.ws.onmessage = (event) => {
                        try {
                            const data = JSON.parse(event.data);

                            // Server liveness probe — ack immediately. Event-driven
                            // (not a timer), so this works even in throttled
                            // background tabs. No pong for LIVENESS_TIMEOUT (45s)
                            // → the server treats the session as disconnected.
                            if (data.action === 'ping') {
                                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                                    try {
                                        this.ws.send(JSON.stringify({
                                            action: 'pong', timestamp: Date.now()
                                        }));
                                    } catch (e) {}
                                }
                                return;
                            }

                            // Phase 3D: monotonic seq tracking — see ui/app.py for rationale.
                            if (typeof data.seq === 'number' && data.seq > this.lastSeq) {
                                if (this.lastSeq && data.seq > this.lastSeq + 1) {
                                    console.warn('[WS] Seq gap: expected ' + (this.lastSeq + 1) +
                                                 ', got ' + data.seq);
                                }
                                this.lastSeq = data.seq;
                            }

                            // Phase 3D: persist server-issued session_token; reset lastSeq on
                            // new token (Case 4 grant) so the new session's seq=1 is accepted.
                            if (data.type === 'identify_response' && data.session_token) {
                                if (data.session_token !== this.sessionToken) {
                                    // Reconnected as a NEW session (old one was
                                    // released, e.g. suspend > grace window):
                                    // messages missed while away never arrive via
                                    // replay — refetch the whole chat display.
                                    // Skipped on first connect (no old token).
                                    if (this.sessionToken) {
                                        const rbtn = document.querySelector('#ws-update-trigger');
                                        if (rbtn) {
                                            rbtn.style.display = 'block';
                                            rbtn.click();
                                            setTimeout(() => { rbtn.style.display = 'none'; }, 10);
                                        }
                                    }
                                    this.lastSeq = 0;
                                    console.log('[WS] New session_token, lastSeq reset:',
                                                data.session_token.substring(0,8));
                                } else {
                                    console.log('[WS] session_token preserved (resume):',
                                                data.session_token.substring(0,8));
                                }
                                this.sessionToken = data.session_token;
                            }

                            if (data.type === 'connected') {
                                console.log('[WS] Server acknowledged');
                                // フールプルーフ: 機能→ブロック理由(null=利用可)
                                if (data.feature_availability) {
                                    window.featureAvailability = data.feature_availability;
                                }
                                if (data.feature_status) {
                                    window.talkThemeEnabled = data.feature_status.talk_theme_enabled !== false;
                                    window.speechlessEnabled = data.feature_status.speechless_enabled || false;
                                    window.notesEnabled = data.feature_status.notes_enabled === true;
                                    window.imageGenerationEnabled = data.feature_status.image_generation_enabled || false;
                                    window.deepSearchEnabled = data.feature_status.deep_search_enabled || false;
                                    window.elythEnabled = data.feature_status.elyth_enabled || false;
                                    window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
                                }
                            } else if (data.type === 'identify_response') {
                                console.log('[WS] Identify response:', data.status, 'mode:', data.mode);
                                if (data.status === 'blocked') {
                                    window._showSessionBlockedOverlay();
                                    window.wsManager.disconnect();
                                } else if (data.mode === 'companion') {
                                    window.isCompanionMode = true;
                                    document.body.classList.add('companion-mode');
                                    window._updateImageSlotStatus(data.image_count || 0, 5);
                                    // Disable native select (iOS ignores pointer-events:none)
                                    const sel = document.getElementById('mobile-char-select');
                                    if (sel) sel.disabled = true;
                                    // Set active character from server
                                    if (data.character_id) {
                                        window._setCompanionCharacter(data.character_id, data.character_name);
                                    }
                                    // Set connection status lamp and _connectionStarted
                                    window._setConnectionLamp(data.conversation_started);
                                    // Set initial generating state
                                    if (data.is_generating) {
                                        window._isGenerating = true;
                                        window._setCompanionGeneratingState(true);
                                    }
                                    // Force show companion camera bar via JS
                                    const bar = document.getElementById('companion-camera-bar');
                                    if (bar) bar.style.display = 'block';
                                    // Fix chat-display layout for companion mode via <style> injection.
                                    // A "body.companion-mode #chat-display" rule in get_mobile_css() cannot win:
                                    // the base "#chat-display" rule uses !important and Gradio 5's prefix_css
                                    // emits a ".gradio-container .contain"-scoped copy of it whose specificity
                                    // (1 id, 3 classes) beats body.companion-mode #chat-display (1 id, 1 class).
                                    // Injected <style> tags are NOT scoped, and #id#id (2 ids) + !important
                                    // beats the scoped copy. Survives DOM re-renders.
                                    const companionStyle = document.createElement('style');
                                    companionStyle.textContent = '#chat-display#chat-display { bottom: auto !important; height: calc(100vh - 56px - 160px - env(safe-area-inset-bottom, 0px)) !important; }';
                                    document.head.appendChild(companionStyle);
                                    console.log('[WS] Companion mode activated');
                                }
                                // 全モード共通: 会話状態で入力欄プレースホルダーを初期化
                                if (data.status === 'accepted' && typeof data.conversation_started === 'boolean') {
                                    if (!window.isCompanionMode) {
                                        window._connectionStarted = data.conversation_started;
                                    }
                                    window._setMobileInputPlaceholder(data.conversation_started);
                                }
                            } else if (data.type === 'force_disconnected') {
                                // Phase 2C: superseded by close code; legacy JSON now no-op
                                console.log('[WS] (legacy) force_disconnected JSON received, ignored');

                            } else if (data.action === 'feature_toggle_response') {
                                // フールプルーフ層2の拒否: 理由を通知(状態代入が
                                // 楽観フリップ済みのJS状態を正へ巻き戻す)
                                if (data.success === false && data.popup_message &&
                                    typeof window.showNotification === 'function') {
                                    window.showNotification(data.popup_title || '', data.popup_message, 'warning', 5000);
                                }
                                window.talkThemeEnabled = data.talk_theme_enabled !== false;
                                window.speechlessEnabled = data.speechless_enabled || false;
                                window.notesEnabled = data.notes_enabled === true;
                                window.imageGenerationEnabled = data.image_generation_enabled || false;
                                window.deepSearchEnabled = data.deep_search_enabled || false;
                                window.elythEnabled = data.elyth_enabled || false;
                                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();

                            } else if (data.action === 'feature_availability') {
                                // キャラ切替/APIキー保存後の可用性再配信。
                                // feature_status同梱=サーバー側の強制OFFを反映
                                const fa = data.data || {};
                                window.featureAvailability = fa.reasons || {};
                                if (fa.feature_status) {
                                    window.talkThemeEnabled = fa.feature_status.talk_theme_enabled !== false;
                                    window.speechlessEnabled = fa.feature_status.speechless_enabled || false;
                                    window.notesEnabled = fa.feature_status.notes_enabled === true;
                                    window.imageGenerationEnabled = fa.feature_status.image_generation_enabled || false;
                                    window.deepSearchEnabled = fa.feature_status.deep_search_enabled || false;
                                    window.elythEnabled = fa.feature_status.elyth_enabled || false;
                                }
                                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();

                            } else if (data.action === 'update_chat') {
                                const btn = document.querySelector('#ws-update-trigger');
                                if (btn) { btn.style.display='block'; btn.click(); setTimeout(()=>{btn.style.display='none';},10); }

                            } else if (data.action === 'update_status') {
                                const btn = document.querySelector('#ws-status-update-trigger');
                                if (btn) { btn.style.display='block'; btn.click(); setTimeout(()=>{btn.style.display='none';},10); }

                            } else if (data.action === 'tts_audio') {
                                if (window.isCompanionMode) {
                                    console.log('[WS] TTS Audio skipped (companion mode)');
                                } else {
                                console.log('[WS] TTS Audio received');
                                // iOS Safari: <audio>.play() fails from WebSocket callbacks
                                // (not a user gesture). Use Web Audio API instead —
                                // AudioContext was unlocked on Start Connection click.
                                try {
                                    const audioData = data.data;
                                    const binaryString = atob(audioData.audio_base64);
                                    const bytes = new Uint8Array(binaryString.length);
                                    for (let i = 0; i < binaryString.length; i++) {
                                        bytes[i] = binaryString.charCodeAt(i);
                                    }
                                    const ctx = window.ttsAudioContext;
                                    if (ctx && ctx.state !== 'closed') {
                                        if (ctx.state === 'suspended') {
                                            ctx.resume().catch(() => {});
                                        }
                                        // decodeAudioData detaches the buffer, so pass a copy
                                        const abCopy = bytes.buffer.slice(
                                            bytes.byteOffset,
                                            bytes.byteOffset + bytes.byteLength
                                        );
                                        // Use callback form for max Safari compatibility
                                        ctx.decodeAudioData(abCopy,
                                            function(audioBuffer) {
                                                // Stop previous source if still playing
                                                if (window._ttsCurrentSource) {
                                                    try { window._ttsCurrentSource.stop(); } catch(e) {}
                                                }
                                                const source = ctx.createBufferSource();
                                                source.buffer = audioBuffer;
                                                const gain = ctx.createGain();
                                                gain.gain.value = audioData.volume || 1.0;
                                                source.connect(gain);
                                                gain.connect(ctx.destination);
                                                window._ttsCurrentSource = source;
                                                source.onended = function() {
                                                    window._ttsCurrentSource = null;
                                                    console.log('[WS] TTS playback completed');
                                                    // Phase 2.5: notify server (companion mode is skipped above)
                                                    if (audioData.playback_id && window.wsManager) {
                                                        window.wsManager.enqueueSend(JSON.stringify({
                                                            action: 'tts_playback_completed',
                                                            playback_id: audioData.playback_id
                                                        }));
                                                    }
                                                };
                                                source.start(0);
                                                console.log('[WS] TTS playing via Web Audio API, vol:', audioData.volume);
                                            },
                                            function(err) {
                                                console.error('[WS] TTS decodeAudioData failed:', err);
                                            }
                                        );
                                    } else {
                                        console.warn('[WS] No AudioContext — press Start Connection first');
                                    }
                                } catch(e) { console.error('[WS] TTS processing error:', e); }
                                }

                            } else if (data.action === 'is_generating_update') {
                                window._isGenerating = data.data.is_generating;
                                window._setCompanionGeneratingState(data.data.is_generating);
                                // 会話Start/Endトグルも生成中は無効化(デスクトップの
                                // #conversation-toggleと同仕様・稜裁定 2026-08-21)。
                                // サーバ側ガード(toggle_start_end)が本丸でここは見た目。
                                const endBtnWrap = document.querySelector('#mobile-start-end-btn');
                                if (endBtnWrap) {
                                    const endBtn = endBtnWrap.tagName === 'BUTTON' ? endBtnWrap : endBtnWrap.querySelector('button');
                                    if (endBtn) endBtn.disabled = !!data.data.is_generating;
                                }
                                console.log('[WS] Generating state:', data.data.is_generating);

                            } else if (data.action === 'auto_prompt_chat_update') {
                                // モバイルは自動プロンプトを持たない(強制オフ)。
                                // デスクトップ側が生成した場合のチャット表示同期のみ行う。
                                console.log('[WS] Auto Prompt chat update (display sync only), stage:', data.data?.stage);
                                const btn = document.querySelector('#ws-update-trigger');
                                if (btn) { btn.style.display='block'; btn.click(); setTimeout(()=>{btn.style.display='none';},10); }

                            } else if (data.action === 'command_pre_response') {
                                // Replace "Generating response..." spinner with AI text
                                const preData = data.data || {};
                                const preText = preData.text || '';
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer && preText) {
                                    const escaped = preText.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const formattedText = escaped.replace(/\\n/g, '<br>');
                                    const spinner = chatContainer.querySelector('.generating-message');
                                    if (spinner) {
                                        const bubble = spinner.querySelector('.ai-bubble');
                                        if (bubble) {
                                            bubble.innerHTML = formattedText;
                                        }
                                        spinner.classList.remove('generating-message');
                                    } else {
                                        // Fallback: no spinner to replace — create new AI message div
                                        const iconEl = chatContainer.querySelector('.ai-message .char-icon');
                                        const iconTag = iconEl ? `<img src="${iconEl.src}" class="char-icon" />` : '';
                                        const aiDiv = document.createElement('div');
                                        aiDiv.className = 'ai-message';
                                        aiDiv.innerHTML = `
                                            <div class="ai-icon-row">${iconTag}</div>
                                            <div class="message-content">
                                                <div class="ai-bubble">${formattedText}</div>
                                            </div>`;
                                        chatContainer.appendChild(aiDiv);
                                    }
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'command_loop_spinner') {
                                // Show "Generating response..." spinner for next tool-call loop iteration
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const iconEl = chatContainer.querySelector('.ai-message .char-icon');
                                    const iconTag = iconEl ? `<img src="${iconEl.src}" class="char-icon" />` : '';
                                    const spinnerDiv = document.createElement('div');
                                    spinnerDiv.className = 'ai-message generating-message';
                                    spinnerDiv.innerHTML = `
                                        <div class="ai-icon-row">${iconTag}</div>
                                        <div class="message-content">
                                            <div class="ai-bubble">
                                                <div class="loading-spinner"></div> Generating response...
                                            </div>
                                        </div>`;
                                    chatContainer.appendChild(spinnerDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'talk_theme_block') {
                                // Add a talk theme change block to chat
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const bd = data.data || {};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const action = bd.action || '';
                                    const theme = esc(bd.theme || '');
                                    const themeDiv = document.createElement('div');
                                    themeDiv.className = 'talk-theme-msg';
                                    if (action === 'set_talk_theme') {
                                        themeDiv.innerHTML = `<div class="theme-header">Talk Theme Set</div><div>${theme}</div>`;
                                    } else {
                                        themeDiv.innerHTML = `<div class="theme-header">Talk Theme Cleared</div>`;
                                    }
                                    chatContainer.appendChild(themeDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'talk_theme_updated') {
                                // Phase 4B: snapshot s90. Replaces the polling-
                                // based theme refresh. Mobile uses gr.Markdown
                                // (#mobile-current-theme) — update via textContent
                                // for safety, since AI/user-supplied theme text
                                // may contain HTML chars.
                                const el = document.querySelector('#mobile-current-theme');
                                if (el) {
                                    const theme = data.theme || '';
                                    // Markdown component renders inner content;
                                    // replace its text content directly.
                                    el.textContent = theme || '(none)';
                                }

                            } else if (data.action === 'error_notification') {
                                // 仕様(稜裁定 2026-08-02): モバイルは生ログトーストを
                                // 表示しない(300字の生例外が画面を覆う実害・サブOS実機)。
                                // popup_notification のハンドラも意図的に無い。
                                // モバイルのエラー表示は ①会話エラー=入力欄下の
                                // ❌ステータス(エラー時のみ約10秒表示) ②機能トグル/
                                // 添付拒否=専用アクション ③詳細=システムログページ。

                            } else if (data.action === 'attach_image_rejected') {
                                // フールプルーフ: Ollamaキャラ中の画像添付拒否。
                                // 理由はサーバー側でローカライズ済み(正はWS層の
                                // attach_image 拒否=クライアント側プリチェックなし)
                                if (typeof window.showNotification === 'function') {
                                    window.showNotification(data.popup_title || '', data.popup_message || '', 'warning', 5000);
                                }

                            } else if (data.action === 'image_generating_spinner') {
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const old = chatContainer.querySelector('.image-generating-spinner');
                                    if (old) old.remove();
                                    const div = document.createElement('div');
                                    div.className = 'image-generating-spinner';
                                    div.innerHTML = '<div class="loading-spinner"></div> Generating image...';
                                    chatContainer.appendChild(div);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'image_generation_block') {
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const imgSpinner = chatContainer.querySelector('.image-generating-spinner');
                                    if (imgSpinner) imgSpinner.remove();
                                    const bd = data.data || {};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const status = bd.status || 'error';
                                    const prompt = esc(bd.prompt || '');
                                    const igDiv = document.createElement('div');
                                    igDiv.className = 'image-gen-msg';
                                    if (status === 'success' && bd.thumbnail_base64) {
                                        const fullSrc = bd.full_base64
                                            ? 'data:image/png;base64,' + bd.full_base64
                                            : 'data:image/png;base64,' + bd.thumbnail_base64;
                                        const thumbSrc = 'data:image/png;base64,' + bd.thumbnail_base64;
                                        igDiv.innerHTML = `<div class="image-gen-header">Image Generated</div>`
                                            + `<img src="${thumbSrc}" data-full-src="${fullSrc}" class="lightbox-image" />`
                                            + `<div class="image-gen-prompt">${prompt}</div>`;
                                    } else {
                                        igDiv.innerHTML = `<div class="image-gen-header">Image Generation Failed</div>`
                                            + `<div class="image-gen-prompt">${prompt}</div>`;
                                    }
                                    chatContainer.appendChild(igDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'deep_search_spinner') {
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const old = chatContainer.querySelector('.deep-search-spinner');
                                    if (old) old.remove();
                                    const bd = data.data || {};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const div = document.createElement('div');
                                    div.className = 'deep-search-spinner';
                                    if (tool === 'search_web') {
                                        const q = esc(bd.query || '');
                                        div.innerHTML = '<div class="loading-spinner"></div> Searching: ' + q;
                                    } else {
                                        const u = esc(bd.url || '');
                                        div.innerHTML = '<div class="loading-spinner"></div> Reading: ' + u;
                                    }
                                    chatContainer.appendChild(div);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'deep_search_block') {
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const dsSpinner = chatContainer.querySelector('.deep-search-spinner');
                                    if (dsSpinner) dsSpinner.remove();
                                    const bd = data.data || {};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const status = bd.status || 'error';
                                    const query = esc(bd.query || '');
                                    const url = esc(bd.url || '');
                                    const dsDiv = document.createElement('div');
                                    dsDiv.className = 'deep-search-msg';
                                    const statusIcon = status === 'success' ? '🔍' : (status === 'rate_limited' ? '⏳' : '❌');
                                    const hitUrls = bd.hit_urls || [];
                                    const pageTitle = esc(bd.page_title || '');
                                    if (tool === 'search_web') {
                                        const label = status === 'success' ? 'Web Search' : 'Web Search Failed';
                                        let inner = `<div class="ds-header">${statusIcon} ${label}</div>`;
                                        if (query) inner += `<div class="ds-detail">${query}</div>`;
                                        if (hitUrls.length > 0) {
                                            const urlList = hitUrls.map(u => esc(u)).map(u => `<div class="ds-url">${u}</div>`).join('');
                                            inner += `<details class="ds-results"><summary>${'Results ({count})'.replace('{count}', hitUrls.length)}</summary>${urlList}</details>`;
                                        }
                                        dsDiv.innerHTML = inner;
                                    } else {
                                        const label = status === 'success' ? 'Page Read' : 'Page Read Failed';
                                        let inner = `<div class="ds-header">${statusIcon} ${label}</div>`;
                                        if (url) inner += `<div class="ds-detail">${url}</div>`;
                                        if (pageTitle) inner += `<div class="ds-detail">${pageTitle}</div>`;
                                        dsDiv.innerHTML = inner;
                                    }
                                    chatContainer.appendChild(dsDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'map_search_spinner') {
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const old = chatContainer.querySelector('.map-search-spinner');
                                    if (old) old.remove();
                                    const bd = data.data || {};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const div = document.createElement('div');
                                    div.className = 'map-search-spinner';
                                    if (tool === 'search_places') {
                                        div.innerHTML = '<div class="loading-spinner"></div> 🗺 Searching: ' + esc(bd.query || '');
                                    } else if (tool === 'get_place_details') {
                                        div.innerHTML = '<div class="loading-spinner"></div> 🗺 Getting reviews...';
                                    } else if (tool === 'get_directions') {
                                        div.innerHTML = '<div class="loading-spinner"></div> 🗺 Getting directions...';
                                    }
                                    chatContainer.appendChild(div);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'map_search_block') {
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const msSpinner = chatContainer.querySelector('.map-search-spinner');
                                    if (msSpinner) msSpinner.remove();
                                    const bd = data.data || {};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const status = bd.status || 'error';
                                    const msDiv = document.createElement('div');
                                    msDiv.className = 'map-search-msg';
                                    const ok = status === 'success';
                                    const icon = ok ? '🗺' : (status === 'rate_limited' ? '⏳' : '❌');
                                    if (tool === 'search_places') {
                                        const label = ok ? 'Place Search' : 'Place Search Failed';
                                        msDiv.innerHTML = `<div class="ds-header">${icon} ${label}</div>`
                                            + (bd.query ? `<div class="ds-detail">${esc(bd.query)}</div>` : '');
                                    } else if (tool === 'get_place_details') {
                                        const label = ok ? 'Place Details' : 'Place Details Failed';
                                        msDiv.innerHTML = `<div class="ds-header">${icon} ${label}</div>`;
                                    } else if (tool === 'get_directions') {
                                        const label = ok ? 'Directions' : 'Directions Failed';
                                        const modeMap = {'walking':'🚶','driving':'🚗','transit':'🚃'};
                                        const modeIcon = modeMap[bd.mode] || '';
                                        msDiv.innerHTML = `<div class="ds-header">${icon} ${label} ${modeIcon}</div>`;
                                    }
                                    chatContainer.appendChild(msDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }

                            } else if (data.action === 'image_slot_update') {
                                window._updateImageSlotStatus(data.data.count, data.data.max, data.data.text);

                            } else if (data.action === 'active_character_update') {
                                if (window.isCompanionMode && data.data) {
                                    window._setCompanionCharacter(data.data.character_id, data.data.character_name);
                                    // Trigger chat refresh to load new character's history
                                    const btn = document.querySelector('#ws-update-trigger');
                                    if (btn) { btn.style.display='block'; btn.click(); setTimeout(()=>{btn.style.display='none';},10); }
                                }

                            } else if (data.action === 'conversation_state_update') {
                                if (data.data) {
                                    window._connectionStarted = data.data.conversation_started;
                                    window._setMobileInputPlaceholder(data.data.conversation_started);
                                    if (window.isCompanionMode) {
                                        window._setConnectionLamp(data.data.conversation_started);
                                    }
                                }

                            } else if (data.action === 'extraction_started' || data.action === 'extraction_completed') {
                                const btn = document.querySelector('#extraction-status-trigger');
                                if (btn) { btn.style.display='block'; btn.click(); setTimeout(()=>{btn.style.display='none';},10); }

                            } else if (data.action === 'location_update_response') {
                                // Flash both location buttons (only the visible one matters:
                                // companion bar in companion mode, input-row 🗺 in standalone)
                                [['companion-location-btn', '#2a2a4a'], ['mobile-attach-btn', '']].forEach(function(pair) {
                                    var locBtn = document.getElementById(pair[0]);
                                    if (!locBtn) return;
                                    locBtn.style.opacity = '1';
                                    locBtn.style.background = data.success ? '#4caf50' : '#f44336';
                                    setTimeout(function() { locBtn.style.background = pair[1]; }, 2000);
                                });
                                if (data.success) {
                                    window._setAttachStatusText(String.fromCodePoint(0x1F5FA) + ' 位置情報を送信しました', '#4caf50');
                                    console.log('[Location] Updated:', data.address);
                                } else {
                                    window._setAttachStatusText('⚠️ 位置情報の送信に失敗しました', '#f44336');
                                    console.error('[Location] Update failed:', data.error);
                                }
                                setTimeout(function() {
                                    window._setAttachStatusText(window._lastAttachStatusText || '', '');
                                }, 4000);
                            }
                        } catch(e) { console.error('[WS] Parse error:', e); }
                    };

                    this.ws.onerror = (e) => { console.error('[WS] Error:', e); };

                    this.ws.onclose = (event) => {
                        // Stale onclose guard: if this.ws no longer points at the
                        // WS that just closed, we've already replaced/discarded it
                        // (typically via navigator.offline → ws.close() + ws=null,
                        // followed by online → new WS). The late-arriving close
                        // (e.g. server-side stale-replace 4002) must NOT trigger
                        // an error overlay on top of the already-running flow.
                        if (this.ws !== event.target) {
                            console.log('[WS] Stale onclose ignored (code=' + event.code + ')');
                            return;
                        }
                        console.log('[WS] Closed code=' + event.code + ' reason=' + event.reason);
                        this.ws = null;
                        if (this.closeIntentional) return;
                        const noReconnectMap = {
                            1000: null,
                            4001: 'error_4001',
                            4002: 'error_4002',
                            4003: 'error_4003',
                            4004: 'error_4004',
                            4006: 'error_4006',
                            4007: 'error_4007'
                        };
                        if (event.code in noReconnectMap) {
                            const state = noReconnectMap[event.code];
                            if (state) {
                                this.noReconnect = true;
                                this.showOverlay(state);
                            }
                            return;
                        }
                        this.scheduleReconnect();
                    };
                } catch(e) { console.error('[WS] Failed to create WebSocket:', e); }
            },

            disconnect: function() {
                this.closeIntentional = true;
                if (this.ws) {
                    try { this.ws.close(1000); } catch (e) {}
                    this.ws = null;
                }
                clearTimeout(this.reconnectTimer);
            },

            // Phase 2C helpers ------------------------------------------------
            enqueueSend: function(payload) {
                if (typeof payload !== 'string') {
                    console.warn('[WS] Binary payload not queueable, dropping');
                    return false;
                }
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    try { this.ws.send(payload); return true; } catch (e) {}
                }
                if (this.sendQueue.length >= this.queueMaxSize) {
                    this.sendQueue.shift();
                    console.warn('[WS] Queue full, dropping oldest message');
                }
                this.sendQueue.push(payload);
                return false;
            },

            flushQueue: function() {
                while (this.sendQueue.length > 0
                       && this.ws && this.ws.readyState === WebSocket.OPEN) {
                    const payload = this.sendQueue.shift();
                    try { this.ws.send(payload); }
                    catch (e) { this.sendQueue.unshift(payload); break; }
                }
            },

            scheduleReconnect: function() {
                if (this.reconnectStartedAt === null) {
                    this.reconnectStartedAt = Date.now();
                }
                const elapsed = Date.now() - this.reconnectStartedAt;
                if (elapsed > this.giveUpAfterMs) {
                    this.showOverlay('error_giveup');
                    return;
                }
                const idx = Math.min(this.reconnectAttempt, this.backoffSchedule.length - 1);
                const delay = this.backoffSchedule[idx];
                this.reconnectAttempt++;
                this.showOverlay('reconnecting', {
                    detail: '{sec}秒後に再試行（試行 {attempt}）'.replace('{sec}', Math.ceil(delay / 1000)).replace('{attempt}', this.reconnectAttempt)
                });
                this.reconnectTimer = setTimeout(() => this.connect(), delay);
            },

            showOverlay: function(state, options) {
                options = options || {};
                const overlay = document.getElementById('ws-overlay');
                if (!overlay) return;
                const icon = document.getElementById('ws-overlay-icon');
                const title = document.getElementById('ws-overlay-title');
                const message = document.getElementById('ws-overlay-message');
                const detail = document.getElementById('ws-overlay-detail');
                const action = document.getElementById('ws-overlay-action');
                const states = {
                    reconnecting: {
                        iconHtml: '⟳', title: '再接続中…',
                        message: 'ネットワークが不安定なため再接続を試みています。',
                        showAction: false, spinning: true
                    },
                    error_4001: {
                        iconHtml: '⚠', title: '別のデバイスで使用中',
                        message: 'AGは別のデバイスから接続中です。先に切断してから再度お試しください。',
                        showAction: false, spinning: false
                    },
                    error_4002: {
                        iconHtml: '🚫', title: '管理者により切断されました',
                        message: 'AGが再起動された可能性があります。リロードしてください。',
                        showAction: true, spinning: false
                    },
                    error_4003: {
                        iconHtml: '⏱', title: 'セッションタイムアウト',
                        message: '操作のない時間が長く続いたためセッションが終了しました。',
                        showAction: true, spinning: false
                    },
                    error_4004: {
                        iconHtml: '🔄', title: '別のウィンドウで開かれました',
                        message: 'このタブは切断されました。新しく開いたウィンドウをご利用ください。',
                        showAction: false, spinning: false
                    },
                    error_4006: {
                        iconHtml: '🔌', title: 'プライマリ端末が切断されました',
                        message: 'プライマリ端末の接続が終了したため、この画面も切断されました。プライマリ端末で再接続後、リロードしてください。',
                        showAction: true, spinning: false
                    },
                    error_4007: {
                        iconHtml: '🔌', title: '接続を解除しました',
                        message: 'このウィンドウは切断されました。再接続するにはリロードしてください。',
                        showAction: true, spinning: false
                    },
                    error_giveup: {
                        iconHtml: '⚠', title: '再接続できません',
                        message: '5分間再接続を試みましたが成功しませんでした。ネットワーク接続を確認してリロードしてください。',
                        showAction: true, spinning: false
                    }
                };
                const s = states[state];
                if (!s) return;
                if (icon) {
                    icon.innerHTML = s.iconHtml;
                    if (s.spinning) icon.classList.add('spinning');
                    else icon.classList.remove('spinning');
                }
                if (title) title.textContent = s.title;
                if (message) message.textContent = s.message;
                if (detail) detail.textContent = options.detail || '';
                if (action) action.style.display = s.showAction ? 'inline-block' : 'none';
                overlay.classList.add('visible');
            },

            hideOverlay: function() {
                const overlay = document.getElementById('ws-overlay');
                if (overlay) overlay.classList.remove('visible');
            }
        };

        // Phase 2C+: navigator.onLine — instant detection of physical network changes.
        // TCP keepalive can take minutes to detect a dead connection; OS-level
        // network interface state changes (Wi-Fi toggle, airplane mode) are
        // surfaced by the browser within ~1s via these events.
        //
        // Grace window: ignore offline events shorter than OFFLINE_GRACE_MS to
        // avoid flicker on transient mobile network drops (~1s typical).
        window._wsOfflineTimer = null;
        const OFFLINE_GRACE_MS = 3000;

        window.addEventListener('offline', () => {
            console.log('[WS] navigator: offline detected (grace ' + OFFLINE_GRACE_MS + 'ms)');
            if (window._wsOfflineTimer) clearTimeout(window._wsOfflineTimer);
            window._wsOfflineTimer = setTimeout(() => {
                window._wsOfflineTimer = null;
                if (navigator.onLine) return;
                const m = window.wsManager;
                if (!m) return;
                clearTimeout(m.reconnectTimer);
                if (m.ws) {
                    try { m.ws.close(); } catch(e) {}
                    m.ws = null;
                }
                m.showOverlay('reconnecting', { detail: 'ネットワーク接続なし' });
            }, OFFLINE_GRACE_MS);
        });

        window.addEventListener('online', () => {
            console.log('[WS] navigator: online detected');
            if (window._wsOfflineTimer) {
                clearTimeout(window._wsOfflineTimer);
                window._wsOfflineTimer = null;
            }
            const m = window.wsManager;
            if (!m) return;
            if (!m.ws || m.ws.readyState !== WebSocket.OPEN) {
                clearTimeout(m.reconnectTimer);
                m.reconnectAttempt = 0;
                m.reconnectStartedAt = null;
                m.connect();
            }
        });

        // Session blocked overlay
        window._showSessionBlockedOverlay = function() {
            if (document.getElementById('session-blocked-overlay')) return;
            const overlay = document.createElement('div');
            overlay.id = 'session-blocked-overlay';
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:99999;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#fff;font-family:sans-serif;';
            overlay.innerHTML = '<div style="text-align:center;padding:20px;"><div style="font-size:48px;margin-bottom:20px;">&#128274;</div><div style="font-size:24px;font-weight:bold;margin-bottom:12px;">現在使用中です</div><div style="font-size:16px;color:#aaa;">別のデバイスで接続中のため、このページは表示できません。</div></div>';
            document.body.appendChild(overlay);
        };

        // (Phase 2C: _showForceDisconnectedOverlay removed — use wsManager.showOverlay)

        // Feature toggle state
        window.talkThemeEnabled = true;
        window.speechlessEnabled = false;
        window.notesEnabled = false;
        window.elythEnabled = false;

        window.toggleTalkTheme = function() {
            const newState = !window.talkThemeEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({action: 'set_talk_theme', enabled: newState}));
            }
        };
        window.toggleSpeechless = function() {
            const newState = !window.speechlessEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({action: 'set_speechless', enabled: newState}));
            }
        };
        window.toggleNotes = function() {
            const newState = !window.notesEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({action: 'set_notes', enabled: newState}));
            }
        };
        window.toggleImageGeneration = function() {
            const newState = !window.imageGenerationEnabled;
            window.imageGenerationEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({action: 'set_image_generation', enabled: newState}));
            }
        };

        window.toggleDeepSearch = function() {
            const newState = !window.deepSearchEnabled;
            window.deepSearchEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({action: 'set_deep_search', enabled: newState}));
            }
        };

        window.toggleElyth = function() {
            const newState = !window.elythEnabled;
            window.elythEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {
                window.wsManager.enqueueSend(JSON.stringify({action: 'set_elyth', enabled: newState}));
            }
        };

        // Lightbox for images (mobile version) - event delegation
        (function() {
            function ensureMobileLightbox() {
                let lb = document.getElementById('image-lightbox');
                if (lb) return lb;
                lb = document.createElement('div');
                lb.id = 'image-lightbox';
                lb.style.cssText = 'display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:99999;justify-content:center;align-items:center;flex-direction:column;cursor:pointer;';
                lb.innerHTML = '<img id="lightbox-img" style="max-width:90%;max-height:80%;object-fit:contain;border-radius:8px;cursor:default;" />'
                    + '<div style="margin-top:12px;display:flex;gap:12px;">'
                    + '<button id="lightbox-download-btn" style="padding:8px 16px;background:rgba(255,255,255,0.15);color:#fff;border:1px solid rgba(255,255,255,0.3);border-radius:6px;cursor:pointer;font-size:13px;">Download</button>'
                    + '<button id="lightbox-close-btn" style="padding:8px 16px;background:rgba(255,255,255,0.15);color:#fff;border:1px solid rgba(255,255,255,0.3);border-radius:6px;cursor:pointer;font-size:13px;">Close</button>'
                    + '</div>';
                document.body.appendChild(lb);
                lb.addEventListener('click', function(e) { if (e.target === lb) lb.style.display = 'none'; });
                document.getElementById('lightbox-close-btn').addEventListener('click', function() {
                    document.getElementById('image-lightbox').style.display = 'none';
                });
                document.getElementById('lightbox-download-btn').addEventListener('click', function() {
                    const img = document.getElementById('lightbox-img');
                    if (!img.src) return;
                    const a = document.createElement('a'); a.href = img.src; a.download = 'image.png';
                    document.body.appendChild(a); a.click(); document.body.removeChild(a);
                });
                return lb;
            }
            ensureMobileLightbox();
            // Event delegation for .lightbox-image clicks
            document.addEventListener('click', function(e) {
                const img = e.target.closest('.lightbox-image');
                if (!img) return;
                const lb = ensureMobileLightbox();
                const fullSrc = img.getAttribute('data-full-src') || img.src;
                document.getElementById('lightbox-img').src = fullSrc;
                lb.style.display = 'flex';
            });
        })();

        window.updateFeatureToggleButtons = function() {
            // フールプルーフ: ブロック理由がある機能へ disabled/グレー/tooltip を
            // 毎描画で再適用(サーバー側が強制OFFする前提で常に無効化。
            // 横線は廃止=稜裁定 2026-08-15・デスクトップと同規則)
            const applyAvail = function(btn, feature) {
                const reason = (window.featureAvailability || {})[feature];
                if (reason) {
                    btn.title = reason;
                    btn.disabled = true;
                    btn.style.opacity = '0.5';
                } else {
                    btn.title = '';
                    btn.disabled = false;
                    btn.style.opacity = '1';
                }
            };
            const ttBtn = document.getElementById('mobile-talk-theme-toggle-btn');
            const slBtn = document.getElementById('mobile-speechless-toggle-btn');
            if (ttBtn) {
                ttBtn.textContent = 'Talk Theme: {state}'.replace('{state}', window.talkThemeEnabled ? 'ON' : 'OFF');
                ttBtn.style.backgroundColor = window.talkThemeEnabled ? '#4caf50' : '#607d8b';
            }
            if (slBtn) {
                slBtn.textContent = 'Speechless: {state}'.replace('{state}', window.speechlessEnabled ? 'ON' : 'OFF');
                slBtn.style.backgroundColor = window.speechlessEnabled ? '#4caf50' : '#607d8b';
            }
            const ntBtn = document.getElementById('mobile-notes-toggle-btn');
            if (ntBtn) {
                ntBtn.textContent = 'Notes: {state}'.replace('{state}', window.notesEnabled ? 'ON' : 'OFF');
                ntBtn.style.backgroundColor = window.notesEnabled ? '#4caf50' : '#607d8b';
                applyAvail(ntBtn, 'notes');
            }
            const igBtn = document.getElementById('mobile-image-gen-toggle-btn');
            if (igBtn) {
                igBtn.textContent = 'ImageGen: {state}'.replace('{state}', window.imageGenerationEnabled ? 'ON' : 'OFF');
                igBtn.style.backgroundColor = window.imageGenerationEnabled ? '#4caf50' : '#607d8b';
                applyAvail(igBtn, 'image_generation');
            }

            const dsBtn = document.getElementById('mobile-deep-search-toggle-btn');
            if (dsBtn) {
                dsBtn.textContent = 'DeepSearch: {state}'.replace('{state}', window.deepSearchEnabled ? 'ON' : 'OFF');
                dsBtn.style.backgroundColor = window.deepSearchEnabled ? '#4caf50' : '#607d8b';
                applyAvail(dsBtn, 'deep_search');
            }

            const elBtn = document.getElementById('mobile-elyth-toggle-btn');
            if (elBtn) {
                elBtn.textContent = 'ELYTH: {state}'.replace('{state}', window.elythEnabled ? 'ON' : 'OFF');
                elBtn.style.backgroundColor = window.elythEnabled ? '#4caf50' : '#607d8b';
                applyAvail(elBtn, 'elyth');
            }
        };

        // Menu open/close
        window.toggleMobileMenu = function() {
            const menu = document.getElementById('mobile-hamburger-menu');
            const backdrop = document.getElementById('mobile-menu-backdrop');
            if (menu && backdrop) {
                const isOpen = menu.classList.contains('open');
                menu.classList.toggle('open', !isOpen);
                backdrop.classList.toggle('open', !isOpen);
            }
        };
        window.closeMobileMenu = function() {
            const menu = document.getElementById('mobile-hamburger-menu');
            const backdrop = document.getElementById('mobile-menu-backdrop');
            if (menu) menu.classList.remove('open');
            if (backdrop) backdrop.classList.remove('open');
        };
        window._closeMobileAttachMenu = function() {
            const menu = document.getElementById('mobile-attach-menu');
            if (menu) menu.classList.remove('open');
        };

        // Native <select> → Gradio bridge for character selection
        // Sets hidden textbox value via nativeSetter, then clicks trigger button
        document.addEventListener('change', function(e) {
            if (e.target.id === 'mobile-char-select') {
                const charId = e.target.value;
                console.log('[Mobile] Character selected:', charId);

                // Step 1: Set hidden textbox value (Svelte bind:value syncs on 'input' event)
                const container = document.querySelector('#mobile-char-hidden');
                if (container) {
                    const el = container.querySelector('textarea') || container.querySelector('input');
                    if (el) {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLTextAreaElement.prototype, 'value'
                        ).set || Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(el, charId);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        console.log('[Mobile] Hidden textbox set to:', charId);
                    } else {
                        console.warn('[Mobile] Hidden textbox input element not found');
                    }
                } else {
                    console.warn('[Mobile] Hidden textbox container not found');
                }

                // Step 2: Click trigger after delay (let Svelte process the value)
                setTimeout(function() {
                    const btn = document.querySelector('#mobile-char-switch-trigger');
                    if (btn) {
                        btn.style.display = 'block';
                        btn.click();
                        setTimeout(function() { btn.style.display = 'none'; }, 10);
                        console.log('[Mobile] Trigger clicked');
                    } else {
                        console.warn('[Mobile] Trigger button not found');
                    }
                }, 200);
            }
        });

        // Event delegation for feature toggle + backdrop + attach menu
        document.addEventListener('click', function(e) {
            // Backdrop click closes menu
            if (e.target.id === 'mobile-menu-backdrop') {
                window.closeMobileMenu();
                return;
            }
            // 📎 attach action menu (file attach / location send)
            if (e.target.closest('#mobile-attach-btn')) {
                const menu = document.getElementById('mobile-attach-menu');
                if (menu) menu.classList.toggle('open');
                return;
            }
            if (e.target.closest('#mobile-attach-menu-file')) {
                window._closeMobileAttachMenu();
                // Click the hidden UploadButton synchronously (keeps user activation
                // so the OS file picker is allowed to open)
                const up = document.getElementById('mobile-attach-upload');
                const b = up && (up.matches('button') ? up : up.querySelector('button'));
                if (b) b.click();
                return;
            }
            if (e.target.closest('#mobile-attach-menu-location')) {
                window._closeMobileAttachMenu();
                window._mobileSendLocation();
                return;
            }
            // Any other tap closes the attach menu (falls through to other handlers)
            window._closeMobileAttachMenu();
            // Feature toggle buttons (Gradio strips onclick)
            const ttToggle = e.target.closest('#mobile-talk-theme-toggle-btn');
            if (ttToggle) { window.toggleTalkTheme(); return; }
            const slToggle = e.target.closest('#mobile-speechless-toggle-btn');
            if (slToggle) { window.toggleSpeechless(); return; }
            const ntToggle = e.target.closest('#mobile-notes-toggle-btn');
            if (ntToggle) { window.toggleNotes(); return; }
            const igToggle = e.target.closest('#mobile-image-gen-toggle-btn');
            if (igToggle) { window.toggleImageGeneration(); return; }
            const dsToggle = e.target.closest('#mobile-deep-search-toggle-btn');
            if (dsToggle) { window.toggleDeepSearch(); return; }
            const elToggle = e.target.closest('#mobile-elyth-toggle-btn');
            if (elToggle) { window.toggleElyth(); return; }
        });

        // Auto-resize native textarea as user types (mobile + companion)
        document.addEventListener('input', function(e) {
            if (e.target.id === 'mobile-native-input' || e.target.id === 'companion-text-input') {
                e.target.style.height = 'auto';
                e.target.style.height = Math.min(e.target.scrollHeight, 100) + 'px';
            }
        });

        // TTS Audio init
        window.ttsAudioEnabled = false;
        window.ttsAudioContext = null;

        // Cross-client generating state
        window._isGenerating = false;
        window._connectionStarted = false;

        // Native textarea → Python bridge for text send
        window._mobileSendText = function() {
            const textarea = document.getElementById('mobile-native-input');
            if (!textarea) { console.warn('[Mobile] Native textarea not found'); return; }
            const text = textarea.value.trim();
            if (!text) { console.log('[Mobile] Empty text, ignoring'); return; }
            // Block before conversation start (matches companion behavior;
            // the server would silently swallow the text otherwise)
            if (!window._connectionStarted) { console.log('[Mobile] Conversation not started, blocking send'); return; }
            // Block if another client is generating
            if (window._isGenerating) { console.log('[Mobile] Generation in progress, blocking send'); return; }
            console.log('[Mobile] Sending text:', text.substring(0, 50));

            // Step 1: Set hidden textbox value via nativeSetter
            const container = document.querySelector('#mobile-text-hidden');
            if (container) {
                const el = container.querySelector('textarea') || container.querySelector('input');
                if (el) {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLTextAreaElement.prototype, 'value'
                    ).set || Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    console.log('[Mobile] Hidden text bridge set');
                }
            }

            // Step 2: Clear native textarea immediately and reset height to 1 line
            textarea.value = '';
            textarea.style.height = '';

            // Step 3: Click trigger after delay (let Svelte process the value)
            setTimeout(function() {
                const btn = document.querySelector('#mobile-text-send-trigger');
                if (btn) {
                    btn.style.display = 'block';
                    btn.click();
                    setTimeout(function() { btn.style.display = 'none'; }, 10);
                    console.log('[Mobile] Text send trigger clicked');
                }
            }, 200);
        };

        // Send button click (event delegation – no Gradio handler on send_btn)
        document.addEventListener('click', function(e) {
            if (e.target.closest('#mobile-send-btn')) {
                e.preventDefault();
                e.stopPropagation();
                window._mobileSendText();
            }
        }, true);  // useCapture to fire before Gradio

        // Enter key sends, Shift+Enter inserts newline (mobile + companion)
        document.addEventListener('keydown', function(e) {
            // IME変換中のEnter(=確定操作)では送信しない。Android Gboardは
            // IME経由キーがkeyCode=229で届く。iOSのフリック変換確定も同様
            if (e.isComposing || e.keyCode === 229) return;
            if (e.target.id === 'mobile-native-input' && e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                window._mobileSendText();
            }
            if (e.target.id === 'companion-text-input' && e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                window._companionSendText();
            }
        });

        // ---- Companion Mode & Image Slot Status ----
        // text: server-built combined text (images + docs). Falls back to a
        // locally built image-only text when absent (companion identify path).
        window._updateImageSlotStatus = function(count, max, text) {
            window._currentImageCount = count;
            window._currentImageMax = max;
            if (text === undefined || text === null) {
                text = count > 0
                    ? String.fromCodePoint(0x1F4CE) + ' ' + count + '/' + max + ' image' + (count !== 1 ? 's' : '') + ' attached'
                    : '';
            }
            window._lastAttachStatusText = text;
            // Companion mode status
            const compEl = document.getElementById('companion-attach-status');
            if (compEl) { compEl.textContent = text; compEl.style.color = ''; }
            // Normal mobile mode status (JS-owned plain div)
            const mobEl = document.getElementById('mobile-attach-status');
            if (mobEl) { mobEl.textContent = text; mobEl.style.color = ''; }
        };

        window._setCompanionCharacter = function(charId, charName) {
            const sel = document.getElementById('mobile-char-select');
            if (!sel) return;
            // Try to set value to charId
            sel.value = charId;
            // If charId doesn't exist as an option, add it
            if (sel.value !== charId && charId) {
                const opt = document.createElement('option');
                opt.value = charId;
                opt.textContent = charName || charId;
                sel.appendChild(opt);
                sel.value = charId;
            }
            console.log('[Companion] Character set:', charName || charId);
        };

        // Primary mobile input placeholder — reflects conversation started state.
        // 初期HTMLは未開始表示で生成され、identify_response と
        // conversation_state_update がここで最新状態に揃える(書き手はJSのみ)
        window._setMobileInputPlaceholder = function(started) {
            const ta = document.getElementById('mobile-native-input');
            if (ta) {
                ta.placeholder = started ? 'メッセージを入力...' : 'まずは「会話を開始」ボタンを押して下さい';
                // 会話未開始は入力自体をロック(コンパニオンの disabled 方式と同じ)。
                // 生成中のロックは _setCompanionGeneratingState 側が管理する
                if (!window._isGenerating) { ta.disabled = !started; }
            }
            const sendBtn = document.getElementById('mobile-send-btn');
            if (sendBtn && !window._isGenerating) { sendBtn.disabled = !started; }
        };

        window._setConnectionLamp = function(started) {
            window._connectionStarted = started;
            // gr.HTML has no <p> — find the deepest text-holding element
            const root = document.getElementById('mobile-connection-status');
            if (!root) return;
            const emoji = started ? String.fromCodePoint(0x1F7E2) : String.fromCodePoint(0x1F534);
            // Walk into Gradio wrapper divs to find the innermost element
            let target = root;
            while (target.children.length === 1 && target.firstElementChild) {
                target = target.firstElementChild;
            }
            target.textContent = emoji;
            // Enable/disable companion text input based on conversation state
            if (window.isCompanionMode && !window._isGenerating) {
                var textInput = document.getElementById('companion-text-input');
                var sendBtn = document.getElementById('companion-send-btn');
                if (textInput) {
                    textInput.disabled = !started;
                    textInput.placeholder = started ? '\u30E1\u30C3\u30BB\u30FC\u30B8\u3092\u5165\u529B...' : '\u4F1A\u8A71\u672A\u958B\u59CB';
                }
                if (sendBtn) sendBtn.disabled = !started;
            }
        };

        // Companion text send — reuses existing text_hidden → text_send_trigger bridge
        window._companionSendText = function() {
            var textarea = document.getElementById('companion-text-input');
            if (!textarea) { console.warn('[Companion] Text input not found'); return; }
            var text = textarea.value.trim();
            if (!text) { console.log('[Companion] Empty text, ignoring'); return; }
            // Guard 1: conversation not started
            if (!window._connectionStarted) { console.log('[Companion] Conversation not started'); return; }
            // Guard 2: generation in progress
            if (window._isGenerating) { console.log('[Companion] Generation in progress, blocking'); return; }

            console.log('[Companion] Sending text:', text.substring(0, 50));

            // Set hidden textbox value via nativeSetter (same bridge as primary mobile)
            var container = document.querySelector('#mobile-text-hidden');
            if (container) {
                var el = container.querySelector('textarea') || container.querySelector('input');
                if (el) {
                    var setter = Object.getOwnPropertyDescriptor(
                        HTMLTextAreaElement.prototype, 'value'
                    ).set || Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    console.log('[Companion] Hidden text bridge set');
                }
            }

            // Clear companion textarea immediately
            textarea.value = '';
            textarea.style.height = '';

            // Click trigger after delay (let Svelte process the value)
            setTimeout(function() {
                var btn = document.querySelector('#mobile-text-send-trigger');
                if (btn) {
                    btn.style.display = 'block';
                    btn.click();
                    setTimeout(function() { btn.style.display = 'none'; }, 10);
                    console.log('[Companion] Text send trigger clicked');
                }
            }, 200);
        };

        // Set companion generating state (disable/enable text input)
        window._setCompanionGeneratingState = function(generating) {
            var textInput = document.getElementById('companion-text-input');
            var sendBtn = document.getElementById('companion-send-btn');
            if (generating) {
                if (textInput) { textInput.disabled = true; textInput.placeholder = '\u751F\u6210\u4E2D...'; }
                if (sendBtn) sendBtn.disabled = true;
            } else {
                // Restore based on connection state
                var canSend = window._connectionStarted;
                if (textInput) {
                    textInput.disabled = !canSend;
                    textInput.placeholder = canSend ? '\u30E1\u30C3\u30BB\u30FC\u30B8\u3092\u5165\u529B...' : '\u4F1A\u8A71\u672A\u958B\u59CB';
                }
                if (sendBtn) sendBtn.disabled = !canSend;
            }
            // Also control primary mobile input. 生成終了時も会話未開始なら
            // ロックを維持する(未開始ロックの上書き解除を防ぐ)
            var nativeInput = document.getElementById('mobile-native-input');
            var mobileSendBtn = document.getElementById('mobile-send-btn');
            var mobileLocked = generating || !window._connectionStarted;
            if (nativeInput) nativeInput.disabled = mobileLocked;
            if (mobileSendBtn) mobileSendBtn.disabled = mobileLocked;
        };

        // ---- Common image-attach helpers (used by Primary attach hook and Companion file pickers) ----
        window._compressAndSendImage = function(file) {
            return new Promise((resolve) => {
                if (!file) { resolve(false); return; }
                const reader = new FileReader();
                reader.onload = function(e) {
                    const img = new Image();
                    img.onload = function() {
                        const maxDim = 1024;
                        let w = img.width, h = img.height;
                        if (w > maxDim || h > maxDim) {
                            if (w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
                            else { w = Math.round(w * maxDim / h); h = maxDim; }
                        }
                        const canvas = document.createElement('canvas');
                        canvas.width = w; canvas.height = h;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, w, h);
                        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
                        const b64 = dataUrl.split(',')[1];
                        if (window.wsManager) {
                            window.wsManager.enqueueSend(JSON.stringify({
                                action: 'attach_image',
                                image_data: b64,
                                content_type: 'image/jpeg',
                                idempotency_key: crypto.randomUUID()
                            }));
                            console.log('[Attach] Image sent:', w + 'x' + h, Math.round(b64.length * 3/4 / 1024) + 'KB');
                            resolve(true);
                        } else {
                            console.warn('[Attach] wsManager not available');
                            resolve(false);
                        }
                    };
                    img.onerror = () => resolve(false);
                    img.src = e.target.result;
                };
                reader.onerror = () => resolve(false);
                reader.readAsDataURL(file);
            });
        };

        // Backward-compat wrapper for companion file pickers (kept to avoid touching existing change handlers)
        window._companionAttachImage = function(file) {
            return window._compressAndSendImage(file);
        };

        // Helper: write text to all attach-status elements (Desktop / Mobile / Companion)
        window._setAttachStatusText = function(text, color) {
            const targets = [];
            const mobEl = document.getElementById('mobile-attach-status');
            if (mobEl) targets.push(mobEl);
            const compEl = document.getElementById('companion-attach-status');
            if (compEl) targets.push(compEl);
            const dtCont = document.querySelector('#attach-status');
            if (dtCont) {
                let dtEl = dtCont.querySelector('p');
                if (!dtEl) { dtEl = document.createElement('p'); dtCont.appendChild(dtEl); }
                targets.push(dtEl);
            }
            targets.forEach(el => {
                el.textContent = text;
                el.style.color = color || '';
            });
        };

        window._showAttachWarning = function(msg) {
            window._setAttachStatusText('⚠️ ' + msg, '#f44336');
            // Auto-restore after 4s to the last broadcast attach-status text
            setTimeout(() => {
                window._setAttachStatusText(window._lastAttachStatusText || '', '');
            }, 4000);
        };

        window._showAttachProgress = function(msg) {
            window._setAttachStatusText(msg, '#888');
        };

        // ---- Primary attach button: prefilter images → WS, leave docs to Gradio HTTP ----
        window._setupAttachHook = function(buttonElemId) {
            const setup = () => {
                const button = document.getElementById(buttonElemId);
                if (!button) return false;
                // Gradio 5's UploadButton renders <input type="file"> as a SIBLING
                // of the <button> (BaseButton), not as a child. Walk up the ancestor
                // chain until we find the <input> in the same wrapper component.
                let input = button.querySelector('input[type="file"]');
                if (!input) {
                    let cur = button.parentElement;
                    for (let i = 0; i < 6 && cur && !input; i++) {
                        input = cur.querySelector('input[type="file"]');
                        cur = cur.parentElement;
                    }
                }
                if (!input) return false;
                if (input.dataset.attachHookInstalled === '1') return true;
                input.dataset.attachHookInstalled = '1';

                input.addEventListener('change', async function(e) {
                    if (!e.target.files || e.target.files.length === 0) return;

                    const IMG_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
                    const allFiles = Array.from(e.target.files);
                    const imageFiles = [];
                    const docFiles = [];

                    for (const f of allFiles) {
                        const lower = (f.name || '').toLowerCase();
                        const isImage = IMG_EXTS.some(ext => lower.endsWith(ext));
                        (isImage ? imageFiles : docFiles).push(f);
                    }

                    // Image upper-limit pre-check (matches MAX_IMAGES_PER_MESSAGE on the server)
                    const MAX_IMAGES = window._currentImageMax || 5;
                    const currentCount = window._currentImageCount || 0;
                    const remaining = Math.max(0, MAX_IMAGES - currentCount);
                    const imagesToSend = imageFiles.slice(0, remaining);
                    const droppedCount = imageFiles.length - imagesToSend.length;

                    if (droppedCount > 0) {
                        window._showAttachWarning(
                            '画像は最大{max}枚まで。{dropped}枚は添付されません。'
                                .replace('{max}', MAX_IMAGES).replace('{dropped}', droppedCount)
                        );
                    }

                    if (imagesToSend.length > 0) {
                        const wsReady = window.wsManager && window.wsManager.ws &&
                                       window.wsManager.ws.readyState === 1;
                        if (!wsReady) {
                            window._showAttachWarning(
                                'WebSocketが切断されています。再接続後に画像を再添付してください。'
                            );
                        } else {
                            for (let i = 0; i < imagesToSend.length; i++) {
                                if (imagesToSend.length > 1) {
                                    window._showAttachProgress(
                                        '画像送信中... {i}/{total}'.replace('{i}', i + 1).replace('{total}', imagesToSend.length)
                                    );
                                }
                                await window._compressAndSendImage(imagesToSend[i]);
                            }
                        }
                    }

                    // Replace input.files so Gradio uploads only documents (best-effort).
                    // If DataTransfer is unsupported, the server-side handler still skips images by extension.
                    if (imageFiles.length > 0) {
                        try {
                            const dt = new DataTransfer();
                            for (const f of docFiles) dt.items.add(f);
                            input.files = dt.files;
                        } catch (err) {
                            console.warn('[Attach] DataTransfer not supported, server will filter images:', err);
                        }

                        if (docFiles.length === 0) {
                            // Suppress Gradio upload entirely (no docs left to upload)
                            e.stopImmediatePropagation();
                            e.preventDefault();
                            setTimeout(() => { try { input.value = ''; } catch (e2) {} }, 0);
                        }
                    }
                }, true);  // capture phase: run before Gradio's listener

                console.log('[Attach] Hook installed on', buttonElemId);
                return true;
            };

            if (!setup()) {
                let attempts = 0;
                const interval = setInterval(() => {
                    attempts++;
                    if (setup() || attempts > 50) clearInterval(interval);
                }, 200);
            }
        };

        window._companionOpenCamera = function() {
            const el = document.getElementById('companion-camera-input');
            if (el) { el.value = ''; el.click(); }
        };
        window._companionOpenAttach = function() {
            const el = document.getElementById('companion-attach-input');
            if (el) { el.value = ''; el.click(); }
        };

        // Shared location send (companion 🗺 button + standalone input-row 🗺 button).
        // idleBg: background to restore after the 2s success/error flash
        // ('' = revert to stylesheet, used for the Gradio button in standalone).
        window._sendLocation = function(btnId, idleBg) {
            if (!navigator.geolocation) {
                console.warn('[Location] Geolocation not supported');
                return;
            }
            var btn = document.getElementById(btnId);
            if (btn) btn.style.opacity = '0.5';
            navigator.geolocation.getCurrentPosition(
                function(position) {
                    if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === 1) {
                        window.wsManager.enqueueSend(JSON.stringify({
                            action: 'update_location',
                            latitude: position.coords.latitude,
                            longitude: position.coords.longitude
                        }));
                        console.log('[Location] Sent:', position.coords.latitude, position.coords.longitude);
                    } else {
                        console.warn('[Location] WebSocket not connected');
                        if (btn) btn.style.opacity = '1';
                    }
                },
                function(error) {
                    console.error('[Location] Geolocation error:', error.message);
                    if (btn) {
                        btn.style.opacity = '1';
                        btn.style.background = '#f44336';
                        setTimeout(function() { btn.style.background = idleBg; }, 2000);
                    }
                },
                { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
            );
        };
        window._companionSendLocation = function() { window._sendLocation('companion-location-btn', '#2a2a4a'); };
        // Standalone: 🗺 lives inside the 📎 menu, so the visible clip button carries the flash
        window._mobileSendLocation = function() { window._sendLocation('mobile-attach-btn', ''); };

        // File input change handlers for companion mode
        ['companion-camera-input', 'companion-attach-input'].forEach(function(id) {
            document.addEventListener('change', function(e) {
                if (e.target.id === id && e.target.files) {
                    Array.from(e.target.files).forEach(function(f) {
                        window._companionAttachImage(f);
                    });
                }
            });
        });

        // ---- Chat Auto-Scroll (MutationObserver) ----
        // Gradio js= runs BEFORE fn= updates DOM, so the existing
        // MOBILE_SCROLL_JS scrolls stale content. MutationObserver
        // fires AFTER DOM changes, guaranteeing correct scroll.
        (function() {
            const setupScroll = () => {
                const chatDisplay = document.getElementById('chat-display');
                if (!chatDisplay) {
                    setTimeout(setupScroll, 200);
                    return;
                }
                if (window._chatScrollObserver) return;

                // iOS Safari scroll-chaining fix: never let scrollTop sit exactly
                // at 0 or at the maximum. When at the boundary, iOS chains the
                // scroll gesture to the parent container instead of scrolling
                // this element. Keeping 1px off the boundary prevents this.
                const scrollToBottom = () => {
                    const max = chatDisplay.scrollHeight - chatDisplay.clientHeight;
                    chatDisplay.scrollTop = max > 0 ? max - 1 : 0;
                };
                chatDisplay.addEventListener('scroll', () => {
                    const max = chatDisplay.scrollHeight - chatDisplay.clientHeight;
                    if (max <= 0) return;
                    if (chatDisplay.scrollTop <= 0) {
                        chatDisplay.scrollTop = 1;
                    } else if (chatDisplay.scrollTop >= max) {
                        chatDisplay.scrollTop = max - 1;
                    }
                }, {passive: true});

                let scrollTimer = null;
                const observer = new MutationObserver(() => {
                    clearTimeout(scrollTimer);
                    scrollTimer = setTimeout(scrollToBottom, 50);
                });
                observer.observe(chatDisplay, { childList: true, subtree: true });
                window._chatScrollObserver = observer;

                // Initial scroll for content already loaded before observer
                setTimeout(scrollToBottom, 100);

                console.log('[Mobile] Chat auto-scroll observer installed');
            };
            setupScroll();
        })();

        // Install attach hook on Mobile Primary attach button (Phase 1A)
        // Images → WebSocket attach_image, documents → Gradio HTTP upload
        window._setupAttachHook('mobile-attach-upload');

        // Start connection
        window.wsManager.connect();

        // Cleanup on unload. pagehide is registered too because iOS Safari
        // never fires beforeunload — without it the server only sees a silent
        // death instead of an intentional close(1000).
        window.addEventListener('beforeunload', () => {
            if (window.wsManager) window.wsManager.disconnect();
        });
        window.addEventListener('pagehide', () => {
            if (window.wsManager) window.wsManager.disconnect();
        });
        // pagehide also fires on bfcache eviction (back/forward navigation).
        // On restore the WS is dead and closeIntentional is still true — without
        // this reset the page would stay silent forever.
        window.addEventListener('pageshow', () => {
            const m = window.wsManager;
            if (m && !m.noReconnect && !m.ws) {
                clearTimeout(m.reconnectTimer);
                m.connect();
            }
        });
        // Screen-on / tab refocus: reconnect immediately instead of waiting out
        // the backoff timer (suspend-resume UX; liveness monitor may have
        // released the session server-side — reclaim/resume transparently).
        document.addEventListener('visibilitychange', () => {
            const m = window.wsManager;
            if (!document.hidden && m && !m.noReconnect && !m.ws) {
                clearTimeout(m.reconnectTimer);
                m.connect();
            }
        });

        // 返り値を返さない: js+fn 同居では js の返り値がイベント引数に
        // 化けるため、0引数の load エンドポイントに対して毎回コンソール
        // エラー(Parameter 0 is not a valid keyword argument)が出ていた
        // (CDP実測 2026-08-03)。初期化完了はログ不要の正常系。
    }
    """
    # i18n 後置換: この JS リテラルは f-string ではないため、t() 済み文字列を
    # 生成後の文字列置換で埋め込む（ws_client_js.py の f-string + _js() 方式の
    # 非 f-string 版）。訳語にシングルクォート・バッククォートを含めないこと。
    for src, dst in (
        ('{sec}秒後に再試行（試行 {attempt}）', t('js.ovl.retry_in')),
        ('AGは別のデバイスから接続中です。先に切断してから再度お試しください。', t('js.ovl.in_use_msg')),
        ('AGが再起動された可能性があります。リロードしてください。', t('js.ovl.kicked_msg')),
        ('操作のない時間が長く続いたためセッションが終了しました。', t('js.ovl.timeout_msg')),
        ('5分間再接続を試みましたが成功しませんでした。ネットワーク接続を確認してリロードしてください。', t('js.ovl.giveup_msg')),
        ('ネットワークが不安定なため再接続を試みています。', t('js.ovl.reconnecting_msg')),
        ('別のデバイスで接続中のため、このページは表示できません。', t('js.ovl.blocked_msg')),
        ('再接続中…', t('js.ovl.reconnecting_title')),
        ('別のデバイスで使用中', t('js.ovl.in_use_title')),
        ('管理者により切断されました', t('js.ovl.kicked_title')),
        ('このタブは切断されました。新しく開いたウィンドウをご利用ください。', t('js.ovl.other_window_msg')),
        ('別のウィンドウで開かれました', t('js.ovl.other_window_title')),
        ('プライマリ端末の接続が終了したため、この画面も切断されました。プライマリ端末で再接続後、リロードしてください。', t('js.ovl.primary_left_msg')),
        ('プライマリ端末が切断されました', t('js.ovl.primary_left_title')),
        ('このウィンドウは切断されました。再接続するにはリロードしてください。', t('js.ovl.self_disconnect_msg')),
        ('接続を解除しました', t('js.ovl.self_disconnect_title')),
        ('セッションタイムアウト', t('js.ovl.timeout_title')),
        ('再接続できません', t('js.ovl.giveup_title')),
        ('ネットワーク接続なし', t('js.ovl.no_network')),
        ('現在使用中です', t('js.ovl.blocked_title')),
        ('画像は最大{max}枚まで。{dropped}枚は添付されません。', t('js.attach.too_many')),
        ('WebSocketが切断されています。再接続後に画像を再添付してください。', t('js.attach.ws_disconnected')),
        ('画像送信中... {i}/{total}', t('js.attach.sending')),
        ('位置情報を送信しました', t('js.attach.location_sent')),
        ('位置情報の送信に失敗しました', t('js.attach.location_failed')),
        ('まずは「会話を開始」ボタンを押して下さい', t('conv.text_placeholder_locked')),
        ('[Timer] Next auto prompt in: {n}s', t('js.auto.countdown')),
        ('[Timer] Sending auto prompt...', t('js.auto.sending')),
        ('[Timer] Auto Prompt: Inactive', t('js.auto.inactive')),
        ('[Timer] Auto Prompt: Disabled', t('js.auto.disabled')),
        ('メッセージを入力...', t('mobile.input_placeholder')),
        ('会話未開始', t('mobile.not_started')),
        ('生成中...', t('mobile.generating')),
        ('(none)', t('mobile.theme_none')),
        ('エラー', t('js.notify.error_title')),
        ('Image Generation Failed', t('chat.image_gen_failed')),
        ('Image Generated', t('chat.image_generated')),
        ('Talk Theme Cleared', t('gen.talk_theme_cleared')),
        ('Talk Theme Set', t('gen.talk_theme_set')),
        ('Web Search Failed', t('gen.web_search_failed')),
        ('Web Search', t('gen.web_search')),
        ('Page Read Failed', t('gen.page_read_failed')),
        ('Page Read', t('gen.page_read')),
        ('Place Search Failed', t('gen.place_search_failed')),
        ('Place Search', t('gen.place_search')),
        ('Place Details Failed', t('gen.place_details_failed')),
        ('Place Details', t('gen.place_details')),
        ('Directions Failed', t('gen.directions_failed')),
        ('Directions', t('gen.directions')),
        ('Generating response...', t('gen.generating')),
        ('Results ({count})', t('gen.results')),
        ('Talk Theme: {state}', t('utility.talk_theme')),
        ('Speechless: {state}', t('utility.speechless')),
        ('Notes: {state}', t('utility.notes')),
        ('ImageGen: {state}', t('utility.imagegen')),
        ('DeepSearch: {state}', t('utility.deepsearch')),
        ('ELYTH: {state}', t('utility.elyth')),
        ('>Download</button>', '>' + t('js.lightbox.download') + '</button>'),
        ('>Close</button>', '>' + t('js.lightbox.close') + '</button>'),
        ('No Talk Theme', t('gen.no_talk_theme')),
    ):
        js = js.replace(src, dst)
    return js


# ---------------------------------------------------------------------------
# Utility panel HTML (feature toggle buttons)
# ---------------------------------------------------------------------------
def _mobile_avail_attrs(reason):
    """前提条件つき機能のボタン装飾(モバイル)。

    ブロック理由がある場合は常に disabled+グレー+tooltip(横線は廃止=稜裁定
    2026-08-15)。利用不能になった機能はバックエンドが強制OFFする前提の
    表示規則(稜裁定 2026-07-25)。デスクトップの ui/utility_panel._avail_attrs
    と同じ規則。
    """
    import html as _html
    if not reason:
        return '', ''
    title = f' title="{_html.escape(reason, quote=True)}"'
    return 'opacity:0.5;', f' disabled{title}'


def _build_utility_panel_html(talk_theme: bool = True, speechless: bool = False,
                              notes: bool = False, image_generation: bool = False,
                              deep_search: bool = False, elyth: bool = False,
                              availability=None) -> str:
    """Build HTML for feature toggle buttons in hamburger menu.

    PC Status / Screen Capture are deliberately absent: from a mobile client
    they would only report the server PC's state — meaningless remotely
    (稜裁定 2026-07-16). Camera toggles are desktop-page-only for the same
    reason (the camera hardware lives on the desktop page).

    availability: {feature: ローカライズ済みブロック理由 | None}(フールプルーフ。
    動的な更新はWSの feature_availability → JS が担う)。
    """
    av = availability or {}
    tt_bg = '#4caf50' if talk_theme else '#607d8b'
    tt_label = 'ON' if talk_theme else 'OFF'
    sl_bg = '#4caf50' if speechless else '#607d8b'
    sl_label = 'ON' if speechless else 'OFF'
    nt_bg = '#4caf50' if notes else '#607d8b'
    nt_label = 'ON' if notes else 'OFF'
    nt_style, nt_attr = _mobile_avail_attrs(av.get('notes'))
    ig_bg = '#4caf50' if image_generation else '#607d8b'
    ig_label = 'ON' if image_generation else 'OFF'
    ig_style, ig_attr = _mobile_avail_attrs(av.get('image_generation'))
    ds_bg = '#4caf50' if deep_search else '#607d8b'
    ds_label = 'ON' if deep_search else 'OFF'
    ds_style, ds_attr = _mobile_avail_attrs(av.get('deep_search'))
    el_bg = '#4caf50' if elyth else '#607d8b'
    el_label = 'ON' if elyth else 'OFF'
    el_style, el_attr = _mobile_avail_attrs(av.get('elyth'))
    # レイアウト(2列グリッド)は #mobile-utility-panel のCSSが担当。インラインは
    # 状態依存の背景色のみ(JSも style.backgroundColor だけを更新する)。
    return f"""
    <div id="mobile-utility-panel">
        <button id="mobile-talk-theme-toggle-btn" style="background-color:{tt_bg};">
            {t('utility.talk_theme', state=tt_label)}
        </button>
        <button id="mobile-speechless-toggle-btn" style="background-color:{sl_bg};">
            {t('utility.speechless', state=sl_label)}
        </button>
        <button id="mobile-notes-toggle-btn" style="background-color:{nt_bg};{nt_style}"{nt_attr}>
            {t('utility.notes', state=nt_label)}
        </button>
        <button id="mobile-image-gen-toggle-btn" style="background-color:{ig_bg};{ig_style}"{ig_attr}>
            {t('utility.imagegen', state=ig_label)}
        </button>
        <button id="mobile-elyth-toggle-btn" style="background-color:{el_bg};{el_style}"{el_attr}>
            {t('utility.elyth', state=el_label)}
        </button>
        <button id="mobile-deep-search-toggle-btn" style="background-color:{ds_bg};{ds_style}"{ds_attr}>
            {t('utility.deepsearch', state=ds_label)}
        </button>
    </div>
    """


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def _build_char_select_html(options=None, selected_value=None):
    """Build native <select> HTML for character selection (iOS compatible)."""
    opts_html = f'<option value="">{t("mobile.select_character")}</option>'
    if options:
        for name, char_id in options:
            if not char_id:
                continue
            selected = ' selected' if char_id == selected_value else ''
            # Escape HTML entities in the name
            safe_name = str(name).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            safe_id = str(char_id).replace('&', '&amp;').replace('"', '&quot;')
            opts_html += f'<option value="{safe_id}"{selected}>{safe_name}</option>'
    return f'<select id="mobile-char-select">{opts_html}</select>'


def _mobile_handle_attach(files, current_images, current_docs):
    """Route attached documents to document_buffer.

    Phase 1A: Images are filtered out by the JS attach hook and sent via WebSocket
    (`attach_image` action) — they never reach this handler. As a defensive
    fallback, image-extension files are silently ignored here so the buffer
    cannot be polluted if DataTransfer unavailability lets one through.

    The attach-status div is not updated here (JS-owned). The server-side
    `_broadcast_image_slot_update` is the single source of truth — it reads
    both image_buffer and document_buffer counts and pushes one combined text
    to all clients via WebSocket.
    """
    import os as _os
    from backend.shared.constants import (
        VALID_ATTACHMENT_IMAGE_EXTENSIONS,
        VALID_ATTACHMENT_DOCUMENT_EXTENSIONS,
    )
    if not files:
        return [], current_docs or []

    docs = list(current_docs or [])
    doc_added = False

    for f in files:
        path = f if isinstance(f, str) else getattr(f, 'name', str(f))
        ext = _os.path.splitext(path)[1].lower()

        if ext in VALID_ATTACHMENT_IMAGE_EXTENSIONS or ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            # JS hook should have intercepted; ignore as defensive fallback
            continue
        elif ext in VALID_ATTACHMENT_DOCUMENT_EXTENSIONS:
            from backend.shared.document_extractor import extract_text
            result = extract_text(path)
            if not result["success"]:
                logger.warning(f"[Mobile] Document extraction failed: {result['error']}")
                _notify_attach_rejected(result["filename"], result.get("error_code", ""))
                continue
            doc_info = {
                "filename": result["filename"],
                "text": result["text"],
                "char_count": result["char_count"],
                "file_path": path,
            }
            docs.append(doc_info)
            try:
                from backend.backend import _backend_state
                if _backend_state:
                    _backend_state.document_buffer.add_document(
                        file_path=path,
                        filename=result["filename"],
                        text=result["text"],
                        char_count=result["char_count"],
                        source="mobile",
                    )
                    doc_added = True
            except Exception:
                pass
        else:
            # Unsupported extension — 無言スキップせず通知(稜裁定 2026-08-15)
            logger.warning(f"[Mobile] Unsupported attachment extension: {path}")
            _notify_attach_rejected(_os.path.basename(path), "unsupported")

    # Trigger broadcast so JS-managed attach-status reflects new doc count
    if doc_added:
        try:
            from backend.backend import _backend_state
            from backend.server.websocket_server import get_websocket_manager
            if _backend_state:
                get_websocket_manager().broadcast_image_slot_update_sync(
                    _backend_state.image_buffer.get_count()
                )
        except Exception:
            pass

    return [], docs


def _get_current_theme_text():
    """Read current talk theme directly from backend (not cache)."""
    try:
        result = backend.get_talk_theme()
        if result.get("success") and result.get("theme"):
            return result["theme"]
    except Exception as e:
        logger.warning(f"[Mobile] get_talk_theme failed: {e}")
    return t('mobile.theme_none')


def mobile_handle_text_send(text_value, images, documents):
    """Bridge: call handle_text_input with native textarea value, return mobile outputs."""
    result = handle_text_input(text_value, images, documents)
    # result = (cleared_text, status_markdown, cleared_images, cleared_docs, cleared_attach_status)
    # Skip cleared_text (index 0) – native textarea already cleared by JS.
    # Skip cleared_attach_status (index 4) – attach-status div is JS-owned
    # (WS image_slot_update broadcast resets it after send).
    return result[1], result[2], result[3]


def mobile_switch_character(char_id):
    """Switch character and return mobile-relevant outputs (chat, error status)."""
    if not char_id:
        return get_chat_history(), ""
    try:
        switch_character(char_id)  # Return values ignored; use app_state
    except Exception as e:
        logger.warning(f"[Mobile] switch_character error (ignored): {e}")
    logger.info(f"[Mobile] Character switched: {app_state.active_character_id}")

    # LLM未接続(遅延再生成待ち)はキャラ読み込み時点で知らせる(稜依頼
    # 2026-08-03: 初回送信まで無言だと気づけない)。表示はエラー時のみ
    # 出るステータストースト(チェーン末尾のERROR_SHOW JS)が担う。
    status = ""
    try:
        if app_state.active_character_id == char_id:
            llm_check = backend.is_llm_ready(char_id)
            if isinstance(llm_check, dict) and llm_check.get('result') is False:
                status = error_status_text(
                    t('charui.llm_not_ready_title') + ": "
                    + t('err.service_unavailable'))
    except Exception as llm_check_err:
        logger.debug(f"[Mobile] LLM readiness check failed (ignored): {llm_check_err}")
    return get_chat_history(), status


def mobile_toggle_start_end():
    """Toggle Start / End Connection and return mobile outputs."""
    toggle_start_end()
    is_started = app_state.conversation_started
    logger.info(f"[Mobile] toggle_start_end: started={is_started}, char={app_state.active_character_id}")
    return (
        t('btn.end_conversation') if is_started else t('btn.start_conversation'),
        get_chat_history(),
        "🟢" if is_started else "🔴",
        _get_current_theme_text(),
    )


def mobile_update_theme(new_theme):
    """Update talk theme, return mobile outputs."""
    logger.info(f"[Mobile] update_theme called with: '{new_theme}'")
    if not new_theme or not new_theme.strip():
        logger.warning("[Mobile] update_theme: empty theme, ignoring")
        return _get_current_theme_text(), get_chat_history()
    result = update_talk_theme_click(new_theme, app_state, backend)
    # result: (current_display, input_clear, error_update, input_update)
    display_text = result[0]
    logger.info(f"[Mobile] update_theme result: '{display_text}'")
    # display_text is the theme value or "No Talk Theme" on error
    return display_text if display_text != t('gen.no_talk_theme') else t('mobile.theme_none'), get_chat_history()


def mobile_clear_theme():
    """Clear talk theme, return mobile outputs."""
    logger.info("[Mobile] clear_theme called")
    result = clear_talk_theme_click(app_state, backend)
    # result: (current_display, input_update, error_update)
    display_text = result[0]
    logger.info(f"[Mobile] clear_theme result: '{display_text}'")
    return t('mobile.theme_none'), get_chat_history()


def mobile_disconnect():
    """Force disconnect session."""
    try:
        from backend.server.session_manager import (
            get_session_manager, CLOSE_CODE_SELF_DISCONNECT,
        )
        sm = get_session_manager()
        # 自己切断: admin kick既定のままだと自分の画面に「管理者により
        # 切断されました」が出る(稜実機 2026-07-30)
        result = sm.force_disconnect(
            close_code=CLOSE_CODE_SELF_DISCONNECT, message="接続を解除しました"
        )
        if result:
            return f"<div style='color:#4caf50;padding:10px;'>{t('mobile.disconnected')}</div>"
        else:
            return f"<div style='color:#ff9800;padding:10px;'>{t('admin.no_active_session')}</div>"
    except Exception as e:
        return f"<div style='color:#f44336;padding:10px;'>{t('common.error_with', error=e)}</div>"


def mobile_switch_page(page_id):
    """Return visibility updates for pages."""
    pages = ["conversation", "logs", "system"]
    return [gr.update(visible=(p == page_id)) for p in pages]


def mobile_on_load():
    """Page load initialiser – returns latest state for all components."""
    try:
        # Build native <select> HTML with character list
        char_options = refresh_char_list()
        logger.info(f"[Mobile] on_load: refresh_char_list returned {len(char_options)} items")
        valid_opts = [o for o in char_options if o[1]]  # Filter out empty IDs
        logger.info(f"[Mobile] on_load: {len(valid_opts)} valid characters")
        char_select = _build_char_select_html(valid_opts, app_state.active_character_id)

        theme_text = _get_current_theme_text()
        btn_label = t('btn.end_conversation') if app_state.conversation_started else t('btn.start_conversation')
        status_icon = "🟢" if app_state.conversation_started else "🔴"

        # Build utility panel with current feature toggle states from backend
        from backend.shared.feature_availability import get_block_reasons
        feature_status = backend.get_feature_status()
        utility_html = _build_utility_panel_html(
            talk_theme=feature_status.get("talk_theme_enabled", True),
            speechless=feature_status.get("speechless_enabled", False),
            notes=feature_status.get("notes_enabled", False),
            image_generation=feature_status.get("image_generation_enabled", False),
            deep_search=feature_status.get("deep_search_enabled", False),
            elyth=feature_status.get("elyth_enabled", False),
            availability=get_block_reasons(),
        )

        return (
            char_select,                                                 # char_select_html
            theme_text,                                                  # current_theme_display
            gr.update(value=app_state.tts_volume * 100),                 # tts_volume_slider
            get_chat_history(),                                          # chat_display
            btn_label,                                                   # start_end_btn
            status_icon,                                                 # connection_status
            utility_html,                                                # utility_panel
        )
    except Exception as e:
        logger.error(f"[Mobile] on_load FAILED: {e}", exc_info=True)
        # Return safe defaults so the page still renders
        return (
            _build_char_select_html(),  # empty select
            t('mobile.theme_none'),
            gr.update(),
            "",
            t('btn.start_conversation'), "🔴",
            _build_utility_panel_html(),                                 # utility_panel (defaults)
        )


# ---------------------------------------------------------------------------
# Main interface builder
# ---------------------------------------------------------------------------
def create_mobile_interface(server_mode_enabled: bool = False) -> gr.Blocks:
    """Create the mobile Gradio interface.

    Args:
        server_mode_enabled: Kept for call-signature symmetry with
            create_gradio_interface(). Mobile no longer branches on it —
            log pages are always manual-refresh (帯域節約・稜裁定2026-08-02)
            and mobile is mounted only in server mode in practice.
    """

    # Phase 2C: inject ws-overlay (style + DOM) on page load
    mobile_page_load_js = """
    () => {
        // Android Chrome 108+の既定(resizes-visual)ではソフトキーボードが
        // 下部固定の入力行を覆う。resizes-contentでレイアウトビューポートを
        // 縮ませ、fixed要素をキーボード上に出す(iOS/デスクトップは未知キーを
        // 無視するため無害。Gradioのmetaにはこの指定が無いことを実測済み)
        const vpMeta = document.querySelector('meta[name="viewport"]');
        if (vpMeta && vpMeta.content.indexOf('interactive-widget') === -1) {
            vpMeta.content += ', interactive-widget=resizes-content';
        }
        if (!document.getElementById('ws-overlay-style')) {
            const style = document.createElement('style');
            style.id = 'ws-overlay-style';
            style.textContent = ''
                + '.ws-overlay { position: fixed; inset: 0; z-index: 100000;'
                + '   background: rgba(0,0,0,0.65); display: none;'
                + '   align-items: center; justify-content: center;'
                + '   font-family: inherit; }'
                + '.ws-overlay.visible { display: flex; }'
                + '.ws-overlay-card { background: #1a1a1a; color: #fff;'
                + '   padding: 32px 28px; border-radius: 12px;'
                + '   max-width: min(420px, calc(100vw - 32px)); text-align: center;'
                + '   box-shadow: 0 8px 32px rgba(0,0,0,0.4); }'
                + '.ws-overlay-icon { font-size: 48px; margin-bottom: 12px;'
                + '   line-height: 1; display: inline-block; }'
                + '.ws-overlay-icon.spinning { animation: ws-spin 1s linear infinite; }'
                + '@keyframes ws-spin { to { transform: rotate(360deg); } }'
                + '.ws-overlay-title { font-size: 20px; margin: 0 0 12px; font-weight: 600; }'
                + '.ws-overlay-message { font-size: 14px; margin: 0 0 16px;'
                + '   opacity: 0.85; line-height: 1.5; }'
                + '.ws-overlay-detail { font-size: 12px; opacity: 0.6;'
                + '   margin: 0 0 16px; min-height: 1em; }'
                + '.ws-overlay-action { background: #2563eb; color: #fff; border: none;'
                + '   padding: 10px 24px; border-radius: 6px; cursor: pointer;'
                + '   font-size: 14px; font-weight: 500; }';
            document.head.appendChild(style);
        }
        if (!document.getElementById('ws-overlay')) {
            const overlay = document.createElement('div');
            overlay.id = 'ws-overlay';
            overlay.className = 'ws-overlay';
            overlay.innerHTML = ''
                + '<div class="ws-overlay-card">'
                + '<div class="ws-overlay-icon" id="ws-overlay-icon"></div>'
                + '<h2 class="ws-overlay-title" id="ws-overlay-title"></h2>'
                + '<p class="ws-overlay-message" id="ws-overlay-message"></p>'
                + '<p class="ws-overlay-detail" id="ws-overlay-detail"></p>'
                + '<button class="ws-overlay-action" id="ws-overlay-action"'
                + ' style="display:none;" onclick="location.reload()">リロード</button>'
                + '</div>';
            document.body.appendChild(overlay);
        }

        // Phase 4D: install showNotification (mobile variant). Bottom-anchored
        // (bottom:120px) to clear the input row + record button. textContent
        // for XSS safety since the message comes from a logger record.
        if (!window.showNotification) {
            window.showNotification = function(title, message, type, durationMs) {
                type = type || 'info';
                durationMs = durationMs || 4000;
                const notif = document.createElement('div');
                notif.className = 'popup-container popup-' + type;
                notif.style.cssText = ''
                    + 'position:fixed;bottom:120px;left:10px;right:10px;'
                    + 'z-index:9999;max-width:none;';
                const h3 = document.createElement('h3');
                h3.textContent = title;
                const p = document.createElement('p');
                p.textContent = message;
                notif.appendChild(h3);
                notif.appendChild(p);
                document.body.appendChild(notif);
                setTimeout(function() { notif.remove(); }, durationMs);
            };
        }
    }
    """.replace('リロード', t('mobile.reload'))

    mobile_demo = gr.Blocks(
        title="AG Mobile",
        theme=gr.themes.Soft(
            primary_hue="blue",
            # デスクトップUIと同じ同梱Source Sans 3(文字列=LocalFont指定・
            # @font-faceはLOCAL_FONTS_CSSが供給。外部通信ゼロ方針=稜裁定2026-07-19)
            font=["Source Sans 3", "ui-sans-serif", "system-ui", "sans-serif"],
        ),
        css=LOCAL_FONTS_CSS + get_mobile_css(),
        # CHAT_CSS_BODY supplies the chat-message styling that get_chat_history()
        # no longer prepends inline (Plan F). Routed via head= (not css=) so it
        # lands at the end of <head>, after Gradio's stylesheets — required for
        # cascade priority over Gradio's dark-mode defaults.
        head=(
            '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
            f'<style>{CHAT_CSS_BODY}</style>'
        ),
        js=mobile_page_load_js,
    )

    with mobile_demo:
        # ---- Backdrop ----
        gr.HTML('<div id="mobile-menu-backdrop"></div>')

        # ---- Hamburger Menu ----
        with gr.Column(visible=True, elem_id="mobile-hamburger-menu") as hamburger_menu:
            nav_conversation = gr.Button(f"💬 {t('conv.title')}", elem_classes=["mobile-nav-btn", "active"])
            nav_logs = gr.Button(t('logs.tab.system'), elem_classes=["mobile-nav-btn"])
            nav_system = gr.Button(t('mobile.nav_system'), elem_classes=["mobile-nav-btn"])

            start_end_btn = gr.Button(t('btn.start_conversation'), elem_id="mobile-start-end-btn",
                                      variant="primary")

            utility_panel = gr.HTML(value=_build_utility_panel_html())

            gr.Markdown(f"**{t('mobile.talk_theme_setting')}**")
            gr.Markdown(t('mobile.current_theme'), elem_classes=["mobile-label"])
            current_theme_display = gr.Markdown(t('mobile.theme_none'), elem_id="mobile-current-theme")
            gr.Markdown(t('mobile.new_theme'), elem_classes=["mobile-label"])
            theme_input_html = gr.HTML(
                value=f'<textarea id="mobile-theme-textarea" placeholder="{t("mobile.theme_placeholder")}" rows="2"></textarea>',
                elem_id="mobile-theme-textarea-container",
            )
            with gr.Row():
                theme_update_btn = gr.Button(t('mobile.theme_update'), size="sm", elem_id="mobile-theme-update-btn")
                theme_clear_btn = gr.Button(t('mobile.theme_clear'), size="sm", elem_id="mobile-theme-clear-btn")

        # ---- Header (hamburger + character select + connection status) ----
        with gr.Row(elem_id="mobile-header-row"):
            hamburger_btn = gr.Button("☰", elem_id="mobile-hamburger-btn")
            char_select_html = gr.HTML(
                value=_build_char_select_html(),
                elem_id="mobile-char-select-container",
            )
            connection_status = gr.HTML("🔴", elem_id="mobile-connection-status")

        # Hidden textboxes for JS→Python bridges (CSS hidden, Svelte stays active)
        char_hidden = gr.Textbox(value="", elem_id="mobile-char-hidden", show_label=False)
        theme_hidden = gr.Textbox(value="", elem_id="mobile-theme-hidden", show_label=False)

        # ---- Page: Conversation (visible=True) ----
        with gr.Column(visible=True, elem_id="mobile-page-conversation") as page_conversation:
            chat_display = gr.HTML(elem_id="chat-display")

        # ---- Input dock (always visible, outside page containers) ----
        # 固定コンテナ(縦積み): 入力行の下に添付ステータスが通常フローで並ぶ
        # (コンパニオンバーと同型。旧実装の固定座標bottom:56pxは入力バーの
        # 実高に届かず表示が丸ごとバー背後に隠れていた)
        with gr.Column(elem_id="mobile-input-dock"):
            with gr.Row(elem_id="mobile-input-row"):
                # Visible 📎 opens the attach action menu (file attach / location send).
                # Handled by JS delegation in create_mobile_ws_js — no Gradio wiring.
                gr.Button("📎", elem_id="mobile-attach-btn")
                # Real UploadButton (CSS-hidden; clicked programmatically by the menu)
                attach_btn = gr.UploadButton(
                    "📎",
                    # 明示5拡張子=JS判定リストと一致(.heic等の無言消滅対策)。
                    # PDF/DOCXは動作検証未了のため除外(稜裁定 2026-08-15)
                    file_types=[
                        ".png", ".jpg", ".jpeg", ".gif", ".webp",
                        ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
                        ".log", ".ini", ".toml",
                        ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp",
                        ".h", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".bat", ".ps1",
                    ],
                    file_count="multiple",
                    elem_id="mobile-attach-upload",
                )
                # Native <textarea> for text input (iOS compatible – gr.Textbox renders 0x0)
                text_input_html = gr.HTML(
                    value=f'<textarea id="mobile-native-input" placeholder="{t("conv.text_placeholder_locked")}" rows="1" disabled></textarea>',
                    elem_id="mobile-text-input-container",
                )
                send_btn = gr.Button("➤", elem_id="mobile-send-btn")
            # 添付ステータスはJS専有の素div(書き手はWS image_slot_update系のみ)。
            # gr.Markdown("")は空値だと<p>を描画しないためJSセレクタが空振りする
            gr.HTML('<div id="mobile-attach-status"></div>',
                    elem_id="mobile-attach-status-wrap")

        # 📎 action menu (opened by the input-row clip; clicks handled by
        # JS delegation in create_mobile_ws_js — Gradio strips onclick attrs)
        gr.HTML(f"""
        <div id="mobile-attach-menu">
          <button id="mobile-attach-menu-file" class="mobile-attach-menu-item">📎 {t('mobile.attach_menu_file')}</button>
          <button id="mobile-attach-menu-location" class="mobile-attach-menu-item">🗺 {t('mobile.attach_menu_location')}</button>
        </div>
        """)

        # Hidden textbox for JS→Python text bridge (CSS hidden, Svelte stays active)
        text_hidden = gr.Textbox(value="", elem_id="mobile-text-hidden", show_label=False)

        # モバイルの唯一の会話エラー表示面(稜裁定 2026-08-03)。DOM上はこの
        # 位置(通常フロー)だが、CSSの position:fixed で入力ドック直上の
        # トースト位置に移設されている。可視化はチェーン末尾JS
        # (MOBILE_STATUS_ERROR_SHOW_JS)の「❌始まりのときだけ約10秒」が唯一の
        # 経路で、送信済み/✅等の非エラー文言も書き込まれるが表示されない。
        # fixed を外すとチャット領域内に浮いて出る(2026-08-03実測)ので、
        # 位置・可視化を変えるときは CSS(#mobile-text-status)とJSを必ず対で。
        text_status = gr.Markdown("", elem_id="mobile-text-status")
        images_state = gr.State([])
        documents_state = gr.State([])

        # ---- Companion Camera Bar (hidden by default, shown in companion mode via CSS) ----
        gr.HTML(f"""
        <div id="companion-camera-bar">
          <div id="companion-text-row">
            <textarea id="companion-text-input" placeholder="{t('mobile.connecting')}" rows="1" disabled></textarea>
            <button id="companion-send-btn" onclick="window._companionSendText()" disabled>➤</button>
          </div>
          <div class="companion-btn-row">
            <button class="companion-btn" id="companion-location-btn" onclick="window._companionSendLocation()">🗺</button>
            <button class="companion-btn" onclick="window._companionOpenAttach()">📎</button>
            <button class="companion-btn" onclick="window._companionOpenCamera()">📷</button>
          </div>
          <div id="companion-attach-status"></div>
          <input type="file" id="companion-camera-input" accept="image/*" capture="environment" style="display:none">
          <input type="file" id="companion-attach-input" accept="image/*" multiple style="display:none">
        </div>
        """)

        # ---- Page: System Logs (visible=False) ----
        with gr.Column(visible=False, elem_id="mobile-page-logs") as page_logs:
            with gr.Tabs():
                with gr.Tab(t('logs.tab.system')):
                    # モバイルのログは常に手動更新(帯域節約・稜裁定2026-08-02)。
                    # ポーリングタイマーは置かない
                    gr.Markdown(t('logs.manual_refresh_note'))
                    log_container, log_components = create_log_panel()
                    log_container.visible = True
                    log_textbox = log_components["log_textbox"]
                    log_textbox.value = update_log_view()
                    if "refresh_logs_btn" in log_components:
                        log_components["refresh_logs_btn"].click(
                            fn=update_log_view, outputs=[log_textbox]
                        )

                with gr.Tab(t('logs.tab.prompt')):
                    with gr.Column():
                        gr.Markdown(f"### {t('logs.prompt_title')}")
                        gr.Markdown(t('logs.manual_refresh_note'))
                        with gr.Row():
                            gr.Markdown("")
                            prompt_refresh_btn = gr.Button(t('common.refresh'), size="sm", scale=0)
                        prompt_textbox = gr.Textbox(
                            label="", value=update_prompt_view(),
                            lines=20, max_lines=40, interactive=False,
                            show_copy_button=True,
                        )
                        prompt_refresh_btn.click(fn=update_prompt_view, outputs=[prompt_textbox])

        # ---- Page: System (visible=False) ----
        with gr.Column(visible=False, elem_id="mobile-page-system") as page_system:
            # Disconnect
            gr.Markdown(f"### {t('system.disconnect')}")
            disconnect_btn = gr.Button(f"🔌 {t('system.disconnect')}", variant="stop")
            disconnect_status = gr.HTML("")

            # TTS Volume
            gr.Markdown(f"### {t('conv.tts_volume')}")
            tts_volume_slider = gr.Slider(
                minimum=0, maximum=100, step=1,
                value=app_state.tts_volume * 100,
                label=t('mobile.volume_pct'),
            )
            tts_test_btn = gr.Button(t('conv.test_voice'), size="sm")
            tts_test_result = gr.Markdown("", elem_id="mobile-tts-test-result")

            # AGPL-3.0 section 13: offer the Corresponding Source to every user
            # who reaches this program over a network (mounted at "/mobile").
            gr.HTML(license_notice_html(), elem_id="ag-license-notice")

        # ---- Hidden triggers (WS → Gradio bridge) ----
        with gr.Row(visible=False, elem_id="mobile-hidden-triggers"):
            ws_update_trigger = gr.Button("t", elem_id="ws-update-trigger", visible=False)
            ws_status_update_trigger = gr.Button("t", elem_id="ws-status-update-trigger", visible=False)
            extraction_status_trigger = gr.Button("t", elem_id="extraction-status-trigger", visible=False)
            char_switch_trigger = gr.Button("t", elem_id="mobile-char-switch-trigger", visible=False)
            text_send_trigger = gr.Button("t", elem_id="mobile-text-send-trigger", visible=False)

        # ======================================================================
        # EVENT WIRING
        # ======================================================================

        # -- Hamburger button --
        hamburger_btn.click(
            fn=lambda: None, inputs=[], outputs=[],
            js="() => window.toggleMobileMenu()",
        )

        # -- Navigation --
        page_outputs = [page_conversation, page_logs, page_system]

        def _nav(page_id):
            return mobile_switch_page(page_id)

        nav_conversation.click(
            fn=lambda: _nav("conversation"),
            inputs=[], outputs=page_outputs,
            js="() => window.closeMobileMenu()",
        )
        nav_logs.click(
            fn=lambda: _nav("logs"),
            inputs=[], outputs=page_outputs,
            js="() => window.closeMobileMenu()",
        )
        nav_system.click(
            fn=lambda: _nav("system"),
            inputs=[], outputs=page_outputs,
            js="() => window.closeMobileMenu()",
        )

        # -- Start/End Connection --
        start_end_btn.click(
            fn=mobile_toggle_start_end,
            inputs=[],
            outputs=[start_end_btn, chat_display, connection_status, current_theme_display],
            js="""() => {
                window.closeMobileMenu();
                if (!window.ttsAudioContext) {
                    window.ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
                }
                window.ttsAudioContext.resume().then(() => { window.ttsAudioEnabled = true; });
            }""",
        ).then(
            fn=get_chat_history,
            inputs=[],
            outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        )

        # -- Talk Theme (direct wiring on visible buttons with async JS bridge) --
        # The hidden trigger pattern failed for theme_update_trigger (Gradio didn't
        # bind the event handler). Instead, wire the visible buttons directly.
        # Async JS bridges native textarea → hidden Gradio textbox, then awaits
        # 200ms for Svelte to process before Gradio reads inputs.
        # JS return value replaces the input to Python fn.
        # Synchronous JS — async functions break Gradio's event pipeline.
        theme_update_btn.click(
            fn=mobile_update_theme,
            inputs=[theme_hidden],
            outputs=[current_theme_display, chat_display],
            js="""(current_value) => {
                const ta = document.getElementById('mobile-theme-textarea');
                const text = ta ? ta.value.trim() : '';
                if (ta) ta.value = '';
                window.closeMobileMenu && window.closeMobileMenu();
                console.log('[Mobile] Theme update JS returning:', text);
                return text;
            }""",
        ).then(
            fn=get_chat_history,
            inputs=[], outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        )
        theme_clear_btn.click(
            fn=mobile_clear_theme,
            inputs=[],
            outputs=[current_theme_display, chat_display],
            js="""() => {
                const ta = document.getElementById('mobile-theme-textarea');
                if (ta) ta.value = '';
                window.closeMobileMenu && window.closeMobileMenu();
                console.log('[Mobile] Theme clear btn clicked');
            }""",
        ).then(
            fn=get_chat_history,
            inputs=[], outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        )

        # -- Character switch (native <select> → JS sets hidden textbox → trigger reads it) --
        char_switch_trigger.click(
            fn=mobile_switch_character,
            inputs=[char_hidden],
            outputs=[chat_display, text_status],
        ).then(
            # キャラ読み込み時点のLLM未接続警告(❌のときだけ約10秒表示)
            fn=lambda: None, inputs=[], outputs=[],
            js=MOBILE_STATUS_ERROR_SHOW_JS, queue=False
        ).then(
            fn=wait_for_history_load,
            inputs=[], outputs=[],
        ).then(
            fn=get_chat_history,
            inputs=[],
            outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        )

        # -- Attachment (images + documents) --
        attach_btn.upload(
            fn=_mobile_handle_attach,
            inputs=[attach_btn, images_state, documents_state],
            outputs=[images_state, documents_state],
        )

        # -- Text send (native textarea → JS bridge → hidden trigger) --
        # send_btn click and Enter key are handled by JS event delegation
        # which calls _mobileSendText() → sets text_hidden → clicks text_send_trigger
        text_send_trigger.click(
            fn=mobile_handle_text_send,
            inputs=[text_hidden, images_state, documents_state],
            outputs=[text_status, images_state, documents_state],
        ).then(
            # 前回のエラー表示/タイマーをリセット(js専用then)
            fn=lambda: None, inputs=[], outputs=[],
            js=MOBILE_STATUS_RESET_JS, queue=False
        ).then(
            fn=get_chat_history,
            inputs=[], outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        ).then(
            fn=start_text_generation,
            inputs=[], outputs=[text_status],
        ).then(
            fn=get_chat_history,
            inputs=[], outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        ).then(
            fn=process_text_generation,
            inputs=[], outputs=[text_status],
        ).then(
            fn=get_chat_history,
            inputs=[], outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        ).then(
            # エラー(❌)のときだけ約10秒表示して自動で隠す(稜裁定 2026-08-02)
            fn=lambda: None, inputs=[], outputs=[],
            js=MOBILE_STATUS_ERROR_SHOW_JS, queue=False
        )

        # -- WS trigger: chat update --
        ws_update_trigger.click(
            fn=get_chat_history,
            inputs=[], outputs=[chat_display],
            js=MOBILE_SCROLL_JS,
        )

        # -- WS trigger: status update (no-op for mobile, but keep for consistency) --
        ws_status_update_trigger.click(
            fn=lambda: None, inputs=[], outputs=[],
        )

        # -- WS trigger: extraction --
        extraction_status_trigger.click(
            fn=lambda: None, inputs=[], outputs=[],
        )

        # -- System page: Disconnect --
        disconnect_btn.click(
            fn=mobile_disconnect,
            inputs=[], outputs=[disconnect_status],
        )

        # -- System page: TTS Volume --
        tts_volume_slider.change(
            fn=update_tts_volume,
            inputs=[tts_volume_slider],
            outputs=[tts_test_result],
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=status_auto_hide_js('mobile-tts-test-result'), queue=False
        )
        # js: 再生用AudioContextはStart Connection押下時にしか作られないため、
        # 接続前にTest Voiceを押すと無音だった。押下=ユーザージェスチャで
        # 生成/resumeしておく(iOS Safariの自動再生ポリシー対策)。
        tts_test_btn.click(
            fn=test_tts_voice,
            inputs=[], outputs=[tts_test_result],
            js="""
            () => {
                try {
                    if (!window.ttsAudioContext) {
                        window.ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
                    }
                    window.ttsAudioContext.resume().then(() => {
                        window.ttsAudioEnabled = true;
                        console.log('[TTS] Audio enabled via Test Voice tap');
                    }).catch(() => {});
                } catch (e) {}
            }
            """,
        ).then(
            fn=lambda: None, inputs=[], outputs=[],
            js=status_auto_hide_js('mobile-tts-test-result'), queue=False
        )

        # ======================================================================
        # PAGE LOAD
        # ======================================================================
        # Python initialisation (character list, settings, etc.)
        mobile_demo.load(
            fn=mobile_on_load,
            inputs=[],
            outputs=[
                char_select_html,
                current_theme_display,
                tts_volume_slider,
                chat_display,
                start_end_btn,
                connection_status,
                utility_panel,
            ],
        )
        # WebSocket client initialisation. js は値を返さない(返すと 0引数
        # エンドポイントへの引数超過でコンソールエラー・2026-08-03 CDP実測)。
        # lambda *args は防御のまま温存。
        mobile_demo.load(
            fn=lambda *args: None,
            js=create_mobile_ws_js(),
        )

    return mobile_demo
