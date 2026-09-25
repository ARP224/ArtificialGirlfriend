/**
 * AG Client Addon - i18n helper (main process)
 *
 * Locales live in locales/<code>.json; adding a language = adding one file
 * (its "_name" key is the display name shown in the settings dropdown).
 * The renderer receives the merged dictionary via IPC (ag:get-init) and
 * uses a small inline t() helper — no separate renderer module needed.
 * Same pattern as MotionPNGPlayer/i18n.js.
 */

const fs = require('fs');
const path = require('path');

const LOCALES_DIR = path.join(__dirname, 'locales');
const FALLBACK = 'en';

const locales = {};
let currentLanguage = FALLBACK;

function loadLocales() {
    for (const file of fs.readdirSync(LOCALES_DIR)) {
        if (!file.endsWith('.json')) continue;
        const code = path.basename(file, '.json');
        try {
            locales[code] = JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, file), 'utf8'));
        } catch (error) {
            console.error('[i18n] Failed to load locale:', file, error);
        }
    }
}
loadLocales();

/** Set the active language. Accepts full locales like "ja-JP" (matched by prefix). */
function setLanguage(lang) {
    if (!lang) return;
    if (locales[lang]) {
        currentLanguage = lang;
        return;
    }
    const prefix = String(lang).split(/[-_]/)[0];
    if (locales[prefix]) {
        currentLanguage = prefix;
    }
}

function getLanguage() {
    return currentLanguage;
}

/** Translate a key; {0},{1}... are replaced with the given arguments. */
function t(key, ...args) {
    const table = locales[currentLanguage] || {};
    const fallback = locales[FALLBACK] || {};
    const template = table[key] || fallback[key] || key;
    return template.replace(/\{(\d+)\}/g, (m, i) => (args[i] !== undefined ? args[i] : m));
}

/** Full dictionary for the active language (fallback-merged) — sent to renderers. */
function all() {
    return { ...(locales[FALLBACK] || {}), ...(locales[currentLanguage] || {}) };
}

/** Available languages for the settings dropdown. */
function available() {
    return Object.keys(locales).sort().map(code => ({
        code,
        name: (locales[code] && locales[code]._name) || code
    }));
}

module.exports = { t, all, available, setLanguage, getLanguage };
