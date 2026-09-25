"""
ui/auto_prompt_state.py

Auto-prompt state for the Artificial Girlfriend UI (UI/state layer).

This is the home for the "auto prompt" feature knobs the UI keeps around: the
enabled flag, the silence-timer duration, the live timer-running state, and the
two prompt body texts (Japanese / English) that get injected after the user has
been silent for a while. It came out of the AppState god-object split.

These fields used to live directly on ``ui/state.py``'s ``AppState`` and are
read/written by ``ui/conversation.py`` (timer arm/cancel, send-on-silence,
toggle / duration / text update helpers), ``ui/settings_manager.py``
(load/save persistence), and ``ui/pages.py`` / ``ui/mobile_app.py`` (control
rendering and value seeding).

Placement: mirrors the sibling carve-outs (DataState / AudioState; backend
CommandState / TokenState / MemoryCaches) but lives under ``ui/`` because this
is UI-layer state. It is a pure stdlib state container.

Layering: this module owns no behavior — only the state container. The
auto-prompt *behavior* (timer loop, dispatch, persistence) stays in
``ui/conversation.py`` / ``ui/settings_manager.py`` and reaches these fields
through AppState's backward-compat properties, so the model-facing contract is
unaffected (auto-prompt is a UI/real-machine path, outside the harness — so the
smoke import + a real-AppState round-trip are the gate).
"""


class AutoPromptState:
    """Owns the auto-prompt knobs carved out of AppState.

    AppState holds a single instance as ``app_state.auto_prompt`` and keeps
    backward-compat properties for the legacy ``app_state.<field>`` access paths,
    so existing call sites are unchanged while ownership now lives here. Defaults
    mirror the values that used to be declared directly on ``AppState``.
    """

    def __init__(self) -> None:
        self.enabled: bool = False
        self.timer_duration: int = 60  # seconds
        self.timer_active: bool = False
        # Auto prompt texts (Japanese and English)
        self.ja: str = "これは自動送信です。これはユーザーが入力したプロンプトではありません。ユーザーは無言状態が続いています。話の続きをしたり、何か話しかけてあげてください。"
        self.en: str = "This is an automatic message. This is not a prompt entered by the user. The user has been silent for a while. Please continue the conversation or say something to them."
