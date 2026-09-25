"""
backend/elyth/elyth_availability.py

ELYTHセッションを実行できるモデルかの判定（真実源）。

セッションはFCツールループで動くため tools capability が必須（C9・稜裁定
2026-08-11）。判定は Ollama モデルのみ capability（モデル blob の静的属性・
永続キャッシュ）で行い、Ollamaの現在の死活は見ない（死活の可視化は
ステータスインジケーターの責務）。APIプロバイダは各社の /models が
capability情報を返さず照会手段が無いため「対応前提」＝常に実行可
（稜裁定 2026-08-15: 非対応モデルなら実行時に失敗するだけ、の割り切り）。

読み手3者（条件ドリフト禁止＝全員ここを読む）:
  - ELYTHタブのキャラ別リスト描画（ui/pages.py・グレーアウト）
  - WS elyth_toggle_character のON方向ガード（websocket_server）
  - セッション実行時のC9バックストップ（elyth_session_manager）
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 確定非対応（known=True・tools無し）= モデル差し替え等の確定情報 → 強制OFF対象
BLOCK_NO_TOOLS = "no_tools"
# 判定不能（未照会・旧Ollama等）= 一時的でありうる → fail-closedスキップ・ON設定温存
BLOCK_UNKNOWN = "unknown"


def block_reason(model_provider: Optional[str],
                 model_name: Optional[str]) -> Optional[str]:
    """ELYTHセッション実行を塞ぐ理由（None=実行可）。

    Returns:
        None / BLOCK_NO_TOOLS / BLOCK_UNKNOWN。
        理由のi18nキーは f"avail.reason_ollama_{reason}"（既存キー再利用）。
    """
    if model_provider != "ollama":
        return None
    if not model_name:
        return BLOCK_UNKNOWN
    from backend.llm.ollama_capabilities import get_caps
    caps = get_caps(model_name)
    if not caps.get("known"):
        return BLOCK_UNKNOWN
    if not caps.get("tools"):
        return BLOCK_NO_TOOLS
    return None


def char_block_reason(char_id: str) -> Optional[str]:
    """キャラ設定を読んで block_reason を返す。設定不読は判定不能扱い。"""
    from backend.conversation.character_manager import (
        _find_config_file_by_id, _load_config_file)
    config_file = _find_config_file_by_id(char_id)
    if not config_file:
        return BLOCK_UNKNOWN
    try:
        config: Dict[str, Any] = _load_config_file(config_file)
    except Exception:
        return BLOCK_UNKNOWN
    return block_reason(config.get("model_provider", "ollama"),
                        config.get("model_name", ""))


def enforce_character_order() -> List[str]:
    """確定非対応（BLOCK_NO_TOOLS）のキャラを character_order から外す＝強制OFF。

    判定不能（BLOCK_UNKNOWN）は温存する: 一時的な状態でユーザーのON設定を
    破壊しない（稜裁定 2026-08-15。実行はC9側がfail-closedでスキップする）。

    Returns:
        除去した char_id のリスト（無ければ空）。
    """
    from backend.shared.settings_store import get_setting, update_setting
    order = get_setting("elyth", "character_order", [])
    removed = [cid for cid in order
               if char_block_reason(cid) == BLOCK_NO_TOOLS]
    if not removed:
        return []
    update_setting("elyth", "character_order",
                   [cid for cid in order if cid not in removed])
    logger.info(f"[ELYTH] Force-disabled non-tools characters: {removed}")
    try:
        from backend.elyth.elyth_session_manager import get_elyth_session_manager
        get_elyth_session_manager().reload_settings()
    except Exception:
        pass
    return removed
