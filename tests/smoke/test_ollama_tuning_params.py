"""DirectOllamaChat の tuning パラメータ送信契約のスモーク。

契約（2026-08-15）: repeat_last_n は範囲 0〜10000（TUNING_RANGES）。
-1（llama.cpp の「全体を対象」値）は Ollama 0.32.x の検証層が 400 で拒否し、
全文脈窓×ペナルティは絵文字暴走の実因でもあったため廃止（送信時展開も撤去）。
正値はそのまま送信。0（=無効）も明示送信する（未送信だと Ollama 既定の
64 窓が効いてしまう＝「0=無効」が嘘になる）。
"""

from backend.llm.ollama_integration import DirectOllamaChat
from backend.shared.constants import TUNING_RANGES


def test_repeat_last_n_range_lower_bound_is_zero():
    assert TUNING_RANGES["repeat_last_n"][0] == 0


def test_repeat_last_n_positive_passes_through():
    chat = DirectOllamaChat(model="dummy", repeat_last_n=500, num_ctx=12345)
    assert chat._options["repeat_last_n"] == 500


def test_repeat_last_n_zero_is_sent_explicitly():
    chat = DirectOllamaChat(model="dummy", repeat_last_n=0, num_ctx=12345)
    assert chat._options["repeat_last_n"] == 0
