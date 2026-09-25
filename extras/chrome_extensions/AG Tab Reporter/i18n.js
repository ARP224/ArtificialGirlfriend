// AG Tab Reporter - popup i18n
// _locales/<lang>/messages.json を単一真実源として popup 文言を実行時に切り替える。
// Chrome標準の chrome.i18n.getMessage はブラウザ言語固定で切替できないため、
// 同じ翻訳ファイルを fetch で自前ロードする方式（manifest の名前/説明文は
// Chrome標準機構でブラウザ言語に自動追随、popup はセレクタに追随）。
//
// 言語追加の手順:
//   1. _locales/<lang>/messages.json を追加（en を複製して翻訳）
//   2. 下の SUPPORTED_LANGUAGES に1行追加

const SUPPORTED_LANGUAGES = [
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
];
const DEFAULT_LANGUAGE = "en";
const LANGUAGE_STORAGE_KEY = "language";

let messages = {};

// ブラウザのUI言語から初期言語を決定（対応外の言語は英語）
function detectBrowserLanguage() {
  const uiLang = chrome.i18n.getUILanguage().toLowerCase();
  const match = SUPPORTED_LANGUAGES.find(
    (l) => uiLang === l.code || uiLang.startsWith(l.code + "-")
  );
  return match ? match.code : DEFAULT_LANGUAGE;
}

// 保存済みの言語設定を取得（未保存ならブラウザ言語）
async function getSavedLanguage() {
  const stored = await chrome.storage.local.get(LANGUAGE_STORAGE_KEY);
  const saved = stored[LANGUAGE_STORAGE_KEY];
  if (SUPPORTED_LANGUAGES.some((l) => l.code === saved)) {
    return saved;
  }
  return detectBrowserLanguage();
}

// 言語を保存して翻訳をロード
async function setLanguage(code) {
  await chrome.storage.local.set({ [LANGUAGE_STORAGE_KEY]: code });
  await loadMessages(code);
}

// 翻訳ファイルをロード
async function loadMessages(code) {
  const url = chrome.runtime.getURL(`_locales/${code}/messages.json`);
  const response = await fetch(url);
  messages = await response.json();
}

// 翻訳を取得（キー未定義時はキー名をそのまま返す＝欠落が目視できる）
function t(key) {
  return messages[key] && messages[key].message ? messages[key].message : key;
}

// data-i18n属性を持つ全要素に翻訳を適用
function applyTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
}
