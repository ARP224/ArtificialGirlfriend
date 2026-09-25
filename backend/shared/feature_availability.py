"""
backend/shared/feature_availability.py

Utility Panel 機能の「前提条件つき可用性」の共有インターフェース(フールプルーフ)。

平時からバックエンドのツールゲート(conversation_manager._should_include_*)は
前提を満たさない機能をLLMに渡さず黙って縮退するが、UI側はトグルを押せてしまい
「ONなのに何も起きない」状態になっていた。本モジュールはその前提条件を
UI/transport から読める形で一元化する:

  - :func:`compute_availability` — 純関数。入力(プロバイダ・キー設定有無)から
    機能→ブロック理由キー(None=利用可)を導出する。判定基準はツールゲートの
    静的条件と同一(レート制限等の動的条件は含めない=ゲート側に残す)。
  - provider レジストリ — 所有側(backend.backend)が実状態の読み手を登録する
    (runtime_state と同じ V2/V3 逆転の形。shared から上向き import しない)。
  - :func:`get_block_reasons` — 消費者向け。理由キーを i18n で解決した
    {feature: 理由文 | None} を返す(パネルHTML・WS配信・tooltip用)。

理由キーは locales の i18n キーそのもの(avail.reason_*)。
"""

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 前提条件つきの8機能。Talk Theme は対象外(Ollamaでも手動テーマの
# プロンプト注入が生きる=部分動作するため。2026-07-25 稜裁定)。
# screen_capture は画像をLLMに送る機能(PC Status本体はテキスト情報のみで対象外)。
GATED_FEATURES = (
    "notes",
    "image_generation",
    "screen_capture",
    "camera_capture",
    "ambient_camera",
    "deep_search",
    "elyth",
    "command_execution",
)

# Ollamaキャラにのみ適用される機能→(tools要否, vision要否)の軸要件
# (2026-08-11 稜裁定: プロバイダ一律ゲートを廃し、モデルのcapabilityで判定)。
# - image_generation は生成画像を自分でも見るため tools+vision の両対応
# - camera_capture は vision のみ必須: vision-onlyモデルでは「ambient一括
#   スイッチ」として機能し(撮影ツールの提供有無は会話ゲート側のtools軸が
#   絞る)、tools-onlyモデルでは撮った画像を見られないためブロック
FEATURE_AXES = {
    "notes": (True, False),
    "image_generation": (True, True),
    "screen_capture": (False, True),
    "camera_capture": (False, True),
    "ambient_camera": (False, True),
    "deep_search": (True, False),
    "elyth": (True, False),
    "command_execution": (True, False),
}

# 理由キー(= i18n キー)
REASON_OLLAMA_NO_TOOLS = "avail.reason_ollama_no_tools"
REASON_OLLAMA_NO_VISION = "avail.reason_ollama_no_vision"
REASON_OLLAMA_UNKNOWN = "avail.reason_ollama_unknown"
REASON_NO_GOOGLE_KEY = "avail.reason_no_google_key"
REASON_NO_IMAGEN_MODEL = "avail.reason_no_imagen_model"
REASON_NO_ELYTH_KEY = "avail.reason_no_elyth_key"
REASON_NO_OPENAI_KEY = "avail.reason_no_openai_key"

# 疑似機能: STTエンジン選択の「OpenAI API」選択肢(パネルのトグルではないが
# 同じ可用性チャンネルで配る。provider 側が付与し、JSのラジオロックが読む)
STT_OPENAI = "stt_openai"

# 疑似機能: 📎画像添付。vision非対応のOllamaモデルでは入口で塞ぐ
# (2026-07-31 稜裁定・2026-08-11 per-model化)。キャラ未選択では塞がない
# (添付自体はキャラ非依存の操作)。provider 側が付与し、WS attach_image の
# サーバー側拒否とJS通知が読む。ドキュメント添付は対象外(テキスト化される)。
IMAGE_ATTACH = "image_attach"

# 疑似機能: Utility Panel の Appear ボタン(MotionPNGPlayer起動)。
# Motionフォルダ未設定/実体なしのキャラ(またはキャラ未選択)では起動が
# 必ず失敗するため入口でグレーアウトする(稜GO 2026-08-15)。GATED_FEATURES
# には入れない=トグルではないので enforce の強制OFF対象外。
# 注意: 表示中は同じボタンが Disappear として働くため、JS側は
# window.motionPngTuberActive が真の間は無効化を適用しない
# (「キャラ切替ではPlayerを閉じない」稜裁定 2026-08-01 と両立させる)。
MOTION_APPEAR = "motion_appear"
REASON_NO_MOTION_FOLDER = "avail.reason_no_motion_folder"
REASON_MOTION_FOLDER_MISSING = "avail.reason_motion_folder_missing"
REASON_NO_CHARACTER = "avail.reason_no_character"


def compute_availability(model_provider: Optional[str],
                         elyth_key_set: bool,
                         google_key_set: bool,
                         imagen_model_set: bool,
                         ollama_caps: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Optional[str]]:
    """機能→ブロック理由キー(None=利用可)を導出する純関数。

    Args:
        model_provider: アクティブキャラの model_provider。None=キャラ未選択。
            未選択はブロックしない: これから選ぶキャラ次第で使える機能を
            先回りで封じない(封じるとON+横線+disabledの解除不能デッドに
            なる)。使えない組合せはキャラ選択時の enforce_feature_availability
            が強制OFFする(稜裁定 2026-08-02)。キャラ非依存の確定条件
            (Googleキー/Imagenモデル)だけは未選択でも判定する。
        elyth_key_set: アクティブキャラに elyth_api_key が設定されているか
            (キャラ依存条件のため model_provider=None では判定しない)。
        google_key_set: Google APIキーが設定されているか。
        imagen_model_set: 画像生成モデル(Imagen)が設定されているか。
        ollama_caps: model_provider=="ollama" のときのモデルcapability
            (ollama_capabilities.get_caps() の返り値
            {"known", "tools", "vision", ...})。None・known=False は判定不能
            = 全軸ブロック(fail-closed。理由は REASON_OLLAMA_UNKNOWN)。
            非Ollamaプロバイダでは無視される。
    """
    def _ollama_reason(feature: str) -> Optional[str]:
        if model_provider != "ollama":
            return None
        caps = ollama_caps or {}
        if not caps.get("known"):
            return REASON_OLLAMA_UNKNOWN
        needs_tools, needs_vision = FEATURE_AXES[feature]
        if needs_tools and not caps.get("tools"):
            return REASON_OLLAMA_NO_TOOLS
        if needs_vision and not caps.get("vision"):
            return REASON_OLLAMA_NO_VISION
        return None

    reasons: Dict[str, Optional[str]] = {
        f: _ollama_reason(f) for f in GATED_FEATURES}

    # 画像生成はプロバイダ条件に加えてグローバル設定2点が必要
    # (キャラ非依存の確定条件=キャラ未選択でも判定する)
    if reasons["image_generation"] is None:
        if not google_key_set:
            reasons["image_generation"] = REASON_NO_GOOGLE_KEY
        elif not imagen_model_set:
            reasons["image_generation"] = REASON_NO_IMAGEN_MODEL

    # ELYTH はキャラ別 API キーが必要(キャラ依存条件=キャラ選択時のみ判定)
    if model_provider is not None and reasons["elyth"] is None and not elyth_key_set:
        reasons["elyth"] = REASON_NO_ELYTH_KEY

    return reasons


# --------------------------------------------------------------------------
# Provider registry (owner = backend.backend / 実状態の読み手)
# --------------------------------------------------------------------------

AvailabilityProvider = Callable[[], Dict[str, Optional[str]]]

_provider: Optional[AvailabilityProvider] = None


def register_availability_provider(provider: AvailabilityProvider) -> None:
    """所有側の provider を登録する(最後の登録が勝つ=単一所有)。"""
    global _provider
    _provider = provider


def unregister_availability_provider() -> None:
    """登録解除(未登録でもエラーにしない)。主にテスト用。"""
    global _provider
    _provider = None


def get_availability() -> Dict[str, Optional[str]]:
    """機能→ブロック理由キー(None=利用可)の現在値。

    provider 未登録/例外時は {} を返す=全機能利用可扱い(配線欠落で
    機能を封じない縮退。実ブロックは登録済み provider が担う)。
    """
    if _provider is None:
        return {}
    try:
        return _provider() or {}
    except Exception:
        logger.exception("feature-availability provider raised; treating as no blocks")
        return {}


def get_block_reasons() -> Dict[str, Optional[str]]:
    """機能→ローカライズ済みブロック理由文(None=利用可)。

    パネルHTML(tooltip)・WS配信・拒否ポップアップの共通表示形。
    """
    from backend.shared.i18n import t
    reasons: Dict[str, Optional[str]] = {}
    for feature, key in get_availability().items():
        reasons[feature] = t(key) if key else None
    return reasons
