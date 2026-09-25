"""Character voice dropdown handlers.

言語→ローカルTTSエンジンは1対1(ja=SBV2 / en=Kokoro・2026-07-26裁定。
ElevenLabsは多言語APIのため常に併記)。キャラ作成/編集フォームの言語DDの
``.input``(ユーザー操作のみ発火・プログラム的な値セットでは発火しない)で
ボイスDDの選択肢を絞り込む。編集フォームへのキャラ読込は
load_character_for_edit が自前で絞った choices を供給するので競合しない。
保存時の validate_character_data が決定論的バックストップ。
"""

import gradio as gr

from .api_settings import _list_sbv2_models


def update_voice_choices_for_language(language, current_value):
    """Re-filter the voice dropdown for the newly selected language.

    Keeps the current selection when it is still valid under the new filter
    (ElevenLabs voices survive both languages); clears it otherwise.
    """
    from backend.shared.api_settings import get_all_tts_choices
    choices = get_all_tts_choices(_list_sbv2_models(), language=language)
    valid_values = {value for _label, value in choices}
    return gr.update(
        choices=choices,
        value=current_value if current_value in valid_values else None,
    )
