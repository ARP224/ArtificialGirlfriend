import logging
import os
import requests
import time
import json
import uuid
import numpy as np
from typing import List, Optional, Any, Tuple, Dict, Union, Callable
from datetime import datetime
from pathlib import Path
import socket
import re
from contextlib import contextmanager

# Define custom exception classes for more specific error handling
class OllamaConnectionError(Exception):
    """Raised when the Ollama server is unreachable or returns connection errors"""
    pass

class OllamaServiceError(Exception):
    """Raised when the Ollama service is running but returns an error response"""
    pass

class OllamaModelNotFoundError(ValueError):
    """Raised when a requested model is not available in Ollama"""
    pass

class OllamaTimeoutError(TimeoutError):
    """Raised when an operation with Ollama times out"""
    pass

from backend.shared.constants import (
    OLLAMA_HOST, OLLAMA_PORT, OLLAMA_SERVER_URL,
    LOG_ALL_LLM_CALLS, LOGS_DIR,
    DIRECT_API_CONNECTION_RETRY_COUNT, DIRECT_API_RETRY_DELAY,
    OLLAMA_KEEP_ALIVE
)

logger = logging.getLogger(__name__)
logger.info(f"Using Ollama server at: {OLLAMA_SERVER_URL}")

# ========================================
# Last Ollama Request JSON storage (for UI prompt log)
# ========================================
_last_ollama_request_json: Optional[str] = None
_last_ollama_request_timestamp: Optional[str] = None
# トークン表示(2026-08-14 稜裁定): 推定は保存時に計算し、実測(prompt_eval_count)
# は応答受信後に同一レコードへ追記する。seqは並行リクエスト(会話とYouTube返信等)
# で実測が別リクエストのレコードへ付く取り違えを防ぐ通し番号。
_last_ollama_request_est_tokens: Optional[int] = None
_last_ollama_request_actual_tokens: Optional[int] = None
_last_ollama_request_num_ctx: Optional[int] = None
_last_ollama_request_seq: int = 0
_ollama_seq_counter: int = 0


def get_last_ollama_request_json() -> Dict[str, Any]:
    """Get the last Ollama request JSON for UI display.

    Returns:
        Dict with JSON string, timestamp and token info (est_tokens=テキスト
        推定・actual_tokens=応答のprompt_eval_count実測・num_ctx=送信値)
    """
    return {
        "json": _last_ollama_request_json,
        "timestamp": _last_ollama_request_timestamp,
        "est_tokens": _last_ollama_request_est_tokens,
        "actual_tokens": _last_ollama_request_actual_tokens,
        "num_ctx": _last_ollama_request_num_ctx,
    }


def _estimate_prompt_tokens(sanitized: Dict[str, Any]) -> Optional[int]:
    """プロンプトログ表示用のテキスト推定（画像は含まない＝サイズラベル
    置換後の payload に対して呼ぶ）。tools定義はOllamaがテンプレートへ
    練り込む見えない消費のためJSON文字列で概算に含める（実測比で
    過大傾向＝num_ctx判断の安全側）。"""
    try:
        from backend.shared.token_manager import estimate_token_count
        total = 0
        for msg in sanitized.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                total += estimate_token_count(content)
        tools = sanitized.get("tools")
        if tools:
            total += estimate_token_count(json.dumps(tools, ensure_ascii=False))
        return total
    except Exception as e:
        logger.debug(f"Prompt token estimate failed: {e}")
        return None


def _save_last_request_json(payload: Dict[str, Any]) -> int:
    """Save the request payload as formatted JSON string for UI display.

    Base64 image data in messages[].images[] is replaced with size
    placeholders to keep the prompt log readable.

    Returns:
        このレコードの通し番号（実測追記の取り違えガード用）。保存失敗時は
        -1（どのレコードとも一致しない=追記は捨てられる）。
    """
    global _last_ollama_request_json, _last_ollama_request_timestamp, \
        _last_ollama_request_est_tokens, _last_ollama_request_actual_tokens, \
        _last_ollama_request_num_ctx, _last_ollama_request_seq, \
        _ollama_seq_counter
    _ollama_seq_counter += 1
    seq = _ollama_seq_counter
    try:
        import copy
        sanitized = copy.deepcopy(payload)
        for msg in sanitized.get("messages", []):
            if "images" in msg:
                sanitized_images = []
                for img_b64 in msg["images"]:
                    size_kb = len(img_b64) * 3 // 4 // 1024
                    sanitized_images.append(f"[image: {size_kb}KB]")
                msg["images"] = sanitized_images
        _last_ollama_request_json = json.dumps(sanitized, ensure_ascii=False, indent=2)
        _last_ollama_request_timestamp = datetime.now().isoformat()
        _last_ollama_request_est_tokens = _estimate_prompt_tokens(sanitized)
        _last_ollama_request_actual_tokens = None
        _last_ollama_request_num_ctx = (
            sanitized.get("options", {}).get("num_ctx")
            if isinstance(sanitized.get("options"), dict) else None)
        _last_ollama_request_seq = seq
        _publish_prompt_tokens()
        return seq
    except Exception as e:
        logger.warning(f"Failed to save request JSON for display: {e}")
        return -1


def _attach_last_ollama_actual_tokens(seq: int, prompt_eval_count: int) -> None:
    """応答の実測プロンプトトークン数を保存済みレコードへ追記する。

    seq不一致（応答待ちの間に別リクエストがレコードを上書きした）は捨てる。
    KVキャッシュ再利用時のprompt_eval_countは新規評価分のみ=フル値でない
    ことがある（表示側は推定との大きい方を採用する）。
    """
    global _last_ollama_request_actual_tokens
    if prompt_eval_count > 0 and seq == _last_ollama_request_seq:
        _last_ollama_request_actual_tokens = prompt_eval_count
        _publish_prompt_tokens()


def _publish_prompt_tokens() -> None:
    """トークン行をWS→JSのDOM直接更新で配信する（'prompt_token_info'）。

    gr.Timer出力の可視要素はgr.skip+show_progress=hiddenでもちらつく既知
    問題のため、表示更新はGradioイベントを通さない（recording_displayと
    同方式）。配信失敗は表示だけの問題=握って続行。
    """
    try:
        from backend.shared.prompt_token_display import (
            format_token_line, select_ollama_display)
        from backend.shared.ui_events import publish_ui_update
        count, source = select_ollama_display(
            _last_ollama_request_est_tokens, _last_ollama_request_actual_tokens)
        html = format_token_line({
            "provider": "ollama", "count": count, "source": source,
            "num_ctx": _last_ollama_request_num_ctx, "model": None,
        })
        publish_ui_update("prompt_token_info", data={"html": html})
    except Exception as e:
        logger.debug(f"prompt token publish failed: {e}")


# Generic validation helper
def validate_param(value: Any, param_name: str, param_type: type,
                  validator_func: Optional[Callable[[Any, str], Dict[str, Any]]] = None,
                  allow_none: bool = False) -> Dict[str, Any]:
    """
    Generic parameter validator to reduce boilerplate.
    
    Args:
        value: The value to validate
        param_name: Name of the parameter (for error messages)
        param_type: Expected type or tuple of types
        validator_func: Optional custom validation function
        allow_none: Whether None values are allowed
        
    Returns:
        Dictionary with validation result following standard format
    """
    # Handle None values
    if value is None:
        if allow_none:
            return {"valid": True, "value": None}
        else:
            return {"valid": False, "error": f"{param_name} cannot be None"}
    
    # Type check
    if not isinstance(value, param_type):
        # Handle tuple of types vs single type
        if isinstance(param_type, tuple):
            type_names = "/".join(t.__name__ for t in param_type)
        else:
            type_names = param_type.__name__
        return {
            "valid": False,
            "error": f"{param_name} must be {type_names}, got {type(value).__name__}"
        }
    
    # Run custom validator if provided
    if validator_func:
        return validator_func(value, param_name)
    
    return {"valid": True, "value": value}


# Specific validators using the generic framework
def _validate_model_name_logic(value: str, param_name: str) -> Dict[str, Any]:
    """Model name specific validation logic."""
    normalized = value.strip()
    
    if not normalized:
        return {"valid": False, "error": f"{param_name} cannot be empty"}
    
    # Check for invalid characters
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$', normalized):
        return {"valid": False, "error": f"{param_name} contains invalid characters: {normalized}"}
    
    return {"valid": True, "value": normalized}


def _validate_numeric_range(value: Union[int, float], param_name: str,
                           min_val: Optional[float] = None,
                           max_val: Optional[float] = None,
                           warn_ranges: Optional[List[Tuple[float, float, str]]] = None) -> Dict[str, Any]:
    """Numeric range validation with warning support."""
    num_val = float(value)
    
    # Check for NaN or infinity
    if not np.isfinite(num_val):
        return {"valid": False, "error": f"{param_name} must be a finite number"}
    
    # Check hard limits
    if min_val is not None and num_val < min_val:
        return {"valid": False, "error": f"{param_name} must be >= {min_val}"}
    
    if max_val is not None and num_val > max_val:
        return {"valid": False, "error": f"{param_name} must be <= {max_val}"}
    
    # Check warning ranges
    result = {"valid": True, "value": num_val}
    if warn_ranges:
        for low, high, message in warn_ranges:
            if low <= num_val <= high:
                result["warning"] = message.format(value=num_val, param=param_name)
                break
    
    return result


# Convenience functions that maintain the same interface
def validate_model_name(model_name: Any) -> Dict[str, Any]:
    """Validate model name parameter."""
    return validate_param(model_name, "Model name", str, _validate_model_name_logic)


def validate_timeout(timeout: Any) -> Dict[str, Any]:
    """Validate timeout parameter."""
    def timeout_validator(val: float, name: str) -> Dict[str, Any]:
        return _validate_numeric_range(
            val, name,
            min_val=0.001,  # Must be positive
            warn_ranges=[
                (0.001, 1.0, "Timeout of {value}s is very short and may cause frequent failures"),
                (300, float('inf'), "Timeout of {value}s is very long and may cause unnecessary waiting")
            ]
        )
    
    return validate_param(timeout, "Timeout", (int, float), timeout_validator)


def check_ollama_running() -> Tuple[bool, str]:
    """
    Simple check to see if Ollama server is responding to requests.
    
    Returns:
        Tuple[bool, str]: (is_running, error_message)
            - is_running: True if Ollama is running, False otherwise
            - error_message: Empty string if running, error description otherwise
    """
    try:
        # Try to connect to the Ollama server
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect((OLLAMA_HOST, int(OLLAMA_PORT)))
        sock.close()
        
        # Then check if it responds to a health check
        # Use root endpoint as /api/health doesn't exist in Ollama
        response = requests.get(f"{OLLAMA_SERVER_URL}/", timeout=2)
        if response.status_code != 200:
            return False, f"Ollama server responded with status code {response.status_code}"
            
        return True, ""
    except socket.timeout:
        return False, f"Connection to Ollama server timed out after 2s"
    except socket.error as e:
        return False, f"Failed to connect to Ollama server: {e}"
    except requests.exceptions.Timeout:
        return False, "Health check to Ollama server timed out"
    except requests.exceptions.RequestException as e:
        return False, f"Error connecting to Ollama server: {e}"
    except Exception as e:
        logger.error(f"Unexpected error checking Ollama server: {e}")
        return False, f"Unexpected error: {str(e)}"


def validate_ollama_model(model_name: str, raise_error: bool = True) -> Tuple[bool, str]:
    """
    Validate that the requested model is available in Ollama.

    Args:
        model_name (str): The name of the model to validate
        raise_error (bool): Whether to raise an exception if the model is unavailable

    Returns:
        Tuple[bool, str]: (is_valid, error_message)
            - is_valid: True if validation succeeded, False if couldn't verify or invalid
            - error_message: Empty string if success, error description otherwise

    Raises:
        OllamaModelNotFoundError: If the model is confirmed to be unavailable and raise_error=True
        OllamaConnectionError: If the Ollama server is unreachable and raise_error=True
        TypeError: If the model name is invalid
    """
    # Input validation
    if not model_name or not isinstance(model_name, str):
        error_msg = f"Invalid model name format: {model_name}"
        logger.error(error_msg)
        if raise_error:
            raise TypeError(error_msg)
        return False, error_msg
    
    # Normalize model name (trim whitespace)
    model_name = model_name.strip()
    
    try:
        # Get available models
        available_models, status_message = list_ollama_models()
        
        if status_message:
            # If there was an error getting models
            logger.warning(
                f"Could not verify model availability: {status_message}. "
                f"Attempting to use model '{model_name}' anyway."
            )
            return False, status_message
            
        # Model list was retrieved successfully
        if model_name not in available_models:
            error_msg = f"Model '{model_name}' not found among available models: {available_models}"
            logger.error(error_msg)
            if raise_error:
                raise OllamaModelNotFoundError(error_msg)
            return False, error_msg
            
        # Model is valid and available
        return True, ""
            
    except Exception as e:
        # Unexpected error
        error_msg = f"Unexpected error validating model '{model_name}': {e}"
        logger.error(error_msg)
        if raise_error:
            raise
        return False, error_msg


def list_ollama_models(max_retries: int = 3, retry_delay: int = 1) -> Tuple[List[str], str]:
    """
    Retrieve a list of locally available Ollama model names by querying the server.
    If the Ollama service is offline or the request fails, an empty list is returned
    and an error is logged.

    Args:
        max_retries: Maximum number of connection attempts
        retry_delay: Initial delay between retries in seconds

    Returns:
        Tuple[List[str], str]: (model_list, status_message)
            - model_list: Available models or empty list if error
            - status_message: Empty string if successful, error message otherwise
    """
    tags_endpoint = f"{OLLAMA_SERVER_URL}/api/tags"
    current_delay = retry_delay

    for attempt in range(max_retries):
        try:
            response = requests.get(tags_endpoint, timeout=5)
            response.raise_for_status()
            data = response.json()

            # The server returns a JSON object with a "models" field
            # that is a list of model objects with "name" field
            models = data.get("models", [])
            if not isinstance(models, list):
                error_msg = "Unexpected response format from Ollama: 'models' not a list."
                logger.warning(error_msg)
                return [], error_msg

            # Extract model names from the list of model objects
            model_names = []
            for model in models:
                if isinstance(model, dict) and "name" in model:
                    model_names.append(model["name"])
                elif isinstance(model, str):
                    model_names.append(model)

            logger.debug(f"Found {len(model_names)} Ollama model(s): {model_names}")
            return model_names, ""

        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries - 1:
                logger.warning(f"Connection attempt {attempt+1} failed: {e}. Retrying in {current_delay} seconds...")
                time.sleep(current_delay)
                current_delay *= 2  # Exponential backoff
            else:
                error_msg = f"Failed to connect to Ollama server after {max_retries} attempts: {e}"
                # warning: モデル一覧照会はインジケータ等のプローブ用途で
                # graceful degrade する=Ollama停止中の照会毎にERRORトーストを
                # 出さない(稜実機 2026-08-02: System画面で毎回トースト化)
                logger.warning(error_msg)
                return [], "Ollama service unreachable"
        except requests.HTTPError as e:
            status_code = e.response.status_code if hasattr(e, 'response') else "unknown"
            error_msg = f"HTTP error {status_code} from Ollama server: {e}"
            logger.error(error_msg)
            return [], f"HTTP error {status_code}"
        except requests.RequestException as e:
            error_msg = f"Failed to connect to Ollama server at {tags_endpoint}: {e}"
            logger.error(error_msg)
            return [], f"Request error: {str(e)}"
        except ValueError as e:
            error_msg = f"Failed to parse Ollama server response at {tags_endpoint}: {e}"
            logger.error(error_msg)
            return [], "Invalid response format"
        except Exception as e:
            error_msg = f"Unexpected error querying Ollama server: {e}"
            logger.error(error_msg)
            return [], f"Unexpected error: {str(e)}"


def get_model_info(model_name: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific Ollama model.
    
    Args:
        model_name: Name of the model (e.g., "llama3.1:8b")
        
    Returns:
        Dict containing model information including context window size
        {
            "success": bool,
            "model_info": {
                "name": str,
                "context_window": int,  # Tokens
                "parameters": dict,
                "template": str,
                "system": str
            },
            "error": str (if failed)
        }
    """
    show_endpoint = f"{OLLAMA_SERVER_URL}/api/show"
    
    try:
        logger.debug(f"Fetching model info for {model_name} from {show_endpoint}")
        response = requests.post(
            show_endpoint,
            json={"name": model_name},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        # Log the raw response for debugging
        logger.debug(f"Raw model info response: {data}")
        
        parameters = data.get("parameters", {})

        # 実効値=min(ユーザー設定, ネイティブ文脈長)を表示する
        # (create_chat_ollama が実際に使う値と同じ真実源=C4.6)
        from backend.llm.ollama_capabilities import get_effective_num_ctx
        num_ctx = get_effective_num_ctx(model_name)
        logger.info(f"Model info returning effective context window: {num_ctx:,} tokens")
            
        return {
            "success": True,
            "model_info": {
                "name": model_name,
                "context_window": num_ctx,
                "parameters": parameters,
                "template": data.get("template", ""),
                "system": data.get("system", "")
            }
        }
        
    except Exception as e:
        # warning: 既定コンテキスト窓で縮退継続するプローブ経路=Ollama停止中に
        # 照会毎のERRORトーストを出さない(/api/tags と同判断・稜裁定 2026-08-03)
        logger.warning(f"Failed to get model info for {model_name}: {e}")
        context_window = get_default_context_window(model_name)
        logger.info(f"Using default context window for {model_name}: {context_window}")
        return {
            "success": False,
            "error": str(e),
            "model_info": {
                "name": model_name,
                "context_window": context_window
            }
        }


def get_default_context_window(model_name: str) -> int:
    """Get default context window for known models."""
    defaults = {
        "llama3.1": 131072,  # 128K context
        "llama3": 8192,
        "llama2": 4096,
        "mistral": 8192,
        "mixtral": 32768,
        "gemma": 8192,
        "gemma2": 8192,
        "qwen": 8192,
        "qwen2": 32768,  # Qwen2 models typically support 32K
        "phi3": 4096,
        "phi": 2048,
        "deepseek-r1": 131072,  # DeepSeek-R1 supports 128K
        "deepseek-coder": 16384,
        "deepseek": 32768,  # Default DeepSeek models
        "vicuna": 4096,
        "codellama": 16384,
        "yi": 32768,
        "solar": 4096
    }
    
    # Extract base model name (before colon)
    base_model = model_name.split(':')[0]
    
    # Handle special cases like yuma/model-name
    if '/' in base_model:
        # For models with namespace, try to identify the base model
        lower_name = base_model.lower()
        if 'deepseek' in lower_name:
            return 131072  # DeepSeek variants typically support 128K
        elif 'qwen' in lower_name:
            return 32768  # Qwen variants
        elif 'llama' in lower_name:
            return 8192  # Llama variants
    
    return defaults.get(base_model, 8192)  # Changed default to 8192 from 2048


def handle_ollama_request_errors(func):
    """
    Decorator to handle common Ollama API request errors and convert them to specific exception types.
    
    Args:
        func: The function to decorate
        
    Returns:
        Wrapped function with standardized error handling
    """
    def wrapper(*args, **kwargs):
        try:
            # Check if Ollama is running first
            is_running, error = check_ollama_running()
            if not is_running:
                raise OllamaConnectionError(f"Ollama server is not available: {error}")
                
            # Call the original function
            return func(*args, **kwargs)
            
        except requests.Timeout as e:
            logger.error(f"Timeout exceeded in {func.__name__}: {e}")
            raise OllamaTimeoutError(f"Ollama operation timed out: {e}")
        except requests.ConnectionError as e:
            logger.error(f"Connection error in {func.__name__}: {e}")
            raise OllamaConnectionError(f"Connection to Ollama server failed: {e}")
        except requests.HTTPError as e:
            status_code = e.response.status_code if hasattr(e, 'response') else "unknown"
            logger.error(f"HTTP error {status_code} in {func.__name__}: {e}")
            raise OllamaServiceError(f"Ollama service returned HTTP {status_code}: {e}")
        except requests.RequestException as e:
            logger.error(f"Request error in {func.__name__}: {e}")
            raise OllamaConnectionError(f"Error connecting to Ollama: {e}")
    return wrapper


def ensure_valid_model(model_name: str, fallback_model: Optional[str] = None) -> Dict[str, Any]:
    """
    Ensures that a valid model is available, trying fallback if necessary.

    Args:
        model_name: Primary model name to try
        fallback_model: Optional fallback model if primary is unavailable

    Returns:
        Dict[str, Any]: A dictionary with standardized response format:
            - success (bool): Whether operation was successful
            - response (str): The model name to use (primary or fallback)
            - warning (str, optional): Warning message if using fallback model
            - error (str, optional): Error message if failed
            - error_type (str, optional): Category of error
            - details (dict, optional): Additional context information

    Raises:
        OllamaModelNotFoundError: If no valid model is available
    """
    # Verify that the requested model is available
    is_valid, error_msg = validate_ollama_model(model_name, raise_error=False)

    # If primary model is valid, use it
    if is_valid:
        return {
            "success": True,
            "response": model_name
        }

    # Try fallback if primary fails and fallback is provided
    if fallback_model and fallback_model != model_name:
        logger.warning(f"Primary model '{model_name}' unavailable: {error_msg}. Trying fallback '{fallback_model}'")
        is_valid_fallback, fallback_error = validate_ollama_model(fallback_model, raise_error=False)

        # If fallback is valid, use it
        if is_valid_fallback:
            logger.info(f"Using fallback model '{fallback_model}' instead of '{model_name}'")
            return {
                "success": True,
                "response": fallback_model,
                "warning": f"Using fallback model: {fallback_model}",
                "details": {
                    "primary_model": model_name,
                    "primary_error": error_msg,
                    "fallback_model": fallback_model
                }
            }

    # No valid model found
    if fallback_model:
        error = f"Neither primary ({model_name}) nor fallback ({fallback_model}) models available"
        details = {
            "primary_model": model_name,
            "primary_error": error_msg,
            "fallback_model": fallback_model,
            "fallback_error": fallback_error if 'fallback_error' in locals() else "Not attempted"
        }
    else:
        error = f"Model '{model_name}' not available: {error_msg}"
        details = {
            "model": model_name,
            "error_details": error_msg
        }

    logger.error(error)

    return {
        "success": False,
        "error": error,
        "error_type": "NOT_FOUND",
        "details": details
    }


def remove_think_tags(content: str) -> str:
    """
    Remove <think>...</think> tags from content.
    Handles both properly closed tags and unclosed tags.
    
    Args:
        content: The text content that may contain thinking tags
        
    Returns:
        str: The cleaned content with thinking tags removed
    """
    if not content:
        return content
        
    # First try to remove properly closed tags
    cleaned = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL)
    
    # If there are still <think> tags (unclosed), remove everything after <think>
    if '<think>' in cleaned:
        think_pos = cleaned.find('<think>')
        if think_pos >= 0:
            cleaned = cleaned[:think_pos].strip()
    
    return cleaned.strip()


# ============================================================================
# Direct API Implementation (LangChain-free Ollama access)
# ============================================================================

# ログディレクトリのキャッシュ（毎回mkdir呼び出しを避ける）
_llm_log_dir_created = False


def _save_llm_log_if_needed(
    request: Dict[str, Any],
    response: Dict[str, Any],
    model: str,
    is_error: bool
) -> None:
    """
    条件に応じてLLM呼び出しログを保存。

    保存条件:
    - is_error=True の場合は常に保存
    - LOG_ALL_LLM_CALLS=true の場合は常に保存
    - それ以外は保存しない

    Args:
        request: 送信したリクエストペイロード
        response: 受け取ったレスポンス
        model: モデル名
        is_error: エラーかどうか
    """
    # 保存条件のチェック
    if not is_error and not LOG_ALL_LLM_CALLS:
        return

    global _llm_log_dir_created

    try:
        log_dir = Path(LOGS_DIR) / "llm_calls"

        # ディレクトリ作成（初回のみ）
        if not _llm_log_dir_created:
            log_dir.mkdir(parents=True, exist_ok=True)
            _llm_log_dir_created = True

        # ファイル名生成（衝突防止のため短いUUIDを追加）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        short_uuid = uuid.uuid4().hex[:6]
        status = "error" if is_error else "success"
        model_safe = model.replace(":", "_").replace("/", "_")
        filename = f"{model_safe}_{timestamp}_{short_uuid}_{status}.json"

        filepath = log_dir / filename

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "status": status,
            "request": request,
            "response": {
                k: v for k, v in response.items()
                if k != "raw_response"  # 生レスポンスは大きいので除外
            }
        }

        # raw_responseは全件ログモード時のみ含める
        if LOG_ALL_LLM_CALLS and "raw_response" in response:
            log_data["raw_response"] = response["raw_response"]

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2, default=str)

        logger.debug(f"[DirectAPI] Log saved: {filepath.name}")

    except Exception as e:
        logger.warning(f"[DirectAPI] Failed to save log: {e}")


def call_ollama_chat(
    model: str,
    messages: List[Dict[str, str]],
    options: Optional[Dict[str, Any]] = None,
    timeout: float = 90.0,
    stream: bool = False,
    retry_on_connection_error: bool = True,
    think: Optional[bool] = None,
    tools: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Ollama /api/chat エンドポイントを直接呼び出す低レベルラッパー。

    Args:
        model: モデル名 (e.g., "qwen3:14b", "llama3.1:8b")
        messages: OpenAI互換形式のメッセージリスト
            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, ...]
        options: Ollamaのサンプリングオプション
            {"temperature": 0.8, "top_p": 0.95, "repeat_penalty": 1.15, ...}
        timeout: HTTPリクエストのタイムアウト（秒）
        stream: ストリーミングモード（現在はFalseのみサポート）
        retry_on_connection_error: 接続エラー時に1回リトライするか
        tools: ツール定義（OpenAI Chat Completions形式。
            tool_schemas.format_tools_for_provider("ollama", ...) の出力）。
            None/空なら tools キー自体を送らない（非tools対応モデルへの
            素の会話を壊さない）。

    Returns:
        Dict[str, Any]: 標準化されたレスポンス
        成功時: {"success": True, "content": str, "tool_calls": list, "model": str, ...}
        失敗時: {"success": False, "error": str, "error_type": str, ...}
    """
    # streamingモードの警告（現在は未サポート）
    if stream:
        logger.warning("[DirectAPI] streaming=True is not yet supported, using stream=False")
        stream = False

    url = f"{OLLAMA_SERVER_URL}/api/chat"

    # リクエストペイロードの構築
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "keep_alive": OLLAMA_KEEP_ALIVE  # モデルをVRAMに保持
    }

    # think パラメータを追加（明示的に指定された場合のみ）
    if think is not None:
        payload["think"] = think

    if options:
        payload["options"] = options

    if tools:
        payload["tools"] = tools

    # UI表示用にリクエストJSONを保存（seqは実測トークン追記のガード）
    request_seq = _save_last_request_json(payload)

    # デバッグログ出力
    logger.debug(f"[DirectAPI] Request to {url}")
    logger.debug(f"[DirectAPI] Model: {model}, Messages: {len(messages)}")
    if options:
        logger.debug(f"[DirectAPI] Options: {options}")
    if think is not None:
        logger.debug(f"[DirectAPI] Think: {think}")

    # リトライ設定
    max_attempts = (DIRECT_API_CONNECTION_RETRY_COUNT + 1) if retry_on_connection_error else 1
    last_error = None

    for attempt in range(max_attempts):
        if attempt > 0:
            logger.info(f"[DirectAPI] Retry attempt {attempt}/{DIRECT_API_CONNECTION_RETRY_COUNT}")
            time.sleep(DIRECT_API_RETRY_DELAY)

        try:
            start_time = time.time()
            response = requests.post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"}
            )
            elapsed_time = time.time() - start_time

            logger.debug(f"[DirectAPI] Response in {elapsed_time:.2f}s, status={response.status_code}")

            # HTTPエラーチェック
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                logger.error(f"[DirectAPI] HTTP error: {error_msg}")

                result = {
                    "success": False,
                    "error": error_msg,
                    "error_type": "HTTP",
                    "raw_request": payload
                }
                _save_llm_log_if_needed(payload, result, model, is_error=True)
                return result

            # JSONパース
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"[DirectAPI] JSON parse error: {e}")
                result = {
                    "success": False,
                    "error": f"Failed to parse response JSON: {e}",
                    "error_type": "PARSE",
                    "raw_request": payload
                }
                _save_llm_log_if_needed(payload, result, model, is_error=True)
                return result

            # レスポンスからコンテンツを抽出
            message = data.get("message", {})
            content = message.get("content", "")
            raw_tool_calls = message.get("tool_calls") or []

            if not content and not raw_tool_calls:
                logger.warning("[DirectAPI] Empty content in response")

            result = {
                "success": True,
                "content": content,
                "tool_calls": raw_tool_calls,
                "model": data.get("model", model),
                "total_duration": data.get("total_duration", 0),
                "eval_count": data.get("eval_count", 0),
                "prompt_eval_count": data.get("prompt_eval_count", 0),
                "raw_request": payload,
                "raw_response": data
            }

            logger.info(f"[DirectAPI] Success: {len(content)} chars, "
                       f"tool_calls={len(raw_tool_calls)}, "
                       f"eval={result['eval_count']}, prompt_eval={result['prompt_eval_count']}")

            _attach_last_ollama_actual_tokens(request_seq, result["prompt_eval_count"])
            _save_llm_log_if_needed(payload, result, model, is_error=False)
            return result

        except requests.ConnectionError as e:
            last_error = e
            logger.warning(f"[DirectAPI] Connection error (attempt {attempt+1}): {e}")
            # 接続エラーはリトライ対象 - ループを継続
            continue

        except requests.Timeout:
            # タイムアウトはリトライしない
            # warning: この失敗dictは上位(conversation_manager)が受けて
            # ERRORログする=二重トースト防止(稜実機 2026-08-02)
            logger.warning(f"[DirectAPI] Timeout after {timeout}s")
            result = {
                "success": False,
                "error": f"Request timed out after {timeout} seconds",
                "error_type": "TIMEOUT",
                "raw_request": payload
            }
            _save_llm_log_if_needed(payload, result, model, is_error=True)
            return result

        except requests.RequestException as e:
            # その他のリクエストエラーはリトライしない
            # warning: 上位がERRORログ(上のTimeoutと同じ理由)
            logger.warning(f"[DirectAPI] Request error: {e}")
            result = {
                "success": False,
                "error": f"Request failed: {e}",
                "error_type": "REQUEST",
                "raw_request": payload
            }
            _save_llm_log_if_needed(payload, result, model, is_error=True)
            return result

        except Exception as e:
            # 予期せぬエラーはリトライしない
            # warning(+trace): 上位がERRORログ(上のTimeoutと同じ理由)
            logger.warning(f"[DirectAPI] Unexpected error: {e}", exc_info=True)
            result = {
                "success": False,
                "error": f"Unexpected error: {e}",
                "error_type": "INTERNAL",
                "raw_request": payload
            }
            _save_llm_log_if_needed(payload, result, model, is_error=True)
            return result

    # リトライ後も接続エラー
    # warning: 上位がERRORログ(稜実機 2026-08-02: Ollama停止で本行+上位の
    # 2枚トーストになっていた=C2の取りこぼし)
    logger.warning(f"[DirectAPI] Connection failed after {max_attempts} attempts")
    result = {
        "success": False,
        "error": f"Connection failed after {max_attempts} attempts: {last_error}",
        "error_type": "CONNECTION",
        "raw_request": payload
    }
    _save_llm_log_if_needed(payload, result, model, is_error=True)
    return result


def call_ollama_embedding(
    model: str,
    text: str,
    timeout: float = 30.0,
    retry_on_connection_error: bool = True
) -> Dict[str, Any]:
    """
    Ollama /api/embeddings エンドポイントを直接呼び出す。

    Args:
        model: モデル名 (e.g., "qwen3:14b")
        text: 埋め込みを生成するテキスト
        timeout: HTTPタイムアウト（秒）
        retry_on_connection_error: 接続エラー時にリトライ

    Returns:
        成功時: {"success": True, "embedding": List[float], "model": str}
        失敗時: {"success": False, "error": str, "error_type": str}
    """
    url = f"{OLLAMA_SERVER_URL}/api/embeddings"
    payload = {
        "model": model,
        "prompt": text,  # Ollama 0.13+ uses "prompt" key, not "input"
        "keep_alive": OLLAMA_KEEP_ALIVE  # モデルをVRAMに保持
    }

    logger.debug(f"[DirectAPI] Embedding request to {url}")
    logger.debug(f"[DirectAPI] Model: {model}, Text length: {len(text)}")

    max_attempts = (DIRECT_API_CONNECTION_RETRY_COUNT + 1) if retry_on_connection_error else 1

    for attempt in range(max_attempts):
        if attempt > 0:
            logger.info(f"[DirectAPI] Embedding retry attempt {attempt}/{DIRECT_API_CONNECTION_RETRY_COUNT}")
            time.sleep(DIRECT_API_RETRY_DELAY)

        try:
            start_time = time.time()
            response = requests.post(url, json=payload, timeout=timeout)
            elapsed_time = time.time() - start_time

            logger.debug(f"[DirectAPI] Embedding response in {elapsed_time:.2f}s, status={response.status_code}")

            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                logger.error(f"[DirectAPI] Embedding HTTP error: {error_msg}")
                return {"success": False, "error": error_msg, "error_type": "HTTP"}

            try:
                data = response.json()
            except json.JSONDecodeError as e:
                return {"success": False, "error": f"JSON parse error: {e}", "error_type": "PARSE"}

            # Ollama 0.13+ returns "embedding" (singular), older versions may use "embeddings" (plural)
            embedding = data.get("embedding")
            if embedding is None:
                # Fallback for older API format
                embeddings = data.get("embeddings", [])
                embedding = embeddings[0] if embeddings else None

            if embedding and len(embedding) > 0:
                # 埋め込みの検証
                if not isinstance(embedding, list):
                    return {
                        "success": False,
                        "error": "Invalid embedding format: expected non-empty list",
                        "error_type": "INVALID_RESPONSE"
                    }

                logger.info(f"[DirectAPI] Embedding generated: {len(embedding)} dimensions")
                return {
                    "success": True,
                    "embedding": embedding,
                    "model": data.get("model", model)
                }
            else:
                return {
                    "success": False,
                    "error": "No embeddings in response",
                    "error_type": "EMPTY_RESPONSE"
                }

        except requests.exceptions.ConnectionError as e:
            if attempt < max_attempts - 1:
                logger.warning(f"[DirectAPI] Connection error, will retry: {e}")
                continue
            return {"success": False, "error": f"Connection error: {e}", "error_type": "CONNECTION"}
        except requests.exceptions.Timeout:
            return {"success": False, "error": f"Timeout after {timeout}s", "error_type": "TIMEOUT"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {e}", "error_type": "UNKNOWN"}

    return {"success": False, "error": "All retries exhausted", "error_type": "RETRY_EXHAUSTED"}


def warm_ollama_model(model: str, num_ctx: Optional[int] = None,
                      timeout: float = 180.0) -> Dict[str, Any]:
    """
    Ollamaモデルをプロンプト無しでVRAMへロードだけさせる
    (unload_ollama_model の対称形・会話開始時のプリウォーム用)。

    /api/generate は prompt 無しだと生成せず load だけして即返る
    (done_reason=load, qwen3:14b実測22.8秒 2026-08-01)。call_ollama_chat を
    使わないのは _save_last_request_json がUIの「最後のOllamaリクエスト」
    表示をダミーで上書きするため。embedding専用モデルはこのエンドポイント
    でロードできない(400 "does not support generate" 実測)ので、そちらは
    call_ollama_embedding で温めること。

    num_ctx: 本番と同じ値(ollama_capabilities.get_effective_num_ctx(model))を
    渡すこと。省略するとOllamaが既定ctxでロードし、直後の本番呼び出しが
    ctx違いでモデルを再ロードしてウォームが無意味になる(create_chat_ollama
    の extraction 分岐が同じ理由で実効値を共有している)。

    Returns:
        {"success": True} または {"success": False, "error": str}
    """
    url = f"{OLLAMA_SERVER_URL}/api/generate"
    payload: Dict[str, Any] = {"model": model, "keep_alive": OLLAMA_KEEP_ALIVE}
    if num_ctx:
        payload["options"] = {"num_ctx": num_ctx}

    logger.debug(f"[DirectAPI] Warming model '{model}' (num_ctx={num_ctx})")

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code == 200:
            logger.info(f"[DirectAPI] Model '{model}' warmed (loaded into memory)")
            return {"success": True}
        error_msg = f"HTTP {response.status_code}"
        logger.warning(f"[DirectAPI] Model warm returned: {error_msg}")
        return {"success": False, "error": error_msg}
    except Exception as e:
        logger.warning(f"[DirectAPI] Failed to warm model '{model}': {e}")
        return {"success": False, "error": str(e)}


def unload_ollama_model(model: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Ollamaモデルをアンロードしてメモリを解放する。
    keep_alive=0 を設定することで即座にメモリを解放。

    Args:
        model: アンロードするモデル名
        timeout: タイムアウト（秒）

    Returns:
        {"success": True} または {"success": False, "error": str}
    """
    url = f"{OLLAMA_SERVER_URL}/api/generate"
    payload = {"model": model, "keep_alive": 0}

    logger.debug(f"[DirectAPI] Unloading model '{model}'")

    try:
        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code == 200:
            logger.info(f"[DirectAPI] Model '{model}' unloaded successfully")
            return {"success": True}
        else:
            error_msg = f"HTTP {response.status_code}"
            logger.warning(f"[DirectAPI] Model unload returned: {error_msg}")
            return {"success": False, "error": error_msg}
    except Exception as e:
        logger.warning(f"[DirectAPI] Failed to unload model '{model}': {e}")
        return {"success": False, "error": str(e)}


def check_model_cpu_offload(model: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """/api/ps でモデルのCPUオフロード状況を確認し、発生していればWARNする。

    num_ctx増(既定32000=C4.6)でKVキャッシュがVRAMに収まらないモデルは
    エラーにならず「静かに数倍遅く」なるだけで原因に気づけない。会話開始の
    ウォームアップ後に呼び、痕跡をログに残す(稜承認計画の緩和策)。

    Returns:
        {"size": int, "size_vram": int, "offloaded": bool} または照会失敗/
        モデル未ロードで None。Never raises。
    """
    try:
        response = requests.get(f"{OLLAMA_SERVER_URL}/api/ps", timeout=timeout)
        response.raise_for_status()
        base = model.split(":")[0]
        for m in response.json().get("models", []):
            name = m.get("name", "")
            if name == model or name.split(":")[0] == base:
                size = int(m.get("size", 0) or 0)
                size_vram = int(m.get("size_vram", 0) or 0)
                offloaded = size > 0 and size_vram < size
                if offloaded:
                    pct = round(100 * (size - size_vram) / size)
                    logger.warning(
                        f"[Ollama] Model '{name}' is partially offloaded to CPU "
                        f"({pct}% off-GPU, size={size}, size_vram={size_vram}) — "
                        f"generation will be much slower. Consider lowering the "
                        f"Ollama context size in API Settings.")
                return {"size": size, "size_vram": size_vram,
                        "offloaded": offloaded}
    except Exception as e:
        logger.debug(f"[Ollama] /api/ps offload check failed: {e}")
    return None


class OllamaResponse:
    """
    LangChainのAIMessage互換のレスポンスオブジェクト。

    互換性:
    - result.content でテキストにアクセス
    - result[-1].content でもアクセス可能（旧コード互換）
    - hasattr(result, 'content') は True

    Attributes:
        content: AIの応答テキスト
        model: 使用したモデル名
        eval_count: 生成トークン数
        prompt_eval_count: プロンプトトークン数
        total_duration: 総処理時間（ナノ秒）
        raw_response: Ollama APIの生レスポンス
        tool_calls: 正規化済みツール呼び出し [{"id", "name", "arguments"}, ...]
            （APIResponse.tool_calls と同一契約。ツールループが読む）
    """

    def __init__(
        self,
        content: str,
        model: str = "",
        eval_count: int = 0,
        prompt_eval_count: int = 0,
        total_duration: int = 0,
        raw_response: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None
    ):
        self.content = content
        self.model = model
        self.eval_count = eval_count
        self.prompt_eval_count = prompt_eval_count
        self.total_duration = total_duration
        self.raw_response = raw_response or {}
        self.tool_calls = tool_calls or []

    def __getitem__(self, index: int) -> 'OllamaResponse':
        """
        リスト風のアクセスに対応。
        conversation_manager.py:1391 の `result[-1].content` 互換。
        """
        if index == -1 or index == 0:
            return self
        raise IndexError(f"OllamaResponse only supports index 0 or -1, got {index}")

    def __len__(self) -> int:
        """len()対応（リスト互換）"""
        return 1

    def __iter__(self):
        """イテレーション対応（リスト互換）"""
        yield self

    def __repr__(self) -> str:
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"OllamaResponse(content='{preview}', model='{self.model}')"

    def __str__(self) -> str:
        return self.content


def _encode_image_base64(file_path: str) -> Optional[str]:
    """画像ファイルを Ollama /api/chat の images 用 base64 文字列にする。

    圧縮・サイズ上限はAPIプロバイダと同じ経路（_encode_image_file）に乗る
    （呼出時import＝llmパッケージ内の横参照・モジュールロード時循環の回避）。
    失敗は None（その画像だけスキップ）。
    """
    try:
        from backend.llm.api_integration import _encode_image_file
        encoded = _encode_image_file(file_path)
        return encoded["base64"] if encoded else None
    except Exception as e:
        logger.warning(f"[DirectOllamaChat] Failed to encode image {file_path}: {e}")
        return None


class DirectOllamaChat:
    """
    ChatOllamaの代替クラス（直接API版）。

    既存の chat_llm.invoke(messages) インターフェースを維持しつつ、
    LangChainを介さずにOllama APIを直接呼び出す。

    メリット:
    - 送信されるJSONが完全に可視化できる
    - LangChainの暗黙的な変換がない
    - デバッグが容易

    使用例:
        llm = DirectOllamaChat(
            model="qwen3:14b",
            temperature=0.8,
            num_predict=1000,
            num_ctx=12000
        )

        result = llm.invoke([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"}
        ])

        print(result.content)
    """

    def __init__(
        self,
        model: str,
        temperature: float = 0.7,
        top_p: float = 0.95,
        top_k: int = 40,
        min_p: float = 0.05,
        repeat_last_n: int = 500,
        num_predict: int = 1000,
        repeat_penalty: float = 1.0,
        presence_penalty: float = 0.0,
        frequency_penalty: float = 0.0,
        num_ctx: int = 12000,
        timeout: float = 90.0,
        disable_thinking: bool = True
    ):
        """
        Args:
            model: Ollamaモデル名
            temperature: サンプリング温度（0.0-2.0）
            top_p: Nucleus sampling のp値（0.0-1.0）
            top_k: Top-k sampling のk値（0=無効）
            min_p: 最低確率フィルタ（0.0-1.0、0.0=無効）
            repeat_last_n: 繰り返し検出の範囲（トークン数）
            num_predict: 最大生成トークン数
            repeat_penalty: 繰り返しペナルティ（1.0以上）
            presence_penalty: 存在ペナルティ（0.0-2.0）
            frequency_penalty: 頻度ペナルティ（0.0-2.0）
            num_ctx: コンテキストウィンドウサイズ
            timeout: APIタイムアウト（秒）
            disable_thinking: 思考モードを無効化するか（Trueの場合、Ollamaに
                              think=Falseを送信し、レスポンスから<think>タグも除去）
        """
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.min_p = min_p
        self.repeat_last_n = repeat_last_n
        self.num_predict = num_predict
        self.repeat_penalty = repeat_penalty
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.request_timeout = timeout  # ChatOllama互換
        self.disable_thinking = disable_thinking

        # ChatOllama互換属性
        self.base_url = OLLAMA_SERVER_URL

        # Ollamaに送信するoptions（基本パラメータ）
        self._options = {
            "temperature": temperature,
            "top_p": top_p,
            "num_predict": num_predict,
            "repeat_penalty": repeat_penalty,
            "num_ctx": num_ctx
        }

        # top_k は 0 = disabled なので常に送信
        self._options["top_k"] = top_k
        # repeat_last_n は Ollama 側の既定が 64（未送信=64窓のまま各ペナルティが
        # 掛かる）なので 0 = disabled も含めて常に送信する
        self._options["repeat_last_n"] = repeat_last_n
        # 以下は 0 = disabled でデフォルトも 0 のため、0より大きい場合のみ送信
        if min_p > 0:
            self._options["min_p"] = min_p
        if presence_penalty > 0:
            self._options["presence_penalty"] = presence_penalty
        if frequency_penalty > 0:
            self._options["frequency_penalty"] = frequency_penalty

        logger.info(f"[DirectOllamaChat] Initialized: model={model}, "
                   f"temp={temperature}, num_ctx={num_ctx}, "
                   f"num_predict={num_predict}, repeat_penalty={repeat_penalty}, "
                   f"top_k={top_k}, min_p={min_p}, repeat_last_n={repeat_last_n}, "
                   f"presence_penalty={presence_penalty}, frequency_penalty={frequency_penalty}")

    def invoke(self, messages: List[Any],
               tools: Optional[List[Dict[str, Any]]] = None,
               **kwargs) -> OllamaResponse:
        """
        メッセージリストを受け取り、LLM応答を生成。

        Args:
            messages: LangChain Message型またはdict形式のリスト
            tools: ツール定義（format_tools_for_provider("ollama", ...) の
                OpenAI Chat Completions形式）。APIクライアントの
                invoke(messages, tools=...) と同一シグネチャ＝ツールループが
                プロバイダ非依存に呼べる。
            **kwargs: 追加オプション（将来の拡張用）

        Returns:
            OllamaResponse: .content でテキスト・.tool_calls でツール呼び出し

        Raises:
            OllamaTimeoutError: タイムアウト時
            OllamaConnectionError: 接続エラー時
            OllamaServiceError: その他のAPIエラー時
        """
        # メッセージをdict形式に変換
        converted = self._convert_messages(messages)

        logger.debug(f"[DirectOllamaChat.invoke] {len(converted)} messages, "
                     f"{len(tools) if tools else 0} tools")

        # API呼び出し
        result = call_ollama_chat(
            model=self.model,
            messages=converted,
            options=self._options,
            timeout=self.timeout,
            retry_on_connection_error=True,
            think=False if self.disable_thinking else None,
            tools=tools
        )

        # エラー時は適切な例外を投げる
        if not result["success"]:
            error_type = result.get("error_type", "INTERNAL")
            error_msg = result.get("error", "Unknown error")

            if error_type == "TIMEOUT":
                raise OllamaTimeoutError(f"LLM call timed out: {error_msg}")
            elif error_type == "CONNECTION":
                raise OllamaConnectionError(f"Failed to connect to Ollama: {error_msg}")
            else:
                raise OllamaServiceError(f"LLM call failed ({error_type}): {error_msg}")

        content = result["content"]

        # 溢れ実測ガード: prompt evalが窓の上限帯に達した=Ollama側で黙って
        # 切り詰められた可能性(エラーにならず、system先頭欠け=人格崩れとして
        # 現れる)。KVキャッシュ再利用時のprompt_eval_countは新規評価分のみ
        # のため見逃しはありうるが、誤検知はほぼ無い(best-effort・推定側の
        # ガードはtoken_manager側=2段構え)。
        pec = result.get("prompt_eval_count", 0)
        if pec and pec >= max(1, self.num_ctx - self.num_predict):
            logger.warning(
                f"[DirectOllamaChat] prompt_eval_count={pec} reached the "
                f"context window (num_ctx={self.num_ctx}) — the prompt may "
                f"have been silently truncated by Ollama")

        # <think>タグの除去
        if self.disable_thinking and content:
            original_len = len(content)
            content = remove_think_tags(content)
            if len(content) != original_len:
                logger.debug(f"[DirectOllamaChat] Removed <think> tags: "
                           f"{original_len} -> {len(content)} chars")

        # Ollamaのtool_callsを正規形 {"id","name","arguments"} へ変換
        # (APIResponse.tool_callsと同一契約)。idは新しめのOllamaが返す
        # (0.32.8実測: "call_xxxx")のでそれを使い、無い旧版は合成。
        # argumentsは通常dictで返るが、文字列化する実装系も見るため防御的に
        # パースする。
        tool_calls: List[Dict[str, Any]] = []
        for i, tc in enumerate(result.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append({
                "id": tc.get("id") or f"ollama_call_{i}",
                "name": fn.get("name", ""),
                "arguments": args,
            })

        return OllamaResponse(
            content=content,
            model=result.get("model", self.model),
            eval_count=result.get("eval_count", 0),
            prompt_eval_count=result.get("prompt_eval_count", 0),
            total_duration=result.get("total_duration", 0),
            raw_response=result.get("raw_response"),
            tool_calls=tool_calls
        )

    def _convert_messages(self, messages: List[Any]) -> List[Dict[str, Any]]:
        """様々な形式のメッセージをdict形式に変換。

        画像はモデルが vision 対応のときだけ送る（パス→base64変換）。
        非対応・判定不能なら破棄（出口の安全網）。
        """
        # vision可否は invoke 毎に遅延ゲッターで読む（キャッシュ命中=HTTPなし）。
        # 構築時に焼き込むと、キャラ単位のLLMインスタンスキャッシュ経由で
        # モデル差し替え後も古い判定が残るため、ここで都度参照する。
        # 判定不能（未照会・旧Ollama・サーバー停止）は fail-closed=破棄。
        from backend.llm.ollama_capabilities import get_caps
        vision_ok = get_caps(self.model)["vision"]

        converted = []
        dropped_images = 0

        for msg in messages:
            extra: Dict[str, Any] = {}
            if isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                msg_images = msg.get("images", [])
                # ツールループの往復メッセージを素通しする:
                # - assistant + tool_calls (build_assistant_msg_with_tool_calls)
                # - role "tool" の結果 (format_tool_result_message("ollama"))
                if role == "assistant" and msg.get("tool_calls"):
                    extra["tool_calls"] = msg["tool_calls"]
                if role == "tool" and "tool_name" in msg:
                    extra["tool_name"] = msg["tool_name"]
            elif hasattr(msg, 'content'):
                content = msg.content
                role = self._get_role_from_message(msg)
                msg_images = []
            else:
                logger.warning(f"[DirectOllamaChat] Unknown message type: {type(msg)}")
                role = "user"
                content = str(msg)
                msg_images = []

            entry = {"role": role, "content": content, **extra}

            # 画像: vision対応モデルにはbase64で送り、非対応モデルでは破棄する
            # (出口の安全網)。非対応モデルに画像を送ると生成自体がエラーに
            # なる。入口(📎添付・カメラ系の可用性ゲート)が平時の防御で、
            # ここはプロバイダ/モデル切替で履歴に画像が残るケースの最終網
            # (2026-07-31 稜裁定・2026-08-11 per-model化)。
            if msg_images and role != "system":
                if vision_ok:
                    encoded = []
                    for img_path in msg_images:
                        b64 = _encode_image_base64(img_path)
                        if b64:
                            encoded.append(b64)
                    if encoded:
                        entry["images"] = encoded
                else:
                    dropped_images += len(msg_images)

            converted.append(entry)

        if dropped_images:
            logger.warning(
                f"[DirectOllamaChat] Dropped {dropped_images} image(s) from prompt: "
                f"model '{self.model}' has no vision capability")

        return converted

    def _get_role_from_message(self, msg) -> str:
        """LangChain Messageオブジェクトからroleを判定"""
        class_name = msg.__class__.__name__

        if "System" in class_name:
            return "system"
        elif "Human" in class_name:
            return "user"
        elif "AI" in class_name:
            return "assistant"
        else:
            logger.warning(f"[DirectOllamaChat] Unknown message class: {class_name}")
            return "user"


    def __repr__(self) -> str:
        return f"DirectOllamaChat(model='{self.model}', temp={self.temperature})"


# ============================================================================
# End of Direct API Implementation
# ============================================================================


@handle_ollama_request_errors
def create_chat_ollama(
    model_name: str,
    system_prompt: Optional[str] = None,
    tuning: Optional[Dict[str, Any]] = None,
    timeout: float = 90.0,
    fallback_model: Optional[str] = None,
    usage_type: str = 'conversation',
) -> Dict[str, Any]:
    """
    Create and return a DirectOllamaChat instance for conversation-based LLM usage.

    This function verifies that the requested model_name is in the list of available models
    reported by Ollama. If the model is not found, it tries the fallback model if provided.

    Uses Direct API mode (DirectOllamaChat) which directly calls Ollama REST API.

    Args:
        model_name (str): Name of the model (e.g. "llama2" or "vicuna:13b-q4_0").
        system_prompt (Optional[str]): System instructions/persona for the AI.
                                      Note: This is stored for reference but must be passed
                                      as a SystemMessage when invoking the model.
        tuning (Optional[Dict[str, Any]]): Tuning parameters dictionary.
                                          If None, uses TUNING_DEFAULTS for conversation
                                          or EXTRACTION_TUNING for extraction.
                                          Ignored when usage_type='extraction'.
        timeout (float): Timeout in seconds for API calls (default: 90.0).
        fallback_model (Optional[str]): Model to use if primary model fails.
        usage_type (str): Usage type ('conversation' or 'extraction') for context configuration.

    Returns:
        Dict[str, Any]: A dictionary with standardized response format:
            - success (bool): Whether operation was successful
            - response (Any, optional): The DirectOllamaChat instance if successful
            - model_used (str, optional): The actual model used (might be fallback)
            - warning (str, optional): Any warning messages if operation succeeded with issues
            - error (str, optional): Error message if failed
            - error_type (str, optional): Category of error
            - details (dict, optional): Additional context information

    Raises:
        OllamaModelNotFoundError: If the model is not found and no fallback is available
        OllamaConnectionError: If the Ollama server is unreachable
        OllamaTimeoutError: If the operation times out
        OllamaServiceError: If the Ollama service returns an error
        ValueError: For invalid parameters
        MemoryError: If there's insufficient memory to load the model
    """
    # Parameter validation
    combined_warnings = []

    # Validate model name
    model_validation = validate_model_name(model_name)
    if not model_validation["valid"]:
        return {
            "success": False,
            "error": model_validation["error"],
            "error_type": "VALIDATION",
            "details": {
                "parameter": "model_name",
                "provided_value": model_name
            }
        }
    validated_model_name = model_validation["value"]

    # Validate timeout
    timeout_validation = validate_timeout(timeout)
    if not timeout_validation["valid"]:
        return {
            "success": False,
            "error": timeout_validation["error"],
            "error_type": "VALIDATION",
            "details": {
                "parameter": "timeout",
                "provided_value": timeout
            }
        }
    validated_timeout = timeout_validation["value"]
    if "warning" in timeout_validation:
        combined_warnings.append(timeout_validation["warning"])
        logger.warning(timeout_validation["warning"])

    # Try to get valid model (primary or fallback)
    model_result = ensure_valid_model(validated_model_name, fallback_model)

    # Check if model validation was successful
    if not model_result.get("success", False):
        # Already in standardized error format, just add stage info
        if "details" in model_result:
            model_result["details"]["stage"] = "model_validation"
        else:
            model_result["details"] = {"stage": "model_validation"}
        return model_result

    # Extract model name and status message from result
    active_model = model_result["response"]
    if "warning" in model_result:
        warning = model_result.get("warning", "")
        combined_warnings.append(warning)
        status_message = warning
    else:
        status_message = ""

    # Instantiate LLM with Direct API mode
    try:
        from backend.shared.constants import (
            CONVERSATION_CONFIG,
            get_tuning_defaults, EXTRACTION_TUNING
        )

        # Get usage-specific configuration
        if usage_type == 'conversation':
            config = CONVERSATION_CONFIG
            # Use provided tuning or defaults
            effective_tuning = tuning if tuning else get_tuning_defaults()
        elif usage_type == 'extraction':
            config = CONVERSATION_CONFIG  # disable_thinking共有(num_ctxは下の実効値=会話と同値でreload回避)
            # Extraction always uses fixed parameters (ignore provided tuning)
            effective_tuning = EXTRACTION_TUNING.copy()
        else:
            logger.warning(f"Unknown usage_type: {usage_type}, defaulting to conversation")
            config = CONVERSATION_CONFIG
            effective_tuning = tuning if tuning else get_tuning_defaults()

        # Get disable_thinking setting
        disable_thinking = config.get('disable_thinking', True)

        # num_ctx はユーザー設定(既定32000)をモデルのネイティブ文脈長で
        # クランプした実効値(C4.6)。conversation/extraction とも同じ値=
        # ctx違いによるモデル再ロードを避ける従来設計を維持
        from backend.llm.ollama_capabilities import get_effective_num_ctx
        effective_num_ctx = get_effective_num_ctx(active_model)

        # Create DirectOllamaChat instance with tuning parameters
        llm = DirectOllamaChat(
            model=active_model,
            temperature=effective_tuning.get("temperature", 0.7),
            top_p=effective_tuning.get("top_p", 0.9),
            top_k=effective_tuning.get("top_k", 40),
            min_p=effective_tuning.get("min_p", 0.05),
            repeat_last_n=effective_tuning.get("repeat_last_n", 500),
            num_predict=effective_tuning.get("num_predict", 800),
            repeat_penalty=effective_tuning.get("repeat_penalty", 1.0),
            presence_penalty=effective_tuning.get("presence_penalty", 0.0),
            frequency_penalty=effective_tuning.get("frequency_penalty", 0.0),
            num_ctx=effective_num_ctx,
            timeout=validated_timeout,
            disable_thinking=disable_thinking
        )

        logger.info(f"Created DirectOllamaChat for {usage_type}: model='{active_model}', "
                   f"num_ctx={effective_num_ctx}, num_predict={effective_tuning.get('num_predict')}, "
                   f"temp={effective_tuning.get('temperature')}, min_p={effective_tuning.get('min_p')}, "
                   f"repeat_last_n={effective_tuning.get('repeat_last_n')}, timeout={validated_timeout}s")

        # Return successful result with standardized dictionary format
        result = {
            "success": True,
            "response": llm,
            "model_used": active_model
        }

        # Add warnings if present
        if combined_warnings:
            result["warning"] = "; ".join(combined_warnings)

        return result

    except ValueError as e:
        error_msg = f"Invalid parameter for DirectOllamaChat with model '{active_model}': {e}"
        logger.error(error_msg)
        return {
            "success": False,
            "error": error_msg,
            "error_type": "VALIDATION",
            "details": {
                "model": active_model,
                "parameter_error": str(e),
                "tuning": effective_tuning if 'effective_tuning' in locals() else None,
                "validated_timeout": validated_timeout,
                "stage": "chat_ollama_creation"
            }
        }
    except MemoryError:
        error_msg = f"Insufficient memory to load model '{active_model}'"
        logger.error(error_msg)
        return {
            "success": False,
            "error": error_msg,
            "error_type": "RESOURCE_LIMIT",
            "details": {
                "model": active_model,
                "stage": "chat_ollama_creation"
            }
        }
    except Exception as e:
        error_msg = f"Unexpected error creating ChatOllama for model '{active_model}': {e}"
        logger.error(error_msg)
        return {
            "success": False,
            "error": error_msg,
            "error_type": "INTERNAL",
            "details": {
                "model": active_model,
                "exception_type": type(e).__name__,
                "stage": "chat_ollama_creation"
            }
        }
