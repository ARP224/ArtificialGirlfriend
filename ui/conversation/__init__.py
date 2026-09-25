"""
ui/conversation package

会話UIの玄関(公開名の re-export のみ)。実装は責務別サブモジュールに住む:
lifecycle(会話開始/終了) / recording(音声入力) / beep(ビープ音) /
tts_playback(TTS再生) / auto_prompt(自動プロンプト) / generation(AI応答生成) /
text_input(テキスト入力) / electron_prompt(MotionPNGPlayer入力) /
history(履歴ロード) / commands(コマンド許可/拒否) /
display(表示設定) / notify(UI更新通知)
"""

from .lifecycle import (
    toggle_start_end,
    refresh_character_info_on_start,
    restore_character_info_on_load,
    sync_char_dropdown_lock,
    sync_text_controls_lock,
    sync_conversation_controls_on_load,
)
from .recording import (
    toggle_voice_recording,
    update_recording_time,
    reset_recording_state,
    record_speech,
    stop_and_transcribe_phase1,
    start_voice_generation,
    process_voice_ai_generation,
    voice_btn_chain,
    external_start_recording,
    external_stop_recording,
)
from .beep import (
    toggle_beep,
    create_beep_sounds,
    play_beep_sound,
    test_beep_sound,
    update_beep_volume,
)
from .tts_playback import (
    update_tts_volume,
    test_tts_voice,
    validate_tts_data,
    handle_tts_error,
    play_tts_response,
)
from .auto_prompt import (
    process_auto_prompt,
    execute_auto_prompt_generation,
    start_auto_prompt_timer,
    reset_auto_prompt_timer,
    toggle_auto_prompt,
    update_auto_prompt_timer_duration,
)
from .generation import (
    validate_active_character,
    generate_ai_response,
    is_error_response,
    handle_generate_reply,
)
from .text_input import (
    handle_text_input,
    start_text_generation,
    process_text_generation,
    clear_text_input,
)
from .electron_prompt import process_electron_text_prompt
from .history import load_conversation_history, wait_for_history_load
from .commands import handle_command_deny, handle_command_accept
from .display import update_chat_font_size
from .notify import notify_ui_update


# Export public API
__all__ = [
    'toggle_start_end',
    'sync_char_dropdown_lock',
    'sync_text_controls_lock',
    'sync_conversation_controls_on_load',
    'toggle_beep',
    'create_beep_sounds',
    'play_beep_sound',
    'reset_recording_state',
    'record_speech',
    'validate_active_character',
    'generate_ai_response',
    'is_error_response',
    'validate_tts_data',
    'handle_tts_error',
    'play_tts_response',
    'handle_generate_reply',
    'external_start_recording',
    'external_stop_recording',
    'test_beep_sound',
    'update_beep_volume',
    'update_tts_volume',
    'test_tts_voice',
    'toggle_voice_recording',
    'update_recording_time',
    'handle_text_input',
    'start_text_generation',
    'process_text_generation',
    'clear_text_input',
    'process_electron_text_prompt',
    'load_conversation_history',
    'wait_for_history_load',
    'stop_and_transcribe_phase1',
    'start_voice_generation',
    'process_voice_ai_generation',
    'voice_btn_chain',
    'notify_ui_update',
    'process_auto_prompt',
    'execute_auto_prompt_generation',
    'start_auto_prompt_timer',
    'reset_auto_prompt_timer',
    'toggle_auto_prompt',
    'update_auto_prompt_timer_duration',
    'handle_command_deny',
    'handle_command_accept'
]
