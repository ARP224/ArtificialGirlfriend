/**
 * AG Client Addon - preload (context-isolated bridge)
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('agAddon', {
    getInit: () => ipcRenderer.invoke('ag:get-init'),
    saveConfig: (cfg) => ipcRenderer.invoke('ag:save-config', cfg),
    setLanguage: (lang) => ipcRenderer.invoke('ag:set-language', lang),
    reportWsStatus: (status) => ipcRenderer.send('ag:ws-status', status),
    onHotkey: (cb) => ipcRenderer.on('ag:hotkey', (_event, action) => cb(action))
});
