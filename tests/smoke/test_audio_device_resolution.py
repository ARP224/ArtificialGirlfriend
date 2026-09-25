"""
tests/smoke/test_audio_device_resolution.py

マイクデバイスの候補収集(_input_candidate_ids)の検証。偽のPortAudio表で
「MMEの31文字切断名も同一物理デバイスとして束ねる」「API優先順に並ぶ」
「マッパー/出力専用/無関係デバイスを含めない」を固定する
(2026-07-25 実障害: 凍結表のズレで誤変種を掴み既定へ劣化した回帰の網)。
"""

import pytest

from audio_input.audio_input import AudioInputManager

FAKE_HOSTAPIS = (
    {'name': 'MME'},
    {'name': 'Windows DirectSound'},
    {'name': 'Windows WASAPI'},
    {'name': 'Windows WDM-KS'},
)

# 実機の観測(GSX 1000)を模した表: MMEのみ名前が31文字で切断されている
FAKE_DEVICES = [
    {'name': 'マイク (GSX 1000 Communication Aud', 'max_input_channels': 1, 'hostapi': 0},
    {'name': 'Microsoft Sound Mapper - Input', 'max_input_channels': 2, 'hostapi': 0},
    {'name': 'マイク (GSX 1000 Communication Audio)', 'max_input_channels': 1, 'hostapi': 1},
    {'name': 'スピーカー (GSX 1000 Main Audio)', 'max_input_channels': 0, 'hostapi': 2},
    {'name': 'マイク (GSX 1000 Communication Audio)', 'max_input_channels': 1, 'hostapi': 2},
    {'name': 'マイク (GSX 1000 Communication Audio)', 'max_input_channels': 2, 'hostapi': 3},
    {'name': 'Line (3- Yamaha AG03MK2)', 'max_input_channels': 2, 'hostapi': 2},
]

SAVED_NAME = 'マイク (GSX 1000 Communication Audio)'


@pytest.fixture
def mgr(monkeypatch):
    import audio_input.audio_input as ai
    monkeypatch.setattr(ai.sd, 'query_devices', lambda *a, **k: FAKE_DEVICES)
    monkeypatch.setattr(ai.sd, 'query_hostapis', lambda *a, **k: FAKE_HOSTAPIS)
    return AudioInputManager()


class TestInputCandidateIds:
    def test_all_variants_in_api_priority_order(self, mgr):
        """WASAPI→DirectSound→MME(切断名)→WDM-KS の順で全変種が並ぶ。"""
        candidates = mgr._input_candidate_ids(SAVED_NAME)
        assert [(i, api) for i, api in candidates] == [
            (4, 'Windows WASAPI'),
            (2, 'Windows DirectSound'),
            (0, 'MME'),
            (5, 'Windows WDM-KS'),
        ]

    def test_truncated_mme_name_is_grouped(self, mgr):
        """MMEの31文字切断名も同一物理デバイスとして候補に入る(実測形)。"""
        ids = [i for i, _api in mgr._input_candidate_ids(SAVED_NAME)]
        assert 0 in ids

    def test_unrelated_and_output_only_excluded(self, mgr):
        """無関係デバイス(Yamaha)・出力専用・サウンドマッパーは含めない。"""
        ids = [i for i, _api in mgr._input_candidate_ids(SAVED_NAME)]
        assert 1 not in ids  # Sound Mapper
        assert 3 not in ids  # output-only
        assert 6 not in ids  # Yamaha

    def test_unknown_name_returns_empty(self, mgr):
        assert mgr._input_candidate_ids('Ghost Microphone XYZ') == []
