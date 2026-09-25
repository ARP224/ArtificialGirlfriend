"""
PC Status Manager - PC状況取得機能

このモジュールは、現在のPC状況（ウィンドウ・Chromeタブ情報）を取得し、
トークテーマとして使用するための機能を提供します。

主な機能:
- Chrome拡張との通信用WebSocketサーバー（ポート5002）
- pywin32によるウィンドウ情報取得
- PC状況の整形・切り捨て処理
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set, Tuple

# NOTE: stdout/stderr のUTF-8化はプロセス全体の責務であり、合成根(ランチャーが
# PYTHONIOENCODING=utf-8 を注入)で一元化済み。ツールモジュールの import 時に
# sys.stdout/stderr をプロセス全体で貼り替えるのは越権で、§7 の stdio fd 問題と
# 干渉するため撤去した。

from backend.shared.platform_caps import IS_MAC
from backend.shared.token_manager import estimate_token_count

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

try:
    import win32gui
    import win32process
    import win32con
    PYWIN32_AVAILABLE = True
except ImportError:
    PYWIN32_AVAILABLE = False

logger = logging.getLogger(__name__)

# 旧PC Statusモード検出用の凍結マーカー。保存済み talk_theme のレガシーデータは
# 日本語でしか存在しないため、表示言語に関わらずこの日本語リテラルで判定する。
# 出力整形用のプレフィックスはカタログ側(prompt_text 'pc_status.prefix')に移設済み。
LEGACY_PC_STATUS_MARKER = "【現在のPC状況】"

# 除外するシステムウィンドウタイトル
SYSTEM_WINDOW_TITLES = [
    "Program Manager",
    "Windows Input Experience",
    "MSCTFIME UI",
    "Default IME",
    "Windows Shell Experience Host",
    "Microsoft Text Input Application",
    "Windows Default Lock Screen",
    "PopupHost",
    "Setup",
    "",  # 空のタイトルも除外
]

# Chromeウィンドウを識別するキーワード
CHROME_KEYWORDS = ["Google Chrome", "Chrome"]

# AGウィンドウを識別するキーワード（PC状況から除外）
AG_WINDOW_KEYWORDS = ["Artificial Girlfriend"]


def is_ag_window(title: str) -> bool:
    """AGのウィンドウ/タブかどうかを判定

    Args:
        title: ウィンドウまたはタブのタイトル

    Returns:
        bool: AGのウィンドウ/タブならTrue
    """
    if not title:
        return False
    for keyword in AG_WINDOW_KEYWORDS:
        if keyword in title:
            return True
    return False


@dataclass
class PCStatusResult:
    """PC状況取得結果"""
    success: bool
    active_window: str = ""
    chrome_tabs: List[Dict[str, Any]] = field(default_factory=list)
    windows: List[Dict[str, Any]] = field(default_factory=list)
    formatted_status: str = ""
    errors: List[str] = field(default_factory=list)
    chrome_connected: bool = False
    tabs_truncated: int = 0
    windows_truncated: int = 0


class PCStatusWebSocketServer:
    """Chrome拡張との通信用WebSocketサーバー"""

    def __init__(self, port: int = 5002):
        self.port = port
        self.clients: Set = set()
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.request_counter = 0
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_event: Optional[asyncio.Event] = None

    async def _handler(self, websocket):
        """WebSocketクライアント接続ハンドラ"""
        self.clients.add(websocket)
        client_info = f"{websocket.remote_address}" if hasattr(websocket, 'remote_address') else "unknown"
        logger.debug(f"[PC Status WS] Client connected: {client_info}")
        logger.debug(f"[PC Status WS] Connected clients: {len(self.clients)}")

        try:
            async for message in websocket:
                await self._handle_message(websocket, message)
        except Exception as e:
            logger.debug(f"[PC Status WS] Client disconnected: {client_info} - {e}")
        finally:
            self.clients.discard(websocket)
            logger.debug(f"[PC Status WS] Connected clients: {len(self.clients)}")

    async def _handle_message(self, websocket, message: str):
        """受信メッセージを処理"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "tabs_response":
                request_id = data.get("request_id")
                tabs = data.get("tabs", [])

                logger.info(f"[PC Status WS] Received {len(tabs)} tabs (request_id: {request_id})")

                if request_id in self.pending_requests:
                    self.pending_requests[request_id].set_result(tabs)

            elif msg_type == "ping":
                await websocket.send(json.dumps({"type": "pong"}))

        except json.JSONDecodeError as e:
            logger.error(f"[PC Status WS] JSON parse error: {e}")

    async def _request_tabs(self, timeout: float = 2.0) -> Optional[List[Dict]]:
        """Chrome拡張にタブ情報を要求"""
        if not self.clients:
            logger.debug("[PC Status WS] No connected clients")
            return None

        self.request_counter += 1
        request_id = f"req_{self.request_counter}"

        request = {
            "type": "request_tabs",
            "request_id": request_id
        }

        logger.debug(f"[PC Status WS] Requesting tabs (request_id: {request_id})")

        future = self.loop.create_future()
        self.pending_requests[request_id] = future

        # 全クライアントにリクエスト送信
        for client in self.clients.copy():
            try:
                await client.send(json.dumps(request))
            except Exception as e:
                logger.debug(f"[PC Status WS] Failed to send request: {e}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[PC Status WS] Tab request timeout ({timeout}s)")
            return None
        finally:
            self.pending_requests.pop(request_id, None)

    def request_tabs_sync(self, timeout: float = 2.0) -> Optional[List[Dict]]:
        """同期版: タブ情報を要求"""
        if self.loop is None or not self._running:
            logger.debug("[PC Status WS] Server not running")
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._request_tabs(timeout=timeout),
                self.loop
            )
            return future.result(timeout=timeout + 1.0)
        except Exception as e:
            logger.error(f"[PC Status WS] Sync request failed: {e}")
            return None

    def has_connected_clients(self) -> bool:
        """接続中のクライアントがあるかチェック"""
        return len(self.clients) > 0

    async def _start_server(self):
        """WebSocketサーバーを起動"""
        self.loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()

        try:
            self.server = await websockets.serve(
                self._handler,
                "localhost",
                self.port,
                ping_interval=20,
                ping_timeout=10
            )
            logger.info(f"[PC Status WS] Server started on ws://localhost:{self.port}")
            self._running = True

            # stop() が call_soon_threadsafe でセットするまで待機。
            # (旧実装の `await asyncio.Future()` は誰にも解決されない永久待機で、
            #  stop() 後もスレッドが残りトグルOFF→ONのたびに1本ずつ漏れていた)
            await self._stop_event.wait()
            self.server.close()
            await self.server.wait_closed()
            logger.info(f"[PC Status WS] Server on port {self.port} shut down")

        except OSError as e:
            logger.error(f"[PC Status WS] Failed to start server on port {self.port}: {e}")
            self._running = False
            raise

    def start(self) -> bool:
        """サーバーを別スレッドで起動"""
        if self._running:
            logger.warning("[PC Status WS] Server already running")
            return True

        def run_server():
            try:
                asyncio.run(self._start_server())
            except Exception as e:
                logger.error(f"[PC Status WS] Server error: {e}")
                self._running = False

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()

        # サーバー起動を待機（最大5秒）
        for _ in range(50):
            if self._running:
                return True
            time.sleep(0.1)

        logger.error("[PC Status WS] Server startup timeout")
        return False

    def stop(self) -> None:
        """サーバーを停止し、サーバースレッドの終了まで見届ける"""
        self._running = False
        if self.loop is not None and self._stop_event is not None:
            try:
                # asyncio オブジェクトはオーナーループのスレッドから触る
                self.loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass  # loop already closed
        if self._server_thread is not None:
            self._server_thread.join(timeout=5.0)
            if self._server_thread.is_alive():
                logger.warning("[PC Status WS] Server thread did not exit within 5s")
            self._server_thread = None
        logger.info("[PC Status WS] Server stopped")


# グローバルインスタンス
_pc_status_server: Optional[PCStatusWebSocketServer] = None
_server_lock = threading.Lock()


def get_pc_status_server() -> PCStatusWebSocketServer:
    """PC Status WebSocketサーバーのシングルトンインスタンスを取得"""
    global _pc_status_server

    with _server_lock:
        if _pc_status_server is None:
            _pc_status_server = PCStatusWebSocketServer(port=5002)
        return _pc_status_server


def start_pc_status_server() -> bool:
    """PC Status WebSocketサーバーを起動"""
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("[PC Status] websockets module not available")
        return False

    server = get_pc_status_server()
    return server.start()


def stop_pc_status_server() -> None:
    """PC Status WebSocketサーバーを停止"""
    global _pc_status_server

    with _server_lock:
        if _pc_status_server:
            _pc_status_server.stop()


def sanitize_title(title: str, max_length: int = 100) -> str:
    """タイトルをサニタイズして切り詰め"""
    if not title:
        return ""

    # 改行・タブを削除
    title = title.replace('\n', ' ').replace('\r', '').replace('\t', ' ')
    # 連続スペースを1つに
    title = ' '.join(title.split())
    # 長すぎる場合は切り詰め
    if len(title) > max_length:
        title = title[:max_length - 3] + "..."
    return title


def is_chrome_window(title: str) -> bool:
    """Chromeウィンドウかどうかを判定"""
    return any(kw in title for kw in CHROME_KEYWORDS)


def _get_window_info_mac() -> List[Dict[str, Any]]:
    """Quartz CGWindowListでウィンドウ情報を取得 (Mac 3-5)。

    2段縮退: 画面収録権限が無いと kCGWindowName が返らない→アプリ名のみ。
    権限があればウィンドウタイトルへ自動昇格。CGWindowList は照会のみで
    権限ダイアログを出さない(PC状況単体でダイアログを出さない設計)。
    OnScreenOnly のため最小化ウィンドウは列挙されない(is_minimized 恒偽)。
    """
    try:
        import Quartz
    except Exception as e:
        logger.warning(f"[PC Status] Quartz unavailable: {e}")
        return []

    windows = []
    try:
        try:
            from AppKit import NSWorkspace
            front_app = NSWorkspace.sharedWorkspace().frontmostApplication()
            front_pid = front_app.processIdentifier() if front_app else -1
        except Exception as e:
            logger.debug(f"[PC Status] frontmost app lookup failed: {e}")
            front_pid = -1

        info_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []

        seen = set()
        active_assigned = False
        for info in info_list:  # z順(前→後)で返る
            if info.get(Quartz.kCGWindowLayer, 0) != 0:
                continue  # メニューバー/Dock/オーバーレイ層を除外
            owner = info.get(Quartz.kCGWindowOwnerName) or ''
            title = info.get(Quartz.kCGWindowName) or ''
            display = f"{title} - {owner}" if title else owner
            if not display or display in SYSTEM_WINDOW_TITLES:
                continue
            if display in seen:
                continue  # 無権限時はアプリ名が重複する→1エントリに集約
            seen.add(display)
            is_active = ((not active_assigned)
                         and info.get(Quartz.kCGWindowOwnerPID) == front_pid)
            if is_active:
                active_assigned = True
            windows.append({
                "hwnd": int(info.get(Quartz.kCGWindowNumber, 0) or 0),
                "title": display,
                "is_minimized": False,
                "is_active": is_active,
                "is_chrome": is_chrome_window(display),
            })
    except Exception as e:
        logger.error(f"[PC Status] CGWindowList failed: {e}")
    return windows


def get_window_info() -> List[Dict[str, Any]]:
    """ウィンドウ情報を取得 (nt=pywin32 / darwin=Quartz 2段縮退)"""
    if IS_MAC:
        return _get_window_info_mac()
    if not PYWIN32_AVAILABLE:
        logger.warning("[PC Status] pywin32 module not available")
        return []

    windows = []
    active_hwnd = win32gui.GetForegroundWindow()

    def enum_callback(hwnd, _):
        try:
            # 可視ウィンドウのみ
            if not win32gui.IsWindowVisible(hwnd):
                return True

            # タイトルを取得
            title = win32gui.GetWindowText(hwnd)

            # タイトルがないウィンドウは除外
            if not title or title in SYSTEM_WINDOW_TITLES:
                return True

            # ウィンドウ情報を収集
            is_minimized = win32gui.IsIconic(hwnd)
            is_active = hwnd == active_hwnd

            windows.append({
                "hwnd": hwnd,
                "title": title,
                "is_minimized": is_minimized,
                "is_active": is_active,
                "is_chrome": is_chrome_window(title)
            })

        except Exception as e:
            logger.debug(f"[PC Status] Error enumerating window: {e}")

        return True

    try:
        win32gui.EnumWindows(enum_callback, None)
    except Exception as e:
        logger.error(f"[PC Status] EnumWindows failed: {e}")

    return windows


def get_active_window_display(chrome_tabs: List[Dict], windows: List[Dict]) -> str:
    """アクティブウィンドウの表示名を取得

    Args:
        chrome_tabs: Chromeタブのリスト（フィルタリング前のオリジナル）
        windows: ウィンドウのリスト（AGフィルタリング後）

    Returns:
        str: アクティブウィンドウの表示名。AGがアクティブの場合は空文字
    """
    active_window = next((w for w in windows if w.get("is_active")), None)

    if not active_window:
        # アクティブウィンドウがない（AGが除外された場合など）
        return ""

    title = active_window["title"]

    # Chromeがアクティブな場合、アクティブタブも表示
    if active_window.get("is_chrome") and chrome_tabs:
        active_tab = next((t for t in chrome_tabs if t.get("active")), None)
        if active_tab:
            tab_title = active_tab.get("title", "")
            # AGタブがアクティブなら空を返す
            if is_ag_window(tab_title):
                return ""
            return f"Chrome - {sanitize_title(tab_title, 40)}"

    return sanitize_title(title, 50)


def truncate_pc_status(
    chrome_tabs: List[Dict],
    non_chrome_windows: List[Dict],
    max_tokens: int = 1500,
    *,
    language: str
) -> Tuple[List[Dict], List[Dict], int, int]:
    """PC状況をトークン上限に収まるように切り捨て"""
    from backend.shared.prompt_i18n import prompt_text
    tabs_truncated = 0
    windows_truncated = 0

    # 作業用コピー
    tabs = chrome_tabs.copy()
    windows = non_chrome_windows.copy()

    def build_status_text():
        """現在の状態でステータステキストを構築（format_pc_statusと同じ文言=見積のズレ防止）"""
        lines = []
        if tabs:
            tab_titles = [f'「{sanitize_title(t.get("title", ""), 30)}」' for t in tabs]
            truncated_note = prompt_text("pc_status.truncated_tabs", language, count=tabs_truncated) if tabs_truncated > 0 else ""
            lines.append(prompt_text("pc_status.chrome_line", language, titles=' '.join(tab_titles), truncated_note=truncated_note))

        for w in windows:
            lines.append(f"- {sanitize_title(w.get('title', ''), 50)}")

        if windows_truncated > 0:
            lines.append(prompt_text("pc_status.truncated_windows", language, count=windows_truncated))

        return "\n".join(lines)

    # 切り捨てループ
    while True:
        status_text = build_status_text()
        current_tokens = estimate_token_count(status_text)

        if current_tokens <= max_tokens:
            break

        # Chromeタブから優先的に削除（非アクティブ優先）
        if tabs:
            non_active_tabs = [t for t in tabs if not t.get("active")]
            if non_active_tabs:
                tabs.remove(non_active_tabs[-1])
            else:
                tabs.pop()
            tabs_truncated += 1
            continue

        # Chromeタブがなくなったらウィンドウから削除
        if windows:
            windows.pop()
            windows_truncated += 1
            continue

        # もう削除できるものがない
        break

    return tabs, windows, tabs_truncated, windows_truncated


def format_pc_status(
    chrome_tabs: List[Dict],
    windows: List[Dict],
    active_display: str,
    tabs_truncated: int = 0,
    windows_truncated: int = 0,
    chrome_error: Optional[str] = None,
    *,
    language: str
) -> str:
    """PC状況を整形

    AGがフィルタリングされた後のデータを受け取り、整形します。
    """
    from backend.shared.prompt_i18n import prompt_text
    lines = [
        prompt_text("pc_status.prefix", language) + prompt_text("pc_status.header", language),
    ]

    # アクティブウィンドウが空でない場合のみ追加（AGがアクティブの場合は空）
    if active_display:
        lines.append(prompt_text("pc_status.active", language, display=active_display))

    # Chromeウィンドウを除外した一覧
    non_chrome_windows = [w for w in windows if not w.get("is_chrome")]

    # ウィンドウやタブが存在するかチェック
    has_chrome_content = chrome_tabs or (chrome_error and any(w.get("is_chrome") for w in windows))
    has_window_content = bool(non_chrome_windows)
    has_content = has_chrome_content or has_window_content

    if has_content:
        lines.append("")
        lines.append(prompt_text("pc_status.windows_heading", language))

        # Chromeセクション
        if chrome_tabs:
            tab_titles = [f'「{sanitize_title(t.get("title", ""))}」' for t in chrome_tabs]
            truncated_note = prompt_text("pc_status.truncated_tabs", language, count=tabs_truncated) if tabs_truncated > 0 else ""
            lines.append(prompt_text("pc_status.chrome_line", language, titles=' '.join(tab_titles), truncated_note=truncated_note))
        elif chrome_error:
            # Chromeは開いているがタブ情報は取得できなかった
            has_chrome = any(w.get("is_chrome") for w in windows)
            if has_chrome:
                lines.append(prompt_text("pc_status.chrome_tab_error", language, error=chrome_error))

        # その他のウィンドウ
        for w in non_chrome_windows:
            lines.append(f"- {sanitize_title(w.get('title', ''), 50)}")

        if windows_truncated > 0:
            lines.append(prompt_text("pc_status.truncated_windows", language, count=windows_truncated))
    else:
        # AG以外何も開いていない場合
        lines.append(prompt_text("pc_status.ask_user", language))

    return "\n".join(lines)


def get_current_pc_status(timeout: float = 2.0, max_tokens: int = 1500, *, language: str) -> PCStatusResult:
    """現在のPC状況を取得

    AGウィンドウ/タブは自動的に除外されます。
    """
    from backend.shared.prompt_i18n import prompt_text
    result = PCStatusResult(success=False)
    chrome_error: Optional[str] = None
    original_tabs = []  # フィルタリング前のタブリスト（アクティブ判定用）

    # Chrome拡張からタブ情報を取得
    server = get_pc_status_server()

    if server.has_connected_clients():
        result.chrome_connected = True
        tabs = server.request_tabs_sync(timeout=timeout)

        if tabs is not None:
            original_tabs = tabs  # オリジナルを保持
            # AGタブを除外
            result.chrome_tabs = [t for t in tabs if not is_ag_window(t.get("title", ""))]
            ag_tabs_filtered = len(tabs) - len(result.chrome_tabs)
            logger.info(f"[PC Status] Retrieved {len(result.chrome_tabs)} Chrome tabs (filtered {ag_tabs_filtered} AG tabs)")
        else:
            chrome_error = prompt_text("pc_status.tab_timeout", language)
            result.errors.append("Chrome tab request timeout")
    else:
        chrome_error = prompt_text("pc_status.not_connected", language)
        result.errors.append("Chrome extension not connected")

    # ウィンドウ情報を取得（AGも含む生のリスト）
    raw_windows = get_window_info()

    if raw_windows:
        logger.info(f"[PC Status] Retrieved {len(raw_windows)} windows (before filtering)")
    else:
        result.errors.append("Failed to get window info")

    # 情報が何も取得できなかった場合（フィルタリング前にチェック）
    if not original_tabs and not raw_windows:
        result.formatted_status = (prompt_text("pc_status.prefix", language)
                                   + prompt_text("pc_status.fetch_failed", language))
        return result

    # AGウィンドウを除外
    result.windows = [w for w in raw_windows if not is_ag_window(w.get("title", ""))]
    ag_windows_filtered = len(raw_windows) - len(result.windows)
    if ag_windows_filtered > 0:
        logger.debug(f"[PC Status] Filtered {ag_windows_filtered} AG window(s)")

    # AGタブ除外後、残りが0件ならChromeウィンドウも除外
    if len(result.chrome_tabs) == 0 and original_tabs:
        chrome_windows_before = len([w for w in result.windows if w.get("is_chrome")])
        result.windows = [w for w in result.windows if not w.get("is_chrome")]
        if chrome_windows_before > 0:
            logger.debug("[PC Status] Removed Chrome window (AG was the only tab)")

    # アクティブウィンドウを取得（オリジナルタブとフィルタリング後ウィンドウを使用）
    result.active_window = get_active_window_display(original_tabs, result.windows)

    # Chromeウィンドウを除外
    non_chrome_windows = [w for w in result.windows if not w.get("is_chrome")]

    # トークン上限に収まるように切り捨て
    truncated_tabs, truncated_windows, tabs_truncated, windows_truncated = truncate_pc_status(
        result.chrome_tabs,
        non_chrome_windows,
        max_tokens=max_tokens - 200,  # ヘッダー部分の余裕
        language=language
    )

    result.tabs_truncated = tabs_truncated
    result.windows_truncated = windows_truncated

    # 整形
    result.formatted_status = format_pc_status(
        truncated_tabs,
        result.windows,
        result.active_window,
        tabs_truncated,
        windows_truncated,
        chrome_error,
        language=language
    )

    result.success = True
    return result


def is_pc_status_mode(talk_theme: str) -> bool:
    """PC状況モードかどうかを判定（レガシーデータ検出＝言語非依存）"""
    if not talk_theme:
        return False
    return talk_theme == LEGACY_PC_STATUS_MARKER or talk_theme.startswith(LEGACY_PC_STATUS_MARKER)


def get_pc_status_for_prompt(timeout: float = 2.0, max_tokens: int = 1500, *, language: str) -> Tuple[str, List[str]]:
    """
    プロンプト構築用にPC状況を取得

    Returns:
        Tuple[str, List[str]]: (formatted_status, errors)
    """
    result = get_current_pc_status(timeout=timeout, max_tokens=max_tokens, language=language)
    return result.formatted_status, result.errors
