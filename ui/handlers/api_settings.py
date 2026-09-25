"""
ui/handlers/api_settings.py

Gradio handlers for the API-settings tab (embedding / image-generation
model selection, web-search & image-input blacklists, Google Maps key) that
were wired inline in app.py's ``create_gradio_interface``.

B11 (ST4) extracts the handler bodies here. These are self-contained leaves:
each body uses only its own argument(s), ``gr.update`` and a call-time
``from backend.shared.api_settings import ...`` -- none closes over a local Gradio
component variable, so they move verbatim (dedent only) and are re-imported
into app.py while the ``.click(fn=...)`` wiring (which references the local
``api_settings_components`` dict) stays in app.py.
"""

import gradio as gr

from backend.shared.i18n import t


def _save_embedding_handler(selected_value):
    # Generator (段階表示): 較正は最大数十秒の同期API/Ollama往復で、出力先
    # Markdownがvisible=False始まりのためスピナーの貼り先すら無く、押下後
    # 完全無反応に見えていた(稜サブOSテスト2026-08-01)。保存成功を即yield
    # してから較正に入る。
    if not selected_value:
        yield gr.update(value=t('hdl.api_settings.no_model_selected'), visible=True)
        return
    from backend.shared.api_settings import (
        save_embedding_model, decode_model_value, get_provider_display_name,
    )
    save_embedding_model(selected_value)
    provider, model_name = decode_model_value(selected_value)
    saved_msg = t(
        'hdl.api_settings.embedding_saved',
        model=model_name, provider=get_provider_display_name(provider),
    )
    yield gr.update(
        value=saved_msg + "\n\n" + t('hdl.api_settings.calibrating'),
        visible=True,
    )
    msg = saved_msg
    # Calibrate the memory-relevance threshold for the newly saved model
    # (cosine scales differ per embedding model; a fixed threshold silently
    # disabled long-term memory on text-embedding-3-large — ST6 §7-4).
    # Failure is non-fatal: the model stays saved and memory search falls
    # back to the seeded/default threshold; re-saving retries calibration.
    from backend.memory.embedding_calibration import calibrate_and_save
    cal = calibrate_and_save(selected_value)
    if cal.get("success"):
        msg += "\n\n" + t(
            'hdl.api_settings.calibration_done',
            threshold=cal['threshold'], rel_min=cal['rel_min'], rel_max=cal['rel_max'],
            irr_min=cal['irr_min'], irr_max=cal['irr_max'], noise_pass=cal['noise_pass'],
        )
    else:
        msg += "\n\n" + t(
            'hdl.api_settings.calibration_failed',
            error=cal.get('error', 'unknown error'),
        )
    # Re-embedding migration (ST6 §7-4 sequel): memories stamped with a
    # different embedding model are dormant for search until re-embedded.
    # Runs in the background; completion is logged. Failure is non-fatal
    # (startup rescan / re-save retries) — try/except で保存成功メッセージを
    # 道連れにしない(コメントの意図と実装の不一致解消)。
    try:
        from backend.backend import start_embedding_migration
        mig = start_embedding_migration()
    except Exception as e:
        mig = {"success": False, "error": str(e)}
    if mig.get("success") and mig.get("pending", 0) > 0:
        if mig.get("started"):
            msg += "\n\n" + t('hdl.api_settings.migration_started', pending=mig['pending'])
        else:
            msg += "\n\n" + t('hdl.api_settings.migration_running', pending=mig['pending'])
    elif not mig.get("success"):
        msg += "\n\n" + t(
            'hdl.api_settings.migration_failed',
            error=mig.get('error', 'unknown error'),
        )
    yield gr.update(value=msg, visible=True)

def _save_image_gen_handler(selected_value):
    if not selected_value:
        return gr.update(value=t('hdl.api_settings.no_model_selected'), visible=True)
    from backend.shared.api_settings import save_image_generation_model
    result = save_image_generation_model(selected_value)
    # 画像生成の前提条件が変わった=utility panelのグレー状態を再配信
    try:
        from backend.backend import enforce_feature_availability
        enforce_feature_availability()
    except Exception:
        pass
    return gr.update(value=result.get("message", ""), visible=True)

def _save_ollama_ctx_handler(selected_value):
    from backend.shared.api_settings import save_ollama_num_ctx
    result = save_ollama_num_ctx(selected_value)
    if result.get("success"):
        # num_ctxはLLMインスタンス構築時に焼き込まれるため、キャッシュ済み
        # Ollama LLMを無効化して次の生成で新値により再作成させる
        # (=モデル再ロードが走る。会話中でも次の送信から反映)
        try:
            from backend.backend import invalidate_ollama_llm_cache
            invalidate_ollama_llm_cache()
        except Exception:
            pass
    return gr.update(value=result.get("message", ""), visible=True)


def _refresh_blacklist():
    from backend.shared.api_settings import (
        get_web_search_blacklist, decode_model_value,
        get_provider_display_name
    )
    blacklist = get_web_search_blacklist()
    choices = []
    for encoded in blacklist:
        provider, model_name = decode_model_value(encoded)
        display = get_provider_display_name(provider)
        choices.append((f"{model_name} ({display})", encoded))
    return gr.update(choices=choices, value=[])

def _remove_from_blacklist(selected):
    if selected:
        from backend.shared.api_settings import remove_from_web_search_blacklist
        remove_from_web_search_blacklist(selected)
    return _refresh_blacklist()

def _refresh_image_blacklist():
    from backend.shared.api_settings import (
        get_image_blacklist, decode_model_value,
        get_provider_display_name
    )
    blacklist = get_image_blacklist()
    choices = []
    for encoded in blacklist:
        provider, model_name = decode_model_value(encoded)
        display = get_provider_display_name(provider)
        choices.append((f"{model_name} ({display})", encoded))
    return gr.update(choices=choices, value=[])

def _remove_from_image_blacklist(selected):
    if selected:
        from backend.shared.api_settings import remove_from_image_blacklist
        remove_from_image_blacklist(selected)
    return _refresh_image_blacklist()

def _save_google_maps_key(key):
    from backend.shared.api_settings import save_google_maps_api_key
    result = save_google_maps_api_key(key)
    return gr.update(value=result["message"], visible=True)

def _save_api_key_handler(provider: str):
    """Create a save handler for a specific provider."""
    def handler(key_value):
        from backend.shared.api_settings import save_api_key
        result = save_api_key(provider, key_value)
        msg = result.get("message", "")
        # Re-verdict the status indicator with the new key right away —
        # a stale 🔴/🟢 from the previous key must not linger (best-effort).
        try:
            from ui.status_probe import invalidate
            from backend.shared.ui_events import publish_ui_update
            invalidate(provider)
            publish_ui_update("update_status", reason=f"api_key_saved:{provider}")
        except Exception:
            pass
        # キー有無で機能可用性(画像生成のGoogleキー条件等)が変わりうる
        try:
            from backend.backend import enforce_feature_availability
            enforce_feature_availability()
        except Exception:
            pass
        return gr.update(value=msg, visible=True)
    return handler

def _embedding_choices_update():
    """gr.update with the current embedding-capable model choices.

    value も一緒に張り直す(_imagen_choices_update と同型): APIキー保存後の
    再populateで保存済み選択が画面上だけ解除され、そのままSaveを押すと
    「モデルが選択されていません。」に落ちる取りこぼしを防ぐ。
    """
    from backend.shared.api_settings import (
        get_embedding_model_choices, load_api_settings,
    )
    choices = get_embedding_model_choices()
    saved = load_api_settings().get("embedding_model", "")
    value = saved if any(v == saved for _, v in choices) else None
    return gr.update(choices=choices, value=value)

def _imagen_choices_update():
    """gr.update with the current Imagen (image generation) model choices.

    value も一緒に張り直す: モデル更新で保存済み選択が解除された場合
    (提供終了モデルの掃除)にドロップダウンの見た目が旧モデルのまま残る
    のを防ぐ。保存はSaveボタン式(.change非配線)なので書き戻しピンポンは
    起きない。
    """
    from backend.shared.api_settings import get_imagen_models, load_api_settings
    choices = get_imagen_models()
    saved = load_api_settings().get("image_generation_model", "")
    value = saved if any(v == saved for _, v in choices) else None
    return gr.update(choices=choices, value=value)

def _save_key_and_refresh(provider: str):
    """Save the key, fetch the provider's model list, and push every dropdown
    that derives from it.

    The embedding / image-generation choices were build-time frozen, so a
    freshly registered key needed an app restart to show any models (Mac
    実機 2026-07-22). Saving a key now runs the same fetch as the provider's
    refresh button and re-populates the derived dropdowns in one event.
    Clearing a key skips the network fetch but still recomputes the derived
    choices (so provider models correctly disappear from them).

    Output shape per provider (must match the app.py wiring):
      openai:    (status, models_dd, stt_dd, create_llm, edit_llm, embedding)
      google:    (status, models_dd, create_llm, edit_llm, embedding, imagen)
      anthropic / xai: (status, models_dd, create_llm, edit_llm)
    """
    def handler(key_value):
        from backend.shared.api_settings import save_api_key, fetch_provider_models
        result = save_api_key(provider, key_value)
        msg = result.get("message", "")
        # Re-verdict the status indicator with the new key right away —
        # a stale 🔴/🟢 from the previous key must not linger (best-effort).
        try:
            from ui.status_probe import invalidate
            from backend.shared.ui_events import publish_ui_update
            invalidate(provider)
            publish_ui_update("update_status", reason=f"api_key_saved:{provider}")
        except Exception:
            pass
        if (key_value or "").strip():
            fetch = fetch_provider_models(provider)
            msg += "\n\n" + fetch.get("message", "")
            # A failed fetch does not overwrite the stored model list, so the
            # dropdown must keep its current choices too (no-op update).
            models_update = (gr.update(choices=fetch.get("models", []), value=None)
                             if fetch.get("success") else gr.update())
        else:
            models_update = gr.update()

        # キー有無で機能可用性(画像生成のGoogleキー条件等)が変わりうる。
        # fetch の後に走らせる: fetch は提供終了imagenの掃除で保存済み
        # 画像生成モデルを解除することがあり(imagen_model_set 条件)、
        # その結果を織り込んでゲートを畳む必要がある。
        try:
            from backend.backend import enforce_feature_availability
            enforce_feature_availability()
        except Exception:
            pass

        status_update = gr.update(value=msg, visible=True)
        llm_update = _character_llm_choices_update()
        if provider == "openai":
            from backend.shared.api_settings import get_openai_stt_model_choices
            return (
                status_update,
                models_update,
                gr.update(choices=get_openai_stt_model_choices()),
                llm_update,
                llm_update,
                _embedding_choices_update(),
            )
        if provider == "google":
            return (
                status_update,
                models_update,
                llm_update,
                llm_update,
                _embedding_choices_update(),
                _imagen_choices_update(),
            )
        return (status_update, models_update, llm_update, llm_update)
    return handler

def _refresh_models_handler(provider: str):
    """Create a model refresh handler for a specific provider."""
    def handler():
        from backend.shared.api_settings import fetch_provider_models
        result = fetch_provider_models(provider)
        models = result.get("models", [])
        msg = result.get("message", "")
        return (
            gr.update(choices=models, value=None),
            gr.update(value=msg, visible=True)
        )
    return handler

def _character_llm_choices_update():
    """gr.update with the current unified (Ollama + API providers) LLM choices.

    Shared by the per-provider model refresh so the character create/edit
    model dropdowns reflect a newly fetched model list immediately
    (no page re-navigation needed) — same pattern as the TTS voices refresh.
    """
    from backend.shared.api_settings import get_all_available_models
    ollama_models = []
    try:
        import backend
        ollama_response = backend.list_ollama_models()
        if isinstance(ollama_response, dict):
            ollama_models = ollama_response.get('result', []) or []
        elif isinstance(ollama_response, list):
            ollama_models = ollama_response
    except Exception:
        pass
    return gr.update(choices=get_all_available_models(ollama_models))

def _refresh_models_and_character_dropdowns(provider: str):
    """Model refresh handler that also re-populates the character
    create/edit model dropdowns with the updated unified list."""
    def handler():
        models_update, status_update = _refresh_models_handler(provider)()
        llm_choices_update = _character_llm_choices_update()
        return (
            models_update,
            status_update,
            llm_choices_update,
            llm_choices_update,
        )
    return handler

def _search_toggle_handler(provider: str, setting_name: str):
    """Create a search toggle handler for a specific provider."""
    def handler(value):
        from backend.shared.api_settings import update_search_setting
        update_search_setting(provider, setting_name, value)
    return handler

def _list_sbv2_models():
    """Current SBV2 model folder names (empty list on any failure)."""
    sbv2_models = []
    try:
        import backend
        tts_response = backend.list_tts_models()
        if isinstance(tts_response, dict):
            sbv2_models = tts_response.get('result', []) or []
        elif isinstance(tts_response, list):
            sbv2_models = tts_response
    except Exception:
        pass
    return sbv2_models

def _character_tts_choices_update(language=None):
    """gr.update with the current mixed TTS choices (SBV2+Kokoro+ElevenLabs).

    Shared by the voices refresh so the character create/edit dropdowns
    reflect a new voice list immediately (no page re-navigation needed).
    language filters the local engines (ja=SBV2/en=Kokoro); None keeps all.
    """
    from backend.shared.api_settings import get_all_tts_choices
    return gr.update(choices=get_all_tts_choices(_list_sbv2_models(), language=language))

def _refresh_elevenlabs_voices(create_lang=None, edit_lang=None):
    """Refresh the user's ElevenLabs account voices (My Voices).

    Also pushes the updated mixed TTS choices into the character create/edit
    dropdowns (they live on the same page and would otherwise stay stale
    until the next page navigation). Each form keeps its own language filter
    (ja=SBV2/en=Kokoro) so the refresh does not undo the filtering.
    """
    from backend.shared.api_settings import (
        fetch_elevenlabs_voices, get_elevenlabs_voice_display_choices,
    )
    result = fetch_elevenlabs_voices()
    msg = result.get("message", "")
    return (
        gr.update(choices=get_elevenlabs_voice_display_choices(), value=None),
        gr.update(value=msg, visible=True),
        _character_tts_choices_update(create_lang),
        _character_tts_choices_update(edit_lang),
    )

def _refresh_openai_models_and_stt():
    """OpenAI models refresh + STT model dropdown update in one step.

    The Audio Settings STT dropdown derives its choices from the fetched
    OpenAI model list (new transcription models appear without a code
    change), so the refresh must update it too. Also re-populates the
    character create/edit model dropdowns (same as the other providers)
    and the embedding dropdown (OpenAI embedding models derive from the
    same fetched list).
    """
    from backend.shared.api_settings import get_openai_stt_model_choices
    models_update, status_update = _refresh_models_handler("openai")()
    llm_choices_update = _character_llm_choices_update()
    return (
        models_update,
        status_update,
        gr.update(choices=get_openai_stt_model_choices()),
        llm_choices_update,
        llm_choices_update,
        _embedding_choices_update(),
    )

def _refresh_google_models_full():
    """Google model refresh + every Google-derived dropdown.

    Same as _refresh_models_and_character_dropdowns("google") plus the
    embedding and image-generation dropdowns, whose choices are filtered
    from the fetched Google model list.
    """
    models_update, status_update = _refresh_models_handler("google")()
    # 更新で保存済み画像生成モデルが解除されうる(提供終了の掃除) —
    # image_generation ゲート(imagen_model_set 条件)を畳み直す
    try:
        from backend.backend import enforce_feature_availability
        enforce_feature_availability()
    except Exception:
        pass
    llm_choices_update = _character_llm_choices_update()
    return (
        models_update,
        status_update,
        llm_choices_update,
        llm_choices_update,
        _embedding_choices_update(),
        _imagen_choices_update(),
    )

def _save_elevenlabs_key_and_refresh(key_value, create_lang=None, edit_lang=None):
    """Save the ElevenLabs key, then fetch voices + TTS models in one event.

    Same rationale as _save_key_and_refresh for the LLM providers: a freshly
    registered key otherwise needs the two manual refresh buttons before any
    voice/model shows up. Clearing the key skips the network fetches but
    still re-populates the derived dropdowns from the stored lists.
    """
    from backend.shared.api_settings import (
        save_api_key, fetch_elevenlabs_voices, fetch_elevenlabs_models,
        get_elevenlabs_voice_display_choices, get_elevenlabs_model_choices,
        get_elevenlabs_model_id,
    )
    result = save_api_key("elevenlabs", key_value)
    msg = result.get("message", "")
    # Re-verdict the status indicator with the new key right away —
    # a stale 🔴/🟢 from the previous key must not linger (best-effort).
    try:
        from ui.status_probe import invalidate
        from backend.shared.ui_events import publish_ui_update
        invalidate("elevenlabs")
        publish_ui_update("update_status", reason="api_key_saved:elevenlabs")
    except Exception:
        pass
    if (key_value or "").strip():
        voices_fetch = fetch_elevenlabs_voices()
        models_fetch = fetch_elevenlabs_models()
        msg += "\n\n" + voices_fetch.get("message", "")
        msg += "\n" + models_fetch.get("message", "")
    try:
        from backend.backend import enforce_feature_availability
        enforce_feature_availability()
    except Exception:
        pass
    model_choices = get_elevenlabs_model_choices()
    current = get_elevenlabs_model_id()
    model_value = current if current in [v for _, v in model_choices] else None
    return (
        gr.update(value=msg, visible=True),
        gr.update(choices=get_elevenlabs_voice_display_choices(), value=None),
        gr.update(choices=model_choices, value=model_value),
        _character_tts_choices_update(create_lang),
        _character_tts_choices_update(edit_lang),
    )

def _refresh_elevenlabs_models():
    """Refresh the ElevenLabs TTS model list (TTS-capable only, cost labels)."""
    from backend.shared.api_settings import (
        fetch_elevenlabs_models, get_elevenlabs_model_choices, get_elevenlabs_model_id,
    )
    result = fetch_elevenlabs_models()
    msg = result.get("message", "")
    choices = get_elevenlabs_model_choices()
    current = get_elevenlabs_model_id()
    value = current if current in [v for _, v in choices] else None
    return (
        gr.update(choices=choices, value=value),
        gr.update(value=msg, visible=True)
    )

def _save_elevenlabs_model(selected):
    """Persist the selected ElevenLabs TTS model id."""
    from backend.shared.api_settings import save_elevenlabs_model_id
    if not selected:
        return gr.update(value=t('hdl.api_settings.no_model_selected'), visible=True)
    result = save_elevenlabs_model_id(selected)
    return gr.update(value=result.get("message", ""), visible=True)
