"""
ui/handlers/navigation.py

Gradio handlers for top-level page navigation that were wired inline in app.py's
``create_gradio_interface``:

* ``switch_page`` -- switch the visible page and update the nav-button active
  state (returns the page-id State plus the per-page / per-nav-button
  ``gr.update`` list).
* ``load_character_page_data`` -- thin wrapper that returns
  ``open_character_management()`` when the character-settings page is opened.
* ``refresh_both_char_dropdowns`` -- refresh the conversation + edit character
  dropdowns after create/edit/delete, preserving the active selection.
* ``refresh_char_dropdowns_with_youtube`` -- the same plus the YouTube reply
  tab's character dropdown (wired only when that tab exists).

B11 (ST4) extracts the handler bodies here. These are self-contained: each body
uses only its own argument(s), ``gr.update``, and module-level names
(``app_state``, ``Pages``, ``open_character_management``, ``refresh_char_list``)
-- none closes over a local Gradio component variable, so the bodies move
verbatim (a pure 8-space dedent) and are re-imported into app.py while the
``.click(fn=...)`` / ``.then(fn=...)`` wiring (which references the local
component objects) stays in app.py.
"""

import gradio as gr

from ..state import app_state
from ..pages import Pages
from ..character_ui import open_character_management, refresh_char_list


# Load TTS/Ollama models when navigating to character settings page
def load_character_page_data(create_lang="ja", create_tts=None, create_model=None,
                             edit_lang="en", edit_tts=None, edit_model=None,
                             current_edit_char=None):
    """Load data when character settings page is opened.

    引数はフォームの現在値パススルー(選択肢の撒き直しで保持中の値が
    無効化される検証エラー/編集キャラ選択の消失を防ぐ。詳細は
    open_character_management の docstring)。
    """
    # open_character_management() already returns gr.update objects
    # No need to wrap them again
    return open_character_management(create_lang, create_tts, create_model,
                                     edit_lang, edit_tts, edit_model,
                                     current_edit_char)

# Helper function to refresh both character dropdowns
def refresh_both_char_dropdowns(current_edit_char=None):
    """Refresh character dropdowns after create/edit/delete operations.

    編集DDは「選択中キャラがまだ存在すれば保持」(稜裁定 2026-08-15:
    旧実装の常時クリアは、保存後にフォーム値だけ残って選択が消える
    不整合を生んでいた)。削除後は選択キャラがリストから消えるので
    自然に未選択へ落ちる。
    """
    char_list = refresh_char_list()
    # Get current selected values to preserve them if possible
    current_conv_char = getattr(app_state, 'active_character_id', None)

    # Check if current character still exists in the list
    char_ids = [char_id for _, char_id in char_list]
    conv_value = current_conv_char if current_conv_char in char_ids else None
    edit_value = current_edit_char if current_edit_char in char_ids else None

    # Return gr.update objects for both dropdowns
    return (
        gr.update(choices=char_list, value=conv_value),  # conversation dropdown - preserve selection
        gr.update(choices=char_list, value=edit_value)   # edit dropdown - preserve if still exists
    )


def refresh_char_dropdowns_with_youtube(current_edit_char=None):
    """``refresh_both_char_dropdowns`` + the YouTube reply tab's character DD.

    The YouTube tab freezes its character choices at build time, so a newly
    created (or renamed/deleted) character was invisible there until a page
    reload. app.py wires this variant instead when the tab exists; server
    mode does not build the tab, so the two-dropdown version stays in use.
    """
    # 呼出時 import: ui.handlers.youtube はタブ本体を持つ重い葉なので
    # ナビゲーションのロード時に巻き込まない
    from .youtube import youtube_character_choices_update

    conv_update, edit_update = refresh_both_char_dropdowns(current_edit_char)
    return conv_update, edit_update, youtube_character_choices_update()

# Navigation event handlers
def switch_page(page_id, nav_btn_id):
    """Switch to a different page and update navigation button states."""
    # Note: page navigation is driven by the Gradio ``current_page`` State component
    # in ui/app.py; the old ``app_state.current_page`` tracking field was dead
    # (write-only, zero readers) and was removed in ST5-S12.

    # Create button updates - all buttons become inactive except the clicked one
    nav_updates = []
    for nav_id in ["nav-conversation", "nav-characters", "nav-history", "nav-logs", "nav-system"]:
        if nav_id == nav_btn_id:
            nav_updates.append(gr.update(elem_classes="nav-btn active"))
        else:
            nav_updates.append(gr.update(elem_classes="nav-btn"))

    # Create page visibility updates
    page_updates = []
    pages = [Pages.CONVERSATION, Pages.CHARACTER_SETTINGS, Pages.CONVERSATION_HISTORY, 
             Pages.SYSTEM_LOGS, Pages.SYSTEM_CONTROLS]

    for page in pages:
        page_updates.append(gr.update(visible=(page == page_id)))

    # Update the current page state
    return [page_id] + page_updates + nav_updates
