"""
ui/handlers/tuning.py

Gradio handlers for the per-character Tuning tab (temperature / top_k / ... /
num_predict sliders, Check / Load buttons, and the per-input change-color
validation) that were wired inline in app.py's ``create_gradio_interface``.

B11 (ST4) extracts the handler bodies here. These are self-contained: each body
uses only its own argument(s), ``gr.update``, the module-level tuning helpers
(``get_tuning_for_character`` / ``handle_tuning_check`` / ``handle_tuning_load``
from ``..tuning``), ``app_state`` and call-time ``from backend.shared.constants
import ...`` -- none of them closes over a local Gradio component variable, so
the bodies move verbatim (pure dedent) and are re-imported into app.py while the
``.change(fn=...)`` / ``.click(fn=...)`` wiring (which references the local
component dict) stays in app.py. ``get_tuning_input_class`` is co-located here
because ``create_tuning_change_handler`` closes over it; it is not re-imported
into app.py (internal to this module).
"""

import gradio as gr

from backend.shared.i18n import t
from ..state import app_state
from ..tuning import get_tuning_for_character, handle_tuning_check, handle_tuning_load


def update_tuning_on_character_change(character_id):
    """Update tuning inputs when character changes."""
    if not character_id:
        # No character selected - disable inputs and return defaults
        from backend.shared.constants import get_tuning_defaults
        defaults = get_tuning_defaults()
        return [
            gr.update(value=defaults['temperature'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['top_k'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['top_p'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['min_p'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['repeat_last_n'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['repeat_penalty'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['presence_penalty'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['frequency_penalty'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(value=defaults['num_predict'], interactive=False, elem_classes=["tuning-input"]),
            gr.update(interactive=False),  # check button
            gr.update(interactive=False),  # load button
            "",  # status
            {}   # original values
        ]

    tuning = get_tuning_for_character(character_id)
    return [
        gr.update(value=tuning.get('temperature', 0.7), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('top_k', 40), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('top_p', 0.9), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('min_p', 0.05), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('repeat_last_n', 500), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('repeat_penalty', 1.1), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('presence_penalty', 0.4), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('frequency_penalty', 0.5), interactive=True, elem_classes=["tuning-input"]),
        gr.update(value=tuning.get('num_predict', 800), interactive=True, elem_classes=["tuning-input"]),
        gr.update(interactive=True),   # check button
        gr.update(interactive=True),   # load button
        t('hdl.tuning.params_loaded'),  # status
        tuning  # store original values
    ]


def get_tuning_input_class(param_name: str, value, original_values: dict) -> list:
    """
    Determine elem_classes based on input value validation.

    Returns:
        - ["tuning-input", "tuning-error"] if value is out of range (red)
        - ["tuning-input", "tuning-changed"] if value differs from original (green)
        - ["tuning-input"] if value is valid and unchanged (default)
    """
    from backend.shared.constants import TUNING_RANGES

    base_class = "tuning-input"

    # Empty or None value → error (red)
    if value is None or value == "":
        return [base_class, "tuning-error"]

    # Range validation (skip if range is None; currently none — defensive)
    range_def = TUNING_RANGES.get(param_name)
    if range_def is not None:
        min_val, max_val = range_def
        try:
            if not (min_val <= float(value) <= max_val):
                return [base_class, "tuning-error"]  # Out of range → red
        except (TypeError, ValueError):
            return [base_class, "tuning-error"]

    # Compare with original value
    original_value = original_values.get(param_name)
    if original_value is not None:
        try:
            if float(value) != float(original_value):
                return [base_class, "tuning-changed"]  # Changed → green
        except (TypeError, ValueError):
            pass

    return [base_class]  # Default


def handle_check_click():
    """Handle Check button click."""
    character_id = app_state.active_character_id
    result = handle_tuning_check(character_id)

    if result.get('success'):
        tuning = result.get('tuning', {})
        # Reset all inputs to default color (values now match original)
        return [
            gr.update(value=tuning.get('temperature', 0.7), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('top_k', 40), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('top_p', 0.9), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('min_p', 0.05), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('repeat_last_n', 500), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('repeat_penalty', 1.1), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('presence_penalty', 0.4), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('frequency_penalty', 0.5), elem_classes=["tuning-input"]),
            gr.update(value=tuning.get('num_predict', 800), elem_classes=["tuning-input"]),
            f"<span class='tuning-status-success'>{result.get('message', 'Loaded')}</span>",
            tuning
        ]
    else:
        error = result.get('error', 'Failed to load')
        return [
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(),
            f"<span class='tuning-status-error'>{t('common.error_with', error=error)}</span>",
            gr.update()
        ]


def handle_load_click(temp, top_k, top_p, min_p, rep_last_n, rep_pen, pres_pen, freq_pen, num_pred):
    """Handle Load button click."""
    character_id = app_state.active_character_id

    tuning = {
        'temperature': temp,
        'top_k': int(top_k),
        'top_p': top_p,
        'min_p': min_p,
        'repeat_last_n': int(rep_last_n),
        'repeat_penalty': rep_pen,
        'presence_penalty': pres_pen,
        'frequency_penalty': freq_pen,
        'num_predict': int(num_pred)
    }

    result = handle_tuning_load(character_id, tuning)

    if result.get('success'):
        # Reset all inputs to default color (values now match new original)
        return [
            f"<span class='tuning-status-success'>{result.get('message', 'Saved')}</span>",
            tuning,  # Update original values to new saved values
            gr.update(elem_classes=["tuning-input"]),  # temperature
            gr.update(elem_classes=["tuning-input"]),  # top_k
            gr.update(elem_classes=["tuning-input"]),  # top_p
            gr.update(elem_classes=["tuning-input"]),  # min_p
            gr.update(elem_classes=["tuning-input"]),  # repeat_last_n
            gr.update(elem_classes=["tuning-input"]),  # repeat_penalty
            gr.update(elem_classes=["tuning-input"]),  # presence_penalty
            gr.update(elem_classes=["tuning-input"]),  # frequency_penalty
            gr.update(elem_classes=["tuning-input"]),  # num_predict
        ]
    else:
        error = result.get('error', 'Failed to save')
        return [
            f"<span class='tuning-status-error'>{t('common.error_with', error=error)}</span>",
            gr.update(),  # Don't update original values on error
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update()
        ]


def create_tuning_change_handler(param_name):
    """Factory function to create change handler for a specific parameter."""
    def handler(value, original):
        classes = get_tuning_input_class(param_name, value, original)
        return gr.update(elem_classes=classes)
    return handler
