"""
backend/shared/settings_store.py

Shared settings-persistence core for Artificial Girlfriend.

This is the *shared/foundation layer* home for user-settings persistence
(directory resolution, defaults, load/save/get/update). It has no backend or
ui dependencies — only the standard library — so any layer may depend on it
downward.

History: this core used to live in ``ui/settings_manager.py``. Backend modules
imported it upward (``backend -> ui.settings_manager``), and because importing
``ui`` triggers ``ui/__init__.py -> ui.app`` (which imports backend), that was a
hard import cycle. The core was
moved here; ``ui/settings_manager.py`` now re-exports these names and
keeps only the app_state glue (``apply_settings_to_app_state`` /
``save_app_state_settings``), which belongs to the UI layer.
"""

import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
import os

logger = logging.getLogger(__name__)

# Guards read-modify-write in update_setting, the write in save_settings, AND
# plain reads in load_settings. Without it, two concurrent update_setting
# calls (load→modify→save) lose one update — and on Windows a concurrent
# reader holding the file open makes the writer's os.replace fail with
# PermissionError (WinError 5) = the write is silently lost (実機 2026-07-17:
# STTモデル選択の保存がこれで消えた). RLock: update_setting nests load/save.
_settings_lock = threading.RLock()


# Settings file location - in user's app data directory
def get_settings_dir() -> Path:
    """Get the directory for storing user settings.

    正式名称は Artificial Girlfriend（旧綴り Airtificial は誤記）。旧綴りの
    設定ディレクトリが残っていれば一度だけリネームして引き継ぐ。
    """
    if os.name == 'nt':  # Windows
        app_data = os.environ.get('APPDATA', '.')
        settings_dir = Path(app_data) / 'ArtificialGirlfriend'
        legacy_dir = Path(app_data) / 'AirtificialGirlfriend'
    else:  # Unix-like systems
        home = Path.home()
        settings_dir = home / '.config' / 'ArtificialGirlfriend'
        legacy_dir = home / '.config' / 'AirtificialGirlfriend'

    # One-time migration from the misspelled directory
    if legacy_dir.exists() and not settings_dir.exists():
        try:
            legacy_dir.rename(settings_dir)
            logger.info(f"Migrated settings dir: {legacy_dir} -> {settings_dir}")
        except OSError as e:
            logger.warning(f"Settings dir migration failed, using legacy: {e}")
            return legacy_dir

    # Create directory if it doesn't exist
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir


SETTINGS_FILE = get_settings_dir() / 'user_settings.json'

# Default settings structure
DEFAULT_SETTINGS = {
    'auto_prompt': {
        'enabled': False,
        'timer_duration': 60,
        'prompt_ja': 'これは自動送信です。これはユーザーが入力したプロンプトではありません。ユーザーは無言状態が続いています。話の続きをしたり、何か話しかけてあげてください。',
        'prompt_en': 'This is an automatic message. This is not a prompt entered by the user. The user has been silent for a while. Please continue the conversation or say something to them.'
    },
    'audio': {
        'beep_enabled': True,
        'beep_volume': 0.5,
        'tts_volume': 0.7,
        # STT engine: 'faster_whisper' (local GPU) | 'openai' (transcription API)
        'stt_engine': 'faster_whisper',
        'stt_api_model': 'whisper-1',
        # Local faster-whisper model size (audio_input.VALID_MODEL_SIZES)
        'stt_local_model': 'turbo',
        # Mic device for standalone (sounddevice) recording. Raw device name
        # ('' = system default) — IDs shift with (un)plugging, so the name is
        # resolved to an ID at each recording start.
        'input_device': ''
    },
    'display': {
        'chat_font_size': 14,
        # UI language: 'auto' = follow the OS UI language (see shared/i18n.py)
        'language': 'auto'
    },
    'hotkeys': {
        # Recording global hotkeys. key: '0'-'9' / 'a'-'z' / 'f1'-'f12'
        # (lowercase). Mac keeps these defaults until darwin key handling is
        # verified (the hotkey editor is greyed out on Mac).
        'start': {'ctrl': True, 'alt': False, 'shift': False, 'key': '1'},
        'stop': {'ctrl': True, 'alt': False, 'shift': False, 'key': '0'}
    },
    'features': {
        'pc_status_enabled': False,
        'screen_capture_enabled': False,
        'talk_theme_enabled': True,
        'speechless_enabled': False,
        'command_execution_enabled': False,
        # ゲート対象機能(GATED_FEATURES)の既定は全てOFF。notesだけONだった
        # 時代は「キャラ未選択でON+disabled=解除不能」の初回起動バグを生んだ
        # (クリーンOS実測 2026-08-01)。2026-08-02 以降キャラ未選択は
        # ブロックなし(compute_availability)なのでデッドロック自体は消滅
        # したが、既定OFFは「選んだキャラで使えない機能が最初からON」を
        # 避ける値としてそのまま維持する。
        'notes_enabled': False,
        'image_generation_enabled': False,
        'camera_capture_enabled': False,
        'ambient_camera_enabled': False,
        'deep_search_enabled': False,
        'elyth_enabled': False
    },
}


# stat キーの読み込みキャッシュ。起動時だけで数十回 load_settings が呼ばれ、
# 毎回のファイル再読込+INFOログ数十行のノイズ源だった(2026-07-18 実測40回)。
# キーにパスを含めるのはテストが SETTINGS_FILE を monkeypatch で差し替えるため。
# (-1, -1) はファイル欠損状態のキャッシュ(初回起動のログ連発も防ぐ)。
_settings_cache: Optional[Dict[str, Any]] = None
_settings_cache_key = None  # (path, st_mtime_ns, st_size) | (path, -1, -1)


def load_settings() -> Dict[str, Any]:
    """
    Load user settings from file.

    Returns:
        Dict containing user settings, or defaults if file doesn't exist.

    (パス, mtime_ns, size) が前回読込と一致すればファイルを読み直さない。
    実行中の手編集は stat の変化として検知される=従来挙動の維持。返り値は
    毎回 deepcopy(呼び手の書き換えからキャッシュを守る=「毎回新品を返す」
    従来セマンティクスと同一)。破損時はキャッシュせず毎回再試行(従来同等)。
    """
    global _settings_cache, _settings_cache_key
    # Locked: an open read handle during the writer's os.replace fails the
    # write on Windows (PermissionError) — readers must not overlap a save.
    with _settings_lock:
        try:
            if SETTINGS_FILE.exists():
                st = SETTINGS_FILE.stat()
                cache_key = (str(SETTINGS_FILE), st.st_mtime_ns, st.st_size)
                if _settings_cache is not None and _settings_cache_key == cache_key:
                    return copy.deepcopy(_settings_cache)
                # utf-8-sig: tolerate a BOM from hand edits (see launch_config.py)
                with open(SETTINGS_FILE, 'r', encoding='utf-8-sig') as f:
                    settings = json.load(f)
                logger.info(f"Loaded user settings from {SETTINGS_FILE}")

                # Merge with defaults to ensure all keys exist (deep copy to avoid mutation)
                merged_settings = copy.deepcopy(DEFAULT_SETTINGS)
                for category, values in settings.items():
                    if category in merged_settings and isinstance(values, dict):
                        merged_settings[category].update(values)
                    else:
                        merged_settings[category] = values

                _settings_cache = merged_settings
                _settings_cache_key = cache_key
                return copy.deepcopy(merged_settings)
            else:
                cache_key = (str(SETTINGS_FILE), -1, -1)
                if _settings_cache is not None and _settings_cache_key == cache_key:
                    return copy.deepcopy(_settings_cache)
                logger.info("No settings file found, using defaults")
                _settings_cache = copy.deepcopy(DEFAULT_SETTINGS)
                _settings_cache_key = cache_key
                return copy.deepcopy(DEFAULT_SETTINGS)
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings: Dict[str, Any]) -> bool:
    """
    Save user settings to file.

    Args:
        settings: Dictionary containing user settings

    Returns:
        True if successful, False otherwise
    """
    global _settings_cache, _settings_cache_key
    with _settings_lock:
        try:
            # Ensure settings directory exists
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write (tmp + os.replace): a crash mid-write must not leave a
            # half-written JSON that the next load falls back from → all settings
            # silently reverting to defaults.
            tmp_path = SETTINGS_FILE.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            # Windows: os.replace fails with PermissionError while ANY handle
            # is open on the target. In-process readers are serialized by
            # _settings_lock, but external openers (AV scan, indexer, editor)
            # can still hold one — retry briefly instead of losing the write.
            for attempt in range(10):
                try:
                    os.replace(str(tmp_path), str(SETTINGS_FILE))
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05)

            # 書込データをそのままキャッシュせず無効化のみ: load_settings の
            # defaults マージを経た形とズレる可能性を残さない(次回読込で正規化)
            _settings_cache = None
            _settings_cache_key = None

            logger.info(f"Saved user settings to {SETTINGS_FILE}")
            return True
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False


def update_setting(category: str, key: str, value: Any) -> bool:
    """
    Update a specific setting and save to file.

    Args:
        category: Settings category (e.g., 'auto_prompt', 'audio')
        key: Setting key within the category
        value: New value for the setting

    Returns:
        True if successful, False otherwise
    """
    # Hold the lock across the whole read-modify-write so concurrent updates to
    # different keys don't clobber each other (load→modify→save is not atomic).
    with _settings_lock:
        try:
            # Load current settings
            settings = load_settings()

            # Update the specific setting
            if category not in settings:
                settings[category] = {}
            settings[category][key] = value

            # Save updated settings
            return save_settings(settings)
        except Exception as e:
            logger.error(f"Failed to update setting {category}.{key}: {e}")
            return False


def delete_setting(category: str, key: str) -> bool:
    """
    Remove a specific setting key and save to file (missing key is success).

    「未編集ならキー無し(読み手が使用時にデフォルトを解決)」を表現するための
    update_setting の対。null を書く方式は get_setting がキー存在時に null を
    そのまま返すため default フォールバックが効かない — 削除が正しい表現。
    """
    with _settings_lock:
        try:
            settings = load_settings()
            if key in settings.get(category, {}):
                del settings[category][key]
                return save_settings(settings)
            return True
        except Exception as e:
            logger.error(f"Failed to delete setting {category}.{key}: {e}")
            return False


def get_setting(category: str, key: str, default: Any = None) -> Any:
    """
    Get a specific setting value.

    Args:
        category: Settings category
        key: Setting key within the category
        default: Default value if setting doesn't exist

    Returns:
        Setting value or default
    """
    try:
        settings = load_settings()
        return settings.get(category, {}).get(key, default)
    except Exception as e:
        logger.error(f"Failed to get setting {category}.{key}: {e}")
        return default
