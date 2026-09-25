"""
ui/handlers/audio_controls.py

Gradio handlers for the audio control widgets (beep volume / beep test /
TTS volume / TTS voice test) that were wired inline in app.py's
``create_gradio_interface``.

B11 (ST4) extracts the handler bodies here. These are true leaves: each body
uses only its own argument(s), ``gr.update`` and a call-time
``from ..conversation import ...`` -- it does not close over any local Gradio
component variable, so it can be moved verbatim and re-imported into app.py
while the ``.change(fn=...)`` / ``.click(fn=...)`` wiring (which references the
local component dict) stays in app.py. The only mechanical change versus the
inline definitions is the relative-import depth (``.conversation`` ->
``..conversation``) now that the handlers live one package level deeper.
"""

import gradio as gr


def handle_volume_change(volume):
    from ..conversation import update_beep_volume
    status = update_beep_volume(volume)
    return gr.update(value=f"**{volume:.0f}%**"), gr.update(value=status, visible=True)


def test_both_beeps():
    from ..conversation import test_beep_sound
    return gr.update(value=test_beep_sound("both"), visible=True)


def handle_tts_volume_change(volume):
    from ..conversation import update_tts_volume
    status = update_tts_volume(volume)
    return gr.update(value=f"**{volume:.0f}%**"), gr.update(value=status, visible=True)


def test_voice():
    from ..conversation import test_tts_voice
    return gr.update(value=test_tts_voice(), visible=True)
