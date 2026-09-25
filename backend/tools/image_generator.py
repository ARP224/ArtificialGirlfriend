"""
backend/image_generator.py

Image generation via Function Calling for API providers.
Uses Google Imagen API to generate images.
Ollama: available on tools+vision capable models (2026-08-11).
"""

import base64
import logging
import requests
from typing import List, Dict, Any

from backend.shared.image_storage import save_generated_image
from backend.tools.tool_schemas import format_tools_for_provider, localize_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_GEN_TOOL_NAMES = frozenset({"generate_image"})

# API request timeout for image generation (seconds)
IMAGE_GEN_API_TIMEOUT = 60

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

GENERATE_IMAGE_TOOL = {
    "name": "generate_image",
    "description": "画像を生成する。自己表現や図解など、画像で表現するのが望ましいときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "生成したい画像の詳細な説明"
            }
        },
        "required": ["prompt"]
    }
}

_IMAGE_GEN_TOOLS = [GENERATE_IMAGE_TOOL]


# ---------------------------------------------------------------------------
# Provider formatting
# ---------------------------------------------------------------------------

def get_image_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return image generation tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools(_IMAGE_GEN_TOOLS, language))


# ---------------------------------------------------------------------------
# Image generation API call
# ---------------------------------------------------------------------------

def generate_image(
    prompt: str,
    model_name: str,
    api_key: str,
    language: str,
) -> Dict[str, Any]:
    """Call Google Imagen API to generate an image.

    Args:
        prompt: Image description text.
        model_name: Imagen model name (e.g., "imagen-3.0-generate-002").
        api_key: Google API key.

    Returns:
        Dict with:
            - success (bool)
            - image_bytes (bytes): Raw image data (on success)
            - mime_type (str): e.g., "image/png" (on success)
            - error (str): Error message (on failure)
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model_name}:predict"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
        },
    }

    from backend.shared.prompt_i18n import prompt_text
    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=IMAGE_GEN_API_TIMEOUT,
        )

        if response.status_code == 200:
            data = response.json()
            return _parse_imagen_response(data, language)
        else:
            error_msg = _parse_error_response(response)
            logger.error(
                f"[ImageGen] API error {response.status_code}: {error_msg}"
            )
            return {"success": False, "error": error_msg}

    except requests.exceptions.Timeout:
        logger.error("[ImageGen] API request timed out")
        return {"success": False, "error": prompt_text("res.image.api_timeout", language)}
    except requests.exceptions.ConnectionError:
        logger.error("[ImageGen] Cannot connect to API")
        return {"success": False, "error": prompt_text("res.image.api_unreachable", language)}
    except Exception as e:
        logger.error(f"[ImageGen] Unexpected error: {e}")
        return {"success": False, "error": prompt_text("res.image.api_unexpected", language, error=e)}


def _parse_imagen_response(data: Dict[str, Any], language: str) -> Dict[str, Any]:
    """Parse Imagen API response and extract image bytes.

    Handles multiple possible response formats from the Imagen API.
    """
    from backend.shared.prompt_i18n import prompt_text
    # Format 1: predictions[].bytesBase64Encoded
    predictions = data.get("predictions", [])
    if predictions:
        prediction = predictions[0]
        b64_data = prediction.get("bytesBase64Encoded", "")
        if b64_data:
            try:
                image_bytes = base64.b64decode(b64_data)
                mime_type = prediction.get("mimeType", "image/png")
                return {
                    "success": True,
                    "image_bytes": image_bytes,
                    "mime_type": mime_type,
                }
            except Exception as e:
                return {"success": False, "error": prompt_text("res.image.decode_failed", language, error=e)}

    # Format 2: generatedImages[].image.imageBytes
    generated = data.get("generatedImages", [])
    if generated:
        image_data = generated[0].get("image", {})
        b64_data = image_data.get("imageBytes", "")
        if b64_data:
            try:
                image_bytes = base64.b64decode(b64_data)
                return {
                    "success": True,
                    "image_bytes": image_bytes,
                    "mime_type": "image/png",
                }
            except Exception as e:
                return {"success": False, "error": prompt_text("res.image.decode_failed", language, error=e)}

    # No recognizable image data
    logger.error(f"[ImageGen] Unrecognized response format: {list(data.keys())}")
    return {
        "success": False,
        "error": prompt_text("res.image.api_bad_response", language),
    }


def _parse_error_response(response: requests.Response) -> str:
    """Extract error message from API error response."""
    try:
        data = response.json()
        error = data.get("error", {})
        if isinstance(error, dict):
            message = error.get("message", "")
            status = error.get("status", "")
            if message:
                return f"{status}: {message}" if status else message
        return f"HTTP {response.status_code}"
    except Exception:
        return f"HTTP {response.status_code}: {response.text[:200]}"


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch_image_tool(
    character_id: str,
    tool_call: dict,
    model_name: str,
    api_key: str,
    language: str,
) -> Dict[str, Any]:
    """Dispatch an image generation tool call.

    Args:
        character_id: Character UUID.
        tool_call: Tool call dict with name and arguments.
        model_name: Imagen model name.
        api_key: Google API key.

    Returns:
        Dict with:
            - status: "success" or "error"
            - result: Result text for LLM
            - image_path: Full-size image path (on success)
            - thumbnail_path: Thumbnail path (on success)
            - prompt: The generation prompt
    """
    name = tool_call.get("name", "")
    args = tool_call.get("arguments", {})
    prompt = args.get("prompt", "").strip()

    from backend.shared.prompt_i18n import prompt_text

    if name != "generate_image":
        return {
            "status": "error",
            "result": prompt_text("res.image.unknown_tool", language, name=name),
            "prompt": prompt,
        }

    if not prompt:
        return {
            "status": "error",
            "result": prompt_text("res.image.empty_prompt", language),
            "prompt": "",
        }

    # Call Imagen API
    gen_result = generate_image(prompt, model_name, api_key, language)

    if not gen_result["success"]:
        return {
            "status": "error",
            "result": gen_result["error"],
            "prompt": prompt,
        }

    # Save image to storage
    try:
        full_path, thumb_path = save_generated_image(
            character_id,
            gen_result["image_bytes"],
            prompt,
        )

        return {
            "status": "success",
            "result": prompt_text("res.image.generated", language),
            "image_path": full_path,
            "thumbnail_path": thumb_path,
            "prompt": prompt,
        }
    except Exception as e:
        logger.error(f"[ImageGen] Failed to save generated image: {e}")
        return {
            "status": "error",
            "result": prompt_text("res.image.save_failed", language, error=e),
            "prompt": prompt,
        }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_image_generation_prompt(language: str) -> str:
    """Build the image generation section for the system prompt."""
    from backend.shared.prompt_i18n import prompt_section
    return prompt_section("image_generation", language)
