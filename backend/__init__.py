"""
Backend module for Artificial Girlfriend

This module provides the core backend functionality including character management,
conversation handling, memory management, and integration with LLMs.
"""

from .backend import (
    init_backend,
    shutdown_backend,
    get_backend_state,
    is_memory_task_running,
    has_pending_memory_tasks,
    reset_short_term_history,
    # Character management
    load_character_list,
    load_character_config,
    create_character,
    edit_character,
    remove_character,
    activate_character,
    is_llm_ready,
    # Conversation
    generate_reply,
    start_conversation,
    stop_conversation,
    get_conversation_history,
    get_last_llm_prompt,
    # Memory data
    get_character_memory_data,
    add_memory,
    edit_memory,
    delete_memory,
    pin_memory,
    # Resource listing
    list_ollama_models,
    list_tts_models,
    list_character_icons,
    list_stt_models,
    # Queue status
    get_queue_status,
    # Talk Theme management
    get_talk_theme,
    update_talk_theme,
    clear_talk_theme,
    # PC Status & Screen Capture & Talk Theme
    set_pc_status_enabled,
    set_screen_capture_enabled,
    set_talk_theme_enabled,
    set_speechless_enabled,
    get_feature_status
)

__all__ = [
    'init_backend',
    'shutdown_backend',
    'get_backend_state',
    'load_character_list',
    'load_character_config',
    'create_character',
    'edit_character',
    'remove_character',
    'activate_character',
    'is_llm_ready',
    'generate_reply',
    'start_conversation',
    'stop_conversation',
    'get_conversation_history',
    'get_last_llm_prompt',
    'get_character_memory_data',
    'add_memory',
    'edit_memory',
    'delete_memory',
    'pin_memory',
    'list_ollama_models',
    'list_tts_models',
    'list_character_icons',
    'list_stt_models',
    'get_queue_status',
    'is_memory_task_running',
    'has_pending_memory_tasks',
    'reset_short_term_history',
    'get_talk_theme',
    'update_talk_theme',
    'clear_talk_theme',
    'set_pc_status_enabled',
    'set_screen_capture_enabled',
    'set_talk_theme_enabled',
    'set_speechless_enabled',
    'get_feature_status'
]