/**
 * MotionPNGPlayer - Electron Preload Script
 *
 * Exposes a safe API to the renderer process for:
 * - IPC communication with main process
 * - File system access (through main process)
 * - Window controls
 * - Remote mode: WS command handling, settings
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
    // Receive initialization data from main process
    onInit: (callback) => {
        ipcRenderer.on('init', (event, data) => callback(data));
    },

    // Show context menu (right-click)
    showContextMenu: () => {
        ipcRenderer.send('show-context-menu');
    },

    // Resize window (scroll wheel)
    resizeWindow: (delta) => {
        ipcRenderer.send('resize-window', delta);
    },

    // Set scale directly (from slider)
    setScale: (scale) => {
        ipcRenderer.send('set-scale', scale);
    },

    // Toggle resize slider visibility
    onToggleResizeSlider: (callback) => {
        ipcRenderer.on('toggle-resize-slider', (event, scale) => callback(scale));
    },

    // Receive scale changed notification
    onScaleChanged: (callback) => {
        ipcRenderer.on('scale-changed', (event, scale) => callback(scale));
    },

    // Band (speech bubble / prompt input) visibility toggled from the context menu
    onSetBandVisibility: (callback) => {
        ipcRenderer.on('set-band-visibility', (event, bands) => callback(bands));
    },

    // Set base window size (when video dimensions are known)
    setBaseSize: (width, height) => {
        ipcRenderer.send('set-base-size', width, height);
    },

    // Load character folder and get motion data
    loadCharacterFolder: (folderPath) => {
        return ipcRenderer.invoke('load-character-folder', folderPath);
    },

    // Read file content
    readFile: (filePath) => {
        return ipcRenderer.invoke('read-file', filePath);
    },

    // Window drag (move window)
    startDrag: () => {
        ipcRenderer.send('window-drag-start');
    },
    dragWindow: (deltaX, deltaY) => {
        ipcRenderer.send('window-drag-move', deltaX, deltaY);
    },

    // --- Remote mode APIs ---

    // Handle WebSocket command via main process (remote mode)
    handleWsCommand: (data) => {
        return ipcRenderer.invoke('handle-ws-command', data);
    },

    // Notify main process of connection status (updates Tray menu)
    notifyConnectionStatus: (status) => {
        ipcRenderer.send('connection-status', status);
    },

    // --- Settings page APIs ---

    // Receive settings data when settings page loads
    onSettingsLoaded: (callback) => {
        ipcRenderer.on('settings-loaded', (event, settings) => callback(settings));
    },

    // Save settings
    saveSettings: (settings) => {
        return ipcRenderer.invoke('save-settings', settings);
    },

    // Test connection
    testConnection: (serverUrl, wsPort) => {
        return ipcRenderer.invoke('test-connection', serverUrl, wsPort);
    }
});

console.log('[Preload] electronAPI exposed to renderer');
