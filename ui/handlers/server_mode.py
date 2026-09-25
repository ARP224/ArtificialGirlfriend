"""
ui/handlers/server_mode.py

System ページ「サーバーモード」ボタンのハンドラ本体(ジェネレータ)。
prepare_switch_to_server を別スレッドで走らせ、tailscale.py が注入
コールバック経由で通知する証明書取得の試行イベントをキューで受けて、
ステータス欄へ順次 yield する(進捗表示・2026-08-05 稜承認)。
ステータス欄の書き手は Gradio(fn)専有のまま=書き手一人原則を維持。
再起動(_execute_shutdown)はプロセス生命期を持つ合成根(app.py)の所有物の
ため、配線時に execute_shutdown として注入される(公認の継ぎ目=注入)。
"""

import logging
import queue
import threading

import backend
from backend.shared.i18n import t

logger = logging.getLogger(__name__)

# 色の言語(2026-08-03 稜裁定: warning=黄 #ff9800・error=赤 と整合):
# 進行中=中立 / リトライ待ち=黄(未確定の失敗) / 確定失敗=赤 / 成功=緑
_NEUTRAL_STYLE = "color:#b0bec5;padding:15px;border-radius:8px;background:#1a1f26;"
_WARN_STYLE = "color:#ff9800;padding:15px;border-radius:8px;background:#2a2115;"
_ERROR_STYLE = "color:#f44336;padding:15px;border-radius:8px;background:#2a1515;"
_SUCCESS_STYLE = "color:#4caf50;padding:15px;border-radius:8px;background:#1a2a1a;"

_DONE = object()


def _progress_html(ev: dict) -> str:
    """CertProgress イベント(dict)をステータス欄HTMLへ。未知イベントは無視。"""
    try:
        phase = ev.get("phase")
        if phase == "attempt":
            return (f"<div style='{_NEUTRAL_STYLE}'>"
                    f"{t('system.switch_cert_attempt', attempt=ev['attempt'], total=ev['total'])}</div>")
        if phase == "retry_wait":
            # 秒数はCSSカウンターアニメーション(.ag-countdown)が5→1へ実時間で
            # 減らす(app.py の system_css + CSS.registerProperty と対)。
            countdown = "<span class='ag-countdown'></span>"
            return (f"<div style='{_WARN_STYLE}'>"
                    f"{t('system.switch_cert_retry_wait', attempt=ev['attempt'], total=ev['total'], countdown=countdown)}</div>")
    except Exception as e:
        logger.debug(f"progress event render failed: {e}")
    return ""


def make_switch_to_server_handler(execute_shutdown):
    """合成根が _execute_shutdown を注入してハンドラを得るファクトリ。"""

    def handle_switch_to_server_mode():
        # 記憶タスク中ガード(抽出/relationship 更新中): Exit/Restart・admin の
        # 「ローカルへ切替」・トレイ経由と同じ防御。従来このボタンだけ無ガードで、
        # 抽出中に押すと即再起動が走り抽出を殺した(2026-08-16 Mac 実機)。
        # 黄=未確定の待機。ボタンは .then の js が .shutdown-initiated 不在を
        # 見て元に戻す(通常はグレーアウト済みで押せない=安全弁)。
        if backend.is_memory_task_running():
            yield f"<div style='{_WARN_STYLE}'>{t('system.switch_blocked_extracting')}</div>"
            return
        # 最初の yield は即時(Tailscale プローブ帯の無言をなくす)
        yield f"<div style='{_NEUTRAL_STYLE}'>{t('system.switch_cert_preparing')}</div>"
        try:
            from backend.server.mode_switch import prepare_switch_to_server
            events: queue.Queue = queue.Queue()
            result = {}

            def _worker():
                try:
                    result["v"] = prepare_switch_to_server(progress=events.put)
                except Exception as we:
                    result["e"] = we
                finally:
                    events.put(_DONE)

            threading.Thread(
                target=_worker, name="switch-to-server-prepare", daemon=True
            ).start()
            while True:
                ev = events.get()
                if ev is _DONE:
                    break
                html = _progress_html(ev)
                if html:
                    yield html
            if "e" in result:
                raise result["e"]
            ok, err = result["v"]
            if not ok:
                yield f"<div style='{_ERROR_STYLE}'>{err}</div>"
                return
            threading.Thread(
                target=execute_shutdown,
                args=(True,),
                name="switch-to-server",
                daemon=False,
            ).start()
            yield (f"<div class='shutdown-initiated' style='{_SUCCESS_STYLE}'>"
                   f"{t('system.switch_to_server_progress')}</div>")
        except Exception as e:
            yield f"<div style='{_ERROR_STYLE}'>{t('common.error_with', error=e)}</div>"

    return handle_switch_to_server_mode
