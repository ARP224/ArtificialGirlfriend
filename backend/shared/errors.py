"""
backend/shared/errors.py

AG-wide error type carrying a stable error code for UI-side i18n (葉モジュール
— 他のbackendモジュールをimportしない)。

audio_input/errors.py の STTError と同じ ``ag_code``/``ag_params`` 属性規約。
UI層は ``getattr(e, "ag_code", None)`` の duck typing で照合するため、この
クラスと STTError をクラス階層で繋ぐ必要はない(層規則上も繋げない)。

Policy (稜裁定 2026-07-25): ``str(e)`` は常に英語(ログ用)。翻訳はUI層が
``t(f"err.{ag_code}", **ag_params)`` で行う。
"""


class AGError(Exception):
    """Application error with a stable code (``ag_code``) and format params."""

    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.ag_code = code
        self.ag_params = params
