# api_integration.py
"""
External API provider integration for Artificial Girlfriend.

Provides LLM client classes for OpenAI, Anthropic, xAI, and Google APIs,
with a unified factory function that dispatches based on model_provider.

Each client implements .invoke(messages) returning a response with .content,
matching the DirectOllamaChat interface from ollama_integration.py.
"""

import json
import logging
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Last API Request JSON storage (for UI prompt log)
# ============================================================================

_last_api_request_json: Optional[str] = None
_last_api_request_timestamp: Optional[str] = None
# トークン表示(2026-08-14 稜裁定): APIは応答usageの実測1本（常にフル値・
# 推定不要）。seqは並行リクエストでの実測取り違えを防ぐ通し番号。
_last_api_request_actual_tokens: Optional[int] = None
_last_api_request_model_label: Optional[str] = None
_last_api_request_seq: int = 0
_api_seq_counter: int = 0


def get_last_api_request_json() -> Dict[str, Any]:
    """Get the last API request JSON for UI display.

    Returns:
        Dict with JSON string, timestamp and token info
        (actual_tokens=応答usageの入力トークン実測・model_label=表示用).
    """
    return {
        "json": _last_api_request_json,
        "timestamp": _last_api_request_timestamp,
        "actual_tokens": _last_api_request_actual_tokens,
        "model_label": _last_api_request_model_label,
    }


def _b64_size_label(b64_str: str) -> str:
    """Convert base64 string length to approximate original size label."""
    size_kb = len(b64_str) * 3 // 4 // 1024
    return f"[image: {size_kb}KB]"


def _sanitize_api_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a copy of the API payload with base64 image data replaced by placeholders."""
    import copy
    sanitized = copy.deepcopy(payload)

    # Anthropic: messages[].content[].source.data (type == "image")
    for msg in sanitized.get("messages", []):
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "image":
                    source = block.get("source", {})
                    if "data" in source:
                        source["data"] = _b64_size_label(source["data"])

    # OpenAI / xAI Responses API: input[].content[].image_url (type == "input_image")
    for msg in sanitized.get("input", []):
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "input_image":
                    url = block.get("image_url", "")
                    if isinstance(url, str) and url.startswith("data:"):
                        parts = url.split(",", 1)
                        if len(parts) == 2:
                            block["image_url"] = _b64_size_label(parts[1])

    # Gemini: contents[].parts[].inline_data.data
    for content in sanitized.get("contents", []):
        if isinstance(content, dict):
            for part in content.get("parts", []):
                if isinstance(part, dict) and "inline_data" in part:
                    inline = part["inline_data"]
                    if "data" in inline:
                        inline["data"] = _b64_size_label(inline["data"])

    return sanitized


def _save_last_api_request_json(payload: Dict[str, Any],
                                model_label: Optional[str] = None) -> int:
    """Save the API request payload as formatted JSON string for UI display.

    Returns:
        このレコードの通し番号（usage実測追記の取り違えガード用）。
        保存失敗時は -1（追記は捨てられる）。
    """
    global _last_api_request_json, _last_api_request_timestamp, \
        _last_api_request_actual_tokens, _last_api_request_model_label, \
        _last_api_request_seq, _api_seq_counter
    _api_seq_counter += 1
    seq = _api_seq_counter
    try:
        sanitized = _sanitize_api_payload(payload)
        _last_api_request_json = json.dumps(sanitized, ensure_ascii=False, indent=2)
        _last_api_request_timestamp = datetime.now().isoformat()
        _last_api_request_actual_tokens = None
        _last_api_request_model_label = model_label
        _last_api_request_seq = seq
        _publish_prompt_tokens()
        return seq
    except Exception as e:
        logger.warning(f"Failed to save API request JSON for display: {e}")
        return -1


def _extract_input_tokens(data: Dict[str, Any]) -> int:
    """応答JSONから入力トークン実測を取り出す（3プロバイダ形式対応）。

    OpenAI/xAI Responses・Anthropic: usage.input_tokens（旧Chat Completions
    形式は usage.prompt_tokens）／ Gemini: usageMetadata.promptTokenCount。
    取れなければ 0。
    """
    usage = data.get("usage")
    if isinstance(usage, dict):
        n = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        if n:
            return int(n)
    meta = data.get("usageMetadata")
    if isinstance(meta, dict):
        return int(meta.get("promptTokenCount") or 0)
    return 0


def _attach_last_api_actual_tokens(seq: int, input_tokens: int) -> None:
    """応答usageの実測を保存済みレコードへ追記（seq不一致は捨てる）。"""
    global _last_api_request_actual_tokens
    if input_tokens > 0 and seq == _last_api_request_seq:
        _last_api_request_actual_tokens = input_tokens
        _publish_prompt_tokens()


def _publish_prompt_tokens() -> None:
    """トークン行をWS→JSのDOM直接更新で配信する（'prompt_token_info'）。

    gr.Timer出力の可視要素はちらつく既知問題のためGradioイベントを通さない
    （ollama_integration側と同方式）。配信失敗は握って続行。
    """
    try:
        from backend.shared.prompt_token_display import format_token_line
        from backend.shared.ui_events import publish_ui_update
        actual = _last_api_request_actual_tokens
        html = format_token_line({
            "provider": "api", "count": actual,
            "source": "actual" if actual else None,
            "num_ctx": None, "model": _last_api_request_model_label,
        })
        publish_ui_update("prompt_token_info", data={"html": html})
    except Exception as e:
        logger.debug(f"prompt token publish failed: {e}")


# ============================================================================
# Error Types
# ============================================================================

class APIError(Exception):
    """Base exception for external API provider errors."""
    pass


class APITimeoutError(APIError):
    """API request timed out."""
    pass


class APIConnectionError(APIError):
    """Failed to connect to API server."""
    pass


class APIAuthenticationError(APIError):
    """API authentication failed (invalid or expired key)."""
    pass


class APIRateLimitError(APIError):
    """API rate limit exceeded."""
    pass


class APIServiceError(APIError):
    """API returned an error response."""
    pass


# ============================================================================
# APIResponse
# ============================================================================

class APIResponse:
    """
    Response object for external API calls.

    Mirrors OllamaResponse interface:
    - .content for text access
    - [-1].content for backward compatibility
    - hasattr(result, 'content') is True

    Attributes:
        content: AI response text
        model: Model name used
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/generated tokens
        raw_response: Raw API response dict
    """

    def __init__(
        self,
        content: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        raw_response: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None
    ):
        self.content = content
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raw_response = raw_response or {}
        self.tool_calls = tool_calls or []  # [{id, name, arguments}, ...]

    def __getitem__(self, index: int) -> 'APIResponse':
        """List-style access for backward compatibility (result[-1].content)."""
        if index == -1 or index == 0:
            return self
        raise IndexError(f"APIResponse only supports index 0 or -1, got {index}")

    def __len__(self) -> int:
        return 1

    def __iter__(self):
        yield self

    def __repr__(self) -> str:
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"APIResponse(content='{preview}', model='{self.model}')"

    def __str__(self) -> str:
        return self.content


# ============================================================================
# Tool Unsupported Error Detection
# ============================================================================

# Each provider returns a distinct error message when a model does not support
# server-side tools (web_search, x_search, google_search).  These patterns are
# checked against the error string to decide whether an automatic retry without
# tools is worthwhile.
_TOOL_UNSUPPORTED_PATTERNS = [
    "is not supported with",                        # OpenAI
    "does not support tool types",                   # Anthropic
    "is not supported when using server-side tools", # xAI
    "Search as tool is not enabled for",             # Gemini
]

# Each provider returns a distinct error when a model does not support image
# inputs.  These patterns trigger an automatic retry without images.
_IMAGE_UNSUPPORTED_PATTERNS = [
    "does not support image inputs",         # OpenAI
    "Image inputs are not supported",        # xAI
    "Image input modality is not enabled",   # Gemini
    # Anthropic: all current models support images; add pattern here when needed
]


# ============================================================================
# Base API Chat Client
# ============================================================================

class BaseAPIChat:
    """
    Base class for external API LLM clients.

    Subclasses implement provider-specific methods:
    - _get_url(): API endpoint URL
    - _get_headers(): HTTP headers including auth
    - _convert_messages(): Transform standard messages to provider format
    - _build_payload(): Build the request body
    - _parse_response(): Parse response JSON into APIResponse
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        timeout: float = 180.0
    ):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def invoke(self, messages: List[Any], tools: Optional[List[Dict]] = None, **kwargs) -> APIResponse:
        """
        Send messages to the API and return the response.

        Args:
            messages: List of message dicts [{"role": "...", "content": "..."}]
                      or LangChain Message objects.
            tools: Optional list of tool definitions (provider-specific format).

        Returns:
            APIResponse with .content for text access and .tool_calls for tool calls.

        Raises:
            APITimeoutError: Request timed out
            APIConnectionError: Failed to connect
            APIAuthenticationError: Invalid API key
            APIRateLimitError: Rate limit exceeded
            APIServiceError: Other API errors
        """
        standardized = self._standardize_messages(messages)

        # Proactively strip images if model is known to not support them
        if self._has_images(standardized) and self._is_image_blacklisted():
            logger.info(
                f"[{self.__class__.__name__}] Stripping images for "
                f"blacklisted model {self.model}"
            )
            standardized = self._strip_images(standardized)

        converted = self._convert_messages(standardized)
        payload = self._build_payload(converted, tools=tools)

        # Send request with auto-retry for unsupported features.
        # At most 2 retries: one for tools, one for images.
        retries_remaining = 2
        while True:
            try:
                data = self._make_request(payload)
                break
            except APIServiceError as e:
                error_msg = str(e)

                # Auto-detect tool-unsupported error
                if retries_remaining > 0 and "tools" in payload and \
                   self._is_tool_unsupported_error(error_msg):
                    logger.warning(
                        f"[{self.__class__.__name__}] Tool not supported for "
                        f"{self.model}, retrying without tools"
                    )
                    self._auto_blacklist_model()
                    payload.pop("tools", None)
                    retries_remaining -= 1
                    continue

                # Auto-detect image-unsupported error
                if retries_remaining > 0 and self._has_images(standardized) and \
                   self._is_image_unsupported_error(error_msg):
                    logger.warning(
                        f"[{self.__class__.__name__}] Image not supported for "
                        f"{self.model}, retrying without images"
                    )
                    self._auto_blacklist_model_image()
                    standardized = self._strip_images(standardized)
                    converted = self._convert_messages(standardized)
                    # tools はツール非対応リトライで外れていない限り引き継ぐ
                    # (旧実装は tools を渡し忘れ、画像リトライ時だけ黙ってツールが脱落していた)
                    payload = self._build_payload(
                        converted, tools=tools if "tools" in payload else None
                    )
                    retries_remaining -= 1
                    continue

                raise

        response = self._parse_response(data)

        # Remove <think> tags (safety measure for all providers)
        if response.content:
            from backend.llm.ollama_integration import remove_think_tags
            original_len = len(response.content)
            response.content = remove_think_tags(response.content)
            if len(response.content) != original_len:
                logger.debug(f"[{self.__class__.__name__}] Removed <think> tags: "
                           f"{original_len} -> {len(response.content)} chars")

        return response

    def _standardize_messages(self, messages: List[Any]) -> List[Dict[str, Any]]:
        """Convert various message formats to standard dict format, preserving images."""
        result = []
        for msg in messages:
            if isinstance(msg, dict):
                # Pass through Responses API items (type-based, no role key)
                # e.g. function_call_output, function_call, message items
                if "type" in msg and "role" not in msg:
                    # Flatten response_output wrapper into individual items
                    if msg["type"] == "response_output":
                        for item in msg.get("items", []):
                            result.append(item)
                    else:
                        result.append(msg)
                    continue
                # Pass through Gemini API format messages (have "parts", no "content")
                # e.g. model messages with functionCall, function role with functionResponse
                if "parts" in msg and "content" not in msg:
                    result.append(msg)
                    continue
                standardized = {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                }
                if msg.get("images"):
                    standardized["images"] = msg["images"]
                result.append(standardized)
            elif hasattr(msg, 'content'):
                # LangChain message compatibility
                class_name = msg.__class__.__name__
                if "System" in class_name:
                    role = "system"
                elif "Human" in class_name:
                    role = "user"
                elif "AI" in class_name:
                    role = "assistant"
                else:
                    role = "user"
                result.append({"role": role, "content": msg.content})
            else:
                logger.warning(f"[{self.__class__.__name__}] Unknown message type: {type(msg)}")
                result.append({"role": "user", "content": str(msg)})
        return result

    def _make_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Make HTTP POST request with error handling."""
        url = self._get_url()
        headers = self._get_headers()

        # Save for UI prompt log (before sending, like Ollama)
        provider = getattr(self, "PROVIDER_NAME", "") or self.__class__.__name__
        try:
            from backend.shared.api_settings import get_provider_display_name
            provider = get_provider_display_name(provider)
        except Exception:
            pass
        request_seq = _save_last_api_request_json(
            payload, f"{self.model} ({provider})")

        logger.debug(f"[{self.__class__.__name__}] POST {url} model={self.model}")

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )
        except requests.exceptions.Timeout:
            raise APITimeoutError(
                f"Request to {self.__class__.__name__} timed out after {self.timeout}s"
            )
        except requests.exceptions.ConnectionError as e:
            raise APIConnectionError(
                f"Failed to connect to {self.__class__.__name__}: {e}"
            )
        except requests.exceptions.RequestException as e:
            raise APIServiceError(
                f"Request to {self.__class__.__name__} failed: {e}"
            )

        # Handle HTTP error status codes
        if response.status_code == 401:
            raise APIAuthenticationError(
                f"Invalid or expired API key (401)"
            )
        elif response.status_code == 403:
            raise APIAuthenticationError(
                f"Access denied (403)"
            )
        elif response.status_code == 429:
            raise APIRateLimitError(
                f"Rate limit exceeded (429)"
            )
        elif response.status_code >= 500:
            try:
                error_data = response.json()
                error_msg = self._extract_error_message(error_data)
            except Exception:
                error_msg = response.text[:500]
            raise APIServiceError(
                f"Server error ({response.status_code}): {error_msg}"
            )
        elif not response.ok:
            # Other non-2xx status codes
            try:
                error_data = response.json()
                error_msg = self._extract_error_message(error_data)
            except Exception:
                error_msg = response.text[:500]
            raise APIServiceError(
                f"HTTP {response.status_code}: {error_msg}"
            )

        try:
            data = response.json()
        except ValueError:
            raise APIServiceError("Invalid JSON response from API")
        _attach_last_api_actual_tokens(request_seq, _extract_input_tokens(data))
        return data

    def _extract_error_message(self, error_data: Dict) -> str:
        """Extract human-readable error message from API error response."""
        if "error" in error_data:
            err = error_data["error"]
            if isinstance(err, dict):
                return err.get("message", str(err))
            return str(err)
        return str(error_data)[:500]

    # --- Tool blacklist helpers ---

    @staticmethod
    def _is_tool_unsupported_error(error_message: str) -> bool:
        """Check if an error message indicates tool incompatibility."""
        msg_lower = error_message.lower()
        return any(p.lower() in msg_lower for p in _TOOL_UNSUPPORTED_PATTERNS)

    def _auto_blacklist_model(self) -> None:
        """Auto-blacklist the current model for web search tools."""
        provider = getattr(self, 'PROVIDER_NAME', '')
        if provider and self.model:
            from backend.shared.api_settings import add_to_web_search_blacklist
            add_to_web_search_blacklist(provider, self.model)
            logger.info(
                f"[{self.__class__.__name__}] Auto-blacklisted "
                f"{provider}::{self.model} for web search tools"
            )

    # --- Image blacklist helpers ---

    @staticmethod
    def _is_image_unsupported_error(error_message: str) -> bool:
        """Check if an error message indicates image input incompatibility."""
        msg_lower = error_message.lower()
        return any(p.lower() in msg_lower for p in _IMAGE_UNSUPPORTED_PATTERNS)

    def _auto_blacklist_model_image(self) -> None:
        """Auto-blacklist the current model for image inputs."""
        provider = getattr(self, 'PROVIDER_NAME', '')
        if provider and self.model:
            from backend.shared.api_settings import add_to_image_blacklist
            add_to_image_blacklist(provider, self.model)
            logger.info(
                f"[{self.__class__.__name__}] Auto-blacklisted "
                f"{provider}::{self.model} for image inputs"
            )

    def _is_image_blacklisted(self) -> bool:
        """Check if the current model is in the image blacklist."""
        provider = getattr(self, 'PROVIDER_NAME', '')
        if not provider:
            return False
        from backend.shared.api_settings import load_api_settings
        settings = load_api_settings()
        blacklist = settings.get("image_blacklist", [])
        return f"{provider}::{self.model}" in blacklist

    @staticmethod
    def _has_images(standardized: list) -> bool:
        """Check if any standardized message contains images."""
        return any(msg.get("images") for msg in standardized)

    @staticmethod
    def _strip_images(standardized: list) -> list:
        """Return a copy of standardized messages with images removed.

        _standardize_messages は role/content キーを持たないアイテム
        (Responses API の function_call_output 等)を素通しするため、
        ここでも同様に素通しする(旧実装は m["role"] で KeyError になり、
        画像ブラックリスト済みモデル+ツールループ継続でクラッシュしていた)。
        """
        return [
            {"role": m["role"], "content": m["content"]}
            if ("role" in m and "content" in m) else m
            for m in standardized
        ]

    # --- Methods to override in subclasses ---

    def _get_url(self) -> str:
        raise NotImplementedError

    def _get_headers(self) -> Dict[str, str]:
        raise NotImplementedError

    def _convert_messages(self, messages: List[Dict[str, str]]) -> Any:
        raise NotImplementedError

    def _build_payload(self, converted: Any, tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def _parse_response(self, data: Dict[str, Any]) -> APIResponse:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model='{self.model}')"


# ============================================================================
# Image Encoding Helper
# ============================================================================

# Maximum image size in bytes (Anthropic is most restrictive at 5MB)
_MAX_IMAGE_BYTES = 4_800_000  # 4.8MB - safe margin under 5MB limit


def _compress_image_bytes(file_path: str, max_bytes: int = _MAX_IMAGE_BYTES) -> Optional[bytes]:
    """
    Read an image file and compress it if it exceeds max_bytes.

    Uses progressive JPEG quality reduction and resolution downscaling.

    Args:
        file_path: Path to the image file.
        max_bytes: Maximum allowed size in bytes.

    Returns:
        Compressed image bytes, or None on error.
    """
    from pathlib import Path
    import io

    path = Path(file_path)
    if not path.exists():
        logger.warning(f"Image file not found: {file_path}")
        return None

    # Read original file
    raw_data = path.read_bytes()

    # If already under limit, return as-is
    if len(raw_data) <= max_bytes:
        return raw_data

    # Need compression - use Pillow
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw_data))

        # Convert RGBA/palette to RGB for JPEG compression
        if img.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        original_size = len(raw_data)

        # Strategy 1: Try JPEG quality reduction at current resolution
        for quality in (85, 70, 55, 40):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                logger.info(f"Image compressed: {original_size} -> {buf.tell()} bytes (quality={quality})")
                return buf.getvalue()

        # Strategy 2: Downscale resolution progressively
        for scale in (0.75, 0.5, 0.35, 0.25):
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            if new_w < 100 or new_h < 100:
                break
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=60, optimize=True)
            if buf.tell() <= max_bytes:
                logger.info(
                    f"Image compressed: {original_size} -> {buf.tell()} bytes "
                    f"(scale={scale}, {new_w}x{new_h})"
                )
                return buf.getvalue()

        # Last resort: aggressive downscale
        resized = img.resize((800, int(800 * img.height / img.width)), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=40, optimize=True)
        logger.warning(
            f"Image aggressively compressed: {original_size} -> {buf.tell()} bytes (800px wide)"
        )
        return buf.getvalue()

    except ImportError:
        logger.error("Pillow not installed - cannot compress large image")
        return raw_data  # Return original, let API reject it
    except Exception as e:
        logger.error(f"Image compression failed for {file_path}: {e}")
        return raw_data


def _encode_image_file(file_path: str) -> Optional[Dict[str, str]]:
    """
    Encode an image file to base64 with MIME type detection.
    Automatically compresses images exceeding the API size limit.

    Args:
        file_path: Path to image file.

    Returns:
        Dict with "base64" and "mime_type" keys, or None on error.
    """
    import base64

    try:
        image_bytes = _compress_image_bytes(file_path)
        if image_bytes is None:
            return None

        data = base64.b64encode(image_bytes).decode("utf-8")

        # Determine MIME type from original file extension
        from pathlib import Path
        ext = Path(file_path).suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }

        # If compression was applied (original was > limit), output is JPEG
        original_size = Path(file_path).stat().st_size
        if original_size > _MAX_IMAGE_BYTES:
            mime_type = "image/jpeg"
        else:
            mime_type = mime_map.get(ext, "image/png")

        return {"base64": data, "mime_type": mime_type}
    except Exception as e:
        logger.error(f"Failed to encode image {file_path}: {e}")
        return None


# ============================================================================
# Provider Client Classes
# ============================================================================

class ResponsesAPIChat(BaseAPIChat):
    """
    Base class for Responses API format (shared by OpenAI and xAI).

    Provides common logic for the Responses API structure.
    Not instantiated directly — use OpenAIChat or XAIChat instead.
    """

    def __init__(self, base_url: str, system_role: str = "developer", **kwargs):
        super().__init__(**kwargs)
        self.base_url = base_url
        self.system_role = system_role

    def _get_url(self) -> str:
        return f"{self.base_url}/v1/responses"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert system role to provider-specific role name, with image support."""
        converted = []
        for msg in messages:
            # Pass through Responses API items (function_call, message, function_call_output)
            if "type" in msg and "role" not in msg:
                converted.append(msg)
                continue
            role = msg["role"]
            if role == "system":
                role = self.system_role

            images = msg.get("images", [])
            if images and role != self.system_role:
                # Build content array with text + images
                content_parts = [{"type": "input_text", "text": msg["content"]}]
                for img_path in images:
                    encoded = _encode_image_file(img_path)
                    if encoded:
                        content_parts.append({
                            "type": "input_image",
                            "image_url": f"data:{encoded['mime_type']};base64,{encoded['base64']}"
                        })
                converted.append({"role": role, "content": content_parts})
            else:
                converted.append({"role": role, "content": msg["content"]})
        return converted

    def _build_payload(self, converted: List[Dict[str, str]], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        return {
            "model": self.model,
            "input": converted,
        }

    def _parse_response(self, data: Dict[str, Any]) -> APIResponse:
        try:
            output_items = data.get("output", [])

            # Responses API output may contain multiple items:
            # message blocks, function_call blocks, reasoning blocks.
            text = None
            tool_calls = []

            for output_item in output_items:
                # Function call items (execute_command tool calls)
                if output_item.get("type") == "function_call":
                    try:
                        arguments = json.loads(output_item.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                    tool_calls.append({
                        "id": output_item.get("call_id", output_item.get("id", "")),
                        "name": output_item.get("name", ""),
                        "arguments": arguments
                    })
                    continue

                # Try "content" array (message-type items)
                for content_item in output_item.get("content", []):
                    if isinstance(content_item, dict):
                        if content_item.get("type") == "output_text" and "text" in content_item:
                            text = content_item["text"]
                            break
                        elif "text" in content_item and content_item.get("type") != "refusal":
                            text = content_item["text"]
                            break
                if text is not None and not tool_calls:
                    # Keep searching for tool_calls in remaining items
                    pass

            # Text may be None when response is tool_call only — that's valid
            if text is None and not tool_calls:
                logger.error(f"[ResponsesAPI] Could not extract text or tool_calls. Full output: {output_items}")
                raise APIServiceError(
                    "Could not extract text from Responses API output"
                )
        except APIServiceError:
            raise
        except (KeyError, IndexError, TypeError) as e:
            raise APIServiceError(f"Unexpected response format from Responses API: {e}")

        # Strip inline citation markers from web search responses
        if text:
            text = self._strip_citation_markers(text)

        usage = data.get("usage", {})
        return APIResponse(
            content=text or "",
            model=data.get("model", self.model),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw_response=data,
            tool_calls=tool_calls
        )

    @staticmethod
    def _strip_citation_markers(text: str) -> str:
        """Remove inline citation markers inserted by web search.

        Handles two formats:
        - OpenAI: ([text](URL))
        - xAI:    [[N]](URL)
        """
        import re
        # [[N]](URL) — numbered reference links (xAI style)
        text = re.sub(r'\[\[\d+\]\]\([^)]+\)', '', text)
        # ([text](URL)) — parenthesized citation links (OpenAI style)
        text = re.sub(r'\(\[([^\]]*)\]\([^)]+\)\)', '', text)
        # Clean up extra whitespace left behind
        text = re.sub(r' {2,}', ' ', text)
        return text


class OpenAIChat(ResponsesAPIChat):
    """
    Client for OpenAI Responses API.

    Uses base_url=api.openai.com and system_role="developer".
    Supports web_search tool when enabled in api_settings.
    """

    PROVIDER_NAME = "openai"

    def __init__(self, **kwargs):
        super().__init__(
            base_url="https://api.openai.com",
            system_role="developer",
            **kwargs
        )

    def _build_payload(self, converted: List[Dict[str, str]], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        payload = super()._build_payload(converted, tools=tools)
        all_tools = []
        from backend.shared.api_settings import load_api_settings
        settings = load_api_settings()
        if settings.get("openai", {}).get("web_search_enabled", False):
            blacklist = settings.get("web_search_blacklist", [])
            if f"openai::{self.model}" not in blacklist:
                all_tools.append({"type": "web_search"})
        if tools:
            all_tools.extend(tools)
        if all_tools:
            payload["tools"] = all_tools
        return payload


class XAIChat(ResponsesAPIChat):
    """
    Client for xAI (Grok) Responses API.

    Uses base_url=api.x.ai and system_role="system".
    Supports web_search and x_search tools when enabled in api_settings.
    """

    PROVIDER_NAME = "xai"

    def __init__(self, **kwargs):
        super().__init__(
            base_url="https://api.x.ai",
            system_role="system",
            **kwargs
        )

    def _build_payload(self, converted: List[Dict[str, str]], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        payload = super()._build_payload(converted, tools=tools)
        all_tools = []
        from backend.shared.api_settings import load_api_settings
        settings = load_api_settings()
        xai_settings = settings.get("xai", {})
        blacklist = settings.get("web_search_blacklist", [])
        if f"xai::{self.model}" not in blacklist:
            if xai_settings.get("web_search_enabled", False):
                all_tools.append({"type": "web_search"})
            if xai_settings.get("x_search_enabled", False):
                all_tools.append({"type": "x_search"})
        if tools:
            all_tools.extend(tools)
        if all_tools:
            payload["tools"] = all_tools
        return payload


class AnthropicChat(BaseAPIChat):
    """
    Client for Anthropic Messages API.

    Key differences from other providers:
    - System prompt is a separate "system" parameter, not in messages array
    - messages must alternate user/assistant roles
    - max_tokens is mandatory
    - temperature range is 0.0-1.0 (not 0.0-2.0)
    """

    PROVIDER_NAME = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str,
        timeout: float = 180.0,
        auto_inject_web_search: bool = True,
        enable_prompt_caching: bool = False,
    ):
        super().__init__(model=model, api_key=api_key, timeout=timeout)
        # Controls whether the web_search tool is auto-added when the user
        # setting `anthropic.web_search_enabled` is true. ELYTH sessions set
        # this to False to avoid injecting an unused tool.
        self.auto_inject_web_search = auto_inject_web_search
        # When True, the last content block of the last message receives
        # `cache_control: ephemeral`, enabling within-session prompt caching.
        self.enable_prompt_caching = enable_prompt_caching

    def _get_url(self) -> str:
        return "https://api.anthropic.com/v1/messages"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01"
        }

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Separate system messages and ensure alternating roles, with image support."""
        system_parts = []
        conversation = []

        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(msg["content"])
            else:
                images = msg.get("images", [])
                if images:
                    # Build content array with text + images for Anthropic
                    content_parts = [{"type": "text", "text": msg["content"]}]
                    for img_path in images:
                        encoded = _encode_image_file(img_path)
                        if encoded:
                            content_parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": encoded["mime_type"],
                                    "data": encoded["base64"]
                                }
                            })
                    conversation.append({
                        "role": msg["role"],
                        "content": content_parts
                    })
                else:
                    conversation.append({
                        "role": msg["role"],
                        "content": msg["content"]
                    })

        # Anthropic requires alternating user/assistant messages.
        # Merge consecutive messages with the same role.
        merged = []
        for msg in conversation:
            if merged and merged[-1]["role"] == msg["role"]:
                # Handle merging when content may be string or list
                prev_content = merged[-1]["content"]
                curr_content = msg["content"]
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    merged[-1]["content"] = prev_content + "\n\n" + curr_content
                else:
                    # Convert both to list format and concatenate
                    if isinstance(prev_content, str):
                        prev_content = [{"type": "text", "text": prev_content}]
                    if isinstance(curr_content, str):
                        curr_content = [{"type": "text", "text": curr_content}]
                    merged[-1]["content"] = prev_content + curr_content
            else:
                merged.append(msg.copy())

        system_text = "\n\n".join(system_parts) if system_parts else None

        # Prompt caching: place a single ephemeral cache_control breakpoint on
        # the last content block of the last message. Anthropic resolves the
        # longest matching cached prefix automatically, so one moving
        # breakpoint per call is sufficient for within-session caching.
        if self.enable_prompt_caching and merged:
            last_msg = merged[-1]
            content = last_msg.get("content")
            if isinstance(content, str):
                # Promote string content to a text block carrying cache_control
                last_msg["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }]
            elif isinstance(content, list) and content:
                # Replace the final block with a copy that has cache_control,
                # avoiding in-place mutation of the caller's dict
                new_content = list(content[:-1])
                last_block = content[-1]
                if isinstance(last_block, dict):
                    new_content.append({
                        **last_block,
                        "cache_control": {"type": "ephemeral"},
                    })
                else:
                    new_content.append(last_block)
                last_msg["content"] = new_content

        return {"system": system_text, "messages": merged}

    def _build_payload(self, converted: Dict[str, Any], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        # max_tokens は Anthropic では必須パラメータ。旧値1000は日本語≈1字1トークン強で
        # 記憶抽出のJSONを無音切断し、7/20以降の抽出全滅の根本原因だった(2026-07-25実測)。
        # 16000は非ストリーミングHTTPの推奨上限で、現行全モデルの出力上限(最小32K)内。
        payload = {
            "model": self.model,
            "max_tokens": 16000,
            "messages": converted["messages"],
        }
        if converted["system"]:
            payload["system"] = converted["system"]

        all_tools = []
        if self.auto_inject_web_search:
            from backend.shared.api_settings import load_api_settings
            settings = load_api_settings()
            if settings.get("anthropic", {}).get("web_search_enabled", False):
                blacklist = settings.get("web_search_blacklist", [])
                if f"anthropic::{self.model}" not in blacklist:
                    all_tools.append({
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 5
                    })
        if tools:
            all_tools.extend(tools)
        if all_tools:
            payload["tools"] = all_tools

        return payload

    def _parse_response(self, data: Dict[str, Any]) -> APIResponse:
        try:
            text_parts = []
            tool_calls = []

            for block in data.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input", {})
                    })
                # Skip server_tool_use, web_search_tool_result, etc.

            text = "".join(text_parts)

            if not text and not tool_calls:
                # Anthropic may return end_turn with no content after tool results.
                # Treat as empty-text response instead of error.
                text = ""
        except APIServiceError:
            raise
        except (KeyError, IndexError, TypeError) as e:
            raise APIServiceError(f"Unexpected response format from Anthropic: {e}")

        # max_tokens 切断は正常応答と同じ形で返る=無音で壊れる(抽出JSON切断の教訓)。
        # 上限に当たったら必ずログに残す。
        if data.get("stop_reason") == "max_tokens":
            logger.warning(
                f"[Anthropic] Response truncated at max_tokens "
                f"(model={data.get('model', self.model)}, output was cut off)")

        usage = data.get("usage", {})
        return APIResponse(
            content=text,
            model=data.get("model", self.model),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw_response=data,
            tool_calls=tool_calls
        )


class GeminiChat(BaseAPIChat):
    """
    Client for Google Gemini generateContent API.

    Key differences from other providers:
    - Model name is part of the URL
    - System prompt uses "systemInstruction" field
    - Role "assistant" maps to "model"
    - Content wrapped in "parts" array
    - Auth via x-goog-api-key header
    """

    PROVIDER_NAME = "google"

    def _get_url(self) -> str:
        return (
            f"https://generativelanguage.googleapis.com"
            f"/v1beta/models/{self.model}:generateContent"
        )

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key
        }

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Convert to Gemini format: parts-based contents + systemInstruction, with image support."""
        system_parts = []
        contents = []

        for msg in messages:
            # Pass through already-formatted Gemini messages (have "parts")
            if "parts" in msg and "content" not in msg:
                contents.append(msg)
                continue
            if msg["role"] == "system":
                system_parts.append(msg["content"])
            else:
                # assistant → model
                role = "model" if msg["role"] == "assistant" else "user"
                parts = [{"text": msg["content"]}]

                # Add images as inline_data parts
                for img_path in msg.get("images", []):
                    encoded = _encode_image_file(img_path)
                    if encoded:
                        parts.append({
                            "inline_data": {
                                "mime_type": encoded["mime_type"],
                                "data": encoded["base64"]
                            }
                        })

                contents.append({
                    "role": role,
                    "parts": parts
                })

        # Gemini also requires alternating user/model roles.
        # Merge consecutive messages with the same role.
        merged = []
        for item in contents:
            if merged and merged[-1]["role"] == item["role"]:
                merged[-1]["parts"].extend(item["parts"])
            else:
                merged.append(item)

        system_instruction = None
        if system_parts:
            system_instruction = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }

        return {"systemInstruction": system_instruction, "contents": merged}

    def _build_payload(self, converted: Dict[str, Any], tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        payload = {
            "contents": converted["contents"],
        }
        if converted["systemInstruction"]:
            payload["systemInstruction"] = converted["systemInstruction"]

        # Gemini API does not allow combining built-in tools (google_search)
        # with custom tools (Function Calling) in the same request.
        # If function calling tools are provided, skip google_search.
        if tools:
            payload["tools"] = list(tools)
        else:
            from backend.shared.api_settings import load_api_settings
            settings = load_api_settings()
            if settings.get("google", {}).get("web_search_enabled", False):
                blacklist = settings.get("web_search_blacklist", [])
                if f"google::{self.model}" not in blacklist:
                    payload["tools"] = [{"google_search": {}}]

        return payload

    def _parse_response(self, data: Dict[str, Any]) -> APIResponse:
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = ""
            tool_calls = []

            for idx, part in enumerate(parts):
                if "text" in part:
                    # Accumulate: Gemini can return multiple text parts (function-call
                    # mix / thinking models). 代入だと最後の1個しか残らない
                    # (Anthropic 側は join 済み)。
                    text += part["text"]
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append({
                        # part index ベースの安定ID。旧 id(fc)(メモリアドレス)は
                        # GC後にアドレス再利用され、ELYTH ログ経由で別プロバイダへ
                        # 再送される際に衝突しうる。
                        "id": f"{fc.get('name', 'tool')}_{idx}",
                        "name": fc.get("name", ""),
                        "arguments": fc.get("args", {})
                    })

            if not text and not tool_calls:
                raise APIServiceError("No text or function calls in Gemini response")

        except APIServiceError:
            raise
        except (KeyError, IndexError, TypeError) as e:
            raise APIServiceError(f"Unexpected response format from Gemini: {e}")

        usage = data.get("usageMetadata", {})
        return APIResponse(
            content=text,
            model=self.model,
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            raw_response=data,
            tool_calls=tool_calls
        )


# ============================================================================
# Tool Result Message Formatting
# ============================================================================

def format_tool_result_message(
    provider: str,
    tool_call_id: str,
    tool_name: str,
    content: str
) -> Dict[str, Any]:
    """
    Build a tool result message in the provider-specific format.

    Args:
        provider: 'openai', 'xai', 'anthropic', 'google', or 'ollama'
        tool_call_id: The ID from the original tool_call
        tool_name: The tool function name (e.g. 'execute_command')
        content: The result text to return to the LLM

    Returns:
        Dict formatted as the provider expects for tool results.
    """
    if provider == "ollama":
        # Ollama /api/chat: role "tool" メッセージ。idは往復不要。
        # tool_name は新しめのOllamaがモデルへの対応付けに使う
        # (旧版は未知フィールドとして無視する)。
        return {
            "role": "tool",
            "content": content,
            "tool_name": tool_name
        }
    elif provider in ("openai", "xai"):
        # Responses API: function_call_output item
        return {
            "type": "function_call_output",
            "call_id": tool_call_id,
            "output": content
        }
    elif provider == "anthropic":
        # Anthropic Messages API: user message with tool_result block
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": content
            }]
        }
    elif provider == "google":
        # Gemini: function response
        return {
            "role": "function",
            "parts": [{
                "functionResponse": {
                    "name": tool_name,
                    "response": {"result": content}
                }
            }]
        }
    else:
        # Fallback: generic user message
        return {
            "role": "user",
            "content": f"[Tool Result] {content}"
        }


def build_assistant_msg_with_tool_calls(
    response: APIResponse,
    provider: str
) -> Dict[str, Any]:
    """
    Reconstruct the assistant message including tool_calls for the message history.

    This is needed because when sending tool_results back to the LLM,
    the preceding assistant message (with tool_calls) must be included.

    Args:
        response: The APIResponse that contained tool_calls
        provider: Provider identifier

    Returns:
        Dict formatted as the provider expects for assistant messages with tool calls.
    """
    if provider == "ollama":
        # Ollama: 応答と同形の tool_calls を履歴へ積み直す（argumentsはdict）。
        # response は OllamaResponse（.content / .tool_calls のダックタイプ）。
        return {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in response.tool_calls
            ]
        }
    elif provider in ("openai", "xai"):
        # For Responses API, the output items from the response
        # are automatically tracked by the API. We return the raw output
        # so it can be re-sent as context.
        items = []
        raw_output = response.raw_response.get("output", [])
        for item in raw_output:
            if item.get("type") in ("message", "function_call"):
                items.append(item)
        # The Responses API expects previous output as part of input
        return {"type": "response_output", "items": items}

    elif provider == "anthropic":
        # Anthropic: assistant message with content blocks
        content_blocks = []
        if response.content:
            content_blocks.append({"type": "text", "text": response.content})
        for tc in response.tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["arguments"]
            })
        return {"role": "assistant", "content": content_blocks}

    elif provider == "google":
        # Preserve raw content including thought_signature for function calls
        try:
            return response.raw_response["candidates"][0]["content"]
        except (KeyError, IndexError):
            # Fallback: reconstruct (thought_signature will be missing)
            parts = []
            if response.content:
                parts.append({"text": response.content})
            for tc in response.tool_calls:
                parts.append({
                    "functionCall": {
                        "name": tc["name"],
                        "args": tc["arguments"]
                    }
                })
            return {"role": "model", "parts": parts}

    else:
        return {"role": "assistant", "content": response.content or ""}


# ============================================================================
# Factory Function
# ============================================================================

def create_llm_client(
    model_provider: str,
    model_name: str,
    tuning: Optional[Dict[str, Any]] = None,
    timeout: float = 180.0,
    usage_type: str = 'conversation',
    auto_inject_web_search: bool = True,
    enable_prompt_caching: bool = False,
) -> Dict[str, Any]:
    """
    Create an LLM client based on model provider.

    Dispatches to create_chat_ollama() for Ollama, or creates an API client
    for external providers. Returns the same dict format as create_chat_ollama().

    Args:
        model_provider: Provider identifier ('ollama', 'openai', 'anthropic', 'xai', 'google').
        model_name: Model name (e.g., 'gpt-4o', 'claude-sonnet-4-5-20250514').
        tuning: Tuning parameters dict (Ollama format). Ignored for extraction.
        timeout: Request timeout in seconds.
        usage_type: 'conversation' or 'extraction'.

    Returns:
        Dict with standardized response format:
            - success (bool): Whether creation was successful
            - response (Any): The LLM client instance if successful
            - model_used (str): The model name
            - error (str, optional): Error message if failed
            - error_type (str, optional): Error category
    """
    # Ollama path - delegate to existing function
    if model_provider == "ollama":
        from backend.llm.ollama_integration import create_chat_ollama
        try:
            return create_chat_ollama(
                model_name=model_name,
                tuning=tuning,
                timeout=timeout,
                usage_type=usage_type
            )
        except Exception as e:
            # create_chat_ollama は @handle_ollama_request_errors 経由でサーバ停止時に
            # OllamaConnectionError 等を raise する。外部APIパスは例外を dict に畳むのに
            # ollama だけ例外が素通りし「dict を返す」ファクトリ契約が破れていたため、
            # ここで同じ契約(dict)に統一する。
            # warning: この失敗dictは上位(conversation_manager)がERRORログ
            # する=コールドパス3段トーストの解消(稜裁定 2026-08-02)
            logger.warning(f"Failed to create Ollama client: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": "CONNECTION",
                "model_used": model_name,
            }

    # External API providers
    try:
        from backend.shared.api_settings import load_api_settings, PROVIDER_CONFIG

        # Get API key
        settings = load_api_settings()
        provider_settings = settings.get(model_provider, {})
        api_key = provider_settings.get("api_key", "")

        if not api_key:
            display_name = PROVIDER_CONFIG.get(model_provider, {}).get(
                "display_name", model_provider
            )
            return {
                "success": False,
                "error": f"{display_name} API key is not set.",
                "error_type": "AUTHENTICATION"
            }

        # Common kwargs for all API clients
        client_kwargs = {
            "model": model_name,
            "api_key": api_key,
            "timeout": timeout,
        }

        # Create provider-specific client
        if model_provider == "openai":
            client = OpenAIChat(**client_kwargs)
        elif model_provider == "xai":
            client = XAIChat(**client_kwargs)
        elif model_provider == "anthropic":
            client = AnthropicChat(
                **client_kwargs,
                auto_inject_web_search=auto_inject_web_search,
                enable_prompt_caching=enable_prompt_caching,
            )
        elif model_provider == "google":
            client = GeminiChat(**client_kwargs)
        else:
            return {
                "success": False,
                "error": f"Unknown model provider: {model_provider}",
                "error_type": "VALIDATION"
            }

        logger.info(
            f"[create_llm_client] Created {model_provider} client: "
            f"model='{model_name}', timeout={timeout}s"
        )

        return {
            "success": True,
            "response": client,
            "model_used": model_name
        }

    except Exception as e:
        logger.error(f"[create_llm_client] Failed to create {model_provider} client: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "INTERNAL"
        }


# ============================================================================
# Embedding API
# ============================================================================

def call_api_embedding(
    provider: str,
    model: str,
    text: str,
    timeout: float = 30.0
) -> Dict[str, Any]:
    """Generate an embedding via an external API provider.

    Supports openai, xai (OpenAI-compatible), and google.
    Anthropic does not offer an embeddings endpoint.

    Args:
        provider: Provider identifier ('openai', 'xai', 'google').
        model: Model name (e.g. 'text-embedding-3-small').
        text: Text to embed.
        timeout: Request timeout in seconds.

    Returns:
        Dict with keys:
            success (bool), embedding (List[float]), model (str)
        or on failure:
            success (bool), error (str), error_type (str)
    """
    if provider == "anthropic":
        return {
            "success": False,
            "error": "Anthropic does not provide an embeddings API.",
            "error_type": "UNSUPPORTED"
        }

    try:
        from backend.shared.api_settings import load_api_settings

        settings = load_api_settings()
        api_key = settings.get(provider, {}).get("api_key", "")
        if not api_key:
            return {
                "success": False,
                "error": f"API key for {provider} is not set.",
                "error_type": "AUTHENTICATION"
            }

        # Build request per provider
        if provider in ("openai", "xai"):
            base_url = "https://api.openai.com" if provider == "openai" else "https://api.x.ai"
            url = f"{base_url}/v1/embeddings"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            payload = {"model": model, "input": text}
        elif provider == "google":
            url = (
                f"https://generativelanguage.googleapis.com"
                f"/v1beta/models/{model}:embedContent"
            )
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            }
            payload = {"content": {"parts": [{"text": text}]}}
        else:
            return {
                "success": False,
                "error": f"Unknown embedding provider: {provider}",
                "error_type": "VALIDATION"
            }

        response = requests.post(url, headers=headers, json=payload, timeout=timeout)

        # Handle HTTP errors
        if response.status_code == 401 or response.status_code == 403:
            return {
                "success": False,
                "error": f"Authentication failed ({response.status_code})",
                "error_type": "AUTHENTICATION"
            }
        elif response.status_code == 429:
            return {
                "success": False,
                "error": "Rate limit exceeded (429)",
                "error_type": "RATE_LIMIT"
            }
        elif response.status_code >= 500:
            return {
                "success": False,
                "error": f"Server error ({response.status_code})",
                "error_type": "SERVER_ERROR"
            }
        elif not response.ok:
            try:
                err_body = response.json()
                err_msg = err_body.get("error", {}).get("message", response.text[:500])
            except Exception:
                err_msg = response.text[:500]
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {err_msg}",
                "error_type": "API_ERROR"
            }

        data = response.json()

        # Extract embedding vector
        if provider in ("openai", "xai"):
            embedding = data["data"][0]["embedding"]
        else:  # google
            embedding = data["embedding"]["values"]

        return {
            "success": True,
            "embedding": embedding,
            "model": model,
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": f"Embedding request timed out after {timeout}s",
            "error_type": "TIMEOUT"
        }
    except requests.exceptions.ConnectionError as e:
        return {
            "success": False,
            "error": f"Failed to connect to {provider} API: {e}",
            "error_type": "CONNECTION"
        }
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"[call_api_embedding] Unexpected response format from {provider}: {e}")
        return {
            "success": False,
            "error": f"Unexpected response format: {e}",
            "error_type": "PARSE_ERROR"
        }
    except Exception as e:
        logger.error(f"[call_api_embedding] Unexpected error for {provider}: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "INTERNAL"
        }


def call_api_embedding_batch(
    provider: str,
    model: str,
    texts: List[str],
    timeout: float = 30.0
) -> Dict[str, Any]:
    """Generate embeddings for multiple texts in a single API call.

    Only OpenAI-compatible providers (openai, xai) expose array-input batch
    embedding via `input: [...]`. For any other provider this returns
    success=False / UNSUPPORTED and the caller should fall back to per-text
    `call_api_embedding`.

    Args:
        provider: Provider identifier ('openai', 'xai').
        model: Model name (e.g. 'text-embedding-3-large').
        texts: List of texts to embed.
        timeout: Request timeout in seconds.

    Returns:
        Dict with keys:
            success (bool), embeddings (List[List[float]]), model (str)
            -- embeddings are returned in the same order as `texts`.
        or on failure:
            success (bool), error (str), error_type (str)
    """
    if provider not in ("openai", "xai"):
        return {
            "success": False,
            "error": f"Batch embedding not supported for provider: {provider}",
            "error_type": "UNSUPPORTED"
        }

    if not texts:
        return {"success": True, "embeddings": [], "model": model}

    try:
        from backend.shared.api_settings import load_api_settings

        settings = load_api_settings()
        api_key = settings.get(provider, {}).get("api_key", "")
        if not api_key:
            return {
                "success": False,
                "error": f"API key for {provider} is not set.",
                "error_type": "AUTHENTICATION"
            }

        base_url = "https://api.openai.com" if provider == "openai" else "https://api.x.ai"
        url = f"{base_url}/v1/embeddings"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {"model": model, "input": texts}

        response = requests.post(url, headers=headers, json=payload, timeout=timeout)

        # Handle HTTP errors (mirrors call_api_embedding)
        if response.status_code == 401 or response.status_code == 403:
            return {
                "success": False,
                "error": f"Authentication failed ({response.status_code})",
                "error_type": "AUTHENTICATION"
            }
        elif response.status_code == 429:
            return {
                "success": False,
                "error": "Rate limit exceeded (429)",
                "error_type": "RATE_LIMIT"
            }
        elif response.status_code >= 500:
            return {
                "success": False,
                "error": f"Server error ({response.status_code})",
                "error_type": "SERVER_ERROR"
            }
        elif not response.ok:
            try:
                err_body = response.json()
                err_msg = err_body.get("error", {}).get("message", response.text[:500])
            except Exception:
                err_msg = response.text[:500]
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {err_msg}",
                "error_type": "API_ERROR"
            }

        data = response.json()
        items = data["data"]
        if len(items) != len(texts):
            return {
                "success": False,
                "error": f"Embedding count mismatch: got {len(items)}, expected {len(texts)}",
                "error_type": "PARSE_ERROR"
            }

        # Order by `index` to guarantee alignment with the input `texts`
        items_sorted = sorted(items, key=lambda x: x.get("index", 0))
        embeddings = [it["embedding"] for it in items_sorted]

        return {
            "success": True,
            "embeddings": embeddings,
            "model": model,
        }

    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": f"Embedding request timed out after {timeout}s",
            "error_type": "TIMEOUT"
        }
    except requests.exceptions.ConnectionError as e:
        return {
            "success": False,
            "error": f"Failed to connect to {provider} API: {e}",
            "error_type": "CONNECTION"
        }
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"[call_api_embedding_batch] Unexpected response format from {provider}: {e}")
        return {
            "success": False,
            "error": f"Unexpected response format: {e}",
            "error_type": "PARSE_ERROR"
        }
    except Exception as e:
        logger.error(f"[call_api_embedding_batch] Unexpected error for {provider}: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "INTERNAL"
        }
