"""
ui/handlers/ollama_vram.py

Ollama VRAMライフサイクル: 会話開始で温める(warm)・会話終了で返す(release)。
稜裁定 2026-08-01「終了で返す・開始で温めるのセット」。

- warm: 初回送信の「埋め込みロード→チャットロード」直列コールドスタート
  (qwen3:14b実測22.8秒+埋め込み)を、開始確定後のバックグラウンドロードで
  先払いする。チャットは本番と同じ num_ctx でロードすること(ctx違いだと
  本番初回で再ロードが走り無意味 — warm_ollama_model の docstring 参照)。
- release: Endボタン=ユーザーの「もう使わない」宣言でVRAMを返す。ただし
  ターン完了直後は記憶保存(埋め込み)・記憶抽出(チャット+埋め込み)の背景
  タスクが数分生き残るため、backend.has_pending_memory_tasks() の
  立ち下がりを待ってから解放する。待機中に会話が再開されたら中止。
  上限到達時も諦める(keep_alive 30分が後始末する)。アンロードはベスト
  エフォート(連続アンロード直後の空振りを実測済み・失敗はログのみ)。

配置がUI層である理由: backend側 stop_conversation() はキャラ切替・
サーバーモードのセッションタイムアウト・アプリ終了からも呼ばれ、特に
キャラ切替では「アンロード→即再ロード」の無駄が出る。Endボタン経路
(デスクトップ/モバイル共通の toggle_start_end)だけを捕まえるため、
stt_engine.warm_local_stt_model と同型の薄いUIハンドラに置く。Never raises。
"""

import logging
import threading
import time

from ..state import app_state

logger = logging.getLogger(__name__)

# 二重起動ガード(audio_input.start_model_load の正準形: ロック内でフラグ
# 判定→ロック外でスレッド起動)。warm/release は互いに独立の1本ずつ。
_thread_lock = threading.Lock()
_warm_thread = None
_release_thread = None

# 抽出はLLM往復込みで数分ありうる。上限後はアンロードを諦める
# (30分keep_aliveが自然解放する)。
_RELEASE_WAIT_LIMIT_SECONDS = 600
_RELEASE_POLL_INTERVAL_SECONDS = 1.0


def _resolve_ollama_targets():
    """provider=="ollama" のロード対象を [('embedding'|'chat', model), ...] で返す。

    埋め込みが先: 本番ターンの処理順(記憶検索=埋め込み→生成=チャット)に
    合わせ、VRAM逼迫で追い出しが起きる場合も後にロードしたチャット側を
    生存させる。チャットがAPIプロバイダで埋め込みだけollama、の混在構成も
    正規に有り得るため個別に判定する。
    """
    targets = []
    try:
        from backend.shared.api_settings import get_embedding_model
        emb = get_embedding_model()
        if emb and emb[0] == "ollama" and emb[1]:
            targets.append(("embedding", emb[1]))
    except Exception as e:
        logger.debug(f"Embedding model resolve failed: {e}")
    try:
        char_id = app_state.active_character_id
        if char_id:
            import backend
            response = backend.load_character_config(char_id)
            # load_character_config は {'success','result'} ラップ形と生dict形の
            # 両方を返しうる(ui/status_checker._active_llm_provider と同形)
            if isinstance(response, dict) and 'result' in response:
                config = response.get('result') or {}
            else:
                config = response or {}
            if config.get("model_provider") == "ollama":
                name = config.get("model_name") or config.get("ollama_model_name")
                if name:
                    targets.append(("chat", name))
    except Exception as e:
        logger.debug(f"Chat model resolve failed: {e}")
    return targets


def _warm_worker(targets):
    from backend.llm.ollama_integration import (
        call_ollama_embedding, check_model_cpu_offload, warm_ollama_model,
    )
    from backend.llm.ollama_capabilities import get_effective_num_ctx
    for kind, model in targets:
        try:
            if kind == "embedding":
                # embedding専用モデルは /api/generate でロードできない
                # (400実測)ため実呼び出しで温める。1文字なら計算コストは無視可
                result = call_ollama_embedding(model, " ")
            else:
                result = warm_ollama_model(
                    model, num_ctx=get_effective_num_ctx(model))
                if result.get("success"):
                    # num_ctx増でVRAMに収まらない場合は「静かに遅く」なるだけ
                    # なので、ここで唯一の痕跡(WARN)を残す(C4.6)
                    check_model_cpu_offload(model)
            if result.get("success"):
                logger.info(f"Ollama warm done: {kind} '{model}'")
            else:
                logger.warning(
                    f"Ollama warm failed: {kind} '{model}': "
                    f"{result.get('error', 'unknown')}")
        except Exception as e:
            logger.warning(f"Ollama warm failed: {kind} '{model}': {e}")


def warm_ollama_models() -> None:
    """会話開始時に呼ぶ(toggle_start_end の開始確定後)。Never raises・non-blocking。"""
    global _warm_thread
    try:
        targets = _resolve_ollama_targets()
        if not targets:
            return
        with _thread_lock:
            if _warm_thread is not None and _warm_thread.is_alive():
                return
            _warm_thread = threading.Thread(
                target=_warm_worker, args=(targets,),
                name="ollama-warm", daemon=True)
        _warm_thread.start()
    except Exception as e:
        logger.warning(f"Could not start Ollama warmup: {e}")


def _release_worker(models):
    import backend
    from backend.llm.ollama_integration import unload_ollama_model
    deadline = time.monotonic() + _RELEASE_WAIT_LIMIT_SECONDS
    while True:
        if app_state.conversation_started:
            logger.info("Ollama release aborted: conversation restarted")
            return
        try:
            pending = backend.has_pending_memory_tasks()
        except Exception as e:
            logger.warning(f"Ollama release: pending check failed: {e}")
            return
        if not pending:
            break
        if time.monotonic() >= deadline:
            # 抽出が長引いている=モデルはまだ使用中。ここで無理に外すと
            # 直後に再ロードされるだけなので keep_alive の自然解放に任せる
            logger.info(
                "Ollama release skipped: memory tasks still running after "
                f"{_RELEASE_WAIT_LIMIT_SECONDS}s (keep_alive will reclaim)")
            return
        time.sleep(_RELEASE_POLL_INTERVAL_SECONDS)
    if app_state.conversation_started:
        logger.info("Ollama release aborted: conversation restarted")
        return
    for model in dict.fromkeys(models):
        try:
            result = unload_ollama_model(model)
            if result.get("success"):
                logger.info(f"Ollama model '{model}' unloaded (conversation ended)")
            else:
                logger.warning(
                    f"Ollama unload failed for '{model}': "
                    f"{result.get('error', 'unknown')}")
        except Exception as e:
            logger.warning(f"Ollama unload failed for '{model}': {e}")


def release_ollama_models() -> None:
    """会話終了時に呼ぶ(toggle_start_end のEnd分岐)。Never raises・non-blocking。"""
    global _release_thread
    try:
        targets = _resolve_ollama_targets()
        if not targets:
            return
        models = [m for _, m in targets]
        with _thread_lock:
            if _release_thread is not None and _release_thread.is_alive():
                return
            _release_thread = threading.Thread(
                target=_release_worker, args=(models,),
                name="ollama-release", daemon=True)
        _release_thread.start()
    except Exception as e:
        logger.warning(f"Could not start Ollama release: {e}")
