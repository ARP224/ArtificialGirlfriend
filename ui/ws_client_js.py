"""
ui/ws_client_js.py

WebSocket client initialization JavaScript generator (SL12 WS-Transport).

Extracted verbatim from ui/app.py (ST4 / B11a) as the first composition-root
slimming seam: a pure string builder. app.py re-imports the public name so
call sites are unchanged. The command-step status tables are injected from
ui/conversation/command_format.py (single truth source — M32) so the WS live
path cannot drift from the Python live/reload paths again.
"""

import json

from backend.shared.i18n import t
from .conversation.command_format import COMMAND_STATUS_ICONS, COMMAND_STATUS_LABELS
from .status_js import STATUS_AUTO_HIDE_MS


def _js(text: str) -> str:
    """t() 済み文字列を JS 文字列リテラルとして埋め込む(json.dumps でエスケープ)。"""
    return json.dumps(text, ensure_ascii=False)


def create_websocket_init_js(port: int, client_type: str = "desktop",
                             server_mode: bool = False,
                             feature_status: dict = None) -> str:
    """
    Create JavaScript code for WebSocket client initialization.

    Args:
        port: WebSocket server port number (used in local mode only)
        client_type: Client type for identify protocol (desktop|admin|mobile)
        server_mode: If True, use /ws endpoint on same port instead of separate port

    Returns:
        str: JavaScript code for initialization
    """
    fs = feature_status or {}
    pc_init = 'true' if fs.get('pc_status_enabled', False) else 'false'
    sc_init = 'true' if fs.get('screen_capture_enabled', False) else 'false'
    tt_init = 'true' if fs.get('talk_theme_enabled', True) else 'false'
    sl_init = 'true' if fs.get('speechless_enabled', False) else 'false'
    ce_init = 'true' if fs.get('command_execution_enabled', False) else 'false'
    nt_init = 'true' if fs.get('notes_enabled', False) else 'false'
    ig_init = 'true' if fs.get('image_generation_enabled', False) else 'false'
    cc_init = 'true' if fs.get('camera_capture_enabled', False) else 'false'
    amb_init = 'true' if fs.get('ambient_camera_enabled', False) else 'false'
    ds_init = 'true' if fs.get('deep_search_enabled', False) else 'false'
    el_init = 'true' if fs.get('elyth_enabled', False) else 'false'
    # Mac 3-6 layer 2: platform-unsupported flags for the JS side, so button
    # re-renders (updateFeatureToggleButtons) re-apply the disabled styling.
    from backend.shared.platform_caps import is_feature_supported
    ce_unsupported = 'false' if is_feature_supported('command_execution') else 'true'

    return f"""
    async () => {{
        // Check if already initialized
        if (window.wsManager) {{
            console.log('[WS] Already initialized');
            return;  // 値を返さない(理由は初期化JS末尾の注記)
        }}

        // Create WebSocket manager (Phase 2C: backoff reconnect + send queue + overlay,
        //                           Phase 3D: session_token + last_seq for resume)
        window.wsManager = {{
            ws: null,
            port: {port},
            serverMode: {'true' if server_mode else 'false'},
            // Phase 2C state
            sendQueue: [],
            reconnectAttempt: 0,
            reconnectStartedAt: null,
            reconnectTimer: null,
            backoffSchedule: [1000, 2000, 4000, 8000, 16000],
            giveUpAfterMs: 5 * 60 * 1000,
            queueMaxSize: 50,
            closeIntentional: false,
            // Set when the server closed us with a no-reconnect code (4001-4004).
            // Blocks the pageshow/visibilitychange auto-reconnect below — an
            // admin-kicked or timed-out page must stay dead until manual reload.
            noReconnect: false,
            // Phase 3D state — kept in JS memory only (not localStorage).
            // Refresh = new session by design (spec §12.2).
            sessionToken: null,
            lastSeq: 0,

            connect: function() {{
                this.closeIntentional = false;
                const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                let url;
                if (this.serverMode) {{
                    // Server mode: use /ws endpoint on same host:port
                    const host = window.location.host;
                    url = protocol + '//' + host + '/ws';
                }} else {{
                    // Local mode: use separate port
                    const host = window.location.hostname;
                    url = protocol + '//' + host + ':' + this.port;
                }}
                console.log('[WS] Connecting to', url);

                try {{
                    this.ws = new WebSocket(url);

                    this.ws.onopen = () => {{
                        console.log('[WS] Connected successfully');
                        this.reconnectAttempt = 0;
                        this.reconnectStartedAt = null;
                        clearTimeout(this.reconnectTimer);
                        this.hideOverlay();
                        // Send identify message (NOT queued — must arrive within 10s server timeout).
                        // Phase 3D: include sessionToken + lastSeq so server can resume on reconnect.
                        // Initial connect → sessionToken=null, lastSeq=null.
                        this.ws.send(JSON.stringify({{
                            type: 'identify',
                            client_type: '{client_type}',
                            session_token: this.sessionToken,
                            last_seq: this.sessionToken ? this.lastSeq : null
                        }}));
                        console.log('[WS] Identify sent: {client_type}, token=' +
                                    (this.sessionToken ? this.sessionToken.substring(0,8) : 'null') +
                                    ', last_seq=' + (this.sessionToken ? this.lastSeq : 'null'));
                        // Drain send queue
                        this.flushQueue();
                        // Re-enable record button if disabled by onclose
                        const recBtn = document.querySelector('#voice-record-btn button');
                        if (recBtn) recBtn.disabled = false;
                        // Live Camera: re-announce provider on (re)connect
                        // (also resets the server-side timeout breaker)
                        if (window.ambientCam && window.ambientCam.active) {{
                            window.ambientCam.announce(true);
                        }}
                        // Status indicator self-heal: an API probe finishing
                        // BEFORE this socket opened published update_status to
                        // nobody — one render now closes that startup/reconnect
                        // race (probes still running publish over this socket).
                        const stBtn = document.querySelector('#ws-status-update-trigger');
                        if (stBtn) {{
                            // Always restore to 'none', never to a captured value: two calls
                            // within the 10ms window would otherwise capture 'block' and leave
                            // the button stuck visible (and self-perpetuating from then on).
                            stBtn.style.display = 'block';
                            stBtn.click();
                            setTimeout(() => {{ stBtn.style.display = 'none'; }}, 10);
                        }}
                    }};
                    
                    this.ws.onmessage = (event) => {{
                        try {{
                            const data = JSON.parse(event.data);

                            // Server liveness probe — ack immediately. Event-driven
                            // (not a timer), so this works even in throttled
                            // background tabs. No pong for LIVENESS_TIMEOUT (45s)
                            // → the server treats the session as disconnected.
                            if (data.action === 'ping') {{
                                if (this.ws && this.ws.readyState === WebSocket.OPEN) {{
                                    try {{
                                        this.ws.send(JSON.stringify({{
                                            action: 'pong', timestamp: Date.now()
                                        }}));
                                    }} catch (e) {{}}
                                }}
                                return;
                            }}
                            console.log('[WS] Received:', data);

                            // Phase 3D: track last seq for replay continuity.
                            // MONOTONIC ONLY — resume delivery interleaves with concurrent
                            // broadcasts, so older seq values may arrive after newer ones.
                            // Without this guard, lastSeq would regress and the next
                            // reconnect's last_seq would request already-delivered messages,
                            // causing DOM duplication.
                            if (typeof data.seq === 'number' && data.seq > this.lastSeq) {{
                                if (this.lastSeq && data.seq > this.lastSeq + 1) {{
                                    console.warn('[WS] Seq gap: expected ' + (this.lastSeq + 1) +
                                                 ', got ' + data.seq);
                                }}
                                this.lastSeq = data.seq;
                            }}

                            // Phase 3D: persist server-issued session_token. New token =
                            // new session (Case 4 grant), so reset lastSeq — the new
                            // session's seq starts at 1 and the monotonic guard above
                            // would otherwise reject every message forever.
                            if (data.type === 'identify_response' && data.session_token) {{
                                if (data.session_token !== this.sessionToken) {{
                                    // Reconnected as a NEW session (old one was
                                    // released, e.g. suspend > grace window):
                                    // messages missed while away never arrive via
                                    // replay — refetch the whole chat display.
                                    // Skipped on first connect (no old token).
                                    if (this.sessionToken) {{
                                        const rbtn = document.querySelector('#ws-update-trigger');
                                        if (rbtn) {{
                                            rbtn.style.display = 'block';
                                            rbtn.click();
                                            setTimeout(() => {{ rbtn.style.display = 'none'; }}, 10);
                                        }}
                                    }}
                                    this.lastSeq = 0;
                                    console.log('[WS] New session_token, lastSeq reset:',
                                                data.session_token.substring(0,8));
                                }} else {{
                                    console.log('[WS] session_token preserved (resume):',
                                                data.session_token.substring(0,8));
                                }}
                                this.sessionToken = data.session_token;
                            }}

                            // Handle different message types
                            if (data.type === 'connected') {{
                                console.log('[WS] Server acknowledged connection');
                                // フールプルーフ: 機能→ブロック理由(null=利用可)。
                                // updateFeatureToggleButtons が毎描画で適用する
                                if (data.feature_availability) {{
                                    window.featureAvailability = data.feature_availability;
                                }}
                                // Restore feature toggle states from welcome message
                                if (data.feature_status) {{
                                    window.pcStatusEnabled = data.feature_status.pc_status_enabled || false;
                                    window.screenCaptureEnabled = data.feature_status.screen_capture_enabled || false;
                                    window.talkThemeEnabled = data.feature_status.talk_theme_enabled !== false;
                                    window.speechlessEnabled = data.feature_status.speechless_enabled || false;
                                    window.commandExecutionEnabled = data.feature_status.command_execution_enabled || false;
                                    window.notesEnabled = data.feature_status.notes_enabled === true;
                                    window.imageGenerationEnabled = data.feature_status.image_generation_enabled || false;
                                    window.cameraCaptureEnabled = data.feature_status.camera_capture_enabled || false;
                                    window.ambientCameraEnabled = data.feature_status.ambient_camera_enabled || false;
                                    window.deepSearchEnabled = data.feature_status.deep_search_enabled || false;
                                    window.elythEnabled = data.feature_status.elyth_enabled || false;
                                    window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
                                    // Camera hardware follows the feature state
                                    if (window.ambientCam) window.ambientCam.syncWithFeature(window.cameraCaptureEnabled);
                                }}
                            }} else if (data.type === 'identify_response') {{
                                console.log('[WS] Identify response:', data.status);
                                if (data.status === 'blocked') {{
                                    window._showSessionBlockedOverlay();
                                    window.wsManager.disconnect();
                                }}
                            }} else if (data.type === 'force_disconnected') {{
                                // Phase 2C: superseded by close code (4001/4002/4003).
                                // Server still sends this JSON for backward compat; treat as no-op.
                                console.log('[WS] (legacy) force_disconnected JSON received, ignored');
                            }} else if (data.action === 'feature_toggle_response') {{
                                // Handle PC Status / Screen Capture / Talk Theme toggle response
                                console.log('[WS] Feature toggle response:', data);
                                // フールプルーフ層2の拒否: 理由ポップアップを表示。
                                // 応答には現在の全トグル状態が同梱され、下の代入が
                                // 楽観フリップ済みのJS状態を正へ巻き戻す
                                if (data.success === false && data.popup_message &&
                                    typeof showNotification === 'function') {{
                                    showNotification(data.popup_title || '', data.popup_message, 'warning', 5000);
                                }}
                                window.pcStatusEnabled = data.pc_status_enabled || false;
                                window.screenCaptureEnabled = data.screen_capture_enabled || false;
                                window.talkThemeEnabled = data.talk_theme_enabled !== false;
                                window.speechlessEnabled = data.speechless_enabled || false;
                                window.commandExecutionEnabled = data.command_execution_enabled || false;
                                window.notesEnabled = data.notes_enabled === true;
                                window.imageGenerationEnabled = data.image_generation_enabled || false;
                                window.cameraCaptureEnabled = data.camera_capture_enabled || false;
                                window.ambientCameraEnabled = data.ambient_camera_enabled || false;
                                window.deepSearchEnabled = data.deep_search_enabled || false;
                                window.elythEnabled = data.elyth_enabled || false;
                                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
                                // Camera hardware follows the feature state (start/stop
                                // getUserMedia + provider announce on this page)
                                if (window.ambientCam) window.ambientCam.syncWithFeature(window.cameraCaptureEnabled);
                            }} else if (data.action === 'feature_availability') {{
                                // キャラ切替/APIキー保存後の可用性再配信(フールプルーフ)。
                                // feature_status同梱=サーバー側の強制OFFを全クライアントへ反映
                                const fa = data.data || {{}};
                                window.featureAvailability = fa.reasons || {{}};
                                if (fa.feature_status) {{
                                    window.pcStatusEnabled = fa.feature_status.pc_status_enabled || false;
                                    window.screenCaptureEnabled = fa.feature_status.screen_capture_enabled || false;
                                    window.talkThemeEnabled = fa.feature_status.talk_theme_enabled !== false;
                                    window.speechlessEnabled = fa.feature_status.speechless_enabled || false;
                                    window.commandExecutionEnabled = fa.feature_status.command_execution_enabled || false;
                                    window.notesEnabled = fa.feature_status.notes_enabled === true;
                                    window.imageGenerationEnabled = fa.feature_status.image_generation_enabled || false;
                                    window.cameraCaptureEnabled = fa.feature_status.camera_capture_enabled || false;
                                    window.ambientCameraEnabled = fa.feature_status.ambient_camera_enabled || false;
                                    window.deepSearchEnabled = fa.feature_status.deep_search_enabled || false;
                                    window.elythEnabled = fa.feature_status.elyth_enabled || false;
                                    if (window.ambientCam) window.ambientCam.syncWithFeature(window.cameraCaptureEnabled);
                                }}
                                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
                            }} else if (data.action === 'update_status') {{
                                console.log('[WS] Triggering status update:', data.reason);
                                
                                // Find and click the hidden status update button
                                const statusBtn = document.querySelector('#ws-status-update-trigger');
                                if (statusBtn) {{
                                    // Make button temporarily visible to click
                                    statusBtn.style.display = 'block';
                                    statusBtn.click();
                                    
                                    // Hide again after short delay
                                    setTimeout(() => {{
                                        statusBtn.style.display = 'none';
                                    }}, 10);
                                    console.log('[WS] Status update triggered');
                                }} else {{
                                    console.warn('[WS] Status update trigger button not found');
                                }}
                            }} else if (data.action === 'update_chat') {{
                                console.log('[WS] Triggering chat update:', data.reason);

                                // Phase 5 Day 1: transcribe finished, return MediaRecorder state to idle.
                                // Covers all paths that surface a successful user-message append.
                                if (data.reason === 'transcription_complete' ||
                                    data.reason === 'hotkey_transcription_complete' ||
                                    data.reason === 'user_message_sent') {{
                                    clearTimeout(window._transcribeJsTimeout);
                                    window._browserMicState = 'idle';
                                }}

                                // Find and click the hidden update button
                                // (scroll is handled by MutationObserver after DOM update)
                                const btn = document.querySelector('#ws-update-trigger');
                                if (btn) {{
                                    // Make button temporarily visible to click
                                    btn.style.display = 'block';
                                    btn.click();
                                    
                                    // Hide again after short delay
                                    setTimeout(() => {{
                                        btn.style.display = 'none';
                                    }}, 10);
                                }} else {{
                                    console.warn('[WS] Update button not found');
                                    // Fallback: try to find voice button for visual feedback
                                    const voiceBtn = document.querySelector('.voice-btn-ready, .voice-btn-recording');
                                    if (voiceBtn) {{
                                        voiceBtn.style.animation = 'pulse 0.5s';
                                        setTimeout(() => {{
                                            voiceBtn.style.animation = '';
                                        }}, 500);
                                    }}
                                }}
                            }} else if (data.action === 'recording_display') {{
                                // 録音UIの同期(hotkey開始/停止・5分自動停止・非同期生成完了)。
                                // Gradio再描画を通さないDOM直接更新なのでちらつかない
                                // (Auto Promptカウントダウンと同方式)。値が同じなら触らない。
                                const s = data.data || {{}};
                                const ind = document.querySelector('#mic-status-display .mic-status-indicator');
                                if (ind && s.state) {{
                                    const cls = 'mic-status-indicator ' + s.state;
                                    if (ind.className !== cls) ind.className = cls;
                                    const txt = ind.querySelector('.status-text');
                                    if (txt && s.text && txt.textContent !== s.text) {{
                                        txt.textContent = s.text;
                                    }}
                                }}
                                if (s.button) {{
                                    const vbtn = document.getElementById('voice-record-btn');
                                    if (vbtn) {{
                                        if (vbtn.textContent.trim() !== s.button) {{
                                            vbtn.textContent = s.button;
                                        }}
                                        vbtn.classList.remove('voice-btn-ready', 'voice-btn-recording', 'voice-btn-processing');
                                        if (s.button_class) vbtn.classList.add(s.button_class);
                                        vbtn.disabled = false;
                                    }}
                                }}
                                // 会話Start/Endトグルは音声ターン中(録音/文字起こし)も
                                // 無効化(稜裁定 2026-08-21)。生成〜TTS再生区間は
                                // is_generating_update が担い、これで録音開始→TTS完了が
                                // 切れ目なく繋がる。enableは'ready'のみ('inactive'は
                                // 現状発生源なし=防御で含める)。サーバ側ガード
                                // (toggle_start_end)が本丸でここは見た目の一次防御。
                                if (s.state) {{
                                    const convToggleWrap = document.querySelector('#conversation-toggle');
                                    if (convToggleWrap) {{
                                        const convToggle = convToggleWrap.tagName === 'BUTTON' ? convToggleWrap : convToggleWrap.querySelector('button');
                                        if (convToggle) {{
                                            if (s.state === 'recording' || s.state === 'processing') {{
                                                convToggle.disabled = true;
                                            }} else if (s.state === 'ready' || s.state === 'inactive') {{
                                                convToggle.disabled = false;
                                            }}
                                        }}
                                    }}
                                }}
                            }} else if (data.action === 'prompt_token_info') {{
                                // プロンプトログのトークン行。Gradio再描画を通さない
                                // DOM直接更新(recording_displayと同方式)なので
                                // ちらつかない。値が同じならDOMに触らない。
                                const tokEl = document.getElementById('prompt-token-info');
                                const tokHtml = (data.data && typeof data.data.html === 'string')
                                    ? data.data.html : null;
                                if (tokEl && tokHtml !== null && tokEl.innerHTML !== tokHtml) {{
                                    tokEl.innerHTML = tokHtml;
                                }}
                            }} else if (data.action === 'trigger_record_click') {{
                                // AG Client Addonのホットキー: 録音ボタンを実クリックし、
                                // 既存の全ガード(JS状態機械のMediaRecorder前処理+
                                // record_speechのM19ロック)をそのまま通す。
                                // start/stop妥当性はサーバー側で判定済み(クリック=トグル)。
                                let vbtn = document.getElementById('voice-record-btn');
                                if (vbtn && vbtn.tagName !== 'BUTTON') {{
                                    vbtn = vbtn.querySelector('button');
                                }}
                                if (vbtn && !vbtn.disabled) {{
                                    vbtn.click();
                                    console.log('[WS] trigger_record_click: record button clicked');
                                }} else {{
                                    console.warn('[WS] trigger_record_click: record button unavailable');
                                }}
                            }} else if (data.action === 'ambient_capture_request') {{
                                // Live Camera: server asks for one frame (this page
                                // announced itself as the provider)
                                if (window.ambientCam) window.ambientCam.capture(data);
                            }} else if (data.action === 'ambient_camera_display') {{
                                // Indicator truth source: server broadcast
                                if (window.ambientCam) window.ambientCam.showDisplay(data.data || {{}});
                            }} else if (data.action === 'stt_model_status') {{
                                // STTステータス行(切替/保存/ダウンロード中/ロード中/完了/失敗)。
                                // 表示先はJS専有の #stt-engine-status-text (gr.HTML内の素のdiv。
                                // Gradioは中身を再描画しない=svelteとのDOM競合なし)。
                                // 値が同じなら触らない(Auto Prompt方式)。
                                const sttData = data.data || {{}};
                                const sttMsg = sttData.message || '';
                                const sttLine = document.getElementById('stt-engine-status-text');
                                if (sttLine && sttMsg && sttLine.textContent !== sttMsg) {{
                                    sttLine.textContent = sttMsg;
                                }}
                                // micインジケータ: モデル準備中は「マイク準備中」・完了で復帰。
                                // 録音中/処理中(recording/processing)は絶対に触らない。
                                const sttMic = document.querySelector('#mic-status-display .mic-status-indicator');
                                if (sttMic && sttData.mic_text &&
                                    (sttMic.classList.contains('inactive') || sttMic.classList.contains('ready'))) {{
                                    const sttMicTxt = sttMic.querySelector('.status-text');
                                    if (sttMicTxt && sttMicTxt.textContent !== sttData.mic_text) {{
                                        sttMicTxt.textContent = sttData.mic_text;
                                    }}
                                }}
                                // 録音ボタン: モデル準備中は無効化(サーバー側の
                                // record_speech ガードと対。trigger_record_click の
                                // !disabled チェックで hotkey/Addon 経由も止まる)
                                if (typeof sttData.record_disabled === 'boolean') {{
                                    let sttVbtn = document.getElementById('voice-record-btn');
                                    if (sttVbtn && sttVbtn.tagName !== 'BUTTON') {{
                                        sttVbtn = sttVbtn.querySelector('button');
                                    }}
                                    if (sttVbtn && sttVbtn.disabled !== sttData.record_disabled) {{
                                        sttVbtn.disabled = sttData.record_disabled;
                                    }}
                                }}
                            }} else if (data.action === 'remote_switch_status') {{
                                // リモート切替リスナーの状態行。表示先はJS専有の
                                // #remote-switch-status-text (gr.HTML内の素のdiv)。
                                // STT行と違い空文字も書く=トグルOFFで表示を消す。
                                const rsMsg = (data.data || {{}}).message || '';
                                const rsLine = document.getElementById('remote-switch-status-text');
                                if (rsLine && rsLine.textContent !== rsMsg) {{
                                    rsLine.textContent = rsMsg;
                                }}
                            }} else if (data.action === 'extraction_started' || data.action === 'extraction_completed') {{
                                // Check if shutdown is in progress - ignore updates during shutdown
                                if (window.wsManager && window.wsManager.shutdownInitiated) {{
                                    console.log('[WS] Ignoring extraction update - shutdown in progress');
                                    return;
                                }}

                                console.log('[WS] Triggering extraction status update:', data.action);
                                const btn = document.querySelector('#extraction-status-trigger');
                                if (btn) {{
                                    btn.style.display = 'block';
                                    btn.click();
                                    setTimeout(() => {{
                                        btn.style.display = 'none';
                                    }}, 10);
                                    console.log('[WS] Extraction status update triggered');
                                }} else {{
                                    console.warn('[WS] Extraction status trigger button not found');
                                }}
                            }} else if (data.action === 'auto_prompt_countdown_start') {{
                                // Auto Prompt カウントダウン開始
                                console.log('[WS] Auto Prompt countdown started, duration:', data.data?.duration);
                                window.autoPromptCountdown = window.autoPromptCountdown || {{}};
                                window.autoPromptCountdown.startTime = Date.now() / 1000;
                                window.autoPromptCountdown.duration = data.data?.duration || 60;

                                // 既存のインターバルをクリア
                                if (window.autoPromptCountdown.intervalId) {{
                                    clearInterval(window.autoPromptCountdown.intervalId);
                                }}

                                // カウントダウン表示を更新するインターバルを開始
                                window.autoPromptCountdown.lastTick = Date.now() / 1000;
                                window.autoPromptCountdown.intervalId = setInterval(() => {{
                                    const display = document.querySelector('#auto-prompt-countdown p');
                                    // スリープ/フリーズ補正: tick間隔の異常な飛び(=スリープ等)分は
                                    // 経過に数えず startTime を前進させる(Python側のawake判定と整合)。
                                    const nowSec = Date.now() / 1000;
                                    const gap = nowSec - (window.autoPromptCountdown.lastTick || nowSec);
                                    if (gap > 3) {{
                                        window.autoPromptCountdown.startTime += (gap - 1);
                                    }}
                                    window.autoPromptCountdown.lastTick = nowSec;
                                    if (display) {{
                                        const elapsed = nowSec - window.autoPromptCountdown.startTime;
                                        const remaining = Math.max(0, window.autoPromptCountdown.duration - elapsed);
                                        if (remaining > 0) {{
                                            display.textContent = {_js(t('js.auto.countdown'))}.replace('{{n}}', Math.floor(remaining));
                                        }} else {{
                                            display.textContent = {_js(t('js.auto.sending'))};
                                        }}
                                    }}
                                }}, 1000);

                            }} else if (data.action === 'auto_prompt_countdown_stop') {{
                                // Auto Prompt カウントダウン停止
                                console.log('[WS] Auto Prompt countdown stopped, enabled:', data.data?.enabled);

                                // インターバルをクリア
                                if (window.autoPromptCountdown && window.autoPromptCountdown.intervalId) {{
                                    clearInterval(window.autoPromptCountdown.intervalId);
                                    window.autoPromptCountdown.intervalId = null;
                                }}

                                // 表示を更新
                                const display = document.querySelector('#auto-prompt-countdown p');
                                if (display) {{
                                    display.textContent = data.data?.enabled
                                        ? {_js(t('js.auto.inactive'))}
                                        : {_js(t('js.auto.disabled'))};
                                }}

                            }} else if (data.action === 'auto_prompt_chat_update') {{
                                // Auto Prompt によるチャット更新
                                console.log('[WS] Auto Prompt chat update, stage:', data.data?.stage);

                                const btnId = data.data?.stage === 'generating'
                                    ? '#auto-prompt-generating-trigger'
                                    : '#auto-prompt-complete-trigger';
                                const btn = document.querySelector(btnId);
                                if (btn) {{
                                    btn.style.display = 'block';
                                    btn.click();
                                    setTimeout(() => {{
                                        btn.style.display = 'none';
                                    }}, 10);
                                    console.log('[WS] Auto Prompt chat update triggered:', btnId);
                                }} else {{
                                    console.warn('[WS] Auto Prompt trigger button not found:', btnId);
                                }}
                            }} else if (data.action === 'tts_audio') {{
                                // TTS Audio playback for browser
                                // 既知の制限(2026-07-18 稜裁定=見送り): ここは <audio>.play() を
                                // WSコールバック=非ジェスチャ文脈で呼ぶため、Safari系では自動再生
                                // ポリシーにブロックされ無音になる。Chrome前提の設計なので現状は
                                // 移行しない。Safari対応が必要になったら mobile_app.py の
                                // Web Audio API方式(decodeAudioData+BufferSource+GainNode)を移植。
                                console.log('[WS] TTS Audio received, duration:', data.data?.duration_ms, 'ms');

                                if (!window.ttsAudioEnabled) {{
                                    try {{
                                        if (!window.ttsAudioContext) {{
                                            window.ttsAudioContext = new (window.AudioContext || window.webkitAudioContext)();
                                        }}
                                        window.ttsAudioContext.resume().then(() => {{
                                            window.ttsAudioEnabled = true;
                                            console.log('[TTS] Audio enabled via fallback');
                                        }}).catch(() => {{}});
                                    }} catch (e) {{}}
                                }}

                                try {{
                                    const audioData = data.data;

                                    // Decode Base64 to binary
                                    const binaryString = atob(audioData.audio_base64);
                                    const bytes = new Uint8Array(binaryString.length);
                                    for (let i = 0; i < binaryString.length; i++) {{
                                        bytes[i] = binaryString.charCodeAt(i);
                                    }}

                                    // Create Blob and URL (AAC in MP4 — see Phase 1B)
                                    const blob = new Blob([bytes], {{ type: 'audio/mp4' }});
                                    const url = URL.createObjectURL(blob);

                                    // Get or create Audio element
                                    let audio = document.getElementById('tts-audio-player');
                                    if (!audio) {{
                                        audio = document.createElement('audio');
                                        audio.id = 'tts-audio-player';
                                        document.body.appendChild(audio);
                                    }}

                                    // Cleanup previous Blob URL
                                    if (audio.dataset.blobUrl) {{
                                        URL.revokeObjectURL(audio.dataset.blobUrl);
                                    }}

                                    // Set audio source and volume
                                    audio.src = url;
                                    audio.dataset.blobUrl = url;
                                    // typeof check (not ||): volume 0 = mute must not fall back to 1.0
                                    audio.volume = (typeof audioData.volume === 'number') ? audioData.volume : 1.0;

                                    // Cleanup on playback end
                                    audio.onended = () => {{
                                        URL.revokeObjectURL(url);
                                        audio.dataset.blobUrl = null;
                                        console.log('[WS] TTS Audio playback completed');
                                        // Phase 2.5: notify server so it can unblock the conversation thread
                                        if (audioData.playback_id && window.wsManager) {{
                                            window.wsManager.enqueueSend(JSON.stringify({{
                                                action: 'tts_playback_completed',
                                                playback_id: audioData.playback_id
                                            }}));
                                        }}
                                    }};

                                    // Play audio
                                    audio.play().catch(error => {{
                                        console.error('[WS] TTS Audio playback failed:', error);
                                        URL.revokeObjectURL(url);
                                    }});

                                    console.log('[WS] TTS Audio playing, volume:', audioData.volume);

                                }} catch (audioError) {{
                                    console.error('[WS] TTS Audio processing error:', audioError);
                                }}
                            }} else if (data.action === 'character_appear_response') {{
                                // Motion PNG Tuber appear response
                                console.log('[WS] Character appear response:', data.success, data.message);
                                if (window.handleAppearResponse) {{
                                    window.handleAppearResponse(data.success, data.message);
                                }}
                            }} else if (data.action === 'character_appear_closed') {{
                                // Motion PNG Tuber closed notification
                                console.log('[WS] Character appear closed:', data.reason || '');
                                if (window.handleAppearClosed) {{
                                    window.handleAppearClosed(data.message || '');
                                }}
                            }} else if (data.action === 'command_pre_response') {{
                                // Replace "Generating response..." spinner with 1st AI text
                                const preData = data.data || {{}};
                                const preText = preData.text || '';
                                const chatDisplay = document.getElementById('chat-display');
                                if (chatDisplay && preText) {{
                                    const spinner = chatDisplay.querySelector('.generating-message');
                                    if (spinner) {{
                                        const escaped = preText.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                        const bubble = spinner.querySelector('.ai-bubble');
                                        if (bubble) {{
                                            bubble.innerHTML = escaped.replace(/\\n/g, '<br>');
                                        }}
                                        spinner.classList.remove('generating-message');
                                    }}
                                    chatDisplay.scrollTop = chatDisplay.scrollHeight;
                                }}

                            }} else if (data.action === 'command_approval_pending') {{
                                // Grey-list command approval request
                                console.log('[WS] Command approval pending:', data.data);
                                const approvalData = data.data || {{}};
                                // esc: command/reason は LLM ツールコール出力=Web検索由来の
                                // プロンプトインジェクションで汚染されうる。未エスケープで
                                // innerHTML に入れると承認UI内でスクリプトが走り、承認ボタンを
                                // 自動クリックしてゲートをバイパスされる。他ブロック同様に必ず esc。
                                const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                const charName = esc(approvalData.character_name || {_js(t('js.approval.character'))});
                                const command = esc(approvalData.command || '');
                                const reason = esc(approvalData.reason || '');

                                // Append inside .chat-container (Gradio-managed content)
                                // so next Gradio re-render naturally removes it.
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                const wsRef = this.ws;
                                if (chatContainer) {{
                                    const existing = chatContainer.querySelector('.command-approval-msg');
                                    if (existing) existing.remove();

                                    const approvalDiv = document.createElement('div');
                                    approvalDiv.className = 'command-approval-msg';
                                    approvalDiv.innerHTML = `
                                        <div class="approval-header">${{{_js(t('js.approval.title'))}}}</div>
                                        <div>${{{_js(t('js.approval.body'))}.replace('{{name}}', charName)}}</div>
                                        <div class="approval-command">${{command}}</div>
                                        ${{reason ? `<div class="approval-reason">${{{_js(t('js.approval.reason_prefix'))}}}${{reason}}</div>` : ''}}
                                        <div class="approval-buttons">
                                            <button class="approval-deny-btn" type="button">${{{_js(t('js.approval.deny'))}}}</button>
                                            <button class="approval-accept-btn" type="button">${{{_js(t('js.approval.accept'))}}}</button>
                                        </div>
                                    `;
                                    chatContainer.appendChild(approvalDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;

                                    // Wire inline buttons via wsManager.enqueueSend (Phase 2C)
                                    const inlineDeny = approvalDiv.querySelector('.approval-deny-btn');
                                    const inlineAccept = approvalDiv.querySelector('.approval-accept-btn');
                                    if (inlineDeny) {{
                                        inlineDeny.addEventListener('click', () => {{
                                            window.wsManager.enqueueSend(JSON.stringify({{
                                                action: 'command_deny',
                                                idempotency_key: crypto.randomUUID()
                                            }}));
                                        }});
                                    }}
                                    if (inlineAccept) {{
                                        inlineAccept.addEventListener('click', () => {{
                                            window.wsManager.enqueueSend(JSON.stringify({{
                                                action: 'command_approve',
                                                idempotency_key: crypto.randomUUID()
                                            }}));
                                        }});
                                    }}
                                }}

                            }} else if (data.action === 'command_approval_resolved') {{
                                // Just remove the approval UI; command block + spinner
                                // will be added by subsequent command_loop_block / command_loop_spinner events
                                console.log('[WS] Command approval resolved:', data.data);
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const approvalMsg = chatContainer.querySelector('.command-approval-msg');
                                    if (approvalMsg) {{
                                        approvalMsg.remove();
                                    }}
                                }}

                            }} else if (data.action === 'command_loop_spinner') {{
                                // Add a "Generating Response..." spinner for next loop iteration
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    // 直前の反復が本文なし(ツールのみ・note等)だと初期スピナーが
                                    // 消費されず残る=二重表示。積む前に必ず古いものを除去
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const iconEl = chatContainer.querySelector('.ai-message .char-icon');
                                    const iconTag = iconEl ? `<img src="${{iconEl.src}}" class="char-icon" />` : '';
                                    const spinnerDiv = document.createElement('div');
                                    spinnerDiv.className = 'ai-message generating-message';
                                    spinnerDiv.innerHTML = `
                                        <div class="ai-icon-row">${{iconTag}}</div>
                                        <div class="message-content">
                                            <div class="ai-bubble">
                                                <div class="loading-spinner"></div> {t('gen.generating')}
                                            </div>
                                        </div>`;
                                    chatContainer.appendChild(spinnerDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'command_loop_block') {{
                                // Add a command execution block (remove orphaned spinner first)
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const cmd = esc(bd.command);
                                    const reason = esc(bd.reason);
                                    const status = bd.status || '';
                                    const result = esc(bd.result);
                                    const statusIcon = {json.dumps(COMMAND_STATUS_ICONS, ensure_ascii=False)}[status] || '❓';
                                    const statusLabel = {json.dumps(COMMAND_STATUS_LABELS, ensure_ascii=False)}[status] || '';
                                    const resultHtml = result ? `<details><summary>${{{_js(t('gen.cmd_result'))}}}</summary><pre>${{result}}</pre></details>` : '';
                                    const cmdDiv = document.createElement('div');
                                    cmdDiv.className = 'command-msg';
                                    cmdDiv.innerHTML = `<div class="cmd-header">${{statusIcon}} ${{cmd}} — ${{statusLabel}}</div>`
                                        + (reason ? `<div class="cmd-reason">${{reason}}</div>` : '')
                                        + resultHtml;
                                    chatContainer.appendChild(cmdDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'talk_theme_block') {{
                                // Add a talk theme change block
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const action = bd.action || '';
                                    const theme = esc(bd.theme || '');
                                    const themeDiv = document.createElement('div');
                                    themeDiv.className = 'talk-theme-msg';
                                    if (action === 'set_talk_theme') {{
                                        themeDiv.innerHTML = `<div class="theme-header">${{{_js(t('gen.talk_theme_set'))}}}</div><div>${{theme}}</div>`;
                                    }} else {{
                                        themeDiv.innerHTML = `<div class="theme-header">${{{_js(t('gen.talk_theme_cleared'))}}}</div>`;
                                    }}
                                    chatContainer.appendChild(themeDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'image_generating_spinner') {{
                                // Show "Generating image..." indicator
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    // Remove any existing image-generating spinner
                                    const old = chatContainer.querySelector('.image-generating-spinner');
                                    if (old) old.remove();
                                    const div = document.createElement('div');
                                    div.className = 'image-generating-spinner';
                                    div.innerHTML = '<div class="loading-spinner"></div> ' + {_js(t('js.spinner.image_generating'))};
                                    chatContainer.appendChild(div);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'image_generation_block') {{
                                // Add an image generation block to chat
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const imgSpinner = chatContainer.querySelector('.image-generating-spinner');
                                    if (imgSpinner) imgSpinner.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const status = bd.status || 'error';
                                    const prompt = esc(bd.prompt || '');
                                    const igDiv = document.createElement('div');
                                    igDiv.className = 'image-gen-msg';
                                    if (status === 'success' && bd.thumbnail_base64) {{
                                        const fullSrc = bd.full_base64
                                            ? 'data:image/png;base64,' + bd.full_base64
                                            : 'data:image/png;base64,' + bd.thumbnail_base64;
                                        const thumbSrc = 'data:image/png;base64,' + bd.thumbnail_base64;
                                        igDiv.innerHTML = `<div class="image-gen-header">${{{_js(t('chat.image_generated'))}}}</div>`
                                            + `<img src="${{thumbSrc}}" data-full-src="${{fullSrc}}" class="lightbox-image" />`
                                            + `<div class="image-gen-prompt">${{prompt}}</div>`;
                                    }} else {{
                                        igDiv.innerHTML = `<div class="image-gen-header">${{{_js(t('chat.image_gen_failed'))}}}</div>`
                                            + `<div class="image-gen-prompt">${{prompt}}</div>`;
                                    }}
                                    chatContainer.appendChild(igDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'camera_capture_block') {{
                                // Add a camera capture block to chat (remove orphaned spinner
                                // first — other *_block handlers do the same)
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const status = bd.status || 'error';
                                    const reason = esc(bd.reason || '');
                                    const camDiv = document.createElement('div');
                                    camDiv.className = 'camera-capture-msg';
                                    let inner = `<div class="camera-header">${{status === 'success' ? {_js(t('chat.camera_capture'))} : {_js(t('chat.camera_failed'))}}}</div>`;
                                    if (reason) inner += `<div class="camera-reason">${{reason}}</div>`;
                                    if (status === 'success' && bd.image_base64) {{
                                        const imgSrc = 'data:image/jpeg;base64,' + bd.image_base64;
                                        inner += `<details><summary>${{{_js(t('chat.captured_image'))}}}</summary><img src="${{imgSrc}}" style="max-width:300px;border-radius:6px;margin-top:4px;" /></details>`;
                                    }}
                                    camDiv.innerHTML = inner;
                                    chatContainer.appendChild(camDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'deep_search_spinner') {{
                                // Show "Searching..." / "Reading page..." indicator
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const old = chatContainer.querySelector('.deep-search-spinner');
                                    if (old) old.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const div = document.createElement('div');
                                    div.className = 'deep-search-spinner';
                                    if (tool === 'search_web') {{
                                        const q = esc(bd.query || '');
                                        div.innerHTML = '<div class="loading-spinner"></div> ' + {_js(t('js.spinner.searching'))} + q;
                                    }} else {{
                                        const u = esc(bd.url || '');
                                        div.innerHTML = '<div class="loading-spinner"></div> ' + {_js(t('js.spinner.reading'))} + u;
                                    }}
                                    chatContainer.appendChild(div);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'deep_search_block') {{
                                // Add a deep search execution block to chat
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const dsSpinner = chatContainer.querySelector('.deep-search-spinner');
                                    if (dsSpinner) dsSpinner.remove();
                                    const bd = data.data || {{}};
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
                                    if (tool === 'search_web') {{
                                        const label = status === 'success' ? {_js(t('gen.web_search'))} : {_js(t('gen.web_search_failed'))};
                                        let inner = `<div class="ds-header">${{statusIcon}} ${{label}}</div>`;
                                        if (query) inner += `<div class="ds-detail">${{query}}</div>`;
                                        if (hitUrls.length > 0) {{
                                            const urlList = hitUrls.map(u => esc(u)).map(u => `<div class="ds-url">${{u}}</div>`).join('');
                                            inner += `<details class="ds-results"><summary>${{{_js(t('gen.results'))}.replace('{{count}}', hitUrls.length)}}</summary>${{urlList}}</details>`;
                                        }}
                                        dsDiv.innerHTML = inner;
                                    }} else {{
                                        const label = status === 'success' ? {_js(t('gen.page_read'))} : {_js(t('gen.page_read_failed'))};
                                        let inner = `<div class="ds-header">${{statusIcon}} ${{label}}</div>`;
                                        if (url) inner += `<div class="ds-detail">${{url}}</div>`;
                                        if (pageTitle) inner += `<div class="ds-detail">${{pageTitle}}</div>`;
                                        dsDiv.innerHTML = inner;
                                    }}
                                    chatContainer.appendChild(dsDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'map_search_spinner') {{
                                // Show Map Search spinner (searching / getting details / directions)
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const old = chatContainer.querySelector('.map-search-spinner');
                                    if (old) old.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const div = document.createElement('div');
                                    div.className = 'map-search-spinner';
                                    if (tool === 'search_places') {{
                                        div.innerHTML = '<div class="loading-spinner"></div> ' + {_js(t('js.spinner.map_places'))} + esc(bd.query || '');
                                    }} else if (tool === 'get_place_details') {{
                                        div.innerHTML = '<div class="loading-spinner"></div> ' + {_js(t('js.spinner.map_reviews'))};
                                    }} else if (tool === 'get_directions') {{
                                        div.innerHTML = '<div class="loading-spinner"></div> ' + {_js(t('js.spinner.map_directions'))};
                                    }}
                                    chatContainer.appendChild(div);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'map_search_block') {{
                                // Add a map search result block to chat
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const msSpinner = chatContainer.querySelector('.map-search-spinner');
                                    if (msSpinner) msSpinner.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const tool = bd.tool || '';
                                    const status = bd.status || 'error';
                                    const msDiv = document.createElement('div');
                                    msDiv.className = 'map-search-msg';
                                    const ok = status === 'success';
                                    const icon = ok ? '🗺' : (status === 'rate_limited' ? '⏳' : '❌');
                                    if (tool === 'search_places') {{
                                        const label = ok ? {_js(t('gen.place_search'))} : {_js(t('gen.place_search_failed'))};
                                        msDiv.innerHTML = `<div class="ds-header">${{icon}} ${{label}}</div>`
                                            + (bd.query ? `<div class="ds-detail">${{esc(bd.query)}}</div>` : '');
                                    }} else if (tool === 'get_place_details') {{
                                        const label = ok ? {_js(t('gen.place_details'))} : {_js(t('gen.place_details_failed'))};
                                        msDiv.innerHTML = `<div class="ds-header">${{icon}} ${{label}}</div>`;
                                    }} else if (tool === 'get_directions') {{
                                        const label = ok ? {_js(t('gen.directions'))} : {_js(t('gen.directions_failed'))};
                                        const modeMap = {{'walking':'🚶','driving':'🚗','transit':'🚃'}};
                                        const modeIcon = modeMap[bd.mode] || '';
                                        msDiv.innerHTML = `<div class="ds-header">${{icon}} ${{label}} ${{modeIcon}}</div>`;
                                    }}
                                    chatContainer.appendChild(msDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'elyth_block') {{
                                // Add an ELYTH tool execution block to chat
                                const chatContainer = document.querySelector('#chat-display .chat-container');
                                if (chatContainer) {{
                                    const orphanSpinner = chatContainer.querySelector('.generating-message');
                                    if (orphanSpinner) orphanSpinner.remove();
                                    const bd = data.data || {{}};
                                    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                                    const toolLabels = {{
                                        'create_post': {_js(t('gen.elyth_post'))},
                                        'create_reply': {_js(t('gen.elyth_reply'))},
                                        'like_post': {_js(t('gen.elyth_like'))},
                                        'follow_aituber': {_js(t('gen.elyth_follow'))},
                                        'get_notifications': {_js(t('gen.elyth_notifications'))},
                                        'get_timeline': {_js(t('gen.elyth_timeline'))},
                                        'get_aituber': {_js(t('gen.elyth_profile'))},
                                        'get_thread': {_js(t('gen.elyth_thread'))},
                                        'get_my_posts': {_js(t('gen.elyth_my_posts'))},
                                        'mark_notifications_read': {_js(t('gen.elyth_mark_read'))},
                                    }};
                                    const icon = bd.status === 'success' ? '📡' : '❌';
                                    let label = toolLabels[bd.tool] || ('ELYTH ' + (bd.tool || ''));
                                    if (bd.status !== 'success') label += ' Failed';
                                    const elDiv = document.createElement('div');
                                    elDiv.className = 'elyth-msg';
                                    let inner = `<div class="ds-header">${{icon}} ${{label}}</div>`;
                                    if (bd.content) inner += `<div class="ds-detail">${{esc(bd.content.substring(0,200))}}</div>`;
                                    elDiv.innerHTML = inner;
                                    chatContainer.appendChild(elDiv);
                                    chatContainer.scrollTop = chatContainer.scrollHeight;
                                }}

                            }} else if (data.action === 'mic_state_reset') {{
                                // Phase 5 Day 1: server tells us the bridge slot was
                                // cleared due to transcribe timeout. Detach all callbacks
                                // from the current MediaRecorder so any late-firing onstop
                                // does not send a stale (or zero-byte) blob, then return
                                // the JS state machine to idle.
                                console.log('[BrowserMic] State reset by server');
                                clearTimeout(window._transcribeJsTimeout);
                                if (window.mediaRecorder) {{
                                    try {{ window.mediaRecorder.onstop = null; }} catch (e) {{}}
                                    try {{ window.mediaRecorder.ondataavailable = null; }} catch (e) {{}}
                                    if (window.mediaRecorder.state === 'recording') {{
                                        try {{ window.mediaRecorder.stop(); }} catch (e) {{}}
                                    }}
                                    window.mediaRecorder = null;
                                }}
                                window.recordedChunks = [];
                                window._browserMicRecording = false;
                                window._browserMicState = 'idle';
                                clearTimeout(window._browserMicTimeout);

                            }} else if (data.action === 'is_generating_update') {{
                                window._isGenerating = data.data.is_generating;
                                // Disable/enable send button
                                const sendBtnWrap = document.querySelector('#send-text-btn');
                                if (sendBtnWrap) {{
                                    const btn = sendBtnWrap.tagName === 'BUTTON' ? sendBtnWrap : sendBtnWrap.querySelector('button');
                                    if (btn) btn.disabled = data.data.is_generating;
                                }}
                                // Phase 5 Day 1: disable record button during AI generation
                                // to prevent the burst-click → silent-blob storm.
                                const recBtnWrap = document.querySelector('#voice-record-btn');
                                if (recBtnWrap) {{
                                    const recBtn = recBtnWrap.tagName === 'BUTTON' ? recBtnWrap : recBtnWrap.querySelector('button');
                                    if (recBtn) recBtn.disabled = !!data.data.is_generating;
                                }}
                                // 会話Start/Endトグルも生成中は無効化(稜裁定 2026-08-21):
                                // 走行中ターンは止められず、生成中のEndは終了済み会話への
                                // コミット/終了後TTSを生む。解除=is_generating False
                                // =TTS再生完了時。サーバ側ガード(toggle_start_end)が本丸で
                                // ここは見た目の一次防御。
                                const endBtnWrap = document.querySelector('#conversation-toggle');
                                if (endBtnWrap) {{
                                    const endBtn = endBtnWrap.tagName === 'BUTTON' ? endBtnWrap : endBtnWrap.querySelector('button');
                                    if (endBtn) endBtn.disabled = !!data.data.is_generating;
                                }}
                                // Update text status (marker approach to avoid Gradio chain conflict)
                                let statusP = document.querySelector('#text-status p');
                                if (!statusP) {{
                                    const container = document.querySelector('#text-status');
                                    if (container) {{
                                        statusP = document.createElement('p');
                                        container.appendChild(statusP);
                                    }}
                                }}
                                if (statusP) {{
                                    if (data.data.is_generating) {{
                                        statusP.textContent = {_js(t('conv.stat_generating'))};
                                        statusP.dataset.wsGenStatus = 'true';
                                    }} else if (statusP.dataset.wsGenStatus === 'true') {{
                                        statusP.textContent = {_js(t('conv.text_ready'))};
                                        delete statusP.dataset.wsGenStatus;
                                    }}
                                }}
                                // Trigger chat refresh so generating spinner appears/disappears
                                const chatBtn = document.querySelector('#ws-update-trigger');
                                if (chatBtn) {{
                                    chatBtn.style.display = 'block';
                                    chatBtn.click();
                                    setTimeout(() => {{ chatBtn.style.display = 'none'; }}, 10);
                                }}
                                console.log('[WS] Generating state:', data.data.is_generating);

                            }} else if (data.action === 'image_slot_update') {{
                                // Image buffer sync (companion or self)
                                window._currentImageCount = data.data.count;
                                window._currentImageMax = data.data.max;
                                let el = document.querySelector('#attach-status p');
                                if (!el) {{
                                    // Gradio Markdown renders no <p> when empty; create one
                                    const container = document.querySelector('#attach-status');
                                    if (container) {{
                                        el = document.createElement('p');
                                        container.appendChild(el);
                                    }}
                                }}
                                if (el) {{ el.textContent = data.data.text || ''; el.style.color = ''; }}

                            }} else if (data.action === 'talk_theme_updated') {{
                                // Phase 4B: snapshot s90. Replaces the 2s polling
                                // timer. textContent (not innerHTML) for safety.
                                const ta = document.querySelector('#current-theme-display textarea');
                                if (ta) {{
                                    ta.value = data.theme || {_js(t('gen.no_talk_theme'))};
                                    ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                }}

                            }} else if (data.action === 'error_notification') {{
                                // Phase 4D: discard s91. ERROR/CRITICAL log
                                // forwarded by WebSocketErrorHandler. 5s display
                                // (vs default 4s) per spec §10.1.4.
                                if (typeof showNotification === 'function') {{
                                    showNotification({_js(t('js.notify.error_title'))}, data.message || '', 'error', 5000);
                                }}

                            }} else if (data.action === 'popup_notification') {{
                                // ui.state.show_*_popup delivery (replaces the
                                // gr.Timer-polled error_display, which flickered
                                // on every 0.5s tick). Title/message arrive
                                // pre-localized from the Python side. 5s display
                                // preserves the old POPUP_DISPLAY_DURATION.
                                if (typeof showNotification === 'function') {{
                                    showNotification(data.title || '', data.message || '', data.level || 'info', 5000);
                                }}

                            }} else if (data.action === 'attach_image_rejected') {{
                                // フールプルーフ: Ollamaキャラ中の画像添付拒否。
                                // 理由はサーバー側でローカライズ済み(正はWS層の
                                // attach_image 拒否=クライアント側プリチェックなし)
                                if (typeof showNotification === 'function') {{
                                    showNotification(data.popup_title || '', data.popup_message || '', 'warning', 5000);
                                }}

                            }} else if (data.action === 'elyth_character_toggled') {{
                                // フールプルーフ: tools非対応モデルのON拒否は理由を通知
                                // (理由はサーバー側でローカライズ済み)。下のrefreshが
                                // 行クリック時の楽観的ボタン反転の巻き戻しも兼ねる
                                if (data.success === false && typeof showNotification === 'function') {{
                                    showNotification({_js(t('elyth.char_settings'))}, data.message || '', 'warning', 5000);
                                }}
                                // Refresh ELYTH character list after toggle
                                console.log('[WS] ELYTH character toggled:', data.character_id, data.enabled);
                                const refreshBtn = document.querySelector('#elyth-refresh-chars-btn button');
                                if (refreshBtn) refreshBtn.click();
                                // Toggling changes who can run — stale
                                // "no character is ON" guidance must not linger
                                const tmsg = document.getElementById('elyth-session-msg');
                                if (tmsg) tmsg.style.display = 'none';
                            }} else if (data.action === 'elyth_status') {{
                                // Real-time ELYTH session status update
                                window._elythUpdateStatus && window._elythUpdateStatus(data.data);
                            }} else if (data.action === 'youtube_status') {{
                                // Real-time YouTube reply-session status update
                                window._youtubeUpdateStatus && window._youtubeUpdateStatus(data.data || {{}});
                            }} else if (data.action === 'youtube_start_response') {{
                                // Manual start result — notify only failures (成功
                                // ポップアップは不要=稜指示 2026-07-12。成功の可視化は
                                // 状態表示+アクティビティログが担う。ELYTHも通知なし)
                                if (!data.success && typeof showNotification === 'function') {{
                                    showNotification({_js(t('youtube.status_heading'))}, data.message || '', 'error', 5000);
                                }}
                                // Re-render so a rejected start re-enables the button
                                // immediately (no status broadcast follows a rejection)
                                if (window._youtubeState) {{
                                    window._youtubeRenderStatus();
                                }} else {{
                                    // No status broadcast yet — restore the button
                                    // directly so it can't get stuck disabled
                                    const sbtn = document.getElementById('youtube-session-btn');
                                    if (sbtn) {{ sbtn.disabled = false; sbtn.style.opacity = '1'; }}
                                }}
                            }} else if (data.action === 'youtube_loop_response') {{
                                // Toggle acknowledged — status broadcast follows and updates the buttons
                                console.log('[WS] YouTube auto-reply enabled:', data.enabled);
                            }} else if (data.action === 'elyth_start_response') {{
                                // Manual start result — show the rejection
                                // reason inline under the session buttons
                                // (message arrives pre-localized from Python)
                                console.log('[WS] ELYTH start response:', data.success);
                                const emsg = document.getElementById('elyth-session-msg');
                                if (!data.success) {{
                                    if (emsg) {{
                                        emsg.textContent = data.message || '';
                                        emsg.style.display = 'block';
                                    }}
                                    // No status broadcast follows a rejection —
                                    // restore the button (YouTube 同型)
                                    if (window._elythState) {{
                                        window._elythRenderStatus();
                                    }} else {{
                                        const sbtn = document.getElementById('elyth-session-btn');
                                        if (sbtn) {{ sbtn.disabled = false; sbtn.style.opacity = '1'; }}
                                    }}
                                }} else if (emsg) {{
                                    emsg.style.display = 'none';
                                }}
                            }} else if (data.action === 'elyth_loop_response') {{
                                // Loop toggled — when turned ON with no runnable
                                // character, warn inline (buttons update via the
                                // loop_resumed/loop_paused status broadcast)
                                const lmsg = document.getElementById('elyth-session-msg');
                                if (lmsg) {{
                                    if (data.warning) {{
                                        lmsg.textContent = data.warning;
                                        lmsg.style.display = 'block';
                                    }} else {{
                                        lmsg.style.display = 'none';
                                    }}
                                }}
                            }} else if (data.action === 'elyth_stop_response') {{
                                // Stop response - status will be updated by next elyth_status broadcast
                                console.log('[WS] ELYTH session response:', data.action, data.success);
                            }}
                        }} catch (e) {{
                            console.error('[WS] Message parse error:', e);
                        }}
                    }};
                    
                    this.ws.onerror = (error) => {{
                        console.error('[WS] Connection error:', error);
                    }};

                    this.ws.onclose = (event) => {{
                        // Stale onclose guard: if this.ws no longer points at the
                        // WS that just closed, we've already replaced/discarded it
                        // (typically via navigator.offline → ws.close() + ws=null,
                        // followed by online → new WS). The late-arriving close
                        // (e.g. server-side stale-replace 4002) must NOT trigger
                        // an error overlay on top of the already-running flow.
                        if (this.ws !== event.target) {{
                            console.log('[WS] Stale onclose ignored (code=' + event.code + ')');
                            return;
                        }}
                        console.log('[WS] Closed code=' + event.code + ' reason=' + event.reason);
                        this.ws = null;
                        if (this.closeIntentional) return;

                        // Stop ongoing recording if any (mic guard)
                        if (window.mediaRecorder && window.mediaRecorder.state === 'recording') {{
                            try {{ window.mediaRecorder.stop(); }} catch (e) {{}}
                        }}
                        const recBtn = document.querySelector('#voice-record-btn button')
                                       || document.querySelector('.voice-btn-recording');
                        if (recBtn) recBtn.disabled = true;

                        // close code dispatch
                        const noReconnectMap = {{
                            1000: null,
                            4001: 'error_4001',
                            4002: 'error_4002',
                            4003: 'error_4003',
                            4004: 'error_4004',
                            4007: 'error_4007'
                        }};
                        if (event.code in noReconnectMap) {{
                            const state = noReconnectMap[event.code];
                            if (state) {{
                                this.noReconnect = true;
                                this.showOverlay(state);
                            }}
                            return;
                        }}
                        // Unexpected close → schedule reconnect
                        this.scheduleReconnect();
                    }};
                }} catch (e) {{
                    console.error('[WS] Failed to create WebSocket:', e);
                }}
            }},

            disconnect: function() {{
                this.closeIntentional = true;
                if (this.ws) {{
                    try {{ this.ws.close(1000); }} catch (e) {{}}
                    this.ws = null;
                }}
                clearTimeout(this.reconnectTimer);
            }},

            // Phase 2C helpers ------------------------------------------------
            enqueueSend: function(payload) {{
                // Only string (JSON) payloads are queueable. Binary is not — see plan §7.1.3.
                if (typeof payload !== 'string') {{
                    console.warn('[WS] Binary payload not queueable, dropping');
                    return false;
                }}
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {{
                    try {{ this.ws.send(payload); return true; }} catch (e) {{ /* fall through to queue */ }}
                }}
                if (this.sendQueue.length >= this.queueMaxSize) {{
                    this.sendQueue.shift();
                    console.warn('[WS] Queue full, dropping oldest message');
                }}
                this.sendQueue.push(payload);
                return false;
            }},

            flushQueue: function() {{
                while (this.sendQueue.length > 0
                       && this.ws && this.ws.readyState === WebSocket.OPEN) {{
                    const payload = this.sendQueue.shift();
                    try {{ this.ws.send(payload); }}
                    catch (e) {{ this.sendQueue.unshift(payload); break; }}
                }}
            }},

            scheduleReconnect: function() {{
                if (this.reconnectStartedAt === null) {{
                    this.reconnectStartedAt = Date.now();
                }}
                const elapsed = Date.now() - this.reconnectStartedAt;
                if (elapsed > this.giveUpAfterMs) {{
                    this.showOverlay('error_giveup');
                    return;
                }}
                const idx = Math.min(this.reconnectAttempt, this.backoffSchedule.length - 1);
                const delay = this.backoffSchedule[idx];
                this.reconnectAttempt++;
                this.showOverlay('reconnecting', {{
                    detail: {_js(t('js.ovl.retry_in'))}.replace('{{sec}}', Math.ceil(delay / 1000)).replace('{{attempt}}', this.reconnectAttempt)
                }});
                this.reconnectTimer = setTimeout(() => this.connect(), delay);
            }},

            showOverlay: function(state, options) {{
                options = options || {{}};
                const overlay = document.getElementById('ws-overlay');
                if (!overlay) return;
                const icon = document.getElementById('ws-overlay-icon');
                const title = document.getElementById('ws-overlay-title');
                const message = document.getElementById('ws-overlay-message');
                const detail = document.getElementById('ws-overlay-detail');
                const action = document.getElementById('ws-overlay-action');
                const states = {{
                    reconnecting: {{
                        iconHtml: '<span class="ws-overlay-icon spinning">⟳</span>',
                        title: {_js(t('js.ovl.reconnecting_title'))},
                        message: {_js(t('js.ovl.reconnecting_msg'))},
                        showAction: false
                    }},
                    error_4001: {{
                        iconHtml: '⚠',
                        title: {_js(t('js.ovl.in_use_title'))},
                        message: {_js(t('js.ovl.in_use_msg'))},
                        showAction: false
                    }},
                    error_4002: {{
                        iconHtml: '🚫',
                        title: {_js(t('js.ovl.kicked_title'))},
                        message: {_js(t('js.ovl.kicked_msg'))},
                        showAction: true
                    }},
                    error_4003: {{
                        iconHtml: '⏱',
                        title: {_js(t('js.ovl.timeout_title'))},
                        message: {_js(t('js.ovl.timeout_msg'))},
                        showAction: true
                    }},
                    error_4004: {{
                        iconHtml: '🔄',
                        title: {_js(t('js.ovl.other_window_title'))},
                        message: {_js(t('js.ovl.other_window_msg'))},
                        showAction: false
                    }},
                    error_4007: {{
                        iconHtml: '🔌',
                        title: {_js(t('js.ovl.self_disconnect_title'))},
                        message: {_js(t('js.ovl.self_disconnect_msg'))},
                        showAction: true
                    }},
                    error_giveup: {{
                        iconHtml: '⚠',
                        title: {_js(t('js.ovl.giveup_title'))},
                        message: {_js(t('js.ovl.giveup_msg'))},
                        showAction: true
                    }}
                }};
                const s = states[state];
                if (!s) return;
                if (icon) {{
                    if (state === 'reconnecting') {{
                        icon.innerHTML = '⟳';
                        icon.classList.add('spinning');
                    }} else {{
                        icon.innerHTML = s.iconHtml;
                        icon.classList.remove('spinning');
                    }}
                }}
                if (title) title.textContent = s.title;
                if (message) message.textContent = s.message;
                if (detail) detail.textContent = options.detail || '';
                if (action) action.style.display = s.showAction ? 'inline-block' : 'none';
                overlay.classList.add('visible');
            }},

            hideOverlay: function() {{
                const overlay = document.getElementById('ws-overlay');
                if (overlay) overlay.classList.remove('visible');
            }}
        }};

        // Phase 2C+: navigator.onLine — instant detection of physical network changes.
        // TCP keepalive can take minutes to detect a dead connection; OS-level
        // network interface state changes (Wi-Fi toggle, airplane mode) are
        // surfaced by the browser within ~1s via these events.
        //
        // Grace window: ignore offline events shorter than OFFLINE_GRACE_MS to
        // avoid flicker on transient mobile network drops (~1s typical).
        window._wsOfflineTimer = null;
        const OFFLINE_GRACE_MS = 3000;

        window.addEventListener('offline', () => {{
            console.log('[WS] navigator: offline detected (grace ' + OFFLINE_GRACE_MS + 'ms)');
            if (window._wsOfflineTimer) clearTimeout(window._wsOfflineTimer);
            window._wsOfflineTimer = setTimeout(() => {{
                window._wsOfflineTimer = null;
                if (navigator.onLine) return;  // recovered during grace, do nothing
                const m = window.wsManager;
                if (!m) return;
                clearTimeout(m.reconnectTimer);
                if (m.ws) {{
                    try {{ m.ws.close(); }} catch(e) {{}}
                    m.ws = null;
                }}
                m.showOverlay('reconnecting', {{ detail: {_js(t('js.ovl.no_network'))} }});
            }}, OFFLINE_GRACE_MS);
        }});

        window.addEventListener('online', () => {{
            console.log('[WS] navigator: online detected');
            if (window._wsOfflineTimer) {{
                clearTimeout(window._wsOfflineTimer);
                window._wsOfflineTimer = null;
            }}
            const m = window.wsManager;
            if (!m) return;
            if (!m.ws || m.ws.readyState !== WebSocket.OPEN) {{
                clearTimeout(m.reconnectTimer);
                m.reconnectAttempt = 0;
                m.reconnectStartedAt = null;
                m.connect();
            }}
        }});

        // Session blocked overlay (another device is connected)
        window._showSessionBlockedOverlay = function() {{
            if (document.getElementById('session-blocked-overlay')) return;
            const overlay = document.createElement('div');
            overlay.id = 'session-blocked-overlay';
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.85);z-index:99999;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#fff;font-family:sans-serif;';
            overlay.innerHTML = '<div style="text-align:center;"><div style="font-size:48px;margin-bottom:20px;">&#128274;</div><div style="font-size:24px;font-weight:bold;margin-bottom:12px;">' + {_js(t('js.ovl.blocked_title'))} + '</div><div style="font-size:16px;color:#aaa;">' + {_js(t('js.ovl.blocked_msg'))} + '</div></div>';
            document.body.appendChild(overlay);
        }};

        // (Phase 2C: _showForceDisconnectedOverlay removed — use wsManager.showOverlay)

        // ---- Common image-attach helpers (Phase 1A) ----
        // Compress an image File (canvas resize to 1024px max, JPEG q=0.7) and send it
        // through the WebSocket as `attach_image`. Used by Desktop attach hook and
        // (via Mobile JS) by Companion file pickers.
        window._compressAndSendImage = function(file) {{
            return new Promise((resolve) => {{
                if (!file) {{ resolve(false); return; }}
                const reader = new FileReader();
                reader.onload = function(e) {{
                    const img = new Image();
                    img.onload = function() {{
                        const maxDim = 1024;
                        let w = img.width, h = img.height;
                        if (w > maxDim || h > maxDim) {{
                            if (w > h) {{ h = Math.round(h * maxDim / w); w = maxDim; }}
                            else {{ w = Math.round(w * maxDim / h); h = maxDim; }}
                        }}
                        const canvas = document.createElement('canvas');
                        canvas.width = w; canvas.height = h;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, w, h);
                        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
                        const b64 = dataUrl.split(',')[1];
                        if (window.wsManager) {{
                            window.wsManager.enqueueSend(JSON.stringify({{
                                action: 'attach_image',
                                image_data: b64,
                                content_type: 'image/jpeg',
                                idempotency_key: crypto.randomUUID()
                            }}));
                            console.log('[Attach] Image sent:', w + 'x' + h, Math.round(b64.length * 3/4 / 1024) + 'KB');
                            resolve(true);
                        }} else {{
                            console.warn('[Attach] WebSocket not connected');
                            resolve(false);
                        }}
                    }};
                    img.onerror = () => resolve(false);
                    img.src = e.target.result;
                }};
                reader.onerror = () => resolve(false);
                reader.readAsDataURL(file);
            }});
        }};

        // Helper: write text to the desktop attach-status element
        window._setAttachStatusText = function(text, color) {{
            const dtCont = document.querySelector('#attach-status');
            if (!dtCont) return;
            let dtEl = dtCont.querySelector('p');
            if (!dtEl) {{ dtEl = document.createElement('p'); dtCont.appendChild(dtEl); }}
            dtEl.textContent = text;
            dtEl.style.color = color || '';
        }};

        window._showAttachWarning = function(msg) {{
            window._setAttachStatusText('⚠️ ' + msg, '#f44336');
            setTimeout(() => {{
                const count = window._currentImageCount || 0;
                const max = window._currentImageMax || 5;
                const restored = count > 0
                    ? String.fromCodePoint(0x1F4CE) + ' ' + count + '/' + max + ' image' + (count !== 1 ? 's' : '') + ' attached'
                    : '';
                window._setAttachStatusText(restored, '');
            }}, 4000);
        }};

        window._showAttachProgress = function(msg) {{
            window._setAttachStatusText(msg, '#888');
        }};

        // Primary attach button: prefilter images → WS, leave docs to Gradio HTTP
        window._setupAttachHook = function(buttonElemId) {{
            const setup = () => {{
                const button = document.getElementById(buttonElemId);
                if (!button) return false;
                // Gradio 5's UploadButton renders <input type="file"> as a SIBLING
                // of the <button> (BaseButton), not as a child. Walk up the ancestor
                // chain until we find the <input> in the same wrapper component.
                let input = button.querySelector('input[type="file"]');
                if (!input) {{
                    let cur = button.parentElement;
                    for (let i = 0; i < 6 && cur && !input; i++) {{
                        input = cur.querySelector('input[type="file"]');
                        cur = cur.parentElement;
                    }}
                }}
                if (!input) return false;
                if (input.dataset.attachHookInstalled === '1') return true;
                input.dataset.attachHookInstalled = '1';

                input.addEventListener('change', async function(e) {{
                    if (!e.target.files || e.target.files.length === 0) return;

                    const IMG_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
                    const allFiles = Array.from(e.target.files);
                    const imageFiles = [];
                    const docFiles = [];

                    for (const f of allFiles) {{
                        const lower = (f.name || '').toLowerCase();
                        const isImage = IMG_EXTS.some(ext => lower.endsWith(ext));
                        (isImage ? imageFiles : docFiles).push(f);
                    }}

                    // Image upper-limit pre-check (matches MAX_IMAGES_PER_MESSAGE on the server)
                    const MAX_IMAGES = window._currentImageMax || 5;
                    const currentCount = window._currentImageCount || 0;
                    const remaining = Math.max(0, MAX_IMAGES - currentCount);
                    const imagesToSend = imageFiles.slice(0, remaining);
                    const droppedCount = imageFiles.length - imagesToSend.length;

                    if (droppedCount > 0) {{
                        window._showAttachWarning(
                            {_js(t('js.attach.too_many'))}.replace('{{max}}', MAX_IMAGES).replace('{{dropped}}', droppedCount)
                        );
                    }}

                    if (imagesToSend.length > 0) {{
                        const wsReady = window.wsManager && window.wsManager.ws &&
                                       window.wsManager.ws.readyState === 1;
                        if (!wsReady) {{
                            window._showAttachWarning(
                                {_js(t('js.attach.ws_disconnected'))}
                            );
                        }} else {{
                            for (let i = 0; i < imagesToSend.length; i++) {{
                                if (imagesToSend.length > 1) {{
                                    window._showAttachProgress(
                                        {_js(t('js.attach.sending'))}.replace('{{i}}', i + 1).replace('{{total}}', imagesToSend.length)
                                    );
                                }}
                                await window._compressAndSendImage(imagesToSend[i]);
                            }}
                        }}
                    }}

                    // Replace input.files so Gradio uploads only documents (best-effort).
                    // If DataTransfer is unsupported, the server-side handler still skips images by extension.
                    if (imageFiles.length > 0) {{
                        try {{
                            const dt = new DataTransfer();
                            for (const f of docFiles) dt.items.add(f);
                            input.files = dt.files;
                        }} catch (err) {{
                            console.warn('[Attach] DataTransfer not supported, server will filter images:', err);
                        }}

                        if (docFiles.length === 0) {{
                            // Suppress Gradio upload entirely (no docs left to upload)
                            e.stopImmediatePropagation();
                            e.preventDefault();
                            setTimeout(() => {{ try {{ input.value = ''; }} catch (e2) {{}} }}, 0);
                        }}
                    }}
                }}, true);  // capture phase: run before Gradio's listener

                console.log('[Attach] Hook installed on', buttonElemId);
                return true;
            }};

            if (!setup()) {{
                let attempts = 0;
                const interval = setInterval(() => {{
                    attempts++;
                    if (setup() || attempts > 50) clearInterval(interval);
                }}, 200);
            }}
        }};

        // Install hook on Desktop attach button (Phase 1A)
        window._setupAttachHook('attach-btn');

        // ---- Live Camera (server-orchestrated client camera) ----
        // This page is the frame provider: it announces camera ON/OFF, answers
        // ambient_capture_request with one downscaled JPEG (ambient_frame),
        // and renders the indicator from ambient_camera_display broadcasts.
        // The frame is prompt-only on the server (never saved to history).
        window.ambientCam = {{
            active: false,       // stream running on this page
            _starting: 0,        // generation of the start() in flight (0 = none)
            _gen: 0,             // bumped by start()/stop()/_restart(): a superseded
                                 // in-flight start discards its result (no leaked stream)
            _restartPending: false,
            _statusHoldUntil: 0, // a transient local message is on screen:
                                 // routine ready/off broadcasts must not wipe it
            _errorSticky: false, // camera unavailable on this page: ready/off broadcasts
                                 // are ignored until a local transition (start ok / stop)
            _displaySeq: 0,      // bumps per status write (auto-clear guard)
            _onEnded: null,      // 'ended' listener on the track in use
            _devChangeTimer: null,
            _camSig: '',         // videoinput set at the last enumerate (devicechange filter)
            deviceId: localStorage.getItem('ambientCamDeviceId') || '',
            stream: null,
            video: null,
            // Transient messages (fallback / attached / timeout) last as long as
            // every other UI notice (稜裁定 2026-08-15: 全UI5秒統一 → ui/status_js.py)
            TRANSIENT_MS: {STATUS_AUTO_HIDE_MS},
            TEXT: {{
                off: {_js(t('ambcam.state_off'))},
                ready: {_js(t('ambcam.state_ready'))},
                capturing: {_js(t('ambcam.state_capturing'))},
                attached: {_js(t('ambcam.state_attached'))},
                timeout: {_js(t('ambcam.state_timeout'))},
                degraded: {_js(t('ambcam.state_degraded'))},
                error: {_js(t('ambcam.state_error'))},
                fallback: {_js(t('ambcam.state_fallback'))},
                fallback_hint: {_js(t('ambcam.state_fallback_hint'))}
            }},

            // Camera hardware follows the Camera feature toggle (server-
            // persisted); called from toggle clicks / feature_toggle_response /
            // initial state.
            syncWithFeature(enabled) {{
                if (enabled) {{
                    if (this.active) return;
                    if (this._starting && this._starting === this._gen) return;  // valid start in flight
                    this.start();   // (a superseded start still winding down queues this one)
                }} else if (this.active || this.stream || this._starting || this._errorSticky) {{
                    // (_starting: an acquisition in flight must be cancelled;
                    //  _errorSticky: OFF while unavailable must still show 'off')
                    this.stop();
                }}
            }},

            async start() {{
                if (this._starting) {{ this._restartPending = true; return; }}  // applied once it settles
                const gen = ++this._gen;
                this._starting = gen;
                try {{
                    let fellBack = false;
                    let stream;
                    try {{
                        const constraints = this.deviceId
                            ? {{ video: {{ deviceId: {{ exact: this.deviceId }} }} }}
                            : {{ video: true }};
                        stream = await navigator.mediaDevices.getUserMedia(constraints);
                    }} catch (e) {{
                        if (!this.deviceId) throw e;
                        // Saved camera is unplugged: fall back to the default
                        // camera instead of dying (稜指摘の実装の穴の対処)
                        console.warn('[AmbCam] Saved camera unavailable (' + e.name + '), falling back to default');
                        stream = await navigator.mediaDevices.getUserMedia({{ video: true }});
                        fellBack = true;
                    }}
                    // Keep a playing <video> as grabFrame fallback source
                    const video = document.createElement('video');
                    video.muted = true;
                    video.playsInline = true;
                    video.srcObject = stream;
                    await video.play();
                    if (gen !== this._gen) {{
                        // stop()/restart happened while acquiring: this stream is not wanted
                        stream.getTracks().forEach(t => {{ try {{ t.stop(); }} catch (e2) {{}} }});
                        return;
                    }}
                    this.stream = stream;
                    this.video = video;
                    this.active = true;
                    this._errorSticky = false;
                    this._watchTrackEnd();
                    if (fellBack) {{
                        // Shown before announce(true): the 'ready' broadcast the
                        // announce triggers must not wipe it (hold inside)
                        this.showTransient(this.TEXT.fallback, 'timeout', this.TEXT.fallback_hint);
                    }} else {{
                        this._statusHoldUntil = 0;   // let the 'ready' broadcast through
                    }}
                    this.announce(true);
                    // The dropdown always shows the camera ACTUALLY in use
                    // (falls back visibly; the saved preference is kept so the
                    // preferred camera returns once reconnected — _afterDeviceChange)
                    const track = this.stream.getVideoTracks()[0];
                    const actualId = (track && track.getSettings)
                        ? (track.getSettings().deviceId || '') : '';
                    await this.populateDevices(actualId);
                    console.log('[AmbCam] started' + (fellBack ? ' (fallback to default camera)' : ''));
                }} catch (e) {{
                    if (gen !== this._gen) return;   // superseded: the newer state owns the display
                    console.error('[AmbCam] start failed:', e);
                    this.stopStream();
                    this.active = false;
                    // No camera to ask: tell the server (no capture waits, tool not
                    // offered). The feature toggle stays ON — a camera that shows
                    // up later resumes via devicechange.
                    this.announce(false);
                    // Sticky: the camera really is unavailable here, so neither the
                    // 'off' broadcast triggered above nor a later 'ready' (another
                    // provider page) may replace it — only start ok / stop do
                    this._errorSticky = true;
                    this.setStatusText(this.TEXT.error, 'error');
                    // Remember the camera set this failure was seen with, so the
                    // devicechange that usually follows an unplug does not retry
                    // the very same set
                    this._camList().then(cams => {{ if (cams) this._camSig = this._sigOf(cams); }});
                }} finally {{
                    this._starting = 0;
                    if (this._restartPending) {{
                        // start()/setDevice()/track loss arrived while acquiring: apply now
                        this._restartPending = false;
                        this.start();
                    }}
                }}
            }},

            // Re-acquire (device change / track loss): invalidates an in-flight
            // start (it discards its stream), then starts anew — or queues the
            // start behind the in-flight one.
            _restart() {{
                this._gen++;
                this.stopStream();
                this.active = false;
                this.start();
            }},

            stop() {{
                this._gen++;                 // an in-flight start discards its result
                this._restartPending = false;
                this.stopStream();
                this.active = false;
                this._statusHoldUntil = 0;
                this._errorSticky = false;
                this.announce(false);
                const sel = document.getElementById('ambient-cam-device');
                if (sel) sel.disabled = true;
                this.setStatusText(this.TEXT.off, 'off');
            }},

            stopStream() {{
                if (this.stream) {{
                    this.stream.getTracks().forEach(t => {{
                        if (this._onEnded) {{
                            try {{ t.removeEventListener('ended', this._onEnded); }} catch (e) {{}}
                        }}
                        try {{ t.stop(); }} catch (e) {{}}
                    }});
                }}
                this._onEnded = null;
                this.stream = null;
                this.video = null;
            }},

            // The camera in use can vanish while ON (USB unplugged, Continuity
            // Camera dropped): watch its track and re-acquire — saved camera →
            // default camera → error when none is left (Mac Studio 等). Own
            // stop() calls do not fire 'ended' (spec) and detach the listener.
            _watchTrackEnd() {{
                const track = this.stream && this.stream.getVideoTracks()[0];
                if (!track) return;
                this._onEnded = () => this._onTrackEnded(track);
                track.addEventListener('ended', this._onEnded);
            }},

            _onTrackEnded(track) {{
                if (!this.stream || this.stream.getVideoTracks().indexOf(track) < 0) return;  // stale
                if (!this.active) return;
                console.warn('[AmbCam] camera track ended (device removed?) — re-acquiring');
                this._restart();
            }},

            // devicechange fires for microphones/speakers too — only act when
            // the set of cameras actually changed (a headphone plug must not
            // trigger a getUserMedia retry / permission prompt). Debounced:
            // a just-plugged camera needs a moment before getUserMedia works.
            _onDeviceChange() {{
                clearTimeout(this._devChangeTimer);
                this._devChangeTimer = setTimeout(() => this._afterDeviceChange(), 1000);
            }},

            async _camList() {{
                try {{
                    const devices = await navigator.mediaDevices.enumerateDevices();
                    return devices.filter(d => d.kind === 'videoinput');
                }} catch (e) {{
                    console.warn('[AmbCam] enumerateDevices failed:', e);
                    return null;
                }}
            }},

            _sigOf(cams) {{
                return cams.map(d => d.deviceId).sort().join('|') + '#' + cams.length;
            }},

            async _afterDeviceChange() {{
                if (!window.cameraCaptureEnabled) return;
                if (this._starting) {{ this._onDeviceChange(); return; }}  // look again once start settles
                const cams = await this._camList();
                if (!cams) return;
                if (this._starting) {{ this._onDeviceChange(); return; }}  // a start began during the await
                const sig = this._sigOf(cams);
                if (sig === this._camSig) return;      // no camera change (audio device etc.)
                this._camSig = sig;
                const track = this.stream && this.stream.getVideoTracks()[0];
                if (!this.active || !track || track.readyState === 'ended') {{
                    // No usable camera right now (lost, or never came up while
                    // ON) and the camera set just changed: try again
                    console.log('[AmbCam] camera set changed — (re)starting');
                    this._restart();
                    return;
                }}
                const actualId = track.getSettings ? (track.getSettings().deviceId || '') : '';
                if (this.deviceId && actualId && actualId !== this.deviceId &&
                    cams.some(d => d.deviceId === this.deviceId)) {{
                    // Saved camera is back while we run on the fallback: switch back
                    console.log('[AmbCam] saved camera reconnected — switching back');
                    this._restart();
                    return;
                }}
                await this.populateDevices(actualId);
            }},

            announce(on) {{
                if (window.wsManager && window.wsManager.ws &&
                    window.wsManager.ws.readyState === WebSocket.OPEN) {{
                    window.wsManager.ws.send(JSON.stringify({{
                        action: 'ambient_camera_status', enabled: !!on
                    }}));
                }}
            }},

            async populateDevices(actualId) {{
                try {{
                    const sel = document.getElementById('ambient-cam-device');
                    if (!sel) {{
                        // Gradio may still be rendering the panel — retry once
                        setTimeout(() => this.populateDevices(actualId), 1000);
                        return;
                    }}
                    const cams = await this._camList();
                    if (!cams) return;
                    this._camSig = this._sigOf(cams);
                    sel.innerHTML = '';
                    cams.forEach((d, i) => {{
                        const opt = document.createElement('option');
                        opt.value = d.deviceId;
                        opt.textContent = d.label || ('Camera ' + (i + 1));
                        sel.appendChild(opt);
                    }});
                    // Selection shows the camera actually in use
                    const want = actualId || this.deviceId;
                    if (want && cams.some(d => d.deviceId === want)) {{
                        sel.value = want;
                    }}
                    sel.disabled = false;
                }} catch (e) {{
                    console.warn('[AmbCam] enumerateDevices failed:', e);
                }}
            }},

            setDevice(deviceId) {{
                this.deviceId = deviceId || '';
                localStorage.setItem('ambientCamDeviceId', this.deviceId);
                if (this.active || this.stream || this._starting) {{
                    // Re-acquire the stream on the newly selected camera
                    this._restart();
                }}
            }},

            async capture(req) {{
                if (!this.active || !this.stream) return;
                this.setStatusText(this.TEXT.capturing, 'capturing');
                try {{
                    // Prefer ImageCapture.grabFrame (works in background tabs
                    // where a throttled <video> can yield black/stale frames);
                    // fall back to the playing <video> element.
                    let source = this.video;
                    let w = this.video ? this.video.videoWidth : 0;
                    let h = this.video ? this.video.videoHeight : 0;
                    const track = this.stream.getVideoTracks()[0];
                    if (typeof ImageCapture !== 'undefined' && track) {{
                        try {{
                            const bmp = await new ImageCapture(track).grabFrame();
                            source = bmp; w = bmp.width; h = bmp.height;
                        }} catch (e) {{ /* fall back to video element */ }}
                    }}
                    if (!source || !w || !h) {{
                        console.warn('[AmbCam] no frame source');
                        return;
                    }}
                    const maxEdge = req.max_edge || 1280;
                    const scale = Math.min(1, maxEdge / Math.max(w, h));
                    const canvas = document.createElement('canvas');
                    canvas.width = Math.round(w * scale);
                    canvas.height = Math.round(h * scale);
                    canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
                    const blob = await new Promise(res =>
                        canvas.toBlob(res, 'image/jpeg', req.jpeg_quality || 0.8));
                    if (!blob) return;
                    const bytes = new Uint8Array(await blob.arrayBuffer());
                    let binary = '';
                    const CHUNK = 0x8000;
                    for (let i = 0; i < bytes.length; i += CHUNK) {{
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
                    }}
                    if (window.wsManager && window.wsManager.ws &&
                        window.wsManager.ws.readyState === WebSocket.OPEN) {{
                        window.wsManager.ws.send(JSON.stringify({{
                            action: 'ambient_frame',
                            request_id: req.request_id,
                            image_data: btoa(binary),
                            content_type: 'image/jpeg'
                        }}));
                        console.log('[AmbCam] frame sent:', bytes.length, 'bytes,',
                                    canvas.width + 'x' + canvas.height);
                    }}
                }} catch (e) {{
                    console.error('[AmbCam] capture failed:', e);
                }}
            }},

            showDisplay(d) {{
                if (d.state === 'attached') {{
                    const text = this.TEXT.attached
                        .replace('{{ms}}', d.elapsed_ms != null ? d.elapsed_ms : '?');
                    this.showTransient(text, 'attached');
                    return;
                }}
                if (d.state === 'timeout') {{
                    if (d.degraded) {{
                        // Sticky: needs a user action (toggle ON / tab refocus) to resume
                        this.setStatusText(this.TEXT.degraded, 'degraded');
                    }} else {{
                        this.showTransient(this.TEXT.timeout, 'timeout');
                    }}
                    return;
                }}
                // Routine ready/off must not wipe a local message off the screen
                // (transient: for its duration / error: until a local transition)
                if ((d.state === 'ready' || d.state === 'off') &&
                    (this._errorSticky || Date.now() < this._statusHoldUntil)) return;
                this.setStatusText(this.TEXT[d.state] || d.state, d.state);
            }},

            // Transient message: shown for TRANSIENT_MS, then back to 'ready'
            // unless something newer was written meanwhile (seq guard). The
            // hold keeps routine broadcasts from cutting it short.
            showTransient(text, cls, hint) {{
                this.setStatusText(text, cls, hint);
                this._statusHoldUntil = Date.now() + this.TRANSIENT_MS;
                const seq = this._displaySeq;
                setTimeout(() => {{
                    if (this._displaySeq === seq && this.active) {{
                        this.setStatusText(this.TEXT.ready, 'ready');
                    }}
                }}, this.TRANSIENT_MS);
            }},

            setStatusText(text, cls, hint) {{
                this._displaySeq = (this._displaySeq || 0) + 1;
                const el = document.getElementById('ambient-cam-status');
                if (!el) return;
                el.textContent = text;
                // Full text on hover: the one-line row ellipsizes long messages
                el.title = hint || text;
                el.className = 'amb-' + cls;
            }}
        }};

        // Device dropdown handler (inline onchange in the utility panel HTML —
        // survives Gradio re-renders, unlike addEventListener wiring)
        window.ambientCamSetDevice = function(deviceId) {{
            if (window.ambientCam) window.ambientCam.setDevice(deviceId);
        }};

        // Camera plug/unplug while ON: recover / switch back (see _afterDeviceChange)
        if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {{
            navigator.mediaDevices.addEventListener('devicechange', () => {{
                if (window.ambientCam) window.ambientCam._onDeviceChange();
            }});
        }}

        // Initial state: Camera feature is server-persisted — bring the
        // camera up on page load when it is ON. Deferred with setTimeout(0):
        // the feature flags (window.cameraCaptureEnabled = ...) are assigned
        // later in this same script, so a direct check here would read
        // undefined and never start.
        setTimeout(() => {{
            if (window.cameraCaptureEnabled && window.ambientCam) {{
                window.ambientCam.syncWithFeature(true);
            }}
        }}, 0);

        // Tab refocus: re-announce so the server-side timeout breaker resets
        // after a background/sleep period (plan §circuit-breaker recovery)
        document.addEventListener('visibilitychange', () => {{
            if (!document.hidden && window.ambientCam && window.ambientCam.active) {{
                window.ambientCam.announce(true);
            }}
        }});

        // Server-mode desktop: show the browser's current mic device on load.
        // Display-only via enumerateDevices (no getUserMedia here — holding a
        // stream from page load would keep the tab's recording indicator on).
        // Labels are empty until mic permission has been granted once — show
        // the pending note then. #browser-mic-name is JS-owned (writers:
        // this init / Start-click / 🔄 refresh, see browser_mic_name_html).
        if (window.wsManager.serverMode && !window.isMobileUI) {{
            let micNameTries = 0;
            const micNameInit = async () => {{
                const el = document.getElementById('browser-mic-name');
                if (!el) {{
                    // Gradio render race: the div may not be in the DOM yet
                    if (++micNameTries < 10) setTimeout(micNameInit, 1000);
                    return;
                }}
                try {{
                    const devs = await navigator.mediaDevices.enumerateDevices();
                    const inputs = devs.filter(d => d.kind === 'audioinput');
                    const def = inputs.find(d => d.deviceId === 'default') || inputs[0];
                    el.textContent = (def && def.label)
                        ? (el.dataset.prefix || '') + def.label
                        : (el.dataset.pending || '');
                }} catch (e) {{
                    el.textContent = el.dataset.pending || '';
                }}
            }};
            micNameInit();
        }}

        // Start connection
        window.wsManager.connect();

        // Clean up on page unload. pagehide is registered too because iOS
        // Safari never fires beforeunload — without it the server only sees a
        // silent death instead of an intentional close(1000).
        window.addEventListener('beforeunload', () => {{
            if (window.wsManager) {{
                window.wsManager.disconnect();
            }}
        }});
        window.addEventListener('pagehide', () => {{
            if (window.wsManager) {{
                window.wsManager.disconnect();
            }}
        }});
        // pagehide also fires on bfcache eviction (back/forward navigation).
        // On restore the WS is dead and closeIntentional is still true — without
        // this reset the page would stay silent forever.
        window.addEventListener('pageshow', () => {{
            const m = window.wsManager;
            if (m && !m.noReconnect && !m.ws) {{
                clearTimeout(m.reconnectTimer);
                m.connect();
            }}
        }});
        // Screen-on / tab refocus: reconnect immediately instead of waiting out
        // the backoff timer (suspend-resume UX; liveness monitor may have
        // released the session server-side — reclaim/resume transparently).
        document.addEventListener('visibilitychange', () => {{
            const m = window.wsManager;
            if (!document.hidden && m && !m.noReconnect && !m.ws) {{
                clearTimeout(m.reconnectTimer);
                m.connect();
            }}
        }});

        // TTS Audio initialization
        window.ttsAudioEnabled = false;
        window.ttsAudioContext = null;

        // Motion PNG Tuber Appear/Disappear function
        window.motionPngTuberActive = false;
        window.appearCharacter = function() {{
            try {{
                const btn = document.getElementById('appear-btn');
                const statusEl = document.getElementById('appear-status');

                if (window.motionPngTuberActive) {{
                    // --- Disappear action ---
                    console.log('[Appear] Character disappear triggered');
                    if (window.wsManager && window.wsManager.ws &&
                        window.wsManager.ws.readyState === WebSocket.OPEN) {{
                        window.wsManager.enqueueSend(JSON.stringify({{
                            action: 'character_disappear',
                            timestamp: Date.now()
                        }}));
                    }}
                    // Reset immediately (don't wait for server response)
                    window.motionPngTuberActive = false;
                    if (btn) {{
                        btn.textContent = {_js(t('utility.appear'))};
                        btn.style.backgroundColor = '#9c27b0';
                    }}
                    if (statusEl) {{
                        statusEl.style.display = 'none';
                    }}
                    // Disappear後はAppearモードに戻る=可用性グレーを再評価
                    if (window.updateFeatureToggleButtons) window.updateFeatureToggleButtons();
                    return;
                }}

                // --- Appear action ---
                console.log('[Appear] Character appear triggered');
                if (window.wsManager) {{
                    const message = {{
                        action: 'character_appear',
                        timestamp: Date.now(),
                        idempotency_key: crypto.randomUUID()
                    }};
                    window.wsManager.enqueueSend(JSON.stringify(message));

                    // Update UI
                    if (btn) {{
                        btn.textContent = {_js(t('utility.launching'))};
                        btn.style.backgroundColor = '#ff9800';
                    }}
                    if (statusEl) {{
                        statusEl.textContent = {_js(t('utility.launching_status'))};
                        statusEl.style.color = '#2196f3';
                        statusEl.style.display = 'inline';
                    }}

                    console.log('[Appear] Request sent via WebSocket');
                }} else {{
                    console.error('[Appear] WebSocket not connected');
                    if (statusEl) {{
                        statusEl.textContent = {_js(t('utility.ws_not_connected'))};
                        statusEl.style.color = '#f44336';
                        statusEl.style.display = 'inline';
                    }}
                }}
            }} catch (e) {{
                console.error('[Appear] Error:', e);
            }}
        }};

        // Handle appear response from server
        window.handleAppearResponse = function(success, message) {{
            const btn = document.getElementById('appear-btn');
            const statusEl = document.getElementById('appear-status');

            if (success) {{
                window.motionPngTuberActive = true;
                if (btn) {{
                    btn.textContent = {_js(t('utility.disappear'))};
                    btn.style.backgroundColor = '#e53935';
                }}
                if (statusEl) {{
                    statusEl.style.display = 'none';
                }}
            }} else {{
                if (btn) {{
                    btn.textContent = {_js(t('utility.appear'))};
                    btn.style.backgroundColor = '#9c27b0';
                }}
                if (statusEl) {{
                    statusEl.textContent = message || 'Failed to launch';
                    statusEl.style.color = '#f44336';
                    statusEl.style.display = 'inline';
                    setTimeout(() => {{ statusEl.style.display = 'none'; }}, 3000);
                }}
            }}
            // 表示状態が変わった=Appearボタンの可用性グレーを再評価
            if (window.updateFeatureToggleButtons) window.updateFeatureToggleButtons();
        }};

        // Handle appear closed event (Electron closed or disconnected)
        window.handleAppearClosed = function(message) {{
            window.motionPngTuberActive = false;
            const btn = document.getElementById('appear-btn');
            const statusEl = document.getElementById('appear-status');
            if (btn) {{
                btn.textContent = {_js(t('utility.appear'))};
                btn.style.backgroundColor = '#9c27b0';
            }}
            if (statusEl) {{
                if (message) {{
                    statusEl.textContent = message;
                    statusEl.style.color = '#ff9800';
                    statusEl.style.display = 'inline';
                    setTimeout(() => {{ statusEl.style.display = 'none'; }}, 3000);
                }} else {{
                    statusEl.style.display = 'none';
                }}
            }}
            // Player閉鎖=Appearモードに戻る=可用性グレーを再評価
            if (window.updateFeatureToggleButtons) window.updateFeatureToggleButtons();
        }};

        // PC Status & Screen Capture & Talk Theme toggle state
        window.pcStatusEnabled = {pc_init};
        window.screenCaptureEnabled = {sc_init};
        window.talkThemeEnabled = {tt_init};
        window.speechlessEnabled = {sl_init};
        window.commandExecutionEnabled = {ce_init};
        window.notesEnabled = {nt_init};
        window.imageGenerationEnabled = {ig_init};
        window.cameraCaptureEnabled = {cc_init};
        window.ambientCameraEnabled = {amb_init};
        window.deepSearchEnabled = {ds_init};
        window.elythEnabled = {el_init};
        // Mac 3-6 layer 2: features unavailable on this platform
        window.platformUnsupported = {{ command_execution: {ce_unsupported} }};
        // サーバーモードで使えないホストPC側機能(PC Status / Screen Capture /
        // Command Execution)。platformUnsupported と同方式で毎描画再適用する
        window.serverModeBlocked = {'true' if server_mode else 'false'};

        window.togglePcStatus = function() {{
            if (window.serverModeBlocked) {{
                console.log('[Feature] PC Status is not available in server mode');
                return;
            }}
            const newState = !window.pcStatusEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_pc_status', enabled: newState
                }}));
                console.log('[Feature] PC Status toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        window.toggleScreenCapture = function() {{
            if (window.serverModeBlocked) {{
                console.log('[Feature] Screen Capture is not available in server mode');
                return;
            }}
            if (!window.pcStatusEnabled) return;
            const newState = !window.screenCaptureEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_screen_capture', enabled: newState
                }}));
                console.log('[Feature] Screen Capture toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        window.toggleTalkTheme = function() {{
            const newState = !window.talkThemeEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_talk_theme', enabled: newState
                }}));
                console.log('[Feature] Talk Theme toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        window.toggleSpeechless = function() {{
            const newState = !window.speechlessEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_speechless', enabled: newState
                }}));
                console.log('[Feature] Speechless toggle requested:', newState);
            }} else {{
                // Gradio fallback
                const tb = document.querySelector('#speechless-toggle-value textarea');
                if (tb) {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    nativeSetter.call(tb, newState ? 'true' : 'false');
                    tb.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                const btn = document.querySelector('#speechless-toggle-trigger button');
                if (btn) btn.click();
                // Optimistic UI update
                window.speechlessEnabled = newState;
                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            }}
        }};

        window.toggleCommandExecution = function() {{
            if (window.platformUnsupported && window.platformUnsupported.command_execution) {{
                console.log('[Feature] Command Execution is not supported on this platform');
                return;
            }}
            if (window.serverModeBlocked) {{
                console.log('[Feature] Command Execution is not available in server mode');
                return;
            }}
            const newState = !window.commandExecutionEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_command_execution', enabled: newState
                }}));
                console.log('[Feature] Command Execution toggle requested:', newState);
            }} else {{
                // Gradio fallback
                const tb = document.querySelector('#command-execution-toggle-value textarea');
                if (tb) {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    nativeSetter.call(tb, newState ? 'true' : 'false');
                    tb.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                const btn = document.querySelector('#command-execution-toggle-trigger button');
                if (btn) btn.click();
                window.commandExecutionEnabled = newState;
                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            }}
        }};

        window.toggleNotes = function() {{
            const newState = !window.notesEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_notes', enabled: newState
                }}));
                console.log('[Feature] Notes toggle requested:', newState);
            }} else {{
                // Gradio fallback
                const tb = document.querySelector('#notes-toggle-value textarea');
                if (tb) {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    nativeSetter.call(tb, newState ? 'true' : 'false');
                    tb.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                const btn = document.querySelector('#notes-toggle-trigger button');
                if (btn) btn.click();
                window.notesEnabled = newState;
                window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            }}
        }};

        window.toggleImageGeneration = function() {{
            const newState = !window.imageGenerationEnabled;
            window.imageGenerationEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_image_generation', enabled: newState
                }}));
                console.log('[Feature] Image Generation toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        window.toggleCameraCapture = function() {{
            const newState = !window.cameraCaptureEnabled;
            window.cameraCaptureEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_camera_capture', enabled: newState
                }}));
                console.log('[Feature] Camera Capture toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
            // This page hosts the camera hardware: follow the toggle
            // immediately (the getUserMedia permission prompt needs the
            // user-gesture context of this click).
            if (window.ambientCam) window.ambientCam.syncWithFeature(newState);
        }};

        window.toggleAmbientCamera = function() {{
            if (!window.cameraCaptureEnabled) return;
            const newState = !window.ambientCameraEnabled;
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_ambient_camera', enabled: newState
                }}));
                console.log('[Feature] Live Camera toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        window.toggleDeepSearch = function() {{
            const newState = !window.deepSearchEnabled;
            window.deepSearchEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_deep_search', enabled: newState
                }}));
                console.log('[Feature] Deep Search toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        window.toggleElyth = function() {{
            const newState = !window.elythEnabled;
            window.elythEnabled = newState;
            window.updateFeatureToggleButtons && window.updateFeatureToggleButtons();
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'set_elyth', enabled: newState
                }}));
                console.log('[Feature] ELYTH toggle requested:', newState);
            }} else {{
                console.error('[Feature] WebSocket not connected');
            }}
        }};

        // ELYTH Control tab: toggle character enable/disable via WebSocket
        // ====== ELYTH Real-time Status ======
        window._elythState = null;
        window._elythTimerInterval = null;
        window._elythActivityEntries = [];

        window._elythFormatTime = function(seconds) {{
            seconds = Math.max(0, Math.floor(seconds));
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = seconds % 60;
            if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
            return m + ':' + String(s).padStart(2, '0');
        }};

        window._elythUpdateStatus = function(status) {{
            window._elythState = status;
            // Calculate server-client time offset for accurate timer display
            if (status.timestamp) {{
                window._elythTimeOffset = (status.timestamp - Date.now() / 1000);
            }}
            const event = status.event || 'status';

            // Add activity log entries for session events
            if (event === 'turn_text' && status.last_activity_text) {{
                const name = status.current_character_name || '?';
                window._elythActivityEntries.push(name + ': ' + status.last_activity_text);
                if (window._elythActivityEntries.length > 20) window._elythActivityEntries.shift();
            }}
            if (event === 'tool_call' && status.last_tool_name) {{
                window._elythActivityEntries.push('  -> ' + status.last_tool_name + '()');
                if (window._elythActivityEntries.length > 20) window._elythActivityEntries.shift();
            }}
            if (event === 'session_start') {{
                window._elythActivityEntries = [];
                const name = status.current_character_name || '?';
                window._elythActivityEntries.push('--- Session: ' + name + ' ---');
            }}
            if (event === 'session_end') {{
                window._elythActivityEntries.push('--- Session ended ---');
            }}

            window._elythRenderStatus();

            // Start timer if not running
            if (!window._elythTimerInterval) {{
                window._elythTimerInterval = setInterval(window._elythRenderTimer, 1000);
            }}
        }};

        window._elythRenderStatus = function() {{
            const s = window._elythState;
            if (!s) return;
            const state = s.state;
            const stateMap = {{
                'running':  [{_js(t('js.elyth.running'))}, '#4caf50'],
                'paused':   [{_js(t('js.elyth.paused'))}, '#f44336'],
                'blocked':  [{_js(t('js.elyth.blocked'))}, '#ff9800'],
                'idle':     [{_js(t('js.elyth.idle'))}, '#888'],
            }};
            const [label, color] = stateMap[state] || [{_js(t('js.elyth.unknown'))}, '#666'];

            const dot = document.getElementById('elyth-status-dot');
            const labelEl = document.getElementById('elyth-status-label');
            const container = document.getElementById('elyth-status-container');
            const detailEl = document.getElementById('elyth-status-detail');
            const logEl = document.getElementById('elyth-activity-log');

            if (dot) dot.style.background = color;
            if (labelEl) {{ labelEl.textContent = label; labelEl.style.color = color; }}
            if (container) container.style.borderLeftColor = color;

            // Detail text
            let detail = '';
            const charCount = (s.character_order || []).length;
            if (charCount > 0) detail = {_js(t('js.elyth.active_chars'))}.replace('{{n}}', charCount);

            if (state === 'running' && s.current_character_name) {{
                detail = s.current_character_name + ' (Turn ' + (s.current_turn || 0) + '/' + (s.max_turns || 10) + ')';
                if (charCount > 1) detail += ' / ' + {_js(t('js.elyth.active_chars'))}.replace('{{n}}', charCount);
            }} else if (state === 'paused') {{
                detail = {_js(t('js.elyth.loop_stopped'))} + (charCount > 0 ? ' / ' + detail : '');
            }} else if (state === 'blocked') {{
                detail = {_js(t('js.elyth.waiting_conversation'))} + ' / ' + detail;
            }}
            if (detailEl) detailEl.innerHTML = detail;

            // Activity log
            if (logEl) {{
                if (state === 'running' && window._elythActivityEntries.length > 0) {{
                    logEl.style.display = 'block';
                    logEl.innerHTML = window._elythActivityEntries.map(e => {{
                        const escaped = e.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                        return '<div>' + escaped + '</div>';
                    }}).join('');
                    logEl.scrollTop = logEl.scrollHeight;
                }} else if (state !== 'running') {{
                    logEl.style.display = 'none';
                }}
            }}

            // Update buttons
            window._elythUpdateBtns(state, s.loop_paused);

            // Update timer immediately
            window._elythRenderTimer();
        }};

        window._elythRenderTimer = function() {{
            const s = window._elythState;
            if (!s) return;
            const timerEl = document.getElementById('elyth-status-timer');
            if (!timerEl) return;

            // Use server-aligned time for accurate calculations
            const offset = window._elythTimeOffset || 0;
            const now = Date.now() / 1000 + offset;
            const state = s.state;

            if (state === 'running' && s.session_start_time) {{
                const elapsed = now - s.session_start_time;
                timerEl.textContent = window._elythFormatTime(elapsed);
            }} else if (state === 'idle') {{
                // J9 timer (YE): countdown from the server-reported remaining
                // seconds (snapshot taken at s.timestamp; the timer only
                // advances in the idle state, so extrapolation is safe here)
                if (typeof s.timer_remaining === 'number') {{
                    const sinceUpdate = s.timestamp ? Math.max(0, now - s.timestamp) : 0;
                    const remaining = s.timer_remaining - sinceUpdate;
                    if (remaining > 0) {{
                        timerEl.textContent = {_js(t('js.elyth.until_next'))}.replace('{{time}}', window._elythFormatTime(remaining));
                    }} else {{
                        timerEl.textContent = {_js(t('js.elyth.waiting_start'))};
                    }}
                }} else {{
                    timerEl.textContent = '';
                }}
            }} else {{
                timerEl.textContent = '';
            }}
        }};

        window._elythUpdateBtns = function(state, loopPaused) {{
            // Auto Loop button
            const loopBtn = document.getElementById('elyth-loop-btn');
            if (loopBtn) {{
                if (loopPaused) {{
                    loopBtn.textContent = {_js(t('elyth.auto_loop_off'))};
                    loopBtn.style.background = '#f44336';
                }} else {{
                    loopBtn.textContent = {_js(t('elyth.auto_loop_on'))};
                    loopBtn.style.background = '#4caf50';
                }}
                loopBtn.disabled = false;
                loopBtn.style.opacity = '1';
            }}
            // Manual Start Session button — auto-loop OFF (paused) does NOT
            // grey it out: a manual one-shot cycle is allowed while paused
            // (稜指示 2026-07-12・YouTube返信の手動一回分と同一仕様)
            const sessionBtn = document.getElementById('elyth-session-btn');
            if (sessionBtn) {{
                if (state === 'running') {{
                    sessionBtn.textContent = {_js(t('elyth.stop_session'))};
                    sessionBtn.style.background = '#f44336';
                    sessionBtn.disabled = false;
                    sessionBtn.style.opacity = '1';
                }} else {{
                    sessionBtn.textContent = {_js(t('elyth.start_session'))};
                    sessionBtn.style.background = '#2196f3';
                    sessionBtn.disabled = false;
                    sessionBtn.style.opacity = '1';
                    sessionBtn.style.cursor = 'pointer';
                }}
            }}
        }};

        window._elythLoopBtnClick = function() {{
            const s = window._elythState;
            const isPaused = s && s.loop_paused;
            const action = isPaused ? 'elyth_resume_loop' : 'elyth_pause_loop';
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{ action: action }}));
                const btn = document.getElementById('elyth-loop-btn');
                if (btn) {{ btn.disabled = true; btn.style.opacity = '0.5'; }}
            }}
        }};

        window._elythSessionBtnClick = function() {{
            const s = window._elythState;
            const isRunning = s && s.state === 'running';
            const action = isRunning ? 'elyth_stop_session' : 'elyth_start_session';
            if (window.wsManager) {{
                const payload = {{ action: action }};
                if (action === 'elyth_start_session') {{
                    payload.idempotency_key = crypto.randomUUID();
                }}
                window.wsManager.enqueueSend(JSON.stringify(payload));
                const btn = document.getElementById('elyth-session-btn');
                if (btn) {{ btn.disabled = true; btn.style.opacity = '0.5'; }}
            }}
        }};
        // ====== End ELYTH Real-time Status ======

        // ====== YouTube Reply Real-time Status ======
        window._youtubeState = null;
        window._youtubeTimeOffset = 0;
        window._youtubeTimerInterval = null;
        window._youtubeActivityEntries = [];

        window._youtubeUpdateStatus = function(status) {{
            window._youtubeState = status;
            if (status.timestamp) {{
                window._youtubeTimeOffset = (status.timestamp - Date.now() / 1000);
            }}
            const event = status.event || 'status';

            // ELYTH同型のアクティビティログ（コメント→返信/判定を積む）
            if (event === 'session_start') {{
                window._youtubeActivityEntries = [{_js(t('js.youtube.log_session_start'))}];
            }}
            // 「停止中」表示になったら常にログを消す — 稜指示 2026-07-12×2回目
            // (トグル操作時に限らず、OFFのまま手動実行→終了で停止中へ戻る
            //  ケースも含む。生成結果は logs/youtube_session_log.json に恒久保存)
            if (status.state === 'paused') {{
                window._youtubeActivityEntries = [];
            }}
            if (event === 'comment_processed' && status.last_comment) {{
                const c = status.last_comment;
                window._youtubeActivityEntries.push((c.author || '?') + ': ' + (c.text || ''));
                let line;
                if (c.status === 'posted') {{
                    line = '  → ' + (c.reply || '') + ' ' + {_js(t('js.youtube.log_posted'))};
                }} else if (c.status === 'skipped') {{
                    line = (c.skip_reason === 'denied')
                        ? '  → ' + {_js(t('js.youtube.log_denied'))}
                        : '  → ' + {_js(t('js.youtube.log_skipped'))}.replace('{{reason}}', c.skip_reason || '?');
                }} else {{
                    line = '  → ' + (c.reply || '');
                }}
                window._youtubeActivityEntries.push(line);
                if (window._youtubeActivityEntries.length > 40) {{
                    window._youtubeActivityEntries.splice(0, window._youtubeActivityEntries.length - 40);
                }}
            }}
            if (event === 'session_end') {{
                window._youtubeActivityEntries.push({_js(t('js.youtube.log_session_end'))});
            }}

            window._youtubeRenderStatus();
            if (!window._youtubeTimerInterval) {{
                window._youtubeTimerInterval = setInterval(window._youtubeRenderTimer, 1000);
            }}
        }};

        window._youtubeRenderStatus = function() {{
            const s = window._youtubeState;
            if (!s) return;
            const state = s.state;
            const stateMap = {{
                'running':    [{_js(t('js.youtube.running'))}, '#4caf50'],
                'posting':    [{_js(t('js.youtube.posting'))}, '#8bc34a'],
                'waiting':    [{_js(t('js.youtube.waiting'))}, '#ff9800'],
                'paused':     [{_js(t('js.youtube.paused'))}, '#f44336'],
                'auth_error': [{_js(t('js.youtube.auth_error'))}, '#e91e63'],
                'idle':       [{_js(t('js.youtube.idle'))}, '#888'],
            }};
            const [label, color] = stateMap[state] || [{_js(t('js.youtube.unknown'))}, '#666'];

            const dot = document.getElementById('youtube-status-dot');
            const labelEl = document.getElementById('youtube-status-label');
            const container = document.getElementById('youtube-status-container');
            const detailEl = document.getElementById('youtube-status-detail');

            if (dot) dot.style.background = color;
            if (labelEl) {{ labelEl.textContent = label; labelEl.style.color = color; }}
            if (container) container.style.borderLeftColor = color;

            const parts = [];
            // 実行中は生成の進捗 n/m を先頭に
            if (state === 'running' && s.session_total > 0) {{
                parts.push({_js(t('js.youtube.progress'))}
                    .replace('{{done}}', s.session_done || 0)
                    .replace('{{total}}', s.session_total));
            }}
            // waiting: タイマー満了なのに開始しない理由を無言にしない (spec §7)
            if (state === 'waiting' && s.waiting_reason) {{
                const reasonMap = {{
                    'conversation':    {_js(t('js.youtube.waiting.conversation'))},
                    'elyth_session':   {_js(t('js.youtube.waiting.elyth_session'))},
                    'worker_running':  {_js(t('js.youtube.waiting.worker_running'))},
                    'session_running': {_js(t('js.youtube.waiting.session_running'))},
                    'daily_limit':     {_js(t('js.youtube.waiting.daily_limit'))},
                }};
                parts.push(reasonMap[s.waiting_reason] || s.waiting_reason);
            }}
            if (state === 'auth_error') {{
                parts.push({_js(t('js.youtube.reauth_required'))});
            }}
            const queueTotal = (s.queue_generated || 0) + (s.queue_posting || 0);
            if (queueTotal > 0) {{
                parts.push({_js(t('js.youtube.queue'))}.replace('{{n}}', queueTotal));
            }}
            parts.push({_js(t('js.youtube.daily'))}
                .replace('{{n}}', s.daily_count || 0)
                .replace('{{limit}}', s.daily_post_limit || 0));
            if (s.authorized_channel && s.authorized_channel.channel_title) {{
                parts.push('@' + s.authorized_channel.channel_title);
            }}
            if (s.dry_run) parts.push({_js(t('js.youtube.dry_run'))});
            // 直前のセッション結果（数秒で終わるdry-run等の成果が見えるように）
            if (s.last_session) {{
                const lastMap = {{
                    'completed':           {_js(t('js.youtube.last.completed'))},
                    'dry_run_completed':   {_js(t('js.youtube.last.dry_run_completed'))},
                    'no_new_comments':     {_js(t('js.youtube.last.no_new_comments'))},
                    'initial_baseline':    {_js(t('js.youtube.last.initial_baseline'))},
                    'drain_queue':         {_js(t('js.youtube.last.drain_queue'))},
                    'daily_limit':         {_js(t('js.youtube.last.daily_limit'))},
                    'interrupted':         {_js(t('js.youtube.last.interrupted'))},
                    'auth_error':          {_js(t('js.youtube.last.auth_error'))},
                }};
                parts.push({_js(t('js.youtube.last_prefix'))}.replace('{{result}}', lastMap[s.last_session] || s.last_session));
            }}
            if (detailEl) detailEl.textContent = parts.join(' / ');

            // Activity log (残す: 数秒で終わるセッションの結果を後から読めるように)
            const logEl = document.getElementById('youtube-activity-log');
            if (logEl) {{
                if (window._youtubeActivityEntries.length > 0) {{
                    logEl.style.display = 'block';
                    logEl.innerHTML = window._youtubeActivityEntries.map(e => {{
                        const escaped = e.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                        return '<div>' + escaped + '</div>';
                    }}).join('');
                    logEl.scrollTop = logEl.scrollHeight;
                }} else {{
                    logEl.style.display = 'none';
                }}
            }}

            window._youtubeUpdateBtns(state, !!s.enabled);
            window._youtubeRenderTimer();
        }};

        window._youtubeUpdateBtns = function(state, enabled) {{
            // Master toggle button (ELYTH auto-loop 同型)
            const loopBtn = document.getElementById('youtube-loop-btn');
            if (loopBtn) {{
                if (enabled) {{
                    loopBtn.textContent = {_js(t('youtube.auto_loop_on'))};
                    loopBtn.style.background = '#4caf50';
                }} else {{
                    loopBtn.textContent = {_js(t('youtube.auto_loop_off'))};
                    loopBtn.style.background = '#f44336';
                }}
                loopBtn.disabled = false;
                loopBtn.style.opacity = '1';
            }}
            // Manual start button: grey out only while busy / re-auth needed.
            // Auto-reply OFF does NOT grey it — a manual one-shot session is
            // allowed while the loop is off (稜指示 2026-07-12)
            const sessionBtn = document.getElementById('youtube-session-btn');
            if (sessionBtn) {{
                const busy = (state === 'running' || state === 'posting' || state === 'auth_error');
                sessionBtn.textContent = {_js(t('youtube.start_session'))};
                if (busy) {{
                    sessionBtn.style.background = '#607d8b';
                    sessionBtn.disabled = true;
                    sessionBtn.style.opacity = '0.5';
                    sessionBtn.style.cursor = 'not-allowed';
                }} else {{
                    sessionBtn.style.background = '#2196f3';
                    sessionBtn.disabled = false;
                    sessionBtn.style.opacity = '1';
                    sessionBtn.style.cursor = 'pointer';
                }}
            }}
        }};

        window._youtubeLoopBtnClick = function() {{
            const s = window._youtubeState;
            const target = !(s && s.enabled);
            if (window.wsManager && window.wsManager.ws && window.wsManager.ws.readyState === WebSocket.OPEN) {{
                window.wsManager.enqueueSend(JSON.stringify({{ action: 'youtube_set_enabled', enabled: target }}));
                const btn = document.getElementById('youtube-loop-btn');
                if (btn) {{ btn.disabled = true; btn.style.opacity = '0.5'; }}
            }}
        }};

        window._youtubeSessionBtnClick = function() {{
            const s = window._youtubeState;
            if (s && (s.state === 'running' || s.state === 'posting')) return; // greyed out
            if (window.wsManager) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'youtube_start_session',
                    idempotency_key: crypto.randomUUID()
                }}));
                const btn = document.getElementById('youtube-session-btn');
                if (btn) {{ btn.disabled = true; btn.style.opacity = '0.5'; }}
            }}
        }};

        window._youtubeRenderTimer = function() {{
            const s = window._youtubeState;
            if (!s) return;
            const timerEl = document.getElementById('youtube-status-timer');
            if (!timerEl) return;
            if (s.state !== 'idle' || typeof s.timer_remaining !== 'number') {{
                timerEl.textContent = '';
                return;
            }}
            // Countdown from the server snapshot. timer_paused=true means a
            // conversation is pausing the timer — show the frozen value.
            const offset = window._youtubeTimeOffset || 0;
            const now = Date.now() / 1000 + offset;
            const sinceUpdate = (s.timestamp && !s.timer_paused)
                ? Math.max(0, now - s.timestamp) : 0;
            const remaining = s.timer_remaining - sinceUpdate;
            if (remaining > 0) {{
                timerEl.textContent = {_js(t('js.youtube.until_next'))}.replace('{{time}}', window._elythFormatTime(remaining));
            }} else {{
                timerEl.textContent = {_js(t('js.youtube.waiting_start'))};
            }}
        }};
        // ====== End YouTube Reply Real-time Status ======

        window._toggleElythChar = function(charId) {{
            // Instant UI update: find the button for this character and flip it
            const container = document.querySelector('#elyth-char-list-container');
            if (container) {{
                const buttons = container.querySelectorAll('button[data-char-id]');
                buttons.forEach(btn => {{
                    if (btn.dataset.charId === charId) {{
                        const isOn = btn.textContent.trim() === 'ON';
                        btn.textContent = isOn ? 'OFF' : 'ON';
                        btn.style.backgroundColor = isOn ? '#607d8b' : '#4caf50';
                    }}
                }});
            }}
            // Send toggle to backend via WebSocket
            if (window.wsManager) {{
                window.wsManager.enqueueSend(JSON.stringify({{
                    action: 'elyth_toggle_character',
                    character_id: charId,
                    idempotency_key: crypto.randomUUID()
                }}));
            }}
        }};

        window.updateFeatureToggleButtons = function() {{
            // フールプルーフ: ブロック理由がある機能へ disabled/グレー/tooltip を
            // 毎描画で再適用(platformUnsupported の ce ボタンと同方式)。
            // 利用不能になった機能はサーバー側が強制OFFする(ONのまま無効は
            // 有効に見えて紛らわしい=稜裁定 2026-07-25)ため常に無効化。
            // 無効表現はグレー+disabled+tooltip の1形式(横線は廃止=稜裁定
            // 2026-08-15。従属無効も同じ見た目で tooltip の文言だけが違う)
            const applyAvail = function(btn, feature) {{
                const reason = (window.featureAvailability || {{}})[feature];
                if (reason) {{
                    btn.title = reason;
                    btn.disabled = true;
                    btn.style.opacity = '0.5';
                    btn.style.cursor = 'not-allowed';
                }} else {{
                    btn.title = '';
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    btn.style.cursor = 'pointer';
                }}
            }};

            const pcBtn = document.getElementById('pc-status-toggle-btn');
            const scBtn = document.getElementById('screen-capture-toggle-btn');
            const ttBtn = document.getElementById('talk-theme-toggle-btn');
            const slBtn = document.getElementById('speechless-toggle-btn');

            // サーバーモードのグレーアウト再適用ヘルパー(platformUnsupported の
            // ce ボタンと同方式: WS pushが文言/色を書き換えた後に毎回上塗りする)
            const applyServerModeBlock = function(btn) {{
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
                btn.title = {_js(t('utility.server_mode_unsupported'))};
            }};

            if (pcBtn) {{
                pcBtn.textContent = {_js(t('utility.pc_status'))}.replace('{{state}}', window.pcStatusEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                pcBtn.style.backgroundColor = window.pcStatusEnabled ? '#4caf50' : '#607d8b';
                if (window.serverModeBlocked) applyServerModeBlock(pcBtn);
            }}

            if (scBtn) {{
                scBtn.textContent = {_js(t('utility.screen_capture'))}.replace('{{state}}', window.screenCaptureEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                scBtn.style.backgroundColor = window.screenCaptureEnabled ? '#4caf50' : '#607d8b';
                applyAvail(scBtn, 'screen_capture');
                // 従属無効(PC Status OFF)は可用性より優先して無効化する。
                // tooltip は 利用不能理由 > 従属 (applyAvail が理由なしなら
                // title を空にした直後なので、空のときだけ従属文を入れる)
                if (!window.pcStatusEnabled) {{
                    scBtn.disabled = true;
                    scBtn.style.opacity = '0.5';
                    scBtn.style.cursor = 'not-allowed';
                    if (!scBtn.title) scBtn.title = {_js(t('utility.needs_pc_status'))};
                }}
                if (window.serverModeBlocked) applyServerModeBlock(scBtn);
            }}

            if (ttBtn) {{
                ttBtn.textContent = {_js(t('utility.talk_theme'))}.replace('{{state}}', window.talkThemeEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                ttBtn.style.backgroundColor = window.talkThemeEnabled ? '#4caf50' : '#607d8b';
            }}

            if (slBtn) {{
                slBtn.textContent = {_js(t('utility.speechless'))}.replace('{{state}}', window.speechlessEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                slBtn.style.backgroundColor = window.speechlessEnabled ? '#4caf50' : '#607d8b';
            }}

            const ceBtn = document.getElementById('command-execution-toggle-btn');
            if (ceBtn) {{
                ceBtn.textContent = {_js(t('utility.command'))}.replace('{{state}}', window.commandExecutionEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                ceBtn.style.backgroundColor = window.commandExecutionEnabled ? '#4caf50' : '#607d8b';
                applyAvail(ceBtn, 'command_execution');
                if (window.platformUnsupported && window.platformUnsupported.command_execution) {{
                    // Mac 3-6 layer 2: re-apply the greyed-out styling on
                    // every re-render (WS pushes rewrite text/colors above)
                    ceBtn.disabled = true;
                    ceBtn.style.opacity = '0.5';
                    ceBtn.style.cursor = 'not-allowed';
                    ceBtn.title = {_js(t('utility.platform_unsupported'))};
                }} else if (window.serverModeBlocked) {{
                    applyServerModeBlock(ceBtn);
                }}
            }}

            const ntBtn = document.getElementById('notes-toggle-btn');
            if (ntBtn) {{
                ntBtn.textContent = {_js(t('utility.notes'))}.replace('{{state}}', window.notesEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                ntBtn.style.backgroundColor = window.notesEnabled ? '#4caf50' : '#607d8b';
                applyAvail(ntBtn, 'notes');
            }}

            const igBtn = document.getElementById('image-gen-toggle-btn');
            if (igBtn) {{
                igBtn.textContent = {_js(t('utility.imagegen'))}.replace('{{state}}', window.imageGenerationEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                igBtn.style.backgroundColor = window.imageGenerationEnabled ? '#4caf50' : '#607d8b';
                applyAvail(igBtn, 'image_generation');
            }}

            const ccBtn = document.getElementById('camera-capture-toggle-btn');
            if (ccBtn) {{
                ccBtn.textContent = {_js(t('utility.camera'))}.replace('{{state}}', window.cameraCaptureEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                ccBtn.style.backgroundColor = window.cameraCaptureEnabled ? '#4caf50' : '#607d8b';
                applyAvail(ccBtn, 'camera_capture');
            }}

            // Live Camera depends on Camera (same shape as Screen Capture <- PC Status)
            const amBtn = document.getElementById('ambient-camera-toggle-btn');
            if (amBtn) {{
                amBtn.textContent = {_js(t('utility.ambient'))}.replace('{{state}}', window.ambientCameraEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                amBtn.style.backgroundColor = window.ambientCameraEnabled ? '#4caf50' : '#607d8b';
                applyAvail(amBtn, 'ambient_camera');
                // 従属無効(Camera OFF)は可用性より優先して無効化する。
                // tooltip は 利用不能理由 > 従属 (Screen Capture と同型)
                if (!window.cameraCaptureEnabled) {{
                    amBtn.disabled = true;
                    amBtn.style.opacity = '0.5';
                    amBtn.style.cursor = 'not-allowed';
                    if (!amBtn.title) amBtn.title = {_js(t('utility.needs_camera'))};
                }}
            }}

            const dsBtn = document.getElementById('deep-search-toggle-btn');
            if (dsBtn) {{
                dsBtn.textContent = {_js(t('utility.deepsearch'))}.replace('{{state}}', window.deepSearchEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                dsBtn.style.backgroundColor = window.deepSearchEnabled ? '#4caf50' : '#607d8b';
                applyAvail(dsBtn, 'deep_search');
            }}

            const elBtn = document.getElementById('elyth-toggle-btn');
            if (elBtn) {{
                elBtn.textContent = {_js(t('utility.elyth'))}.replace('{{state}}', window.elythEnabled ? {_js(t('utility.on'))} : {_js(t('utility.off'))});
                elBtn.style.backgroundColor = window.elythEnabled ? '#4caf50' : '#607d8b';
                applyAvail(elBtn, 'elyth');
            }}

            // Talk Theme Settings panel greyout
            const themePanel = document.getElementById('talk-theme-panel');
            if (themePanel) {{
                themePanel.style.opacity = window.talkThemeEnabled ? '1' : '0.5';
                themePanel.style.pointerEvents = window.talkThemeEnabled ? 'auto' : 'none';
            }}

            // フールプルーフ: STTエンジン選択の「OpenAI API」選択肢ロック。
            // OpenAIキー未設定なら選択不可(gr.Radioは選択肢単位のdisabledを
            // 持たないためJSでinputを無効化)。選択中(キー削除の残存)は
            // 触らない=ローカルへ戻す操作を封じない。安全網はサーバー側の
            // _change_stt_engine ガード。
            const sttReason = (window.featureAvailability || {{}}).stt_openai;
            const sttRadio = document.getElementById('stt-engine-radio');
            if (sttRadio) {{
                const sttInput = sttRadio.querySelector('input[value="openai"]');
                const sttLabel = sttInput ? sttInput.closest('label') : null;
                if (sttInput && !sttInput.checked) {{
                    sttInput.disabled = !!sttReason;
                    if (sttLabel) {{
                        sttLabel.style.opacity = sttReason ? '0.5' : '1';
                        sttLabel.style.cursor = sttReason ? 'not-allowed' : 'pointer';
                        sttLabel.title = sttReason || '';
                    }}
                }}
            }}

            // 疑似機能: Appear ボタン(motion_appear)。Motionフォルダ未設定/
            // 実体なし/キャラ未選択でグレーアウト(稜GO 2026-08-15)。
            // ただし表示中は同じボタンが Disappear として働くため
            // 無効化しない(表示中のキャラを消せなくなるデッド防止=
            // トグル群の「OFF操作は常に許可」と同型。Appearは会話外でも
            // 押せる+キャラ切替ではPlayerを閉じない裁定 2026-08-01 のため、
            // 表示中にMotion無しキャラへ切り替わるのは通常運用内)。
            const apBtn = document.getElementById('appear-btn');
            if (apBtn && !window.motionPngTuberActive) {{
                applyAvail(apBtn, 'motion_appear');
                // ブロック中は背景もグレー(紫のままだと目立つ=稜指摘
                // 2026-08-15)。利用可なら通常の紫へ戻す
                const apReason = (window.featureAvailability || {{}}).motion_appear;
                apBtn.style.backgroundColor = apReason ? '#607d8b' : '#9c27b0';
            }} else if (apBtn) {{
                // Disappearモード: グレー適用済みの痕跡を必ず解除
                apBtn.title = '';
                apBtn.disabled = false;
                apBtn.style.opacity = '1';
                apBtn.style.cursor = 'pointer';
            }}
        }};

        // 返り値を返さない: js+fn 同居では js の返り値がイベント引数に化けるため、
        // 0引数の load エンドポイントに対して毎ページロードで
        // Parameter `0` is not a valid keyword argument が発生し、
        // check_extraction_status の初期同期ごとサブミットが中断されていた
        // (稜のDevTools実測 2026-08-08・モバイルの先例=0ad9bee)。
    }}
    """
