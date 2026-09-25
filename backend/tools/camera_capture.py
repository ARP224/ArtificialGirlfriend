"""
backend/camera_capture.py

Camera capture via Function Calling for API providers.
Phase 3 (2026-07-16): frames come from the connected client browser camera
(the Live Camera provider page) via the WS request/deliver path — the
former server-side OpenCV capture is gone, so this works in both local and
server mode. Ollama: available on tools+vision capable models (2026-08-11).
"""

import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional

from backend.tools.tool_schemas import format_tools_for_provider, localize_tools
from backend.shared.constants import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMERA_CAPTURE_TOOL_NAMES = frozenset({"capture_camera"})

# CWD相対だと起動元ディレクトリ次第で temp_captures が散らばる。リポジトリ
# 固定の DATA_DIR 配下に置いて安定させる。
TEMP_CAPTURES_DIR = DATA_DIR / "temp_captures"

# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic)
# ---------------------------------------------------------------------------

CAPTURE_CAMERA_TOOL = {
    "name": "capture_camera",
    "description": "PCのWebカメラで写真を撮影する。ユーザーの様子が見たいときや気になるときに使う。",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "なぜ撮影したいか（動機や気持ち）"
            }
        },
        "required": ["reason"]
    }
}

_CAMERA_TOOLS = [CAPTURE_CAMERA_TOOL]


# ---------------------------------------------------------------------------
# Provider formatting
# ---------------------------------------------------------------------------

def get_camera_tool_definitions_for_provider(provider: str, language: str) -> List[Dict]:
    """Return camera capture tool definitions formatted for the given provider."""
    return format_tools_for_provider(provider, localize_tools(_CAMERA_TOOLS, language))


# ---------------------------------------------------------------------------
# Camera capture
# ---------------------------------------------------------------------------

def capture_image(character_id: str, language: str) -> Dict[str, Any]:
    """Capture one frame from the connected client camera.

    Phase 3 (2026-07-16): the capture backend is the Live Camera provider
    page (browser getUserMedia via the WS request/deliver path) — the former
    server-side OpenCV capture is gone, which also makes this tool work in
    server mode. Device selection lives with the provider (the camera row
    under Function Calling), so there is no device_index here anymore.

    Args:
        character_id: Character UUID (used for storage directory).
        language: Prompt language for LLM-facing error strings.

    Returns:
        Dict with:
            - success (bool)
            - image_path (str): Path to captured image (on success)
            - error (str): Error message (on failure)
    """
    from backend.shared.prompt_i18n import prompt_text
    from backend.shared.ambient_camera_state import (
        is_provider_available, request_tool_frame,
    )
    from backend.shared.constants import AMBIENT_TOOL_CAPTURE_TIMEOUT

    try:
        if not is_provider_available():
            logger.info("[CameraCapture] No camera provider connected")
            return {
                "success": False,
                "error": prompt_text("res.camera.not_connected", language)
            }

        frame_path = request_tool_frame(AMBIENT_TOOL_CAPTURE_TIMEOUT)
        if not frame_path:
            logger.error("[CameraCapture] Frame request failed or timed out")
            return {
                "success": False,
                "error": prompt_text("res.camera.capture_failed", language)
            }

        # Move into temp_captures/{character_id}/ — same storage layout and
        # cleanup lifecycle as before (cleanup_captured_images).
        save_dir = TEMP_CAPTURES_DIR / character_id
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
        image_path = save_dir / filename
        shutil.move(frame_path, image_path)

        if not image_path.exists():
            return {
                "success": False,
                "error": prompt_text("res.camera.save_failed", language)
            }

        logger.info(f"[CameraCapture] Image saved to {image_path}")
        return {
            "success": True,
            "image_path": str(image_path),
        }

    except Exception as e:
        logger.error(f"[CameraCapture] Unexpected error: {e}")
        return {
            "success": False,
            "error": prompt_text("res.camera.unexpected", language, error=e)
        }


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def dispatch_camera_tool(character_id: str, tool_call: dict,
                         language: str) -> Dict[str, Any]:
    """Dispatch a camera capture tool call.

    Args:
        character_id: Character UUID.
        tool_call: Tool call dict with name and arguments.

    Returns:
        Dict with:
            - status: "success" or "error"
            - result: Result text for LLM
            - image_path: Captured image path (on success)
            - reason: The capture reason
    """
    name = tool_call.get("name", "")
    args = tool_call.get("arguments", {})
    reason = args.get("reason", "").strip()

    from backend.shared.prompt_i18n import prompt_text

    if name != "capture_camera":
        return {
            "status": "error",
            "result": prompt_text("res.camera.unknown_tool", language, name=name),
            "reason": reason,
        }

    # Capture image
    cap_result = capture_image(character_id, language=language)

    if not cap_result["success"]:
        return {
            "status": "error",
            "result": cap_result["error"],
            "reason": reason,
        }

    return {
        "status": "success",
        "result": prompt_text("res.camera.captured", language),
        "image_path": cap_result["image_path"],
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_captured_images(character_id: Optional[str] = None) -> None:
    """Delete captured images.

    Args:
        character_id: If specified, delete only that character's captures.
                      If None, delete the entire temp_captures directory.
    """
    try:
        if character_id:
            target = TEMP_CAPTURES_DIR / character_id
            if target.exists():
                shutil.rmtree(target)
                logger.info(f"[CameraCapture] Cleaned up captures for {character_id}")
        else:
            if TEMP_CAPTURES_DIR.exists():
                shutil.rmtree(TEMP_CAPTURES_DIR)
                logger.info("[CameraCapture] Cleaned up all captured images")
    except Exception as e:
        logger.warning(f"[CameraCapture] Cleanup error: {e}")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_camera_capture_prompt(language: str) -> str:
    """Build the camera capture section for the system prompt."""
    from backend.shared.prompt_i18n import prompt_section
    return prompt_section("camera_capture", language)
