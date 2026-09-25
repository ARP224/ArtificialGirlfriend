"""
ui/components.py

UI component builders and styling constants for the Artificial Girlfriend UI.
This module contains functions to create various UI components and format displays.
"""

import os
import logging
import base64
import gradio as gr
from typing import Tuple, Dict, Any, Optional, List
from pathlib import Path

from backend.shared.i18n import t, current_language
from backend.tools.motion_pngtuber_launcher import list_asset_folders
from .state import app_state
from .constants import DEFAULT_ICON_PATH
import backend

logger = logging.getLogger(__name__)


# CSS Theme Configuration
CSS_THEME = {
    'error': {'bg': '#2e1a1a', 'border': '#f44336', 'text': '#ef9a9a'},
    'warning': {'bg': '#2e2a1a', 'border': '#ffc107', 'text': '#ffe082'},
    'info': {'bg': '#1a2a3e', 'border': '#2196f3', 'text': '#90caf9'}
}


def generate_message_css() -> str:
    """Generate CSS for all message and popup types from theme configuration."""
    css = ""
    for level, colors in CSS_THEME.items():
        css += f"""
.{level}-message {{
    background-color: {colors['bg']};
    border-left: 4px solid {colors['border']};
    padding: 10px 15px;
    margin: 10px 0;
    border-radius: 4px;
}}
.popup-{level} {{
    background-color: {colors['bg']};
    border-left: 4px solid {colors['border']};
    color: {colors['text']};
}}
"""
    # Add prompt log specific CSS
    css += """
/* Prompt Log Panel Styling */
#prompt-log-panel textarea {
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    background-color: #232323;
    border: 1px solid #3a3a3a;
    border-radius: 4px;
    padding: 12px;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-wrap: break-word;
    color: #e0e0e0;
}

#prompt-log-panel .label-wrap {
    display: none;
}

.prompt-section-header {
    font-weight: bold;
    color: #b0b0b0;
    background-color: #303030;
    padding: 4px 8px;
    border-radius: 3px;
    margin: 8px 0;
}

/* WebSocket hidden buttons */
#ws-update-trigger,
#ws-status-update-trigger {
    display: none !important;
    visibility: hidden !important;
    position: absolute !important;
    left: -9999px !important;
    pointer-events: none !important;
}

/* Pulse animation for visual feedback */
@keyframes pulse {
    0% {
        transform: scale(1);
        opacity: 1;
    }
    50% {
        transform: scale(1.05);
        opacity: 0.8;
    }
    100% {
        transform: scale(1);
        opacity: 1;
    }
}
"""
    return css


# CSS Styles
CHAT_CSS = f"""
<style>
/* Global dark-panel overrides — Gradio's defaults (neutral_800 = #27272a)
   render dropdowns / Groups in a bluish light gray that we want to suppress.
   Match the original near-black look (~#181818) with subtle borders. */
:root, .dark, .gradio-container, .gradio-container.dark {{
    --block-background-fill: #181818;
    --block-background-fill-dark: #181818;
    --background-fill-secondary: #181818;
    --background-fill-secondary-dark: #181818;
    --input-background-fill: #1e1e1e;
    --input-background-fill-dark: #1e1e1e;
    --panel-background-fill: #181818;
    --panel-background-fill-dark: #181818;
}}

/* Webkit scrollbar styling — default browser scrollbar is bright light-gray
   on dark themes. Match our panel palette. */
::-webkit-scrollbar {{
    width: 10px;
    height: 10px;
}}
::-webkit-scrollbar-track {{
    background: #181818;
}}
::-webkit-scrollbar-thumb {{
    background: #3a3a3a;
    border-radius: 5px;
    border: 2px solid #181818;
}}
::-webkit-scrollbar-thumb:hover {{
    background: #4a4a4a;
}}
::-webkit-scrollbar-corner {{
    background: #181818;
}}
* {{
    scrollbar-width: thin;
    scrollbar-color: #3a3a3a #181818;
}}

/* Conversation page sections.
   NOTE: Gradio 5.x's Svelte component injects `.gr-group.svelte-XXXXXX`
   with a hard-coded `background: #3F3F46` (Tailwind zinc-700). That has
   higher specificity than `.gr-group` alone, so we use !important. */
.gr-group {{
    border-radius: 8px;
    border: 1px solid #3a3a3a;
    padding: 16px;
    margin-bottom: 12px;
    background-color: #2a2a2a !important;
}}

/* Tab navigation bar — confirmed via F12: `div.tab-container.svelte-XXX`
   role=tablist. Make transparent so it inherits the parent's bg in any
   context (inside .gr-group => #2a2a2a, on bare page => page bg). */
.tab-container,
.tab-container[role="tablist"],
[role="tablist"] {{
    background-color: transparent !important;
    border-bottom-color: #3a3a3a !important;
}}

/* Tab content panel — keep transparent so it inherits .gr-group bg */
.tabitem {{
    background-color: transparent !important;
}}

/* Same #3F3F46 family (CDP実測 2026-07-25・最小再現+elementFromPointで特定):
   Gradio 5 の .form コンテナ(Slider/Checkbox等のフォーム束ね)は svelte が
   #3F3F46 をハードコードで塗る。上の :root 変数で子ブロックを #181818 に
   暗くした結果、.form 自身の背景が上端の隙間+枠線に「細長い明るいグレー帯」
   として露出していた(音声出力タブの音量調整パネル上端で稜実測)。 */
.form {{
    background-color: var(--block-background-fill) !important;
    border-color: #3a3a3a !important;
}}

/* CONFIRMED ROOT CAUSE (verified via Playwright DOM walk on a minimal repro):
   gr.Group renders as `<div class="gr-group"><div class="styler">...</div></div>`,
   and the inner `.styler` div paints the light-gray fill (#3F3F46) that bleeds
   through behind transparent Markdown titles. Override .styler to inherit
   the parent .gr-group's bg. */
.styler {{
    background-color: transparent !important;
    border: none !important;
}}

/* Markdown wrapper blocks — keep transparent so they don't paint another
   strip via --block-background-fill. (gr.Markdown is wrapped in .block.) */
.block:has(> .md) {{
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
}}

/* Input fields — slightly darker than panel for input affordance */
.gr-dropdown,
.gr-textbox,
.gr-textbox > textarea,
.gr-dropdown > .wrap,
input[type="text"],
textarea,
select {{
    background-color: #1a1a1a !important;
}}

/* Remove space between page title and first section */
#page-conversation .page-title {{
    margin-bottom: 5px !important;
    padding-bottom: 5px !important;
    font-size: 20px !important;
}}

/* Reduce top margin of first group */
#page-conversation .gr-group:first-of-type {{
    margin-top: 0 !important;
}}


/* Character info section - more compact */
#page-conversation .gr-group:first-child {{
    background-color: #2a2a2a !important;
    padding: 12px 16px !important;
}}

/* Chat display section */
#chat-display {{
    min-height: 400px;
    max-height: 500px;
    overflow-y: auto;
    overflow-x: hidden;
    background-color: #181818;
    border-radius: 8px;
    padding: 16px;
    border: 1px solid #3a3a3a;
    /* Add smooth scrolling */
    scroll-behavior: smooth;
    /* Improve scroll performance */
    will-change: scroll-position;
}}

/* Voice controls section */
#page-conversation .gr-group:last-child {{
    background-color: #2a2a2a !important;
}}

/* Status indicators */
.status-indicator {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin: 4px 0;
}}

/* Character info card */
.char-info-card {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px;
    background-color: #181818;
    border-radius: 8px;
    border: 1px solid #3a3a3a;
}}

/* Compact layout for character info section */
#page-conversation .gr-group:first-child .gr-markdown {{
    margin: 3px 0;
    line-height: 1.4;
    font-size: 14px;
}}

/* Character info column - better spacing */
#page-conversation .gr-group:first-child .gr-column:nth-child(3) {{
    padding-left: 15px;
}}

/* Force single row layout for character info section */
#page-conversation .gr-group:first-child {{
    min-width: 900px;
}}

/* Balance the character info section */
#page-conversation .gr-group:first-child .gr-row {{
    justify-content: space-between;
    align-items: center;
    flex-wrap: nowrap !important;
    overflow-x: auto;
    gap: 10px;
}}

/* Character dropdown - more compact */
#page-conversation .gr-dropdown {{
    margin-bottom: 0;
}}

#page-conversation .gr-dropdown label {{
    margin-bottom: 2px;
    font-size: 13px;
}}

/* Character icon - ensure proper display */
#page-conversation .gr-image {{
    border-radius: 10px;
    overflow: hidden;
    margin: 0 auto;
    display: block;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}}

/* Character icon container - center alignment */
#page-conversation .gr-group:first-child .gr-column:nth-child(2) {{
    display: flex;
    justify-content: center;
    align-items: center;
    padding: 0 10px;
}}

/* Connection status - compact spacing and no wrap */
#page-conversation .gr-column:last-child .gr-markdown {{
    font-size: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin: 3px 0;
    display: block;
    width: 100%;
    padding-right: 10px;
    line-height: 1.5;
}}

/* Connection status column - visual separation */
#page-conversation .gr-group:first-child .gr-column:last-child {{
    border-left: 1px solid #3a3a3a;
    padding-left: 15px;
}}

/* Conversation buttons */
.conversation-controls {{
    display: flex;
    gap: 8px;
    justify-content: center;
    margin-top: 12px;
}}

/* Voice recording button styles and animations */
#voice-record-btn {{
    font-size: 18px;
    padding: 12px 24px;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
}}

/* Live Camera status colours (the row itself lives in the utility panel;
   behaviour in ws_client_js) */
#ambient-cam-status {{ color: #90a4ae; }}
#ambient-cam-status.amb-ready {{ color: #64b5f6; }}
#ambient-cam-status.amb-capturing {{ color: #ffb300; }}
#ambient-cam-status.amb-attached {{ color: #4caf50; }}
#ambient-cam-status.amb-timeout {{ color: #ff7043; }}
#ambient-cam-status.amb-degraded {{ color: #ef5350; }}
#ambient-cam-status.amb-error {{ color: #ef5350; }}

/* Button states */
.voice-btn-ready {{
    background-color: #3b82f6 !important;
}}

.voice-btn-ready:hover {{
    background-color: #2563eb !important;
    transform: scale(1.02);
}}

.voice-btn-recording {{
    background-color: #ef4444 !important;
    animation: recordPulse 1.5s infinite;
}}

.voice-btn-processing {{
    background-color: #f59e0b !important;
    animation: processingPulse 1s infinite;
}}

/* Recording animations */
@keyframes recordPulse {{
    0% {{ 
        transform: scale(1); 
        box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7);
    }}
    50% {{ 
        transform: scale(1.02); 
        box-shadow: 0 0 0 10px rgba(239, 68, 68, 0);
    }}
    100% {{ 
        transform: scale(1); 
        box-shadow: 0 0 0 0 rgba(239, 68, 68, 0);
    }}
}}

@keyframes processingPulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.6; }}
}}

/* Microphone status indicator */
.mic-status-container {{
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 8px 16px;
    margin-top: 8px;
}}

.mic-status-indicator {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    border-radius: 20px;
    background-color: #303030;
    transition: all 0.3s ease;
}}

.mic-status-indicator.inactive {{
    background-color: #2a2a2a;
}}

.mic-status-indicator.ready {{
    background-color: #1a2a3e;
}}

.mic-status-indicator.recording {{
    background-color: #3a1a1a;
}}

.mic-status-indicator.processing {{
    background-color: #3a3020;
}}

/* Status dot animation */
.status-dot {{
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background-color: #6b7280;
    transition: all 0.3s ease;
}}

.mic-status-indicator.inactive .status-dot {{
    background-color: #6b7280;
}}

.mic-status-indicator.ready .status-dot {{
    background-color: #3b82f6;
}}

.mic-status-indicator.recording .status-dot {{
    background-color: #ef4444;
    animation: recordingDot 1s infinite;
}}

.mic-status-indicator.processing .status-dot {{
    background-color: #f59e0b;
    animation: processingDot 0.8s infinite;
}}

@keyframes recordingDot {{
    0%, 100% {{ transform: scale(1); opacity: 1; }}
    50% {{ transform: scale(1.2); opacity: 0.8; }}
}}

@keyframes processingDot {{
    0% {{ transform: rotate(0deg); }}
    100% {{ transform: rotate(360deg); }}
}}

/* Recording time display */
.recording-time {{
    text-align: center;
    margin-top: 8px;
    font-size: 14px;
    color: #ef4444;
    font-weight: 500;
}}

.recording-time-warning {{
    text-align: center;
    margin-top: 8px;
    font-size: 14px;
    color: #dc2626;
    font-weight: 600;
    animation: warningPulse 1s infinite;
}}

@keyframes warningPulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.6; }}
}}

/* Audio settings accordion */
#audio-settings {{
    margin-top: 8px;
}}

/* Microphone device select */
#mic-device-select {{
    margin-top: 8px;
}}

/* Chat container */
.chat-container {{
    display: flex;
    flex-direction: column;
    gap: 15px;
    font-family: Arial, sans-serif;
    padding: 10px;
}}
.ai-message {{
    display: flex;
    align-items: flex-start;
    margin-right: 20%;
}}
.char-icon {{
    width: 60px;
    height: 60px;
    border-radius: 50%;
    object-fit: cover;
    image-rendering: -webkit-optimize-contrast;
    image-rendering: crisp-edges;
    -ms-interpolation-mode: bicubic;
}}
.ai-icon-row {{
    flex-shrink: 0;
    margin-right: 12px;
}}
.char-name {{
    display: none;
}}
.message-content {{
    display: flex;
    flex-direction: column;
    flex: 1;
}}
.ai-bubble {{
    background-color: #082c41;
    color: #e0e0e0;
    padding: 12px 16px;
    border-radius: 18px;
    max-width: 80%;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
    line-height: 1.4;
}}
.user-message {{
    display: flex;
    justify-content: flex-end;
    margin-left: 20%;
}}
.user-bubble {{
    background-color: #305115;
    color: #e0e0e0;
    padding: 12px 16px;
    border-radius: 18px;
    max-width: 80%;
    text-align: left;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
    line-height: 1.4;
}}
.loading-spinner {{
    display: inline-block;
    width: 20px;
    height: 20px;
    border: 3px solid rgba(255,255,255,.15);
    border-radius: 50%;
    border-top-color: #aaaaaa;
    animation: spin 1s ease-in-out infinite;
}}
@keyframes spin {{
    to {{ transform: rotate(360deg); }}
}}

/* System message (feedback) styling */
.system-msg {{
    background-color: #303030;
    color: #9ca3af;
    padding: 8px 16px;
    margin: 12px auto;
    max-width: 70%;
    text-align: center;
    border-radius: 12px;
    font-style: italic;
    border: 1px solid #3a3a3a;
    /* Font size will be set dynamically to match other messages */
}}

/* Command execution message styling */
.command-msg {{
    background-color: #1a1a2e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a2a4a;
    font-size: 13px;
}}
.command-msg .cmd-header {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: bold;
    margin-bottom: 4px;
}}
.command-msg .cmd-reason {{
    color: #9ca3af;
    font-style: italic;
    margin-bottom: 4px;
    font-size: 12px;
}}
.command-msg details {{
    margin-top: 4px;
}}
.command-msg details summary {{
    cursor: pointer;
    color: #8888cc;
    font-size: 12px;
}}
.command-msg details pre {{
    background-color: #0d0d1a;
    color: #a0a0a0;
    padding: 8px;
    border-radius: 6px;
    margin-top: 4px;
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 150px;
    overflow-y: auto;
}}

/* Talk theme change message styling (AI-initiated) */
.talk-theme-msg {{
    background-color: #1a1a2e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #3a2a4a;
    font-size: 13px;
    text-align: center;
}}
.talk-theme-msg .theme-header {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: bold;
    color: #b06ab3;
    margin-bottom: 4px;
}}

/* Talk theme change message styling (User-initiated) */
.talk-theme-user-msg {{
    background-color: #1a1a2e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a3a4a;
    font-size: 13px;
    text-align: center;
}}
.talk-theme-user-msg .theme-header {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: bold;
    color: #6a8acd;
    margin-bottom: 4px;
}}

/* Command approval pending message styling */
.command-approval-msg {{
    background-color: #2a2210;
    color: #e0d0a0;
    padding: 12px 16px;
    margin: 10px auto;
    max-width: 80%;
    text-align: center;
    border-radius: 10px;
    border: 1px solid #5a4a20;
    font-size: 13px;
    animation: approval-pulse 2s ease-in-out infinite;
}}
.command-approval-msg .approval-header {{
    font-weight: bold;
    margin-bottom: 6px;
    color: #f0c040;
    font-size: 14px;
}}
.command-approval-msg .approval-command {{
    background-color: #1a1808;
    color: #c0b080;
    padding: 6px 10px;
    border-radius: 6px;
    margin: 6px 0;
    font-family: monospace;
    font-size: 12px;
    word-break: break-all;
}}
.command-approval-msg .approval-reason {{
    color: #a09070;
    font-style: italic;
    font-size: 12px;
    margin-top: 4px;
}}
.command-approval-msg .approval-buttons {{
    display: flex;
    justify-content: center;
    gap: 16px;
    margin-top: 10px;
}}
.command-approval-msg .approval-deny-btn,
.command-approval-msg .approval-accept-btn {{
    padding: 8px 24px;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    font-weight: bold;
    cursor: pointer;
    transition: opacity 0.2s;
}}
.command-approval-msg .approval-deny-btn {{
    background-color: #8b2020;
    color: #fff;
}}
.command-approval-msg .approval-deny-btn:hover {{
    opacity: 0.8;
}}
.command-approval-msg .approval-accept-btn {{
    background-color: #206030;
    color: #fff;
}}
.command-approval-msg .approval-accept-btn:hover {{
    opacity: 0.8;
}}
@keyframes approval-pulse {{
    0%, 100% {{ border-color: #5a4a20; }}
    50% {{ border-color: #8a7a40; }}
}}

/* Timestamp styling */
.message-timestamp {{
    font-size: 11px;
    color: #9ca3af;
    margin-top: 4px;
    margin-bottom: 0;
}}

.ai-message .message-timestamp {{
    margin-left: 0;
    padding-left: 16px;  /* 吹き出しの左端から少し内側に配置 */
    text-align: left;
}}

.user-message .message-timestamp {{
    display: block;
    text-align: right;
    padding-right: calc(20% + 16px);  /* 吹き出しの余白(20%) + 内側オフセット */
}}

/* Date divider between messages from different days */
.chat-date-divider {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 16px 0 10px 0;
    color: #9ca3af;
    font-size: 11px;
}}
.chat-date-divider::before,
.chat-date-divider::after {{
    content: "";
    flex: 1;
    border-top: 1px solid rgba(156, 163, 175, 0.35);
}}

/* Speaking animation */
@keyframes speaking {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.8; transform: scale(1.02); }}
}}

@keyframes soundWave {{
    0% {{ opacity: 0.3; }}
    50% {{ opacity: 1; }}
    100% {{ opacity: 0.3; }}
}}

.speaking-indicator {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    animation: speaking 1s infinite;
    background-color: #4CAF50;
    color: white;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    margin-left: 62px;
    margin-top: 8px;
}}

.speaking-indicator::before {{
    content: "🔊";
    font-size: 14px;
}}

/* Generating animation improvements */
.generating-message {{
    opacity: 0.8;
}}

.generating-message .ai-bubble {{
    background-color: #072a3c;
}}
{generate_message_css()}
.popup-container {{
    position: fixed;
    top: 20px;
    right: 20px;
    z-index: 1000;
    max-width: 350px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    transition: all 0.3s ease;
    padding: 12px 16px;
    border-radius: 4px;
    font-family: sans-serif;
}}
/* Phase 4D: type-specific variants for showNotification + error_notification */
.popup-container h3 {{
    margin: 0 0 4px 0;
    font-size: 14px;
    font-weight: 600;
}}
.popup-container p {{
    margin: 0;
    font-size: 13px;
    word-break: break-word;
}}
.popup-info {{
    background-color: #1e3a5f;
    border-left: 4px solid #2196f3;
    color: #e0f2ff;
}}
.popup-error {{
    background-color: #4a1f1f;
    border-left: 4px solid #f44336;
    color: #ffe0e0;
}}
.popup-warning {{
    background-color: #4a3f1f;
    border-left: 4px solid #ff9800;
    color: #fff3e0;
}}
/* Reset button styling when visible */
#reset_btn {{
    background-color: #ff6b6b !important;
    color: white !important;
}}
#reset_btn:hover {{
    background-color: #ff5252 !important;
}}

/* Beep volume test result styling */
#test-result {{
    margin-top: 10px;
    padding: 8px 12px;
    background-color: #1a2a3e;
    border-left: 3px solid #2196f3;
    border-radius: 4px;
    font-size: 13px;
    color: #64b5f6;
}}


/* Volume label styling */
#volume-label {{
    min-width: 50px;
    text-align: center;
    font-size: 16px;
    color: #64b5f6;
}}

/* Beep test button styling */
#test-beep-button {{
    margin-top: 8px;
    margin-bottom: 4px;
    width: 100%;
}}

/* TTS volume test result styling */
#tts-test-result {{
    margin-top: 10px;
    padding: 8px 12px;
    background-color: #1a2e1a;
    border-left: 3px solid #4caf50;
    border-radius: 4px;
    font-size: 13px;
    color: #81c784;
}}

/* TTS volume label styling */
#tts-volume-label {{
    min-width: 50px;
    text-align: center;
    font-size: 16px;
    color: #81c784;
}}

/* TTS test button styling */
#test-tts-button {{
    margin-top: 8px;
    margin-bottom: 4px;
    width: 100%;
}}

/* TTS volume slider styling */
#tts-volume-slider {{
    margin-top: 8px;
}}

/* Status display container - single HTML element to reduce flicker */
#status-display {{
    transition: opacity 0.3s ease;
}}

.status-container {{
    display: flex;
    flex-direction: column;
    gap: 4px;
}}

.status-item {{
    font-size: 14px;
    line-height: 1.5;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}

/* Image generation message block */
.image-gen-msg {{
    background-color: #1a2e1a;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a4a2a;
    font-size: 13px;
    text-align: center;
}}
.image-gen-msg .image-gen-header {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: bold;
    color: #6ab36a;
    margin-bottom: 8px;
}}
.image-gen-msg img {{
    /* サーバー描画のサムネ実寸(image_to_data_url target_size=120)に合わせる。
       WS経由でJSが差し込むブロックは大きいbase64を持つため、CSSで同寸に
       固定しないとライブ中だけ画像が肥大して見える(稜報告 2026-07-24) */
    max-width: 120px;
    max-height: 120px;
    border-radius: 8px;
    cursor: pointer;
    object-fit: cover;
    margin: 4px auto;
    display: block;
}}
.image-gen-msg .image-gen-prompt {{
    color: #9ca3af;
    font-style: italic;
    font-size: 12px;
    margin-top: 6px;
}}
.image-generating-spinner {{
    background-color: #1a2e1a;
    color: #6ab36a;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a4a2a;
    font-size: 13px;
    text-align: center;
}}
.image-generating-spinner .loading-spinner {{
    display: inline-block;
}}
/* Deep search spinner (searching / reading page) */
.deep-search-spinner {{
    background-color: #1a2e2e;
    color: #6ab3b3;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a4a4a;
    font-size: 13px;
    text-align: center;
}}
.deep-search-spinner .loading-spinner {{
    display: inline-block;
}}
/* Camera capture message block */
.camera-capture-msg {{
    background-color: #1a1a2e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a2a4a;
    font-size: 13px;
    text-align: center;
}}
.camera-capture-msg .camera-header {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: bold;
    color: #6a8fb3;
    margin-bottom: 4px;
}}
.camera-capture-msg .camera-reason {{
    color: #9ca3af;
    font-style: italic;
    font-size: 12px;
    margin-bottom: 6px;
}}
.camera-capture-msg details {{
    margin-top: 4px;
}}
.camera-capture-msg details summary {{
    cursor: pointer;
    color: #6a8fb3;
    font-size: 12px;
}}
.camera-capture-msg img {{
    max-width: 300px;
    border-radius: 8px;
    margin: 4px auto;
    display: block;
}}

/* Deep search execution block */
.deep-search-msg {{
    background-color: #1a2e2e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a4a4a;
    font-size: 13px;
    text-align: center;
}}
.deep-search-msg .ds-header {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: bold;
    color: #6ab3b3;
    margin-bottom: 4px;
}}
.deep-search-msg .ds-detail {{
    color: #9ca3af;
    font-style: italic;
    font-size: 12px;
}}
.deep-search-msg .ds-results {{
    margin-top: 4px;
    text-align: left;
}}
.deep-search-msg .ds-results summary {{
    cursor: pointer;
    color: #6ab3b3;
    font-size: 12px;
}}
.deep-search-msg .ds-url {{
    color: #8a8a8a;
    font-size: 11px;
    word-break: break-all;
    padding: 1px 0;
}}

/* Map search execution block (reuses ds-header/ds-detail styles) */
.map-search-msg {{
    background-color: #1a2a1e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a4a2a;
    font-size: 13px;
    text-align: center;
}}

/* ELYTH tool execution block (reuses ds-header/ds-detail styles) */
.elyth-msg {{
    background-color: #1a1e2e;
    color: #c0c0c0;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a2a4a;
    font-size: 13px;
    text-align: center;
}}
.map-search-msg .ds-header {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    font-weight: bold;
    color: #6ab36a;
    margin-bottom: 4px;
}}
.map-search-msg .ds-detail {{
    color: #9ca3af;
    font-style: italic;
    font-size: 12px;
}}
.map-search-spinner {{
    background-color: #1a2a1e;
    color: #6ab36a;
    padding: 10px 16px;
    margin: 8px auto;
    max-width: 80%;
    border-radius: 10px;
    border: 1px solid #2a4a2a;
    font-size: 13px;
    text-align: center;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
}}

/* Lightbox clickable images */
.lightbox-image {{
    cursor: pointer;
}}
/* Lightbox overlay */
#image-lightbox {{
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.85);
    z-index: 99999;
    justify-content: center;
    align-items: center;
    flex-direction: column;
    cursor: pointer;
}}
#image-lightbox.active {{
    display: flex;
}}
#image-lightbox img {{
    max-width: 90%;
    max-height: 80%;
    object-fit: contain;
    border-radius: 8px;
    cursor: default;
}}
#image-lightbox .lightbox-controls {{
    margin-top: 12px;
    display: flex;
    gap: 12px;
}}
#image-lightbox .lightbox-controls button {{
    padding: 8px 16px;
    background: rgba(255,255,255,0.15);
    color: #fff;
    border: 1px solid rgba(255,255,255,0.3);
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
}}
#image-lightbox .lightbox-controls button:hover {{
    background: rgba(255,255,255,0.25);
}}
</style>
"""


# Raw-CSS variant for gr.Blocks(css=...) injection. The Blocks css= param
# expects bare CSS rules (it wraps them in its own <style>), so we strip the
# wrappers from CHAT_CSS to avoid nested <style> tags. After Plan F, this is
# the *only* path through which CHAT_CSS reaches the page — get_chat_history()
# no longer prepends it inline.
CHAT_CSS_BODY = CHAT_CSS.replace("<style>", "").replace("</style>", "").strip()


def create_log_panel() -> Tuple[gr.Column, Dict[str, gr.Component]]:
    """
    Create log panel UI component with refresh button.
    
    Returns:
        Tuple with Column container and dictionary of components
    """
    try:
        with gr.Column(visible=False) as log_container:
            with gr.Row():
                gr.Markdown(f"### {t('logpanel.title')}")
                refresh_logs_btn = gr.Button(t('common.refresh_btn'), size="sm", scale=0)
            
            log_textbox = gr.Textbox(
                label="",
                value="",
                lines=30,
                interactive=False,
                elem_id="log-panel"
            )
        
        components = {
            "log_container": log_container,
            "log_textbox": log_textbox,
            "refresh_logs_btn": refresh_logs_btn
        }
        
        return log_container, components
    except Exception as e:
        logger.error(f"Error creating log panel: {e}")
        # Return a basic log panel
        with gr.Column(visible=True) as log_container:
            log_textbox = gr.Textbox(
                label=t('logpanel.error_label'),
                value=t('logpanel.create_error'),
                lines=30,
                interactive=False,
                visible=True
            )
            refresh_logs_btn = gr.Button(t('common.refresh_btn'), size="sm", visible=False)
        
        return log_container, {
            "log_container": log_container,
            "log_textbox": log_textbox,
            "refresh_logs_btn": refresh_logs_btn
        }


def create_character_creation_ui() -> Dict[str, gr.Component]:
    """
    Create character creation UI components.
    
    Returns:
        Dictionary of components
    """
    try:
        gr.Markdown("---")
        gr.Markdown(f"## {t('charform.create_title')}")
        name_input = gr.Textbox(label=t('charform.name'), value="")
        summary_input = gr.Textbox(label=t('charform.summary'), value="", lines=3)
        
        # Image upload for character icon
        icon_upload = gr.Image(label=t('charform.icon'), type="pil", height=200)
        
        # Language first, then voice: the voice list is filtered by the
        # selected language (ja=SBV2/en=Kokoro), so the language choice must
        # come first or the pre-filtered list looks arbitrary.
        stt_lang_dropdown = gr.Dropdown(label=t('charform.stt_language'), choices=["en", "ja"], value="ja")
        tts_dropdown = gr.Dropdown(label=t('charform.tts_folder'))
        ollama_models_dropdown = gr.Dropdown(label=t('charform.model_list'))
        system_prompt_box = gr.Textbox(label=t('charform.system_prompt'), lines=3)

        # Motion PNG Tuber folder (optional) — dropdown over MotionPNGPlayer/Asset/
        gr.Markdown(f"### {t('charform.motion_title')}")
        with gr.Row():
            motion_pngtuber_folder = gr.Dropdown(
                label=t('charform.motion_folder'),
                choices=[""] + list_asset_folders(),
                value="",
                allow_custom_value=True,
                info=t('charform.motion_info'),
                scale=4
            )
            motion_pngtuber_refresh = gr.Button(t('charform.motion_refresh'), scale=1, elem_id="motion-folder-refresh-create")

        # ELYTH settings
        gr.Markdown(f"### {t('charform.elyth_title')}")
        elyth_system_prompt = gr.Textbox(
            label=t('charform.elyth_prompt'),
            placeholder=t('charform.elyth_prompt_placeholder'),
            lines=8
        )
        elyth_api_key = gr.Textbox(
            label=t('charform.elyth_key'),
            placeholder="elyth_xxxxxxxxxxxx",
            type="password"
        )

        # YouTube返信プロンプト欄はこのフォームに置かない: 編集口は
        # YouTube返信タブに一本化(稜裁定 2026-07-22。configフィールド
        # youtube_system_prompt 自体は存続し、新規作成時はbackendが""で初期化)

        # elem_id: 押下中の「処理中...」表示(app.py の busy/restore js)が掴む取っ手
        create_btn = gr.Button(t('charform.create_btn'), variant="primary",
                               elem_id="char-create-btn")

        # 作成結果の通知は gr.Info / gr.Warning のトースト
        # (稜裁定 2026-08-15: 最下部Markdown表示は廃止)

        components = {
            "name_input": name_input,
            "summary_input": summary_input,
            "icon_upload": icon_upload,
            "tts_dropdown": tts_dropdown,
            "stt_lang_dropdown": stt_lang_dropdown,
            "system_prompt_box": system_prompt_box,
            "ollama_models_dropdown": ollama_models_dropdown,
            "motion_pngtuber_folder": motion_pngtuber_folder,
            "motion_pngtuber_refresh": motion_pngtuber_refresh,
            "elyth_system_prompt": elyth_system_prompt,
            "elyth_api_key": elyth_api_key,
            "create_btn": create_btn
        }
        
        return components
    except Exception as e:
        logger.error(f"Error creating character creation UI: {e}")
        # Return minimal error UI with all expected keys to prevent KeyError
        gr.Markdown(t('charform.create_ui_error'))
        
        # Create dummy components with all expected keys
        dummy_textbox = gr.Textbox(value="", interactive=False, visible=False)
        dummy_dropdown = gr.Dropdown(choices=[], interactive=False, visible=False)
        dummy_button = gr.Button("", interactive=False, visible=False)
        dummy_image = gr.Image(visible=False)
        
        return {
            "name_input": dummy_textbox,
            "summary_input": dummy_textbox,
            "icon_upload": dummy_image,
            "tts_dropdown": dummy_dropdown,
            "stt_lang_dropdown": dummy_dropdown,
            "system_prompt_box": dummy_textbox,
            "ollama_models_dropdown": dummy_dropdown,
            "motion_pngtuber_folder": dummy_dropdown,
            "motion_pngtuber_refresh": dummy_button,
            "elyth_system_prompt": dummy_textbox,
            "elyth_api_key": dummy_textbox,
            "create_btn": dummy_button
        }


def create_character_edit_ui() -> Dict[str, gr.Component]:
    """
    Create character editing UI components.
    
    Returns:
        Dictionary of components
    """
    try:
        gr.Markdown("---")
        gr.Markdown(f"## {t('charform.edit_title')}")
        existing_char_dropdown = gr.Dropdown(
            label=t('charform.select_to_edit'),
            choices=[],  # Will be populated by open_character_management
            interactive=True,
            visible=True
        )
        load_edit_btn = gr.Button(t('charform.load_config'))
        
        # Same fields for editing
        edit_name = gr.Textbox(label=t('charform.name'))
        edit_summary = gr.Textbox(label=t('charform.summary'), lines=3)
        
        # Image upload for character icon
        edit_icon_upload = gr.Image(label=t('charform.icon'), type="pil", height=200)
        edit_icon_original = gr.Textbox(label="Original Icon Path", value="", visible=False)
        # "1" once the user touches the Image component; cleared on Load Config.
        # Save-time icon resolution trusts this flag, not pixel comparison (H8).
        edit_icon_changed = gr.Textbox(label="Icon Changed Flag", value="", visible=False)
        
        # Language first, then voice (same rationale as the create form)
        edit_stt_dropdown = gr.Dropdown(label=t('charform.stt_language'), choices=["en", "ja"], value="en")
        edit_tts_dropdown = gr.Dropdown(label=t('charform.tts_folder'))
        edit_ollama_dropdown = gr.Dropdown(label=t('charform.model_list'))
        edit_system_prompt = gr.Textbox(label=t('charform.system_prompt'), lines=3)

        # Motion PNG Tuber folder (optional) — dropdown over MotionPNGPlayer/Asset/
        gr.Markdown(f"### {t('charform.motion_title')}")
        with gr.Row():
            edit_motion_pngtuber_folder = gr.Dropdown(
                label=t('charform.motion_folder'),
                choices=[""] + list_asset_folders(),
                value="",
                allow_custom_value=True,
                info=t('charform.motion_info'),
                scale=4
            )
            edit_motion_pngtuber_refresh = gr.Button(t('charform.motion_refresh'), scale=1, elem_id="motion-folder-refresh-edit")

        # ELYTH settings
        gr.Markdown(f"### {t('charform.elyth_title')}")
        edit_elyth_system_prompt = gr.Textbox(
            label=t('charform.elyth_prompt'),
            placeholder=t('charform.elyth_prompt_placeholder'),
            lines=8
        )
        edit_elyth_api_key = gr.Textbox(
            label=t('charform.elyth_key'),
            placeholder="elyth_xxxxxxxxxxxx",
            type="password"
        )

        # YouTube返信プロンプト欄はこのフォームに置かない(create form と同じ裁定
        # 2026-07-22)。編集はYouTube返信タブで・部分更新マージのため既存値は保持される

        # elem_id: 押下中の「処理中...」表示(app.py の busy/restore js)が掴む取っ手
        save_edit_btn = gr.Button(t('charform.save_changes'),
                                  elem_id="char-save-edit-btn")
        delete_char_btn = gr.Button(t('charform.delete'), variant="stop")

        # 保存/削除の結果通知は gr.Info / gr.Warning のトースト
        # (稜裁定 2026-08-15: 最下部Markdown表示は廃止)

        # Confirmation dialog for character deletion
        with gr.Group(visible=False) as delete_confirm_box:
            confirm_text = gr.Markdown(t('charform.delete_confirm'))
            confirm_char_id = gr.Textbox(visible=False)
            with gr.Row():
                confirm_yes = gr.Button(t('charform.delete_yes'), variant="stop")
                confirm_no = gr.Button(t('common.cancel'))
        
        components = {
            "existing_char_dropdown": existing_char_dropdown,
            "load_edit_btn": load_edit_btn,
            "edit_name": edit_name,
            "edit_summary": edit_summary,
            "edit_icon_upload": edit_icon_upload,
            "edit_icon_original": edit_icon_original,
            "edit_icon_changed": edit_icon_changed,
            "edit_tts_dropdown": edit_tts_dropdown,
            "edit_stt_dropdown": edit_stt_dropdown,
            "edit_system_prompt": edit_system_prompt,
            "edit_ollama_dropdown": edit_ollama_dropdown,
            "edit_motion_pngtuber_folder": edit_motion_pngtuber_folder,
            "edit_motion_pngtuber_refresh": edit_motion_pngtuber_refresh,
            "edit_elyth_system_prompt": edit_elyth_system_prompt,
            "edit_elyth_api_key": edit_elyth_api_key,
            "save_edit_btn": save_edit_btn,
            "delete_char_btn": delete_char_btn,
            "delete_confirm_box": delete_confirm_box,
            "confirm_text": confirm_text,
            "confirm_char_id": confirm_char_id,
            "confirm_yes": confirm_yes,
            "confirm_no": confirm_no
        }

        return components
    except Exception as e:
        logger.error(f"Error creating character edit UI: {e}")
        # Return minimal error UI with all expected keys to prevent KeyError
        gr.Markdown(t('charform.edit_ui_error'))
        
        # Create dummy components with all expected keys
        dummy_textbox = gr.Textbox(value="", interactive=False, visible=False)
        dummy_dropdown = gr.Dropdown(choices=[], interactive=False, visible=False)
        dummy_button = gr.Button("", interactive=False, visible=False)
        dummy_image = gr.Image(visible=False)
        dummy_group = gr.Group(visible=False)
        
        return {
            "existing_char_dropdown": dummy_dropdown,
            "load_edit_btn": dummy_button,
            "edit_name": dummy_textbox,
            "edit_summary": dummy_textbox,
            "edit_icon_upload": dummy_image,
            "edit_icon_original": dummy_textbox,
            "edit_icon_changed": dummy_textbox,
            "edit_tts_dropdown": dummy_dropdown,
            "edit_stt_dropdown": dummy_dropdown,
            "edit_system_prompt": dummy_textbox,
            "edit_ollama_dropdown": dummy_dropdown,
            "edit_motion_pngtuber_folder": dummy_dropdown,
            "edit_motion_pngtuber_refresh": dummy_button,
            "edit_elyth_system_prompt": dummy_textbox,
            "edit_elyth_api_key": dummy_textbox,
            "save_edit_btn": dummy_button,
            "delete_char_btn": dummy_button,
            "delete_confirm_box": dummy_group,
            "confirm_text": gr.Markdown("", visible=False),
            "confirm_char_id": dummy_textbox,
            "confirm_yes": dummy_button,
            "confirm_no": dummy_button
        }


# Cache decoded/resized attachment data URLs keyed on (path, mtime, size,
# target_size). Chat re-renders call this for every attached image on up to
# MAX_CHAT_HISTORY_SIZE messages; without a cache each re-render re-read +
# LANCZOS-resized + base64-encoded every image from disk (M31).
_IMAGE_DATA_URL_CACHE: Dict[Tuple[str, int, int, int], str] = {}
_IMAGE_DATA_URL_CACHE_MAX = 256


def image_to_data_url(image_path: Optional[str], target_size: int = 120) -> Optional[str]:
    """
    Convert an image file to a base64 data URL for embedding in HTML.
    Resizes the image for optimal display quality.

    Args:
        image_path: Path to the image file
        target_size: Target size for the image (default 120px for 2x display resolution)

    Returns:
        Optional[str]: Data URL string or None if conversion fails
    """
    if not image_path:
        return None

    try:
        path = Path(image_path)
        if not path.exists():
            logger.debug(f"Image file not found: {image_path}")
            return None

        # Cache lookup keyed on file identity + target size
        try:
            st = path.stat()
            cache_key = (str(path), int(st.st_mtime), int(st.st_size), int(target_size))
        except OSError:
            cache_key = None
        if cache_key is not None and cache_key in _IMAGE_DATA_URL_CACHE:
            return _IMAGE_DATA_URL_CACHE[cache_key]

        # Try to use PIL/Pillow for proper image resizing
        try:
            from PIL import Image
            import io
            
            # Open and resize the image with high-quality resampling
            with Image.open(path) as img:
                # Convert RGBA to RGB if needed (for JPEG compatibility)
                if img.mode in ('RGBA', 'LA', 'P'):
                    # Create a white background
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                    img = background
                
                # Calculate aspect ratio and resize
                img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                
                # Save to bytes buffer
                buffer = io.BytesIO()
                
                # Use WebP for better quality/size ratio if the browser supports it
                # Otherwise use PNG for transparency or JPEG for photos
                ext = path.suffix.lower()
                if ext in ['.jpg', '.jpeg'] and img.mode == 'RGB':
                    img.save(buffer, format='JPEG', quality=95, optimize=True)
                    mime_type = 'image/jpeg'
                else:
                    img.save(buffer, format='PNG', optimize=True)
                    mime_type = 'image/png'
                
                image_data = buffer.getvalue()
                
        except ImportError:
            # Fallback: use original image without resizing if PIL is not available
            logger.warning("PIL/Pillow not available, using original image size")
            with open(path, 'rb') as f:
                image_data = f.read()
            
            # Determine MIME type based on extension
            ext = path.suffix.lower()
            mime_types = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp'
            }
            mime_type = mime_types.get(ext, 'image/png')
        
        # Convert to base64
        base64_data = base64.b64encode(image_data).decode('utf-8')

        # Create data URL
        data_url = f"data:{mime_type};base64,{base64_data}"

        if cache_key is not None:
            if len(_IMAGE_DATA_URL_CACHE) >= _IMAGE_DATA_URL_CACHE_MAX:
                # Simple bound: drop an arbitrary existing entry (insertion order)
                _IMAGE_DATA_URL_CACHE.pop(next(iter(_IMAGE_DATA_URL_CACHE)), None)
            _IMAGE_DATA_URL_CACHE[cache_key] = data_url

        return data_url

    except Exception as e:
        logger.error(f"Error converting image to data URL: {e}")
        return None




def get_chat_history() -> str:
    """
    Formats the in-memory chat_history for display in a custom HTML chat component.
    Uses caching to prevent rebuilding HTML on every refresh.
    
    Returns:
        str: HTML representation of the chat history
    """
    # Check if we can use cached HTML
    current_version = app_state._chat_history_version
    
    # Simple cache check
    if (app_state._cached_chat_html and
        current_version == getattr(app_state, '_last_rendered_version', -1) and
        app_state.response_generating == getattr(app_state, '_last_generating_state', None) and
        app_state.is_loading_history == getattr(app_state, '_last_loading_state', None)):  # FIX: Compare loading state changes
        return app_state._cached_chat_html
    
    # Constants for memory management
    MAX_DISPLAYED_MESSAGES = 50  # Show only recent messages
    MAX_HTML_SIZE = 52428800  # 50MB limit for HTML output
    MAX_MESSAGE_LENGTH = 1000  # Truncate individual messages if too long
    
    try:
        # Font size CSS (dynamically generated based on current setting)
        font_css = f"""
        <style id="server-font-style">
        .ai-bubble, .user-bubble, .system-msg {{
            font-size: {app_state.chat_font_size}px;
        }}
        .message-timestamp {{
            font-size: {max(10, app_state.chat_font_size - 3)}px;
        }}
        </style>
        """
        
        # Custom HTML for chat display with character icons.
        # CHAT_CSS is loaded once at page load via gr.Blocks(css=...) — no longer
        # prepended here (saves ~22KB per chat refresh). font_css stays inline
        # since it depends on app_state.chat_font_size (per-render dynamic).
        html_output = font_css + '<div class="chat-container">'

        # Character display name for mobile UI (char-name span)
        import html as html_module
        char_display_name = html_module.escape(app_state.active_character_name or "AI")

        # Convert icon path to data URL for web display with caching
        icon_data_url = None
        icon_path_to_convert = None

        # Server mode: emit a /static/icons/<id>?v=<mtime> URL instead of inlining
        # 40KB base64 per AI message. Browser caches the response and dedupes
        # repeats inside a single HTML payload, so per-render cost drops ~90%.
        if (app_state.server_mode_enabled
                and app_state.active_character_id
                and app_state.active_character_icon):
            try:
                mtime = int(os.path.getmtime(app_state.active_character_icon))
            except OSError:
                mtime = 0
            icon_data_url = f"/static/icons/{app_state.active_character_id}?v={mtime}"

        if not icon_data_url:
            # Local mode (or unresolved character): keep the existing data-URL path.
            cache_key = f"icon_{app_state.active_character_id}" if app_state.active_character_id else "icon_default"
            if hasattr(app_state, '_icon_cache') and cache_key in app_state._icon_cache:
                icon_data_url = app_state._icon_cache[cache_key]
            else:
                try:
                    if app_state.active_character_icon:
                        icon_path_obj = Path(app_state.active_character_icon)
                        if icon_path_obj.exists():
                            icon_path_to_convert = str(icon_path_obj)
                        else:
                            logger.warning(f"Character icon not found: {app_state.active_character_icon}")
                            # Fall back to default icon
                            if DEFAULT_ICON_PATH.exists():
                                icon_path_to_convert = str(DEFAULT_ICON_PATH)
                    else:
                        # No character icon set, use default
                        if DEFAULT_ICON_PATH.exists():
                            icon_path_to_convert = str(DEFAULT_ICON_PATH)

                    # Convert to data URL
                    if icon_path_to_convert:
                        icon_data_url = image_to_data_url(icon_path_to_convert)

                        # Cache the result
                        if icon_data_url:
                            if not hasattr(app_state, '_icon_cache'):
                                app_state._icon_cache = {}
                            app_state._icon_cache[cache_key] = icon_data_url

                except Exception as e:
                    logger.warning(f"Error processing icon: {e}")

        # If we still don't have an icon, create a placeholder
        if not icon_data_url:
            # Create a simple SVG placeholder
            icon_data_url = "data:image/svg+xml;base64," + base64.b64encode(
                b'<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50" viewBox="0 0 50 50">'
                b'<circle cx="25" cy="25" r="20" fill="#3a3a3a"/>'
                b'<text x="25" y="25" text-anchor="middle" dominant-baseline="middle" font-size="20" fill="#aaa">?</text>'
                b'</svg>'
            ).decode('utf-8')
        
        # Add loading indicator if history is being loaded
        if app_state.is_loading_history:
            html_output += f'''
            <div class="info-message" style="text-align: center; padding: 10px; color: #999;">
                <div class="loading-spinner"></div> {t('chat.loading_history')}
            </div>
            '''
        
        # Handle history load errors
        if app_state.history_load_error and not app_state.is_loading_history:
            html_output += f'''
            <div class="warning-message" style="padding: 10px; margin: 10px 0;">
                {t('chat.history_load_error', error=app_state.history_load_error)}
            </div>
            '''
        
        # Get only recent messages to prevent memory issues
        total_messages = len(app_state.chat_history)
        if total_messages > MAX_DISPLAYED_MESSAGES:
            # Show a notice about hidden messages
            hidden_count = total_messages - MAX_DISPLAYED_MESSAGES
            html_output += f'''
            <div class="info-message" style="text-align: center; padding: 10px; color: #999; font-style: italic;">
                {hidden_count} earlier messages hidden for performance. 
                <a href="#" onclick="alert('Full history export coming soon!'); return false;">Export full history</a>
            </div>
            '''
            # Get only recent messages
            messages_to_display = app_state.chat_history[-MAX_DISPLAYED_MESSAGES:]
        else:
            messages_to_display = app_state.chat_history
        
        # Build chat messages
        last_divider_date = None  # local date of the last emitted date divider
        for message_data in messages_to_display:
            # Handle 3-tuple, 4-tuple, 5-tuple, and 6-tuple formats for compatibility
            images = []
            documents = []
            if len(message_data) == 6:
                speaker, text, is_ai, timestamp, images, documents = message_data
            elif len(message_data) == 5:
                speaker, text, is_ai, timestamp, images = message_data
            elif len(message_data) == 4:
                speaker, text, is_ai, timestamp = message_data
            else:
                # Old format without timestamp
                speaker, text, is_ai = message_data
                timestamp = None
            try:
                # Escape HTML to prevent injection
                import html
                from datetime import datetime

                safe_speaker = html.escape(str(speaker))
                # COMMAND / TALK_THEME / IMAGE_GEN / CAMERA_CAPTURE / DEEP_SEARCH / MAP_SEARCH / ELYTH messages contain pre-sanitized HTML; don't escape them
                if speaker in ("COMMAND", "TALK_THEME", "TALK_THEME_USER", "IMAGE_GEN", "CAMERA_CAPTURE", "DEEP_SEARCH", "MAP_SEARCH", "ELYTH"):
                    safe_text = str(text)
                else:
                    safe_text = html.escape(str(text))
                
                # Format timestamp if available
                timestamp_html = ""
                msg_date = None
                if timestamp:
                    try:
                        # Primary format: ISO string (new standard)
                        if isinstance(timestamp, str):
                            # Parse ISO format datetime
                            # Handle both with and without timezone info
                            if 'Z' in timestamp:
                                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            elif '+' in timestamp or timestamp.count('-') > 2:
                                # Already has timezone info
                                dt = datetime.fromisoformat(timestamp)
                            else:
                                # No timezone info, treat as local time
                                dt = datetime.fromisoformat(timestamp)

                            # Convert UTC to local time if needed
                            # If datetime has timezone info and it's UTC, convert to local
                            if dt.tzinfo is not None:
                                # Convert to local timezone
                                dt_local = dt.astimezone()  # Converts to system's local timezone
                                dt = dt_local.replace(tzinfo=None)  # Remove timezone info for display
                        else:
                            # Backward compatibility: handle numeric timestamps
                            # This path will be rarely used after migration
                            dt = datetime.fromtimestamp(timestamp)

                        # 12時間表記は英語UIのみ。日本語UIでは深夜0時台の
                        # "12:53 AM" が正午と誤読される(稜報告 2026-07-24)ため
                        # 24時間表記にする。
                        if current_language() == 'en':
                            time_str = dt.strftime("%I:%M %p")  # e.g., "2:30 PM"
                        else:
                            time_str = dt.strftime("%H:%M")  # e.g., "0:53" -> "00:53"
                        timestamp_html = f'<p class="message-timestamp">{time_str}</p>'
                        msg_date = dt.date()
                    except Exception as e:
                        logger.debug(f"Could not parse timestamp {timestamp}: {e}")
                        # Skip timestamp display if we can't parse it
                        timestamp_html = ""

                # 日付が変わったら日付付き横ラインを挿入。タイムスタンプ欠落/
                # 解析不能のメッセージは日付不明としてスキップ(last_divider_date
                # は更新しない=偽の区切りを出さない)。
                if msg_date is not None and msg_date != last_divider_date:
                    if current_language() == 'en':
                        date_label = msg_date.strftime('%b %d, %Y')
                    else:
                        date_label = f"{msg_date.year}年{msg_date.month}月{msg_date.day}日"
                    html_output += f'<div class="chat-date-divider"><span>{date_label}</span></div>'
                    last_divider_date = msg_date

                # Truncate long messages to prevent UI issues
                # Skip truncation for pre-sanitized HTML speakers (contain embedded images)
                if len(safe_text) > MAX_MESSAGE_LENGTH and speaker not in ("COMMAND", "TALK_THEME", "TALK_THEME_USER", "IMAGE_GEN", "CAMERA_CAPTURE", "DEEP_SEARCH", "MAP_SEARCH", "ELYTH"):
                    safe_text = safe_text[:MAX_MESSAGE_LENGTH] + '... (truncated)'
                
                # Check HTML size before adding more content
                if len(html_output) > MAX_HTML_SIZE - 5000:  # Leave 5KB buffer
                    html_output += f'''
                    <div class="warning-message" style="text-align: center; padding: 10px; color: #ff6b6b;">
                        {t('chat.truncated')}
                    </div>
                    '''
                    break

                # Check if this is a system message (feedback)
                # Build images HTML if present
                images_html = ""
                if images:
                    from backend.shared.image_storage import is_generated_image, get_thumbnail_path_for
                    images_html = '<div class="message-images" style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; margin-bottom: 2px;">'
                    for img_path in images:
                        p = Path(str(img_path))
                        if not p.exists():
                            from backend.shared.image_storage import get_placeholder_html
                            images_html += get_placeholder_html()
                            continue
                        # Thumbnail (120px) for chat display
                        thumb_url = image_to_data_url(str(img_path), target_size=120)
                        if not thumb_url:
                            continue
                        # Full-size (512px) for lightbox
                        if is_generated_image(str(img_path)):
                            thumb_path = get_thumbnail_path_for(str(img_path))
                            full_url = image_to_data_url(thumb_path or str(img_path), target_size=512)
                        else:
                            full_url = image_to_data_url(str(img_path), target_size=512)
                        full_attr = f' data-full-src="{full_url}"' if full_url else ''
                        images_html += f'<img src="{thumb_url}"{full_attr} style="max-width: 200px; max-height: 200px; border-radius: 8px; cursor: pointer; object-fit: cover;" class="lightbox-image" />'
                    images_html += '</div>'

                # Build documents HTML if present
                docs_html = ""
                if documents:
                    docs_html = '<div class="message-documents" style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; margin-bottom: 2px;">'
                    for doc_name in documents:
                        if doc_name == "__location_sent__":
                            docs_html += f'<div style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; background: rgba(106,179,106,0.15); border-radius: 6px; font-size: 0.85em; color: #6ab36a; border: 1px solid rgba(106,179,106,0.3);"><span style="font-size: 1.1em;">📍</span> {t("chat.location_sent")}</div>'
                            continue
                        safe_doc_name = html.escape(str(doc_name))
                        docs_html += f'<div style="display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; background: rgba(255,255,255,0.08); border-radius: 6px; font-size: 0.85em; color: #ccc; border: 1px solid rgba(255,255,255,0.12);"><span style="font-size: 1.1em;">📄</span> {safe_doc_name}</div>'
                    docs_html += '</div>'

                if speaker == "SYSTEM":
                    # System message (centered, gray background)
                    html_output += f'''
                    <div class="system-msg">
                        {safe_text}
                    </div>
                    '''
                elif speaker == "COMMAND":
                    # Command execution message (centered, dark themed)
                    html_output += f'''
                    <div class="command-msg">
                        {safe_text}
                    </div>
                    '''
                elif speaker == "TALK_THEME":
                    # AI-initiated talk theme change
                    html_output += f'''
                    <div class="talk-theme-msg">
                        {safe_text}
                    </div>
                    '''
                elif speaker == "TALK_THEME_USER":
                    # User-initiated talk theme change
                    html_output += f'''
                    <div class="talk-theme-user-msg">
                        {safe_text}
                    </div>
                    '''
                elif speaker == "IMAGE_GEN":
                    # AI-generated image block - render from stored paths
                    ig_inner = ""
                    raw_text = str(text)
                    if raw_text.startswith("IMAGE_GEN_SUCCESS:"):
                        ig_prompt = html.escape(raw_text[len("IMAGE_GEN_SUCCESS:"):])
                        ig_inner = f'<div class="image-gen-header">{t("chat.image_generated")}</div>'
                        # Build image from path (not embedded base64)
                        if images:
                            for img_path in images:
                                p = Path(str(img_path))
                                if not p.exists():
                                    from backend.shared.image_storage import get_placeholder_html
                                    ig_inner += get_placeholder_html()
                                    continue
                                from backend.shared.image_storage import is_generated_image, get_thumbnail_path_for
                                t_url = image_to_data_url(str(img_path), target_size=120)
                                if not t_url:
                                    continue
                                if is_generated_image(str(img_path)):
                                    tp = get_thumbnail_path_for(str(img_path))
                                    f_url = image_to_data_url(tp or str(img_path), target_size=512)
                                else:
                                    f_url = image_to_data_url(str(img_path), target_size=512)
                                f_attr = f' data-full-src="{f_url}"' if f_url else ''
                                ig_inner += f'<img src="{t_url}"{f_attr} class="lightbox-image" />'
                        ig_inner += f'<div class="image-gen-prompt">{ig_prompt}</div>'
                    elif raw_text.startswith("IMAGE_GEN_FAILED:"):
                        ig_error = html.escape(raw_text[len("IMAGE_GEN_FAILED:"):])
                        ig_inner = f'<div class="image-gen-header">{t("chat.image_gen_failed")}</div>'
                        ig_inner += f'<div class="image-gen-prompt">{ig_error}</div>'
                    else:
                        # Legacy format (pre-built HTML) - render as-is
                        ig_inner = safe_text
                    html_output += f'''
                    <div class="image-gen-msg">
                        {ig_inner}
                    </div>
                    '''
                elif speaker == "CAMERA_CAPTURE":
                    # Camera capture block - show log with image in toggle
                    cam_inner = ""
                    raw_text = str(text)
                    if raw_text.startswith("CAMERA_SUCCESS:"):
                        cam_reason = html.escape(raw_text[len("CAMERA_SUCCESS:"):])
                        cam_inner = f'<div class="camera-header">{t("chat.camera_capture")}</div>'
                        if cam_reason:
                            cam_inner += f'<div class="camera-reason">{cam_reason}</div>'
                        if images:
                            for img_path in images:
                                p = Path(str(img_path))
                                if not p.exists():
                                    cam_inner += f'<div style="color:#888;font-size:11px;">{t("chat.image_deleted")}</div>'
                                    continue
                                img_url = image_to_data_url(str(img_path), target_size=300)
                                if img_url:
                                    cam_inner += f'<details><summary>{t("chat.captured_image")}</summary><img src="{img_url}" style="max-width:300px;border-radius:6px;margin-top:4px;" /></details>'
                    elif raw_text.startswith("CAMERA_FAILED:"):
                        cam_error = html.escape(raw_text[len("CAMERA_FAILED:"):])
                        cam_inner = f'<div class="camera-header">{t("chat.camera_failed")}</div>'
                        cam_inner += f'<div class="camera-reason">{cam_error}</div>'
                    else:
                        cam_inner = safe_text
                    html_output += f'''
                    <div class="camera-capture-msg">
                        {cam_inner}
                    </div>
                    '''
                elif speaker == "DEEP_SEARCH":
                    # Deep search execution block
                    html_output += f'''
                    <div class="deep-search-msg">
                        {safe_text}
                    </div>
                    '''
                elif speaker == "MAP_SEARCH":
                    # Map search execution block
                    html_output += f'''
                    <div class="map-search-msg">
                        {safe_text}
                    </div>
                    '''
                elif speaker == "ELYTH":
                    # ELYTH tool execution block
                    html_output += f'''
                    <div class="elyth-msg">
                        {safe_text}
                    </div>
                    '''
                elif is_ai:
                    # AI message with character icon on the left
                    html_output += f'''
                    <div class="ai-message">
                        <div class="ai-icon-row">
                            <img src="{icon_data_url}" class="char-icon" alt="{safe_speaker}" />
                            <span class="char-name">{char_display_name}</span>
                        </div>
                        <div class="message-content">
                            <div class="ai-bubble">{safe_text}</div>
                            {images_html}
                            {docs_html}
                            {timestamp_html}
                        </div>
                    </div>
                    '''
                else:
                    # User message on the right (no avatar)
                    html_output += f'''
                    <div class="user-message">
                        <div class="message-content">
                            <div class="user-bubble">{safe_text}</div>
                            {images_html}
                            {docs_html}
                            {timestamp_html}
                        </div>
                    </div>
                    '''
            except Exception as e:
                logger.error(f"Error rendering chat message: {e}")
                # Skip this message and continue
                continue
        
        # Add loading spinner if a response is being generated
        if app_state.response_generating:
            html_output += f'''
            <div class="ai-message generating-message">
                <div class="ai-icon-row">
                    <img src="{icon_data_url}" class="char-icon" alt="AI" />
                </div>
                <div class="message-content">
                    <div class="ai-bubble">
                        <div class="loading-spinner"></div> {t('gen.generating')}
                    </div>
                </div>
            </div>
            '''

        html_output += '</div>'

        # Cache the result
        app_state._cached_chat_html = html_output
        app_state._last_rendered_version = current_version
        app_state._last_generating_state = app_state.response_generating
        app_state._last_loading_state = app_state.is_loading_history  # FIX: Save loading state for cache comparison
        
        return html_output
        
    except Exception as e:
        logger.error(f"Critical error in get_chat_history: {e}")
        # Return safe fallback HTML
        return f'{CHAT_CSS}<div class="chat-container"><div class="error-message">{t("chat.render_error")}</div></div>'


def update_log_view() -> str:
    """Return current log messages as a string."""
    try:
        return "\n".join(app_state.log_messages)
    except Exception as e:
        logger.error(f"Error updating log view: {e}")
        return t('logpanel.load_error')


def update_prompt_view() -> str:
    """Return the last LLM prompt as formatted text."""
    try:
        # Check if backend module is available
        if not hasattr(backend, 'get_last_llm_prompt'):
            return t('promptlog.not_initialized')
            
        result = backend.get_last_llm_prompt()
        if result.get("success"):
            prompt_text = result.get("prompt", t('promptlog.none'))
            
            # Update app state
            app_state.last_prompt_text = prompt_text
            app_state.last_prompt_timestamp = result.get("timestamp")
            app_state.last_prompt_character = result.get("character")
            
            return prompt_text
        else:
            return t('promptlog.retrieve_error', error=result.get('error', 'Unknown error'))
    except Exception as e:
        logger.error(f"Error updating prompt view: {e}")
        return t('promptlog.load_error', error=str(e))


def update_prompt_token_view() -> str:
    """Return the token info line HTML for the prompt log (build-time initial
    value only).

    実行中の更新はWS 'prompt_token_info' → JSのDOM直接更新が担う
    （gr.Timer出力の可視要素はskip+show_progress=hiddenでもちらつく既知
    問題のため、Gradioイベントを通さない=recording_displayと同方式）。
    """
    try:
        if not hasattr(backend, 'get_last_llm_prompt'):
            return ""
        from backend.shared.prompt_token_display import format_token_line
        result = backend.get_last_llm_prompt()
        tokens = result.get("tokens") if result.get("success") else None
        return format_token_line(tokens)
    except Exception as e:
        logger.error(f"Error updating prompt token view: {e}")
        return ""


def rebroadcast_prompt_tokens() -> None:
    """更新ボタン用: 現在のトークン行をWSで再配信する。

    通常はリクエスト保存/実測追記時のpushで最新が届くが、WS切断中の
    取りこぼしを手動更新で回復できるようにする（出力なしのGradioハンドラ=
    JS専有divへのGradio書き込みはしない・書き手一人原則）。
    """
    try:
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("prompt_token_info",
                          data={"html": update_prompt_token_view()})
    except Exception as e:
        logger.debug(f"prompt token rebroadcast failed: {e}")


def format_history_messages(messages: List[Dict[str, Any]], countdown: int, unprocessed: int = 0) -> str:
    """
    Format short-term messages for history display.
    
    Args:
        messages: List of message dictionaries
        countdown: Number of messages until next summary
        unprocessed: Messages accumulated since the last extraction
            (stats.unprocessed). 閾値はプロバイダで違う(Ollama 50/API 100)ので
            バー幅は countdown+unprocessed(=閾値)に対する比率で出す。旧実装の
            50 直書きは API キャラで幅が負になりバーが動かなかった。
        
    Returns:
        HTML string for display
    """
    import html
    from datetime import datetime
    
    if not messages:
        return f'<div class="history-empty">{t("histview.no_recent")}</div>'
    
    # Countdown bar (countdown == 0 → 発火済み/直前 = 100%)
    threshold_total = unprocessed + countdown
    progress_pct = (unprocessed / threshold_total * 100) if threshold_total > 0 else 0
    countdown_html = f'''
    <div class="countdown-container">
        <div class="countdown-progress" style="width: {progress_pct:.1f}%"></div>
        <div class="countdown-text">
            {t('histview.countdown', count=countdown)}
            {t('histview.approaching') if countdown <= 10 else ''}
        </div>
    </div>
    <div class="messages-container">
    '''
    
    # Messages (newest first for history view)
    # Limit to most recent 100 messages to prevent performance issues
    display_messages = messages[-100:] if len(messages) > 100 else messages
    
    for msg in reversed(display_messages):
        # systemメッセージ（フィードバック）を特別に扱う
        if msg.get('role') == 'system':
            role_class = "system-msg"
        elif msg.get('role') == 'user':
            role_class = "user-msg"
        else:
            role_class = "ai-msg"
        timestamp = msg.get('timestamp', '')
        if timestamp:
            # Format timestamp to readable format
            try:
                if 'T' in timestamp:  # ISO format
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    timestamp = dt.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    timestamp = timestamp[:19]  # Trim to YYYY-MM-DD HH:MM:SS
            except:
                timestamp = timestamp[:19]
        
        content = html.escape(str(msg.get('content', '')))
        if len(content) > 500:
            content = content[:500] + '...'
        
        html_output = f'''
        <div class="history-message {role_class}">
            <div class="msg-header">
                <span class="role">{msg.get('role', 'unknown').title()}</span>
                <span class="timestamp">{timestamp}</span>
            </div>
            <div class="msg-content">{content}</div>
        </div>
        '''
        countdown_html += html_output
    
    countdown_html += '</div>'
    return countdown_html


def format_memory_entries(memories: List[Dict[str, Any]]) -> str:
    """
    Format long-term memory entries for history display with action buttons.

    Args:
        memories: List of memory entry dictionaries

    Returns:
        HTML string for display
    """
    import html as html_mod

    if not memories:
        return f'<div class="history-empty">{t("memview.none")}</div>'

    # Category color map
    cat_colors = {
        "user_fact": ("#3a5a3a", "#a0e0a0"),
        "user_preference": ("#3a3a5a", "#b0b0e0"),
        "character_relationship": ("#5a3a4a", "#e0a0c0"),
        "shared_experience": ("#4a4a3a", "#d0d0a0"),
        "user_opinion": ("#3a4a5a", "#a0c0e0"),
    }
    default_cat_color = ("#3a3a5a", "#b0b0e0")

    css_output = '''
    <style>
    .memory-entry {
        position: relative;
        padding: 10px 15px;
        margin-bottom: 8px;
        background-color: #1e1e2e;
        border: 1px solid #3a3a4a;
        border-radius: 8px;
        transition: all 0.3s ease;
    }
    .memory-entry.pinned {
        border-color: #e67e22;
        background-color: #2e2a1a;
    }
    .memory-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 6px;
        font-size: 12px;
        color: #888;
    }
    .memory-category {
        padding: 2px 8px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: bold;
    }
    .memory-content {
        color: #ddd;
        font-size: 14px;
        line-height: 1.5;
        margin-bottom: 6px;
    }
    .memory-actions {
        display: flex;
        gap: 6px;
        align-items: center;
        margin-top: 4px;
    }
    .memory-actions button {
        background: #333;
        border: 1px solid #555;
        color: #ccc;
        padding: 2px 10px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 12px;
    }
    .memory-actions button:hover {
        background: #444;
        color: #fff;
    }
    .memory-actions button.pin-btn.active {
        background: #e67e22;
        color: #fff;
        border-color: #e67e22;
    }
    .memory-actions button.delete-btn:hover {
        background: #c0392b;
        color: #fff;
    }
    .pinned-badge {
        color: #e67e22;
        font-size: 11px;
        font-weight: bold;
    }
    </style>
    <script>
    function memoryAction(action, memId, extra) {
        var payload = action + '::' + memId;
        if (extra) payload += '::' + extra;
        var trigger = document.querySelector('#memory-action-trigger textarea');
        if (!trigger) trigger = document.querySelector('#memory-action-trigger input');
        if (trigger) {
            var nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set || Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            ).set;
            nativeSetter.call(trigger, payload);
            trigger.dispatchEvent(new Event('input', {bubbles: true}));
            setTimeout(function() {
                var btn = document.querySelector('#memory-action-btn');
                if (btn) btn.click();
            }, 50);
        }
    }
    function memoryPin(memId, currentlyPinned) {
        memoryAction('pin', memId, currentlyPinned ? 'false' : 'true');
    }
    function memoryDelete(memId) {
        if (confirm('Delete this memory entry?')) {
            memoryAction('delete', memId);
        }
    }
    function memoryEdit(memId) {
        var el = document.querySelector('[data-memory-id="' + memId + '"] .memory-content');
        if (!el) return;
        var currentText = el.innerText || el.textContent;
        var newText = prompt('Edit memory content:', currentText);
        if (newText !== null && newText.trim() !== '' && newText !== currentText) {
            memoryAction('edit', memId, newText.trim());
        }
    }
    </script>
    '''

    css_output = css_output.replace('Delete this memory entry?', t('memview.delete_confirm'))
    css_output = css_output.replace('Edit memory content:', t('memview.edit_prompt'))
    html_output = css_output + '<div class="memories-container">'

    for mem in memories:
        category = html_mod.escape(str(mem.get('category', 'unknown')))
        content = html_mod.escape(str(mem.get('content', '')))
        content_display = content.replace('\n', '<br>')
        created = mem.get('created_at', '')[:10]
        updated = mem.get('updated_at', '')[:10]
        is_pinned = mem.get('pinned', False)
        mem_id = html_mod.escape(str(mem.get('id', '')))
        pinned_class = "pinned" if is_pinned else ""
        pinned_badge = f'<span class="pinned-badge">{t("memview.pinned")}</span>' if is_pinned else ''

        bg, fg = cat_colors.get(mem.get('category', ''), default_cat_color)

        date_info = created
        if updated and updated != created:
            date_info += t('memview.updated', date=updated)

        pin_label = t('memview.unpin') if is_pinned else t('memview.pin')
        pin_active = " active" if is_pinned else ""
        pinned_js = "true" if is_pinned else "false"

        html_output += f'''
        <div class="memory-entry {pinned_class}" data-memory-id="{mem_id}">
            <div class="memory-header">
                <span class="memory-category" style="background-color:{bg};color:{fg};">{category}</span>
                <span>{date_info} {pinned_badge}</span>
            </div>
            <div class="memory-content">{content_display}</div>
            <div class="memory-actions">
                <button class="pin-btn{pin_active}" onclick="memoryPin('{mem_id}',{pinned_js})">{pin_label}</button>
                <button onclick="memoryEdit('{mem_id}')">{t('memview.edit')}</button>
                <button class="delete-btn" onclick="memoryDelete('{mem_id}')">{t('memview.delete')}</button>
            </div>
        </div>
        '''

    html_output += '</div>'
    return html_output


# Export public API
__all__ = [
    'CSS_THEME',
    'generate_message_css',
    'CHAT_CSS',
    'create_log_panel',
    'create_character_creation_ui',
    'create_character_edit_ui',
    'get_chat_history',
    'update_log_view',
    'update_prompt_view',
    'update_prompt_token_view',
    'rebroadcast_prompt_tokens',
    'format_history_messages',
    'format_memory_entries'
]