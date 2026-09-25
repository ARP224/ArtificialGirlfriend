"""Pure utility-panel HTML builder for the desktop UI.

Extracted verbatim from ui/app.py (B11p). _build_utility_panel_html is a
true leaf: parameters (feature-toggle booleans) -> HTML string, with no
app_state / module-global dependency. Lowered here so both app.py (utility
panel default render) and ui/handlers/lifecycle.py (initial_load_with_cleanup)
can downward-import it without an upward app.py import.
"""

import html as _html

from backend.shared.i18n import t
from backend.shared.platform_caps import is_feature_supported


def _avail_attrs(reason):
    """前提条件つき機能のボタン装飾 (disabled, opacity, title)。

    reason(ローカライズ済みブロック理由)がある場合は常に disabled+グレー+
    tooltip。利用不能になった機能はバックエンドが強制OFFする
    (enforce_feature_availability)前提の表示規則(稜裁定 2026-07-25)。
    無効表現はグレー+disabled+tooltip の1形式のみ(横線は廃止=稜裁定
    2026-08-15。従属無効(親トグルOFF)も同じ見た目で tooltip の文言だけが違う)。
    """
    if not reason:
        return '', '1', ''
    title = f' title="{_html.escape(reason, quote=True)}"'
    return ' disabled', '0.5', title


def _build_utility_panel_html(pc_status=False, screen_capture=False,
                              talk_theme=True, speechless=False,
                              command_execution=False, notes=False,
                              image_generation=False,
                              camera_capture=False,
                              ambient_camera=False,
                              deep_search=False,
                              elyth=False,
                              availability=None,
                              server_mode=False) -> str:
    """Build Utility Panel HTML with dynamic feature toggle states.

    Generates the same HTML structure as the hardcoded panel, but with
    button colors and labels derived from the actual backend state.
    Default parameter values match the original hardcoded defaults.

    availability: {feature: ローカライズ済みブロック理由 | None}(フールプルーフ。
    None/未指定は「情報なし=グレーなし」。動的な更新はWS→JSの
    feature_availability が担い、ここは初期描画の静的適用のみ)。
    server_mode: True ならホストPC側機能(PC Status / Screen Capture /
    Command Execution)をグレーアウト(クライアントからサーバーPCの画面情報を
    使う意味がない=稜裁定 2026-07-31。platform_unsupported と同型)。
    """
    av = availability or {}
    sm_title = f' title="{t("utility.server_mode_unsupported")}"' if server_mode else ''
    tt_bg = '#4caf50' if talk_theme else '#607d8b'
    tt_label = t('utility.on') if talk_theme else t('utility.off')
    pc_bg = '#4caf50' if pc_status else '#607d8b'
    pc_label = t('utility.on') if pc_status else t('utility.off')
    pc_opacity = '0.5' if server_mode else '1'
    pc_disabled = ' disabled' if server_mode else ''
    sc_bg = '#4caf50' if screen_capture else '#607d8b'
    sc_label = t('utility.on') if screen_capture else t('utility.off')
    # Screen Capture は従属無効(PC Status OFF)+サーバーモード+可用性ブロック
    # (Ollama等)の3条件合成(Live Camera <- Camera と同型)
    sc_av_disabled, _sc_av_opacity, sc_av_title = _avail_attrs(av.get('screen_capture'))
    _sc_blocked = (not pc_status) or server_mode or bool(sc_av_disabled)
    sc_opacity = '0.5' if _sc_blocked else '1'
    sc_disabled = ' disabled' if _sc_blocked else ''
    # tooltip の優先順位: サーバーモード > 利用不能理由 > 従属無効(PC Status OFF)
    if server_mode:
        sc_title = sm_title
    elif sc_av_disabled:
        sc_title = sc_av_title
    elif not pc_status:
        sc_title = f' title="{_html.escape(t("utility.needs_pc_status"), quote=True)}"'
    else:
        sc_title = ''
    sl_bg = '#4caf50' if speechless else '#607d8b'
    sl_label = t('utility.on') if speechless else t('utility.off')
    ce_bg = '#4caf50' if command_execution else '#607d8b'
    ce_label = t('utility.on') if command_execution else t('utility.off')
    # Mac 3-6 layer 1: platform-unsupported feature is greyed out at build
    # time (disabled + tooltip; Screen Capture <- PC Status dependent-disable
    # precedent). Server mode blocks the same way, and the availability block
    # (Ollama etc.) composes as the third condition (platform reason wins the
    # tooltip, then server mode, then availability).
    ce_supported = is_feature_supported('command_execution')
    ce_av_disabled, _ce_av_opacity, ce_av_title = _avail_attrs(av.get('command_execution'))
    ce_usable = ce_supported and not server_mode and not ce_av_disabled
    ce_opacity = '1' if ce_usable else '0.5'
    ce_disabled = '' if ce_usable else ' disabled'
    if ce_usable:
        ce_title = ''
    elif not ce_supported:
        ce_title = f' title="{t("utility.platform_unsupported")}"'
    elif server_mode:
        ce_title = sm_title
    else:
        ce_title = ce_av_title
    nt_bg = '#4caf50' if notes else '#607d8b'
    nt_label = t('utility.on') if notes else t('utility.off')
    nt_disabled, nt_opacity, nt_title = _avail_attrs(av.get('notes'))
    ig_bg = '#4caf50' if image_generation else '#607d8b'
    ig_label = t('utility.on') if image_generation else t('utility.off')
    ig_disabled, ig_opacity, ig_title = _avail_attrs(av.get('image_generation'))
    cc_bg = '#4caf50' if camera_capture else '#607d8b'
    cc_label = t('utility.on') if camera_capture else t('utility.off')
    cc_disabled, cc_opacity, cc_title = _avail_attrs(av.get('camera_capture'))
    # Live Camera depends on Camera (same shape as Screen Capture <- PC Status)
    # + 可用性ブロック(どちらかが立てば無効)
    amb_bg = '#4caf50' if ambient_camera else '#607d8b'
    amb_label = t('utility.on') if ambient_camera else t('utility.off')
    amb_av_disabled, _amb_av_opacity, amb_av_title = _avail_attrs(av.get('ambient_camera'))
    _amb_blocked = (not camera_capture) or bool(amb_av_disabled)
    amb_opacity = '0.5' if _amb_blocked else '1'
    amb_disabled = ' disabled' if _amb_blocked else ''
    # tooltip の優先順位: 利用不能理由 > 従属無効(Camera OFF)
    if amb_av_disabled:
        amb_title = amb_av_title
    elif not camera_capture:
        amb_title = f' title="{_html.escape(t("utility.needs_camera"), quote=True)}"'
    else:
        amb_title = ''
    ds_bg = '#4caf50' if deep_search else '#607d8b'
    ds_label = t('utility.on') if deep_search else t('utility.off')
    ds_disabled, ds_opacity, ds_title = _avail_attrs(av.get('deep_search'))
    el_bg = '#4caf50' if elyth else '#607d8b'
    el_label = t('utility.on') if elyth else t('utility.off')
    el_disabled, el_opacity, el_title = _avail_attrs(av.get('elyth'))
    # Appear: Motionフォルダ未設定/実体なし/キャラ未選択でグレーアウト
    # (疑似機能 motion_appear・稜GO 2026-08-15)。初期描画時は必ず非表示中
    # (JSのmotionPngTuberActiveはロード時false)なのでDisappearモードとの
    # 衝突はない。動的な再適用はJS側 updateFeatureToggleButtons が担う。
    # ブロック中は背景もグレー(紫のままだと目立つ=稜指摘 2026-08-15。
    # トグルOFF色 #607d8b と同系)
    ap_disabled, ap_opacity, ap_title = _avail_attrs(av.get('motion_appear'))
    ap_bg = '#607d8b' if ap_disabled else '#9c27b0'
    return f"""<div style="text-align: center; padding: 8px;">
    <style>
        #audio-btn-row, #feature-btn-row, #speechless-btn-row, #command-btn-row, #notes-btn-row, #camera-btn-row, #elyth-btn-row {{
            display: flex !important;
            gap: 6px !important;
            width: 100% !important;
        }}
        #audio-btn-row > div, #feature-btn-row > div, #speechless-btn-row > div, #command-btn-row > div, #notes-btn-row > div, #camera-btn-row > div, #elyth-btn-row > div {{
            flex: 1 1 0px !important;
            min-width: 0 !important;
        }}
        #audio-btn-row button, #feature-btn-row button, #speechless-btn-row button, #command-btn-row button, #notes-btn-row button, #camera-btn-row button, #elyth-btn-row button {{
            width: 100% !important;
            box-sizing: border-box !important;
        }}
        #audio-btn-row button:disabled, #feature-btn-row button:disabled, #speechless-btn-row button:disabled, #command-btn-row button:disabled, #notes-btn-row button:disabled, #camera-btn-row button:disabled, #elyth-btn-row button:disabled {{
            cursor: not-allowed !important;
        }}
    </style>
    <div id="audio-btn-row">
        <div><button id="talk-theme-toggle-btn"
            onclick="window.toggleTalkTheme && window.toggleTalkTheme()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {tt_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; text-align: center;">
            {t('utility.talk_theme', state=tt_label)}
        </button></div>
        <div><button id="appear-btn"
            onclick="window.appearCharacter && window.appearCharacter()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {ap_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {ap_opacity};
                   text-align: center;"{ap_disabled}{ap_title}>
            {t('utility.appear')}
        </button></div>
    </div>
    <span id="appear-status" style="font-size: 11px; color: #888; display: none;"></span>
    <div id="feature-btn-row" style="margin-top: 4px;">
        <div><button id="pc-status-toggle-btn"
            onclick="window.togglePcStatus && window.togglePcStatus()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {pc_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {pc_opacity};
                   text-align: center;"{pc_disabled}{sm_title}>
            {t('utility.pc_status', state=pc_label)}
        </button></div>
        <div><button id="screen-capture-toggle-btn"
            onclick="window.toggleScreenCapture && window.toggleScreenCapture()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {sc_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {sc_opacity};
                   text-align: center;"{sc_disabled}{sc_title}>
            {t('utility.screen_capture', state=sc_label)}
        </button></div>
    </div>
    <div id="speechless-btn-row" style="margin-top: 4px;">
        <div><button id="speechless-toggle-btn"
            onclick="window.toggleSpeechless && window.toggleSpeechless()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {sl_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; text-align: center;">
            {t('utility.speechless', state=sl_label)}
        </button></div>
        <div><button id="command-execution-toggle-btn"
            onclick="window.toggleCommandExecution && window.toggleCommandExecution()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {ce_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {ce_opacity};
                   text-align: center;"{ce_disabled}{ce_title}>
            {t('utility.command', state=ce_label)}
        </button></div>
    </div>
    <div id="notes-btn-row" style="margin-top: 4px;">
        <div><button id="notes-toggle-btn"
            onclick="window.toggleNotes && window.toggleNotes()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {nt_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {nt_opacity};
                   text-align: center;"{nt_disabled}{nt_title}>
            {t('utility.notes', state=nt_label)}
        </button></div>
        <div><button id="image-gen-toggle-btn"
            onclick="window.toggleImageGeneration && window.toggleImageGeneration()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {ig_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {ig_opacity};
                   text-align: center;"{ig_disabled}{ig_title}>
            {t('utility.imagegen', state=ig_label)}
        </button></div>
    </div>
    <div id="camera-btn-row" style="margin-top: 4px;">
        <div><button id="camera-capture-toggle-btn"
            onclick="window.toggleCameraCapture && window.toggleCameraCapture()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {cc_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {cc_opacity};
                   text-align: center;"{cc_disabled}{cc_title}>
            {t('utility.camera', state=cc_label)}
        </button></div>
        <div><button id="ambient-camera-toggle-btn"
            onclick="window.toggleAmbientCamera && window.toggleAmbientCamera()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {amb_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {amb_opacity};
                   text-align: center;"{amb_disabled}{amb_title}>
            {t('utility.ambient', state=amb_label)}
        </button></div>
    </div>
    <div id="elyth-btn-row" style="margin-top: 4px;">
        <div><button id="elyth-toggle-btn"
            onclick="window.toggleElyth && window.toggleElyth()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {el_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {el_opacity};
                   text-align: center;"{el_disabled}{el_title}>
            {t('utility.elyth', state=el_label)}
        </button></div>
        <div><button id="deep-search-toggle-btn"
            onclick="window.toggleDeepSearch && window.toggleDeepSearch()"
            style="padding: 5px 0; font-size: 12px;
                   background-color: {ds_bg}; color: white; border: none;
                   border-radius: 4px; cursor: pointer; opacity: {ds_opacity};
                   text-align: center;"{ds_disabled}{ds_title}>
            {t('utility.deepsearch', state=ds_label)}
        </button></div>
    </div>
</div>"""


def build_camera_device_row_html() -> str:
    """Camera device row for the sidebar — its own fenced block between the
    Utility Panel (Function Calling) and Talk Theme Settings (稜裁定
    2026-07-16). One line: device select + status. Behaviour lives in
    ws_client_js (inline onchange survives re-renders); living outside the
    utility-panel HTML also means initial_load re-renders cannot wipe the
    populated device options.
    """
    return f"""<div id="ambient-cam-row" style="display:flex; gap:6px; margin:4px 6px;
            align-items:center;">
        <select id="ambient-cam-device" disabled
            onchange="window.ambientCamSetDevice && window.ambientCamSetDevice(this.value)"
            style="flex:1 1 0; min-width:120px; padding:3px 4px; border-radius:4px;
                   font-size:11px; background:#12122a; color:#eceff1;
                   border:1px solid #3a3a5a; text-align:center; text-align-last:center;">
            <option value="">{t('ambcam.device_placeholder')}</option>
        </select>
        <span id="ambient-cam-status"
              style="flex:0 0 auto; max-width:calc(100% - 126px); overflow:hidden;
                     text-overflow:ellipsis; text-align:center; font-size:11px;
                     white-space:nowrap;">{t('ambcam.state_off')}</span>
    </div>"""
