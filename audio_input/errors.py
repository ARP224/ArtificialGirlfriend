"""
audio_input/errors.py

STT-layer error type carrying a stable error code for UI-side i18n.

Layering: audio_input is a pure leaf imported by both backend and ui, so it
must NOT import backend.shared (依存方向が逆転する). The UI layer therefore
matches errors by the ``ag_code`` attribute (duck typing) instead of a shared
class — backend/shared/errors.py の AGError も同じ属性規約を持つ。

Policy (稜裁定 2026-07-25): ``str(e)`` は常に英語のまま(ログ用)。UI層が
``ag_code`` を locale キー ``err.<code>`` に写像してポップアップ本文を翻訳する。
コードを変えるときは locales/{ja,en}.json の対応キーも同時に変えること。
"""


class STTError(RuntimeError):
    """STT error with a stable code (``ag_code``) and format params.

    Args:
        code: Locale-key suffix — the UI resolves ``t(f"err.{code}", **params)``.
        message: English message for logs / str(e) (never shown translated).
        **params: Named placeholders for the locale template.
    """

    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.ag_code = code
        self.ag_params = params


# Stable code constants (single source — string literals は書かない)
STT_NO_SPEECH = "stt_no_speech"
STT_CORRUPTED_AUDIO = "stt_corrupted_audio"
STT_API_KEY_MISSING = "stt_api_key_missing"
STT_AUDIO_TOO_LARGE = "stt_audio_too_large"
STT_API_TIMEOUT = "stt_api_timeout"
STT_API_UNREACHABLE = "stt_api_unreachable"
STT_API_AUTH = "stt_api_auth"
STT_API_RATE_LIMITED = "stt_api_rate_limited"
STT_API_HTTP_ERROR = "stt_api_http_error"
STT_API_BAD_RESPONSE = "stt_api_bad_response"
STT_MODEL_UNAVAILABLE = "stt_model_unavailable"
STT_MODEL_RELEASED = "stt_model_released"
