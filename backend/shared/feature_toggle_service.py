"""
backend/feature_toggle_service.py

Domain-side handlers for feature-toggle commands (V3 inversion, B3).

The WebSocket transport publishes toggle commands via
:func:`backend.shared.feature_commands.dispatch_feature_toggle`; this module registers
the domain handlers that perform the effect:

  1. flip the runtime flag  (``backend.backend.set_<feature>_enabled``)
  2. persist the choice     (``backend.shared.settings_store.update_setting``)

The spine setters are resolved at *call time* (via ``getattr`` on the
``backend.backend`` module) rather than imported at module load, so:

  * test seams that monkeypatch ``backend.backend.set_*_enabled`` are honoured
    (mirrors ``backend.server.websocket_server._ui_event_subscriber`` from B2), and
  * no import cycle forms when this module is imported during start-up.

Naming is fully regular: command ``<feature>`` maps to setter
``set_<feature>_enabled`` and to settings key ``<feature>_enabled`` under the
``features`` section, so the table below is just the list of features plus the
one domain-specific cascade (disabling PC Status also disables Screen Capture).
"""

import logging

from backend.shared.feature_commands import register_feature_toggle_handler
from backend.shared.platform_caps import is_feature_supported
from backend.shared.settings_store import update_setting

logger = logging.getLogger(__name__)

# Feature commands handled here. The WebSocket transport owns the infra policy
# (default value, response framing); this module owns the domain effect and
# the enable gates (platform / availability / server mode).
_FEATURES = [
    "pc_status",
    "screen_capture",
    "talk_theme",
    "speechless",
    "command_execution",
    "notes",
    "image_generation",
    "camera_capture",
    "ambient_camera",
    "deep_search",
    "elyth",
]

# サーバーモードでON不可の機能(ホストPC側の情報/操作なのでクライアントから
# 使う意味がない=稜裁定 2026-07-31)。旧・WS層ガード(command_executionのみ・
# 裸のエラー応答=無言失敗+JS側トグル表示の巻き戻り副作用)をこちらへ統一。
_SERVER_MODE_BLOCKED = frozenset({"pc_status", "screen_capture", "command_execution"})


def _camera_ambient_bundled() -> bool:
    """vision-only(toolsなし)のOllamaモデルでは camera/ambient を一括スイッチ
    として連動させるか(2026-08-11 稜裁定)。

    このモデルではcamera単独ONが無意味(撮影ツールはtools軸が要るため提供
    できず、cameraの残る役割はambientの前提スイッチだけ)なので、cameraの
    ONでambientも同時ON・どちらのOFFでも両方OFFにする。判定は呼出時import
    (公認継ぎ目①=sharedからllm/appへのモジュールロード時importを作らない)。
    判定不能・例外は非連動(通常の2段階操作)へ縮退。
    """
    try:
        from backend import backend as _backend
        state = getattr(_backend, "_backend_state", None)
        char_id = getattr(state, "active_character_id", None) if state else None
        if not char_id:
            return False
        config = _backend.load_character_config(char_id)
        if isinstance(config, dict) and "result" in config:
            config = config.get("result") or {}
        if config.get("model_provider", "ollama") != "ollama":
            return False
        model = config.get("model_name", "") or config.get("ollama_model_name", "")
        if not model:
            return False
        from backend.llm.ollama_capabilities import get_caps
        caps = get_caps(model)
        return bool(caps.get("known") and caps.get("vision")
                    and not caps.get("tools"))
    except Exception:
        logger.exception("camera/ambient bundle check failed; treating as unbundled")
        return False


def _make_handler(feature: str):
    setter_name = f"set_{feature}_enabled"
    settings_key = f"{feature}_enabled"

    def handler(enabled: bool) -> dict:
        # Mac 3-6 layer 3: platform-unsupported features cannot be enabled
        # from any client (the UI greys them out, but WS commands could
        # still arrive from stale/remote clients).
        if enabled and not is_feature_supported(feature):
            logger.warning(
                f"Feature '{feature}' is not supported on this platform — enable ignored")
            return {"success": False, "error": "unsupported_platform"}
        # Resolve the spine setter at call time so monkeypatch seams are honoured
        # and no import cycle forms at module load.
        from backend import backend as _backend

        # サーバーモード: ホストPC側機能のONを理由ポップアップ付きで拒否
        # (UIはグレーアウト済み=これは古い画面・自作クライアント向けの保険。
        # OFFは常に許可。runtime only=設定ファイルは書き換えない)。
        if enabled and feature in _SERVER_MODE_BLOCKED:
            _state = getattr(_backend, "_backend_state", None)
            if _state is not None and getattr(_state, "server_mode", False):
                from backend.shared.i18n import t
                logger.info(f"Feature '{feature}' enable blocked: server mode")
                status = {}
                try:
                    status = getattr(_backend, "get_feature_status")() or {}
                except Exception:
                    pass
                return {**status, "success": False, "error": "server_mode",
                        "popup_title": t("avail.popup_title"),
                        "popup_message": t("utility.server_mode_unsupported")}

        # フールプルーフ層2: 前提条件(Ollamaキャラ/APIキー未設定等)を満たさない
        # 機能はONにできない(UIはグレーアウト済みだが、古い画面・リモート
        # クライアントからのWSコマンドはここで止める)。OFF操作は常に許可
        # (ON+利用不能から抜けられなくなる罠を作らない)。応答に現在の全トグル
        # 状態を含めるのは、JS側が楽観的にフリップ済みの表示を正へ戻すため。
        if enabled:
            from backend.shared.feature_availability import get_availability
            reason_key = get_availability().get(feature)
            if reason_key:
                from backend.shared.i18n import t
                logger.info(f"Feature '{feature}' enable blocked: {reason_key}")
                status = {}
                try:
                    status = getattr(_backend, "get_feature_status")() or {}
                except Exception:
                    pass
                return {**status, "success": False, "error": "feature_unavailable",
                        "popup_title": t("avail.popup_title"),
                        "popup_message": t(reason_key)}

        result = getattr(_backend, setter_name)(enabled)
        update_setting("features", settings_key, enabled)
        # Disabling PC Status also disables Screen Capture (mirrors the legacy
        # _handle_set_pc_status persistence side-effect; the spine setter applies
        # the same cascade to the runtime flag).
        if feature == "pc_status" and not enabled:
            update_setting("features", "screen_capture_enabled", False)
        # Disabling Camera also disables Live Camera (same cascade shape;
        # the spine setter applies it to the runtime flag).
        if feature == "camera_capture" and not enabled:
            update_setting("features", "ambient_camera_enabled", False)

        # vision-only Ollamaモデルの camera/ambient 一括連動 (2026-08-11 稜裁定):
        # camera ON で ambient も同時ON。OFFはどちらを切っても両方OFF
        # (camera OFF→ambient OFF は上の既存カスケードが担い、
        #  ambient OFF→camera OFF をここで足す)。
        if feature == "camera_capture" and enabled and _camera_ambient_bundled():
            from backend.shared.feature_commands import dispatch_feature_toggle
            ambient_result = dispatch_feature_toggle("ambient_camera", True) or {}
            if ambient_result.get("success", True) is False:
                # 片肺ON(camera単独)を作らない: ambient側の有効化が拒否されたら
                # cameraも巻き戻して両方OFFを保つ(稜裁定)
                getattr(_backend, "set_camera_capture_enabled")(False)
                update_setting("features", "camera_capture_enabled", False)
                logger.info("camera/ambient bundle ON rolled back "
                            "(ambient enable rejected)")
                return ambient_result
            # 両方ONを反映した最新の全トグル状態を返す(JSが表示へ適用する)
            result = ambient_result
        if feature == "ambient_camera" and not enabled and _camera_ambient_bundled():
            getattr(_backend, "set_camera_capture_enabled")(False)
            update_setting("features", "camera_capture_enabled", False)
            try:
                result = getattr(_backend, "get_feature_status")() or result
            except Exception:
                pass
        return result

    return handler


for _feature in _FEATURES:
    register_feature_toggle_handler(_feature, _make_handler(_feature))
