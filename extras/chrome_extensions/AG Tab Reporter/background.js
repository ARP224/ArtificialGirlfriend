// AG Tab Reporter - Background Service Worker
// Artificial GirlfriendにChromeタブ情報を提供する

const AG_WS_URL = "ws://localhost:5002";
// MV3のService Workerはアイドル30秒で停止されWSも切れる。20秒間隔でpingを
// 交換し続けることでアイドル判定をリセットする（Chrome 116+の公式推奨パターン。
// AG本体はping受信でpongを返す実装済み: pc_status_manager._handle_message）
const KEEPALIVE_INTERVAL_MS = 20000;
let ws = null;
let reconnectTimer = null;
let keepaliveTimer = null;
let isConnected = false;

// WebSocket接続を開始
function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    console.log("AG Tab Reporter: Already connected or connecting");
    return;
  }

  console.log("AG Tab Reporter: Connecting to", AG_WS_URL);

  try {
    ws = new WebSocket(AG_WS_URL);

    ws.onopen = () => {
      console.log("AG Tab Reporter: Connected to AG");
      isConnected = true;
      // 再接続タイマーをクリア
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      startKeepalive();
    };

    ws.onmessage = async (event) => {
      try {
        const data = JSON.parse(event.data);
        console.log("AG Tab Reporter: Received message", data.type);

        if (data.type === "request_tabs") {
          // タブ情報を要求された
          // AG本体が使うのはtitle/activeのみ。URLは機微情報(検索クエリ・
          // セッションID等)を含みうるため送信しない(プライバシー最小化)
          const tabs = await chrome.tabs.query({});
          const tabData = tabs.map((tab) => ({
            id: tab.id,
            windowId: tab.windowId,
            title: tab.title || "",
            active: tab.active,
          }));

          // レスポンスを送信
          const response = {
            type: "tabs_response",
            request_id: data.request_id,
            tabs: tabData,
          };
          ws.send(JSON.stringify(response));
          console.log("AG Tab Reporter: Sent", tabData.length, "tabs");
        } else if (data.type === "pong") {
          console.log("AG Tab Reporter: Pong received");
        }
      } catch (e) {
        console.error("AG Tab Reporter: Error handling message", e);
      }
    };

    ws.onclose = () => {
      console.log("AG Tab Reporter: Disconnected from AG");
      isConnected = false;
      ws = null;
      stopKeepalive();
      // 5秒後に再接続を試みる
      scheduleReconnect();
    };

    ws.onerror = (error) => {
      console.log("AG Tab Reporter: Connection error (AG may not be running)");
      isConnected = false;
    };
  } catch (e) {
    console.error("AG Tab Reporter: Failed to create WebSocket", e);
    scheduleReconnect();
  }
}

// keepaliveを開始（接続中のみ動作。切断時はstopKeepaliveで停止）
function startKeepalive() {
  stopKeepalive();
  keepaliveTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ping" }));
    }
  }, KEEPALIVE_INTERVAL_MS);
}

// keepaliveを停止
function stopKeepalive() {
  if (keepaliveTimer) {
    clearInterval(keepaliveTimer);
    keepaliveTimer = null;
  }
}

// 再接続をスケジュール
function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 5000);
}

// 接続状態を取得
function getConnectionStatus() {
  return {
    connected: isConnected,
    url: AG_WS_URL,
  };
}

// メッセージリスナー（popupからの要求を受ける）
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "getStatus") {
    sendResponse(getConnectionStatus());
    return true;
  } else if (request.action === "reconnect") {
    connect();
    sendResponse({ status: "reconnecting" });
    return true;
  }
});

// 拡張機能起動時に接続開始
connect();

// chrome.alarmsを使用して定期的に接続状態をチェック
// Service Workerがスリープしても、alarmsでウェイクアップされる
chrome.alarms.create("connectionCheck", { periodInMinutes: 0.5 }); // 30秒ごと

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "connectionCheck") {
    console.log("AG Tab Reporter: Alarm triggered, checking connection");
    if (!isConnected && !reconnectTimer) {
      connect();
    }
  }
});

// インストール/更新時に接続を開始
chrome.runtime.onInstalled.addListener(() => {
  console.log("AG Tab Reporter: Extension installed/updated");
  connect();
});

// Chrome起動時に接続を開始
chrome.runtime.onStartup.addListener(() => {
  console.log("AG Tab Reporter: Chrome started");
  connect();
});
