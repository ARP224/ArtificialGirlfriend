"""
tests/smoke/test_wake_gate.py

覚醒ゲート(backend/shared/wake_gate.py)のスモーク。
時計と idle 照会を注入して駆動する(スレッド・sleep・実OS照会なし)。

- 2時計ギャップでのみ閉じる(ストール・時計巻き戻しでは閉じない)
- 閉状態は無入力の DarkWake 窓(長窓含む)を跨いで維持される
- ユーザー入力で開く(ギャップ検出直後の入力も式が拾う)
- Windows 意味論(idle がスリープ込みで進む)でも判定方向が同じ
- idle 照会不能・内部例外はフェイルオープン
- notify_user_activity は即時再開(検出前入力デッドロックの脱出口)
"""

import pytest

import backend.shared.wake_gate as wg
from backend.shared.wake_gate import WakeGate


class FakeEnv:
    """壁時計・awake時計・OS idle照会の3点を一貫して進める偽環境。

    idle_counts_sleep: macOS HIDIdleTime はスリープ除外(False)、
    Windows GetLastInputInfo 基準はスリープ込み(True)。
    """

    def __init__(self, idle_counts_sleep=False):
        self.wall = 1_000_000.0
        self.awake = 5_000.0
        self.idle = 0.0
        self.idle_counts_sleep = idle_counts_sleep

    def run_awake(self, seconds):
        """覚醒状態(ユーザー在席/DarkWake どちらも)で時間を進める。入力なし。"""
        self.wall += seconds
        self.awake += seconds
        if self.idle is not None:
            self.idle += seconds

    def sleep(self, seconds):
        """本物のスリープ。awake は止まる。"""
        self.wall += seconds
        if self.idle is not None and self.idle_counts_sleep:
            self.idle += seconds

    def user_input(self):
        self.idle = 0.0


@pytest.fixture
def env(monkeypatch):
    e = FakeEnv()
    monkeypatch.setattr(wg, "_wall_seconds", lambda: e.wall)
    monkeypatch.setattr(wg, "awake_seconds", lambda: e.awake)
    monkeypatch.setattr(wg, "_query_idle_seconds", lambda: e.idle)
    return e


def test_initial_open(env):
    gate = WakeGate()
    assert gate.is_open() is True


def test_stall_does_not_close(env):
    """GC/高負荷ストールは両時計が等しく進む=誤検出しない。"""
    gate = WakeGate()
    assert gate.is_open()
    env.run_awake(300)  # 5分間サンプルが来なくても
    assert gate.is_open() is True


def test_clock_set_back_does_not_close(env):
    gate = WakeGate()
    assert gate.is_open()
    env.wall -= 100  # 壁時計の巻き戻し(手動変更/NTP)
    assert gate.is_open() is True


def test_sleep_closes_and_darkwake_windows_stay_closed(env):
    """一晩の形: スリープ→DarkWake窓(長窓92分含む)→スリープ…で閉じ続ける。"""
    gate = WakeGate()
    env.user_input()
    env.run_awake(600)  # ユーザー離席10分(idle=600)で
    assert gate.is_open()
    env.sleep(900)  # 15分眠って最初の窓
    assert gate.is_open() is False
    for window in (60, 488, 92 * 60):  # 実測の平均窓・最長窓
        env.run_awake(window)  # 窓の中: 無入力のまま覚醒時間が進む
        assert gate.is_open() is False, f"window={window}s で誤って開いた"
        env.sleep(900)
        assert gate.is_open() is False


def test_user_input_reopens_and_records_resume(env):
    gate = WakeGate()
    assert gate.is_open()
    env.sleep(900)
    assert gate.is_open() is False
    env.run_awake(30)
    env.user_input()  # 復帰してロック解除
    env.run_awake(1)
    assert gate.is_open() is True
    assert gate.last_resume_awake == pytest.approx(env.awake)
    # 再開後は通常運転(無人の覚醒継続でも開いたまま=J9 自律動作を壊さない)
    env.run_awake(3600)
    assert gate.is_open() is True


def test_input_right_after_gap_detection(env):
    """ギャップ検出直後(マージン境界)の入力を取り逃がさない(判定式の核心)。

    idle_at_gap を右辺に入れない素朴な式(idle < awake経過)だと
    ε秒後の入力は idle ≈ awake経過 − ε となり永久に開かない。
    """
    gate = WakeGate()
    env.user_input()
    env.run_awake(10)  # idle_at_gap は 10 秒程度の小さい値になる
    assert gate.is_open()
    env.sleep(900)
    assert gate.is_open() is False  # ここで idle_at_gap=10 が基準化される
    env.run_awake(1)
    env.user_input()  # 検出のわずか1秒後に入力
    env.run_awake(1)
    assert gate.is_open() is True


def test_windows_idle_semantics(monkeypatch):
    """idle がスリープ込みで進む(GetTickCount 基準)でも判定方向は同じ。"""
    e = FakeEnv(idle_counts_sleep=True)
    monkeypatch.setattr(wg, "_wall_seconds", lambda: e.wall)
    monkeypatch.setattr(wg, "awake_seconds", lambda: e.awake)
    monkeypatch.setattr(wg, "_query_idle_seconds", lambda: e.idle)
    gate = WakeGate()
    assert gate.is_open()
    e.sleep(3600)  # S3 で1時間
    assert gate.is_open() is False
    e.run_awake(120)  # 無人覚醒(自動メンテナンス)では開かない
    assert gate.is_open() is False
    e.user_input()
    e.run_awake(1)
    assert gate.is_open() is True


def test_fail_open_when_idle_unavailable_at_gap(env):
    """ギャップ検出時に idle 照会不能なら閉じない(フェイルオープン)。"""
    env.idle = None
    gate = WakeGate()
    assert gate.is_open()
    env.wall += 900  # スリープ相当のギャップ
    assert gate.is_open() is True


def test_fail_open_when_idle_becomes_unavailable_while_closed(env):
    gate = WakeGate()
    assert gate.is_open()
    env.sleep(900)
    assert gate.is_open() is False
    env.idle = None  # 閉状態で照会が壊れたら開へ倒す
    assert gate.is_open() is True


def test_fail_open_on_internal_exception(env, monkeypatch):
    gate = WakeGate()
    assert gate.is_open()

    def boom():
        raise RuntimeError("clock backend gone")

    monkeypatch.setattr(wg, "awake_seconds", boom)
    assert gate.is_open() is True


def test_notify_user_activity_reopens(env):
    """会話開始シグナル: 検出前に入力が済んでいて式が割れないケースの脱出口。"""
    gate = WakeGate()
    env.user_input()
    env.run_awake(5)
    assert gate.is_open()
    env.sleep(900)
    env.run_awake(2)
    env.user_input()  # ゲートが consult される前に復帰入力
    env.run_awake(1)
    assert gate.is_open() is False  # idle_at_gap が入力後の小値で基準化→式は割れない
    gate.notify_user_activity()  # 会話開始
    assert gate.is_open() is True
    assert gate.last_resume_awake is not None


def test_singleton_accessor():
    assert wg.get_wake_gate() is wg.get_wake_gate()
