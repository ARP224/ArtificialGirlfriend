# Talk Theme Management Functions for UI

from typing import Tuple
import gradio as gr
import logging

from backend.shared.i18n import t

logger = logging.getLogger(__name__)

def poll_talk_theme(app_state, backend) -> Tuple[str, gr.update]:
    """
    Refresh the talk theme display from the backend's persisted value.
    Called from the character-select chain (live updates arrive via WS push).

    Always shows the persisted theme regardless of conversation state:
    the theme lives in the character config and is injected into prompts
    on the next conversation, so hiding it while idle made the UI lie
    (theme looked lost after a restart while the AI still followed it).
    Editing/clearing stays conversation-only via the panel enable state.

    Returns:
        Tuple of (display text, error HTML update)
    """
    try:
        # Get current theme from backend
        result = backend.get_talk_theme()

        if result.get("success"):
            theme = result.get("theme", "")

            # Check if theme changed (version tracking for efficiency)
            if theme != app_state._cached_talk_theme:
                app_state._cached_talk_theme = theme
                app_state._talk_theme_version += 1

                # Format display text
                if theme:
                    display_text = theme
                else:
                    display_text = t('gen.no_talk_theme')

                return display_text, gr.update(value="", visible=False)
            else:
                # No change, return cached value
                if theme:
                    return theme, gr.update(value="", visible=False)
                else:
                    return t('gen.no_talk_theme'), gr.update(value="", visible=False)
        else:
            # Error case - show the no-theme placeholder but don't show error to user
            logger.debug(f"Failed to get talk theme: {result.get('error')}")
            return t('gen.no_talk_theme'), gr.update(value="", visible=False)

    except Exception as e:
        logger.error(f"Error polling talk theme: {e}")
        return t('gen.no_talk_theme'), gr.update(value="", visible=False)


def update_talk_theme_click(new_theme: str, app_state, backend) -> Tuple[str, str, gr.update, gr.update]:
    """
    Handle update theme button click.

    Args:
        new_theme: The new theme text from input

    Returns:
        Tuple of (current theme display, input clear, error update, input update)
    """
    # Check if conversation is active
    if not app_state.conversation_started:
        return (t('gen.no_talk_theme'), new_theme,
                gr.update(value=f"<span style='color: red;'>{t('gen.start_conv_first')}</span>", visible=True),
                gr.update())

    # Trim whitespace
    new_theme = new_theme.strip() if new_theme else ""

    try:
        # Update theme via backend
        result = backend.update_talk_theme(new_theme)

        if result.get("success"):
            # Update was successful
            updated_theme = result.get("theme", "")

            # Update cache
            app_state._cached_talk_theme = updated_theme
            app_state._talk_theme_version += 1

            # Add feedback message to chat
            if result.get("old_theme", "") != updated_theme:
                # 整形は theme_format に一本化(リロード経路 history.py と共有)
                from ui.conversation.theme_format import render_talk_theme_html
                # Theme actually changed
                theme_html = render_talk_theme_html(
                    "set" if updated_theme else "clear", updated_theme, by_user=True)

                app_state.append_chat_message("TALK_THEME_USER", theme_html, is_ai=False)
                app_state._chat_history_version += 1

            # Clear input and hide error
            display_text = updated_theme if updated_theme else t('gen.no_talk_theme')
            return (display_text, "",
                    gr.update(value="", visible=False),
                    gr.update(value=""))  # Clear input
        else:
            # Error from backend
            error_msg = result.get("error", t('gen.theme_update_failed'))
            return (app_state._cached_talk_theme if app_state._cached_talk_theme else t('gen.no_talk_theme'),
                    new_theme,
                    gr.update(value=f"<span style='color: red;'>{error_msg}</span>", visible=True),
                    gr.update())

    except Exception as e:
        logger.error(f"Error updating talk theme: {e}")
        return (app_state._cached_talk_theme if app_state._cached_talk_theme else t('gen.no_talk_theme'),
                new_theme,
                gr.update(value=f"<span style='color: red;'>{t('common.error_with', error=e)}</span>", visible=True),
                gr.update())


def clear_talk_theme_click(app_state, backend) -> Tuple[str, gr.update, gr.update]:
    """
    Handle clear theme button click.

    Returns:
        Tuple of (current theme display, input update, error update)
    """
    # Check if conversation is active
    if not app_state.conversation_started:
        return (t('gen.no_talk_theme'), gr.update(),  # Don't change input field
                gr.update(value=f"<span style='color: red;'>{t('gen.start_conv_first')}</span>", visible=True))

    try:
        # Clear theme via backend
        result = backend.clear_talk_theme()

        if result.get("success"):
            # Clear was successful
            app_state._cached_talk_theme = ""
            app_state._talk_theme_version += 1

            # Add feedback message to chat
            from ui.conversation.theme_format import render_talk_theme_html
            theme_html = render_talk_theme_html("clear", by_user=True)
            app_state.append_chat_message("TALK_THEME_USER", theme_html, is_ai=False)
            app_state._chat_history_version += 1

            return (t('gen.no_talk_theme'), gr.update(), gr.update(value="", visible=False))  # Don't change input field
        else:
            # Error from backend
            error_msg = result.get("error", t('gen.theme_clear_failed'))
            return (app_state._cached_talk_theme if app_state._cached_talk_theme else t('gen.no_talk_theme'),
                    gr.update(),  # Don't change input field
                    gr.update(value=f"<span style='color: red;'>{error_msg}</span>", visible=True))

    except Exception as e:
        logger.error(f"Error clearing talk theme: {e}")
        return (app_state._cached_talk_theme if app_state._cached_talk_theme else t('gen.no_talk_theme'),
                gr.update(),  # Don't change input field
                gr.update(value=f"<span style='color: red;'>{t('common.error_with', error=e)}</span>", visible=True))


def enable_theme_panel(app_state) -> Tuple[gr.update, gr.update, gr.update]:
    """
    Enable the theme panel when conversation starts.

    Returns:
        Tuple of (input update, update button update, clear button update)
    """
    app_state._theme_panel_enabled = True
    return (gr.update(interactive=True),  # Enable input
            gr.update(interactive=True),  # Enable update button
            gr.update(interactive=True))  # Enable clear button


def disable_theme_panel(app_state) -> Tuple[gr.update, gr.update, gr.update]:
    """
    Disable the theme panel when conversation stops.

    Returns:
        Tuple of (input update, update button update, clear button update)
    """
    app_state._theme_panel_enabled = False
    return (gr.update(interactive=False),  # Disable input
            gr.update(interactive=False),  # Disable update button
            gr.update(interactive=False))  # Disable clear button


