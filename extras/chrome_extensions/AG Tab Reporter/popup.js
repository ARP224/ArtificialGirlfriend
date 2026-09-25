// AG Tab Reporter - Popup Script

document.addEventListener("DOMContentLoaded", async () => {
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const serverUrl = document.getElementById("server-url");
  const reconnectBtn = document.getElementById("reconnect-btn");
  const languageSelect = document.getElementById("language-select");

  // 言語切替時に接続状態の動的文言を再描画するため最新値を保持
  let lastStatus = null;

  // 言語セレクタを構築（対応言語はi18n.jsのSUPPORTED_LANGUAGESが真実源）
  SUPPORTED_LANGUAGES.forEach((lang) => {
    const option = document.createElement("option");
    option.value = lang.code;
    option.textContent = lang.label;
    languageSelect.appendChild(option);
  });

  const currentLanguage = await getSavedLanguage();
  languageSelect.value = currentLanguage;
  await loadMessages(currentLanguage);
  applyTranslations();

  languageSelect.addEventListener("change", async () => {
    await setLanguage(languageSelect.value);
    applyTranslations();
    if (lastStatus) {
      updateStatus(lastStatus);
    }
  });

  // Update UI based on connection status
  function updateStatus(status) {
    lastStatus = status;
    if (status.connected) {
      statusDot.classList.add("connected");
      statusText.classList.remove("disconnected");
      statusText.classList.add("connected");
      statusText.textContent = t("connected");
    } else {
      statusDot.classList.remove("connected");
      statusText.classList.remove("connected");
      statusText.classList.add("disconnected");
      statusText.textContent = t("disconnected");
    }
    serverUrl.textContent = status.url;
  }

  // Get current status from background script
  function refreshStatus() {
    chrome.runtime.sendMessage({ action: "getStatus" }, (response) => {
      if (response) {
        updateStatus(response);
      }
    });
  }

  // Initial status check
  refreshStatus();

  // Reconnect button click handler
  reconnectBtn.addEventListener("click", () => {
    reconnectBtn.disabled = true;
    reconnectBtn.textContent = t("reconnecting");

    chrome.runtime.sendMessage({ action: "reconnect" }, (response) => {
      // Wait a moment for connection attempt
      setTimeout(() => {
        refreshStatus();
        reconnectBtn.disabled = false;
        reconnectBtn.textContent = t("reconnect");
      }, 1500);
    });
  });

  // Auto-refresh status every 2 seconds while popup is open
  const refreshInterval = setInterval(refreshStatus, 2000);

  // Clean up interval when popup closes
  window.addEventListener("unload", () => {
    clearInterval(refreshInterval);
  });
});
