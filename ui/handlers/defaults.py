"""
ui/handlers/defaults.py

Gradio handlers for the *global* Tuning Defaults tab (the default temperature /
top_k / ... / num_predict sliders, the Check / Load / Apply-to-all buttons, and
the per-input change-color validation) that were wired inline in app.py's
``create_gradio_interface``.

B11 (ST4) extracts the handler bodies here -- the twin of ``ui/handlers/tuning.py``
(B11i), which handles the per-character Tuning tab. These are self-contained:
each body uses only its own argument(s), ``gr.update`` and call-time
``from backend.shared.constants import ...`` / ``from backend.backend import ...``
(all absolute imports, so the move is a pure dedent = byte-identical) -- none of
them closes over a local Gradio component variable, so the bodies move verbatim
and are re-imported into app.py while the ``.change(fn=...)`` / ``.click(fn=...)``
wiring (which references the local component dict) stays in app.py.
``get_defaults_input_class`` is co-located here because
``create_defaults_change_handler`` closes over it; it is not re-imported into
app.py (internal to this module).
"""

import gradio as gr

from backend.shared.i18n import t


def get_defaults_input_class(param_name: str, value, original_values: dict) -> list:
    """Determine elem_classes based on input value validation for defaults."""
    from backend.shared.constants import TUNING_RANGES

    base_class = "tuning-input"

    if value is None or value == "":
        return [base_class, "tuning-error"]

    range_def = TUNING_RANGES.get(param_name)
    if range_def is not None:
        min_val, max_val = range_def
        try:
            if not (min_val <= float(value) <= max_val):
                return [base_class, "tuning-error"]
        except (TypeError, ValueError):
            return [base_class, "tuning-error"]

    original_value = original_values.get(param_name)
    if original_value is not None:
        try:
            if float(value) != float(original_value):
                return [base_class, "tuning-changed"]
        except (TypeError, ValueError):
            pass

    return [base_class]


def handle_defaults_check_click():
    """Handle Check button click for defaults."""
    from backend.shared.constants import get_tuning_defaults
    tuning = get_tuning_defaults()
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
        f"<span class='tuning-status-success'>{t('hdl.defaults.loaded')}</span>",
        tuning
    ]


def handle_defaults_load_click(temp, top_k, top_p, min_p, rep_last_n, rep_pen, pres_pen, freq_pen, num_pred):
    """Handle Load button click for defaults."""
    from backend.shared.constants import save_tuning_defaults

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

    result = save_tuning_defaults(tuning)

    if result.get('success'):
        return [
            f"<span class='tuning-status-success'>{t('hdl.defaults.saved')}</span>",
            tuning,
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
            gr.update(elem_classes=["tuning-input"]),
        ]
    else:
        error = result.get('error', 'Failed to save')
        return [
            f"<span class='tuning-status-error'>{t('common.error_with', error=error)}</span>",
            gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update()
        ]


def create_defaults_change_handler(param_name):
    """Factory function to create change handler for defaults."""
    def handler(value, original):
        classes = get_defaults_input_class(param_name, value, original)
        return gr.update(elem_classes=classes)
    return handler


def show_apply_all_confirm(temp, top_k, top_p, min_p, rep_last_n, rep_pen, pres_pen, freq_pen, num_pred):
    """Show confirmation dialog with character count."""
    from backend.backend import load_character_list
    char_count = len(load_character_list())

    confirm_msg = t(
        'hdl.defaults.apply_all_confirm',
        count=char_count,
        temp=temp, top_k=int(top_k), top_p=top_p, min_p=min_p,
        repeat_last_n=int(rep_last_n), repeat_penalty=rep_pen,
        presence_penalty=pres_pen, frequency_penalty=freq_pen,
        num_predict=int(num_pred),
    )

    return [
        gr.update(visible=True),  # show confirm box
        gr.update(value=confirm_msg),  # update confirm text
        ""  # clear status
    ]


def execute_apply_all(temp, top_k, top_p, min_p, rep_last_n, rep_pen, pres_pen, freq_pen, num_pred):
    """Execute apply to all characters."""
    from backend.backend import apply_tuning_to_all_characters

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

    result = apply_tuning_to_all_characters(tuning)

    if result.get('success'):
        applied_msg = t('hdl.defaults.applied',
                        updated=result.get('updated_count', 0),
                        total=result.get('total_count', 0))
        failed = result.get('failed') or []
        if failed:
            applied_msg += " " + t('hdl.defaults.apply_failed_names', names=", ".join(failed))
        status_msg = f"<span class='tuning-status-success'>✓ {applied_msg}</span>"
        # Reset input colors and update original values
        return [
            gr.update(visible=False),  # hide confirm box
            status_msg,  # show status
            tuning,  # update original values
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
        error = result.get('error', 'Failed to apply')
        status_msg = f"<span class='tuning-status-error'>✗ {t('common.error_with', error=error)}</span>"
        return [
            gr.update(visible=False),  # hide confirm box
            status_msg,  # show status
            gr.update(),  # don't update original values
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(), gr.update()
        ]


def cancel_apply_all():
    """Cancel apply to all."""
    return [
        gr.update(visible=False),  # hide confirm box
        ""  # clear status
    ]
