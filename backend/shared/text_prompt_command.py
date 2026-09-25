"""
backend/shared/text_prompt_command.py

Shared text-prompt command interface for Artificial Girlfriend.

Decouples the command *producer* (the WebSocket transport, which receives
``text_prompt`` messages from the MotionPNGPlayer input box) from the *domain*
that runs the conversation turn (the ui-layer conversation flow). The producer
calls :func:`dispatch_text_prompt`; the conversation layer registers its
handler via :func:`register_text_prompt_handler` at composition-root wiring
(ui/app.py). Same inversion pattern as :mod:`backend.shared.feature_commands`.

It has no backend or ui dependencies — only the standard library — so any
layer may depend on it downward.
"""

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# A handler receives the prompt text and returns an accept/reject dict:
# {"success": bool, "message": str}. On success the turn runs asynchronously
# (the handler returns as soon as generation has started).
TextPromptHandler = Callable[[str], Dict[str, Any]]

_handler: Optional[TextPromptHandler] = None


def register_text_prompt_handler(handler: TextPromptHandler) -> None:
    """Register the conversation-layer handler for text-prompt commands.

    Last registration wins, so repeated wiring stays safe.
    """
    global _handler
    _handler = handler


def dispatch_text_prompt(text: str) -> Dict[str, Any]:
    """Dispatch a text-prompt command to the registered handler.

    If no handler is registered (a wiring error), returns an error-shaped dict
    so the caller can frame it as a response rather than crashing.
    """
    if _handler is None:
        logger.error("No text-prompt handler registered")
        return {"success": False, "message": "No text-prompt handler registered"}
    return _handler(text)
