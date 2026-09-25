"""
ui/handlers/feature_toggles.py

Gradio fallback handlers for the three feature toggles whose UI fallback path
(speechless / command_execution / notes) was still wired inline in app.py and
reached the spine setters directly.

B11 (ST4) extracts the handler bodies here. At the same time it resolves the
B3-deferred site (recorded in backend/feature_commands.py's header): instead of
importing ``backend.backend.set_<feature>_enabled`` and persisting the choice
itself, each handler now publishes a toggle command through the shared producer
interface ``backend.shared.feature_commands.dispatch_feature_toggle``. The domain
adapter ``backend.shared.feature_toggle_service`` performs the identical effect -- flip
the runtime flag via ``set_<feature>_enabled`` and persist
``features.<feature>_enabled`` -- so behaviour is unchanged; only the dependency
direction is inverted (V3): the UI producer no longer reaches into the spine
setters.

Importing this module self-registers the domain handlers (side-effect import of
``backend.shared.feature_toggle_service``, mirroring backend.server.websocket_server), so the
dispatch path is wired regardless of WebSocket start-up order. No import cycle:
the service depends only on feature_commands + settings_store, neither of which
imports ui.
"""

import backend.shared.feature_toggle_service  # noqa: F401  (side-effect: self-registers handlers)
from backend.shared.feature_commands import dispatch_feature_toggle


def handle_speechless_toggle(value: str) -> str:
    """Handle the Speechless toggle from the Gradio fallback path."""
    enabled = value.strip().lower() == 'true'
    dispatch_feature_toggle('speechless', enabled)
    return ""


def handle_command_execution_toggle(value: str) -> str:
    """Handle the Command Execution toggle from the Gradio fallback path."""
    enabled = value.strip().lower() == 'true'
    dispatch_feature_toggle('command_execution', enabled)
    return ""


def handle_notes_toggle(value: str) -> str:
    """Handle the Notes toggle from the Gradio fallback path."""
    enabled = value.strip().lower() == 'true'
    dispatch_feature_toggle('notes', enabled)
    return ""
