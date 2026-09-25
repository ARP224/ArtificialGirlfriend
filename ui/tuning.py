"""
ui/tuning.py

Handler functions for the Tuning tab in the UI.
These functions handle loading and saving character tuning parameters.
"""

import logging
from typing import Dict, Any, Optional

from backend.shared.i18n import t

logger = logging.getLogger(__name__)


def get_tuning_for_character(character_id: Optional[str]) -> Dict[str, Any]:
    """
    Get tuning parameters for a character.

    Args:
        character_id: Character ID, or None if no character selected

    Returns:
        Dict with tuning parameters or empty dict if no character
    """
    if not character_id:
        return {}

    from backend.backend import get_character_tuning
    return get_character_tuning(character_id)


def handle_tuning_check(character_id: Optional[str]) -> Dict[str, Any]:
    """
    Handle Check button click - load current tuning parameters from config.

    Args:
        character_id: Character ID

    Returns:
        Dict with success status and tuning parameters
    """
    if not character_id:
        return {
            "success": False,
            "error": "No character selected",
            "tuning": {}
        }

    try:
        from backend.backend import get_character_tuning
        tuning = get_character_tuning(character_id)
        return {
            "success": True,
            "tuning": tuning,
            "message": t('hdl.tuning.checked')
        }
    except Exception as e:
        logger.error(f"Error loading tuning parameters: {e}")
        return {
            "success": False,
            "error": str(e),
            "tuning": {}
        }


def handle_tuning_load(character_id: Optional[str], tuning: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Load button click - save tuning parameters to config.

    Args:
        character_id: Character ID
        tuning: Dictionary of tuning parameters

    Returns:
        Dict with success status
    """
    if not character_id:
        return {
            "success": False,
            "error": "No character selected"
        }

    if not tuning:
        return {
            "success": False,
            "error": "No parameters provided"
        }

    try:
        from backend.backend import save_character_tuning
        result = save_character_tuning(character_id, tuning)

        if result.get("success"):
            return {
                "success": True,
                "message": t('hdl.tuning.saved')
            }
        else:
            return {
                "success": False,
                "error": result.get("error", "Failed to save parameters")
            }
    except Exception as e:
        logger.error(f"Error saving tuning parameters: {e}")
        return {
            "success": False,
            "error": str(e)
        }
