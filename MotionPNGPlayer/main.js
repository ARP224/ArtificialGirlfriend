/**
 * MotionPNGPlayer - Electron Main Process
 *
 * Two operating modes, selected by command-line arguments:
 * - Local mode  (--ws-port given): launched by AG itself on the same PC.
 *   Connects to ws://localhost, window shown immediately, quits when the
 *   window closes.
 * - Remote mode (no arguments): tray-resident client app. Connects to the
 *   AG server via WSS (Tailscale).
 * Both modes: lipsync frames only — audio is played by the browser UI
 * client, never by this app (see ws-audio.js header).
 *
 * Character assets live in <this folder>/Asset/<folder name>/ in both modes.
 */

const { app, BrowserWindow, ipcMain, Menu, Tray, protocol, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const i18n = require('./i18n');

// Increase memory limits for handling large audio data
app.commandLine.appendSwitch('js-flags', '--max-old-space-size=512');

// Disable GPU sandbox to prevent renderer crashes with audio processing
app.commandLine.appendSwitch('disable-gpu-sandbox');

// Enable hardware acceleration for video
app.commandLine.appendSwitch('enable-gpu-rasterization');

// Keep the GPU shader cache in memory only: the on-disk cache corrupts when
// the process is force-killed (AG's local mode does taskkill on disappear),
// and a corrupted cache breaks transparent-window compositing system-wide.
app.commandLine.appendSwitch('disable-gpu-shader-disk-cache');

let win = null;
let settingsWin = null;
let tray = null;
let scale = 1.0;

// Base window size (will be adjusted based on video dimensions)
let BASE_WIDTH = 540;
let BASE_HEIGHT = 960;

// Extra UI bands (fixed px, NOT scaled): speech bubble above the character,
// prompt input below. Window height = video area (scaled) + visible bands.
// Heights must match #bubble-band / #input-band CSS in index-electron.html.
const BUBBLE_BAND_H = 150;
const INPUT_BAND_H = 48;

function bandExtraHeight() {
    return (remoteSettings.show_bubble ? BUBBLE_BAND_H : 0)
         + (remoteSettings.show_prompt_input ? INPUT_BAND_H : 0);
}

function windowWidth() {
    return Math.max(100, Math.round(BASE_WIDTH * scale));
}

function windowHeight() {
    return Math.max(100, Math.round(BASE_HEIGHT * scale)) + bandExtraHeight();
}

function bandVisibility() {
    return {
        bubble: !!remoteSettings.show_bubble,
        input: !!remoteSettings.show_prompt_input
    };
}

// --- Mode & command line arguments ---

let mode = 'remote';         // 'local' | 'remote'
let cliWsPort = 0;           // local mode: WS port passed by AG
let cliCharacterFolder = ''; // local mode: character folder (path or bare name)
let cliLanguage = '';        // local mode: AG's UI language (player follows the app)

function parseArgs() {
    const args = process.argv.slice(1); // slice(1): works both packaged and via `electron .`
    for (let i = 0; i < args.length; i++) {
        if (args[i] === '--ws-port' && args[i + 1]) {
            cliWsPort = parseInt(args[i + 1], 10);
            i++;
        }
        if (args[i] === '--character-folder' && args[i + 1]) {
            cliCharacterFolder = args[i + 1];
            i++;
        }
        if (args[i] === '--language' && args[i + 1]) {
            cliLanguage = args[i + 1];
            i++;
        }
    }
    mode = cliWsPort ? 'local' : 'remote';
    console.log('[Main] Mode:', mode,
        mode === 'local' ? `(ws-port=${cliWsPort}, folder=${cliCharacterFolder}, lang=${cliLanguage})` : '');
}

// --- Asset resolution ---
// Bare names resolve to <app>/Asset/<name>; explicit paths are used as-is
// (backward compatibility with configs that still hold full paths).

const ASSET_DIR = path.join(__dirname, 'Asset');

function resolveCharacterPath(value) {
    if (!value) return '';
    const looksLikePath = value.includes('/') || value.includes('\\') || path.isAbsolute(value);
    return looksLikePath ? value : path.join(ASSET_DIR, value);
}

// --- Settings ---
// Settings in app.getPath('userData') (remote mode: server URL/port; both modes: scale)
let remoteSettings = {};

function getSettingsPath() {
    return path.join(app.getPath('userData'), 'settings.json');
}

function loadSettings() {
    const settingsPath = getSettingsPath();
    try {
        if (fs.existsSync(settingsPath)) {
            const data = fs.readFileSync(settingsPath, 'utf8');
            const settings = JSON.parse(data);
            if (typeof settings.scale === 'number') {
                scale = Math.max(0.3, Math.min(2.0, settings.scale));
            }
            remoteSettings = settings;
            console.log('[Main] Loaded settings from:', settingsPath);
        }
    } catch (error) {
        console.error('[Main] Failed to load settings:', error);
    }
}

function saveSettings() {
    const settingsPath = getSettingsPath();
    try {
        remoteSettings.scale = scale;
        // Ensure directory exists
        const dir = path.dirname(settingsPath);
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }
        fs.writeFileSync(settingsPath, JSON.stringify(remoteSettings, null, 2), 'utf8');
        console.log('[Main] Saved settings to:', settingsPath);
    } catch (error) {
        console.error('[Main] Failed to save settings:', error);
    }
}

function hasValidRemoteSettings() {
    return remoteSettings.server_url && (remoteSettings.server_port || remoteSettings.ws_port);
}

// Register custom protocol for local file access.
// Uses registerFileProtocol (native file streaming by Chromium): the
// Response-buffer style (protocol.handle) renders video invisibly in
// transparent windows on Windows — the mouth-only bug the original
// player also hit and solved this same way.
function registerLocalProtocol() {
    protocol.registerFileProtocol('local', (request, callback) => {
        // Chromium percent-encodes non-ASCII path characters (Japanese asset
        // folder names arrive as %E3%..), so decode. A raw '%' in a folder
        // name is left as-is by Chromium and makes decodeURIComponent throw —
        // fall back to the undecoded path instead of dying in the handler
        // (same guard as the MotionPNGCreator-bundled player).
        const encoded = request.url.replace('local://', '');
        let rawPath;
        try {
            rawPath = decodeURIComponent(encoded);
        } catch (e) {
            rawPath = encoded;
        }
        // URLs are local:///<path> (empty authority). For Windows paths the
        // parsed form is /C:/... — strip the leading slash before fs access.
        if (/^\/[A-Za-z]:/.test(rawPath)) {
            rawPath = rawPath.slice(1);
        }
        callback({ path: rawPath });
    });
}

// --- Window Creation ---

function createWindow(options = {}) {
    const show = options.show !== false;
    const initialWidth = windowWidth();
    const initialHeight = windowHeight();

    win = new BrowserWindow({
        width: initialWidth,
        height: initialHeight,
        minWidth: 50,
        minHeight: 50,
        frame: false,
        transparent: true,
        alwaysOnTop: true,
        resizable: false,
        hasShadow: false,
        skipTaskbar: false,
        show: show,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
            webSecurity: false  // Allow loading local files
        }
    });

    // Load the HTML file
    win.loadFile('index-electron.html');

    // Send initialization data when page is ready
    win.webContents.on('did-finish-load', () => {
        if (mode === 'local') {
            win.webContents.send('init', {
                mode: 'local',
                wsPort: cliWsPort,
                wsHost: 'localhost',
                characterPath: resolveCharacterPath(cliCharacterFolder),
                strings: i18n.all(),
                bands: bandVisibility()
            });
        } else {
            // Extract host from server_url (e.g., "https://machine.tailnet.ts.net" → "machine.tailnet.ts.net")
            let wsHost = '';
            try {
                const url = new URL(remoteSettings.server_url);
                wsHost = url.hostname;
            } catch (e) {
                wsHost = remoteSettings.server_url || '';
            }
            win.webContents.send('init', {
                mode: 'remote',
                wsPort: remoteSettings.server_port || remoteSettings.ws_port || 7860,
                wsHost: wsHost,
                strings: i18n.all(),
                bands: bandVisibility()
            });
        }
    });

    // Handle right-click context menu
    win.webContents.on('context-menu', (event, params) => {
        event.preventDefault();
        buildContextMenu().popup({ window: win });
    });

    // Open DevTools (F12 key)
    win.webContents.on('before-input-event', (event, input) => {
        if (input.key === 'F12') {
            win.webContents.toggleDevTools();
        }
    });

    // Transparent windows on Windows can come back with a stale/blank surface
    // after ANY visibility transition (hide/show, minimize/restore) — force a
    // full repaint every time the window becomes visible again.
    win.on('show', () => win.webContents.invalidate());
    win.on('restore', () => win.webContents.invalidate());

    // Handle window close
    win.on('closed', () => {
        win = null;
    });
}

// --- Context Menu ---
// Built on demand so labels always reflect the current language
let alwaysOnTopChecked = true;

function buildContextMenu() {
    return Menu.buildFromTemplate([
        {
            label: i18n.t('menu.minimize'),
            click: () => {
                if (win) win.minimize();
            }
        },
        { type: 'separator' },
        {
            label: i18n.t('menu.alwaysOnTop'),
            type: 'checkbox',
            checked: alwaysOnTopChecked,
            click: (menuItem) => {
                alwaysOnTopChecked = menuItem.checked;
                if (win) win.setAlwaysOnTop(menuItem.checked);
            }
        },
        { type: 'separator' },
        {
            label: i18n.t('menu.showBubble'),
            type: 'checkbox',
            checked: !!remoteSettings.show_bubble,
            click: (menuItem) => toggleBand('bubble', menuItem.checked)
        },
        {
            label: i18n.t('menu.showInput'),
            type: 'checkbox',
            checked: !!remoteSettings.show_prompt_input,
            click: (menuItem) => toggleBand('input', menuItem.checked)
        },
        { type: 'separator' },
        {
            label: i18n.t('menu.resize'),
            click: () => {
                if (win) {
                    win.webContents.send('toggle-resize-slider', scale);
                }
            }
        },
        {
            label: i18n.t('menu.resetSize'),
            click: () => {
                scale = 1.0;
                if (win) {
                    const [x, y] = win.getPosition();
                    win.setBounds({ x, y, width: windowWidth(), height: windowHeight() });
                    win.webContents.send('scale-changed', scale);
                }
                saveSettings();
            }
        },
        { type: 'separator' },
        {
            label: i18n.t('menu.quit'),
            click: () => {
                app.quit();
            }
        }
    ]);
}

// Toggle the speech-bubble / prompt-input band: persist, resize the window
// (bubble grows upward so the character stays put; input grows downward),
// and tell the renderer to show/hide the band.
function toggleBand(kind, on) {
    if (kind === 'bubble') {
        remoteSettings.show_bubble = on;
    } else {
        remoteSettings.show_prompt_input = on;
    }
    saveSettings();
    if (win) {
        const [x, y] = win.getPosition();
        const delta = (kind === 'bubble' ? BUBBLE_BAND_H : INPUT_BAND_H) * (on ? 1 : -1);
        const newY = kind === 'bubble' ? y - delta : y;
        win.setBounds({ x, y: newY, width: windowWidth(), height: windowHeight() });
        win.webContents.send('set-band-visibility', bandVisibility());
    }
}

// --- Auto-start at sign-in (2026-07-30, remote mode only — the tray is the
// --- only entry to this toggle and local mode has no tray) ---
// Win: HKCU Run value via reg.exe. A distinct value name per app on purpose —
// Electron's setLoginItemSettings() registers unpackaged apps under the
// shared "Electron" identity, so this player and AG Client Addon would
// overwrite each other's entry.
// Mac: LaunchAgent plist pointing at the installer-built applet via
// /usr/bin/open — keeps the TCC identity of a manual launch (same design as
// AG's launcher/startup_registry.py). State is a live read (no mirror).
// The installer registers this by default; the tray menu toggles it.

const RUN_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
const RUN_VALUE = 'MotionPNGPlayer';
const LA_LABEL = 'com.motionpngplayer';
const MAC_APPLET = path.join(__dirname, 'MotionPNGPlayer.app');

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

// --- Tray (Remote Mode) ---

let lastConnectionStatus = 'Disconnected';  // enum: 'Connected' | 'Disconnected'

function createTray() {
    // macOS: monochrome template image for the menu bar (22x22 + @2x).
    // Windows: colored 32x32 icon (a template image would be near-invisible).
    const iconFile = process.platform === 'win32' ? 'tray-icon-win.png' : 'tray-icon.png';
    const icon = nativeImage.createFromPath(path.join(__dirname, iconFile));
    if (process.platform === 'darwin') {
        icon.setTemplateImage(true);
    }

    tray = new Tray(icon);
    tray.setToolTip('MotionPNGPlayer');
    updateTrayMenu(lastConnectionStatus);
}

function updateTrayMenu(connectionStatus) {
    lastConnectionStatus = connectionStatus;
    const statusLabel = connectionStatus === 'Connected'
        ? i18n.t('tray.connected') : i18n.t('tray.disconnected');
    const menu = Menu.buildFromTemplate([
        {
            label: i18n.t('tray.status', statusLabel),
            enabled: false
        },
        { type: 'separator' },
        {
            label: i18n.t('tray.settings'),
            click: () => {
                showSettingsWindow();
            }
        },
        { type: 'separator' },
        {
            label: i18n.t('tray.autostart'),
            type: 'checkbox',
            checked: loginItemEnabled(),
            click: () => {
                setLoginItem(!loginItemEnabled());
                updateTrayMenu(lastConnectionStatus);
            }
        },
        { type: 'separator' },
        {
            label: i18n.t('tray.quit'),
            click: () => {
                app.quit();
            }
        }
    ]);
    tray.setContextMenu(menu);
}

// --- Settings Window (Remote Mode) ---

function showSettingsWindow() {
    if (settingsWin) {
        settingsWin.focus();
        return;
    }

    settingsWin = new BrowserWindow({
        width: 480,
        height: 400,
        resizable: false,
        minimizable: false,
        maximizable: false,
        title: i18n.t('settings.title'),
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false
        }
    });

    settingsWin.loadFile('settings-page.html');

    settingsWin.webContents.on('did-finish-load', () => {
        settingsWin.webContents.send('settings-loaded', {
            settings: remoteSettings,
            strings: i18n.all(),
            languages: i18n.available(),
            language: i18n.getLanguage()
        });
    });

    settingsWin.on('closed', () => {
        settingsWin = null;
    });
}

// --- IPC Handlers ---

// Show context menu
ipcMain.on('show-context-menu', () => {
    if (win) {
        buildContextMenu().popup({ window: win });
    }
});

// Resize window (scroll to resize)
ipcMain.on('resize-window', (event, delta) => {
    scale = Math.max(0.3, Math.min(2.0, scale + delta));
    if (win) {
        win.setSize(windowWidth(), windowHeight());
    }
});

// Set scale directly (from slider)
ipcMain.on('set-scale', (event, newScale) => {
    scale = Math.max(0.3, Math.min(2.0, newScale));

    if (win) {
        try {
            const [currentX, currentY] = win.getPosition();
            const [currentWidth, currentHeight] = win.getSize();
            const newWidth = windowWidth();
            const newHeight = windowHeight();

            // Fix bottom-left corner: calculate new Y so bottom stays in place
            const bottomY = currentY + currentHeight;
            const newY = bottomY - newHeight;

            win.setBounds({ x: currentX, y: newY, width: newWidth, height: newHeight });
        } catch (error) {
            console.error('[Main] Error setting scale:', error);
        }
    }
    saveSettings();
});

// Set base window size (called when video dimensions are known)
ipcMain.on('set-base-size', (event, width, height) => {
    console.log('[Main] set-base-size called with:', width, 'x', height);
    BASE_WIDTH = width;
    BASE_HEIGHT = height;
    if (win) {
        const [x, y] = win.getPosition();
        const newWidth = windowWidth();
        const newHeight = windowHeight();
        console.log('[Main] Applying base size with scale:', scale, '-> window:', newWidth, 'x', newHeight);
        win.setBounds({ x, y, width: newWidth, height: newHeight });
    }
});

// Window drag handlers
let dragStartPos = null;

ipcMain.on('window-drag-start', () => {
    if (win) {
        dragStartPos = win.getPosition();
    }
});

ipcMain.on('window-drag-move', (event, deltaX, deltaY) => {
    if (win && dragStartPos) {
        win.setPosition(dragStartPos[0] + deltaX, dragStartPos[1] + deltaY);
    }
});

// Load character folder and return motion data
// Flat structure: videos, track JSONs, and NPZs directly in root, shared mouth/ folder
ipcMain.handle('load-character-folder', async (event, folderPath) => {
    console.log('[Main] Loading character folder:', folderPath);

    if (!folderPath || !fs.existsSync(folderPath)) {
        console.error('[Main] Character folder not found:', folderPath);
        return { error: 'Folder not found', motions: [] };
    }

    try {
        const motions = [];
        const entries = fs.readdirSync(folderPath);

        // Check for shared mouth folder at root
        const mouthPath = path.join(folderPath, 'mouth');
        const hasMouth = fs.existsSync(mouthPath) &&
                         fs.statSync(mouthPath).isDirectory();

        if (!hasMouth) {
            console.error('[Main] No mouth folder found in:', folderPath);
            return { error: 'No mouth folder found', motions: [] };
        }

        console.log('[Main] Mouth folder:', mouthPath);

        // Verify required mouth sprites once (shared across all motions)
        const requiredSprites = ['closed.png', 'open.png'];
        const hasRequiredSprites = requiredSprites.every(sprite =>
            fs.existsSync(path.join(mouthPath, sprite))
        );

        if (!hasRequiredSprites) {
            console.error('[Main] Missing required mouth sprites (closed.png, open.png)');
            return { error: 'Missing required mouth sprites', motions: [] };
        }

        // Build available sprites list once
        const optionalSprites = ['half.png', 'e.png', 'u.png'];
        const availableSprites = ['closed.png', 'open.png'];
        for (const sprite of optionalSprites) {
            if (fs.existsSync(path.join(mouthPath, sprite))) {
                availableSprites.push(sprite);
            }
        }

        console.log('[Main] Available mouth sprites:', availableSprites);

        // Scan for video files directly in root
        for (const entry of entries) {
            if (!entry.endsWith('.webm') && !entry.endsWith('.mp4')) continue;

            // Find matching track JSON (same stem)
            const stem = entry.replace(/\.(webm|mp4)$/, '');
            const trackFile = stem + '.json';
            const trackPath = path.join(folderPath, trackFile);

            if (!fs.existsSync(trackPath)) {
                console.warn('[Main] No track JSON for video:', entry);
                continue;
            }

            motions.push({
                name: stem,
                videoPath: path.join(folderPath, entry),
                trackPath: trackPath,
                mouthPath: mouthPath,
                availableSprites: availableSprites
            });

            console.log('[Main] Found motion:', stem);
        }

        console.log('[Main] Total motions found:', motions.length);
        return { motions: motions };

    } catch (error) {
        console.error('[Main] Error loading character folder:', error);
        return { error: error.message, motions: [] };
    }
});

// Read file content (for mouth_track.json)
ipcMain.handle('read-file', async (event, filePath) => {
    try {
        const content = fs.readFileSync(filePath, 'utf8');
        return { content: content };
    } catch (error) {
        console.error('[Main] Error reading file:', filePath, error);
        return { error: error.message };
    }
});

// --- Remote Mode IPC ---

// Handle WebSocket commands from renderer (remote mode)
ipcMain.handle('handle-ws-command', async (event, data) => {
    const action = data.action;
    console.log('[Main] handle-ws-command:', action);

    if (action === 'character_appear') {
        const folderName = data.folder_name;
        const characterPath = resolveCharacterPath(folderName);

        if (!fs.existsSync(characterPath)) {
            console.error('[Main] Asset folder not found:', characterPath);
            return { success: false, error: i18n.t('player.assetNotFound', folderName) };
        }

        if (win) {
            win.show();
        }
        return { success: true, characterPath: characterPath };

    } else if (action === 'character_disappear') {
        if (win) {
            win.hide();
        }
        return { success: true };

    } else if (action === 'character_changed') {
        const folderName = data.folder_name;
        const characterPath = resolveCharacterPath(folderName);

        if (!fs.existsSync(characterPath)) {
            console.error('[Main] Asset folder not found for character change:', characterPath);
            if (win) {
                win.hide();
            }
            return { success: false, error: i18n.t('player.assetNotFound', folderName) };
        }

        return { success: true, characterPath: characterPath };
    }

    return { success: false, error: 'Unknown command: ' + action };
});

// Save settings from settings page
ipcMain.handle('save-settings', async (event, newSettings) => {
    remoteSettings = { ...remoteSettings, ...newSettings };
    saveSettings();
    console.log('[Main] Settings saved:', remoteSettings);

    // Apply language change immediately (tray menu; windows pick it up on next load)
    if (newSettings.language) {
        i18n.setLanguage(newSettings.language);
        if (tray) {
            updateTrayMenu(lastConnectionStatus);
        }
    }

    // If main window doesn't exist yet and we now have valid settings, create it
    if (!win && hasValidRemoteSettings()) {
        createWindow({ show: false });
    }

    return { success: true };
});

// Test connection from settings page
ipcMain.handle('test-connection', async (event, serverUrl, wsPort) => {
    // Connection test is performed from the settings page renderer directly
    // (it uses native WebSocket which works in the renderer)
    // This handler is a placeholder in case we need main-process logic later
    return { success: true, message: 'Use renderer-side test' };
});

// Notify main process of connection status change (from renderer)
ipcMain.on('connection-status', (event, status) => {
    console.log('[Main] Connection status:', status);
    if (tray) {
        updateTrayMenu(status);
    }
});

// --- App Lifecycle ---

app.whenReady().then(() => {
    parseArgs();
    loadSettings();
    // Language: local mode follows AG's UI language (CLI arg — must beat any
    // stale saved setting); otherwise saved setting wins, then the OS locale
    i18n.setLanguage(cliLanguage || remoteSettings.language || app.getLocale());
    registerLocalProtocol();

    // Ensure the shared asset folder exists (users drop character folders here)
    try {
        fs.mkdirSync(ASSET_DIR, { recursive: true });
    } catch (error) {
        console.error('[Main] Failed to create Asset dir:', error);
    }

    if (mode === 'local') {
        // Local mode: AG launched us — show window immediately, no tray
        createWindow({ show: true });
    } else {
        // Remote mode: tray-resident app
        // Hide dock icon on macOS (menu bar app only)
        if (app.dock) {
            app.dock.hide();
        }

        createTray();

        if (hasValidRemoteSettings()) {
            // Settings exist → create hidden window, renderer will connect via WSS
            createWindow({ show: false });
        }
        // No settings → tray only. Never pop a window at startup (ryo ruling
        // 2026-07-30: a resident app must enter the tray silently at sign-in;
        // settings open from the tray menu, and the save-settings handler
        // creates the hidden player window once a valid URL is saved).
    }
});

app.on('window-all-closed', () => {
    if (mode === 'local') {
        // Local mode: window is the app — closing it ends the process
        // (AG's process monitor detects this and updates the UI)
        app.quit();
    }
    // Remote mode: don't quit (Tray keeps app alive)
});
