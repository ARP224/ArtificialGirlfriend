"""
ui/error_handler.py

Generic error handling utilities for the Artificial Girlfriend UI.
Provides decorators and functions for consistent error handling across modules.
"""

import logging
import traceback
from functools import wraps
from typing import Callable, Any

from backend.shared.i18n import t
from .constants import ERROR_MESSAGE_PREFIX
from .state import show_error_popup, show_warning_popup

logger = logging.getLogger(__name__)


def user_error(key: str, **kwargs) -> str:
    """チャットに表示するユーザー向けエラー文字列（TTS抑制対象）を組み立てる。

    is_error_response() は ERROR_MESSAGE_PREFIX の前方一致で判定するため、
    ユーザー向けエラー文字列はこの関数以外で作らないこと。
    """
    return ERROR_MESSAGE_PREFIX + t(key, **kwargs)


def error_status_text(error_msg: str) -> str:
    """入力欄下のステータス表示用に user_error 文字列を「❌ 本文」へ整形する。

    従来は原因を問わず汎用文 t('gen.error') を出していたが、翻訳済みの
    具体メッセージの唯一の到達経路が事故的なログ転送トーストだったため、
    ステータス行を具体文へ格上げした(稜裁定 2026-08-02)。
    """
    if error_msg.startswith(ERROR_MESSAGE_PREFIX):
        error_msg = error_msg[len(ERROR_MESSAGE_PREFIX):]
    return "❌ " + error_msg


def resolve_error_code(e: BaseException):
    """例外から安定エラーコードを解決する(稜裁定 2026-07-25)。

    1. ``ag_code`` 属性(STTError/AGError/WS橋の動的属性 — duck typing)。
    2. 型で判別できる既知の外部例外(文字列一致は禁止)。sys.modulesガード=
       当該ライブラリが未ロードならその型の例外は存在し得ないので import
       コストを払わない。
    3. どちらでもなければ (None, {}) — 呼び手はタイトルのみ翻訳し本文は
       英語原文 str(e) をそのまま見せる(情報は捨てない)。

    Returns:
        (code, params): code は locales の ``err.<code>`` キー接尾辞 or None。
    """
    import sys

    code = getattr(e, "ag_code", None)
    if code:
        return code, getattr(e, "ag_params", None) or {}

    torch = sys.modules.get("torch")
    if torch is not None:
        oom = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
        if oom is not None and isinstance(e, oom):
            return "cuda_oom", {}

    if isinstance(e, MemoryError):
        return "out_of_memory", {}

    requests_mod = sys.modules.get("requests")
    if requests_mod is not None:
        exc = requests_mod.exceptions
        if isinstance(e, exc.Timeout):
            return "network_timeout", {}
        if isinstance(e, exc.ConnectionError):
            return "network_unreachable", {}

    return None, {}


def translate_error(e: BaseException) -> str:
    """例外をユーザー向け本文へ: コード解決できれば翻訳、できなければ英語原文。"""
    code, params = resolve_error_code(e)
    if code:
        return t(f"err.{code}", **params)
    return str(e)


def handle_module_operation(operation_name: str, show_popup: bool = True) -> Callable:
    """
    Decorator for consistent error handling across UI operations.
    
    Args:
        operation_name: Name of the operation for error messages
        show_popup: Whether to show error popups to user
        
    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            try:
                return func(*args, **kwargs)
            except ModuleNotFoundError as e:
                logger.critical(f"{operation_name} missing dependencies: {e}")
                if show_popup:
                    show_error_popup(t('popup.dependency_error_title'),
                                   t('popup.dependency_error', op=operation_name, error=e))
                raise
            except ImportError as e:
                logger.critical(f"{operation_name} import failed: {e}")
                if show_popup:
                    show_error_popup(t('popup.import_error_title'),
                                   t('popup.import_error', op=operation_name, error=e))
                raise
            except PermissionError as e:
                logger.error(f"{operation_name} permission denied: {e}")
                if show_popup:
                    show_error_popup(t('popup.permission_error_title'),
                                   t('popup.permission_error', op=operation_name, error=e))
                raise
            except TimeoutError as e:
                logger.error(f"{operation_name} timed out: {e}")
                if show_popup:
                    show_warning_popup(t('popup.timeout_title'),
                                     t('popup.timeout', op=operation_name))
                raise
            except RuntimeError as e:
                # ag_expected 付き(無音STT等の「期待されたイベント」)は
                # エラー扱いせず素通し — 呼出元が専用の正常系処理を持つ
                # (稜裁定 2026-08-02)
                if getattr(e, 'ag_expected', False):
                    raise

                error_str = str(e).lower()
                logger.error(f"{operation_name} runtime error: {e}")

                # ag_code付き(STTError等)はコード→翻訳本文で表示(最優先)
                code, params = resolve_error_code(e)
                if code:
                    if show_popup:
                        show_error_popup(t('popup.op_error_title', op=operation_name),
                                         t(f'err.{code}', **params))
                    raise

                # Handle specific runtime errors with appropriate messages
                if "busy" in error_str or "in use" in error_str:
                    if show_popup:
                        show_error_popup(t('popup.resource_busy_title'),
                                       t('popup.resource_busy', op=operation_name))
                elif "not initialized" in error_str:
                    if show_popup:
                        show_error_popup(t('popup.not_initialized_title'),
                                       t('popup.not_initialized', op=operation_name))
                elif "memory" in error_str or "cuda" in error_str or "gpu" in error_str:
                    if show_popup:
                        show_error_popup(t('popup.resource_error_title'),
                                       t('popup.resource_error', op=operation_name))
                else:
                    if show_popup:
                        show_error_popup(t('popup.op_error_title', op=operation_name), str(e))
                raise
            except Exception as e:
                error_details = traceback.format_exc()
                logger.error(f"Unexpected error in {operation_name}: {e}")
                logger.debug(f"{operation_name} error details: {error_details}")

                if show_popup:
                    # 型で判別できる既知例外(CUDA OOM等)は翻訳本文、未知は
                    # 英語原文のまま(タイトルのみ翻訳=稜裁定 2026-07-25)
                    code, params = resolve_error_code(e)
                    if code:
                        show_error_popup(t('popup.op_error_title', op=operation_name),
                                         t(f'err.{code}', **params))
                    else:
                        show_error_popup(t('popup.unexpected_title'),
                                       t('popup.unexpected', op=operation_name, error=str(e)))
                raise
        return wrapper
    return decorator


def handle_llm_operation(operation_name: str) -> Callable:
    """
    Specialized error handler for LLM operations with specific error types.
    
    Args:
        operation_name: Name of the LLM operation
        
    Returns:
        Decorator function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # 既に user_error() 済み(=ERROR_MESSAGE_PREFIX 前置)の例外は
                # 処理済み: ここで再ログ+err.generic 再ラップすると二重
                # マーカー「エラー: [⚠] …」と余分なトーストになる(サブOS
                # 実機 2026-08-02)。無ログで素通しし呼出元に委ねる。
                if str(e).startswith(ERROR_MESSAGE_PREFIX):
                    raise

                error_msg = str(e).lower()

                # Categorize LLM-specific errors. The user_error() text is
                # rendered in the chat (with text-status "❌"), so no popup here.
                if "ollama" in error_msg and ("connect" in error_msg or "refused" in error_msg):
                    logger.error(f"{operation_name}: Ollama connection failed")
                    raise RuntimeError(user_error('err.service_unavailable'))
                elif "model" in error_msg and "not found" in error_msg:
                    logger.error(f"{operation_name}: Model not found")
                    raise RuntimeError(user_error('err.model_not_found'))
                elif "timeout" in error_msg:
                    logger.error(f"{operation_name}: Operation timed out")
                    raise RuntimeError(user_error('err.generation_timeout'))
                elif "memory" in error_msg or "resource" in error_msg:
                    logger.error(f"{operation_name}: Resource limitation")
                    raise RuntimeError(user_error('err.low_resources'))
                else:
                    logger.error(f"{operation_name}: {e}")
                    error_details = traceback.format_exc()
                    logger.debug(f"{operation_name} error details: {error_details}")
                    raise RuntimeError(user_error('err.generic', error=str(e)))
        return wrapper
    return decorator


# Export public API
__all__ = [
    'handle_module_operation',
    'handle_llm_operation',
    'user_error',
    'resolve_error_code',
    'translate_error',
]