"""
backend/hotkey_handler.py

Global hotkey handler for Artificial Girlfriend application.
Listens for system-wide keyboard shortcuts to control voice recording.

Hotkeys are configurable (settings_store 'hotkeys' section, edited in the
Voice Input tab's shortcut editor). Defaults: Ctrl+1 (start) / Ctrl+0 (stop).
"""

import logging
import threading
import time
import re
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass
from enum import Enum

try:
    from pynput import keyboard
    from pynput.keyboard import Key, KeyCode
    PYNPUT_AVAILABLE = True
    logging.info("pynput imported successfully")
except ImportError as e:
    PYNPUT_AVAILABLE = False
    logging.warning(f"pynput not available - global hotkeys will be disabled. Error: {e}")
    keyboard = None
    Key = None
    KeyCode = None

from backend.shared.platform_caps import IS_MAC

logger = logging.getLogger(__name__)


class HotkeyAction(Enum):
    """Available hotkey actions"""
    START_RECORDING = "start_recording"
    STOP_RECORDING = "stop_recording"


@dataclass
class HotkeyConfig:
    """Configuration for a hotkey"""
    key_combination: set
    action: HotkeyAction
    description: str


# 設定で選べるキーの正準形(小文字)。UI 側の候補リストもここが真実源。
VALID_HOTKEY_KEYS = (
    [str(d) for d in range(10)]
    + [chr(c) for c in range(ord('a'), ord('z') + 1)]
    + [f'f{n}' for n in range(1, 13)]
)

# settings_store の DEFAULT_SETTINGS['hotkeys'] と同値(こちらは settings が
# 壊れていた時の最後の受け皿=ホットキーを絶対に失わない)。
DEFAULT_HOTKEYS = {
    'start': {'ctrl': True, 'alt': False, 'shift': False, 'key': '1'},
    'stop': {'ctrl': True, 'alt': False, 'shift': False, 'key': '0'},
}

# darwin(kVK_ANSI_*)の仮想キーコード表(HIToolbox Events.h)。M1実機実測4点
# (r=15 / 1=18 / 0=29 / 2=19・2026-07-25)で照合済み。darwin では char が
# 配列(US/JIS)や Option 併用で化ける(Option+R→'®' 実測)一方、vk は物理キー
# 基準で不変 — 照合は vk に寄せるのが配列非依存の正解。英字/数字は JIS でも
# kVK_ANSI 系の同一コード(JIS固有コードは¥/かな等の周辺キーのみ)。
DARWIN_VK = {
    'a': 0, 'b': 11, 'c': 8, 'd': 2, 'e': 14, 'f': 3, 'g': 5, 'h': 4,
    'i': 34, 'j': 38, 'k': 40, 'l': 37, 'm': 46, 'n': 45, 'o': 31,
    'p': 35, 'q': 12, 'r': 15, 's': 1, 't': 17, 'u': 32, 'v': 9,
    'w': 13, 'x': 7, 'y': 16, 'z': 6,
    '1': 18, '2': 19, '3': 20, '4': 21, '5': 23, '6': 22, '7': 26,
    '8': 28, '9': 25, '0': 29,
}


def normalize_hotkey_entry(entry) -> Optional[Dict[str, Any]]:
    """設定エントリを検証して正準形へ(不正は None)。

    修飾キーなしの裸キーはグローバル奪取(タイピング/ブラウザ操作を全て
    横取り)になるため不正扱い。
    """
    if not isinstance(entry, dict):
        return None
    key = str(entry.get('key', '')).lower()
    if key not in VALID_HOTKEY_KEYS:
        return None
    mods = {m: bool(entry.get(m)) for m in ('ctrl', 'alt', 'shift')}
    if not any(mods.values()):
        return None
    return {**mods, 'key': key}


def hotkey_label(entry: Dict[str, Any]) -> str:
    """人間可読表記 (例: 'Ctrl+Shift+R')。"""
    parts = [name.capitalize() for name in ('ctrl', 'alt', 'shift')
             if entry.get(name)]
    parts.append(entry['key'].upper())
    return '+'.join(parts)


def hotkeys_conflict(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """開始/停止の衝突判定: 同一キーかつ修飾が包含関係なら両方発火しうる。

    マッチングは「必要キー ⊆ 押下集合」で成立するため、完全一致だけで
    なく片方向包含(Ctrl+1 と Ctrl+Shift+1)も同時に成立してしまう。
    """
    if a['key'] != b['key']:
        return False
    a_mods = {m for m in ('ctrl', 'alt', 'shift') if a.get(m)}
    b_mods = {m for m in ('ctrl', 'alt', 'shift') if b.get(m)}
    return a_mods <= b_mods or b_mods <= a_mods


def build_hotkey_configs(hotkeys_cfg, is_mac: bool) -> "list[HotkeyConfig]":
    """設定 dict → HotkeyConfig 列(純関数・要 pynput)。

    両OSとも vk+char の二重登録で、vk 空間だけが異なる:
    - Windows: VK_*(実測: 環境により <49> 形式で届く。Shift+数字は char が
      '!' 等に化けるため vk 変種が本質)
    - darwin: DARWIN_VK(kVK_ANSI)。char は配列/Option 併用で化けるが vk は
      不変(M1実測 2026-07-25)。canonical() はキー種で挙動が非対称なため
      不使用。
    F キーは Key.fN で両OS共通・単一登録(darwin の Fn メディア層 <176> 等は
    対象外=Fnなしの標準ファンクションとして届く必要がある)。
    不正エントリは既定へ劣化する。
    """
    configs = []
    for name, action in (('start', HotkeyAction.START_RECORDING),
                         ('stop', HotkeyAction.STOP_RECORDING)):
        entry = normalize_hotkey_entry((hotkeys_cfg or {}).get(name))
        if entry is None:
            entry = DEFAULT_HOTKEYS[name]
        modifiers = set()
        if entry['ctrl']:
            modifiers.add(Key.ctrl)
        if entry['alt']:
            modifiers.add(Key.alt)
        if entry['shift']:
            modifiers.add(Key.shift)
        key = entry['key']
        verb = 'Start' if name == 'start' else 'Stop'
        desc = f"{verb} voice recording ({hotkey_label(entry)})"
        if len(key) > 1:  # 'f1'-'f12'
            variants = [getattr(Key, key)]
        elif is_mac:
            variants = [KeyCode.from_vk(DARWIN_VK[key]),
                        KeyCode.from_char(key)]
        else:
            variants = [KeyCode.from_vk(ord(key.upper())),
                        KeyCode.from_char(key)]
        for i, variant in enumerate(variants):
            configs.append(HotkeyConfig(
                key_combination=modifiers | {variant},
                action=action,
                description=desc if i == 0 else f"{desc} - char variant"
            ))
    return configs


class HotkeyHandler:
    """
    Handles global keyboard shortcuts for the application.
    
    This class runs in a separate thread and listens for keyboard combinations
    even when the application window is not focused.
    """
    
    def __init__(self):
        """Initialize the hotkey handler"""
        self.listener: Optional[keyboard.Listener] = None
        self.listener_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.current_keys = set()
        self._lock = threading.RLock()
        
        # Callback functions for actions
        self.callbacks: Dict[HotkeyAction, Optional[Callable]] = {
            HotkeyAction.START_RECORDING: None,
            HotkeyAction.STOP_RECORDING: None,
        }
        
        # Define hotkey configurations (settings-driven on both OSes;
        # vk空間の差は build_hotkey_configs が吸収)
        if PYNPUT_AVAILABLE and Key and KeyCode:
            self.hotkeys = self._build_hotkeys()
        else:
            self.hotkeys = []
            logger.warning("Global hotkeys disabled - pynput not available")
        
        # Track last action time to prevent rapid-fire
        self.last_action_time = 0
        self.min_action_interval = 0.5  # Minimum 500ms between actions

    def _build_hotkeys(self) -> "list[HotkeyConfig]":
        """settings_store の hotkeys 設定から組み立てる(取得失敗は既定)。

        darwin も設定駆動(2026-07-25 M1実測でキーイベント表現を確定し
        3層ロックを解除済み — DARWIN_VK 参照)。
        """
        cfg = None
        try:
            from backend.shared.settings_store import get_setting
            cfg = {
                'start': get_setting('hotkeys', 'start', None),
                'stop': get_setting('hotkeys', 'stop', None),
            }
        except Exception as e:
            logger.warning(f"Hotkey settings unavailable, using defaults: {e}")
        return build_hotkey_configs(cfg, IS_MAC)

    def apply_config(self) -> bool:
        """設定変更を反映してリスナーを再構築・再起動する(再起動不要の適用)。

        stop() の後も旧リスナースレッドの finally が is_running を False へ
        書き戻すレースが残るため、join で旧スレッドの終了を待ってから
        start() する。

        Returns:
            bool: 再起動に成功したら True
        """
        if not PYNPUT_AVAILABLE:
            return False
        self.stop()
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0)
        self.listener_thread = None
        with self._lock:
            self.hotkeys = self._build_hotkeys()
            self.current_keys.clear()
        return self.start()

    def _key_matches(self, key1, key2) -> bool:
        """
        Check if two keys match, handling different representations.
        
        Args:
            key1: First key to compare
            key2: Second key to compare
            
        Returns:
            bool: True if keys match
        """
        # Direct equality
        if key1 == key2:
            return True
        
        # String representation match
        if str(key1) == str(key2):
            return True
        
        # Handle keycode variations (e.g., <49> vs KeyCode.from_vk(49))
        key1_str = str(key1)
        key2_str = str(key2)
        
        # Extract numeric values from <49> format
        match1 = re.match(r'<(\d+)>', key1_str)
        match2 = re.match(r'<(\d+)>', key2_str)
        
        if match1 and match2:
            return match1.group(1) == match2.group(1)
        
        # Check if one is <49> and other is from_vk(49)
        if match1:
            vk_value = int(match1.group(1))
            try:
                if hasattr(key2, 'vk') and key2.vk == vk_value:
                    return True
            except Exception:
                pass
        
        if match2:
            vk_value = int(match2.group(1))
            try:
                if hasattr(key1, 'vk') and key1.vk == vk_value:
                    return True
            except Exception:
                pass
        
        return False
    
    def _keys_match_combination(self, current_keys: set, required_keys: set) -> bool:
        """
        Check if current keys match the required combination.
        
        Args:
            current_keys: Set of currently pressed keys
            required_keys: Set of keys required for the hotkey
            
        Returns:
            bool: True if all required keys are pressed
        """
        for required in required_keys:
            found = False
            for current in current_keys:
                if self._key_matches(current, required):
                    found = True
                    break
            if not found:
                return False
        return True
        
    def register_callback(self, action: HotkeyAction, callback: Callable) -> None:
        """
        Register a callback function for a hotkey action.
        
        Args:
            action: The action to register the callback for
            callback: The function to call when the hotkey is pressed
        """
        with self._lock:
            self.callbacks[action] = callback
            logger.info(f"Registered callback for {action.value}")
    
    @staticmethod
    def _translate_control_char(key):
        """Ctrl 併用の英字は制御文字(\\x01-\\x1a)で届く — 元の英字へ翻訳する。

        翻訳しないと Ctrl+英字 は char 変種にも current_keys の掃除にも
        永遠に一致しない(旧実装は早期 return で捨てていた=Ctrl+英字の
        ホットキーが原理的に不成立だった)。英字以外の制御文字はキー扱い
        しない(None)。press/release の両方で同じ翻訳を通し、集合の
        追加と削除を対称に保つ。
        """
        if hasattr(key, 'char') and key.char:
            code = ord(key.char)
            if 1 <= code <= 26:
                return KeyCode.from_char(chr(code + 96))
            if code < 32:
                return None
        return key

    def _on_press(self, key) -> None:
        """
        Handle key press events.

        Args:
            key: The key that was pressed
        """
        try:
            key = self._translate_control_char(key)
            if key is None:
                return

            with self._lock:
                self.current_keys.add(key)
                
                # Check if any hotkey combination is satisfied
                current_time = time.time()
                time_since_last = current_time - self.last_action_time
                
                if time_since_last < self.min_action_interval:
                    # Silently ignore rapid key presses
                    return
                else:
                    # Normalize modifier keys (left/right variants) for matching
                    normalized_keys = set()
                    for k in self.current_keys:
                        if k in (Key.ctrl_l, Key.ctrl_r):
                            normalized_keys.add(Key.ctrl)
                        elif k in (Key.alt_l, Key.alt_r, Key.alt_gr):
                            normalized_keys.add(Key.alt)
                        elif k in (Key.shift_l, Key.shift_r):
                            normalized_keys.add(Key.shift)
                        else:
                            normalized_keys.add(k)
                    
                    for hotkey in self.hotkeys:
                        # Use custom key matching
                        if self._keys_match_combination(normalized_keys, hotkey.key_combination):
                            logger.info(f"Hotkey triggered: {hotkey.action.value}")
                            self._trigger_action(hotkey.action)
                            self.last_action_time = current_time
                            # Clear current keys to prevent repeated triggers
                            self.current_keys.clear()
                            break
                            
        except Exception as e:
            logger.error(f"Error in hotkey press handler: {e}", exc_info=True)
    
    def _on_release(self, key) -> None:
        """
        Handle key release events.

        Args:
            key: The key that was released
        """
        try:
            # press と同じ翻訳を通す(制御文字のまま discard しても集合内の
            # 翻訳済みキーに一致しない)。旧実装の Ctrl 解放時 '\x..' 掃除は
            # 翻訳導入で到達不能になったため削除。
            key = self._translate_control_char(key)
            if key is None:
                return
            with self._lock:
                self.current_keys.discard(key)
        except Exception as e:
            logger.error(f"Error in hotkey release handler: {e}", exc_info=True)
    
    def _trigger_action(self, action: HotkeyAction) -> None:
        """
        Trigger the callback for a hotkey action.
        
        Args:
            action: The action to trigger
        """
        callback = self.callbacks.get(action)
        
        if callback:
            try:
                # Run callback in a separate thread to avoid blocking
                thread = threading.Thread(
                    target=callback,
                    name=f"hotkey-{action.value}",
                    daemon=True
                )
                thread.start()
            except Exception as e:
                logger.error(f"Error triggering hotkey callback for {action.value}: {e}")
        else:
            logger.warning(f"No callback registered for hotkey action: {action.value}")
    
    def start(self) -> bool:
        """
        Start listening for global hotkeys.
        
        Returns:
            bool: True if started successfully, False otherwise
        """
        if not PYNPUT_AVAILABLE:
            logger.error("Cannot start hotkey handler - pynput not available")
            return False
            
        if self.is_running:
            return True
        
        try:
            with self._lock:
                self.listener = keyboard.Listener(
                    on_press=self._on_press,
                    on_release=self._on_release
                )
                
                self.listener_thread = threading.Thread(
                    target=self._run_listener,
                    name="hotkey-listener",
                    daemon=True
                )
                
                self.is_running = True
                self.listener_thread.start()

                descs = list(dict.fromkeys(
                    h.description.replace(' - char variant', '')
                    for h in self.hotkeys))
                logger.info("Global hotkey handler started - " + "; ".join(descs))
                if IS_MAC:
                    # Preflight tie-in (Mac 3-4): a denied Input Monitoring
                    # permission means the listener runs but never receives
                    # keys — make the silent failure visible in the log.
                    try:
                        from backend.shared.mac_permissions import _check_input_monitoring
                        if _check_input_monitoring() != 'granted':
                            logger.warning(
                                "[TCC] Input Monitoring not granted - global hotkeys "
                                "will not receive keys (System Settings > Privacy & Security)")
                    except Exception:
                        pass
                return True
                
        except Exception as e:
            logger.error(f"Failed to start hotkey handler: {e}", exc_info=True)
            self.is_running = False
            return False
    
    def _run_listener(self) -> None:
        """Run the keyboard listener in a separate thread"""
        try:
            self.listener.start()
            self.listener.join()
        except Exception as e:
            logger.error(f"Keyboard listener error: {e}")
        finally:
            self.is_running = False
    
    def stop(self) -> None:
        """Stop listening for global hotkeys"""
        if not self.is_running:
            return
            
        logger.info("Stopping hotkey handler...")
        
        with self._lock:
            self.is_running = False
            
            if self.listener:
                try:
                    self.listener.stop()
                except Exception as e:
                    logger.error(f"Error stopping keyboard listener: {e}")
                self.listener = None
            
            self.current_keys.clear()
            
        logger.info("Hotkey handler stopped")
    
    def is_active(self) -> bool:
        """
        Check if the hotkey handler is currently active.
        
        Returns:
            bool: True if active, False otherwise
        """
        return self.is_running and self.listener is not None
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get detailed status information about the hotkey handler.
        
        Returns:
            Dict containing status information
        """
        status = {
            'pynput_available': PYNPUT_AVAILABLE,
            'is_running': self.is_running,
            'listener_active': self.listener is not None,
            'listener_thread_alive': self.listener_thread.is_alive() if self.listener_thread else False,
            'registered_hotkeys': len(self.hotkeys),
            'registered_callbacks': sum(1 for cb in self.callbacks.values() if cb is not None),
            'current_keys_held': len(self.current_keys),
            'last_action_time': self.last_action_time,
            'hotkey_details': []
        }
        
        # Add hotkey details
        for hotkey in self.hotkeys:
            status['hotkey_details'].append({
                'action': hotkey.action.value,
                'description': hotkey.description,
                'keys': [str(k) for k in hotkey.key_combination],
                'has_callback': self.callbacks.get(hotkey.action) is not None
            })
        
        return status


# Global instance
_hotkey_handler: Optional[HotkeyHandler] = None


def get_hotkey_handler() -> HotkeyHandler:
    """
    Get the global hotkey handler instance.
    
    Returns:
        HotkeyHandler: The global hotkey handler
    """
    global _hotkey_handler
    if _hotkey_handler is None:
        _hotkey_handler = HotkeyHandler()
    return _hotkey_handler
