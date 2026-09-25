# api_settings.py
"""
API Settings management module for Artificial Girlfriend.

Handles reading/writing api_settings.json, fetching model lists
from external API providers (OpenAI, Anthropic, xAI, Google),
and providing a unified model list for character configuration.
"""

import json
import logging
import threading
import requests
from typing import Dict, List, Tuple, Optional, Any

from backend.shared.constants import API_SETTINGS_FILE

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# Provider configuration
PROVIDER_CONFIG = {
    "openai": {
        "display_name": "ChatGPT",
        "models_url": "https://api.openai.com/v1/models",
        "auth_type": "bearer",
    },
    "anthropic": {
        "display_name": "Claude",
        "models_url": "https://api.anthropic.com/v1/models",
        "auth_type": "anthropic",
    },
    "xai": {
        "display_name": "Grok",
        "models_url": "https://api.x.ai/v1/models",
        "auth_type": "bearer",
    },
    "google": {
        "display_name": "Gemini",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "auth_type": "query_param",
    },
    "ollama": {
        "display_name": "Ollama",
    },
    # TTS provider (no models_url: voice/model lists use dedicated endpoints below)
    "elevenlabs": {
        "display_name": "ElevenLabs",
    },
}

DEFAULT_SETTINGS = {
    "openai": {
        "api_key": "",
        "web_search_enabled": False,
        "available_models": []
    },
    "anthropic": {
        "api_key": "",
        "web_search_enabled": False,
        "available_models": []
    },
    "xai": {
        "api_key": "",
        "web_search_enabled": False,
        "x_search_enabled": False,
        "available_models": []
    },
    "google": {
        "api_key": "",
        "web_search_enabled": False,
        "available_models": []
    },
    "elevenlabs": {
        "api_key": "",
        "available_voices": [],   # [{"voice_id": str, "name": str, "category": str}]
        "available_models": [],   # [{"model_id": str, "name": str, "cost_factor": float}]
        "model_id": "eleven_multilingual_v2"
    }
}

# Request timeout for API calls (seconds)
API_REQUEST_TIMEOUT = 30

# Thread safety
_settings_lock = threading.RLock()

# mtime-validated cache: load_api_settings is called several times per
# conversation turn (tool gates, per-request API setup, settings getters);
# re-reading and re-parsing the file each time is wasted disk I/O. The cache
# is keyed on the file's mtime_ns, so external edits are still picked up,
# and save_api_settings invalidates it. Callers receive a deep copy so their
# mutations never leak into the cache.
_settings_cache: Optional[Dict[str, Any]] = None
_settings_cache_mtime: Optional[int] = None


# ============================================================
# Settings File I/O
# ============================================================

def load_api_settings() -> Dict[str, Any]:
    """Load API settings from file. Creates default if not exists.

    Returns:
        Dict containing API settings for all providers.
    """
    global _settings_cache, _settings_cache_mtime
    with _settings_lock:
        try:
            if API_SETTINGS_FILE.exists():
                mtime = API_SETTINGS_FILE.stat().st_mtime_ns
                if _settings_cache is not None and mtime == _settings_cache_mtime:
                    return json.loads(json.dumps(_settings_cache))

                # utf-8-sig: tolerate a BOM — users hand-edit this file to
                # paste API keys, and a BOM would otherwise send the whole
                # file to .corrupt quarantine (= keys "vanish").
                with open(API_SETTINGS_FILE, 'r', encoding='utf-8-sig') as f:
                    settings = json.load(f)

                # Ensure all providers exist (in case of file from older version)
                for provider, defaults in DEFAULT_SETTINGS.items():
                    if provider not in settings:
                        settings[provider] = defaults.copy()
                    else:
                        # Ensure all fields exist for each provider
                        for key, default_value in defaults.items():
                            if key not in settings[provider]:
                                settings[provider][key] = default_value

                # embedding_model はデフォルト補填しない(稜裁定 2026-07-25:
                # デフォルト埋め込み廃止=「未設定」を実在させ、会話開始ガードが
                # 設定を促す。2026-07-19の「デフォルトはollama維持」裁定を上書き)。
                # 既存インストールは補填済みの値がそのまま残るため無影響。

                # Ensure web_search_blacklist exists
                if "web_search_blacklist" not in settings:
                    settings["web_search_blacklist"] = []

                # Ensure image_blacklist exists
                if "image_blacklist" not in settings:
                    settings["image_blacklist"] = []

                # Ensure google_maps_api_key exists
                if "google_maps_api_key" not in settings:
                    settings["google_maps_api_key"] = ""

                _settings_cache = json.loads(json.dumps(settings))
                _settings_cache_mtime = mtime
                return settings
            else:
                # Create default settings file
                settings = _deep_copy_defaults()
                save_api_settings(settings)
                logger.info(f"Created default API settings file: {API_SETTINGS_FILE}")
                return settings
        except json.JSONDecodeError as e:
            # Do NOT overwrite the file with defaults here: a truncated/half-written
            # JSON (crash/power loss/disk full mid-write) would otherwise wipe every
            # provider's API key permanently. Quarantine the corrupt file to
            # .corrupt.<ts> so it can be recovered, then return in-memory defaults
            # WITHOUT persisting them.
            logger.error(f"Failed to parse API settings file: {e}")
            try:
                import os
                import time
                corrupt_path = f"{API_SETTINGS_FILE}.corrupt.{int(time.time())}"
                os.replace(str(API_SETTINGS_FILE), corrupt_path)
                logger.warning(f"Quarantined corrupt API settings to {corrupt_path}")
            except Exception as move_err:
                logger.error(f"Could not quarantine corrupt API settings: {move_err}")
            return _deep_copy_defaults()
        except Exception as e:
            logger.error(f"Failed to load API settings: {e}")
            return _deep_copy_defaults()


def save_api_settings(settings: Dict[str, Any]) -> None:
    """Save API settings to file.

    Args:
        settings: Complete settings dict to save.
    """
    global _settings_cache, _settings_cache_mtime
    with _settings_lock:
        # Invalidate the load cache whether or not the write succeeds — a
        # failed write may still have changed the file on disk.
        _settings_cache = None
        _settings_cache_mtime = None
        try:
            # Atomic write (tmp + os.replace, same pattern as note_io.py): a
            # crash mid-write must not leave a half-written JSON that the next
            # load can't parse (→ would strand all API keys).
            import os
            tmp_path = f"{API_SETTINGS_FILE}.tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(API_SETTINGS_FILE))
            logger.debug("API settings saved successfully")
        except Exception as e:
            logger.error(f"Failed to save API settings: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise


def _deep_copy_defaults() -> Dict[str, Any]:
    """Create a deep copy of DEFAULT_SETTINGS."""
    return json.loads(json.dumps(DEFAULT_SETTINGS))


# ============================================================
# Individual Setting Operations
# ============================================================

def save_api_key(provider: str, key: str) -> Dict[str, Any]:
    """Save API key for a specific provider.

    Args:
        provider: Provider identifier (openai, anthropic, xai, google).
        key: The API key string.

    Returns:
        Result dict with success status and message.
    """
    if provider not in DEFAULT_SETTINGS:
        return {"success": False, "message": f"Unknown provider: {provider}"}

    try:
        settings = load_api_settings()
        settings[provider]["api_key"] = key.strip()
        save_api_settings(settings)

        display_name = PROVIDER_CONFIG[provider]["display_name"]
        if key.strip():
            logger.info(f"API key saved for {display_name}")
            return {"success": True, "message": f"{display_name} API key saved."}
        else:
            logger.info(f"API key cleared for {display_name}")
            return {"success": True, "message": f"{display_name} API key cleared."}
    except Exception as e:
        logger.error(f"Failed to save API key for {provider}: {e}")
        return {"success": False, "message": f"Failed to save: {e}"}


def update_search_setting(provider: str, setting_name: str, value: bool) -> Dict[str, Any]:
    """Update a search toggle setting for a provider.

    Args:
        provider: Provider identifier.
        setting_name: Setting name (web_search_enabled, x_search_enabled).
        value: Boolean value.

    Returns:
        Result dict with success status.
    """
    if provider not in DEFAULT_SETTINGS:
        return {"success": False, "message": f"Unknown provider: {provider}"}

    valid_settings = {"web_search_enabled", "x_search_enabled"}
    if setting_name not in valid_settings:
        return {"success": False, "message": f"Unknown setting: {setting_name}"}

    try:
        settings = load_api_settings()
        settings[provider][setting_name] = bool(value)
        save_api_settings(settings)

        display_name = PROVIDER_CONFIG[provider]["display_name"]
        logger.info(f"{display_name} {setting_name} set to {value}")
        return {"success": True, "message": "Setting updated."}
    except Exception as e:
        logger.error(f"Failed to update {setting_name} for {provider}: {e}")
        return {"success": False, "message": f"Failed to update: {e}"}


# ============================================================
# Model List Fetching
# ============================================================

# 生死プローブ(空:predict)の1モデルあたりタイムアウト。imagen候補は数個
# なので更新1回あたり最悪でも十数秒・通常は数秒で終わる。
IMAGEN_PROBE_TIMEOUT = 10


def _drop_dead_imagen_models(models: List[str], api_key: str) -> List[str]:
    """Googleのモデル一覧から、実行すると404になる画像生成(imagen)モデルを除く。

    ListModels はこのアカウントで実行できない提供終了モデルも生きたモデルと
    同一メタデータで広告するため、名前でもメタデータでも判別できない
    (2026-07-30実測: imagen-4.0-fast/ultra が「no longer available to new
    users」の404を返すのに一覧には載り続ける)。空ペイロードの :predict は
    生存モデルなら 400「Empty instances.」・提供終了なら 404 を返し、画像は
    生成されない=課金ゼロ。404 だけを除外し、ネットワーク失敗等の不確実は
    温存する(誤除外は「使えるモデルが消えた」に見えるため安全側へ)。
    """
    kept = []
    for name in models:
        if "imagen" not in name.lower():
            kept.append(name)
            continue
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{name}:predict",
                json={},
                headers={"x-goog-api-key": api_key},
                timeout=IMAGEN_PROBE_TIMEOUT,
            )
            if response.status_code == 404:
                logger.info(f"Dropping unusable imagen model (404 on probe): {name}")
                continue
        except requests.exceptions.RequestException:
            pass
        kept.append(name)
    return kept


def fetch_provider_models(provider: str) -> Dict[str, Any]:
    """Fetch available models from a provider's API and save to settings.

    Args:
        provider: Provider identifier.

    Returns:
        Result dict with success status, message, and models list.
    """
    if provider not in PROVIDER_CONFIG or "models_url" not in PROVIDER_CONFIG[provider]:
        return {"success": False, "message": f"Cannot fetch models for: {provider}", "models": []}

    settings = load_api_settings()
    api_key = settings.get(provider, {}).get("api_key", "")

    if not api_key:
        display_name = PROVIDER_CONFIG[provider]["display_name"]
        return {
            "success": False,
            "message": f"{display_name} API key is not set.",
            "models": []
        }

    try:
        models = _fetch_models_from_api(provider, api_key)

        if provider == "google":
            models = _drop_dead_imagen_models(models, api_key)

        # Save to settings
        settings[provider]["available_models"] = models

        # 選択済み画像生成モデルがリストから消えた場合は解除する — 残すと
        # 死んだモデルを黙って指し続け、生成が毎回404で落ちる(稜Macテスト
        # 2026-07-30: 旧リストのimagen-4.0-fast/ultraが提供終了)。呼出側が
        # enforce_feature_availability を再実行してゲートを畳む。
        ig_encoded = settings.get("image_generation_model", "")
        if ig_encoded:
            ig_provider, ig_model = decode_model_value(ig_encoded)
            if ig_provider == provider and ig_model not in models:
                logger.info(
                    f"Clearing image generation model no longer available: {ig_model}"
                )
                settings["image_generation_model"] = ""

        save_api_settings(settings)

        display_name = PROVIDER_CONFIG[provider]["display_name"]
        logger.info(f"Fetched {len(models)} models from {display_name}")
        return {
            "success": True,
            "message": f"{len(models)} models found.",
            "models": models
        }
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "unknown"
        if status_code == 401:
            msg = "API key is invalid (401 Unauthorized)."
        elif status_code == 403:
            msg = "Access denied (403 Forbidden)."
        else:
            msg = f"HTTP error: {status_code}"
        logger.error(f"Failed to fetch models for {provider}: {msg}")
        return {"success": False, "message": msg, "models": []}
    except requests.exceptions.ConnectionError:
        msg = "Cannot connect to API server."
        logger.error(f"Failed to fetch models for {provider}: {msg}")
        return {"success": False, "message": msg, "models": []}
    except requests.exceptions.Timeout:
        msg = "Request timed out."
        logger.error(f"Failed to fetch models for {provider}: {msg}")
        return {"success": False, "message": msg, "models": []}
    except Exception as e:
        msg = f"Unexpected error: {e}"
        logger.error(f"Failed to fetch models for {provider}: {msg}")
        return {"success": False, "message": msg, "models": []}


def _fetch_models_from_api(provider: str, api_key: str,
                           timeout: int = API_REQUEST_TIMEOUT) -> List[str]:
    """Internal: Make HTTP request to fetch models from a provider.

    Args:
        provider: Provider identifier.
        api_key: The API key.
        timeout: Request timeout in seconds (probes pass a shorter one).

    Returns:
        List of model name strings.

    Raises:
        requests.exceptions.HTTPError: On HTTP errors.
        requests.exceptions.ConnectionError: On connection errors.
        requests.exceptions.Timeout: On timeout.
    """
    config = PROVIDER_CONFIG[provider]
    url = config["models_url"]
    auth_type = config["auth_type"]

    headers = {"Content-Type": "application/json"}
    params = {}

    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif auth_type == "query_param":
        params["key"] = api_key

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=timeout
    )
    response.raise_for_status()

    data = response.json()
    return _extract_model_names(provider, data)


def _extract_model_names(provider: str, data: Dict[str, Any]) -> List[str]:
    """Extract model name strings from API response.

    Each provider returns models in a different format.

    Args:
        provider: Provider identifier.
        data: Raw API response dict.

    Returns:
        Sorted list of model name strings.
    """
    models = []

    if provider == "google":
        # Gemini: {"models": [{"name": "models/gemini-2.0-flash", ...}, ...]}
        for model in data.get("models", []):
            name = model.get("name", "")
            # Strip "models/" prefix
            if name.startswith("models/"):
                name = name[len("models/"):]
            if name:
                models.append(name)
    else:
        # OpenAI, Anthropic, xAI: {"data": [{"id": "gpt-4o", ...}, ...]}
        for model in data.get("data", []):
            model_id = model.get("id", "")
            if model_id:
                models.append(model_id)

    models.sort()
    return models


# ============================================================
# Unified Model List for Character Dropdown
# ============================================================

def get_all_available_models(ollama_models: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """Get unified model list for character create/edit dropdown.

    Combines Ollama models with API models from providers that have
    valid API keys configured.

    Args:
        ollama_models: List of Ollama model names. If None, returns only API models.

    Returns:
        List of (display_label, encoded_value) tuples.
        encoded_value format: "provider::model_name"
    """
    choices = []

    # Add API models (only from providers with API keys set)
    settings = load_api_settings()
    for provider in ["openai", "anthropic", "xai", "google"]:
        provider_settings = settings.get(provider, {})
        api_key = provider_settings.get("api_key", "")
        available_models = provider_settings.get("available_models", [])

        if api_key and available_models:
            display_name = PROVIDER_CONFIG[provider]["display_name"]
            for model_name in available_models:
                label = f"{model_name} ({display_name})"
                value = f"{provider}::{model_name}"
                choices.append((label, value))

    # Add Ollama models (capability注記つき=C9・稜裁定 2026-08-11)。
    # このリスト構築がcapability先読みの主判定点: 未確認・digest変更モデルを
    # ここで照会してキャッシュを温める(会話開始時に判定済みを保証)。
    # 呼出時import=公認継ぎ目①(sharedからllmへのロード時依存を作らない)。
    if ollama_models:
        try:
            from backend.llm.ollama_capabilities import get_caps, prefetch
            prefetch(ollama_models)
        except Exception:
            logger.exception("Ollama capability prefetch failed; listing without annotations")
            get_caps = None
        for model_name in ollama_models:
            caps = get_caps(model_name) if get_caps else {}
            # embedding専用等、completion capabilityが無いモデルはチャット
            # 不能=会話一覧に出さない（稜裁定 2026-08-14）。判定不能
            # (known=False)は従来どおり掲載（旧Ollamaの正規モデルを隠さない）
            if caps.get("known") and not caps.get("completion"):
                continue
            if caps.get("known"):
                tags = [t for t, on in (("tools", caps.get("tools")),
                                        ("vision", caps.get("vision"))) if on]
                suffix = "+".join(tags) if tags else "text-only"
                label = f"{model_name} (Ollama, {suffix})"
            else:
                label = f"{model_name} (Ollama)"
            value = f"ollama::{model_name}"
            choices.append((label, value))

    return choices


def get_embedding_model_choices() -> List[Tuple[str, str]]:
    """Model choices for the Settings > Embedding Model dropdown ONLY.

    Unlike get_all_available_models (character LLM selection, which only
    excludes non-completion models such as embedding-only ones),
    this narrows to embedding-capable models: picking a chat model here does
    not error out — Ollama happily returns garbage-quality "embeddings" from
    chat models — so the gate is at the entrance (ST6 §7-4 sequel).

    - openai / google: keep models whose name suggests embeddings ("embed").
    - anthropic: no embedding models — provider excluded.
    - xai: no embedding models at present — provider excluded.
    - ollama: capability "embedding" via ollama_capabilities.get_caps
      (persistent cache; prefetch handles re-pull/digest changes), with the
      name heuristic as fallback when capability metadata is unavailable
      (older Ollama, server down).

    Returns:
        List of (display_label, "provider::model") tuples.
    """
    choices = []

    settings = load_api_settings()
    for provider in ["openai", "google"]:
        provider_settings = settings.get(provider, {})
        api_key = provider_settings.get("api_key", "")
        available_models = provider_settings.get("available_models", [])
        if api_key and available_models:
            display_name = PROVIDER_CONFIG[provider]["display_name"]
            for model_name in available_models:
                if "embed" not in model_name.lower():
                    continue
                choices.append((f"{model_name} ({display_name})", f"{provider}::{model_name}"))

    try:
        from backend.llm.ollama_integration import list_ollama_models
        ollama_models, _ = list_ollama_models()
    except Exception:
        ollama_models = []
    if ollama_models:
        # capability照会は永続キャッシュつき真実源へ（会話一覧
        # get_all_available_modelsと同じ様式）。prefetchが再pull（digest変化）
        # 検知と負キャッシュ解除を担う＝キャッシュ統一で鮮度を落とさない
        try:
            from backend.llm.ollama_capabilities import get_caps, prefetch
            prefetch(ollama_models)
        except Exception:
            logger.exception(
                "Ollama capability prefetch failed; using name heuristic")
            get_caps = None
        for model_name in ollama_models:
            caps = get_caps(model_name) if get_caps else {}
            if caps.get("known"):
                if not caps.get("embedding"):
                    continue
            elif "embed" not in model_name.lower():
                continue
            choices.append((f"{model_name} (Ollama)", f"ollama::{model_name}"))

    return choices


def encode_model_value(provider: str, model_name: str) -> str:
    """Encode provider and model name into dropdown value format.

    Args:
        provider: Provider identifier.
        model_name: Model name string.

    Returns:
        Encoded string "provider::model_name".
    """
    return f"{provider}::{model_name}"


def decode_model_value(encoded_value: str) -> Tuple[str, str]:
    """Decode dropdown value into provider and model name.

    Args:
        encoded_value: Encoded string "provider::model_name".

    Returns:
        Tuple of (provider, model_name).
    """
    if "::" in encoded_value:
        parts = encoded_value.split("::", 1)
        return parts[0], parts[1]
    else:
        # Fallback: assume Ollama for unencoded values
        return "ollama", encoded_value


def get_provider_display_name(provider: str) -> str:
    """Get the display name for a provider.

    Args:
        provider: Provider identifier.

    Returns:
        Human-readable display name.
    """
    return PROVIDER_CONFIG.get(provider, {}).get("display_name", provider)


# ============================================================
# Embedding Model Settings
# ============================================================

def get_embedding_model() -> Optional[Tuple[str, str]]:
    """Get the currently configured embedding model.

    Returns:
        Tuple of (provider, model_name), or None when not configured
        (デフォルト補填は2026-07-25に廃止 — 呼び手はNoneを明示処理すること。
        会話開始はconversation_managerのガードがブロックし、記憶系は
        graceful degrade)。
    """
    settings = load_api_settings()
    encoded = settings.get("embedding_model") or ""
    if not encoded:
        return None
    return decode_model_value(encoded)


def save_embedding_model(encoded_value: str) -> Dict[str, Any]:
    """Save embedding model setting.

    Args:
        encoded_value: Encoded string "provider::model_name".

    Returns:
        Result dict with success status and message.
    """
    provider, model_name = decode_model_value(encoded_value)
    settings = load_api_settings()
    settings["embedding_model"] = encoded_value
    save_api_settings(settings)
    display_name = get_provider_display_name(provider)
    return {"success": True, "message": f"Embedding model saved: {model_name} ({display_name})"}


# Models we already warned about missing calibration (one warning per model
# per process; memory search runs every turn and must not spam the log).
_uncalibrated_warned = set()


def save_embedding_threshold(encoded_value: str, calibration: Dict[str, Any]) -> None:
    """Persist a calibrated memory-relevance threshold for an embedding model.

    Args:
        encoded_value: Encoded string "provider::model_name".
        calibration: Result dict from
            `backend.memory.embedding_calibration.calibrate_embedding_model`.
    """
    from datetime import datetime

    provider, model_name = decode_model_value(encoded_value)
    key = f"{provider}::{model_name}"
    with _settings_lock:
        settings = load_api_settings()
        thresholds = settings.setdefault("embedding_thresholds", {})
        thresholds[key] = {
            "threshold": calibration["threshold"],
            "corpus_version": calibration.get("corpus_version"),
            "calibrated_at": datetime.now().isoformat(timespec="seconds"),
            "noise_pass": calibration.get("noise_pass"),
            "rel_range": [calibration.get("rel_min"), calibration.get("rel_max")],
            "irr_range": [calibration.get("irr_min"), calibration.get("irr_max")],
        }
        save_api_settings(settings)


def get_memory_relevance_threshold() -> float:
    """Resolve the memory-relevance cosine threshold for the current embedding model.

    Cosine scales differ per embedding model (ST6 §7-4): a fixed 0.3
    (nomic-embed-text calibration) silently disabled long-term memory on
    text-embedding-3-large. Resolution order:
      1. calibrated value persisted by the settings-UI calibration
         (`embedding_thresholds` in api_settings.json)
      2. shipped seed for known models (constants.MIN_MEMORY_RELEVANCE_SEEDS)
      3. generic default MIN_MEMORY_RELEVANCE, with a one-time warning —
         re-saving the embedding model in Settings calibrates it.
    """
    from backend.shared.constants import MIN_MEMORY_RELEVANCE, MIN_MEMORY_RELEVANCE_SEEDS

    settings = load_api_settings()
    encoded = settings.get("embedding_model") or ""
    if not encoded:
        # 未設定(2026-07-25以降の新規インストール): 検索経路は縮退済みの
        # ため、しきい値は既定値を静かに返す(警告スパム不要)。
        return MIN_MEMORY_RELEVANCE
    provider, model_name = decode_model_value(encoded)
    key = f"{provider}::{model_name}"

    entry = settings.get("embedding_thresholds", {}).get(key)
    if isinstance(entry, dict):
        value = entry.get("threshold")
        if isinstance(value, (int, float)):
            return float(value)

    if key in MIN_MEMORY_RELEVANCE_SEEDS:
        return MIN_MEMORY_RELEVANCE_SEEDS[key]

    if key not in _uncalibrated_warned:
        _uncalibrated_warned.add(key)
        logger.warning(
            f"No calibrated memory-relevance threshold for embedding model "
            f"'{key}'; using default {MIN_MEMORY_RELEVANCE}. Re-save the "
            f"embedding model in Settings to calibrate."
        )
    return MIN_MEMORY_RELEVANCE


# ============================================================
# Web Search Blacklist
# ============================================================

def get_web_search_blacklist() -> List[str]:
    """Get the web search blacklist.

    Returns:
        List of encoded "provider::model_name" strings.
    """
    settings = load_api_settings()
    return settings.get("web_search_blacklist", [])


def add_to_web_search_blacklist(provider: str, model: str) -> None:
    """Add a model to the web search blacklist.

    Thread-safe: wraps read-modify-write in a single lock acquisition.

    Args:
        provider: Provider identifier (openai, anthropic, xai, google).
        model: Model name string.
    """
    with _settings_lock:
        settings = load_api_settings()
        blacklist = settings.get("web_search_blacklist", [])
        encoded = f"{provider}::{model}"
        if encoded not in blacklist:
            blacklist.append(encoded)
            settings["web_search_blacklist"] = blacklist
            save_api_settings(settings)
            logger.info(f"Added to web search blacklist: {encoded}")


def remove_from_web_search_blacklist(encoded_values: List[str]) -> None:
    """Remove models from the web search blacklist.

    Args:
        encoded_values: List of "provider::model_name" strings to remove.
    """
    with _settings_lock:
        settings = load_api_settings()
        blacklist = settings.get("web_search_blacklist", [])
        settings["web_search_blacklist"] = [
            item for item in blacklist if item not in encoded_values
        ]
        save_api_settings(settings)
        logger.info(f"Removed from web search blacklist: {encoded_values}")


# ============================================================
# Image Blacklist
# ============================================================

def get_image_blacklist() -> List[str]:
    """Get the image input blacklist.

    Returns:
        List of encoded "provider::model_name" strings.
    """
    settings = load_api_settings()
    return settings.get("image_blacklist", [])


def add_to_image_blacklist(provider: str, model: str) -> None:
    """Add a model to the image input blacklist.

    Thread-safe: wraps read-modify-write in a single lock acquisition.

    Args:
        provider: Provider identifier (openai, anthropic, xai, google).
        model: Model name string.
    """
    with _settings_lock:
        settings = load_api_settings()
        blacklist = settings.get("image_blacklist", [])
        encoded = f"{provider}::{model}"
        if encoded not in blacklist:
            blacklist.append(encoded)
            settings["image_blacklist"] = blacklist
            save_api_settings(settings)
            logger.info(f"Added to image blacklist: {encoded}")


def remove_from_image_blacklist(encoded_values: List[str]) -> None:
    """Remove models from the image input blacklist.

    Args:
        encoded_values: List of "provider::model_name" strings to remove.
    """
    with _settings_lock:
        settings = load_api_settings()
        blacklist = settings.get("image_blacklist", [])
        settings["image_blacklist"] = [
            item for item in blacklist if item not in encoded_values
        ]
        save_api_settings(settings)
        logger.info(f"Removed from image blacklist: {encoded_values}")


# ============================================================
# Image Generation Model Settings
# ============================================================

def get_google_maps_api_key() -> str:
    """Get the Google Maps API key for reverse geocoding.

    Returns:
        The API key string, or empty string if not set.
    """
    settings = load_api_settings()
    return settings.get("google_maps_api_key", "")


def save_google_maps_api_key(key: str) -> Dict[str, Any]:
    """Save Google Maps API key.

    Args:
        key: The Google Maps API key string.

    Returns:
        Result dict with success status and message.
    """
    settings = load_api_settings()
    settings["google_maps_api_key"] = key.strip()
    save_api_settings(settings)
    if key.strip():
        return {"success": True, "message": "Google Maps API key saved."}
    else:
        return {"success": True, "message": "Google Maps API key cleared."}


def get_image_generation_model() -> Tuple[str, str]:
    """Get the currently configured image generation model.

    Returns:
        Tuple of (provider, model_name). Empty strings if not set.
    """
    settings = load_api_settings()
    encoded = settings.get("image_generation_model", "")
    if not encoded:
        return "", ""
    return decode_model_value(encoded)


def save_image_generation_model(encoded_value: str) -> Dict[str, Any]:
    """Save image generation model setting.

    Args:
        encoded_value: Encoded string "provider::model_name".

    Returns:
        Result dict with success status and message.
    """
    provider, model_name = decode_model_value(encoded_value)
    settings = load_api_settings()
    settings["image_generation_model"] = encoded_value
    save_api_settings(settings)
    display_name = get_provider_display_name(provider)
    return {"success": True, "message": f"Image generation model saved: {model_name} ({display_name})"}


OLLAMA_NUM_CTX_DEFAULT = 32000
OLLAMA_NUM_CTX_CHOICES = (8000, 12000, 16000, 24000, 32000, 48000, 64000)


def get_ollama_num_ctx() -> int:
    """Ollama会話のコンテキスト窓設定（トークン・既定32000=稜裁定2026-08-11）。

    実際にモデルへ渡す値はモデルのネイティブ文脈長でクランプされる
    （ollama_capabilities.get_effective_num_ctx が真実源の読み手）。
    """
    settings = load_api_settings()
    try:
        value = int(settings.get("ollama_num_ctx", OLLAMA_NUM_CTX_DEFAULT))
    except (TypeError, ValueError):
        return OLLAMA_NUM_CTX_DEFAULT
    # 手編集の異常値はロードせず既定へ縮退（2048未満は実用不能）
    return value if value >= 2048 else OLLAMA_NUM_CTX_DEFAULT


def save_ollama_num_ctx(value: Any) -> Dict[str, Any]:
    """Save the Ollama context window setting.

    Returns:
        Result dict with success status and message.
    """
    try:
        num_ctx = int(value)
    except (TypeError, ValueError):
        return {"success": False, "message": f"Invalid context size: {value}"}
    if num_ctx < 2048:
        return {"success": False,
                "message": f"Context size too small: {num_ctx} (minimum 2048)"}
    settings = load_api_settings()
    settings["ollama_num_ctx"] = num_ctx
    save_api_settings(settings)
    return {"success": True,
            "message": f"Ollama context size saved: {num_ctx:,} tokens "
                       f"(applies from the next response; the model will reload)"}


def get_imagen_models() -> List[Tuple[str, str]]:
    """Get available Imagen models from Google's model list.

    Filters Google's available_models to those containing 'imagen'.

    Returns:
        List of (display_label, encoded_value) tuples.
    """
    settings = load_api_settings()
    google_models = settings.get("google", {}).get("available_models", [])
    google_key = settings.get("google", {}).get("api_key", "")

    if not google_key:
        return []

    choices = []
    for model_name in google_models:
        if "imagen" in model_name.lower():
            label = f"{model_name} (Gemini)"
            value = encode_model_value("google", model_name)
            choices.append((label, value))

    return choices


# ============================================================
# ElevenLabs TTS Settings
# ============================================================

ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"
ELEVENLABS_MODELS_URL = "https://api.elevenlabs.io/v1/models"


def get_elevenlabs_api_key() -> str:
    """Get the ElevenLabs API key.

    Returns:
        The API key string, or empty string if not set.
    """
    settings = load_api_settings()
    return settings.get("elevenlabs", {}).get("api_key", "")


def get_elevenlabs_voices() -> List[Dict[str, str]]:
    """Get the saved ElevenLabs voice list (user voices first, then stock).

    Returns:
        List of {"voice_id": str, "name": str, "category": str} dicts from
        the last refresh ("category" may be absent in pre-2026-07-12 saves —
        those lists contained user voices only).
    """
    settings = load_api_settings()
    return settings.get("elevenlabs", {}).get("available_voices", [])


def _is_premade_voice(voice: Dict[str, str]) -> bool:
    """True for ElevenLabs stock voices (usable via API on the free plan)."""
    return voice.get("category") == "premade"


def get_elevenlabs_voice_display_choices() -> List[Tuple[str, str]]:
    """Choices for the API Setting "My Voices" dropdown.

    Stock voices carry a free marker, e.g. "Aria (無料)".

    Returns:
        List of (display_label, voice_id) tuples.
    """
    from backend.shared.i18n import t

    choices = []
    for voice in get_elevenlabs_voices():
        name = voice.get("name", "")
        voice_id = voice.get("voice_id", "")
        if not (name and voice_id):
            continue
        label = f"{name} ({t('api.eleven_free_word')})" if _is_premade_voice(voice) else name
        choices.append((label, voice_id))
    return choices


def get_elevenlabs_model_id() -> str:
    """Get the configured ElevenLabs TTS model id.

    Returns:
        Model id string (defaults to eleven_multilingual_v2).
    """
    settings = load_api_settings()
    return settings.get("elevenlabs", {}).get("model_id", "eleven_multilingual_v2")


def save_elevenlabs_model_id(model_id: str) -> Dict[str, Any]:
    """Save the ElevenLabs TTS model id.

    Args:
        model_id: Model id string (e.g. "eleven_multilingual_v2").

    Returns:
        Result dict with success status and message.
    """
    if not model_id:
        return {"success": False, "message": "No model selected."}
    settings = load_api_settings()
    settings.setdefault("elevenlabs", {})["model_id"] = model_id
    save_api_settings(settings)
    return {"success": True, "message": f"ElevenLabs TTS model saved: {model_id}"}


def _elevenlabs_http_error_message(e: "requests.exceptions.HTTPError") -> str:
    """Human-readable message for an ElevenLabs HTTP error.

    Includes the API's own detail message: ElevenLabs returns 401 both for a
    truly invalid key AND for a scoped key missing a permission (e.g.
    voices_read) — without the detail the two are indistinguishable in the UI.
    """
    status_code = e.response.status_code if e.response is not None else "unknown"
    detail = ""
    try:
        body = e.response.json().get("detail", {})
        detail = body.get("message", "") if isinstance(body, dict) else str(body)
    except Exception:
        pass

    if status_code == 401:
        msg = "API key rejected (401)."
    elif status_code == 403:
        msg = "Access denied (403 Forbidden)."
    else:
        msg = f"HTTP error: {status_code}"
    if detail:
        msg = f"{msg} {detail}"
    return msg


def fetch_elevenlabs_voices() -> Dict[str, Any]:
    """Fetch the user's account voices (My Voices) from ElevenLabs and save.

    Returns:
        Result dict with success status, message, and voices list.
    """
    api_key = get_elevenlabs_api_key()
    if not api_key:
        return {"success": False, "message": "ElevenLabs API key is not set.", "voices": []}

    try:
        response = requests.get(
            ELEVENLABS_VOICES_URL,
            headers={"xi-api-key": api_key},
            timeout=API_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        # /v1/voices returns the user's registered voices AND ElevenLabs'
        # ~20 stock voices (category "premade", 実測 2026-07-12). Both are
        # kept: stock voices are the only ones the API accepts on the free
        # plan (library voices are 402 for free users), and they are listed
        # BELOW the user's own voices with a free marker (稜裁定 2026-07-12).
        user_voices = []
        premade_voices = []
        for voice in data.get("voices", []):
            voice_id = voice.get("voice_id", "")
            name = voice.get("name", "")
            if not (voice_id and name):
                continue
            entry = {
                "voice_id": voice_id,
                "name": name,
                "category": voice.get("category", ""),
            }
            if entry["category"] == "premade":
                premade_voices.append(entry)
            else:
                user_voices.append(entry)
        voices = user_voices + premade_voices

        with _settings_lock:
            settings = load_api_settings()
            settings.setdefault("elevenlabs", {})["available_voices"] = voices
            save_api_settings(settings)

        logger.info(f"Fetched {len(voices)} voices from ElevenLabs")
        return {"success": True, "message": f"{len(voices)} voices found.", "voices": voices}
    except requests.exceptions.HTTPError as e:
        msg = _elevenlabs_http_error_message(e)
        logger.error(f"Failed to fetch ElevenLabs voices: {msg}")
        return {"success": False, "message": msg, "voices": []}
    except requests.exceptions.ConnectionError:
        msg = "Cannot connect to API server."
        logger.error(f"Failed to fetch ElevenLabs voices: {msg}")
        return {"success": False, "message": msg, "voices": []}
    except requests.exceptions.Timeout:
        msg = "Request timed out."
        logger.error(f"Failed to fetch ElevenLabs voices: {msg}")
        return {"success": False, "message": msg, "voices": []}
    except Exception as e:
        msg = f"Unexpected error: {e}"
        logger.error(f"Failed to fetch ElevenLabs voices: {msg}")
        return {"success": False, "message": msg, "voices": []}


def fetch_elevenlabs_models() -> Dict[str, Any]:
    """Fetch TTS-capable models from ElevenLabs and save.

    /v1/models also returns non-TTS models (STT "Scribe", music, voice
    conversion) — the can_do_text_to_speech filter is mandatory: an
    unfiltered list would offer models that cannot speak (silent output).
    Cost comes from the API's model_rates — never a hardcoded table.

    Returns:
        Result dict with success status, message, and models list.
    """
    api_key = get_elevenlabs_api_key()
    if not api_key:
        return {"success": False, "message": "ElevenLabs API key is not set.", "models": []}

    try:
        response = requests.get(
            ELEVENLABS_MODELS_URL,
            headers={"xi-api-key": api_key},
            timeout=API_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        models = []
        for model in data if isinstance(data, list) else data.get("models", []):
            if model.get("can_do_text_to_speech") is not True:
                continue
            model_id = model.get("model_id", "")
            name = model.get("name", "") or model_id
            if not model_id:
                continue
            # Real cost lives in model_rates.character_cost_multiplier
            # (0.5 for Flash/Turbo); token_cost_factor is a legacy field
            # that is 1.0 for every model (実測 2026-07-12).
            rates = model.get("model_rates") or {}
            cost = rates.get("character_cost_multiplier") or model.get("token_cost_factor", 1.0)
            models.append({
                "model_id": model_id,
                "name": name,
                "cost_factor": cost,
            })

        with _settings_lock:
            settings = load_api_settings()
            settings.setdefault("elevenlabs", {})["available_models"] = models
            save_api_settings(settings)

        logger.info(f"Fetched {len(models)} TTS models from ElevenLabs")
        return {"success": True, "message": f"{len(models)} TTS models found.", "models": models}
    except requests.exceptions.HTTPError as e:
        msg = _elevenlabs_http_error_message(e)
        logger.error(f"Failed to fetch ElevenLabs models: {msg}")
        return {"success": False, "message": msg, "models": []}
    except requests.exceptions.ConnectionError:
        msg = "Cannot connect to API server."
        logger.error(f"Failed to fetch ElevenLabs models: {msg}")
        return {"success": False, "message": msg, "models": []}
    except requests.exceptions.Timeout:
        msg = "Request timed out."
        logger.error(f"Failed to fetch ElevenLabs models: {msg}")
        return {"success": False, "message": msg, "models": []}
    except Exception as e:
        msg = f"Unexpected error: {e}"
        logger.error(f"Failed to fetch ElevenLabs models: {msg}")
        return {"success": False, "message": msg, "models": []}


def get_elevenlabs_model_choices() -> List[Tuple[str, str]]:
    """Choices for the ElevenLabs TTS model dropdown, cost factor in the label.

    Returns:
        List of (display_label, model_id) tuples, e.g.
        ("Eleven Flash v2.5 (cost 0.5x)", "eleven_flash_v2_5").
    """
    from backend.shared.i18n import t

    choices = []
    available_models = load_api_settings().get("elevenlabs", {}).get("available_models", [])
    for model in available_models:
        # "token_cost_factor" fallback: lists saved before the cost-field fix
        factor = model.get("cost_factor") or model.get("token_cost_factor", 1.0)
        label = t('api.eleven_cost_label', name=model.get("name", model.get("model_id", "")),
                  factor=f"{factor:g}")
        choices.append((label, model.get("model_id", "")))
    return choices


# ============================================================
# OpenAI STT Model Choices
# ============================================================

# Baseline transcription models (always offered, even before any refresh).
OPENAI_STT_BASE_MODELS = ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"]


def get_openai_stt_model_choices() -> List[str]:
    """Model choices for the Audio Settings STT dropdown.

    Baseline models plus any transcription-capable model from the last
    OpenAI models refresh (API Setting tab) — new models like a future
    gpt-5-transcribe appear after 更新 without a code change. Diarize
    variants are excluded (different response shape, incompatible with the
    plain-text contract of the transcription client).
    """
    choices = list(OPENAI_STT_BASE_MODELS)
    available = load_api_settings().get("openai", {}).get("available_models", [])
    for model_name in available:
        lname = model_name.lower()
        if "diarize" in lname:
            continue
        if ("transcribe" in lname or lname.startswith("whisper")) and model_name not in choices:
            choices.append(model_name)
    return choices


# ============================================================
# Unified TTS Choices for Character Dropdown
# ============================================================

def get_all_tts_choices(
    sbv2_models: Optional[List[str]] = None,
    language: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Get unified TTS list for the character create/edit dropdown.

    Local engines are language-exclusive (2026-07-26 ruling): SBV2 is the
    Japanese route, Kokoro the English one. ElevenLabs (API, multilingual)
    is always offered.

    Args:
        sbv2_models: List of SBV2 model folder names.
        language: Character language "ja"/"en" to filter the local engines;
            None returns every engine (page-load fallback so any saved value
            stays present in the dropdown).

    Returns:
        List of (display_label, encoded_value) tuples. encoded_value format:
        "sbv2::<folder>" / "kokoro::<voice>" / "elevenlabs::<voice_id>".
    """
    choices = []

    if language in (None, "ja"):
        for model_name in sbv2_models or []:
            choices.append((f"{model_name} (Style-bert-vits2)", f"sbv2::{model_name}"))

    if language in (None, "en"):
        # 呼出時import: 声一覧の実体(repo直下 kokoro/voices/ スキャン)は
        # audio_output 側が真実源。
        try:
            from audio_output.kokoro_engine import list_voices
            for voice_name in list_voices():
                choices.append((f"{voice_name} (KokoroTTS)", f"kokoro::{voice_name}"))
        except Exception:
            pass  # Kokoro資材未配置ならエントリなし(ElevenLabsのキー無しと同型)

    if get_elevenlabs_api_key():
        from backend.shared.i18n import t
        # Saved order is user voices first, stock (premade) voices below
        for voice in get_elevenlabs_voices():
            voice_id = voice.get("voice_id", "")
            name = voice.get("name", "") or voice_id
            if not voice_id:
                continue
            if _is_premade_voice(voice):
                label = f"{name} (ElevenLabs / {t('api.eleven_free_word')})"
            else:
                label = f"{name} (ElevenLabs)"
            choices.append((label, f"elevenlabs::{voice_id}"))

    return choices


def decode_tts_value(encoded_value: str) -> Tuple[str, str]:
    """Decode a TTS dropdown value into (provider, identifier).

    Args:
        encoded_value: "sbv2::<folder>", "kokoro::<voice>" or
            "elevenlabs::<voice_id>". Legacy unencoded folder names decode
            as sbv2.

    Returns:
        Tuple of (provider, identifier).
    """
    if "::" in encoded_value:
        parts = encoded_value.split("::", 1)
        return parts[0], parts[1]
    # Fallback: legacy bare SBV2 folder name
    return "sbv2", encoded_value


# ============================================================
# Connectivity Probes (status indicator)
# ============================================================

# Probes are liveness checks, not data fetches: a dead network must resolve
# to "unreachable" in seconds, not API_REQUEST_TIMEOUT (30s).
PROBE_TIMEOUT = 5


def _probe_error_state(e: Exception) -> str:
    """Map a probe exception to an indicator state."""
    if isinstance(e, requests.exceptions.HTTPError):
        status_code = e.response.status_code if e.response is not None else 0
        if status_code in (401, 403):
            return "auth_error"
    return "unreachable"


def probe_provider_api(provider: str) -> Dict[str, Any]:
    """Live connectivity probe for an LLM API provider (no settings write).

    Returns:
        {"state": "ok"|"auth_error"|"unreachable"|"no_key", "model_count": int}.
        "no_key" is decided offline — no request leaves the machine without a key.
    """
    if provider not in PROVIDER_CONFIG or "models_url" not in PROVIDER_CONFIG[provider]:
        return {"state": "unreachable", "model_count": 0}

    api_key = load_api_settings().get(provider, {}).get("api_key", "")
    if not api_key:
        return {"state": "no_key", "model_count": 0}

    try:
        models = _fetch_models_from_api(provider, api_key, timeout=PROBE_TIMEOUT)
        return {"state": "ok", "model_count": len(models)}
    except Exception as e:
        state = _probe_error_state(e)
        logger.debug(f"Probe {provider}: {state} ({e})")
        return {"state": state, "model_count": 0}


def probe_elevenlabs() -> Dict[str, Any]:
    """Live connectivity probe for the ElevenLabs API (no settings write)."""
    api_key = get_elevenlabs_api_key()
    if not api_key:
        return {"state": "no_key", "model_count": 0}

    try:
        response = requests.get(
            ELEVENLABS_MODELS_URL,
            headers={"xi-api-key": api_key},
            timeout=PROBE_TIMEOUT
        )
        response.raise_for_status()
        return {"state": "ok", "model_count": 0}
    except Exception as e:
        state = _probe_error_state(e)
        logger.debug(f"Probe elevenlabs: {state} ({e})")
        return {"state": state, "model_count": 0}
