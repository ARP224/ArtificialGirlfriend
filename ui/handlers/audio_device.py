"""
ui/handlers/audio_device.py

Voice Inputタブ「音声入力設定」のマイクデバイス選択(スタンドアロン時のみ有効)。

サーバーモード中のデスクトップUIの録音はブラウザマイク(ui/app.py の
MediaRecorder経路)のため、ドロップダウン+更新ボタンはグレーアウトされ、
JS専有div(#browser-mic-name)がブラウザの実デバイス名を表示する。

永続化は生デバイス名(display_name は Windows 重複排除のクリーニングで
衝突しうる)。ID は抜き差しで変動するため保存せず、録音開始時に名前→ID を
解決する(audio_input の _configured_input_device)。状態メッセージは
stt-engine-status 行(JS専有・書き手は WS のみ)へ publish=単一書き手を維持。
"""

import logging

import gradio as gr

from backend.shared.i18n import t
from backend.shared.settings_store import get_setting, update_setting

logger = logging.getLogger(__name__)

# ''=システム既定(録音時 device=None)。settings_store の既定値と揃える。
DEFAULT_VALUE = ""


def _publish_status(message_key: str, **fmt) -> None:
    """Push a status-line message over the WS channel (best-effort)."""
    try:
        from backend.shared.ui_events import publish_ui_update
        publish_ui_update("stt_model_status",
                          data={"state": "info", "message": t(message_key, **fmt)})
    except Exception as e:
        logger.debug(f"audio device status publish failed: {e}")


def build_mic_device_choices():
    """(choices, value) — ドロップダウンの内容を実列挙から組み立てる。

    choices は (label=display_name, value=生name) のタプル列。先頭は常に
    「システム既定」で、現在の既定デバイス名を括弧内に埋め込む
    (「システム既定（現在: X）」)。デバイス行に（既定）マーカーは付けない —
    既定表示が2箇所にあると「既定が2つある」ように見える(稜実機 2026-07-25)。
    保存済みデバイスが列挙に無いときは「(未接続)」ラベルで選択肢に残す=
    設定値を勝手に既定へ書き換えない(表示と設定の不一致を作らない)。
    列挙自体の失敗は既定のみの一覧へ劣化。
    """
    saved = get_setting('audio', 'input_device', DEFAULT_VALUE) or DEFAULT_VALUE
    choices = [(t('conv.mic_device_default'), DEFAULT_VALUE)]
    try:
        import audio_input
        devices = audio_input.get_available_devices()
    except Exception as e:
        logger.warning(f"Mic device enumeration failed: {e}")
        devices = []
    seen = False
    default_display = None
    for dev in devices:
        label = dev.get('display_name') or dev['name']
        if dev.get('default'):
            default_display = label
        choices.append((label, dev['name']))
        if dev['name'] == saved:
            seen = True
    if default_display:
        choices[0] = (t('conv.mic_device_default_resolved', name=default_display),
                      DEFAULT_VALUE)
    if saved != DEFAULT_VALUE and not seen:
        choices.append((t('conv.mic_device_disconnected', name=saved), saved))
    return choices, saved


def change_mic_device(value: str) -> None:
    """選択を永続化し、16kHzモノラルで開けるか事前検証(結果は状態行へ)。

    未接続/検証失敗でも保存自体は有効のまま(録音開始時に既定へ劣化する
    フォールバックが audio_input 側にある)。保存失敗は無言で再起動後に
    巻き戻るため必ず可視化(実機 2026-07-17 と同型)。
    """
    value = value if value is not None else DEFAULT_VALUE
    if not update_setting('audio', 'input_device', value):
        _publish_status('hdl.audio_device.save_failed')
        return
    if value == DEFAULT_VALUE:
        _publish_status('hdl.audio_device.saved_default')
        return
    # 事前検証は録音時と同一条件(WASAPI auto_convert 込み)を audio_input に
    # 一元化 — 「WASAPIは16kHzを直接受けない」の知識をここへ複製しない。
    import audio_input
    result = audio_input.precheck_input_device(value)
    if result == 'not_connected':
        _publish_status('hdl.audio_device.saved_not_connected', name=value)
    elif result == 'check_failed':
        _publish_status('hdl.audio_device.saved_check_failed', name=value)
    else:
        _publish_status('hdl.audio_device.saved_ok', name=value)


def browser_mic_name_html() -> str:
    """サーバーモード表示用の JS 専有 div(#browser-mic-name)。

    文言はビルド時に data 属性へ埋め込み(t() はここで解決・JS は dataset を
    読むだけ)。中身の書き手は JS のみ、書き手は3箇所で同じ規約:
    ①ページロード時(ws_client_js)=enumerateDevices の既定エントリで表示
    (権限未許可でラベルが取れない間は pending 文言)
    ②会話開始クリック(app.py)=getUserMedia 実ストリームのトラックラベル
    ③🔄ボタン(app.py BROWSER_MIC_REFRESH_JS)=掴み直し/再列挙で更新。
    空 div は高さ0=スタンドアロン時は何も見えない。
    """
    import html as _html
    return (
        "<div id='browser-mic-name' data-prefix=\"{}\" data-denied=\"{}\""
        " data-pending=\"{}\"></div>"
        .format(_html.escape(t('js.mic.prefix'), quote=True),
                _html.escape(t('js.mic.denied'), quote=True),
                _html.escape(t('js.mic.pending'), quote=True))
    )


def refresh_mic_devices() -> gr.update:
    """デバイス表を再初期化してから再列挙する(選択は保存値を維持)。

    PortAudio の表はプロセス内で凍結され抜き差しが反映されないため、
    再列挙だけの🔄は嘘の一覧になる(実障害 2026-07-25)。録音中は
    再初期化不可(audio_input 側ガード)=凍結表のままの列挙へ劣化。
    """
    import audio_input
    audio_input.refresh_device_table()
    choices, value = build_mic_device_choices()
    _publish_status('hdl.audio_device.refreshed', count=max(0, len(choices) - 1))
    return gr.update(choices=choices, value=value)
