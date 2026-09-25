/**
 * AG Client Addon - main process
 *
 * Tray/menu-bar resident companion for Artificial Girlfriend server mode.
 * Registers global hotkeys via Electron globalShortcut (OS-level hotkey
 * registration: only the configured combos are received — no key logging,
 * and no macOS Accessibility/Input Monitoring permission is needed) and
 * forwards recording intents to the AG server over WebSocket. The WS
 * client itself lives in the (hidden-on-close) settings window renderer,
 * so no extra npm dependency is required beyond Electron.
 *
 * Hotkey semantics: startKey and stopKey are configured separately; setting
 * both to the same combo yields toggle behaviour (a single registration
 * that sends hotkey_toggle_recording).
 */

const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, nativeImage } = require('electron');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const i18n = require('./i18n');

const CONFIG_DEFAULTS = {
    agUrl: '',
    startKey: 'Ctrl+1',
    stopKey: 'Ctrl+1',   // same as startKey -> toggle mode by default
    language: 'ja',
    // Master switch: OFF = no connection, no reconnect attempts, no hotkeys
    // (稜裁定 2026-07-16: 常時再接続の無駄を止める1トグル。旧 hotkeysEnabled 統合)
    enabled: true
};

let tray = null;
let win = null;
let wsStatus = 'disconnected';   // disconnected | connecting | connected

// ---------------------------------------------------------------------------
// Config (lives in the OS user-data dir, not in the repo folder)
// ---------------------------------------------------------------------------

function configPath() {
    return path.join(app.getPath('userData'), 'config.json');
}

function osLanguage() {
    // First-launch default follows the OS UI language (same idea as AG's
    // display.language='auto'); an explicit choice in settings wins forever.
    try {
        return app.getLocale() || CONFIG_DEFAULTS.language;
    } catch (e) {
        return CONFIG_DEFAULTS.language;
    }
}

function loadConfig() {
    try {
        const raw = JSON.parse(fs.readFileSync(configPath(), 'utf8'));
        return { ...CONFIG_DEFAULTS, language: osLanguage(), ...raw };
    } catch (e) {
        return { ...CONFIG_DEFAULTS, language: osLanguage() };
    }
}

function saveConfig(cfg) {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(configPath(), JSON.stringify(cfg, null, 2), 'utf8');
}

// ---------------------------------------------------------------------------
// Global hotkeys
// ---------------------------------------------------------------------------

function sendAction(action) {
    if (win && !win.isDestroyed()) {
        win.webContents.send('ag:hotkey', action);
    }
}

/**
 * (Re-)register global shortcuts from config.
 * Returns { ok: bool, failed: [accelerator, ...] } — a register() returning
 * false means the combo is taken by another app / the OS.
 */
function applyHotkeys(cfg) {
    globalShortcut.unregisterAll();
    const failed = [];
    if (!cfg.enabled) {
        return { ok: true, failed };
    }
    if (cfg.startKey && cfg.startKey === cfg.stopKey) {
        // Same combo on both -> toggle mode
        if (!globalShortcut.register(cfg.startKey, () => sendAction('hotkey_toggle_recording'))) {
            failed.push(cfg.startKey);
        }
    } else {
        if (cfg.startKey &&
            !globalShortcut.register(cfg.startKey, () => sendAction('hotkey_start_recording'))) {
            failed.push(cfg.startKey);
        }
        if (cfg.stopKey &&
            !globalShortcut.register(cfg.stopKey, () => sendAction('hotkey_stop_recording'))) {
            failed.push(cfg.stopKey);
        }
    }
    return { ok: failed.length === 0, failed };
}

// ---------------------------------------------------------------------------
// Auto-start at sign-in (2026-07-30)
// Win: HKCU Run value via reg.exe. A distinct value name per app on purpose —
// Electron's setLoginItemSettings() registers unpackaged apps under the
// shared "Electron" identity, so this addon and MotionPNGPlayer would
// overwrite each other's entry.
// Mac: LaunchAgent plist pointing at the installer-built applet via
// /usr/bin/open — keeps the TCC identity of a manual launch (same design as
// AG's launcher/startup_registry.py). State is a live read (no mirror).
// The installer registers this by default; the tray menu toggles it.
// ---------------------------------------------------------------------------

const RUN_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
const RUN_VALUE = 'AGClientAddon';
const LA_LABEL = 'com.agclientaddon';
const MAC_APPLET = path.join(__dirname, 'AG Client Addon.app');

function launchAgentPath() {
    return path.join(app.getPath('home'), 'Library', 'LaunchAgents', LA_LABEL + '.plist');
}

function loginItemEnabled() {
    if (process.platform === 'win32') {
        try {
            execFileSync('reg', ['query', RUN_KEY, '/v', RUN_VALUE], { stdio: 'ignore' });
            return true;
        } catch (e) {
            return false;
        }
    }
    return fs.existsSync(launchAgentPath());
}

function setLoginItem(enabled) {
    try {
        if (process.platform === 'win32') {
            if (enabled) {
                execFileSync('reg', ['add', RUN_KEY, '/v', RUN_VALUE, '/t', 'REG_SZ',
                    '/d', `"${process.execPath}" "${__dirname}"`, '/f'], { stdio: 'ignore' });
            } else {
                try {
                    execFileSync('reg', ['delete', RUN_KEY, '/v', RUN_VALUE, '/f'], { stdio: 'ignore' });
                } catch (e) { /* already unregistered */ }
            }
            return true;
        }
        const la = launchAgentPath();
        if (enabled) {
            if (!fs.existsSync(MAC_APPLET)) {
                console.warn('[LoginItem] applet missing - run the installer first:', MAC_APPLET);
                return false;
            }
            fs.mkdirSync(path.dirname(la), { recursive: true });
            fs.rmSync(la, { force: true });
            const PB = '/usr/libexec/PlistBuddy';
            execFileSync(PB, ['-c', 'Add :Label string ' + LA_LABEL, la], { stdio: 'ignore' });
            execFileSync(PB, ['-c', 'Add :RunAtLoad bool true', la], { stdio: 'ignore' });
            execFileSync(PB, ['-c', 'Add :ProgramArguments array', la], { stdio: 'ignore' });
            execFileSync(PB, ['-c', 'Add :ProgramArguments:0 string /usr/bin/open', la], { stdio: 'ignore' });
            execFileSync(PB, ['-c', 'Add :ProgramArguments:1 string -a', la], { stdio: 'ignore' });
            execFileSync(PB, ['-c', 'Add :ProgramArguments:2 string ' + MAC_APPLET, la], { stdio: 'ignore' });
        } else {
            fs.rmSync(la, { force: true });
            try {
                execFileSync('launchctl',
                    ['bootout', 'gui/' + process.getuid() + '/' + LA_LABEL], { stdio: 'ignore' });
            } catch (e) { /* not loaded - fine */ }
        }
        return true;
    } catch (e) {
        console.warn('[LoginItem] change failed:', e.message);
        return false;
    }
}

// ---------------------------------------------------------------------------
// Tray / window
// ---------------------------------------------------------------------------

function trayStatusLabel() {
    return i18n.t('tray.status.' + wsStatus);
}

function rebuildTrayMenu() {
    if (!tray) return;
    const menu = Menu.buildFromTemplate([
        { label: trayStatusLabel(), enabled: false },
        { type: 'separator' },
        { label: i18n.t('tray.open_settings'), click: () => showWindow() },
        { type: 'separator' },
        {
            label: i18n.t('tray.autostart'),
            type: 'checkbox',
            checked: loginItemEnabled(),
            click: () => { setLoginItem(!loginItemEnabled()); rebuildTrayMenu(); }
        },
        { type: 'separator' },
        { label: i18n.t('tray.quit'), click: () => { app.isQuitting = true; app.quit(); } }
    ]);
    tray.setContextMenu(menu);
    tray.setToolTip('AG Client Addon — ' + trayStatusLabel());
}

function createTray() {
    // macOS: template image (black shape + alpha) — the "Template" filename
    // suffix opts into system rendering, which draws it white on dark menu
    // bars automatically (@2x variant is picked up by basename). Windows:
    // colored glyph — the taskbar tray follows the system theme and can be
    // light OR dark (Win11 defaults to light), so a fixed white/black shape
    // can vanish; the accent color stays visible on both.
    const iconFile = process.platform === 'darwin'
        ? 'trayTemplate.png' : 'tray-color.png';
    const icon = nativeImage.createFromPath(path.join(__dirname, 'icons', iconFile));
    tray = new Tray(icon);
    tray.on('double-click', () => showWindow());   // Windows convention
    rebuildTrayMenu();
}

function showWindow() {
    if (win && !win.isDestroyed()) {
        win.show();
        win.focus();
    }
}

function createWindow() {
    win = new BrowserWindow({
        width: 520,
        height: 640,
        show: false,
        resizable: true,
        icon: path.join(__dirname, 'icons', 'icon128.png'),
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            // The hidden window hosts the WS client; never throttle it.
            backgroundThrottling: false
        }
    });
    win.setMenuBarVisibility(false);
    win.loadFile('settings.html');

    // Closing the window hides it (tray app); quit only via tray menu.
    win.on('close', (e) => {
        if (!app.isQuitting) {
            e.preventDefault();
            win.hide();
        }
    });

    // While the settings window is focused, suspend the global shortcuts:
    // they are intercepted OS-wide — including inside this very window — so
    // typing an already-registered combo into a key-capture field would fire
    // the hotkey instead of updating the field (稜実測 2026-07-16).
    win.on('focus', () => {
        globalShortcut.unregisterAll();
    });
    win.on('blur', () => {
        applyHotkeys(loadConfig());
    });
}

// ---------------------------------------------------------------------------
// IPC
// ---------------------------------------------------------------------------

ipcMain.handle('ag:get-init', () => {
    const cfg = loadConfig();
    i18n.setLanguage(cfg.language);
    cfg.language = i18n.getLanguage();   // normalize e.g. "ja-JP" -> "ja" for the dropdown
    return {
        config: cfg,
        dict: i18n.all(),
        languages: i18n.available(),
        platform: process.platform
    };
});

ipcMain.handle('ag:save-config', (_event, cfg) => {
    const merged = { ...CONFIG_DEFAULTS, ...cfg };
    saveConfig(merged);
    i18n.setLanguage(merged.language);
    const result = applyHotkeys(merged);
    // Keep shortcuts suspended while the settings window stays focused
    // (applyHotkeys above still validated registration and reported failures)
    if (win && !win.isDestroyed() && win.isFocused()) {
        globalShortcut.unregisterAll();
    }
    rebuildTrayMenu();
    // Renderer re-reads dict for live language switching
    return { ...result, dict: i18n.all() };
});

ipcMain.handle('ag:set-language', (_event, lang) => {
    // Instant language switch (applies + persists immediately; other form
    // fields are untouched — Save keeps its own semantics)
    const cfg = loadConfig();
    cfg.language = lang;
    saveConfig(cfg);
    i18n.setLanguage(lang);
    rebuildTrayMenu();
    return { dict: i18n.all() };
});

ipcMain.on('ag:ws-status', (_event, status) => {
    wsStatus = status;
    rebuildTrayMenu();
});

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
    app.quit();
} else {
    app.on('second-instance', () => showWindow());

    app.whenReady().then(() => {
        const cfg = loadConfig();
        i18n.setLanguage(cfg.language);
        // Tray-only app: no dock icon on macOS
        if (process.platform === 'darwin' && app.dock) {
            app.dock.hide();
        }
        createWindow();
        createTray();
        const result = applyHotkeys(cfg);
        if (!result.ok) {
            console.warn('[Hotkey] Failed to register:', result.failed.join(', '));
        }
        // Never pop the settings window at startup, even unconfigured (ryo
        // ruling 2026-07-30: a resident app must enter the tray silently at
        // sign-in; settings open from the tray icon - double-click or menu).
    });

    app.on('will-quit', () => {
        globalShortcut.unregisterAll();
    });

    // Tray app: keep running when the window is hidden/closed
    app.on('window-all-closed', () => { /* no-op */ });
}
