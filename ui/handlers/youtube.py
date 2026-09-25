"""
ui/handlers/youtube.py

YouTube comment auto-reply — settings tab + GUI authorization + status
display (spec §7). Tab construction follows the ELYTH control tab precedent
(built inside the character-settings Tabs context from ui/pages.py); the
live status display is WS-driven (publish_ui_update "youtube_status" →
ws_client_js DOM updates; gr.Timer is a known-flicker path and is not used).
"""

import glob
import json
import logging
import os
from typing import Any, Dict, List, Tuple

import gradio as gr

from backend.shared.i18n import t

from ..status_js import status_auto_hide_js

logger = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 60  # J8: セッション間隔は60分未満に設定不可（下限）
DEFAULT_INTERVAL_MINUTES = 180  # 未設定時の既定（稜裁定 2026-08-11）。下限とは別物


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_characters() -> List[Tuple[str, str]]:
    """(display name, character_id) choices for the dropdown."""
    choices: List[Tuple[str, str]] = []
    try:
        from backend.shared.constants import CHARACTER_CONFIGS_DIR
        for path in sorted(glob.glob(os.path.join(str(CHARACTER_CONFIGS_DIR), "*.json"))):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                cid = cfg.get("character_id", "")
                if cid:
                    choices.append((cfg.get("name", cid[:8]), cid))
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[YouTube UI] Failed to list characters: {e}")
    return choices


def youtube_character_choices_update():
    """gr.update with the current character choices (value left untouched).

    The tab freezes its choices at build time, so a character created after
    startup stayed invisible here until a page reload. Same pattern as
    ``_character_llm_choices_update`` in handlers/api_settings.py: no ``value=``
    in the update, so whatever the user has selected survives the refresh.
    """
    return gr.update(choices=_list_characters())


def _get_youtube_status_html() -> str:
    """Initial status container; live values are filled by ws_client_js."""
    return f'''<div id="youtube-status-container" style="padding:12px;background:#222;border-radius:8px;border-left:4px solid #888;">
        <div style="display:flex;align-items:center;gap:8px;">
            <span id="youtube-status-dot" style="width:10px;height:10px;border-radius:50%;background:#888;display:inline-block;"></span>
            <span id="youtube-status-label" style="font-weight:bold;font-size:14px;color:#888;">{t('youtube.initializing')}</span>
            <span id="youtube-status-timer" style="font-size:12px;color:#aaa;margin-left:auto;font-family:monospace;"></span>
        </div>
        <div id="youtube-status-detail" style="font-size:12px;color:#aaa;padding-left:18px;"></div>
        <div id="youtube-activity-log" style="font-size:11px;color:#9ca3af;padding-left:18px;margin-top:6px;max-height:160px;overflow-y:auto;display:none;border-top:1px solid #333;padding-top:4px;"></div>
    </div>'''


def _channel_flow_html() -> str:
    """「どのチャンネルが→どのチャンネルへ返信するか」の関係を枠付き1ブロックで
    表示（稜指示 2026-07-12: 矢印と同じ文字サイズでは伝わらない→チャンネル名を
    大きく・囲って見せる）。チャンネルIDは表示しない（名前だけで足りる）。"""
    import html as _html
    poster = ""
    try:
        from backend.youtube import auth
        channel = auth.get_authorized_channel()
        if channel:
            poster = channel.get("channel_title") or ""
    except Exception as e:
        logger.warning(f"[YouTube UI] Failed to read authorized channel: {e}")
    try:
        from backend.shared.settings_store import get_setting
        target = get_setting("youtube", "target_channel_title", "") or ""
    except Exception:
        target = ""

    def _name_html(name: str, missing_key: str) -> str:
        if name:
            return (f'<div style="font-size:17px;font-weight:bold;color:#fff;">'
                    f'{_html.escape(name)}</div>')
        return (f'<div style="font-size:13px;color:#ff9800;">'
                f'{_html.escape(t(missing_key))}</div>')

    return f'''<div style="text-align:center;">
    <div style="margin-top:8px;padding:12px 18px;background:#222;border:1px solid #555;border-left:4px solid #2196f3;border-radius:8px;display:inline-block;min-width:320px;text-align:left;">
        <div style="font-size:11px;color:#9ca3af;">{_html.escape(t('youtube.flow_poster'))}</div>
        {_name_html(poster, 'youtube.flow_unauthorized')}
        <div style="font-size:13px;color:#8bc34a;padding:6px 0 6px 8px;">{_html.escape(t('youtube.flow_arrow'))}</div>
        <div style="font-size:11px;color:#9ca3af;">{_html.escape(t('youtube.flow_target'))}</div>
        {_name_html(target, 'youtube.flow_unset')}
    </div>
    </div>'''


def _load_char_youtube_prompt(character_id: str) -> str:
    """担当キャラのキャラ設定から youtube_system_prompt を読む（タブ内エディタ用。
    実体は character_configs/<id>.json のフィールド＝キャラ編集フォームと同一）。"""
    if not character_id:
        return ""
    try:
        import backend
        response = backend.load_character_config(character_id)
        if isinstance(response, dict) and "result" in response:
            config = response.get("result") or {}
        else:
            config = response or {}
        return config.get("youtube_system_prompt", "") or ""
    except Exception as e:
        logger.warning(f"[YouTube UI] Failed to load character prompt: {e}")
        return ""


def _client_secret_status_html() -> str:
    """client_secret.json の取り込み状態（再起動後もファイル欄が空に見えて
    「消えた」と誤解される — 稜指摘 2026-07-12 — ので枠付きで明示する）。"""
    import html as _html
    imported = False
    try:
        from backend.youtube import auth
        imported = auth.has_client_secret()
    except Exception as e:
        logger.warning(f"[YouTube UI] Failed to check client_secret: {e}")
    text = t('youtube.cs_imported') if imported else t('youtube.cs_missing')
    color = "#4caf50" if imported else "#ff9800"
    return (f'<div style="text-align:center;">'
            f'<div style="margin:6px 0;padding:10px 16px;background:#222;'
            f'border:1px solid #555;border-left:4px solid {color};border-radius:8px;'
            f'display:inline-block;font-size:14px;color:#fff;">'
            f'{_html.escape(text)}</div></div>')


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------

def create_youtube_control_tab() -> Dict[str, Any]:
    """Build the YouTube reply control tab (call inside a gr.Tab context)."""
    from backend.shared.settings_store import get_setting

    components: Dict[str, Any] = {}

    # ----- Beta banner (稜依頼 2026-07-25: タイトルの上で目立たせる。
    # Dry-run検証止まり=実投稿は未保証の旨) -----
    gr.HTML(
        "<div style='border:1px solid rgba(217,119,6,.65);"
        "background:rgba(217,119,6,.14);border-radius:8px;"
        "padding:10px 14px;margin:4px 0 8px 0;'>"
        f"<strong style='color:#d97706;'>{t('youtube.beta_title')}</strong>"
        f"<span style='margin-left:8px;'>{t('youtube.beta_body')}</span>"
        "</div>"
    )

    # ----- Tab title + description (最上部 — 稜指示 2026-07-19) -----
    gr.Markdown(f"### {t('char.tab.youtube')}")
    gr.Markdown(t('youtube.session_desc'))
    gr.Markdown("---")

    # ----- Status (WS-updated) + master toggle / manual start buttons -----
    # ELYTHの制御タブと同型（稜指示 2026-07-12: 同じ提示実行タイプなので
    # 「自動ループ: ON/OFF + セッション開始」のデザインに合わせる。マスター
    # トグルは設定の奥ではなく状態表示の直下＝見える場所に置く）。
    # ボタンは生HTML+WS制御（elyth-loop-btn / elyth-session-btn の先例）。
    gr.Markdown(f"### {t('youtube.session_status')}")
    with gr.Row():
        components["status_display"] = gr.HTML(
            value=_get_youtube_status_html(), elem_id="youtube-status-display")

    enabled_now = bool(get_setting("youtube", "enabled", False))
    loop_label = t('youtube.auto_loop_on') if enabled_now else t('youtube.auto_loop_off')
    loop_color = "#4caf50" if enabled_now else "#f44336"
    # セッション開始ボタンは自動返信OFFでも押せる（手動一回分実行 — 稜指示
    # 2026-07-12。グレーになるのは実行中/要再認可のときだけ=JS側と同じ規則）
    # 縦ズレ根治: 英数字/日本語混在文言のベースライン差で箱ごと縦にズレるため、
    # Utility Panel実証済みパターン(各ボタンをdivで包み<style>+!importantでflex化
    # =インライン整列に参加させない)に統一(稜ヒント 2026-07-19)
    youtube_buttons = gr.HTML(
        value=f'''<style>
            #youtube-session-btn-row {{ display:flex !important; gap:8px !important; margin-top:4px !important; }}
            #youtube-session-btn-row > div {{ flex:0 0 auto !important; }}
            #youtube-session-btn-row button {{
                display:flex !important; align-items:center !important;
                justify-content:center !important; height:30px !important;
                padding:0 16px !important; font-size:12px !important;
                color:white !important; border:none !important;
                border-radius:4px !important; cursor:pointer;
            }}
        </style>
        <div id="youtube-session-btn-row">
            <div><button id="youtube-loop-btn" title="{t('youtube.auto_loop_tooltip')}"
                onclick="window._youtubeLoopBtnClick && window._youtubeLoopBtnClick()"
                style="background:{loop_color};">{loop_label}</button></div>
            <div><button id="youtube-session-btn"
                title="{t('youtube.start_session_tooltip')}"
                onclick="window._youtubeSessionBtnClick && window._youtubeSessionBtnClick()"
                style="background:#2196f3;">{t('youtube.start_session')}</button></div>
        </div>''',
        elem_id="youtube-session-btn-container"
    )
    components["session_btn"] = youtube_buttons

    gr.Markdown("---")

    # 返信の向き（返信するチャンネル → 返信対象のチャンネル）を常時表示
    # (見出しは他セクションと同じ左置きMarkdown — 稜指示 2026-07-19)
    gr.Markdown(f"### {t('youtube.flow_title')}")
    flow_display = gr.HTML(_channel_flow_html())
    components["flow_display"] = flow_display

    gr.Markdown("---")

    # ----- Authorization (stage B GUI — same auth.py as the stage-A CLI) -----
    gr.Markdown(f"### {t('youtube.auth_heading')}")
    gr.Markdown(t('youtube.auth_desc'))
    cs_status_display = gr.HTML(_client_secret_status_html())
    components["cs_status_display"] = cs_status_display
    client_secret_file = gr.File(
        label=t('youtube.client_secret'), file_types=[".json"], type="filepath")
    # 認可の2ステップは「発行 → 確認」の矢印付き横並び（手順が読める配置 — 稜指示。
    # 矢印列は scale=0+固定幅+flex中央寄せ: 可変幅だと行が折り返して矢印が
    # ボタンの下にずれる — 稜実踏 2026-07-12）
    with gr.Row(equal_height=True):
        issue_url_btn = gr.Button(t('youtube.issue_auth_url'), variant="primary",
                                  size="sm", scale=5)
        with gr.Column(scale=0, min_width=40):
            gr.HTML('<div style="display:flex;align-items:center;justify-content:center;'
                    'height:100%;min-height:32px;font-size:22px;color:#8bc34a;">→</div>')
        check_auth_btn = gr.Button(t('youtube.check_auth'), size="sm", scale=5)
    auth_url_display = gr.Markdown("", visible=False)
    auth_status = gr.Markdown("", visible=False, elem_id="youtube-auth-status")
    components["issue_url_btn"] = issue_url_btn
    components["check_auth_btn"] = check_auth_btn

    # 2段方式（2026-07-12変更）: ブラウザは自動で開かない。①URLを発行して表示
    # →ユーザーが「返信チャンネルにログインしているプロファイル」で開いて認可
    # →②確認ボタンで結果を反映。発行は何度押してもよい（前の待受を作り直す）。
    def handle_issue_url(file_path):
        from backend.youtube import auth
        if file_path:
            imported = auth.import_client_secret(file_path)
            if not imported["success"]:
                return (gr.update(visible=False),
                        gr.update(value=t('youtube.auth_failed',
                                          error=imported["error"]), visible=True),
                        _client_secret_status_html())
        if not auth.has_client_secret():
            return (gr.update(visible=False),
                    gr.update(value=t('youtube.client_secret_missing'), visible=True),
                    _client_secret_status_html())
        result = auth.begin_authorization_flow()
        if not result["success"]:
            return (gr.update(visible=False),
                    gr.update(value=t('youtube.auth_failed',
                                      error=result["error"]), visible=True),
                    _client_secret_status_html())
        url = result["auth_url"]
        url_md = (f"{t('youtube.auth_url_ready')}\n\n"
                  f"[{t('youtube.auth_url_link')}]({url})\n\n"
                  f"```\n{url}\n```")
        return (gr.update(value=url_md, visible=True),
                gr.update(visible=False),
                _client_secret_status_html())

    # 一過性の結果通知(auth_status)のみ自動消去。認可URL(auth_url_display)・
    # client_secret状態(cs_status_display)・返信の向き(flow_display)は
    # 作業データ/状態表示なので消さない(稜裁定 2026-08-15)。
    issue_url_btn.click(fn=handle_issue_url,
                        inputs=[client_secret_file],
                        outputs=[auth_url_display, auth_status, cs_status_display]
                        ).then(fn=lambda: None, inputs=[], outputs=[],
                               js=status_auto_hide_js('youtube-auth-status'),
                               queue=False)

    def handle_check_auth():
        from backend.youtube import auth
        status = auth.get_authorization_status()
        state = status.get("status", "idle")
        if state == "pending":
            message = t('youtube.auth_pending')
        elif state == "success":
            message = t('youtube.auth_success',
                        name=status.get("channel_title") or "?")
        elif state == "error":
            message = t('youtube.auth_failed', error=status.get("error", "?"))
        else:  # idle / cancelled — nothing in flight; the flow block tells the state
            message = t('youtube.cs_hint_idle')
        return (gr.update(value=message, visible=True),
                _channel_flow_html())

    check_auth_btn.click(fn=handle_check_auth, inputs=[],
                         outputs=[auth_status, flow_display]
                         ).then(fn=lambda: None, inputs=[], outputs=[],
                                js=status_auto_hide_js('youtube-auth-status'),
                                queue=False)

    gr.Markdown("---")

    # ----- Schedule (ELYTH制御タブの「スケジュール設定」と同型 — 稜指示 2026-07-19) -----
    gr.Markdown(f"### {t('youtube.schedule')}")
    with gr.Row():
        interval_num = gr.Number(
            label=t('youtube.interval'),
            value=max(MIN_INTERVAL_MINUTES,
                      int(get_setting("youtube", "interval_seconds",
                                      DEFAULT_INTERVAL_MINUTES * 60)) // 60),
            minimum=MIN_INTERVAL_MINUTES, maximum=1440, precision=0)
        daily_limit_num = gr.Number(
            label=t('youtube.daily_limit'),
            value=int(get_setting("youtube", "daily_post_limit", 30)),
            minimum=1, maximum=200, precision=0)

    gr.Markdown("---")

    # ----- Settings (settings_store "youtube" namespace) -----
    gr.Markdown(f"### {t('youtube.settings_heading')}")

    char_choices = _list_characters()
    saved_char = get_setting("youtube", "character_id", "")
    if saved_char and saved_char not in [cid for _, cid in char_choices]:
        saved_char = ""

    # 有効/無効のマスタートグルは上の状態表示ボタン（youtube-loop-btn）が担う
    dry_run_cb = gr.Checkbox(
        label=t('youtube.dry_run'),
        info=t('youtube.dry_run_desc'),
        value=bool(get_setting("youtube", "dry_run", False)))
    char_dropdown = gr.Dropdown(
        label=t('youtube.character'),
        choices=char_choices,
        value=saved_char or None)
    # 担当キャラのYouTube返信用システムプロンプト（タブ内で直接編集できるように —
    # 稜指示 2026-07-12。キャラ編集フォームの同項目と同じconfigフィールドを読み書き）
    youtube_prompt_tb = gr.Textbox(
        label=t('youtube.system_prompt'),
        info=t('youtube.system_prompt_desc'),
        value=_load_char_youtube_prompt(saved_char),
        lines=8, max_lines=20)
    char_dropdown.change(fn=_load_char_youtube_prompt,
                         inputs=[char_dropdown], outputs=[youtube_prompt_tb])
    # 解決結果の表示は上部のフローボックスに一本化（重複表示は稜指示で削除）
    target_tb = gr.Textbox(
        label=t('youtube.target_channel'),
        placeholder=t('youtube.target_channel_ph'),
        value=get_setting("youtube", "target_channel_input", ""))
    # 投稿クールタイム(1〜5分ランダム)はUI項目にしない(2026-07-12 稜指示 —
    # いじる意味が薄い)。値は settings_store の youtube.cooldown_min/max
    # (既定60/300秒)のままで、変えたい場合のみ user_settings.json を直接編集。
    rules_tb = gr.Textbox(
        label=t('youtube.rules'),
        info=t('youtube.rules_desc'),
        value=get_setting("youtube", "rules", ""),
        lines=5, max_lines=15)

    save_btn = gr.Button(t('youtube.save'), variant="primary")
    save_status = gr.Markdown("", visible=False, elem_id="youtube-save-status")
    components["save_btn"] = save_btn

    def handle_save(dry_run, character_id, target_input,
                    interval_min, daily_limit, rules, youtube_prompt):
        try:
            from backend.shared.settings_store import update_setting

            # J8 fail-safe: the 60-min floor is enforced here too (and again
            # on read in the session manager).
            interval_min = max(MIN_INTERVAL_MINUTES, int(interval_min or 0))

            # Resolve the target channel (@handle → channel id). Resolution
            # needs an authorized token; keep the raw input in that case so the
            # user can re-save after authorizing.
            # 形式が @ハンドルでないときはチャンネル3項目を1つも書かない: 書くと
            # 入力欄には不正な文字列が残る一方で実際の返信先は前回解決済みの
            # target_channel_id(別チャンネル)のままになり、画面と実挙動が食い違う
            # (=返信先を間違える)。他の設定は通常どおり保存する。
            resolve_note = ""
            target_input = (target_input or "").strip()
            resolved_id = ""
            resolved_title = ""
            save_target = True
            if target_input:
                from backend.youtube.youtube_api import HANDLE_RE
                if not HANDLE_RE.match(target_input):
                    save_target = False
                    resolve_note = t('youtube.channel_handle_invalid')
                else:
                    try:
                        from backend.youtube import youtube_api
                        resolved = youtube_api.resolve_channel(target_input)
                        resolved_id = resolved["channel_id"]
                        resolved_title = resolved["title"]
                    except Exception as e:
                        resolve_note = t('youtube.channel_resolve_failed', error=e)

            update_setting("youtube", "dry_run", bool(dry_run))
            update_setting("youtube", "character_id", character_id or "")
            if save_target:
                update_setting("youtube", "target_channel_input", target_input)
                if resolved_id:
                    update_setting("youtube", "target_channel_id", resolved_id)
                    update_setting("youtube", "target_channel_title", resolved_title)
            update_setting("youtube", "interval_seconds", interval_min * 60)
            update_setting("youtube", "daily_post_limit", int(daily_limit or 30))
            update_setting("youtube", "rules", rules or "")

            try:
                from backend.youtube.youtube_session_manager import (
                    get_youtube_session_manager,
                )
                get_youtube_session_manager().reload_settings()
            except Exception:
                pass

            message = t('youtube.saved')
            if resolve_note:
                message += "\n\n" + resolve_note

            # 担当キャラのシステムプロンプトはキャラconfigへ（変更時のみ書く —
            # 無変更でもedit_characterを呼ぶとアクティブキャラの再activateが走るため）
            if character_id:
                try:
                    new_prompt = (youtube_prompt or "").strip()
                    if new_prompt != _load_char_youtube_prompt(character_id).strip():
                        import backend
                        backend.edit_character(
                            character_id, {"youtube_system_prompt": new_prompt})
                except Exception as e:
                    message += "\n\n" + t('youtube.prompt_save_failed', error=e)

            return (gr.update(value=message, visible=True), _channel_flow_html())
        except Exception as e:
            return (gr.update(value=t('youtube.save_error', error=e), visible=True),
                    gr.update())

    save_btn.click(
        fn=handle_save,
        inputs=[dry_run_cb, char_dropdown, target_tb,
                interval_num, daily_limit_num, rules_tb, youtube_prompt_tb],
        outputs=[save_status, flow_display],
    ).then(
        fn=lambda: None, inputs=[], outputs=[],
        js=status_auto_hide_js('youtube-save-status'), queue=False
    )

    components.update({
        "dry_run": dry_run_cb,
        "character": char_dropdown, "target_channel": target_tb,
        "interval": interval_num, "daily_limit": daily_limit_num,
        "rules": rules_tb,
    })
    return components
