"""
backend/server/websocket_server.py

WebSocket server for real-time UI updates.
This enables hotkey-triggered updates to be pushed to the Gradio UI.
"""

import asyncio
import concurrent.futures
import websockets
import json
import threading
import socket
import time
import logging
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum

from backend.shared.i18n import t
from backend.tools import motion_pngtuber_launcher
from backend.server.ws_replay_buffer import NON_IDEMPOTENT_ACTIONS, ReplayBuffer
# B12: self-contained leaves split out of this module. Re-imported here so the
# public surface (__all__, ui.conversation's get_browser_mic_bridge import) and
# the internal StarletteWSAdapter reference in handle_starlette_ws stay intact.
from backend.server.ws_browser_mic import BrowserMicBridge, get_browser_mic_bridge
from backend.server.ws_starlette_adapter import StarletteWSAdapter

logger = logging.getLogger(__name__)


# B3 / V3 inversion: feature-toggle command specs. Maps the inbound WebSocket
# action string -> (feature, default_enabled, log_label).
# This is the *infra policy* for each toggle; the domain effect (set_*_enabled +
# persist) is dispatched via backend.shared.feature_commands and lives in
# backend.shared.feature_toggle_service. Replaces the 10 near-identical _handle_set_*
# methods (see _handle_feature_toggle). The server-mode guard moved to the
# domain layer (feature_toggle_service._SERVER_MODE_BLOCKED, 2026-07-31) so the
# rejection carries popup text + full toggle state like the availability gate.
_FEATURE_TOGGLE_SPECS = {
    "set_pc_status": ("pc_status", False, "PC Status"),
    "set_screen_capture": ("screen_capture", False, "Screen Capture"),
    "set_talk_theme": ("talk_theme", True, "Talk Theme"),
    "set_speechless": ("speechless", False, "Speechless"),
    "set_command_execution": ("command_execution", False, "Command Execution"),
    "set_notes": ("notes", False, "Notes"),
    "set_image_generation": ("image_generation", False, "Image Generation"),
    # Camera: server-mode guard lifted (Phase 3, 2026-07-16) — capture now runs
    # on the client browser camera (Live Camera provider), not server OpenCV.
    "set_camera_capture": ("camera_capture", False, "Camera Capture"),
    "set_ambient_camera": ("ambient_camera", False, "Live Camera"),
    "set_deep_search": ("deep_search", False, "Deep Search"),
    "set_elyth": ("elyth", False, "ELYTH"),
}


# AG Client Addon: inbound global-hotkey action string -> recording intent.
# Infra policy only; the domain effect is dispatched via
# backend.shared.hotkey_trigger_command (ui layer registers the handler).
_HOTKEY_TRIGGER_INTENTS = {
    "hotkey_start_recording": "start",
    "hotkey_stop_recording": "stop",
    "hotkey_toggle_recording": "toggle",
}


class UpdateAction(Enum):
    """Types of UI updates that can be triggered"""
    UPDATE_CHAT = "update_chat"
    UPDATE_STATUS = "update_status"
    EXTRACTION_STARTED = "extraction_started"
    EXTRACTION_COMPLETED = "extraction_completed"
    # Auto Prompt関連
    AUTO_PROMPT_COUNTDOWN_START = "auto_prompt_countdown_start"
    AUTO_PROMPT_COUNTDOWN_STOP = "auto_prompt_countdown_stop"
    AUTO_PROMPT_CHAT_UPDATE = "auto_prompt_chat_update"
    # TTS Audio for browser playback (MotionPNGTuber integration)
    TTS_AUDIO = "tts_audio"
    # Motion PNG Tuber control
    CHARACTER_APPEAR_RESPONSE = "character_appear_response"
    CHARACTER_APPEAR_CLOSED = "character_appear_closed"
    # Companion mode
    IMAGE_SLOT_UPDATE = "image_slot_update"
    ACTIVE_CHARACTER_UPDATE = "active_character_update"
    CONVERSATION_STATE_UPDATE = "conversation_state_update"
    IS_GENERATING_UPDATE = "is_generating_update"


@dataclass
class UpdateMessage:
    """Structure for update messages"""
    action: UpdateAction
    reason: str = ""
    data: dict = None
    
    def to_dict(self) -> dict:
        result = {
            "action": self.action.value,
            "reason": self.reason,
            "timestamp": time.time()
        }
        if self.data:
            result["data"] = self.data
        return result


@dataclass
class ClientInfo:
    """Information about a connected WebSocket client."""
    client_type: str = "unknown"   # admin|desktop|mobile|motion_pngtuber
    client_ip: str = ""
    session_mode: str = ""  # "primary", "companion", "" (set during identify)
    # Phase 3A: server-issued session token. Empty for admin / motion_pngtuber /
    # local mode — those clients are not subject to seq stamping or replay.
    session_token: str = ""


class WebSocketManager:
    """
    Manages WebSocket server and client connections for real-time updates.
    Runs in a separate thread to avoid blocking the main application.
    """
    
    def __init__(self, server_mode=False, **kwargs):
        """Initialize the WebSocket manager.

        Args:
            server_mode: If True, WebSocket is handled by FastAPI /ws endpoint
                         instead of an internal websockets server.
        """
        self.clients: Dict[websockets.WebSocketServerProtocol, ClientInfo] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.port: Optional[int] = None
        self.server = None
        self.running = False
        self._lock = threading.RLock()
        self._start_event = threading.Event()
        self.server_mode = server_mode
        # ST-F: local-mode current desktop front (last one wins)
        self._local_desktop_ws = None
        # MotionPNGTuber remote client state
        self._motion_pngtuber_ws = None        # Connected Electron client websocket
        self._motion_pngtuber_appeared = False  # Whether character is currently appeared
        self._appear_response_future = None     # Future awaiting appear response from Electron
        # Live Camera: the page currently providing frames (one at a time,
        # last announce wins; availability itself lives in ambient_camera_state)
        self._ambient_provider_ws = None
        # Phase 2.5: TTS playback completion notification
        self._playback_events: Dict[str, threading.Event] = {}
        self._playback_lock = threading.Lock()
        # B12: per-session_token resilience state (seq counters, replay ring
        # buffer, latest-value snapshots, idempotency dedup) is owned by the
        # ReplayBuffer leaf; this manager delegates to it.
        self._replay = ReplayBuffer()

    def find_free_port(self, start_port: int = 8765, max_attempts: int = 10) -> int:
        """
        Find a free port for the WebSocket server.
        
        Args:
            start_port: Port number to start searching from
            max_attempts: Maximum number of ports to try
            
        Returns:
            int: Available port number
            
        Raises:
            RuntimeError: If no free port is found
        """
        for port in range(start_port, start_port + max_attempts):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(('localhost', port))
                    logger.debug(f"Found free port: {port}")
                    return port
                except OSError:
                    logger.debug(f"Port {port} is in use, trying next...")
                    continue
        raise RuntimeError(f"No free port found in range {start_port}-{start_port + max_attempts}")
    
    def get_client_counts(self) -> Dict[str, int]:
        """
        Count connected clients per client_type (identify済みのもののみ).

        Used by the control API (tray launcher) to decide whether a front
        window is already connected before opening a new one.

        Returns:
            Dict mapping client_type ("desktop"/"admin"/"mobile"/...) to count.
        """
        counts: Dict[str, int] = {}
        with self._lock:
            for info in self.clients.values():
                if info.client_type and info.client_type != "unknown":
                    counts[info.client_type] = counts.get(info.client_type, 0) + 1
        return counts

    async def handler(self, websocket):
        """
        Handle WebSocket client connections with identify protocol.

        Flow:
        1. Register client with unknown type
        2. Send welcome message
        3. Wait for identify message (10s timeout)
        4. Session check (stub: always accepted; Phase B adds real logic)
        5. Send identify_response
        6. Enter message loop
        """
        # Get client IP safely
        client_ip = ""
        try:
            if hasattr(websocket, 'remote_address') and websocket.remote_address:
                client_ip = websocket.remote_address[0]
        except Exception:
            pass
        client_info_str = client_ip or "unknown"

        # Register with unknown type
        info = ClientInfo(client_ip=client_ip)
        with self._lock:
            self.clients[websocket] = info
            client_count = len(self.clients)

        logger.info(f"[WebSocket] Client connected from {client_info_str}. Total clients: {client_count}")

        try:
            # Send welcome message with feature status (B3.5: read the runtime
            # snapshot via the shared interface instead of the god object).
            from backend.shared.runtime_state import get_feature_status
            from backend.shared.feature_availability import get_block_reasons
            welcome = {
                "type": "connected",
                "message": "WebSocket connection established",
                "timestamp": time.time(),
                "feature_status": get_feature_status(),
                # フールプルーフ: 機能→ブロック理由文(None=利用可)。JSが
                # utility panel に disabled/横線/tooltip を適用する
                "feature_availability": get_block_reasons(),
            }
            await websocket.send(json.dumps(welcome))

            # Send initial ELYTH status
            try:
                from backend.elyth.elyth_session_manager import get_elyth_session_manager
                elyth_mgr = get_elyth_session_manager()
                elyth_status = elyth_mgr.get_status(event="initial")
                await websocket.send(json.dumps({
                    "action": "elyth_status",
                    "data": elyth_status,
                    "timestamp": time.time(),
                }))
            except Exception:
                pass

            # Wait for identify message (10 second timeout)
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                identify_data = json.loads(raw)
                if identify_data.get("type") != "identify" or "client_type" not in identify_data:
                    logger.warning(f"[WebSocket] Invalid identify from {client_info_str}: {identify_data}")
                    return
            except asyncio.TimeoutError:
                logger.warning(f"[WebSocket] Identify timeout from {client_info_str}")
                return
            except (json.JSONDecodeError, websockets.exceptions.ConnectionClosed):
                logger.warning(f"[WebSocket] Identify failed from {client_info_str}")
                return

            client_type = identify_data["client_type"]
            info.client_type = client_type
            logger.info(f"[WebSocket] Client identified as: {client_type} from {client_info_str}")

            # Track motion_pngtuber client
            if client_type == "motion_pngtuber":
                if self._motion_pngtuber_ws is not None:
                    logger.warning("[WebSocket] Another motion_pngtuber already connected, replacing")
                self._motion_pngtuber_ws = websocket

            # ST-F: local-mode "last one wins" — a new desktop front takes
            # over and the previous one gets an overlay + no-reconnect close.
            # Server mode keeps the full session arbitration below untouched.
            if not self.server_mode and client_type == "desktop":
                previous_ws = self._local_desktop_ws
                self._local_desktop_ws = websocket
                if previous_ws is not None and previous_ws is not websocket:
                    from backend.server.session_manager import (
                        CLOSE_CODE_SUPERSEDED, CLOSE_REASON_MAP,
                    )
                    logger.info("[WebSocket] Local desktop takeover: closing previous front")
                    try:
                        await previous_ws.send(json.dumps({
                            "type": "force_disconnected",
                            "message": "別のウィンドウで開かれました"
                        }))
                    except Exception:
                        pass
                    try:
                        await previous_ws.close(
                            code=CLOSE_CODE_SUPERSEDED,
                            reason=CLOSE_REASON_MAP.get(CLOSE_CODE_SUPERSEDED, ""),
                        )
                    except Exception:
                        pass

            # Session check
            session_accepted = True
            session_mode = "primary"
            session_token = ""
            # Phase 3D: identify carries session_token (for Case 1/3 matching) and
            # last_seq (last server seq the client received). Resume bundle is sent
            # below when the returned token equals the incoming one.
            incoming_session_token = identify_data.get("session_token")
            last_seq = identify_data.get("last_seq")
            if self.server_mode and client_type in ("desktop", "mobile"):
                from backend.server.session_manager import get_session_manager
                session_accepted, session_mode, session_token = get_session_manager().try_claim_session(
                    websocket, client_ip, client_type, incoming_session_token
                )

            info.session_mode = session_mode
            info.session_token = session_token

            if not session_accepted:
                await websocket.send(json.dumps({
                    "type": "identify_response",
                    "status": "blocked",
                    "message": "現在、別のデバイスが接続中です。"
                }))
                # Phase 2A: signal "do not reconnect" via close code 4001
                try:
                    from backend.server.session_manager import (
                        CLOSE_CODE_PRIMARY_OCCUPIED, CLOSE_REASON_MAP
                    )
                    await websocket.close(
                        code=CLOSE_CODE_PRIMARY_OCCUPIED,
                        reason=CLOSE_REASON_MAP.get(CLOSE_CODE_PRIMARY_OCCUPIED, "")
                    )
                except Exception:
                    pass
                return  # finally will clean up

            # Build accepted response
            identify_resp = {
                "type": "identify_response",
                "status": "accepted",
                "mode": session_mode,
                # Phase 3A: server-issued session token. JS persists it in memory
                # and sends it back on reconnect identify (Phase 3D resume).
                "session_token": session_token,
            }
            # B3.5: one runtime snapshot via the shared interface (image
            # count + character + conversation/generating state).
            from backend.shared.runtime_state import get_runtime_snapshot
            snap = get_runtime_snapshot()
            # 会話状態は全モード共通で返す(モバイルstandaloneの入力欄
            # プレースホルダー初期化に必要・companionはランプにも使用)
            identify_resp["conversation_started"] = snap["conversation_active"]
            # Add extra info for companion mode
            if session_mode == "companion":
                identify_resp["image_count"] = snap["image_count"]
                char_id = snap["active_character_id"]
                char_name = ""
                if char_id:
                    try:
                        from backend.conversation.character_manager import load_character_config
                        cfg = load_character_config(char_id)
                        char_name = cfg.get("name", "") if cfg else ""
                    except Exception:
                        pass
                identify_resp["character_id"] = char_id
                identify_resp["character_name"] = char_name
                # Send current generating state so companion can lock input if needed
                identify_resp["is_generating"] = snap["is_generating"]

            await websocket.send(json.dumps(identify_resp))

            # 起動フェーズ境目: 最初のクライアント(種別問わず)が identify
            # した時点で、以後の不達ポップアップはキューに溜めない
            # (backend/shared/popup_state.py 参照・稜裁定 2026-08-02)
            try:
                from backend.shared.popup_state import mark_client_seen
                mark_client_seen()
            except Exception:
                pass

            # Flush popups queued while no desktop client was connected
            # (startup warnings before the browser opened, errors raised while
            # tray-resident). Desktop-only: mobile never rendered these toasts.
            if client_type == "desktop":
                try:
                    from backend.shared.popup_state import drain_popups
                    for popup in drain_popups():
                        await websocket.send(json.dumps({
                            "action": "popup_notification",
                            "title": popup["title"],
                            "message": popup["message"],
                            "level": popup["level"],
                            "timestamp": time.time(),
                        }))
                except Exception:
                    logger.warning("[WebSocket] Failed to flush pending popups", exc_info=True)

            # Phase 3D: replay + snapshot delivery on resume.
            # Resume only fires when the returned token equals the incoming one
            # (i.e. Case 1 reactivation or Case 3 stale-replace, NOT Case 4 new
            # grant where the client's old token is unrelated to the new one).
            is_resume = (
                incoming_session_token
                and session_token
                and incoming_session_token == session_token
                and isinstance(last_seq, int)
            )
            if is_resume:
                try:
                    # Replay: in-order, original seq preserved (for client's monotonic check)
                    buf_snapshot = self._replay.get_replay_entries(session_token)
                    replay_sent = 0
                    for entry in buf_snapshot:
                        entry_seq = entry.get("seq") or 0
                        if entry_seq > last_seq:
                            await websocket.send(json.dumps(entry["message"]))
                            replay_sent += 1

                    # Snapshot: re-stamp with fresh seq so order is well-defined
                    # against any concurrent broadcast. Do NOT write the new seq
                    # back into _snapshot_latest — that would race with concurrent
                    # broadcasts that have already updated it with newer content.
                    snapshot_actions = self._replay.get_snapshot_actions(session_token)
                    snapshot_sent = 0
                    for action in snapshot_actions:
                        msg = self._replay.get_snapshot_message(session_token, action)
                        if msg is None:
                            continue
                        new_seq = self._replay.next_seq(session_token)
                        msg_with_new_seq = {**msg, "seq": new_seq}
                        await websocket.send(json.dumps(msg_with_new_seq))
                        snapshot_sent += 1

                    logger.info(
                        f"[Resume] Replayed {replay_sent} replay entries + "
                        f"{snapshot_sent} snapshots for token={session_token[:8]} "
                        f"(client last_seq={last_seq})"
                    )
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("[Resume] Connection closed during resume delivery")
                    return  # finally cleans up
                except Exception as e:
                    logger.warning(f"[Resume] Send failed during resume: {e}")

            # Send initial session status to admin clients
            if client_type == "admin" and self.server_mode:
                from backend.server.session_manager import get_session_manager
                sm = get_session_manager()
                status = sm.get_status()
                await websocket.send(json.dumps({
                    "type": "session_status_update",
                    "session": status["session"],
                    "timeout_minutes": status["timeout_minutes"],
                    "timestamp": time.time()
                }))

            # Listen for messages from client
            async for message in websocket:
                # Binary message → browser mic audio
                if isinstance(message, bytes):
                    await self._handle_browser_mic_audio(message)
                    continue

                # Text message → JSON action handler
                try:
                    data = json.loads(message)
                    action = data.get("action")

                    # Phase 2D: idempotency check for non-idempotent actions
                    idempotency_key = data.get("idempotency_key")
                    if idempotency_key and action in NON_IDEMPOTENT_ACTIONS:
                        if self._replay.is_duplicate(idempotency_key):
                            logger.debug(
                                f"[Idempotent] Dropped duplicate {action} "
                                f"(key={idempotency_key[:8]})"
                            )
                            continue
                        self._replay.mark_seen(idempotency_key)

                    # Handle character appear/disappear requests
                    if action == "character_appear":
                        await self._handle_character_appear(websocket)
                    elif action == "character_disappear":
                        await self._handle_character_disappear(websocket)
                    elif action == "appear_response":
                        self._handle_appear_response(data)
                    elif action == "disappear_response":
                        logger.info(f"[WebSocket] Disappear response: {data.get('success')}")
                    elif action == "character_change_error":
                        # Electron reports character change failed (missing assets)
                        self._motion_pngtuber_appeared = False
                        await self.broadcast({
                            "action": UpdateAction.CHARACTER_APPEAR_CLOSED.value,
                            "reason": "character_change_failed",
                            "message": data.get("error", ""),
                            "timestamp": time.time()
                        })
                    elif action in _FEATURE_TOGGLE_SPECS:
                        await self._handle_feature_toggle(websocket, action, data)
                    elif action == "elyth_stop_session":
                        await self._handle_elyth_stop_session(websocket)
                    elif action == "elyth_start_session":
                        await self._handle_elyth_start_session(websocket)
                    elif action == "elyth_pause_loop":
                        await self._handle_elyth_pause_loop(websocket)
                    elif action == "elyth_resume_loop":
                        await self._handle_elyth_resume_loop(websocket)
                    elif action == "elyth_toggle_character":
                        await self._handle_elyth_toggle_character(websocket, data)
                    elif action == "youtube_set_enabled":
                        await self._handle_youtube_set_enabled(websocket, data)
                    elif action == "youtube_start_session":
                        await self._handle_youtube_start_session(websocket)
                    elif action == "set_browser_mic_status":
                        self._handle_browser_mic_status(data)
                    elif action == "attach_image":
                        await self._handle_attach_image(websocket, data, info)
                    elif action == "ambient_camera_status":
                        await self._handle_ambient_camera_status(websocket, data)
                    elif action == "ambient_frame":
                        await self._handle_ambient_frame(websocket, data)
                    elif action == "update_location":
                        await self._handle_update_location(websocket, data)
                    elif action == "command_approve":
                        self._handle_command_approval("accepted")
                    elif action == "command_deny":
                        self._handle_command_approval("denied")
                    elif action == "text_prompt":
                        await self._handle_text_prompt(websocket, data)
                    elif action in _HOTKEY_TRIGGER_INTENTS:
                        await self._handle_hotkey_trigger(websocket, action)
                    elif action == "ping":
                        # App-level keepalive (AG Client Addon). Server-mode WS
                        # (FastAPI) sends no protocol pings, and a NAT path that
                        # dies silently leaves the client half-open forever —
                        # the pong lets the client detect a dead link. Same
                        # pattern as pc_status_manager's ping/pong.
                        # A client-initiated ping is also a sign of life for
                        # the session liveness monitor.
                        if self.server_mode:
                            from backend.server.session_manager import get_session_manager
                            get_session_manager().note_ws_alive(websocket)
                        await websocket.send(json.dumps({
                            "action": "pong", "timestamp": time.time()
                        }))
                    elif action == "pong":
                        # Ack for the server-initiated liveness ping
                        # (session_manager._tick). Proves the page JS is
                        # still running even in throttled background tabs.
                        if self.server_mode:
                            from backend.server.session_manager import get_session_manager
                            get_session_manager().note_ws_alive(websocket)
                    elif action == "tts_playback_completed":
                        # Phase 2.5: browser-side notification that TTS playback finished.
                        # Idempotent (Event.set() is safe to call multiple times).
                        playback_id = data.get("playback_id")
                        if playback_id:
                            self.notify_playback_completed(playback_id)

                except json.JSONDecodeError:
                    logger.warning(f"[WebSocket] Invalid JSON from {client_info_str}")
                except Exception as e:
                    logger.error(f"[WebSocket] Error processing message: {e}")

        except websockets.exceptions.ConnectionClosed:
            logger.debug(f"[WebSocket] Client {client_info_str} ({info.client_type}) disconnected normally")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling client {client_info_str}: {e}")
        finally:
            with self._lock:
                self.clients.pop(websocket, None)
                client_count = len(self.clients)
            # ST-F: clear local-front slot if this connection owned it
            if self._local_desktop_ws is websocket:
                self._local_desktop_ws = None
            # Release session if this was a desktop/mobile client (primary or companion)
            if self.server_mode and (
                info.client_type in ("desktop", "mobile") or
                info.session_mode == "companion"
            ):
                try:
                    from backend.server.session_manager import get_session_manager
                    # close code 1000 is sent only by our page JS (pagehide/
                    # beforeunload → ws.close(1000)) = intentional close. Release
                    # immediately instead of entering the 60s grace period.
                    # Anything else (1001/1006/None = network death, browser kill)
                    # keeps the grace path so transient drops can resume.
                    close_code = getattr(websocket, "close_code", None)
                    release_reason = (
                        "client_close" if close_code == 1000 else "disconnect"
                    )
                    get_session_manager().release_session(
                        websocket=websocket, reason=release_reason
                    )
                except Exception as e:
                    logger.error(f"[WebSocket] Session release error: {e}")

            # Live Camera: provider page went away — stop waiting on it
            if self._ambient_provider_ws is websocket:
                self._ambient_provider_ws = None
                try:
                    from backend.shared.ambient_camera_state import get_ambient_camera_state
                    get_ambient_camera_state().set_provider_available(False)
                except Exception:
                    pass
                logger.info("[WebSocket] Live Camera provider disconnected")

            # Handle motion_pngtuber disconnect
            if info.client_type == "motion_pngtuber" and self._motion_pngtuber_ws is websocket:
                logger.info("[WebSocket] MotionPNGTuber client disconnected")
                self._motion_pngtuber_ws = None
                # スリープ耐性: S3スリープではTCP/WSが切れるがElectronプロセスは生存する。
                # プロセスが生きている間はWS断を「一時的」とみなし appeared を維持し、
                # Electronの再接続(identify)で口パク送信を自動再開させる。本当に「閉じた」
                # 判定はプロセス死(on_electron_close)に任せる（これが無いと、復帰時の一時断で
                # 誤って閉じた扱いになり、再接続しても口パクが送られず固着しうる）。
                if self._motion_pngtuber_appeared and not motion_pngtuber_launcher.is_electron_running():
                    self._motion_pngtuber_appeared = False
                    # Notify UI clients that character is no longer appeared
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.ensure_future(self.broadcast({
                            "action": UpdateAction.CHARACTER_APPEAR_CLOSED.value,
                            "reason": "motion_pngtuber_disconnected",
                            "timestamp": time.time()
                        }), loop=loop)
                    except Exception:
                        pass
                # Cancel pending appear future
                if self._appear_response_future and not self._appear_response_future.done():
                    self._appear_response_future.cancel()

            logger.info(f"[WebSocket] Client {client_info_str} ({info.client_type}) disconnected. Total clients: {client_count}")

    async def _handle_character_appear(self, websocket):
        """
        Handle character appear request from client.

        In local mode: launches Electron via subprocess.
        In server mode: sends appear command to remote Electron client.

        Args:
            websocket: The requesting WebSocket client
        """
        try:
            # Import here to avoid circular imports
            from backend.conversation.character_manager import load_character_config
            from backend.shared.runtime_state import get_active_character_id

            # フールプルーフ層2: 可用性理由(ローカライズ済み)で入口拒否。
            # キャラ未選択/Motionフォルダ未設定/実体なしの3ケースを、UI側
            # グレーアウト(疑似機能 motion_appear)と同じ真実源で判定する
            # (稜GO 2026-08-15。旧実装は英語ハードコード文言だった)
            from backend.shared.feature_availability import (
                MOTION_APPEAR, get_block_reasons,
            )
            appear_block_reason = get_block_reasons().get(MOTION_APPEAR)
            if appear_block_reason:
                response = {
                    "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                    "success": False,
                    "message": appear_block_reason,
                    "timestamp": time.time()
                }
                await websocket.send(json.dumps(response))
                return

            # Check if a character is active (B3.5: read via shared interface)
            # (上の可用性ゲートで弾かれるはず=防御的フォールバック)
            char_id = get_active_character_id()
            if not char_id:
                response = {
                    "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                    "success": False,
                    "message": "No character loaded",
                    "timestamp": time.time()
                }
                await websocket.send(json.dumps(response))
                return

            # Load character config
            config = load_character_config(char_id)
            if not config:
                response = {
                    "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                    "success": False,
                    "message": "Failed to load character config",
                    "timestamp": time.time()
                }
                await websocket.send(json.dumps(response))
                return

            # Get motion_pngtuber_folder from character config
            motion_folder = config.get("motion_pngtuber_folder", "")

            if not motion_folder:
                response = {
                    "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                    "success": False,
                    "message": "No Motion PNG Tuber folder configured for this character",
                    "timestamp": time.time()
                }
                await websocket.send(json.dumps(response))
                return

            if self.server_mode:
                # --- Server mode: send to remote Electron ---
                await self._handle_character_appear_remote(websocket, config, motion_folder)
            else:
                # --- Local mode: launch subprocess ---
                await self._handle_character_appear_local(websocket, motion_folder)

        except Exception as e:
            logger.error(f"[WebSocket] Error handling character appear: {e}")
            response = {
                "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                "success": False,
                "message": f"Error: {str(e)}",
                "timestamp": time.time()
            }
            try:
                await websocket.send(json.dumps(response))
            except Exception:
                pass

    async def _handle_character_appear_local(self, websocket, motion_folder: str):
        """Handle character appear in local mode (subprocess launch)."""
        # Set up close callback to notify UI when Electron closes
        def on_electron_close():
            logger.info("[WebSocket] Motion PNG Tuber closed, notifying clients")
            self._motion_pngtuber_appeared = False
            self.send_update_message(
                UpdateAction.CHARACTER_APPEAR_CLOSED,
                reason="electron_process_terminated"
            )

        motion_pngtuber_launcher.set_on_close_callback(on_electron_close)

        # Launch Motion PNG Tuber
        success, message = motion_pngtuber_launcher.launch_motion_pngtuber(
            ws_port=self.port,
            character_folder=motion_folder
        )

        if success:
            self._motion_pngtuber_appeared = True

        response = {
            "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
            "success": success,
            "message": message,
            "timestamp": time.time()
        }
        await websocket.send(json.dumps(response))

        logger.info(f"[WebSocket] Character appear (local): success={success}, message={message}")

    async def _handle_character_appear_remote(self, websocket, config: dict, motion_folder: str):
        """Handle character appear in server mode (remote Electron)."""
        import os

        if self._motion_pngtuber_ws is None:
            response = {
                "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                "success": False,
                "message": t('appear.remote_not_running'),
                "timestamp": time.time()
            }
            await websocket.send(json.dumps(response))
            return

        folder_name = os.path.basename(motion_folder)
        character_name = config.get("name", "")

        # Send appear command to Electron
        try:
            await self._motion_pngtuber_ws.send(json.dumps({
                "action": "character_appear",
                "character_name": character_name,
                "folder_name": folder_name
            }))
        except Exception as e:
            logger.error(f"[WebSocket] Failed to send appear to Electron: {e}")
            response = {
                "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                "success": False,
                "message": t('appear.remote_comm_failed', error=str(e)),
                "timestamp": time.time()
            }
            await websocket.send(json.dumps(response))
            return

        # Wait for response from Electron
        future = asyncio.get_running_loop().create_future()
        self._appear_response_future = future

        try:
            data = await asyncio.wait_for(future, timeout=10.0)
            success = data.get("success", False)
            message = data.get("message", data.get("error", ""))

            if success:
                self._motion_pngtuber_appeared = True

            response = {
                "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                "success": success,
                "message": message,
                "timestamp": time.time()
            }
            await websocket.send(json.dumps(response))
            logger.info(f"[WebSocket] Character appear (remote): success={success}, message={message}")

        except asyncio.TimeoutError:
            response = {
                "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                "success": False,
                "message": t('appear.remote_timeout'),
                "timestamp": time.time()
            }
            await websocket.send(json.dumps(response))
            logger.warning("[WebSocket] Character appear remote: timeout")

        except asyncio.CancelledError:
            response = {
                "action": UpdateAction.CHARACTER_APPEAR_RESPONSE.value,
                "success": False,
                "message": t('appear.remote_disconnected'),
                "timestamp": time.time()
            }
            try:
                await websocket.send(json.dumps(response))
            except Exception:
                pass
            logger.warning("[WebSocket] Character appear remote: cancelled (Electron disconnected)")

        finally:
            self._appear_response_future = None

    async def _handle_character_disappear(self, websocket):
        """Handle character disappear request from client.

        In local mode: stops the subprocess and cleans up.
        In server mode: sends disappear command to remote Electron.
        """
        try:
            if self.server_mode:
                # --- Server mode ---
                if self._motion_pngtuber_ws:
                    try:
                        await self._motion_pngtuber_ws.send(json.dumps({
                            "action": "character_disappear"
                        }))
                    except Exception as e:
                        logger.warning(f"[WebSocket] Failed to send disappear to Electron: {e}")
            else:
                # --- Local mode ---
                # Unset callback first to prevent double notification
                motion_pngtuber_launcher.set_on_close_callback(None)
                motion_pngtuber_launcher.stop_motion_pngtuber()

            self._motion_pngtuber_appeared = False

            # Broadcast CHARACTER_APPEAR_CLOSED to all UI clients.
            # This is sufficient — no separate response is needed.
            # (Sending CHARACTER_APPEAR_RESPONSE here would cause the UI
            # to misinterpret it as "appear succeeded" and re-enable Disappear.)
            await self.broadcast({
                "action": UpdateAction.CHARACTER_APPEAR_CLOSED.value,
                "reason": "user_disappear",
                "timestamp": time.time()
            })
            logger.info("[WebSocket] Character disappear completed")

        except Exception as e:
            logger.error(f"[WebSocket] Error handling character disappear: {e}")

    def _handle_appear_response(self, data: dict):
        """Handle appear_response from Electron client.

        Sets the result on the pending future so _handle_character_appear_remote
        can continue.
        """
        if self._appear_response_future and not self._appear_response_future.done():
            self._appear_response_future.set_result(data)

    async def _handle_feature_toggle(self, websocket, action: str, data: dict):
        """Handle a feature-toggle request (consolidated, B3 / V3 inversion).

        Infra policy lives here — the default ``enabled`` value and the response
        framing (``feature_toggle_response`` + timestamp).
        The *domain* effect (flip ``backend.backend.set_<feature>_enabled`` and
        persist the choice) is dispatched to the handler registered in
        :mod:`backend.shared.feature_commands` by :mod:`backend.shared.feature_toggle_service`,
        so this transport no longer imports the spine setters directly.
        Enable gates (platform / availability / server mode) live in the domain
        handler too — their rejections carry popup text + full toggle state.

        Replaces the 10 near-identical ``_handle_set_*`` methods; behaviour is
        driven by ``_FEATURE_TOGGLE_SPECS`` so per-feature differences (default,
        log label) are preserved exactly.
        """
        feature, default_enabled, label = _FEATURE_TOGGLE_SPECS[action]
        try:
            enabled = data.get("enabled", default_enabled)

            from backend.shared.feature_commands import dispatch_feature_toggle
            result = dispatch_feature_toggle(feature, enabled)
            result["action"] = "feature_toggle_response"
            result["timestamp"] = time.time()
            await websocket.send(json.dumps(result))
            logger.info(f"[WebSocket] {label} set to {enabled}")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling {action}: {e}")
            try:
                await websocket.send(json.dumps({
                    "action": "feature_toggle_response",
                    "success": False,
                    "error": str(e),
                    "timestamp": time.time()
                }))
            except Exception:
                pass

    async def _handle_text_prompt(self, websocket, data: dict):
        """Handle a text prompt from the MotionPNGPlayer input box.

        Infra policy only: extract the text and frame the response. The domain
        effect (validate conversation state and run the headless text turn) is
        dispatched to the handler registered in
        :mod:`backend.shared.text_prompt_command` (the conversation layer
        registers it at composition-root wiring), so this transport does not
        import the ui layer. The handler returns as soon as generation has
        started; the turn's results reach clients via the normal broadcasts.
        """
        try:
            from backend.shared.text_prompt_command import dispatch_text_prompt
            text = (data.get("text") or "").strip()
            # Dispatch on a worker thread: the handler publishes UI updates and
            # send_update() waits for broadcast delivery with future.result() —
            # called on the loop thread that wait can never finish and burns
            # its full 1 s timeout before the broadcast actually runs
            # (実測: Addonホットキーで体感1秒遅延・同構造をここも持っていた).
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, dispatch_text_prompt, text)
            response = {
                "action": "text_prompt_response",
                "success": bool(result.get("success")),
                "message": result.get("message", ""),
                "timestamp": time.time(),
            }
            await websocket.send(json.dumps(response))
            logger.info(f"[WebSocket] text_prompt: success={response['success']}"
                        + (f", message={response['message']}" if not response['success'] else ""))
        except Exception as e:
            logger.error(f"[WebSocket] Error handling text_prompt: {e}")
            try:
                await websocket.send(json.dumps({
                    "action": "text_prompt_response",
                    "success": False,
                    "message": str(e),
                    "timestamp": time.time()
                }))
            except Exception:
                pass

    async def _handle_hotkey_trigger(self, websocket, action: str):
        """Handle a recording hotkey from the AG Client Addon.

        Infra policy only: map the action to an intent and frame the
        response. The domain effect (app-state checks + trigger_record_click
        broadcast) is dispatched to the handler registered in
        :mod:`backend.shared.hotkey_trigger_command` (the ui layer registers
        it at composition-root wiring), so this transport does not import
        the ui layer. Debounce (500 ms) also lives in that module.
        """
        intent = _HOTKEY_TRIGGER_INTENTS[action]
        try:
            from backend.shared.hotkey_trigger_command import dispatch_hotkey_trigger
            # Worker thread, NOT the loop thread: the handler's publish path
            # ends in send_update() which blocks on future.result(timeout=1.0);
            # on the loop thread that deadlocks until timeout and delivered the
            # click ~1 s late (稜実測 2026-07-15). run_in_executor restores the
            # designed non-async calling context (transcribe/geocodeと同型).
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, dispatch_hotkey_trigger, intent)
            response = {
                "action": "hotkey_response",
                "success": bool(result.get("success")),
                "intent": intent,
                "reason": result.get("reason", ""),
                "timestamp": time.time(),
            }
            await websocket.send(json.dumps(response))
            logger.info(
                f"[WebSocket] hotkey {intent}: success={response['success']}"
                + (f", reason={response['reason']}" if not response["success"] else "")
            )
        except Exception as e:
            logger.error(f"[WebSocket] Error handling {action}: {e}")
            try:
                await websocket.send(json.dumps({
                    "action": "hotkey_response",
                    "success": False,
                    "intent": intent,
                    "reason": str(e),
                    "timestamp": time.time()
                }))
            except Exception:
                pass

    async def _handle_elyth_toggle_character(self, websocket, data: dict):
        """Handle ELYTH character toggle (enable/disable for sessions)."""
        try:
            char_id = data.get("character_id", "")
            if not char_id:
                return

            from backend.shared.settings_store import get_setting, update_setting
            order = get_setting("elyth", "character_order", [])

            if char_id in order:
                order.remove(char_id)
                enabled = False
            else:
                # ON方向のみtools capabilityゲート(OFF方向は常に許可=解除不能
                # デッドを作らない)。古いHTMLの行クリック等のすり抜けを塞ぐ
                # 最終ゲート(稜裁定 2026-08-15。判定はelyth_availabilityが真実源)
                from backend.elyth.elyth_availability import char_block_reason
                reason = char_block_reason(char_id)
                if reason is not None:
                    from backend.shared.i18n import t
                    await websocket.send(json.dumps({
                        "action": "elyth_character_toggled",
                        "success": False,
                        "character_id": char_id,
                        "message": t(f"avail.reason_ollama_{reason}"),
                        "timestamp": time.time(),
                    }))
                    logger.info(
                        f"[WebSocket] ELYTH character {char_id} enable rejected ({reason})")
                    return
                order.append(char_id)
                enabled = True

            update_setting("elyth", "character_order", order)

            # Reload session manager settings
            try:
                from backend.elyth.elyth_session_manager import get_elyth_session_manager
                get_elyth_session_manager().reload_settings()
            except Exception:
                pass

            await websocket.send(json.dumps({
                "action": "elyth_character_toggled",
                "character_id": char_id,
                "enabled": enabled,
                "character_order": order,
                "timestamp": time.time(),
            }))
            logger.info(f"[WebSocket] ELYTH character {char_id} {'enabled' if enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"[WebSocket] Error toggling ELYTH character: {e}")

    async def _handle_elyth_stop_session(self, websocket):
        """Handle ELYTH session stop request."""
        try:
            from backend.backend import _backend_state
            _backend_state.elyth_stop_event.set()
            await websocket.send(json.dumps({
                "action": "elyth_stop_response",
                "success": True,
                "message": "Stop signal sent",
                "timestamp": time.time()
            }))
            logger.info("[WebSocket] ELYTH session stop requested")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling elyth_stop_session: {e}")

    async def _handle_elyth_start_session(self, websocket):
        """Handle manual ELYTH session start request."""
        try:
            from backend.shared.i18n import t
            from backend.elyth.elyth_session_manager import get_elyth_session_manager
            mgr = get_elyth_session_manager()
            result = mgr.start_session_manually()
            success = bool(result.get("success"))
            code = "" if success else result.get("error", "")
            await websocket.send(json.dumps({
                "action": "elyth_start_response",
                "success": success,
                "message": "" if success else t(f'elyth.err.{code}'),
                "timestamp": time.time()
            }))
            logger.info(f"[WebSocket] ELYTH manual start: {'success' if success else f'rejected ({code})'}")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling elyth_start_session: {e}")

    async def _handle_elyth_pause_loop(self, websocket):
        """Handle ELYTH auto loop pause request."""
        try:
            from backend.elyth.elyth_session_manager import get_elyth_session_manager
            mgr = get_elyth_session_manager()
            mgr.pause_loop()
            await websocket.send(json.dumps({
                "action": "elyth_loop_response",
                "paused": True,
                "timestamp": time.time()
            }))
            logger.info("[WebSocket] ELYTH loop paused")
        except Exception as e:
            logger.error(f"[WebSocket] Error pausing ELYTH loop: {e}")

    async def _handle_elyth_resume_loop(self, websocket):
        """Handle ELYTH auto loop resume request."""
        try:
            from backend.shared.i18n import t
            from backend.elyth.elyth_session_manager import get_elyth_session_manager
            mgr = get_elyth_session_manager()
            mgr.resume_loop()
            # ONにしても走れるキャラがいなければ理由をインライン警告で返す
            # (ループ自体は再開する — キャラをONにすれば次の満了から走る)
            reason = mgr.no_characters_reason()
            await websocket.send(json.dumps({
                "action": "elyth_loop_response",
                "paused": False,
                "warning": t(f'elyth.err.{reason}') if reason else "",
                "timestamp": time.time()
            }))
            logger.info("[WebSocket] ELYTH loop resumed"
                        + (f" (warning: {reason})" if reason else ""))
        except Exception as e:
            logger.error(f"[WebSocket] Error resuming ELYTH loop: {e}")

    async def _handle_youtube_set_enabled(self, websocket, data):
        """Handle the YouTube auto-reply master toggle (status-area button)."""
        try:
            from backend.youtube.youtube_session_manager import (
                get_youtube_session_manager,
            )
            enabled = bool(data.get("enabled", False))
            get_youtube_session_manager().set_enabled(enabled)
            await websocket.send(json.dumps({
                "action": "youtube_loop_response",
                "enabled": enabled,
                "timestamp": time.time()
            }))
            logger.info(f"[WebSocket] YouTube auto-reply {'enabled' if enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling youtube_set_enabled: {e}")

    async def _handle_youtube_start_session(self, websocket):
        """Handle manual YouTube session start request."""
        try:
            from backend.shared.i18n import t
            from backend.youtube.youtube_session_manager import (
                get_youtube_session_manager,
            )
            result = get_youtube_session_manager().start_session_manually()
            success = bool(result.get("success"))
            if success:
                message = t('youtube.session_started')
            else:
                code = result.get("error", "enqueue_failed")
                message = t(f'youtube.err.{code}', error=code)
            await websocket.send(json.dumps({
                "action": "youtube_start_response",
                "success": success,
                "message": message,
                "timestamp": time.time()
            }))
            logger.info(f"[WebSocket] YouTube manual start: {'success' if success else 'rejected'}")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling youtube_start_session: {e}")

    def _handle_command_approval(self, result: str):
        """Handle command approve/deny from WebSocket (bypasses Gradio queue)."""
        try:
            from backend.backend import _backend_state
            if not _backend_state or not _backend_state.command_approval_pending:
                logger.warning(f"[WebSocket] Command approval '{result}' ignored — no pending approval")
                return
            logger.info(f"[WebSocket] Command approval: {result}")
            _backend_state.command_approval_result = result
            _backend_state.command_approval_event.set()
        except Exception as e:
            logger.error(f"[WebSocket] Error handling command approval: {e}")

    async def _handle_browser_mic_audio(self, audio_bytes: bytes):
        """Receive WebM audio from browser, transcribe, submit result to bridge."""
        import tempfile
        import os

        bridge = get_browser_mic_bridge()
        logger.info(f"[BrowserMic] Received audio: {len(audio_bytes)} bytes")

        temp_path = None
        try:
            # Save to OS temp directory
            with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as f:
                f.write(audio_bytes)
                temp_path = f.name

            # Transcribe in thread pool (don't block the async event loop)
            import audio_input
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                None, audio_input.transcribe_file, temp_path
            )

            if text:
                bridge.submit_result(text)
                logger.info(f"[BrowserMic] Transcription result: '{text}'")
            else:
                from audio_input.errors import STT_NO_SPEECH
                bridge.submit_error("No speech detected in recording",
                                    code=STT_NO_SPEECH)
        except Exception as e:
            logger.error(f"[BrowserMic] Transcription failed: {e}")
            # ag_code(STTError等)はWS橋を越えてUI層のi18n照合へ引き継ぐ
            bridge.submit_error(str(e), code=getattr(e, "ag_code", None))
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _handle_browser_mic_status(self, data: dict):
        """Update browser mic availability from JS client."""
        available = data.get("available", False)
        get_browser_mic_bridge().mic_available = available
        logger.info(f"[BrowserMic] Mic available: {available}")

    async def _handle_update_location(self, websocket, data: dict):
        """Handle location update from mobile client."""
        try:
            lat = data.get("latitude")
            lng = data.get("longitude")
            if lat is None or lng is None:
                await websocket.send(json.dumps({
                    "action": "location_update_response",
                    "success": False,
                    "error": "Missing latitude/longitude",
                    "timestamp": time.time()
                }))
                return

            from backend.shared.api_settings import get_google_maps_api_key
            api_key = get_google_maps_api_key()
            if not api_key:
                await websocket.send(json.dumps({
                    "action": "location_update_response",
                    "success": False,
                    "error": "Google Maps API key not configured",
                    "timestamp": time.time()
                }))
                return

            # Resolve prompt language from the active character (STT language)
            from backend.conversation.character_manager import load_character_config
            from backend.shared.runtime_state import get_active_character_id
            from backend.shared.prompt_i18n import get_prompt_language
            char_id = get_active_character_id()
            config = load_character_config(char_id) if char_id else None
            language = get_prompt_language(config)

            # Run reverse geocoding in thread pool to avoid blocking event loop
            from backend.tools.location_manager import update_location
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, update_location, lat, lng, api_key, language)

            response = {
                "action": "location_update_response",
                "success": result.get("success", False),
                "timestamp": time.time()
            }
            if result.get("success"):
                response["address"] = result.get("address", "")
            else:
                response["error"] = result.get("error", "Unknown error")

            await websocket.send(json.dumps(response))
            logger.info(f"[WebSocket] Location update: lat={lat}, lng={lng}, success={result.get('success')}")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling update_location: {e}")
            try:
                await websocket.send(json.dumps({
                    "action": "location_update_response",
                    "success": False,
                    "error": str(e),
                    "timestamp": time.time()
                }))
            except Exception:
                pass

    async def _handle_attach_image(self, websocket, data: dict, info: ClientInfo):
        """Handle image attachment from a companion (or any) client."""
        try:
            from backend.backend import _backend_state
            if not _backend_state:
                return
            # フールプルーフ: Ollamaキャラ中は画像添付を入口で拒否(画像を
            # 送るとOllamaが生成失敗する)。JS側も送信前に通知するが、古い
            # 画面・自作クライアントからのWSコマンドはここが正で止める。
            # ドキュメント添付は別経路(Gradio)のため影響しない。
            from backend.shared.feature_availability import IMAGE_ATTACH, get_block_reasons
            block_reason = get_block_reasons().get(IMAGE_ATTACH)
            if block_reason:
                logger.info("[WebSocket] attach_image rejected: image attach unavailable (ollama)")
                from backend.shared.i18n import t
                await websocket.send(json.dumps({
                    "action": "attach_image_rejected",
                    "popup_title": t("avail.attach_popup_title"),
                    "popup_message": block_reason,
                    "timestamp": time.time(),
                }))
                return
            b64_data = data.get("image_data", "")
            content_type = data.get("content_type", "image/jpeg")
            source = info.session_mode or info.client_type
            if not b64_data:
                logger.warning("[WebSocket] attach_image: empty image_data")
                return
            count = _backend_state.image_buffer.add_image_from_base64(
                b64_data, content_type, source=source
            )
            await self._broadcast_image_slot_update(count)
        except Exception as e:
            logger.error(f"[WebSocket] Error handling attach_image: {e}")

    async def _broadcast_image_slot_update(self, count: int):
        """Broadcast attach-status (images + documents) to all clients.

        Single source of truth for the attach-status `<p>`: server reads both
        image_buffer and document_buffer counts and produces one combined text.
        Gradio Markdown updates of the same element are now suppressed (Phase 1A
        change), so this broadcast is the only writer.
        """
        from backend.shared.constants import MAX_IMAGES_PER_MESSAGE

        # B3.5: read document counts via the shared interface (returns 0,0 when
        # state is unavailable, matching the old guarded read).
        from backend.shared.runtime_state import get_document_counts
        doc_count, doc_chars = get_document_counts()

        parts = []
        if count > 0:
            parts.append(
                f"{count}/{MAX_IMAGES_PER_MESSAGE} "
                f"image{'s' if count != 1 else ''}"
            )
        if doc_count > 0:
            parts.append(
                f"{doc_count} doc{'s' if doc_count != 1 else ''} "
                f"({doc_chars:,} chars)"
            )
        text = f"\U0001f4ce {', '.join(parts)} attached" if parts else ""

        await self.broadcast({
            "action": UpdateAction.IMAGE_SLOT_UPDATE.value,
            "data": {
                "count": count,
                "max": MAX_IMAGES_PER_MESSAGE,
                "doc_count": doc_count,
                "doc_chars": doc_chars,
                "text": text,
            },
            "timestamp": time.time(),
        })

    def broadcast_image_slot_update_sync(self, count: int):
        """Broadcast image slot update from non-async context (fire-and-forget)."""
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._broadcast_image_slot_update(count),
                self.loop
            )
        except Exception:
            pass

    def broadcast_attach_rejected_sync(self, title: str, message: str):
        """添付の拒否/抽出失敗をトースト表示させる(非同期外から・fire-and-forget)。

        既存の attach_image_rejected アクションを流用: デスクトップ/モバイル
        両JSがこのアクションで showNotification を出す配線を持つため、
        ドキュメント添付の失敗通知(稜裁定 2026-08-15: 無言スキップ廃止)にも
        追加のJS変更なしで届く。文言はPython側でローカライズ済みを渡す。
        """
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": "attach_image_rejected",
                    "popup_title": title,
                    "popup_message": message,
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    async def _handle_ambient_camera_status(self, websocket, data: dict):
        """Handle Live Camera ON/OFF announcement from a provider page.

        Infra policy: remember which connection provides frames and update
        the shared state (a re-announce — WS reconnect / tab visibility —
        also resets the timeout breaker). An OFF from a connection that is
        not the current provider is ignored so a stale tab cannot kill an
        active provider. Indicator truth source: broadcast to all clients.
        """
        try:
            from backend.shared.ambient_camera_state import get_ambient_camera_state
            enabled = bool(data.get("enabled", False))
            if enabled:
                self._ambient_provider_ws = websocket
                get_ambient_camera_state().set_provider_available(True)
                await self._broadcast_ambient_display("ready")
            elif self._ambient_provider_ws is websocket or self._ambient_provider_ws is None:
                self._ambient_provider_ws = None
                get_ambient_camera_state().set_provider_available(False)
                await self._broadcast_ambient_display("off")
        except Exception as e:
            logger.error(f"[WebSocket] Error handling ambient_camera_status: {e}")

    async def _handle_ambient_frame(self, websocket, data: dict):
        """Handle one captured frame from the Live Camera provider page.

        Saves the JPEG to a temp file and hands it to the shared state.
        Frames answering a superseded request are rejected and deleted
        (stale scenery must not leak into a later turn). Accepted frames
        broadcast the indicator "attached" state with the measured
        request→attach elapsed ms.
        """
        import base64
        import os
        import tempfile
        try:
            from backend.shared.ambient_camera_state import get_ambient_camera_state
            request_id = data.get("request_id", "")
            b64_data = data.get("image_data", "")
            if not request_id or not b64_data:
                logger.warning("[WebSocket] ambient_frame: missing request_id/image_data")
                return
            try:
                image_bytes = base64.b64decode(b64_data)
            except Exception:
                logger.warning("[WebSocket] ambient_frame: base64 decode failed")
                return
            fd, temp_path = tempfile.mkstemp(suffix=".jpg", prefix="ag_ambient_")
            with os.fdopen(fd, "wb") as f:
                f.write(image_bytes)
            accepted, elapsed_ms = get_ambient_camera_state().deliver_frame(
                request_id, temp_path)
            if accepted:
                logger.info(f"[WebSocket] ambient_frame accepted: "
                            f"{len(image_bytes)} bytes, {elapsed_ms} ms")
                await self._broadcast_ambient_display("attached", elapsed_ms=elapsed_ms)
            else:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"[WebSocket] Error handling ambient_frame: {e}")

    async def _broadcast_ambient_display(self, state: str, elapsed_ms: int = None):
        """Broadcast the Live Camera indicator state to all clients.

        Single source of truth for the indicator (same principle as the
        attach-status line). Timeout/degraded states are published by
        ambient_camera_state via ui_events; ready/off/attached come from
        this transport where those facts originate.
        """
        payload = {"state": state}
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        await self.broadcast({
            "action": "ambient_camera_display",
            "data": payload,
            "timestamp": time.time(),
        })

    def send_ambient_capture_request_sync(self, request_id: str) -> bool:
        """Ask the Live Camera provider page for one frame.

        Called from worker threads via fire_capture_request (fire-and-
        forget). Returns False when there is no provider connection or the
        loop is down — the caller then releases its claim immediately.
        """
        ws = self._ambient_provider_ws
        if not ws or not self.running or not self.loop:
            return False
        from backend.shared.constants import (
            AMBIENT_FRAME_MAX_EDGE, AMBIENT_JPEG_QUALITY,
        )
        message = json.dumps({
            "action": "ambient_capture_request",
            "request_id": request_id,
            "max_edge": AMBIENT_FRAME_MAX_EDGE,
            "jpeg_quality": AMBIENT_JPEG_QUALITY,
            "timestamp": time.time(),
        })

        async def _send():
            try:
                await ws.send(message)
            except Exception as e:
                logger.warning(f"[WebSocket] ambient_capture_request send failed: {e}")

        try:
            asyncio.run_coroutine_threadsafe(_send(), self.loop)
            return True
        except Exception:
            return False

    def close_motion_pngtuber_sync(self):
        """Close the appeared MotionPNGPlayer (fire-and-forget, thread-safe).

        会話終了(Endボタン)フック(稜裁定 2026-08-01: Endのみ・キャラ切替や
        セッションタイムアウトでは閉じない)。例外(稜裁定 2026-09-19): ローカル
        モードで表示中に「Motionフォルダの無いキャラ」へ切り替えたときも閉じる
        (_notify_character_changed_local)。Disappearボタンと同一の閉鎖
        経路 _handle_character_disappear をWSループに投げる: ローカル=
        Electronプロセス停止/サーバーモード=リモートへdisappear送信、
        いずれもCHARACTER_APPEAR_CLOSED配信でUIのAppearボタンが正しく戻る。
        未表示なら何もしない。Never raises。
        """
        try:
            appeared = self._motion_pngtuber_appeared or (
                not self.server_mode
                and motion_pngtuber_launcher.is_electron_running())
            if not appeared:
                return
            if not self.running or not self.loop:
                return
            # _handle_character_disappear はwebsocket引数を使わない(broadcastのみ)
            asyncio.run_coroutine_threadsafe(
                self._handle_character_disappear(None), self.loop)
        except Exception as e:
            logger.warning(f"[WebSocket] close_motion_pngtuber_sync failed: {e}")

    def broadcast_conversation_state_sync(self, started: bool):
        """Broadcast conversation state change to all clients (fire-and-forget)."""
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": UpdateAction.CONVERSATION_STATE_UPDATE.value,
                    "data": {
                        "conversation_started": started,
                    },
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    def broadcast_generating_state_sync(self, is_generating: bool):
        """Broadcast is_generating state change to all clients (fire-and-forget).

        Phase 3B: detect turn boundary (true→false transition) BEFORE broadcasting,
        so the new turn number is in effect for any messages that follow (the
        atomic transition + per-token turn bump lives in ReplayBuffer).
        """
        # Phase 3B: turn boundary detection
        self._replay.note_turn_transition(is_generating)

        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": UpdateAction.IS_GENERATING_UPDATE.value,
                    "data": {
                        "is_generating": is_generating,
                    },
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    def broadcast_active_character_sync(self, character_id: str, character_name: str):
        """Broadcast active character change to all clients (fire-and-forget)."""
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": UpdateAction.ACTIVE_CHARACTER_UPDATE.value,
                    "data": {
                        "character_id": character_id,
                        "character_name": character_name,
                    },
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    def broadcast_talk_theme_updated_sync(self, theme: str, character_id: str):
        """Phase 4B: broadcast talk theme change (snapshot s90).

        Replaces the polling-based theme refresh with event-driven push.
        Called from update_talk_theme(), _save_theme(), and char-switch path.
        """
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": "talk_theme_updated",
                    "theme": theme,
                    "character_id": character_id,
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    def broadcast_mic_state_reset_sync(self):
        """Phase 5 Day 1: tell clients to flush their MediaRecorder state.

        Called from stop_and_transcribe_phase1 when wait_for_result hits
        TimeoutError, which means the bridge slot has just been cleared
        and the JS-side MediaRecorder may still hold a pending callback
        whose late-firing onstop would otherwise send a stale blob.

        Action: ``mic_state_reset`` (s92, discard). No payload needed.
        """
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": "mic_state_reset",
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    def send_popup_notification_sync(self, title: str, message: str, level: str) -> bool:
        """Deliver a popup toast live to connected desktop clients.

        Transport for ui.state.show_popup (replaces the gr.Timer-polled
        error_display, whose periodic tick flickered). Returns True only when
        at least one identified desktop client is connected and the broadcast
        was queued; on False the caller enqueues to backend.shared.popup_state
        and the toast is flushed when a desktop client next identifies.
        Fire-and-forget after the client check (same as the other *_sync
        broadcasts) — no result wait, so this is safe from any thread.
        """
        if not self.running or not self.loop:
            return False
        with self._lock:
            has_desktop = any(
                info.client_type == "desktop" for info in self.clients.values()
            )
        if not has_desktop:
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": "popup_notification",
                    "title": title,
                    "message": message,
                    "level": level,
                    "timestamp": time.time(),
                }),
                self.loop
            )
            return True
        except Exception:
            return False

    def send_error_notification_sync(self, message: str, level: str):
        """Phase 4D: broadcast error notification toast (discard s91).

        Called from WebSocketErrorHandler when an ERROR/CRITICAL log is
        emitted by an AG-internal logger. Fire-and-forget; failures are
        swallowed silently to prevent recursion (a broadcast send that
        triggers logger.error must not cycle back through this path).

        Server-mode-only; in local mode no clients are connected so the
        broadcast is a no-op anyway.
        """
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast({
                    "action": "error_notification",
                    "message": message,
                    "level": level,
                    "timestamp": time.time(),
                }),
                self.loop
            )
        except Exception:
            pass

    # --- Phase 3D: session lifecycle (replay state owned by ReplayBuffer) ---

    def _drop_session_buffer(self, session_token: str):
        """Drop all replay/seq/snapshot state for a session (B12 delegate).

        Public handle for SessionManager._execute_cleanup (Phase 3D Step 7),
        which reaches it via get_websocket_manager()._drop_session_buffer(). Must
        NOT be invoked while holding session_manager._lock, since ReplayBuffer
        acquires its own _buffer_lock / _seq_lock here.
        """
        self._replay.drop_session(session_token)

    # --- Phase 2.5: TTS playback completion notification ---

    def register_playback(self, playback_id: str) -> threading.Event:
        """Register a TTS playback. Caller MUST call wait_playback() afterwards
        to ensure cleanup of the Event entry (even on exception, with a
        near-zero timeout)."""
        evt = threading.Event()
        with self._playback_lock:
            self._playback_events[playback_id] = evt
        return evt

    def wait_playback(self, playback_id: str, timeout: float) -> bool:
        """Block until playback_completed received or timeout. Always pops the entry."""
        with self._playback_lock:
            evt = self._playback_events.get(playback_id)
        if evt is None:
            return False
        completed = evt.wait(timeout=timeout)
        with self._playback_lock:
            self._playback_events.pop(playback_id, None)
        return completed

    def notify_playback_completed(self, playback_id: str) -> None:
        """Mark a playback as completed (called from WS handler)."""
        with self._playback_lock:
            evt = self._playback_events.get(playback_id)
        if evt is not None:
            evt.set()

    async def broadcast(self, message: dict):
        """
        Broadcast a message to all connected clients.

        Phase 3A: split into two passes so pending_reconnect sessions still get
        their replay/snapshot state updated even though their ws is missing
        from ``self.clients``:
          1. For each active session_token (queried from SessionManager),
             stamp a seq, pre-serialize once, and call _on_outgoing() so the
             replay/snapshot buffers stay current during the grace window.
          2. For each currently-connected client, pick the pre-serialized
             string for its token (or the bare seq-less string for admin /
             motion_pngtuber / token-less clients) and send.

        Args:
            message: Message dictionary to send
        """
        # Phase 3A: collect active session_tokens (including pending_reconnect)
        active_tokens: set = set()
        if self.server_mode:
            try:
                from backend.server.session_manager import get_session_manager
                active_tokens = get_session_manager().get_active_session_tokens()
            except Exception as e:
                logger.warning(f"[Broadcast] Failed to get active tokens: {e}")

        # Pass 1: per-token seq stamping + replay/snapshot tracking
        per_token_msg_str: Dict[str, str] = {}
        for token in active_tokens:
            seq = self._replay.next_seq(token)
            msg_with_seq = {**message, "seq": seq}
            msg_str = json.dumps(msg_with_seq)
            msg_bytes = len(msg_str.encode('utf-8'))
            per_token_msg_str[token] = msg_str
            try:
                self._replay.on_outgoing(token, msg_with_seq, msg_bytes)
            except Exception as e:
                logger.error(f"[Replay] _on_outgoing failed: {e}")

        # Bare (no-seq) string for admin / motion_pngtuber / unidentified clients
        bare_msg_str = json.dumps(message)

        # Pass 2: send to currently connected clients
        if not self.clients:
            logger.debug("[WebSocket] No clients connected, skipping broadcast")
            return

        disconnected = []
        sent_count = 0
        for client, info in list(self.clients.items()):
            token = info.session_token
            if token and token in per_token_msg_str:
                msg_str = per_token_msg_str[token]
            else:
                # No token, or session ended (token not in active set): send bare.
                msg_str = bare_msg_str
            try:
                await client.send(msg_str)
                sent_count += 1
            except websockets.exceptions.ConnectionClosed:
                disconnected.append(client)
                logger.debug("[WebSocket] Client disconnected during broadcast")
            except Exception as e:
                logger.error(f"[WebSocket] Error sending to client: {e}")
                disconnected.append(client)

        # Remove disconnected clients
        if disconnected:
            with self._lock:
                for client in disconnected:
                    self.clients.pop(client, None)
                logger.info(f"[WebSocket] Removed {len(disconnected)} disconnected clients")

        if sent_count > 0:
            logger.debug(f"[WebSocket] Broadcast sent to {sent_count} clients: {message.get('action', 'unknown')}")

    async def send_to_admin(self, message: dict):
        """
        Send message only to admin clients.

        Phase 3A: admin has no session_token (early-return in try_claim_session)
        and is not subject to seq stamping or replay. Send bare (no seq) — admin
        UI re-fetches state on its own.

        Args:
            message: Message dictionary to send
        """
        message_str = json.dumps(message)
        for client, info in list(self.clients.items()):
            if info.client_type == "admin":
                try:
                    await client.send(message_str)
                except Exception:
                    pass

    def send_to_admin_sync(self, message: dict):
        """
        Send message to admin clients from non-async context (fire-and-forget).

        Args:
            message: Message dictionary to send
        """
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.send_to_admin(message),
                self.loop
            )
            # Fire-and-forget: don't call .result()
        except Exception:
            pass

    async def send_to_motion_pngtuber(self, message: dict) -> bool:
        """Send message to the connected MotionPNGTuber Electron client.

        Returns:
            bool: True if message was sent successfully
        """
        if self._motion_pngtuber_ws is None:
            return False
        try:
            await self._motion_pngtuber_ws.send(json.dumps(message))
            return True
        except Exception as e:
            logger.warning(f"[WebSocket] Failed to send to MotionPNGTuber: {e}")
            return False

    def send_to_motion_pngtuber_sync(self, message: dict):
        """Send message to MotionPNGTuber from non-async context (fire-and-forget)."""
        if not self.running or not self.loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.send_to_motion_pngtuber(message),
                self.loop
            )
        except Exception:
            pass

    def notify_character_changed(self, character_name: str, folder_name: str,
                                 motion_folder: str = ""):
        """Tell the appeared MotionPNGPlayer that the active character changed.

        Server mode: the remote player reloads from its own Asset/<folder_name>
        (only the folder name travels; the client holds its own assets).
        Local mode: see _notify_character_changed_local.

        Args:
            character_name: display name of the new character
            folder_name: basename of the character's Motion folder
            motion_folder: the configured value as-is (bare name or explicit
                path); used in local mode, where AG and the player share the disk
        """
        if not self.server_mode:
            self._notify_character_changed_local(
                character_name, motion_folder or folder_name)
            return
        if not self._motion_pngtuber_ws or not self._motion_pngtuber_appeared:
            return
        self.send_to_motion_pngtuber_sync({
            "action": "character_changed",
            "character_name": character_name,
            "folder_name": folder_name
        })

    def _notify_character_changed_local(self, character_name: str, motion_folder: str):
        """Local mode: make the shown player follow a character switch.

        稜裁定 2026-09-19: 表示中にキャラを切り替えたら表示も追随する。切替先の
        Motionフォルダが未設定/実体なしなら、Disappear と同じ経路でプレイヤーを
        閉じる(会話相手と見た目が食い違ったまま残さない)。フォルダの有効性は
        Appear の可用性ゲート(backend._feature_availability_provider)と同じ判定
        =resolve_character_folder + isdir。未表示なら何もしない。Never raises。
        """
        try:
            import os
            appeared = (self._motion_pngtuber_appeared
                        or motion_pngtuber_launcher.is_electron_running())
            if not appeared:
                return
            resolved = (motion_pngtuber_launcher.resolve_character_folder(motion_folder)
                        if motion_folder else "")
            if not resolved or not os.path.isdir(resolved):
                logger.info("[WebSocket] Active character has no usable Motion folder: "
                            "closing the player")
                self.close_motion_pngtuber_sync()
                return
            if self._motion_pngtuber_ws is None:
                # Appear 直後でプレイヤーがまだ WS に繋がっていない短い間だけ起きる。
                # 表示は前のキャラのまま残る(Disappear→Appear で直る)
                logger.warning("[WebSocket] Player not connected yet: character change "
                               "not delivered (Disappear -> Appear refreshes it)")
                return
            # 解決済みの絶対パスを送る(プレイヤーの resolveCharacterPath はパスを
            # そのまま使う)=設定値がフルパスのキャラでも同じフォルダを指す
            self.send_to_motion_pngtuber_sync({
                "action": "character_changed",
                "character_name": character_name,
                "folder_name": resolved
            })
        except Exception as e:
            logger.warning(f"[WebSocket] local character change notify failed: {e}")

    def send_update(self, message: dict) -> bool:
        """
        Send update message from non-async context.
        Thread-safe method for sending updates from hotkey handlers.
        
        Args:
            message: Message dictionary to broadcast
            
        Returns:
            bool: True if message was queued successfully
        """
        if not self.running or not self.loop:
            logger.warning("[WebSocket] Server not running, update not sent")
            return False
            
        try:
            # WSループ自身のスレッドから呼ばれた場合(WSアクションハンドラ→
            # 状態ブロードキャスト等)、result()待ちは自分のループを待つ自己
            # デッドロック=毎回1秒停止+送信欠落になるため、待たずに積むだけ。
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is self.loop:
                self.loop.create_task(self.broadcast(message))
                return True

            # Queue the broadcast in the event loop
            future = asyncio.run_coroutine_threadsafe(
                self.broadcast(message),
                self.loop
            )
            # Wait up to 1 second for completion
            future.result(timeout=1.0)
            return True
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
            # 3.10ではfuture.result()のTimeoutErrorはconcurrent.futures側の
            # 別クラス(str()が空) — asyncio側だけ捕ると汎用exceptに落ちて
            # 「Failed to send update: 」と空メッセージで誤報告される
            logger.warning("[WebSocket] Broadcast timeout")
            return False
        except Exception as e:
            logger.error(f"[WebSocket] Failed to send update: {e}")
            return False
    
    def send_update_message(self, action: UpdateAction, reason: str = "", data: dict = None) -> bool:
        """
        Convenience method to send typed update messages.
        
        Args:
            action: Type of update action
            reason: Reason for the update
            data: Optional additional data
            
        Returns:
            bool: True if message was sent successfully
        """
        message = UpdateMessage(action=action, reason=reason, data=data)
        return self.send_update(message.to_dict())
    
    async def start_server(self):
        """Start the internal WebSocket server (local mode only).

        In server mode, this method is never called — WebSocket connections
        are handled by the FastAPI /ws endpoint instead.
        """
        try:
            self.port = self.find_free_port()

            self.server = await websockets.serve(
                self.handler,
                "localhost",
                self.port,
                compression=None,  # Disable compression for lower latency
                ping_interval=20,  # Keep connection alive
                ping_timeout=10
            )
            logger.info(
                f"[WebSocket] Server started on "
                f"ws://localhost:{self.port}"
            )

            self.running = True
            self._start_event.set()  # Signal that server is ready

            # Keep server running
            await self.server.wait_closed()

        except Exception as e:
            logger.error(f"[WebSocket] Server error: {e}")
            self.running = False
            self._start_event.set()  # Signal even on error so waiting threads don't hang
            raise
    
    def run(self):
        """Run WebSocket server in a separate thread"""
        try:
            # Create new event loop for this thread
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            
            # Run server
            self.loop.run_until_complete(self.start_server())
            
        except Exception as e:
            logger.error(f"[WebSocket] Failed to run server: {e}")
        finally:
            self.running = False
            if self.loop and not self.loop.is_closed():
                self.loop.close()
            logger.info("[WebSocket] Server thread stopped")
    
    async def handle_starlette_ws(self, starlette_ws):
        """Handle a WebSocket connection from FastAPI/Starlette.

        Used in server mode where all WS traffic comes through port 7860
        via the FastAPI ``/ws`` endpoint.

        Args:
            starlette_ws: A Starlette ``WebSocket`` instance (already accepted).
        """
        # Set the event loop reference on first connection
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        adapter = StarletteWSAdapter(starlette_ws)
        await self.handler(adapter)

    def start(self) -> Optional[int]:
        """
        Start WebSocket server in background thread.

        In server mode, the internal websockets server is NOT started.
        WebSocket connections come through FastAPI's ``/ws`` endpoint instead.

        Returns:
            Optional[int]: Port number if successful, None otherwise
        """
        if self.running:
            logger.info(f"[WebSocket] Server already running on port {self.port}")
            return self.port

        if self.server_mode:
            # Server mode: no internal WS server needed.
            # Connections arrive via FastAPI /ws endpoint and are handled
            # by handle_starlette_ws(). The event loop reference (self.loop)
            # is set on first connection.
            self.port = 8765  # Nominal value (not actually bound)
            self.running = True
            self._start_event.set()
            logger.info(
                "[WebSocket] Server mode: WebSocket handled by FastAPI /ws endpoint"
            )
            return self.port

        try:
            # Reset start event
            self._start_event.clear()

            # Start server thread
            thread = threading.Thread(
                target=self.run,
                name="websocket-server",
                daemon=True
            )
            thread.start()

            # Wait for server to start (max 5 seconds)
            if self._start_event.wait(timeout=5.0):
                if self.running and self.port:
                    logger.info(f"[WebSocket] Server successfully started on port {self.port}")
                    return self.port
                else:
                    logger.error("[WebSocket] Server failed to start")
                    return None
            else:
                logger.error("[WebSocket] Server startup timeout")
                return None

        except Exception as e:
            logger.error(f"[WebSocket] Failed to start server: {e}")
            return None
    
    def stop(self):
        """Stop the WebSocket server"""
        if not self.running:
            return

        logger.info("[WebSocket] Stopping server...")
        self.running = False

        # Close internal websockets server (local mode only)
        if self.server:
            self.server.close()

        # Stop event loop (only for internal server thread; skip in server mode
        # where the loop belongs to uvicorn)
        if not self.server_mode and self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)

        # Clear clients
        with self._lock:
            self.clients.clear()

        logger.info("[WebSocket] Server stopped")


# Global instance
_ws_manager: Optional[WebSocketManager] = None
_manager_lock = threading.Lock()


def get_websocket_manager(**kwargs) -> WebSocketManager:
    """
    Get the global WebSocket manager instance (singleton).

    On the first call, creates the instance with the given kwargs.
    Subsequent calls return the existing instance (kwargs are ignored).

    Kwargs (passed to WebSocketManager.__init__ on first call):
        server_mode: If True, WS handled by FastAPI /ws endpoint.

    Returns:
        WebSocketManager: The global manager instance
    """
    global _ws_manager

    with _manager_lock:
        if _ws_manager is None:
            _ws_manager = WebSocketManager(**kwargs)

    return _ws_manager


def start_websocket_server(**kwargs) -> Optional[int]:
    """
    Start the global WebSocket server.

    Kwargs are forwarded to get_websocket_manager() for first-time
    initialization only.

    Returns:
        Optional[int]: Port number if successful, None otherwise
    """
    manager = get_websocket_manager(**kwargs)
    return manager.start()


def get_client_counts() -> Dict[str, int]:
    """Module-level accessor for the control API (see WebSocketManager.get_client_counts)."""
    return get_websocket_manager().get_client_counts()


def stop_websocket_server():
    """Stop the global WebSocket server and SessionManager."""
    # Stop SessionManager timeout thread first
    try:
        from backend.server.session_manager import get_session_manager
        get_session_manager().stop()
    except Exception:
        pass
    manager = get_websocket_manager()
    manager.stop()


def send_ui_update(action: str, reason: str = "", data: dict = None) -> bool:
    """
    Send UI update notification via WebSocket.
    
    Args:
        action: Update action type
        reason: Reason for update
        data: Optional additional data
        
    Returns:
        bool: True if sent successfully
    """
    manager = get_websocket_manager()
    
    # Convert string action to enum if possible
    try:
        action_enum = UpdateAction(action)
    except ValueError:
        # Use custom action
        message = {"action": action, "reason": reason, "timestamp": time.time()}
        if data:
            message["data"] = data
        return manager.send_update(message)
    
    return manager.send_update_message(action_enum, reason, data)


def _ui_event_subscriber(action: str, reason: str = "", data: dict = None) -> bool:
    """Deliver a published UI-update event through this transport.

    Resolves ``send_ui_update`` from module globals at call time (rather than
    capturing the function object), so test seams that monkeypatch
    ``backend.server.websocket_server.send_ui_update`` are still honoured when producers
    publish via :mod:`backend.shared.ui_events`.
    """
    return send_ui_update(action, reason, data)


# Self-register as the transport for the UI-update interface (V4 inversion):
# producers publish via backend.shared.ui_events; this module subscribes. Registration
# runs once on import (the app imports this module at start_websocket_server()).
try:
    from backend.shared.ui_events import register_ui_update_subscriber

    register_ui_update_subscriber(_ui_event_subscriber)
except Exception:  # pragma: no cover - interface module is always importable
    logger.exception("Failed to register UI-update subscriber")


# Register the domain feature-toggle handlers (V3 inversion, B3): importing the
# service self-registers handlers with backend.shared.feature_commands, so toggle
# commands published here reach backend.backend.set_*_enabled without this
# transport importing the spine setters directly. Runs once on import (the app
# imports this module at start_websocket_server()).
try:
    import backend.shared.feature_toggle_service  # noqa: F401  (import for side-effect)
except Exception:  # pragma: no cover - service module is always importable
    logger.exception("Failed to register feature-toggle handlers")


# Register the runtime-state provider (V2/V3 inversion, B3.5): importing the
# service self-registers the provider that reads backend.backend._backend_state,
# so the read snapshots above (feature status, character, conversation/document
# state) reach the god object without this transport importing it directly. Runs
# once on import (the app imports this module at start_websocket_server()).
try:
    import backend.shared.runtime_state_service  # noqa: F401  (import for side-effect)
except Exception:  # pragma: no cover - service module is always importable
    logger.exception("Failed to register runtime-state provider")


# Export main functions
__all__ = [
    'WebSocketManager',
    'ClientInfo',
    'UpdateAction',
    'BrowserMicBridge',
    'get_browser_mic_bridge',
    'get_websocket_manager',
    'start_websocket_server',
    'stop_websocket_server',
    'send_ui_update'
]