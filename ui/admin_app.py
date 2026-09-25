"""
ui/admin_app.py

Admin panel Gradio app mounted at /admin in server mode.
Provides server status, session monitoring, and management controls.
"""

import json
import logging

import gradio as gr

from backend.shared.i18n import t

from .status_js import status_auto_hide_js, status_show_js

logger = logging.getLogger(__name__)


def _js(text: str) -> str:
    """t() 済み文字列を JS 文字列リテラルとして埋め込む(json.dumps でエスケープ)。"""
    return json.dumps(text, ensure_ascii=False)


# status_msg の自動消去(稜依頼 2026-08-03: 強制切断等のフィードバックが
# 消えない)。本体は ui/status_js.py（合成根 app.py に依存しない葉モジュール
# なので standalone の admin からも import 可＝旧インライン複製を撤去）。
# HIDE=表示してから一定時間で隠す(単発書き込みイベント用)。
# SHOW=前回の隠し状態とタイマーを解除するだけ(再起動/シャットダウン等、
# 出しっぱなしにしたいイベントの先頭用 — 隠れたまま新メッセージが
# 見えなくなる事故の防止)。
_ADMIN_STATUS_HIDE_JS = status_auto_hide_js('admin-status-msg')
_ADMIN_STATUS_SHOW_JS = status_show_js('admin-status-msg')

# 記憶タスク中に無効化する3ボタン(ローカル切替/再起動/シャットダウン)の見え方。
# Gradio 5.15 の Button.svelte は `.secondary[disabled]` / `.stop[disabled]` に
# :hover と同じ背景色を使い回し、Soft テーマのダークでは hover 色が
# primary_500(=blue #3b82f6) なので「押せないボタンだけ青く目立つ」逆転が
# 起きていた(Mac 実機 2026-08-16・ローカル UI は Default テーマ=hover がグレー
# で無問題)。無効時だけ有効時と同じ背景/文字色に戻す=Gradio 側の半透明
# (opacity .5)+not-allowed はそのまま生きて「グレー半透明」になる。詳細度
# (0,3,1) は svelte スコープ (0,3,0) に勝つので !important 不要。
_ADMIN_DISABLED_BTN_CSS = """
.gradio-container button.secondary[disabled] {
    background: var(--button-secondary-background-fill);
    color: var(--button-secondary-text-color);
}
.gradio-container button.stop[disabled] {
    background: var(--button-cancel-background-fill);
    color: var(--button-cancel-text-color);
}
"""


def create_admin_ws_js(timeout_minutes: int) -> str:
    """Generate JavaScript for admin WebSocket client and real-time timers."""
    return f"""
    async () => {{
        if (window.adminWs) return;

        // Session data store
        window.agSessionData = null;

        // WebSocket URL: use /ws endpoint on same port (server mode)
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;  // includes port
        const url = protocol + '//' + host + '/ws';

        // Make-visible-then-click helper (Gradio ignores click events on
        // display:none elements — session_trigger と同じパターンの共通化)
        window._agClickAdminTrigger = function(sel) {{
            const trigger = document.querySelector(sel);
            if (!trigger) return;
            // Always restore to 'none', never to a captured value: two calls
            // within the 10ms window would otherwise capture 'block' and leave
            // the button stuck visible (and self-perpetuating from then on).
            trigger.style.display = 'block';
            trigger.click();
            setTimeout(() => {{ trigger.style.display = 'none'; }}, 10);
        }};

        // Reusable connect function for initial connection and reconnects
        window._adminWsConnect = function() {{
            if (window.adminWs) return;
            console.log('[Admin WS] Connecting to', url);
            const ws = new WebSocket(url);

            ws.onopen = () => {{
                console.log('[Admin WS] Connected');
                ws.send(JSON.stringify({{type: 'identify', client_type: 'admin'}}));
                console.log('[Admin WS] Identify sent: admin');
                // (再)接続時に抽出状態を実照会で再同期 — 切断中に取り逃した
                // extraction_* イベントの自己修復(インジケーター改修の
                // onopen再発行と同じ先例)。
                setTimeout(() => window._agClickAdminTrigger('#admin-extraction-trigger'), 500);
            }};

            ws.onmessage = (event) => {{
                try {{
                    const data = JSON.parse(event.data);

                    if (data.type === 'connected') {{
                        console.log('[Admin WS] Server acknowledged');
                    }} else if (data.type === 'identify_response') {{
                        console.log('[Admin WS] Identify response:', data.status);
                    }} else if (data.type === 'session_status_update') {{
                        console.log('[Admin WS] Session update:', data.session);
                        window.agSessionData = data;
                        // Trigger Gradio update (must make visible first — Gradio
                        // ignores click events on display:none elements)
                        window._agClickAdminTrigger('#admin-session-update-trigger');
                    }} else if (data.action === 'extraction_started' ||
                               data.action === 'extraction_completed') {{
                        // メモリ抽出の開始/完了 → バナー表示とボタン活性を再同期
                        // (broadcastはadminにも素で届く — 従来は無視していた)
                        console.log('[Admin WS] Extraction state:', data.action);
                        window._agClickAdminTrigger('#admin-extraction-trigger');
                    }}
                }} catch (e) {{
                    console.error('[Admin WS] Parse error:', e);
                }}
            }};

            ws.onclose = () => {{
                console.log('[Admin WS] Disconnected');
                window.adminWs = null;
                // Reconnect after 3s
                setTimeout(() => window._adminWsConnect(), 3000);
            }};

            ws.onerror = (e) => {{
                console.error('[Admin WS] Error:', e);
            }};

            window.adminWs = ws;
        }};

        window._adminWsConnect();

        // Real-time timer (updates every second)
        window._adminTimerInterval = setInterval(() => {{
            const data = window.agSessionData;
            if (!data || !data.session || !data.session.active) {{
                const connEl = document.getElementById('admin-conn-time');
                if (connEl) connEl.textContent = '--:--:--';
                const timeoutEl = document.getElementById('admin-timeout-remain');
                if (timeoutEl) timeoutEl.textContent = '--:--:--';
                return;
            }}

            const now = Date.now() / 1000;
            const session = data.session;

            // Connection time
            const connSec = now - session.connected_at;
            const connEl = document.getElementById('admin-conn-time');
            if (connEl) connEl.textContent = window._formatHHMMSS(connSec);

            // Timeout remaining
            const idleSec = now - session.last_activity;
            const timeoutTotal = (data.timeout_minutes || {timeout_minutes}) * 60;
            const remain = Math.max(0, timeoutTotal - idleSec);
            const timeoutEl = document.getElementById('admin-timeout-remain');
            if (timeoutEl) timeoutEl.textContent = window._formatHHMMSS(remain);

            // Grace countdown (pending_reconnect session)
            const graceEl = document.getElementById('admin-grace-remain');
            if (graceEl && session.pending_reconnect && session.pending_reconnect_until) {{
                graceEl.textContent = window._formatHHMMSS(
                    Math.max(0, session.pending_reconnect_until - now));
            }}
        }}, 1000);

        // Helper: format seconds to HH:MM:SS
        window._formatHHMMSS = function(totalSec) {{
            const h = Math.floor(totalSec / 3600);
            const m = Math.floor((totalSec % 3600) / 60);
            const s = Math.floor(totalSec % 60);
            return String(h).padStart(2, '0') + ':' +
                   String(m).padStart(2, '0') + ':' +
                   String(s).padStart(2, '0');
        }};

        // Event delegation for all admin buttons
        // (Gradio strips onclick attributes from gr.HTML content)
        document.addEventListener('click', function(e) {{
            // URL Show/Hide toggle
            const toggleUrlBtn = e.target.closest('.admin-toggle-url-btn');
            if (toggleUrlBtn) {{
                const row = toggleUrlBtn.parentElement;
                const code = row ? row.querySelector('.admin-url-value') : null;
                if (code) {{
                    if (code.dataset.visible === 'true') {{
                        code.textContent = '';
                        code.dataset.visible = 'false';
                        toggleUrlBtn.textContent = {_js(t('admin.show'))};
                    }} else {{
                        code.textContent = code.dataset.url;
                        code.dataset.visible = 'true';
                        toggleUrlBtn.textContent = {_js(t('admin.hide'))};
                    }}
                }}
                return;
            }}

            // URL Copy
            const copyUrlBtn = e.target.closest('.admin-copy-url-btn');
            if (copyUrlBtn) {{
                const url = copyUrlBtn.dataset.url;
                if (url) window._adminCopyText(url, copyUrlBtn);
                return;
            }}

            // IP Show/Hide toggle
            const toggleIpBtn = e.target.closest('.admin-toggle-ip-btn');
            if (toggleIpBtn) {{
                const row = toggleIpBtn.parentElement;
                const span = row ? row.querySelector('.ip-value') : null;
                if (span) {{
                    if (span.dataset.visible === 'true') {{
                        span.textContent = '\u25cf'.repeat(10);
                        span.dataset.visible = 'false';
                        toggleIpBtn.textContent = {_js(t('admin.show'))};
                    }} else {{
                        span.textContent = span.dataset.ip;
                        span.dataset.visible = 'true';
                        toggleIpBtn.textContent = {_js(t('admin.hide'))};
                    }}
                }}
                return;
            }}
        }});

        // Clipboard copy with visual feedback and fallback
        window._adminCopyText = function(text, btn) {{
            const orig = btn.textContent;
            function showSuccess() {{
                btn.textContent = {_js(t('admin.copied'))};
                btn.style.color = '#4caf50';
                setTimeout(() => {{ btn.textContent = orig; btn.style.color = ''; }}, 1500);
            }}
            function showFail() {{
                btn.textContent = {_js(t('admin.copy_failed'))};
                btn.style.color = '#f44336';
                setTimeout(() => {{ btn.textContent = orig; btn.style.color = ''; }}, 1500);
            }}
            try {{
                navigator.clipboard.writeText(text).then(showSuccess).catch(() => {{
                    // Fallback: textarea + execCommand
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.style.cssText = 'position:fixed;opacity:0;left:-9999px';
                    document.body.appendChild(ta);
                    ta.select();
                    try {{ document.execCommand('copy'); showSuccess(); }}
                    catch {{ showFail(); }}
                    document.body.removeChild(ta);
                }});
            }} catch {{
                showFail();
            }}
        }};

        // 返り値を返さない: js+fn 同居では js の返り値がイベント引数に化けるため、
        // 0引数の load エンドポイントに対して毎ページロードで
        // Parameter `0` is not a valid keyword argument が発生し、
        // on_session_update の初期同期ごとサブミットが中断されていた
        // (稜のDevTools実測 2026-08-08・モバイルの先例=0ad9bee)。
    }}
    """


def get_server_urls_html() -> str:
    """Generate HTML for server URL display with show/copy buttons."""
    try:
        from backend.server.tailscale import get_tailscale_hostname
        hostname = get_tailscale_hostname()
    except Exception:
        hostname = t('admin.hostname_unavailable')

    from backend.shared.launch_config import get_launch_config_value
    web_port = get_launch_config_value('launcher', 'web_port', 7860)
    desktop_url = f"https://{hostname}:{web_port}"
    mobile_url = f"https://{hostname}:{web_port}/mobile"
    btn_style = (
        "padding:4px 12px;border:1px solid #555;border-radius:4px;"
        "background:#333;color:#fff;cursor:pointer;font-size:12px;"
    )

    rows = []
    for label, url in [(t('admin.desktop_url'), desktop_url), (t('admin.mobile_url'), mobile_url)]:
        rows.append(f"""
        <div style="margin-bottom:10px;display:flex;align-items:center;gap:8px;">
            <span style="color:#aaa;min-width:130px;">{label}:</span>
            <code class="admin-url-value" data-url="{url}" data-visible="false"
                style="background:#2a2a2a;padding:4px 10px;border-radius:4px;
                       font-size:14px;min-width:200px;min-height:1.2em;display:inline-block;">
            </code>
            <button class="admin-toggle-url-btn" style="{btn_style}">{t('admin.show')}</button>
            <button class="admin-copy-url-btn" data-url="{url}" style="{btn_style}">{t('admin.copy')}</button>
        </div>
        """)

    return f'<div style="padding:12px 0;">{"".join(rows)}</div>'


def get_session_status_html() -> str:
    """Generate HTML for current session status display."""
    from backend.server.session_manager import get_session_manager
    sm = get_session_manager()
    status = sm.get_status()
    session = status["session"]
    companion = status.get("companion_session", {})

    if not session["active"]:
        return f"""
        <div style="padding:16px;text-align:center;color:#888;">
            <div style="font-size:18px;">{t('admin.no_connection')}</div>
        </div>
        """

    device = session["device_name"] or ""
    ip = session["client_ip"] or "Unknown"
    # Show device name if resolved, otherwise generic label
    display_name = device if device and device != ip else t('admin.device')
    btn_style = (
        "padding:2px 8px;border:1px solid #555;border-radius:3px;"
        "background:#333;color:#fff;cursor:pointer;font-size:11px;"
    )

    # Companion session info
    companion_html = ""
    if companion.get("active"):
        comp_device = companion.get("device_name", "")
        comp_ip = companion.get("client_ip", "Unknown")
        comp_display = comp_device if comp_device and comp_device != comp_ip else t('admin.device')
        companion_html = f"""
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid #444;">
            <div style="font-size:14px;font-weight:bold;margin-bottom:8px;color:#2196f3;">
                📷 {comp_display} {t('admin.companion_suffix')}
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
                <span style="color:#aaa;">IP:</span>
                <span class="ip-value" data-ip="{comp_ip}" data-visible="false"
                    style="font-family:monospace;">●●●●●●●●●●</span>
                <button class="admin-toggle-ip-btn" style="{btn_style}">{t('admin.show')}</button>
            </div>
        </div>
        """

    # Pending (grace window): the client vanished without an intentional close
    # — show an honest "waiting for reconnect" state with the grace countdown
    # instead of pretending it is a healthy connection.
    if session.get("pending_reconnect"):
        header_html = f"""
        <div style="font-size:16px;font-weight:bold;margin-bottom:12px;color:#ff9800;">
            ⏳ {t('admin.pending_reconnect', name=display_name)}
        </div>"""
        tail_html = f"""
        <div>
            <span style="color:#aaa;">{t('admin.grace_remain_label')}</span>
            <span id="admin-grace-remain" style="font-family:monospace;">--:--:--</span>
        </div>"""
    else:
        header_html = f"""
        <div style="font-size:16px;font-weight:bold;margin-bottom:12px;color:#4caf50;">
            {t('admin.connected', name=display_name)}
        </div>"""
        tail_html = f"""
        <div>
            <span style="color:#aaa;">{t('admin.timeout_label')}</span>
            <span id="admin-timeout-remain" style="font-family:monospace;">--:--:--</span>
        </div>"""

    return f"""
    <div style="padding:12px 0;">
        {header_html}
        <div style="margin-bottom:8px;display:flex;align-items:center;gap:8px;">
            <span style="color:#aaa;">IP:</span>
            <span class="ip-value" data-ip="{ip}" data-visible="false"
                style="font-family:monospace;">●●●●●●●●●●</span>
            <button class="admin-toggle-ip-btn" style="{btn_style}">{t('admin.show')}</button>
        </div>
        <div style="margin-bottom:6px;">
            <span style="color:#aaa;">{t('admin.conn_time_label')}</span>
            <span id="admin-conn-time" style="font-family:monospace;">00:00:00</span>
        </div>
        {tail_html}
        {companion_html}
    </div>
    """


def get_ssl_status_html() -> str:
    """Generate HTML for SSL certificate status."""
    try:
        from backend.server.tailscale import (
            get_tailscale_hostname, find_cert_files, check_cert_expiry
        )
        hostname = get_tailscale_hostname()
        # find_cert_files は 2-tuple か None を返す。None を直接タプル分解すると
        # TypeError になり下の None チェックに届かず「見つかりません」分岐が
        # 死んでいた。先に None を判定してから分解する。
        cert_files = find_cert_files(hostname)
        if cert_files is None:
            return f"""
            <div style="padding:8px;color:#f44336;">
                {t('admin.ssl_not_found')}
            </div>
            """
        cert_path, _key_path = cert_files

        days_remaining = check_cert_expiry(cert_path)

        from cryptography import x509
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        expiry_date = cert.not_valid_after_utc.strftime("%Y-%m-%d")

        if days_remaining < 0:
            color = "#f44336"
            label = t('admin.ssl_expired')
        elif days_remaining < 30:
            color = "#ff9800"
            label = t('admin.ssl_valid')
        else:
            color = "#4caf50"
            label = t('admin.ssl_valid')

        return f"""
        <div style="padding:8px 0;">
            <span style="color:{color};font-weight:bold;">{label}</span>
            <span style="color:#ccc;">{t('admin.ssl_days', days=days_remaining, date=expiry_date)}</span>
            <div style="color:#888;font-size:12px;margin-top:4px;">
                {t('admin.ssl_auto_renew_note')}
            </div>
        </div>
        """
    except Exception as e:
        logger.warning(f"[Admin] SSL status error: {e}")
        return f"""
        <div style="padding:8px;color:#ff9800;">
            {t('admin.ssl_info_failed', error=e)}
        </div>
        """


def handle_force_disconnect():
    """Handle force disconnect button click."""
    from backend.server.session_manager import get_session_manager
    sm = get_session_manager()
    result = sm.force_disconnect()
    if result:
        msg = f"<div style='color:#4caf50;padding:8px;'>{t('admin.force_disconnected')}</div>"
    else:
        msg = f"<div style='color:#ff9800;padding:8px;'>{t('admin.no_active_session')}</div>"
    # Return updated session HTML, force disconnect visibility, and message
    session_html = get_session_status_html()
    status = sm.get_status()
    has_session = status["session"]["active"]
    return session_html, gr.update(visible=has_session), msg


def get_extraction_banner_html() -> str:
    """Memory-task-block banner (empty string while idle).

    Dedicated element — status_msg はクリック結果の書き手が複数いるため、
    抽出バナーは書き手一人の専用HTMLに分離(完了時クリアで正当なメッセージを
    踏み潰さない)。判定は backend.is_memory_task_running(抽出+relationship)。
    """
    import backend
    if backend.is_memory_task_running():
        return (
            "<div style='color:#ff9800;padding:8px;"
            "border:1px solid rgba(255,152,0,.5);border-radius:6px;"
            "background:rgba(255,152,0,.08);'>"
            f"{t('admin.extracting_wait')}</div>"
        )
    return ""


def on_extraction_status():
    """WS/load-triggered refresh: banner + interactive state of blocked buttons.

    backend.is_memory_task_running() を毎回実照会する(イベントは再同期の合図で
    あって真実源ではない)。対象=再起動/シャットダウン/ローカル切替の3ボタン。
    """
    import backend
    extracting = backend.is_memory_task_running()
    return (
        get_extraction_banner_html(),
        gr.update(interactive=not extracting),
        gr.update(interactive=not extracting),
        gr.update(interactive=not extracting),
    )


def handle_switch_to_local():
    """Handle switch to local mode button click."""
    import backend
    from backend.server.mode_switch import prepare_switch_to_local

    # 記憶タスク中ガード(稜依頼 2026-07-25): restart/shutdownと同じ防御。従来
    # このボタンだけ無ガードで、抽出中に押すと即再起動が走っていた。
    if backend.is_memory_task_running():
        return f"<div style='color:#ff9800;padding:8px;'>{t('admin.extracting_wait')}</div>"

    ok, err = prepare_switch_to_local()
    if not ok:
        return f"<div style='color:#f44336;padding:8px;'>{err}</div>"

    # Trigger restart
    try:
        import threading
        from ui.app import _execute_shutdown
        threading.Thread(
            target=_execute_shutdown,
            args=(True,),
            name="switch-to-local",
            daemon=False
        ).start()
    except Exception as e:
        return f"<div style='color:#f44336;padding:8px;'>{t('admin.restart_failed', error=e)}</div>"

    return f"<div class='shutdown-initiated' style='color:#4caf50;padding:8px;'>{t('admin.switching_local')}</div>"


def handle_admin_restart():
    """Handle restart button click on the admin Control page (ST-G)."""
    import backend
    if backend.is_memory_task_running():
        return f"<div style='color:#ff9800;padding:8px;'>{t('admin.extracting_wait')}</div>"
    try:
        import threading
        from ui.app import _execute_shutdown
        threading.Thread(
            target=_execute_shutdown,
            args=(True,),
            name="admin-restart",
            daemon=False,
        ).start()
    except Exception as e:
        return f"<div style='color:#f44336;padding:8px;'>{t('admin.restart_failed', error=e)}</div>"
    return f"<div class='shutdown-initiated' style='color:#2196f3;padding:8px;'>{t('admin.restarting')}</div>"


def handle_admin_shutdown():
    """Handle shutdown button click on the admin Control page (ST-G)."""
    import backend
    if backend.is_memory_task_running():
        return f"<div style='color:#ff9800;padding:8px;'>{t('admin.extracting_wait')}</div>"
    try:
        import threading
        from ui.app import _execute_shutdown
        threading.Thread(
            target=_execute_shutdown,
            args=(False,),
            name="admin-shutdown",
            daemon=False,
        ).start()
    except Exception as e:
        return f"<div style='color:#f44336;padding:8px;'>{t('admin.shutdown_failed', error=e)}</div>"
    return f"<div class='shutdown-initiated' style='color:#4caf50;padding:8px;'>{t('admin.shutting_down')}</div>"


def handle_cert_renew():
    """Handle SSL certificate manual renewal."""
    try:
        from backend.server.tailscale import (
            get_tailscale_hostname, run_tailscale_cert,
            find_cert_files, check_cert_expiry,
        )
        hostname = get_tailscale_hostname()

        # Check days remaining before renewal attempt
        days_before = None
        existing = find_cert_files(hostname)
        if existing:
            days_before = check_cert_expiry(existing[0])

        run_tailscale_cert(hostname)

        # Check days remaining after renewal attempt
        ssl_html = get_ssl_status_html()
        existing_after = find_cert_files(hostname)
        days_after = check_cert_expiry(existing_after[0]) if existing_after else None

        if days_before is not None and days_after is not None and days_after > days_before:
            msg = f"<div style='color:#4caf50;padding:8px;'>{t('admin.ssl_renewed', days=days_after)}</div>"
        else:
            msg = (
                "<div style='color:#2196f3;padding:8px;'>"
                f"{t('admin.ssl_not_renewed')}</div>"
            )
        return ssl_html, msg
    except Exception as e:
        ssl_html = get_ssl_status_html()
        return ssl_html, f"<div style='color:#f44336;padding:8px;'>{t('admin.ssl_renew_failed', error=e)}</div>"


def on_session_update():
    """Called when WS session_status_update triggers the hidden button."""
    session_html = get_session_status_html()
    from backend.server.session_manager import get_session_manager
    status = get_session_manager().get_status()
    has_session = status["session"]["active"]
    return session_html, gr.update(visible=has_session)


def create_admin_interface(timeout_minutes: int) -> gr.Blocks:
    """
    Create the admin panel Gradio app.

    Args:
        timeout_minutes: Session timeout in minutes

    Returns:
        gr.Blocks instance to mount at /admin
    """
    from ui.pages import FOOTER_HIDE_CSS, get_sidebar_css
    from ui.local_fonts import LOCAL_FONTS_CSS
    from ui.license_notice import license_notice_html

    admin_demo = gr.Blocks(
        title="AG Admin",
        theme=gr.themes.Soft(
            primary_hue="blue",
            # デスクトップUIと同じ同梱Source Sans 3(文字列=LocalFont指定・
            # @font-faceはLOCAL_FONTS_CSSが供給。外部通信ゼロ方針=稜裁定2026-07-19)
            font=["Source Sans 3", "ui-sans-serif", "system-ui", "sans-serif"],
        ),
        css=LOCAL_FONTS_CSS + get_sidebar_css() + FOOTER_HIDE_CSS + _ADMIN_DISABLED_BTN_CSS,
    )

    def admin_switch_page(page_id, btn_id):
        is_control = (page_id == "control")
        return [
            page_id,
            gr.update(visible=is_control),
            gr.update(visible=not is_control),
            gr.update(
                elem_classes="nav-btn active"
                if btn_id == "admin-nav-control" else "nav-btn"
            ),
            gr.update(
                elem_classes="nav-btn active"
                if btn_id == "admin-nav-logs" else "nav-btn"
            ),
        ]

    with admin_demo:
        with gr.Row():
            # === Sidebar ===
            with gr.Column(scale=1, elem_id="sidebar"):
                gr.Markdown("## Artificial Girlfriend")
                admin_nav_control = gr.Button(
                    t('admin.nav_control'),
                    elem_classes="nav-btn active",
                    elem_id="admin-nav-control",
                )
                admin_nav_logs = gr.Button(
                    t('logs.tab.system'),
                    elem_classes="nav-btn",
                    elem_id="admin-nav-logs",
                )

            # === Content area ===
            with gr.Column(scale=4):
                admin_current_page = gr.State("control")

                # --- Control page ---
                with gr.Column(visible=True) as admin_page_control:
                    gr.Markdown(f"## {t('system.server_mode')}")

                    # URL display
                    url_html = gr.HTML(value=get_server_urls_html())

                    # メモリ抽出中バナー(専用要素・callable value=ページロード毎に
                    # 実照会。以後はWS→extraction_trigger経由で更新)
                    extraction_banner = gr.HTML(value=get_extraction_banner_html)

                    # Action buttons row
                    with gr.Row():
                        switch_local_btn = gr.Button(
                            t('tray.switch_to_local'),
                            variant="secondary",
                            scale=1,
                        )
                        force_disconnect_btn = gr.Button(
                            t('admin.force_disconnect_btn'),
                            variant="stop",
                            visible=False,
                            scale=1,
                        )
                        restart_btn = gr.Button(
                            t('admin.restart_btn'),
                            variant="secondary",
                            scale=1,
                        )
                        shutdown_btn = gr.Button(
                            t('admin.shutdown_btn'),
                            variant="stop",
                            scale=1,
                        )
                        cert_renew_btn = gr.Button(
                            t('admin.ssl_renew_btn'),
                            variant="secondary",
                            scale=1,
                        )

                    # Connection status
                    with gr.Group():
                        gr.Markdown(f"### {t('admin.connection_status')}")
                        session_html = gr.HTML(
                            value=get_session_status_html(),
                            elem_id="admin-session-info",
                        )

                    # SSL certificate
                    with gr.Group():
                        gr.Markdown(f"### {t('admin.ssl_cert')}")
                        ssl_html = gr.HTML(
                            value=get_ssl_status_html(),
                            elem_id="admin-ssl-info",
                        )

                    # Status message area (自動消去は _ADMIN_STATUS_*_JS が制御)
                    status_msg = gr.HTML(value="", elem_id="admin-status-msg")

                    # Hidden trigger buttons for the WS -> Gradio bridge.
                    # Wrapped in a hidden Row like the desktop and mobile UIs
                    # (app.py:1896 / mobile_app.py): with visible=False on the
                    # button alone, anything that forces display:block onto it
                    # surfaces a stray "trigger" button in the panel.
                    with gr.Row(visible=False):
                        session_trigger = gr.Button(
                            "trigger",
                            visible=False,
                            elem_id="admin-session-update-trigger",
                        )
                        # Extraction started/completed (WS -> Gradio)
                        extraction_trigger = gr.Button(
                            "trigger",
                            visible=False,
                            elem_id="admin-extraction-trigger",
                        )

                # --- System Logs page ---
                with gr.Column(visible=False) as admin_page_logs:
                    from ui.components import (
                        create_log_panel, update_log_view, update_prompt_view
                    )

                    with gr.Tabs():
                        with gr.Tab(t('logs.tab.system')):
                            log_container, log_components = create_log_panel()
                            log_container.visible = True
                            log_textbox = log_components["log_textbox"]
                            log_textbox.value = update_log_view()

                            # Auto-refresh timer
                            log_timer = gr.Timer(1.0)
                            log_timer.tick(
                                fn=update_log_view,
                                outputs=[log_textbox],
                            )

                        with gr.Tab(t('logs.tab.prompt')):
                            with gr.Column():
                                gr.Markdown(f"### {t('logs.prompt_title')}")
                                with gr.Row():
                                    gr.Markdown("")
                                    prompt_refresh_btn = gr.Button(
                                        t('common.refresh'), size="sm", scale=0
                                    )

                                prompt_textbox = gr.Textbox(
                                    label="",
                                    value=update_prompt_view(),
                                    lines=30,
                                    max_lines=50,
                                    interactive=False,
                                    show_copy_button=True,
                                )

                                prompt_refresh_btn.click(
                                    fn=update_prompt_view,
                                    outputs=[prompt_textbox],
                                )

        # === Page navigation events ===
        page_outputs = [
            admin_current_page, admin_page_control, admin_page_logs,
            admin_nav_control, admin_nav_logs,
        ]
        admin_nav_control.click(
            fn=lambda: admin_switch_page("control", "admin-nav-control"),
            inputs=[],
            outputs=page_outputs,
        )
        admin_nav_logs.click(
            fn=lambda: admin_switch_page("logs", "admin-nav-logs"),
            inputs=[],
            outputs=page_outputs,
        )

        # === Button events ===
        force_disconnect_btn.click(
            fn=handle_force_disconnect,
            outputs=[session_html, force_disconnect_btn, status_msg],
        ).then(
            # フィードバックは約10秒で自動消去(稜依頼 2026-08-03)
            fn=lambda: None, inputs=[], outputs=[],
            js=_ADMIN_STATUS_HIDE_JS, queue=False
        )

        switch_local_btn.click(
            fn=handle_switch_to_local,
            outputs=[status_msg],
        ).then(
            # 前回の自動消去で隠れたままにならないよう再表示のみ(UIが閉じる
            # 系のメッセージは消さない)
            fn=lambda: None, inputs=[], outputs=[],
            js=_ADMIN_STATUS_SHOW_JS, queue=False
        ).then(
            fn=lambda: None,
            inputs=[],
            outputs=[],
            js="() => { if(document.querySelector('.shutdown-initiated')) { setTimeout(() => window.close(), 1000); } }"
        )

        # ST-G: restart/shutdown from the admin Control page.
        # The js confirm throws on cancel, which aborts the Gradio event.
        restart_btn.click(
            fn=handle_admin_restart,
            outputs=[status_msg],
            js=f"() => {{ if(!confirm({_js(t('admin.confirm_restart'))})) {{ throw new Error('cancelled'); }} }}",
        ).then(
            # 再表示のみ(再起動メッセージは消さない・UI側が再接続表示に遷移)
            fn=lambda: None, inputs=[], outputs=[],
            js=_ADMIN_STATUS_SHOW_JS, queue=False
        )

        shutdown_btn.click(
            fn=handle_admin_shutdown,
            outputs=[status_msg],
            js=f"() => {{ if(!confirm({_js(t('admin.confirm_shutdown'))})) {{ throw new Error('cancelled'); }} }}",
        ).then(
            # 再表示のみ(シャットダウンメッセージは消さない・window.close が続く)
            fn=lambda: None, inputs=[], outputs=[],
            js=_ADMIN_STATUS_SHOW_JS, queue=False
        ).then(
            fn=lambda: None,
            inputs=[],
            outputs=[],
            js="() => { if(document.querySelector('.shutdown-initiated')) { setTimeout(() => window.close(), 1000); } }"
        )

        cert_renew_btn.click(
            fn=handle_cert_renew,
            outputs=[ssl_html, status_msg],
        ).then(
            # フィードバックは約10秒で自動消去(稜依頼 2026-08-03)
            fn=lambda: None, inputs=[], outputs=[],
            js=_ADMIN_STATUS_HIDE_JS, queue=False
        )

        session_trigger.click(
            fn=on_session_update,
            outputs=[session_html, force_disconnect_btn],
        )

        # 抽出開始/完了(WS)→ バナー+3ボタンのグレーアウト切替
        extraction_trigger.click(
            fn=on_extraction_status,
            outputs=[extraction_banner, switch_local_btn, restart_btn, shutdown_btn],
        )

        # On page load: refresh session status (F5) AND initialize admin WS.
        # Combined into a single demo.load() because Gradio may skip JS
        # execution when demo.load() is called with js= but without fn=.
        admin_demo.load(
            fn=on_session_update,
            inputs=[],
            outputs=[session_html, force_disconnect_btn],
            js=create_admin_ws_js(timeout_minutes),
        )

        # 抽出中にAdminを開いた/リロードした場合のボタン活性の初期同期
        # (バナーはcallable valueが同じ実照会で描画済み)
        admin_demo.load(
            fn=on_extraction_status,
            inputs=[],
            outputs=[extraction_banner, switch_local_btn, restart_btn, shutdown_btn],
        )

        # AGPL-3.0 section 13: offer the Corresponding Source to every user who
        # reaches this program over a network. Mounted at "/admin" in server mode.
        gr.HTML(license_notice_html(), elem_id="ag-license-notice")

    return admin_demo
