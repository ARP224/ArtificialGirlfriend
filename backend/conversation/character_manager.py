# character_manager.py

import os
import json
import uuid
import time
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple

from backend.llm.ollama_integration import list_ollama_models as get_ollama_models
from backend.shared.backend_utils import validate_character_id
from backend.shared.constants import (
    BASE_DIR,
    ATTACHMENTS_DIR,
    CHARACTER_CONFIGS_DIR_STR as CHARACTER_CONFIGS_DIR,
    MEMORY_DIR_STR as MEMORY_DIR,
    CHARACTER_ICONS_DIR_STR as CHARACTER_ICONS_DIR,
    TTS_MODELS_DIR_STR as TTS_MODELS_DIR,
    VALID_CONFIG_EXTENSIONS,
    DEFAULT_CONFIG_EXTENSION as CONFIG_EXTENSION,
    VALID_IMAGE_EXTENSIONS,
    OLLAMA_GENERATION_TIMEOUT,
    resolve_data_path,
    to_repo_relative,
)

logger = logging.getLogger(__name__)

# Check for YAML support
YAML_AVAILABLE = False
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    logger.warning("PyYAML not available. YAML config files will not be supported.")

# Note: Active character state is now managed by backend.py's BackendState class
# This module only handles character configuration and file operations

# remove_character の拒否コード(locales の err.<code> と対応・UI が翻訳して
# トーストにする。命名は既存の err.start_* 一族に合わせて err.<動作>_<条件>)
DELETE_ELYTH_ACTIVE_CODE = "delete_elyth_active"
DELETE_MEMORY_TASK_CODE = "delete_memory_task"

# Cache for file operations
_character_file_cache: Dict[str, Optional[Path]] = {}
_character_config_cache: Dict[str, Dict[str, Any]] = {}
_last_cache_update: float = 0
_CACHE_TTL = 5.0  # seconds - cache lifetime
_MAX_CACHE_SIZE = 100  # Maximum number of cached characters

# Thread safety
_character_lock = threading.RLock()

# File-level locks for concurrent access protection
_file_locks: Dict[str, threading.RLock] = {}
_file_locks_lock = threading.Lock()
_last_lock_cleanup = time.time()
_LOCK_CLEANUP_INTERVAL = 300  # Cleanup every 5 minutes

def _cleanup_stale_locks() -> None:
    """Remove locks for non-existent files to prevent memory leak."""
    global _last_lock_cleanup
    
    current_time = time.time()
    if current_time - _last_lock_cleanup < _LOCK_CLEANUP_INTERVAL:
        return  # Not time for cleanup yet
    
    with _file_locks_lock:
        # Find locks for files that no longer exist
        to_remove = []
        for file_path in _file_locks:
            if not Path(file_path).exists():
                to_remove.append(file_path)
        
        # Remove stale locks
        for file_path in to_remove:
            del _file_locks[file_path]
            logger.debug(f"Removed stale lock for non-existent file: {file_path}")
        
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} stale file locks")
        
        _last_lock_cleanup = current_time


def _get_file_lock(file_path: str, timeout: float = 5.0) -> threading.RLock:
    """Get or create a lock for a specific file path with timeout support.
    
    Args:
        file_path: Path to the file needing locking
        timeout: Maximum time to wait for lock acquisition in seconds
        
    Returns:
        threading.RLock: The acquired lock
        
    Raises:
        TimeoutError: If lock cannot be acquired within timeout
    """
    # Opportunistic cleanup (non-blocking)
    _cleanup_stale_locks()
    
    with _file_locks_lock:
        if file_path not in _file_locks:
            _file_locks[file_path] = threading.RLock()
        lock = _file_locks[file_path]
    
    # Try to acquire the lock with timeout
    acquired = lock.acquire(timeout=timeout)
    if not acquired:
        raise TimeoutError(f"Could not acquire lock for {file_path} within {timeout}s")
    
    return lock


def _ensure_directory_exists(directory_path: str) -> bool:
    """
    Helper function to ensure a directory exists, creating it if needed.

    Args:
        directory_path: Path to the directory to check/create

    Returns:
        bool: True if directory exists or was created, False on error

    Raises:
        ValueError: If directory_path is None
    """
    if directory_path is None:
        raise ValueError("Directory path cannot be None")
    
    try:
        if not os.path.exists(directory_path):
            os.makedirs(directory_path, exist_ok=True)
        return True
    except OSError as e:
        logger.error(f"Failed to create directory {directory_path}: {e}")
        return False


def _get_config_filepath(character_id: str) -> str:
    """
    Internal helper to build the full path of a character config file,
    given its unique ID.

    Args:
        character_id: String identifier for the character

    Returns:
        str: Full path to the character's config file
    """
    # Normalize to lowercase
    character_id = character_id.strip().lower()
    filename = f"{character_id}{CONFIG_EXTENSION}"
    return os.path.join(CHARACTER_CONFIGS_DIR, filename)


def _get_config_files() -> Tuple[List[Path], List[str]]:
    """
    Helper to list all .json/.yaml config files in CHARACTER_CONFIGS_DIR.
    Creates the directory if it doesn't exist.

    Returns:
        Tuple[List[Path], List[str]]: (config_files, errors)
            - config_files: List of Path objects to valid configuration files
            - errors: List of error messages encountered during scan
    """
    errors = []
    
    if not _ensure_directory_exists(CHARACTER_CONFIGS_DIR):
        errors.append(f"Failed to create/access character configs directory: {CHARACTER_CONFIGS_DIR}")
        return [], errors

    config_dir = Path(CHARACTER_CONFIGS_DIR)
    files = []
    
    try:
        for file in config_dir.iterdir():
            try:
                if file.is_file() and file.suffix.lower() in VALID_CONFIG_EXTENSIONS:
                    # Skip YAML files if YAML support is not available
                    if file.suffix.lower() in (".yaml", ".yml") and not YAML_AVAILABLE:
                        errors.append(f"Skipping {file.name}: YAML support not available (install PyYAML)")
                        continue
                    files.append(file)
            except OSError as e:
                errors.append(f"Error accessing file {file.name}: {e}")
                logger.warning(f"Error accessing config file {file}: {e}")
    except OSError as e:
        errors.append(f"Error scanning config directory: {e}")
        logger.error(f"Error scanning config directory {config_dir}: {e}")
        
    return files, errors


# Character-config keys that hold filesystem paths. On disk they are stored
# repo-root-relative (portable across clones/moves); in memory they are always
# absolute so every consumer keeps seeing the same shape as before.
# NOTE: motion_pngtuber_folder is NOT a path key since 2026-07-12 (J2): it holds
# the bare asset-folder name under MotionPNGPlayer/Asset/ — absolutizing it here
# would turn the name into a bogus path.
_CONFIG_PATH_KEYS = ("db_file_path", "icon_path")
_TTS_CONFIG_PATH_KEYS = ("model_path", "config_path", "style_vectors_path")


def _map_config_paths(data: Dict[str, Any], mapper) -> Dict[str, Any]:
    """Return a copy of `data` with every known path field passed through
    `mapper` (resolve_data_path on load / to_repo_relative on save).
    Non-dict / non-string shapes are left untouched."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for key in _CONFIG_PATH_KEYS:
        if isinstance(out.get(key), str):
            out[key] = mapper(out[key])
    tts = out.get("tts_model_config")
    if isinstance(tts, dict):
        new_tts = dict(tts)
        for key in _TTS_CONFIG_PATH_KEYS:
            if isinstance(new_tts.get(key), str):
                new_tts[key] = mapper(new_tts[key])
        out["tts_model_config"] = new_tts
    return out


def _load_config_file(path: Path) -> Dict[str, Any]:
    """
    Load a config file, supporting both JSON and YAML formats.

    Args:
        path: Path to the config file

    Returns:
        Dict[str, Any]: The loaded configuration data
        (path fields resolved to absolute via resolve_data_path)

    Raises:
        ValueError: If file format is not supported or YAML support missing
        IOError: If file cannot be read
    """
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            if not YAML_AVAILABLE:
                raise ValueError(f"Cannot load YAML file {path.name}: PyYAML is not installed. "
                               "Install it with 'pip install pyyaml' or convert the file to JSON.")
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        else:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        return _map_config_paths(data, resolve_data_path)
    except Exception as e:
        logger.error(f"Failed to load config file {path}: {e}")
        raise


def _save_config_file(path: Path, data: Dict[str, Any]) -> None:
    """
    Save a config file atomically, supporting both JSON and YAML formats.

    Args:
        path: Path to the config file
        data: Dictionary containing configuration data to save

    Raises:
        ValueError: If trying to save YAML without PyYAML installed
        IOError: If the file cannot be written
    """
    # Check YAML support before attempting to save
    if path.suffix.lower() in (".yaml", ".yml") and not YAML_AVAILABLE:
        raise ValueError(f"Cannot save YAML file {path.name}: PyYAML is not installed. "
                        "Install it with 'pip install pyyaml' or save as JSON instead.")

    # Persist path fields repo-relative (portable); in-memory dict is untouched
    data = _map_config_paths(data, to_repo_relative)
    
    # Create temporary file in the same directory for atomic write
    temp_fd = None
    temp_path = None
    
    try:
        # Create temp file in same directory to ensure same filesystem
        temp_fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp"
        )
        
        # Write to temp file. fdopen takes ownership of temp_fd immediately, so
        # null it BEFORE the dump can raise — otherwise the except's os.close
        # double-closes (EBADF), masking the real error and skipping temp cleanup.
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
            temp_fd = None  # fd now owned by f; the with-block closes it
            if path.suffix.lower() in (".yaml", ".yml"):
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            else:
                json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Atomic rename (on POSIX systems)
        os.replace(temp_path, str(path))
        temp_path = None  # Successfully renamed
        
    except Exception as e:
        logger.error(f"Failed to save config file {path}: {e}")
        # Clean up temp file if it exists
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
        raise


def _get_or_refresh_cache() -> Tuple[bool, List[str]]:
    """
    Check if the cache needs refreshing, and refresh it if needed.

    Returns:
        Tuple[bool, List[str]]: (was_refreshed, errors)
            - was_refreshed: True if cache was refreshed, False if it was still valid
            - errors: List of error messages encountered during refresh
    """
    global _character_file_cache, _character_config_cache, _last_cache_update

    # Hold _character_lock across the whole check + clear + rebuild: otherwise a
    # TTL-driven refresh (clear→rebuild) races with a concurrent reader's
    # "in cache" check-then-get and raises KeyError / iterates during clear.
    # RLock is reentrant, so nested acquisitions by callers are safe.
    with _character_lock:
        current_time = time.time()
        cache_expired = current_time - _last_cache_update > _CACHE_TTL

        # If cache is still valid AND both caches have data, don't refresh
        if not cache_expired and _character_file_cache and _character_config_cache:
            # Validate cache integrity
            if _validate_cache_integrity():
                return False, []
            else:
                logger.warning("Cache integrity check failed, forcing refresh")

        errors = []

        # Clear existing cache
        _character_file_cache.clear()
        _character_config_cache.clear()

        # Rebuild cache
        config_files, scan_errors = _get_config_files()
        errors.extend(scan_errors)

        for f in config_files:
            try:
                cfg = _load_config_file(f)
                char_id = cfg.get("character_id")
                if char_id:
                    # Validate and fix nested structures during caching
                    if "faster_whisper_config" in cfg:
                        if not isinstance(cfg["faster_whisper_config"], dict):
                            logger.warning(f"Invalid faster_whisper_config in {f}, using default")
                            cfg["faster_whisper_config"] = {"language": "en"}

                    if "tts_model_config" in cfg:
                        if not isinstance(cfg["tts_model_config"], dict):
                            logger.warning(f"Invalid tts_model_config in {f}, using default")
                            cfg["tts_model_config"] = {
                                "model_path": "",
                                "config_path": "",
                                "style_vectors_path": ""
                            }

                    _character_file_cache[char_id] = f
                    _character_config_cache[char_id] = cfg
            except Exception as e:
                error_msg = f"Error caching config file {f}: {e}"
                logger.warning(error_msg)
                errors.append(error_msg)
                continue

        # Implement cache size limit with LRU eviction
        if len(_character_config_cache) > _MAX_CACHE_SIZE:
            # Keep only the most recent _MAX_CACHE_SIZE entries
            # Since dict maintains insertion order in Python 3.7+, we can slice
            excess_count = len(_character_config_cache) - _MAX_CACHE_SIZE
            keys_to_remove = list(_character_config_cache.keys())[:excess_count]
            for key in keys_to_remove:
                del _character_config_cache[key]
                _character_file_cache.pop(key, None)
            logger.info(f"Evicted {excess_count} oldest cache entries to maintain size limit")

        _last_cache_update = current_time
        return True, errors

def _validate_cache_integrity() -> bool:
    """
    Validate that cached data is consistent and not corrupted.
    
    Returns:
        bool: True if cache is valid, False if corrupted
    """
    try:
        # Check that both caches have same keys
        if set(_character_file_cache.keys()) != set(_character_config_cache.keys()):
            return False
            
        # Spot check that cached files still exist
        sample_size = min(5, len(_character_file_cache))
        for char_id in list(_character_file_cache.keys())[:sample_size]:
            if not _character_file_cache[char_id].exists():
                return False
                
        return True
    except Exception as e:
        logger.warning(f"Cache integrity check failed: {e}")
        return False


def update_character_cache(character_id: str, config: Dict) -> None:
    """
    Update the cache for a specific character with new config data.
    Call this after updating a character's config to keep cache in sync.

    Args:
        character_id: The character ID to update
        config: The updated configuration dictionary
    """
    global _character_config_cache

    with _character_lock:
        _character_config_cache[character_id] = config.copy()
        logger.debug(f"Updated config cache for character: {character_id}")


def _enforce_cache_limit() -> None:
    """
    Enforce the cache size limit by removing oldest entries if needed.
    This should be called whenever new entries are added to the cache.
    """
    if len(_character_config_cache) > _MAX_CACHE_SIZE:
        excess_count = len(_character_config_cache) - _MAX_CACHE_SIZE
        keys_to_remove = list(_character_config_cache.keys())[:excess_count]
        for key in keys_to_remove:
            del _character_config_cache[key]
            _character_file_cache.pop(key, None)
        logger.debug(f"Evicted {excess_count} cache entries to maintain size limit")


def _find_config_file_by_id(character_id: str) -> Optional[Path]:
    """
    Find a config file by scanning the character_configs directory
    and matching config["character_id"] == character_id.
    Returns the Path if found, else None.

    Args:
        character_id: String identifier for the character

    Returns:
        Optional[Path]: Path to the config file if found, None otherwise
    """
    if not character_id:
        return None
    
    # Normalize to lowercase for consistent lookups
    character_id = character_id.strip().lower()

    # Check if we need to refresh cache
    _, errors = _get_or_refresh_cache()
    if errors:
        logger.debug(f"Cache refresh encountered {len(errors)} errors")

    # Return cached result if available
    return _character_file_cache.get(character_id)


def load_character_config(character_id: str) -> Dict:
    """
    Retrieves config details for a specific character by reading the corresponding
    config file. Returns a dictionary of the following shape:
      {
        "character_id": <string>,
        "name": <string>,
        "summary_text": <string>,
        "icon_path": <string>,
        "faster_whisper_config": {"language": <string>},
        "model_provider": <string>,
        "model_name": <string>,
        "tts_model_config": {
            # provider "sbv2" (default when the key is absent):
            "provider": "sbv2",
            "model_path": <string>,
            "config_path": <string>,
            "style_vectors_path": <string>
            # provider "kokoro": {"provider": "kokoro", "voice_name": <string>}
            # provider "elevenlabs": {"provider": "elevenlabs",
            #                         "voice_id": <string>, "voice_name": <string>}
        },
        "db_file_path": <string>,
        "system_prompt": <string>
      }

    Args:
        character_id: String identifier for the character

    Returns:
        Dict: Character configuration dictionary

    Raises:
        ValueError: If character_id is invalid
        FileNotFoundError: If the character config doesn't exist
        IOError: If the config file cannot be read
    """
    # Validate character_id
    if not isinstance(character_id, str) or not character_id:
        raise ValueError("character_id must be a non-empty string")
    
    # Normalize to lowercase
    character_id = character_id.strip().lower()
    
    # Additional validation for character_id format
    if not re.match(r'^[a-zA-Z0-9_-]+$', character_id):
        raise ValueError(f"Invalid character_id format: {character_id}. Must contain only alphanumeric characters, hyphens, and underscores.")
    
    # First check if we have it in the cache. Refresh + check-then-get must be
    # under _character_lock together, else a concurrent TTL refresh clearing the
    # dict between the `in` test and the subscript raises KeyError.
    with _character_lock:
        _, cache_errors = _get_or_refresh_cache()
        if cache_errors:
            logger.debug(f"Cache refresh encountered {len(cache_errors)} errors during load_character_config")

        if character_id in _character_config_cache:
            return _character_config_cache[character_id].copy()

    # If not in cache, try to load from file
    config_file = _find_config_file_by_id(character_id)
    if not config_file:
        logger.error(f"Character config file not found for ID: {character_id}")
        raise FileNotFoundError(f"Character '{character_id}' config does not exist.")

    # Use file lock for concurrent access protection
    file_lock = None
    try:
        file_lock = _get_file_lock(str(config_file))
        
        try:
            config = _load_config_file(config_file)
            
            # Validate required fields
            if not config.get("character_id"):
                raise ValueError(f"Invalid character config in {config_file}: missing or empty character_id")
            if not config.get("name"):
                raise ValueError(f"Invalid character config in {config_file}: missing or empty name")
            
            # Validate and fix nested structures
            if "faster_whisper_config" in config:
                if not isinstance(config["faster_whisper_config"], dict):
                    logger.warning(f"Invalid faster_whisper_config for {character_id}, using default")
                    config["faster_whisper_config"] = {"language": "en"}
            
            if "tts_model_config" in config:
                if not isinstance(config["tts_model_config"], dict):
                    logger.warning(f"Invalid tts_model_config for {character_id}, using default")
                    config["tts_model_config"] = {
                        "model_path": "",
                        "config_path": "",
                        "style_vectors_path": ""
                    }
            
            # Add to cache
            with _character_lock:
                _character_file_cache[character_id] = config_file
                _character_config_cache[character_id] = config
                _enforce_cache_limit()
                # Update cache timestamp when adding new entries
                global _last_cache_update
                if _last_cache_update == 0:
                    _last_cache_update = time.time()
            
            return config.copy()
        except Exception as e:
            # warning 止まり: raise した IOError は standardize_response の
            # 汎用分岐が logger.exception する=ERROR二重を防ぐ(稜裁定
            # 2026-08-02)
            logger.warning(f"Error loading character config from {config_file}: {e}")
            raise IOError(f"Failed to load character config from {config_file}: {e}")
    finally:
        if file_lock:
            file_lock.release()


def list_ollama_models(server_url: Optional[str] = None, timeout: Optional[float] = None) -> List[str]:
    """
    Return a list of installed Ollama model names by querying the Ollama server.

    Args:
        server_url: URL of the Ollama server (currently ignored, uses OLLAMA_HOST/PORT from environment)
        timeout: Request timeout in seconds (currently ignored, uses default from ollama_integration)

    Returns:
        List[str]: List of available model names
    """
    # Note: server_url and timeout parameters are kept for backward compatibility
    # but are not used in the current implementation. The ollama_integration module
    # uses environment variables for configuration.
    if server_url is not None:
        logger.info(f"server_url parameter ({server_url}) is deprecated and will be ignored")
    if timeout is not None:
        logger.info(f"timeout parameter ({timeout}) is deprecated and will be ignored")
    
    try:
        # Use the centralized implementation from ollama_integration
        # get_ollama_models returns Tuple[List[str], str] - we only need the list
        models, status_message = get_ollama_models()
        if status_message:
            logger.warning(f"Ollama model listing returned with status: {status_message}")
        return models
    except Exception as e:
        logger.warning(f"Failed to list Ollama models: {e}")
        return []


def list_tts_models() -> List[str]:
    """
    Return a list of available TTS models by scanning sbv2_models/ subfolders
    for .safetensors/.pth, config.json, and style_vectors.npy.

    A valid TTS model must have all three required files in the same directory:
    1. A model file (.safetensors or .pth)
    2. A config.json file
    3. A style_vectors.npy file

    Returns:
        List[str]: List of valid TTS model directory names (not full paths)
    """
    results = []
    base = Path(TTS_MODELS_DIR)
    if not base.exists():
        return results

    for subdir in base.iterdir():
        if subdir.is_dir():
            # Check for required files
            has_model = False
            has_config = False
            has_style = False

            for file in subdir.iterdir():
                if file.suffix in [".safetensors", ".pth"]:
                    has_model = True
                elif file.name == "config.json":
                    has_config = True
                elif file.name == "style_vectors.npy":
                    has_style = True

            if has_model and has_config and has_style:
                results.append(subdir.name)

    return results


def list_character_icons() -> List[str]:
    """
    Return a list of image files in character_icons/ directory.

    Returns:
        List[str]: List of image filenames (not full paths)
    """
    results = []
    icon_dir = Path(CHARACTER_ICONS_DIR)

    if not icon_dir.exists():
        return results

    for file in icon_dir.iterdir():
        if file.is_file() and file.suffix.lower() in VALID_IMAGE_EXTENSIONS:
            results.append(file.name)

    return results


def list_stt_languages() -> List[str]:
    """
    Return a list of available faster-whisper language codes.
    Currently supports English (en) and Japanese (ja) as per requirements.

    Note: This is a static list since the supported languages are
    determined by the application requirements and not dynamically
    detected from the model capabilities.

    Returns:
        List[str]: List of supported language codes
    """
    # In this implementation, we simply return supported languages
    # since faster-whisper uses pretrained models with language settings
    return ["en", "ja"]


def migrate_character_configs() -> Dict[str, Any]:
    """
    Migrate character configs from old format (ollama_model_name) to new format
    (model_provider + model_name). Called once at app startup.

    Old format:
        "ollama_model_name": "qwen3:14b"

    New format:
        "model_provider": "ollama",
        "model_name": "qwen3:14b"

    Migration is idempotent: configs already in new format are skipped.

    Returns:
        Dict with migration results: {"migrated": int, "skipped": int, "errors": list}
    """
    result = {"migrated": 0, "skipped": 0, "errors": []}

    config_files, scan_errors = _get_config_files()
    result["errors"].extend(scan_errors)

    if not config_files:
        logger.info("No character configs found for migration")
        return result

    for config_file in config_files:
        try:
            config_data = _load_config_file(config_file)

            # Check if migration is needed
            has_old_field = "ollama_model_name" in config_data
            has_new_field = "model_provider" in config_data

            if has_new_field:
                # Already migrated
                if has_old_field:
                    # Remove leftover old field
                    del config_data["ollama_model_name"]
                    _save_config_file(config_file, config_data)
                    logger.debug(f"Removed leftover ollama_model_name from {config_file.name}")
                result["skipped"] += 1
                continue

            if has_old_field:
                # Perform migration
                old_model_name = config_data.get("ollama_model_name", "")
                config_data["model_provider"] = "ollama"
                config_data["model_name"] = old_model_name
                del config_data["ollama_model_name"]

                _save_config_file(config_file, config_data)
                result["migrated"] += 1
                logger.info(f"Migrated {config_file.name}: ollama_model_name -> model_provider=ollama, model_name={old_model_name}")
            else:
                # Neither old nor new field present - add defaults
                config_data["model_provider"] = "ollama"
                config_data["model_name"] = ""

                _save_config_file(config_file, config_data)
                result["migrated"] += 1
                logger.warning(f"Added default model fields to {config_file.name} (no previous model field found)")

        except Exception as e:
            error_msg = f"Failed to migrate {config_file.name}: {e}"
            result["errors"].append(error_msg)
            logger.error(error_msg)

    # Clear cache after migration to force reload with new fields
    with _character_lock:
        _character_config_cache.clear()
        _character_file_cache.clear()
        global _last_cache_update
        _last_cache_update = 0

    logger.info(f"Character config migration complete: {result['migrated']} migrated, {result['skipped']} skipped, {len(result['errors'])} errors")
    return result


# ---------------------------------------------------------------------------
# Character tuning (B10 / SL4 Character)
#
# Bodies moved verbatim from backend.backend (ST4-B10 backend.py split).
# App-layer god-object access (_backend_state.active_llm_cache) is injected as
# `state`; the character-list loader is injected as a callable. The character
# config/cache internals (load_character_config / _save_config_file /
# _get_config_filepath / _character_lock / _character_config_cache) are now
# same-module direct references — the cross-module import into backend.backend
# disappears. Dependency direction: domain (this module) <- (injected) app.
# ---------------------------------------------------------------------------

def get_character_tuning(character_id: str) -> Dict[str, Any]:
    """
    Get tuning parameters for a character.

    Args:
        character_id: Character ID

    Returns:
        Dict with tuning parameters (merged with defaults)
    """
    from backend.shared.constants import get_tuning_defaults

    try:
        config = load_character_config(character_id)
        tuning = config.get("tuning", {})

        # Merge with defaults
        result = get_tuning_defaults()
        result.update(tuning)
        return result
    except Exception as e:
        logger.warning(f"Failed to get tuning for {character_id}, using defaults: {e}")
        return get_tuning_defaults()


def validate_tuning_params(tuning: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate tuning parameters against allowed ranges.

    Args:
        tuning: Dictionary of tuning parameters

    Returns:
        {"valid": True} or {"valid": False, "errors": [...]}
    """
    from backend.shared.constants import TUNING_RANGES

    errors = []
    for param, value in tuning.items():
        if param not in TUNING_RANGES:
            continue

        range_def = TUNING_RANGES[param]
        if range_def is None:  # range=None は検証免除（現在該当なし・防御）
            continue

        min_val, max_val = range_def
        try:
            numeric_value = float(value)
            if not (min_val <= numeric_value <= max_val):
                errors.append(f"{param}: {value} is out of range ({min_val}〜{max_val})")
        except (TypeError, ValueError):
            errors.append(f"{param}: invalid value '{value}'")

    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True}


def invalidate_llm_cache(state, character_id: str) -> None:
    """
    Invalidate (remove) the LLM cache for a character.
    Next generate_reply will recreate the LLM with new parameters.

    Args:
        state: Backend state container (injected — owns active_llm_cache).
        character_id: Character ID to invalidate
    """
    if character_id in state.active_llm_cache:
        del state.active_llm_cache[character_id]
        logger.info(f"Invalidated LLM cache for character {character_id}")


def save_character_tuning(state, character_id: str, tuning: Dict[str, Any]) -> Dict[str, Any]:
    """
    Save tuning parameters for a character.

    Args:
        state: Backend state container (injected — for LLM cache invalidation).
        character_id: Character ID
        tuning: Dictionary of tuning parameters

    Returns:
        Dict with success status
    """
    from pathlib import Path

    # Validate parameters
    validation = validate_tuning_params(tuning)
    if not validation["valid"]:
        return {
            "success": False,
            "error": "Invalid parameters: " + "; ".join(validation["errors"]),
            "error_type": "VALIDATION"
        }

    try:
        with _character_lock:
            config = load_character_config(character_id)
            config["tuning"] = tuning

            config_path = _get_config_filepath(character_id)
            _save_config_file(Path(config_path), config)

            # Update cache
            _character_config_cache[character_id] = config.copy()

        # Invalidate LLM cache so next request uses new parameters
        invalidate_llm_cache(state, character_id)

        logger.info(f"Saved tuning parameters for character {character_id}")
        return {"success": True}

    except Exception as e:
        logger.error(f"Failed to save tuning: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "INTERNAL"
        }


def apply_tuning_to_all_characters(state, tuning: Dict[str, Any], load_character_list) -> Dict[str, Any]:
    """
    Apply tuning parameters to all characters and save as defaults.

    This function:
    1. Validates tuning parameters
    2. Saves them as defaults (for new characters)
    3. Updates all existing character config files
    4. Invalidates all cached LLM instances

    Args:
        state: Backend state container (injected — owns active_llm_cache).
        tuning: Dictionary of tuning parameters
        load_character_list: Injected callable returning the character list
            (the backend.backend @standardize_response-wrapped loader).

    Returns:
        Dict with:
            - success: bool
            - updated_count: int (number of successfully updated characters)
            - total_count: int (total number of characters)
            - failed: List of failed character names
            - message: str (summary message)
    """
    from backend.shared.constants import save_tuning_defaults, TUNING_RANGES
    from pathlib import Path

    # 1. Validate parameters
    errors = []
    for param, value in tuning.items():
        if param not in TUNING_RANGES:
            continue

        range_def = TUNING_RANGES.get(param)
        if range_def is None:  # range=None は検証免除（現在該当なし・防御）
            continue

        min_val, max_val = range_def
        try:
            numeric_value = float(value)
            if not (min_val <= numeric_value <= max_val):
                errors.append(f"{param}: {value} is out of range ({min_val}-{max_val})")
        except (TypeError, ValueError):
            errors.append(f"{param}: invalid numeric value")

    if errors:
        return {
            "success": False,
            "error": "; ".join(errors),
            "error_type": "VALIDATION"
        }

    # 2. Save as defaults
    defaults_result = save_tuning_defaults(tuning)
    if not defaults_result.get("success"):
        return defaults_result

    # 3. Update all character configs
    char_list_result = load_character_list()
    # load_character_list returns {'success': True, 'result': [...]} due to @standardize_response
    char_list = char_list_result.get('result', []) if isinstance(char_list_result, dict) else char_list_result
    updated_count = 0
    failed = []

    for char in char_list:
        char_id = char.get("id")
        char_name = char.get("name", char_id)
        try:
            with _character_lock:
                config = load_character_config(char_id)
                config["tuning"] = tuning.copy()

                config_path = _get_config_filepath(char_id)
                if config_path:
                    _save_config_file(Path(config_path), config)
                    # Update cache
                    _character_config_cache[char_id] = config.copy()
                    updated_count += 1
                else:
                    failed.append(char_name)
        except Exception as e:
            logger.warning(f"Failed to update tuning for {char_name}: {e}")
            failed.append(char_name)

    # 4. Invalidate all cached LLM instances
    cached_ids = list(state.active_llm_cache.keys())
    for char_id in cached_ids:
        del state.active_llm_cache[char_id]
        logger.info(f"Invalidated LLM cache for character {char_id}")

    total_count = len(char_list)
    message = f"Updated {updated_count}/{total_count} characters"
    if failed:
        message += f". Failed: {', '.join(failed)}"

    logger.info(f"Applied tuning to all characters: {message}")

    return {
        "success": True,
        "updated_count": updated_count,
        "total_count": total_count,
        "failed": failed,
        "message": message
    }


# ---------------------------------------------------------------------------
# Character CRUD orchestration (B10 / SL4 Character)
#
# Bodies moved verbatim from backend.backend (ST4-B10 backend.py split).
# App-layer god-object access (_backend_state) is injected as `state`; the
# backend-resident collaborators (stop_conversation, atomic_file_operation) and
# the @standardize_response-wrapped load_character_config delegate are injected
# as callables. The config-file helpers (_get_config_files / _load_config_file /
# _save_config_file / _find_config_file_by_id) are now same-module direct
# references -- the cross-module import into backend.backend disappears.
# Dependency direction: domain (this module) <- (injected) app.
#
# These coexist with the strict file-layer load_character_config (raw Dict)
# above; that one is the directly-imported loader used by app/ws/memory/elyth
# and is left untouched. backend's version is kept here as
# load_character_config_managed (version-migrating loader behind the public
# @standardize_response delegate).
# ---------------------------------------------------------------------------
def load_character_list() -> List[Dict[str, Any]]:
    """
    Scan the character_configs/ folder for .json/.yaml config files and return
    a list of metadata (id, name, icon_path, etc.) for each character.
    """
    results = []
    config_files, errors = _get_config_files()
    
    # Log any errors encountered while scanning for config files
    for error in errors:
        logger.warning(f"Error scanning config directory: {error}")
    
    for f in config_files:
        try:
            config = _load_config_file(f)
            results.append({
                "id": config.get("character_id", ""),
                "name": config.get("name", ""),
                "icon_path": config.get("icon_path", "")
            })
        except Exception as e:
            logger.warning(f"Failed to parse config {f}: {e}")
    return results


def any_character_uses_tts_provider(provider: str) -> bool:
    """True if any character config selects the given TTS provider.

    Used by the pre-launch preload to decide whether the Kokoro stack is
    worth importing before open (ja-only installs skip its ~3s import cost).
    Missing provider key means the legacy default "sbv2".
    """
    config_files, _errors = _get_config_files()
    for f in config_files:
        try:
            tts = _load_config_file(f).get("tts_model_config") or {}
            if isinstance(tts, dict) and tts.get("provider", "sbv2") == provider:
                return True
        except Exception:
            continue
    return False


def _validate_character_info(character_info: Dict[str, Any]):
    """Validate all character info before any modifications"""
    required_fields = ["name"]
    for field in required_fields:
        if field not in character_info:
            raise ValueError(f"Missing required field: {field}")
    
    # Validate types
    if not isinstance(character_info.get("name"), str):
        raise TypeError("Character name must be a string")
    
    # Validate Ollama model if specified
    model_provider = character_info.get("model_provider", "ollama")
    if model_provider == "ollama" and character_info.get("model_name"):
        # get_ollama_models returns Tuple[List[str], str] - we only need the list
        available_models, _ = get_ollama_models()
        if character_info["model_name"] not in available_models:
            logger.warning(f"Model {character_info['model_name']} not found in available Ollama models")


def create_character(character_info: Dict[str, Any], atomic_file_operation) -> str:
    """
    Add a new character to the system:
      - Generate a unique character_id if not provided.
      - Create a new .db file or plan to create it on first use.
      - Save a new config file (JSON) with the provided fields.

    Args:
        character_info: Dictionary containing character configuration

    Returns:
        str: The character_id of the created character

    Raises:
        ValueError: For validation errors (invalid input, duplicate character)
        PermissionError: For file system permission issues
        RuntimeError: For other system errors
    """
    # 非対応モデル(思考を無効化できない Ollama モデル)は config を作る前に拒否
    # (2026-08-16 稜裁定。実測プローブ・不明は通す。AGError は
    # standardize_response が error_code=model_thinking_unsupported に載せる)
    from backend.llm.ollama_capabilities import ensure_model_supported
    ensure_model_supported(
        character_info.get("model_provider", "ollama"),
        character_info.get("model_name", "") or character_info.get("ollama_model_name", ""),
    )

    character_id = character_info.get("character_id", str(uuid.uuid4()))
    created_resources = []
    
    try:
        # Validate all inputs first
        _validate_character_info(character_info)
        
        # Validate character_id format
        is_valid, error_msg = validate_character_id(character_id)
        if not is_valid:
            raise ValueError(f"Invalid character_id: {error_msg}")
        
        # Track what we create for rollback
        config_dir = Path(CHARACTER_CONFIGS_DIR)
        config_dir.mkdir(parents=True, exist_ok=True)
        
        config_filename = config_dir / f"{character_id}.json"
        
        # Check for duplicates
        if config_filename.exists():
            raise ValueError(f"Character {character_id} already exists")
        
        # Build config
        from backend.shared.constants import get_tuning_defaults
        config = {
            "version": "1.0",  # Add version for future compatibility
            "character_id": character_id,
            "name": character_info.get("name", f"Character_{character_id}"),
            "icon_path": character_info.get("icon_path", ""),
            "summary_text": character_info.get("summary_text", ""),
            "system_prompt": character_info.get("system_prompt", ""),
            "faster_whisper_config": character_info.get("faster_whisper_config", {"language": "en"}),
            "model_provider": character_info.get("model_provider", "ollama"),
            "model_name": character_info.get("model_name", ""),
            "tts_model_config": character_info.get("tts_model_config", {
                "model_path": "",
                "config_path": "",
                "style_vectors_path": ""
            }),
            "db_file_path": character_info.get("db_file_path", f"{MEMORY_DIR}/{character_id}.db"),
            "tuning": character_info.get("tuning", get_tuning_defaults()),
            # UI/consumers pass these optional fields; the old fixed whitelist
            # dropped them silently (motion PNGTuber folder + ELYTH settings),
            # so a newly created character lost them until re-edited.
            "motion_pngtuber_folder": character_info.get("motion_pngtuber_folder", ""),
            "elyth_system_prompt": character_info.get("elyth_system_prompt", ""),
            "elyth_api_key": character_info.get("elyth_api_key", ""),
            "youtube_system_prompt": character_info.get("youtube_system_prompt", ""),
        }
        
        # Create memory directory
        mem_path = Path(MEMORY_DIR)
        mem_path.mkdir(parents=True, exist_ok=True)
        
        # Create memory database file
        db_path = Path(config["db_file_path"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
        created_resources.append(("file", db_path))
        
        # Write config file atomically (path fields persisted repo-relative)
        with atomic_file_operation(config_filename, "write") as temp_file:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(_map_config_paths(config, to_repo_relative), f, indent=2)
        
        created_resources.append(("file", config_filename))
        
        # Copy icon if provided
        if character_info.get("icon_path"):
            try:
                # Validate and sanitize the source path
                source_path = Path(character_info["icon_path"]).resolve()
                
                # Define allowed source directories for icons
                allowed_dirs = [
                    BASE_DIR,  # Project root (launch-dir independent)
                    Path(CHARACTER_ICONS_DIR),  # Already in icons directory
                    Path.home() / "Pictures",  # User's Pictures folder
                    Path.home() / "Downloads",  # User's Downloads folder
                ]
                
                # Check if source path is within allowed directories
                is_allowed = any(
                    source_path == allowed_dir or source_path.is_relative_to(allowed_dir)
                    for allowed_dir in allowed_dirs
                    if allowed_dir.exists()
                )
                
                if not is_allowed:
                    logger.warning(f"Icon path {source_path} is outside allowed directories, skipping icon copy")
                elif not source_path.exists():
                    logger.warning(f"Icon file {source_path} does not exist, skipping icon copy")
                elif not source_path.is_file():
                    logger.warning(f"Icon path {source_path} is not a file, skipping icon copy")
                elif source_path.stat().st_size > 10 * 1024 * 1024:  # 10MB limit
                    logger.warning(f"Icon file {source_path} exceeds 10MB size limit, skipping icon copy")
                else:
                    # Determine appropriate extension from source
                    icon_ext = source_path.suffix.lower()
                    if icon_ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                        logger.warning(f"Unsupported icon format {icon_ext}, skipping icon copy")
                    else:
                        icon_dest = Path(CHARACTER_ICONS_DIR) / f"{character_id}{icon_ext}"
                        shutil.copy2(source_path, icon_dest)
                        created_resources.append(("file", icon_dest))
                        # Update the config with the new icon path AND re-persist:
                        # the config file was already written above pointing at the
                        # original source (e.g. Downloads). Without re-saving, the
                        # on-disk config keeps the stale path and the copy is orphaned.
                        config["icon_path"] = str(icon_dest)
                        with atomic_file_operation(config_filename, "write") as temp_file:
                            with open(temp_file, 'w', encoding='utf-8') as f:
                                json.dump(_map_config_paths(config, to_repo_relative), f, indent=2)
                        logger.info(f"Copied icon from {source_path} to {icon_dest}")
            except Exception as icon_error:
                logger.warning(f"Failed to copy icon: {icon_error}")
                # Don't fail character creation due to icon issues
        
        logger.info(f"Successfully created character {character_id}")
        return character_id
        
    except Exception as e:
        # Rollback: Remove all created resources
        logger.error(f"Failed to create character, rolling back: {e}")
        
        for resource_type, resource_path in reversed(created_resources):
            try:
                if resource_type == "file" and resource_path.exists():
                    resource_path.unlink()
                    logger.debug(f"Rolled back: {resource_path}")
            except Exception as rollback_error:
                logger.error(f"Failed to rollback {resource_path}: {rollback_error}")
        
        raise RuntimeError(f"Character creation failed: {e}") from e


def edit_character(state, character_id: str, updated_info: Dict[str, Any], atomic_file_operation, activate_character) -> None:
    """
    Modify an existing character's settings. If this character is active,
    we may need to reload STT/TTS or LLM if changed.
    When updating the icon, removes the old icon file to prevent orphaned files.
    """
    config_file = _find_config_file_by_id(character_id)
    if not config_file:
        logger.error(f"No config file found for character_id={character_id}")
        raise ValueError(f"Character {character_id} not found")

    # Track old icon for potential deletion
    old_icon_path = None

    # ロード済みリソース(STT/TTS/LLM)に影響するキー。これ以外の編集
    # (name/summary/icon/システムプロンプト/motionフォルダ/ELYTH等)は
    # 保存のみで反映される=フル再ロードを走らせない(稜GO 2026-08-15)。
    # プロンプトは生成のたびにconfigから読み直されるため対象外。
    reload_keys = ("tts_model_config", "faster_whisper_config",
                   "model_provider", "model_name", "ollama_model_name",
                   "db_file_path")
    old_reload_values = None

    # 非対応モデルへの変更は書き込む前に拒否(2026-08-16 稜裁定)。実効
    # provider/model が変わるときだけ判定する=名前だけの編集は素通り(旧設定が
    # 非対応でも選択時の activate_character が最終防衛線)。atomic_file_operation
    # の外で読む: コンテキスト入場時に .bak が作られるため中で raise しない
    _current = _load_config_file(config_file)
    _cur_provider = _current.get("model_provider", "ollama")
    _cur_model = _current.get("model_name", "") or _current.get("ollama_model_name", "")
    _new_provider = updated_info.get("model_provider", _cur_provider)
    _new_model = (updated_info.get("model_name")
                  or updated_info.get("ollama_model_name")
                  or _cur_model)
    if (_new_provider, _new_model) != (_cur_provider, _cur_model):
        from backend.llm.ollama_capabilities import ensure_model_supported
        ensure_model_supported(_new_provider, _new_model)

    # Use atomic file operation for safe updates
    with atomic_file_operation(Path(config_file), "update"):
        config = _load_config_file(config_file)
        old_reload_values = {k: config.get(k) for k in reload_keys}

        # Check if icon is being updated
        if "icon_path" in updated_info:
            old_icon_path = config.get("icon_path", "")
            new_icon_path = updated_info["icon_path"]
            
            # Only proceed if icon is actually changing
            if old_icon_path and new_icon_path and old_icon_path != new_icon_path:
                logger.info(f"Icon path changing from {old_icon_path} to {new_icon_path}")

        # Overwrite relevant fields
        for k, v in updated_info.items():
            config[k] = v

        # Save
        _save_config_file(config_file, config)
        # Keep the in-memory cache in sync so readers don't serve the pre-edit
        # config for up to _CACHE_TTL seconds.
        update_character_cache(character_id, config)
        logger.info(f"Updated character config for {character_id}")

    # Delete old icon if it was changed
    if old_icon_path and "icon_path" in updated_info:
        new_icon_path = updated_info["icon_path"]
        if old_icon_path != new_icon_path and os.path.exists(old_icon_path):
            try:
                # Only delete if icon is in the character_icons directory
                old_icon_obj = Path(old_icon_path).resolve()
                icons_dir_obj = Path(CHARACTER_ICONS_DIR).resolve()
                
                try:
                    # Try to get relative path - if it works, icon is within the directory
                    old_icon_obj.relative_to(icons_dir_obj)
                    os.remove(old_icon_path)
                    logger.info(f"Deleted old character icon: {old_icon_path}")
                except ValueError:
                    # relative_to raises ValueError if path is outside the directory
                    logger.warning(f"Old icon {old_icon_path} is outside character_icons directory, not deleting")
            except Exception as e:
                logger.error(f"Failed to delete old icon file {old_icon_path}: {e}")
                # Continue even if deletion fails

    # If active, reconfigure STT/TTS/LLM — but only when a reload-relevant
    # field actually changed (TTSモデルのGPUロード+LLMクライアント生成は重い。
    # 通常の編集保存を1秒未満にするための条件付き再ロード)
    if state.active_character_id == character_id:
        needs_reload = any(
            k in updated_info and config.get(k) != old_reload_values[k]
            for k in reload_keys
        )
        if needs_reload:
            # Re-activate to refresh the settings, preserving conversation state
            activate_character(character_id, preserve_conversation=True)
        else:
            logger.info(
                f"Edit of active character {character_id} touched no "
                f"STT/TTS/LLM fields — skipping re-activation")


def _remove_memory_artifacts(character_id: str, db_file_path: str) -> None:
    """Delete a character's memory DB and every file derived from it:
    WAL side files (<db>-wal/-shm/-journal), corruption/backup copies
    (<db>.corrupt.* / .corrupted.* / .backup.*), emergency recovery dumps
    (<uuid>.recovery.*.json) and the attachments folder memory/<uuid>/
    (images/documents). Siblings are matched by prefix (<dbname>- / <dbname>.)
    rather than a fixed suffix list so a future derived file cannot be left
    behind. Nothing here is reachable once the character config is gone.
    """
    def _rm_file(path: str, what: str) -> None:
        try:
            os.remove(path)
            logger.info(f"Permanently deleted {what}: {path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Failed to delete {what} {path}: {e}")

    if db_file_path:
        if os.path.exists(db_file_path):
            _rm_file(db_file_path, "memory DB")
        db_dir = os.path.dirname(db_file_path) or "."
        db_name = os.path.basename(db_file_path)
        try:
            siblings = os.listdir(db_dir)
        except OSError:
            siblings = []
        for name in siblings:
            if name.startswith(db_name + "-") or name.startswith(db_name + "."):
                _rm_file(os.path.join(db_dir, name), "memory DB side file")

    mem_dir = Path(MEMORY_DIR)
    try:
        for name in os.listdir(mem_dir):
            if name.startswith(f"{character_id}.recovery.") and name.endswith(".json"):
                _rm_file(str(mem_dir / name), "memory recovery file")
    except OSError:
        pass

    # 添付置き場は 2026-08-20 に memory/<uuid>/ から attachments/<uuid>/ へ
    # 分離(.db との同居解消)。移行済みなので旧位置の掃除は持たない
    # (旧位置に uuid フォルダが現れたら check_character_data.py が異常として検出)。
    attachments_dir = ATTACHMENTS_DIR / character_id
    if attachments_dir.is_dir():
        try:
            shutil.rmtree(attachments_dir)
            logger.info(f"Permanently deleted attachments folder: {attachments_dir}")
        except Exception as e:
            logger.error(f"Failed to delete attachments folder {attachments_dir}: {e}")


def remove_character(state, character_id: str) -> None:
    """
    Delete a specified character from the system — leaving no trace keyed by
    its id anywhere under character_data/:
    - config file / icon file (if inside character_icons/)
    - memory DB + WAL side files + corruption copies + recovery dumps +
      attachments folder attachments/<uuid>/ (see _remove_memory_artifacts)
    - relationship file, note file, ELYTH session log (+ its <uuid>.tmp
      atomic-write sidecar), ELYTH note / relationships / thread state
      (+ own-handle registry entry), generated_images/<uuid>/
    - If the character is active, switch to a default or null state.
    Every per-character file lives in the module that writes it, which owns a
    remove_*(character_id); add one there when adding a new per-character file.
    """
    config_file = _find_config_file_by_id(character_id)
    if not config_file:
        logger.error(f"Cannot remove character {character_id}: config not found.")
        raise ValueError(f"Character {character_id} not found")

    # --- 拒否ガード ---------------------------------------------------------
    # 下の try: より前に置くこと。try の except は AGError を素の RuntimeError に
    # 詰め替える(=ag_code が落ちて UI が翻訳済みトーストを出せなくなる)ため、
    # ガードを try の内側へ動かすと機械ゲートは緑のまま UI だけ英語に退化する。

    # Refuse while background memory work for this character is still running —
    # deleting the .db under a live writer leaves an orphaned file (the writer
    # holds it open), and the extraction would commit into a removed character.
    from backend.shared.errors import AGError
    with state.background_tasks_lock:
        tasks_snapshot = list(state.background_tasks)
    for task in tasks_snapshot:
        if (task.get('character_id') == character_id
                and task.get('task_type') in ('extraction', 'relationship_update', 'memory_save',
                                              'embedding_migration')
                and task.get('thread') is not None
                and task['thread'].is_alive()):
            logger.warning(f"Cannot remove character {character_id}: {task['task_type']} task still running")
            raise AGError(DELETE_MEMORY_TASK_CODE,
                          "Memory processing in progress for this character. Please try again shortly.")

    # Refuse while an ELYTH session is running *for this character* — it writes
    # its session log every turn (hard-kill resilience), so deleting underneath
    # recreates the file right after the removers ran. 最後の砦は
    # elyth_memory の墓標(remove_session_log)で、こちらは理由をユーザーに返す役。
    # elyth_session_active を先に見るのは短絡のため: フラグを True にできるのは
    # ELYTHSessionManager 自身だけなので、True ならシングルトンは必ず存在する
    # (get_elyth_session_manager は未初期化だと raise する)。
    if getattr(state, 'elyth_session_active', False):
        try:
            from backend.elyth.elyth_session_manager import get_elyth_session_manager
            elyth_running = get_elyth_session_manager().is_session_active_for(character_id)
        except Exception as e:
            logger.warning(f"ELYTH session check failed for {character_id}: {e}")
            elyth_running = False
        if elyth_running:
            logger.warning(f"Cannot remove character {character_id}: ELYTH session still running")
            raise AGError(DELETE_ELYTH_ACTIVE_CODE,
                          "An ELYTH session is running for this character. "
                          "Try again after the session ends.")

    # Get character operation lock to prevent race with activate_character
    char_op_lock = state.get_character_operation_lock(character_id)
    if not char_op_lock.acquire(timeout=5.0):
        logger.error(f"Could not acquire character operation lock for {character_id}")
        raise RuntimeError("Character operation in progress. Please try again.")
    
    try:
        # Load config to find associated files
        config = _load_config_file(config_file)
        db_file_path = config.get("db_file_path", "")
        icon_path = config.get("icon_path", "")
        
        # Remove memory manager if exists — close its DB connections first so
        # the .db can be deleted (Windows cannot delete an open file) and SQLite
        # retires the WAL side files itself.
        memory_manager = state.memory_managers.pop(character_id, None)
        if memory_manager is not None:
            store = getattr(memory_manager, 'store', None)
            if store is not None and hasattr(store, 'close_all'):
                try:
                    store.close_all()
                except Exception as e:
                    logger.warning(f"Failed to close memory DB connections for {character_id}: {e}")
            logger.info(f"Removed memory manager for character {character_id}")
        
        # Remove from LLM cache if exists
        if character_id in state.active_llm_cache:
            del state.active_llm_cache[character_id]
            logger.info(f"Removed LLM instance for character {character_id}")
        
        # Remove database file and everything derived from it
        # (side files / corruption copies / recovery dumps / attachments folder)
        _remove_memory_artifacts(character_id, db_file_path)

        # Remove relationship file
        from backend.memory.relationship_manager import remove_relationship
        try:
            remove_relationship(character_id)
        except Exception as e:
            logger.error(f"Failed to delete relationship file for {character_id}: {e}")

        # Remove the other per-character files, each via its owning module:
        # notes / ELYTH notes / ELYTH relationships / ELYTH thread state
        # (+ own-handle registry entry) / generated images. All are keyed by
        # character_id only — unreachable once the config is gone.
        from backend.memory.note_manager import remove_note
        from backend.elyth.elyth_memory import remove_session_log
        from backend.elyth.elyth_note_manager import remove_elyth_note
        from backend.elyth.elyth_relationship_manager import remove_elyth_relationships
        from backend.elyth.elyth_thread_state import remove_thread_state
        from backend.shared.image_storage import remove_character_images
        for remover in (remove_note, remove_session_log, remove_elyth_note,
                        remove_elyth_relationships, remove_thread_state,
                        remove_character_images):
            try:
                remover(character_id)
            except Exception as e:
                logger.error(f"{remover.__name__} failed for {character_id}: {e}")

        # Remove icon file
        if icon_path and os.path.exists(icon_path):
            try:
                # Only delete if icon is in the character_icons directory
                icon_path_obj = Path(icon_path).resolve()
                icons_dir_obj = Path(CHARACTER_ICONS_DIR).resolve()
                
                # Check if icon is within the character_icons directory
                try:
                    # Try to get relative path - if it works, icon is within the directory
                    icon_path_obj.relative_to(icons_dir_obj)
                    os.remove(icon_path)
                    logger.info(f"Permanently deleted character icon: {icon_path}")
                except ValueError:
                    # relative_to raises ValueError if path is outside the directory
                    logger.warning(f"Icon {icon_path} is outside character_icons directory, not deleting")
            except Exception as e:
                logger.error(f"Failed to delete icon file {icon_path}: {e}")
                # Continue with removal even if icon deletion fails

        # Remove config file
        try:
            os.remove(config_file)
            logger.info(f"Permanently deleted character config: {config_file}")
        except Exception as e:
            # warning 止まり: 外側 except(Failed to remove character)が同一
            # 障害を ERROR ログする=二重防止(稜裁定 2026-08-02)
            logger.warning(f"Failed to delete config file {config_file}: {e}")
            raise RuntimeError(f"Failed to remove character config: {e}")

        # Invalidate cache so a stale entry isn't served for up to _CACHE_TTL.
        with _character_lock:
            _character_config_cache.pop(character_id, None)
            _character_file_cache.pop(character_id, None)

        # If the removed character is active, reset the active state
        if state.active_character_id == character_id:
            state.active_character_id = None
            state.conversation_active = False
            logger.info(f"Active character reset to None (removed {character_id}).")
            
        logger.info(f"Character {character_id} and all associated files permanently deleted")
            
    except Exception as e:
        logger.error(f"Failed to remove character {character_id}: {e}")
        raise RuntimeError(f"Failed to remove character: {e}")
    finally:
        # Always release the character operation lock
        char_op_lock.release()


def load_character_config_managed(character_id: str) -> Dict[str, Any]:
    """
    Retrieve config details for a specific character. 
    Returns a dict with settings (name, icon_path, system_prompt, etc.).
    """
    config_file = _find_config_file_by_id(character_id)
    if config_file:
        try:
            config = _load_config_file(config_file)
            
            # Debug log the loaded config
            logger.debug(f"Loaded config for character {character_id}: system_prompt={repr(config.get('system_prompt', 'NOT FOUND')[:100] if config.get('system_prompt') else 'NOT FOUND')}")
            
            # Version migration - add version if not present
            if "version" not in config:
                config["version"] = "1.0"
                logger.info(f"Migrated character {character_id} config to version 1.0")
                # Optionally save the updated config
                try:
                    _save_config_file(config_file, config)
                except Exception as save_error:
                    logger.warning(f"Could not save migrated config: {save_error}")
            
            return config
        except Exception as e:
            logger.error(f"Failed to load character config for {character_id}: {e}")
    else:
        logger.error(f"No config found for character_id={character_id}")
    return {}


def activate_character(state, character_id: str, preserve_conversation: bool, load_character_config, stop_conversation) -> Dict[str, Any]:
    """
    Switch to using this character for conversation:
      - Load the character config.
      - Initialize the MemoryManager using the .db file.
      - Configure STT language via audio_input.
      - Configure TTS model via audio_output.
      - Create or refresh the local LLM instance with the model name.
    
    Note: This prepares the character but doesn't start a conversation.
    Call start_conversation() to begin chatting.
    
    Args:
        character_id: The ID of the character to activate
        preserve_conversation: If True, preserves the conversation state (useful for config refresh)
    """
    logger.info(f"Activating character: {character_id}, preserve_conversation={preserve_conversation}")
    import audio_input.audio_input as audio_input
    import audio_output.audio_output as audio_output
    from backend.memory.memory_manager import MemoryManager
    
    # Check rate limit
    if not state.check_rate_limit(f"activate_{character_id}"):
        return {
            "success": False,
            "error": "Please wait a moment before switching characters again.",
            "error_type": "RATE_LIMIT"
        }
    
    # Clean up completed background tasks
    state.cleanup_background_tasks()
    
    # Get character operation lock to prevent race with remove_character
    char_op_lock = state.get_character_operation_lock(character_id)
    if not char_op_lock.acquire(timeout=5.0):
        logger.error(f"Could not acquire character operation lock for {character_id}")
        return {
            "success": False,
            "error": "Character operation in progress. Please try again.",
            "error_type": "BUSY"
        }
    
    try:
        # Enhanced activation tracking with timeout handling
        with state.activation_lock:
            # Check for stuck activations
            if hasattr(state, '_activation_info'):
                active_id, start_time = state._activation_info
                elapsed = time.time() - start_time
                
                if elapsed > 30:  # 30 second timeout
                    logger.warning(f"Clearing stuck activation for {active_id} (elapsed: {elapsed:.1f}s)")
                    delattr(state, '_activation_info')
                elif active_id == character_id:
                    logger.warning(f"Character {character_id} activation already in progress")
                    char_op_lock.release()  # Release lock before returning
                    return {"success": False, "error": "Character activation already in progress", "error_type": "BUSY"}
                else:
                    logger.warning(f"Another character ({active_id}) is being activated")
                    char_op_lock.release()  # Release lock before returning
                    return {"success": False, "error": "Another character is being activated. Please wait.", "error_type": "BUSY"}
            
            # Mark activation as in progress with timestamp
            state._activation_info = (character_id, time.time())
        # Load config outside the lock (I/O operation)
        config_result = load_character_config(character_id)
        if not config_result.get("success", False):
            raise ValueError(f"Failed to load character config: {config_result.get('error', 'Unknown error')}")
        config = config_result.get("result", config_result)
        if not config:
            raise ValueError(f"Character {character_id} not found")
        
        # 非対応モデル(思考を無効化できない Ollama モデル)は会話停止・STT/TTS
        # ロードより前に拒否=失敗しても現在の状態は無傷(2026-08-16 稜裁定。
        # 判定済みならキャッシュで即答・初回のみモデルロード込みのプローブ)
        from backend.llm.ollama_capabilities import ensure_model_supported
        ensure_model_supported(
            config.get("model_provider", "ollama"),
            config.get("model_name", "") or config.get("ollama_model_name", ""),
        )

        # Store conversation state
        was_conversation_active = state.conversation_active if preserve_conversation else False
        
        # Stop active conversation if needed (do this before heavy operations)
        if state.conversation_active and not (preserve_conversation and state.active_character_id == character_id):
            logger.info("Stopping active conversation before switching characters")
            stop_conversation()
            state.image_buffer.clear()
        
        # Prepare new resources outside the lock (slow operations)
        new_resources = {}
        new_resources['config'] = config
        
        # Configure STT language early (before potential failures in other components)
        stt_configured = False
        try:
            stt_lang = config.get("faster_whisper_config", {}).get("language", "en")
            audio_input.configure_stt(language=stt_lang)
            stt_configured = True
            logger.info(f"Configured STT language: {stt_lang}")
        except Exception as e:
            logger.warning(f"STT configuration failed (non-critical): {e}")
        
        # Initialize memory manager (I/O operation)
        db_file = config.get("db_file_path", f"{MEMORY_DIR}/{character_id}.db")
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        
        if character_id not in state.memory_managers:
            logger.info(f"Creating new memory manager for {character_id}")
            new_resources['memory_manager'] = MemoryManager(db_file, character_id)
        else:
            logger.info(f"Reusing existing memory manager for {character_id}")
            new_resources['memory_manager'] = state.memory_managers[character_id]
        
        # Create LLM instance (network operation - slowest part)
        model_provider = config.get("model_provider", "ollama")
        model_name = config.get("model_name", "") or config.get("ollama_model_name", "llama2")
        system_prompt = config.get("system_prompt", "")

        # Get model context window information (only for Ollama models)
        if model_provider == "ollama" and model_name not in state.model_context_cache:
            from backend.llm.ollama_integration import get_model_info, get_default_context_window

            model_info_result = get_model_info(model_name)
            if model_info_result["success"]:
                context_window = model_info_result["model_info"]["context_window"]
                state.model_context_cache[model_name] = context_window
                state.model_info_cache[model_name] = model_info_result["model_info"]
                logger.info(f"Model {model_name} has {context_window} token context window")
            else:
                # Use fallback
                context_window = get_default_context_window(model_name)
                state.model_context_cache[model_name] = context_window
                logger.warning(f"Using default context window for {model_name}: {context_window}")
        elif model_provider != "ollama" and model_name not in state.model_context_cache:
            # External API models: use default context window
            from backend.llm.ollama_integration import get_default_context_window
            context_window = get_default_context_window(model_name)
            state.model_context_cache[model_name] = context_window
            logger.info(f"API model {model_name}: using default context window {context_window}")

        # Store context window for active model
        state.active_model_context_window = state.model_context_cache[model_name]

        # Get tuning parameters from character config
        tuning = config.get("tuning", None)

        from backend.llm.api_integration import create_llm_client
        logger.info(f"Creating LLM instance for conversation with model {model_provider}/{model_name}")
        result = create_llm_client(
            model_provider=model_provider,
            model_name=model_name,
            tuning=tuning,
            usage_type='conversation',
            timeout=OLLAMA_GENERATION_TIMEOUT
        )
        
        if not result.get("success", False):
            # LLM生成失敗は有効化の致命傷にしない(稜裁定 2026-08-03)。
            # 旧実装はここで raise していたが、C6でUIが失敗dictを正しく検出
            # するようになった結果「Ollama停止中はOllamaキャラを開けない」
            # 退行が顕在化した。キャッシュ未設定のまま続行すれば、生成時の
            # 遅延再生成(_ensure_llm_in_cache)が毎回再試行し、失敗は
            # SERVICEエラーとしてユーザーに届く=停止中でも履歴閲覧可・
            # Ollama復帰後は次の送信から自動回復。
            logger.warning(
                f"LLM creation failed during activation (deferred to first "
                f"generation): {result.get('error', 'Unknown error')}")
            new_resources['llm'] = None
        else:
            logger.info("LLM creation result received successfully")
            new_resources['llm'] = result["response"]
        
        # Configure TTS (non-critical, do this after LLM to fail fast on critical stuff)
        tts_configured = False
        # Initialized before the provider branch: the except handler below logs
        # it, and the elevenlabs path never assigns it (NameError guard).
        device = None

        try:
            tts_model_cfg = config.get("tts_model_config", {})
            tts_provider = tts_model_cfg.get("provider", "sbv2")

            if tts_provider == "elevenlabs":
                # API route: no local model, no GPU check, no language handling
                # (multilingual models auto-detect). Frees SBV2+BERT VRAM.
                audio_output.configure_elevenlabs_tts(
                    voice_id=tts_model_cfg.get("voice_id", "")
                )
                tts_configured = True
                logger.info(
                    f"ElevenLabs TTS configured successfully "
                    f"(voice: {tts_model_cfg.get('voice_name') or tts_model_cfg.get('voice_id')})"
                )
            elif tts_provider == "kokoro":
                # ローカル英語ルート: 固定資材(repo直下 kokoro/)+声名のみ。
                # device選択は audio_output 側(CUDAあれば使う・CPU可)。
                audio_output.configure_kokoro_tts(
                    voice_name=tts_model_cfg.get("voice_name", "")
                )
                tts_configured = True
                logger.info(
                    f"Kokoro TTS configured successfully "
                    f"(voice: {tts_model_cfg.get('voice_name')})"
                )
            else:
                try:
                    import torch
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except ImportError:
                    device = "cpu"
                    logger.debug("Torch not available, defaulting to CPU for TTS")
                # SBV2 は ja 専用(言語決定ロジックは撤去済み・推論は常に JP)
                audio_output.configure_tts_model(
                    model_path=tts_model_cfg.get("model_path", ""),
                    style_vectors_path=tts_model_cfg.get("style_vectors_path", ""),
                    config_path=tts_model_cfg.get("config_path", ""),
                    device=device
                )
                tts_configured = True
                logger.info("TTS model configured successfully (SBV2, ja)")
        except Exception as e:
            logger.warning(f"TTS configuration failed (non-critical): {e}")
            logger.debug(f"TTS config details - model_path: {tts_model_cfg.get('model_path', 'NOT SET')}")
            logger.debug(f"TTS config details - device: {device}")
        
        # Now acquire lock for quick state update only
        with state.activation_lock:
            # Clean up old LLM instances if cache is full
            if len(state.active_llm_cache) >= state.MAX_LLM_CACHE_SIZE:
                # Move current character to end (most recently used)
                if character_id in state.active_llm_cache:
                    state.active_llm_cache.move_to_end(character_id)
                
                # Remove oldest entries until we have room
                while len(state.active_llm_cache) >= state.MAX_LLM_CACHE_SIZE:
                    oldest_id, oldest_llm = state.active_llm_cache.popitem(last=False)
                    logger.info(f"Evicted LLM cache for character {oldest_id} (LRU)")
                    # Clean up if possible
                    if hasattr(oldest_llm, 'cleanup') and callable(oldest_llm.cleanup):
                        try:
                            oldest_llm.cleanup()
                        except Exception as e:
                            logger.warning(f"Error cleaning up LLM for {oldest_id}: {e}")
                    
                    # Also ensure client connection is closed if it exists
                    if hasattr(oldest_llm, 'client'):
                        if hasattr(oldest_llm.client, 'close') and callable(oldest_llm.client.close):
                            try:
                                oldest_llm.client.close()
                                logger.debug(f"Closed client connection for evicted LLM {oldest_id}")
                            except Exception as e:
                                logger.warning(f"Error closing client connection for {oldest_id}: {e}")
            
            # Update state atomically (fast operations only)
            state.active_character_id = character_id
            state.memory_managers[character_id] = new_resources['memory_manager']
            if new_resources.get('llm') is not None:
                state.active_llm_cache[character_id] = new_resources['llm']
                # Assignment keeps an existing key at its old LRU position —
                # re-activation must land at the MRU end or eviction can hit it.
                state.active_llm_cache.move_to_end(character_id)
            else:
                # 生成失敗時は旧キャラのLLMを掴まされないよう必ず除去
                # (遅延再生成が正しいconfigで作り直す)
                state.active_llm_cache.pop(character_id, None)
            
            # Handle conversation state
            if preserve_conversation and was_conversation_active:
                state.conversation_active = True
            else:
                state.conversation_active = False
            
            # Clear activation flag
            if hasattr(state, '_activation_info') and state._activation_info[0] == character_id:
                delattr(state, '_activation_info')
        
        logger.info(f"Successfully activated character {character_id}")
        
        # Return success with activation details
        return {
            "success": True,
            "character_id": character_id,
            "message": f"Character {character_id} activated successfully",
            "details": {
                "conversation_active": state.conversation_active,
                "stt_configured": stt_configured,
                "tts_configured": tts_configured
            }
        }
        
    except Exception as e:
        # Clear activation flag on error
        with state.activation_lock:
            if hasattr(state, '_activation_info') and state._activation_info[0] == character_id:
                delattr(state, '_activation_info')
        
        if getattr(e, "ag_code", None):
            # コード付きの拒否(非対応モデル等)はユーザー向けに UI が翻訳トーストを
            # 出す。ERROR にすると WS 転送で英語トーストが重なる(2026-08-02 の
            # 「同内容トースト2枚」)ため warning 止まり
            logger.warning(f"Character activation refused for {character_id}: {e}")
        else:
            logger.error(f"Failed to activate character {character_id}: {e}")
        # Let standardize_response handle the exception
        raise
    finally:
        # Always release the character operation lock
        char_op_lock.release()


# --------------------------------------------------------------------------
# Talk Theme Management (moved from backend.py — B10 / SL4 Character)
#
# Injection-style module functions. The backend state container (toggle-flag /
# active-character / memory-managers owner) is injected as `state` (was the
# module-global `_backend_state`). Character config helpers (load_character_config,
# _save_config_file, _get_config_filepath, _character_lock, _character_config_cache,
# update_character_cache) are now same-module direct references. The websocket
# manager stays a call-time import to avoid an import cycle. Behaviour preserved
# verbatim — backend.py keeps thin @standardize_response delegates.
# --------------------------------------------------------------------------


def get_talk_theme(state, character_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get the current talk theme for a character.

    Args:
        character_id: Character ID. If None, uses active character.

    Returns:
        Dict with theme information
    """
    # Use active character if not specified
    if character_id is None:
        character_id = state.active_character_id

    if not character_id:
        return {
            "success": False,
            "error": "No character selected",
            "error_type": "NO_CHARACTER"
        }

    try:
        # Load character config to get talk theme
        config = load_character_config(character_id)

        if not config:
            return {
                "success": False,
                "error": f"Character config not found: {character_id}",
                "error_type": "NOT_FOUND"
            }

        talk_theme = config.get("talk_theme", "")

        return {
            "success": True,
            "theme": talk_theme,
            "character_id": character_id,
            "character_name": config.get("name", "Unknown")
        }
    except Exception as e:
        logger.error(f"Error getting talk theme: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "INTERNAL"
        }


def update_talk_theme(state, theme: str, character_id: Optional[str] = None, source: str = "user") -> Dict[str, Any]:
    """
    Update the talk theme for a character.

    Args:
        theme: New talk theme (can be empty string to clear)
        character_id: Character ID. If None, uses active character.
        source: Source of the update ("user" or "ai")

    Returns:
        Dict with update status
    """
    # Use active character if not specified
    if character_id is None:
        character_id = state.active_character_id

    if not character_id:
        return {
            "success": False,
            "error": "No character selected",
            "error_type": "NO_CHARACTER"
        }

    # Check if conversation is started
    if not state.conversation_active:
        return {
            "success": False,
            "error": "Conversation not started. Please start a conversation first.",
            "error_type": "VALIDATION"
        }

    try:
        with _character_lock:
            # Load current config
            config = load_character_config(character_id)

            if not config:
                return {
                    "success": False,
                    "error": f"Character config not found: {character_id}",
                    "error_type": "NOT_FOUND"
                }

            # Validate theme length (Ollama only — API providers have no limit)
            model_provider = config.get("model_provider", "ollama")
            if model_provider == "ollama" and len(theme) > 300:
                return {
                    "success": False,
                    "error": "Talk theme must be 300 characters or less",
                    "error_type": "VALIDATION"
                }

            # Update talk theme
            old_theme = config.get("talk_theme", "")
            config["talk_theme"] = theme

            # Get config file path
            config_path = _get_config_filepath(character_id)

            # Save updated config
            _save_config_file(Path(config_path), config)

            # Update cache (both backend and character_manager caches)
            _character_config_cache[character_id] = config.copy()

            # Also update character_manager's cache to keep it in sync
            update_character_cache(character_id, config)

            logger.info(f"[TalkTheme] Updated theme for {character_id} from '{old_theme}' to '{theme}' (source: {source})")

        # Add feedback message to conversation if theme changed
        if theme != old_theme and state.memory_managers.get(character_id):
            memory_manager = state.memory_managers[character_id]
            character_name = config.get("name", "キャラクター")
            from backend.shared.prompt_i18n import get_prompt_language
            language = get_prompt_language(config)

            if source == "user":
                if theme:  # Theme was set
                    if language == "en":
                        feedback_msg = f"(User has set the talk theme to \"{theme}\".)"
                    else:
                        feedback_msg = f"（ユーザーが、トークテーマとして「{theme}」を設定しました。）"
                else:  # Theme was cleared
                    if language == "en":
                        feedback_msg = "(User has cleared the talk theme.)"
                    else:
                        feedback_msg = "（ユーザーが、トークテーマをクリアしました。）"

                # Add as system message
                memory_manager.add_message("system", feedback_msg, metadata={"type": "theme_feedback", "source": source})

        # Phase 4B: broadcast talk_theme_updated (snapshot s90).
        # Replaces the 2s polling timer in ui/app.py with event-driven push.
        try:
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_talk_theme_updated_sync(theme, character_id)
        except Exception as broadcast_err:
            logger.warning(f"[TalkTheme] broadcast failed: {broadcast_err}")

        return {
            "success": True,
            "theme": theme,
            "old_theme": old_theme,
            "character_id": character_id,
            "character_name": config.get("name", "Unknown")
        }

    except Exception as e:
        logger.error(f"Error updating talk theme: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "INTERNAL"
        }


def clear_talk_theme(state, character_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Clear the talk theme for a character.

    Args:
        character_id: Character ID. If None, uses active character.

    Returns:
        Dict with clear status
    """
    # Simply call update with empty string
    return update_talk_theme(state, "", character_id, source="user")
