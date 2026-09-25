"""backend/shared/wake_gate.py — 覚醒ゲート(スリープ後の無人覚醒で自律発火を止める)。

なぜ必要か:
    macOS はスリープ中に DarkWake(画面消灯のままの裏起床)で断続的に本当に起動する
    (M1 実測 2026-07-31: womp=0 でも powernap/tcpkeepalive 駆動で7日間307件・平均488秒/回)。
    その間は機械が物理的に覚醒しているため awake_clock は正当に進み、蓋を閉じた一晩で
    ELYTH/YouTube のインターバルが満了して無人のままセッションが実走し API を浪費する。
    Windows も自動メンテナンス等の無人覚醒(wake timer)で同型の構造を持つ。
    awake_clock は壊れていない — 欠けているのは「覚醒」の中の区別
    (ユーザーに見える覚醒か・無人の裏覚醒か)で、本モジュールがそれを供給する。

仕組み(2026-08-01 Mac 実機検証で確定):
    1. スリープ検知 = 2時計ギャップ。呼び出しごとに(壁時計, awake)ペアを採取し、
       前回採取との差分「壁の進み − awake の進み」が閾値を超えたら「スリープを跨いだ」
       としてゲートを閉じる。両時計はプロセスのストール(GC/高負荷)中は等しく進むため
       誤検出せず、NTP ステップ補正等の壁時計ジャンプは安全側(一時停止)に倒れるだけ。
    2. 再開判定 = ユーザー入力の照会。閉状態でのみ OS へ「最終ユーザー入力からの
       経過秒(idle)」を問い、ギャップ以降に入力があればゲートを開く。入力はイベント
       リスナーでなく許可不要のシステム照会で取る(macOS の入力監視 TCC を要求しない):
         Windows = GetLastInputInfo(ctypes) / macOS = IOHIDSystem HIDIdleTime(ioreg)。

再開判定式(実測に基づく・壁時計は使わない):
    無入力なら idle_now ≈ idle_at_gap + awake経過 が成り立つ(macOS の HIDIdleTime は
    スリープ除外カウントと実測 2026-08-01: 16分不在で idle=9.2 秒)。したがって
        idle_now + MARGIN < idle_at_gap + (awake経過 since gap)
    が成立した時のみ「ギャップ以降に入力があった」。壁時計経過と比較すると
    復帰直後の無入力でも「入力あり」と誤認する(idle が覚醒時間分しか進まないため)。
    Windows の GetLastInputInfo 基準(GetTickCount)はスリープ込みで進む逆の意味論だが、
    その場合 idle_now は右辺以上に速く育つだけで判定方向は変わらない(両意味論で正)。

接地事実(M1/macOS 26・2026-08-01 プローブ実測):
    - HIDIdleTime は実入力(マウス/キー/ロック解除)で即リセット・単位 ns
    - enet 起因 DarkWake 窓58秒の間、裏活動ではリセットされず単調増加
    - 未証明1点: Bluetooth 起因 DarkWake でのリセット有無(観測0件。もし BT 再接続が
      リセットするなら誤再開しうるが、反証は出ていない)

フェイルオープン:
    idle 照会不能・例外時は常に「開(発火許可)」へ倒す。本機構の故障で
    セッション機能自体が止まってはならない。起動直後(採取履歴なし)も常に開。
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from backend.shared.awake_clock import awake_seconds

logger = logging.getLogger(__name__)

# 連続採取間の (壁の進み − awakeの進み) がこれを超えたらスリープ跨ぎと判定。
# NTP のスルー補正は採取間隔あたり数十 ms オーダーで届かない。
GAP_THRESHOLD_SECONDS = 3.0
# 再開判定のマージン(idle 照会のレイテンシ+時計の微小ズレ吸収)
RESUME_MARGIN_SECONDS = 5.0


def _wall_seconds() -> float:
    """壁時計(スリープ込みで進む方)。テストはこの名前を patch する。"""
    return time.time()


def _query_idle_seconds() -> Optional[float]:
    """最終ユーザー入力からの経過秒。取得不能なら None(=フェイルオープン)。"""
    try:
        if sys.platform == "win32":
            return _query_idle_windows()
        if sys.platform == "darwin":
            return _query_idle_darwin()
    except Exception as e:
        logger.debug(f"[WakeGate] idle query failed: {e}")
    return None


def _query_idle_windows() -> Optional[float]:
    """GetLastInputInfo ベースの idle 秒(セッションローカル・許可不要)。

    基準の GetTickCount は DWORD(32bit)で49.7日でラップするため、差分は
    DWORD 算術(mod 2^32)で取る。なお GetTickCount はスリープ込みで進む
    (biased)ため idle もスリープを含む — 再開判定式はこの意味論でも正しい
    (モジュール docstring 参照)。
    """
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    now_ticks = ctypes.windll.kernel32.GetTickCount()
    idle_ms = (now_ticks - lii.dwTime) & 0xFFFFFFFF
    return idle_ms / 1000.0


def _query_idle_darwin() -> Optional[float]:
    """IOHIDSystem の HIDIdleTime(ns)を ioreg で照会(許可不要・pyobjc 不要)。

    スリープ除外カウント(M1 実測 2026-08-01)。BT 起因 DarkWake での
    リセット有無のみ未証明(モジュール docstring 参照)。
    """
    out = subprocess.run(
        ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
        capture_output=True, text=True, timeout=2,
    )
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out.stdout)
    if not m:
        return None
    return int(m.group(1)) / 1e9


class WakeGate:
    """スリープ検知でゲートを閉じ、ユーザー入力で開ける状態機械。

    消費者(ELYTH/YouTube スケジューラ tick・AutoPrompt 発火)が is_open() を
    呼ぶこと自体が採取=検知を兼ねる(専用スレッドなし)。ギャップは累積量なので
    採取間隔に依存せず、発火直前の呼び出しで必ず最新判定になる
    (=監視スレッド方式にあった「最初の窓の競合」が構造的に無い)。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_wall: Optional[float] = None
        self._last_awake: Optional[float] = None
        self._suspended = False
        self._idle_at_gap = 0.0
        self._awake_at_gap = 0.0
        # 最後に「開」へ遷移した awake 時刻。AutoPrompt が復帰後にフル duration の
        # 沈黙を要求し直すための基準(復帰直後発火の防止)。
        self.last_resume_awake: Optional[float] = None

    def is_open(self) -> bool:
        """発火してよければ True。故障時は True(フェイルオープン)。"""
        try:
            with self._lock:
                return self._check_locked()
        except Exception as e:
            logger.warning(f"[WakeGate] check failed (fail-open): {e}")
            return True

    def notify_user_activity(self) -> None:
        """会話開始など「ユーザー在席の確実な証拠」による即時再開。

        ギャップ検出より前に復帰入力が済んでいた場合(idle_at_gap が入力後の
        小さい値で基準化され、以後の入力まで式が割れない)の脱出口。
        リモート(モバイル)操作ユーザーもこれで再開できる。
        """
        try:
            with self._lock:
                if self._suspended:
                    self._resume_locked(awake_seconds(), "user activity signal")
        except Exception:
            pass

    # ----- internal ---------------------------------------------------------

    def _check_locked(self) -> bool:
        now_wall = _wall_seconds()
        now_awake = awake_seconds()

        if self._last_wall is not None and self._last_awake is not None:
            gap = (now_wall - self._last_wall) - (now_awake - self._last_awake)
            # 負方向(時計の巻き戻し)は無視。閾値超え=スリープ跨ぎ。
            if gap > GAP_THRESHOLD_SECONDS:
                idle = _query_idle_seconds()
                if idle is not None:
                    if not self._suspended:
                        logger.info(
                            f"[WakeGate] Sleep gap detected ({gap:.1f}s) — "
                            f"autonomous firing suspended until user input"
                        )
                    # 停止中の再ギャップは基準を最新へ更新(式は旧基準でも正だが
                    # 新基準の方が意味が素直)
                    self._suspended = True
                    self._idle_at_gap = idle
                    self._awake_at_gap = now_awake
                # idle 照会不能なら閉じない(フェイルオープン)

        self._last_wall = now_wall
        self._last_awake = now_awake

        if not self._suspended:
            return True

        idle = _query_idle_seconds()
        if idle is None:
            self._resume_locked(now_awake, "idle query unavailable (fail-open)")
            return True
        awake_elapsed = now_awake - self._awake_at_gap
        if idle + RESUME_MARGIN_SECONDS < self._idle_at_gap + awake_elapsed:
            self._resume_locked(now_awake, "user input detected")
            return True
        return False

    def _resume_locked(self, now_awake: float, reason: str) -> None:
        self._suspended = False
        self.last_resume_awake = now_awake
        logger.info(f"[WakeGate] Resumed ({reason}) — autonomous firing re-enabled")


_gate = WakeGate()


def get_wake_gate() -> WakeGate:
    """プロセス共有のシングルトン。テストは WakeGate() を直接生成する。"""
    return _gate
