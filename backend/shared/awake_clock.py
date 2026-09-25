"""Awake（起床）時計ヘルパー — PCスリープ耐性の中核部品。

経過時間の差分比較に使うための、スリープ/休止時間を「数えない」単調時計を提供する。

なぜ必要か:
    Windowsの `time.time()` も `time.monotonic()`(=GetTickCount64, biased) も
    S3スリープ中に経過した実時間を「含んで」進む。そのため、これらの差分で
    「アイドル経過」「サイクル間隔」を判定すると、スリープ復帰直後に差分が一気に
    跳ね上がり、ELYTH自律セッションや自動発話が暴発する。

    `QueryUnbiasedInterruptTime`(Windows) と `time.monotonic()`(Linux/macOS) は
    スリープ/サスペンド時間を「除外」する。これを使えば「起きている時間だけ」で
    判定でき、「スリープは無かったこと」になる。

実機検証(2026-06, Win11/S3): 約2.5分スリープ前後で
    wall経過=248s, monotonic経過=248s(スリープ込み), awake経過=94s(スリープ除外)。
    → Windowsで `time.monotonic()` は使えず、`QueryUnbiasedInterruptTime` が正解と確認済み。

用途の注意:
    戻り値は「プロセス起動とは無関係な単調増加秒（起動からの経過に近い）」であり、
    プロセス間・再起動・UI表示・永続化には使えない。あくまで同一プロセス内の
    「2点間の経過時間」を測る差分専用。
"""
from __future__ import annotations

import sys
import time

if sys.platform == "win32":
    import ctypes

    _kernel32 = ctypes.windll.kernel32
    # QueryUnbiasedInterruptTime(PULONGLONG): 100ナノ秒単位。Windows 7+。
    # スリープ/休止時間を除外した割り込み時間を返す。
    _QueryUnbiasedInterruptTime = _kernel32.QueryUnbiasedInterruptTime

    def awake_seconds() -> float:
        """スリープを除外した単調増加秒を返す（Windows: QueryUnbiasedInterruptTime）。

        bufは呼び出しごとにローカル確保する（共有グローバルだと多スレッドで競合する）。
        """
        buf = ctypes.c_ulonglong()
        _QueryUnbiasedInterruptTime(ctypes.byref(buf))
        return buf.value / 1e7

else:
    def awake_seconds() -> float:
        """スリープを除外した単調増加秒を返す。

        Linux: CLOCK_MONOTONIC / macOS: mach_absolute_time。いずれも
        `time.monotonic()` がサスペンド時間を除外するため、そのまま使える。
        """
        return time.monotonic()
