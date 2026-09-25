"""機能可用性(フールプルーフ)の純関数とレジストリのスモークテスト。

2026-08-11 稜裁定: Ollamaのプロバイダ一律ゲートを廃し、モデルの
capability(tools/vision)による2軸独立判定へ。判定不能はfail-closed。
"""

from backend.shared.feature_availability import (
    FEATURE_AXES,
    GATED_FEATURES,
    REASON_NO_ELYTH_KEY,
    REASON_NO_GOOGLE_KEY,
    REASON_NO_IMAGEN_MODEL,
    REASON_OLLAMA_NO_TOOLS,
    REASON_OLLAMA_NO_VISION,
    REASON_OLLAMA_UNKNOWN,
    compute_availability,
    get_availability,
    get_block_reasons,
    register_availability_provider,
    unregister_availability_provider,
)


def _caps(known=True, tools=False, vision=False):
    return {"known": known, "tools": tools, "vision": vision,
            "context_length": None}


def test_ollama_unknown_caps_blocks_everything():
    """capability判定不能(未照会・旧Ollama・停止)はfail-closedで全ブロック。"""
    for caps in (None, _caps(known=False)):
        reasons = compute_availability("ollama", True, True, True,
                                       ollama_caps=caps)
        assert set(reasons) == set(GATED_FEATURES)
        assert all(v == REASON_OLLAMA_UNKNOWN for v in reasons.values())


def test_ollama_tools_only_model_blocks_vision_axis():
    reasons = compute_availability("ollama", True, True, True,
                                   ollama_caps=_caps(tools=True))
    # tools軸のみの機能は開く
    assert reasons["notes"] is None
    assert reasons["deep_search"] is None
    assert reasons["command_execution"] is None
    assert reasons["elyth"] is None
    # vision軸を要する機能は閉じる(camera系はtools-onlyでは撮っても見えない)
    assert reasons["screen_capture"] == REASON_OLLAMA_NO_VISION
    assert reasons["camera_capture"] == REASON_OLLAMA_NO_VISION
    assert reasons["ambient_camera"] == REASON_OLLAMA_NO_VISION
    # image_generationは両軸必須(生成画像を自分でも見る=稜裁定)
    assert reasons["image_generation"] == REASON_OLLAMA_NO_VISION


def test_ollama_vision_only_model_blocks_tools_axis():
    reasons = compute_availability("ollama", True, True, True,
                                   ollama_caps=_caps(vision=True))
    # vision軸のみの機能は開く(cameraはambient一括スイッチとして機能)
    assert reasons["screen_capture"] is None
    assert reasons["camera_capture"] is None
    assert reasons["ambient_camera"] is None
    # tools軸を要する機能は閉じる
    assert reasons["notes"] == REASON_OLLAMA_NO_TOOLS
    assert reasons["command_execution"] == REASON_OLLAMA_NO_TOOLS
    assert reasons["image_generation"] == REASON_OLLAMA_NO_TOOLS


def test_ollama_dual_capable_model_is_fully_available():
    reasons = compute_availability("ollama", True, True, True,
                                   ollama_caps=_caps(tools=True, vision=True))
    assert all(v is None for v in reasons.values())


def test_feature_axes_cover_exactly_the_gated_features():
    assert set(FEATURE_AXES) == set(GATED_FEATURES)


def test_no_character_blocks_nothing_provider_dependent():
    """キャラ未選択はプロバイダ依存ブロックを付けない(稜裁定 2026-08-02)。

    未選択で封じるとON+横線+disabledの解除不能デッドになる。使えない
    組合せはキャラ選択時の enforce が強制OFFする。キャラ非依存の確定条件
    (Googleキー/Imagenモデル)のみ未選択でも判定する。
    """
    reasons = compute_availability(None, False, False, False)
    assert reasons["image_generation"] == REASON_NO_GOOGLE_KEY  # キャラ非依存
    others = {f: r for f, r in reasons.items() if f != "image_generation"}
    assert all(v is None for v in others.values())
    # ELYTHキーはキャラ依存条件=未選択では判定しない
    assert reasons["elyth"] is None
    # キャラ非依存条件が揃っていれば未選択は全機能ブロックなし
    assert all(v is None for v in compute_availability(None, False, True, True).values())


def test_api_provider_with_everything_set_is_all_available():
    reasons = compute_availability("anthropic", True, True, True)
    assert all(v is None for v in reasons.values())


def test_image_generation_requires_google_key_then_model():
    no_key = compute_availability("openai", True, False, False)
    assert no_key["image_generation"] == REASON_NO_GOOGLE_KEY
    no_model = compute_availability("openai", True, True, False)
    assert no_model["image_generation"] == REASON_NO_IMAGEN_MODEL
    # 他機能は影響を受けない
    assert no_key["notes"] is None
    assert no_key["deep_search"] is None
    # Ollama両対応モデルでも(軸が開いた後の)グローバル条件は生きる
    ollama_no_key = compute_availability(
        "ollama", True, False, False, ollama_caps=_caps(tools=True, vision=True))
    assert ollama_no_key["image_generation"] == REASON_NO_GOOGLE_KEY


def test_elyth_requires_character_key():
    reasons = compute_availability("google", False, True, True)
    assert reasons["elyth"] == REASON_NO_ELYTH_KEY
    assert reasons["deep_search"] is None
    # Ollama tools対応でもキャラ別キーは必要
    ollama_reasons = compute_availability(
        "ollama", False, True, True, ollama_caps=_caps(tools=True))
    assert ollama_reasons["elyth"] == REASON_NO_ELYTH_KEY


def test_registry_fallback_and_provider():
    unregister_availability_provider()
    try:
        # 未登録 → {} (=ブロックなし扱い・配線欠落で機能を封じない)
        assert get_availability() == {}

        register_availability_provider(lambda: {"notes": REASON_OLLAMA_NO_TOOLS})
        assert get_availability() == {"notes": REASON_OLLAMA_NO_TOOLS}

        # provider例外 → {} に縮退
        def _boom():
            raise RuntimeError("boom")
        register_availability_provider(_boom)
        assert get_availability() == {}
    finally:
        unregister_availability_provider()


def test_block_reasons_are_localized():
    register_availability_provider(
        lambda: {"notes": REASON_OLLAMA_NO_TOOLS, "deep_search": None})
    try:
        reasons = get_block_reasons()
        assert reasons["deep_search"] is None
        # i18nキーが実訳文へ解決されている(キー素通しでない)
        assert reasons["notes"] and reasons["notes"] != REASON_OLLAMA_NO_TOOLS
    finally:
        unregister_availability_provider()
