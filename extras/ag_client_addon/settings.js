/**
 * AG Client Addon - settings window renderer
 *
 * Hosts both the settings form and the WebSocket client (the window is
 * hidden on close, never destroyed, and backgroundThrottling is off, so
 * the connection lives for the whole app lifetime).
 *
 * WS protocol: connects to the AG server, identifies as client_type
 * "addon", then sends hotkey_{start,stop,toggle}_recording actions when
 * the main process reports a global hotkey press. The server answers with
 * hotkey_response {success, intent, reason}.
 */

let dict = {};
let config = null;
let platform = null;   // process.platform from the main process (via ag:get-init)

function t(key) {
    return dict[key] || key;
}

function applyI18nToDom() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.getAttribute('data-i18n'));
    });
    document.title = t('app.title');
}

// ---------------------------------------------------------------------------
// WebSocket client
// ---------------------------------------------------------------------------

// Reconnect backoff: quick retries first, then settle at 60 s (no more
// hammering a server that is simply off). Reset on success / manual connect.
const RECONNECT_SCHEDULE_MS = [5000, 15000, 30000, 60000];
// App-level keepalive: server-mode WS sends no protocol pings, and a NAT
// path that dies silently leaves this socket half-open forever ("接続中"
// のまま固まる稜実測 2026-07-16). Ping every 30 s; no pong in 10 s = dead.
const KEEPALIVE_INTERVAL_MS = 30000;
const PONG_TIMEOUT_MS = 10000;
let ws = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let keepaliveTimer = null;
let pongWatchdog = null;

/** "https://host:port" -> "wss://host:port/ws"; ws:///wss:// passed through. */
function deriveWsUrl(input) {
    const raw = (input || '').trim().replace(/\/+$/, '');
    if (!raw) return '';
    if (raw.startsWith('ws://') || raw.startsWith('wss://')) return raw;
    try {
        const u = new URL(raw);
        const proto = u.protocol === 'https:' ? 'wss:' : 'ws:';
        return proto + '//' + u.host + '/ws';
    } catch (e) {
        return '';
    }
}

let currentWsStatus = 'disconnected';

function setWsStatus(status) {
    currentWsStatus = status;
    const dotEl = document.getElementById('ws-dot');
    dotEl.className = 'dot ' + status;
    document.getElementById('ws-status-text').textContent = t('ws.' + status);
    window.agAddon.reportWsStatus(status);
}

function logEvent(line) {
    const el = document.getElementById('event-log');
    const time = new Date().toLocaleTimeString();
    el.textContent = (time + '  ' + line + '\n' + el.textContent)
        .split('\n').slice(0, 6).join('\n');
}

function disconnect(statusToken) {
    stopKeepalive();
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) {
        // Detach handlers first: the old socket's onclose must not fire and
        // schedule a reconnect that would fight the new state.
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
        try { ws.close(); } catch (e) { /* ignore */ }
        ws = null;
    }
    setWsStatus(statusToken || 'disconnected');
}

function connect() {
    if (!config.enabled) {
        disconnect('disabled');
        return;
    }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    const url = deriveWsUrl(config.agUrl);
    if (!url) {
        setWsStatus('disconnected');
        return;
    }
    disconnect();
    setWsStatus('connecting');
    try {
        ws = new WebSocket(url);
    } catch (e) {
        setWsStatus('disconnected');
        scheduleReconnect();
        return;
    }

    ws.onopen = () => {
        ws.send(JSON.stringify({ type: 'identify', client_type: 'addon' }));
        reconnectAttempt = 0;
        setWsStatus('connected');
        logEvent(t('log.connected'));
        startKeepalive();
    };

    ws.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch (e) { return; }
        if (data.action === 'pong') {
            if (pongWatchdog) { clearTimeout(pongWatchdog); pongWatchdog = null; }
        } else if (data.action === 'hotkey_response') {
            const intentLabel = t('intent.' + data.intent);
            if (data.success) {
                logEvent(t('log.hotkey_ok').replace('{intent}', intentLabel));
            } else {
                const reason = t('reason.' + data.reason);
                logEvent(t('log.hotkey_ng')
                    .replace('{intent}', intentLabel)
                    .replace('{reason}', reason));
            }
        }
        // Other broadcasts (chat updates etc.) are irrelevant to the addon.
    };

    ws.onclose = () => {
        ws = null;
        stopKeepalive();
        setWsStatus('disconnected');
        scheduleReconnect();
    };

    ws.onerror = () => { /* onclose follows */ };
}

function scheduleReconnect() {
    if (!config.enabled) return;
    if (reconnectTimer || !deriveWsUrl(config.agUrl)) return;
    const delay = RECONNECT_SCHEDULE_MS[
        Math.min(reconnectAttempt, RECONNECT_SCHEDULE_MS.length - 1)];
    reconnectAttempt++;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, delay);
}

function startKeepalive() {
    stopKeepalive();
    keepaliveTimer = setInterval(() => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        try { ws.send(JSON.stringify({ action: 'ping' })); } catch (e) { return; }
        if (pongWatchdog) clearTimeout(pongWatchdog);
        pongWatchdog = setTimeout(() => {
            pongWatchdog = null;
            // No pong: the link is dead (silent NAT drop). Force the close
            // path so status turns red and the backoff reconnect takes over.
            console.warn('[Keepalive] pong timeout — closing dead socket');
            if (ws) {
                const dead = ws;
                ws = null;
                dead.onopen = dead.onmessage = dead.onclose = dead.onerror = null;
                try { dead.close(); } catch (e) { /* ignore */ }
                stopKeepalive();
                setWsStatus('disconnected');
                scheduleReconnect();
            }
        }, PONG_TIMEOUT_MS);
    }, KEEPALIVE_INTERVAL_MS);
}

function stopKeepalive() {
    if (keepaliveTimer) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
    if (pongWatchdog) { clearTimeout(pongWatchdog); pongWatchdog = null; }
}

// Global hotkey pressed (from main process) -> send the action
window.agAddon.onHotkey((action) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        const idem = (typeof crypto !== 'undefined' && crypto.randomUUID)
            ? crypto.randomUUID()
            : Date.now() + '-' + Math.random().toString(16).slice(2);
        ws.send(JSON.stringify({
            action: action,
            idempotency_key: idem
        }));
        logEvent(t('log.sent').replace('{action}', t('intent.' + action.replace('hotkey_', '').replace('_recording', ''))));
    } else {
        logEvent(t('log.not_connected'));
    }
});

// ---------------------------------------------------------------------------
// Hotkey capture fields (keydown -> Electron accelerator string)
// ---------------------------------------------------------------------------

function baseKeyFromEvent(e) {
    // e.code (physical key) first — IME/layout-proof (Japanese IME can turn
    // e.key into 'Process'). e.key as fallback — covers input gadgets and
    // remappers that synthesize events without a proper code.
    const code = e.code || '';
    if (/^Key[A-Z]$/.test(code)) return code.slice(3);
    if (/^Digit\d$/.test(code)) return code.slice(5);
    if (/^Numpad\d$/.test(code)) return 'num' + code.slice(6);
    if (/^F\d{1,2}$/.test(code)) return code;
    if (code === 'Space') return 'Space';
    if (/^Arrow(Up|Down|Left|Right)$/.test(code)) return code.slice(5);
    const k = e.key || '';
    if (/^[a-zA-Z]$/.test(k)) return k.toUpperCase();
    if (/^[0-9]$/.test(k)) return k;
    if (/^F\d{1,2}$/.test(k)) return k;
    if (k === ' ') return 'Space';
    return null;   // pure modifier press / unsupported key
}

// Capture mode: click the base-key box to arm it (blue outline), then the
// NEXT key press anywhere in the window lands in it. Only a SINGLE key needs
// to arrive: modifiers are chosen via checkboxes, because macOS system
// shortcuts (Mission Control's Ctrl+digit etc.) can consume modifier combos
// BEFORE the app ever sees them — 稜実測 2026-07-16「単一キーなら動く・
// 組み合わせは届かない」の対処. (Normally those combos still work as
// hotkeys because RegisterEventHotKey outranks the system shortcut; while
// the settings window suspends registration they fall through to the OS.)
// If a full combo DOES arrive, its modifiers fill the checkboxes as a bonus.
let captureTarget = null;

function setCaptureTarget(el) {
    if (captureTarget) captureTarget.classList.remove('capturing');
    captureTarget = el;
    if (el) el.classList.add('capturing');
}

function wireKeyCapture(inputId) {
    const el = document.getElementById(inputId);
    el.addEventListener('click', (e) => {
        e.stopPropagation();
        setCaptureTarget(el);
    });
    el.addEventListener('focus', () => setCaptureTarget(el));
}

document.addEventListener('keydown', (e) => {
    if (!captureTarget) return;
    // Typing in a different field (URL etc.): stop capturing, let it through
    const tag = (e.target && e.target.tagName) || '';
    if (e.target !== captureTarget &&
        (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA')) {
        setCaptureTarget(null);
        return;
    }
    e.preventDefault();
    e.stopPropagation();
    console.log('[KeyCapture]', e.code, e.key);
    if (e.code === 'Escape') {
        setCaptureTarget(null);
        return;
    }
    if (e.code === 'Backspace' || e.code === 'Delete') {
        captureTarget.value = '';
        return;
    }
    const base = baseKeyFromEvent(e);
    if (!base) return;   // modifier-only press: keep waiting for the base key
    captureTarget.value = base;
    // Bonus: if a full combo made it through the OS, reflect its modifiers
    const prefix = captureTarget.id.replace('-key', '');
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) {
        document.getElementById(prefix + '-mod-ctrl').checked = e.ctrlKey;
        document.getElementById(prefix + '-mod-cmd').checked = e.metaKey;
        document.getElementById(prefix + '-mod-alt').checked = e.altKey;
        document.getElementById(prefix + '-mod-shift').checked = e.shiftKey;
    }
    setCaptureTarget(null);   // one key per arm — done
}, true);

// Clicking anywhere else disarms capture mode
document.addEventListener('click', (e) => {
    if (captureTarget && e.target !== captureTarget) setCaptureTarget(null);
});

// ---------------------------------------------------------------------------
// Form
// ---------------------------------------------------------------------------

/** "Ctrl+Shift+1" -> checkboxes + base-key box for the given prefix. */
function setKeyUi(prefix, accel) {
    const mods = { ctrl: false, cmd: false, alt: false, shift: false };
    let base = '';
    (accel || '').split('+').filter(Boolean).forEach(p => {
        if (p === 'Ctrl' || p === 'Control') mods.ctrl = true;
        else if (p === 'Cmd' || p === 'Command') mods.cmd = true;
        else if (p === 'CommandOrControl' || p === 'CmdOrCtrl') { mods.ctrl = true; }
        else if (p === 'Alt' || p === 'Option') mods.alt = true;
        else if (p === 'Shift') mods.shift = true;
        else base = p;
    });
    document.getElementById(prefix + '-mod-ctrl').checked = mods.ctrl;
    document.getElementById(prefix + '-mod-cmd').checked = mods.cmd;
    document.getElementById(prefix + '-mod-alt').checked = mods.alt;
    document.getElementById(prefix + '-mod-shift').checked = mods.shift;
    document.getElementById(prefix + '-key').value = base;
}

/** Checkboxes + base-key box -> "Ctrl+Shift+1" ('' when no base key set). */
function buildAccelerator(prefix) {
    const base = document.getElementById(prefix + '-key').value.trim();
    if (!base) return '';
    const parts = [];
    if (document.getElementById(prefix + '-mod-ctrl').checked) parts.push('Ctrl');
    // Cmd exists only on macOS; elsewhere the (hidden) checkbox could still
    // get checked via the capture bonus path (metaKey = Windows key), so the
    // guard lives here at the single point that emits accelerators.
    if (platform === 'darwin' &&
        document.getElementById(prefix + '-mod-cmd').checked) parts.push('Cmd');
    if (document.getElementById(prefix + '-mod-alt').checked) parts.push('Alt');
    if (document.getElementById(prefix + '-mod-shift').checked) parts.push('Shift');
    parts.push(base);
    return parts.join('+');
}

function fillForm() {
    document.getElementById('ag-url').value = config.agUrl;
    document.getElementById('addon-enabled').checked = config.enabled;
    setKeyUi('start', config.startKey);
    setKeyUi('stop', config.stopKey);
    document.getElementById('language').value = config.language;
}

async function save() {
    const newConfig = {
        agUrl: document.getElementById('ag-url').value.trim().replace(/\/+$/, ''),
        enabled: document.getElementById('addon-enabled').checked,
        startKey: buildAccelerator('start'),
        stopKey: buildAccelerator('stop'),
        language: document.getElementById('language').value
    };
    const urlChanged = newConfig.agUrl !== config.agUrl;
    const result = await window.agAddon.saveConfig(newConfig);
    config = newConfig;
    dict = result.dict;          // live language switch
    applyI18nToDom();

    const resEl = document.getElementById('save-result');
    if (result.ok) {
        resEl.textContent = t('settings.saved');
        resEl.className = 'ok';
    } else {
        resEl.textContent = t('settings.key_register_failed')
            .replace('{keys}', result.failed.join(', '));
        resEl.className = 'err';
    }
    setTimeout(() => { resEl.textContent = ''; }, 4000);

    reconnectAttempt = 0;
    if (!config.enabled) {
        disconnect('disabled');
    } else if (urlChanged || !ws) {
        connect();
    } else {
        setWsStatus(ws.readyState === WebSocket.OPEN ? 'connected' : 'connecting');
    }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async () => {
    const init = await window.agAddon.getInit();
    config = init.config;
    dict = init.dict;
    platform = init.platform;

    const langSel = document.getElementById('language');
    init.languages.forEach(l => {
        const opt = document.createElement('option');
        opt.value = l.code;
        opt.textContent = l.name;
        langSel.appendChild(opt);
    });
    // Language applies (and persists) the instant it is switched (稜要望)
    langSel.addEventListener('change', async () => {
        const result = await window.agAddon.setLanguage(langSel.value);
        config.language = langSel.value;
        dict = result.dict;
        applyI18nToDom();
        setWsStatus(currentWsStatus);   // re-localize the status line too
    });

    applyI18nToDom();
    fillForm();
    // Cmd is a macOS-only modifier: hide (and clear) its checkboxes elsewhere.
    // After fillForm, so a stored accelerator cannot re-check a hidden box.
    if (platform !== 'darwin') {
        ['start-mod-cmd', 'stop-mod-cmd'].forEach(id => {
            const box = document.getElementById(id);
            box.checked = false;
            box.closest('label').style.display = 'none';
        });
    }
    wireKeyCapture('start-key');
    wireKeyCapture('stop-key');
    document.getElementById('save-btn').addEventListener('click', save);
    document.getElementById('connect-btn').addEventListener('click', () => {
        if (!config.enabled) {
            logEvent(t('log.disabled'));
            return;
        }
        reconnectAttempt = 0;   // manual connect skips any backoff wait
        connect();
    });
    connect();   // sets 'disabled' status itself when the toggle is off
})();
