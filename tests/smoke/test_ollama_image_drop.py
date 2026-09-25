"""DirectOllamaChat の画像出口網のスモークテスト。

契約（2026-08-11 per-model化）: 画像はモデルが vision 対応のときだけ
base64 で送り、非対応・判定不能（fail-closed）なら破棄する。入口
(📎添付・カメラ系の可用性ゲート)をすり抜けて履歴に残った画像
(別プロバイダ/モデル時代の添付/生成/カメラ画像)への最終網。FakeLLMが
LLM層ごと差し替わる既存ハーネスではこの経路が走らないため、純関数として
直接検証する。ツールループ往復メッセージの素通しも同関数の契約。
"""

import backend.llm.ollama_capabilities as oc
from backend.llm.ollama_integration import DirectOllamaChat


def _make_chat():
    return DirectOllamaChat(model="dummy-model")


def _set_caps(monkeypatch, known, vision):
    monkeypatch.setattr(
        oc, "get_caps",
        lambda model: {"known": known, "tools": False, "vision": vision,
                       "context_length": None})


def test_convert_messages_drops_images_without_vision(monkeypatch):
    _set_caps(monkeypatch, known=True, vision=False)
    chat = _make_chat()
    converted = chat._convert_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "この写真見て", "images": ["/no/such/a.png", "/no/such/b.png"]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "続き"},
    ])
    # 画像フィールドはどのメッセージにも残らない
    assert all("images" not in m for m in converted)
    # role/content は無傷(順序も維持)
    assert [m["role"] for m in converted] == ["system", "user", "assistant", "user"]
    assert converted[1]["content"] == "この写真見て"


def test_convert_messages_drops_images_when_caps_unknown(monkeypatch):
    """判定不能（未照会・旧Ollama・サーバー停止）は fail-closed=破棄。"""
    _set_caps(monkeypatch, known=False, vision=False)
    chat = _make_chat()
    converted = chat._convert_messages([
        {"role": "user", "content": "x", "images": ["/no/such/a.png"]},
    ])
    assert "images" not in converted[0]


def test_convert_messages_sends_base64_images_with_vision(monkeypatch, tmp_path):
    _set_caps(monkeypatch, known=True, vision=True)
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG fake image bytes")
    chat = _make_chat()

    converted = chat._convert_messages([
        {"role": "user", "content": "この写真見て", "images": [str(img)]},
    ])

    import base64
    assert converted[0]["images"] == [
        base64.b64encode(b"\x89PNG fake image bytes").decode("utf-8")]
    assert converted[0]["content"] == "この写真見て"


def test_convert_messages_skips_unreadable_image_without_crash(
        monkeypatch, tmp_path):
    """エンコード失敗（消えたファイル等）はその画像だけスキップ。"""
    _set_caps(monkeypatch, known=True, vision=True)
    img = tmp_path / "ok.png"
    img.write_bytes(b"ok")
    chat = _make_chat()

    converted = chat._convert_messages([
        {"role": "user", "content": "x", "images": ["/no/such/gone.png", str(img)]},
    ])

    import base64
    assert converted[0]["images"] == [base64.b64encode(b"ok").decode("utf-8")]


def test_convert_messages_without_images_unchanged(monkeypatch):
    _set_caps(monkeypatch, known=True, vision=False)
    chat = _make_chat()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    converted = chat._convert_messages(msgs)
    assert converted == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


def test_convert_messages_drop_is_logged(monkeypatch, caplog):
    _set_caps(monkeypatch, known=True, vision=False)
    chat = _make_chat()
    with caplog.at_level("WARNING"):
        chat._convert_messages([
            {"role": "user", "content": "x", "images": ["/no/such/c.png"]},
        ])
    assert any("Dropped 1 image" in r.message for r in caplog.records)


def test_convert_messages_passes_tool_roundtrip_through(monkeypatch):
    """ツールループ往復（assistant tool_calls / toolロール結果）の素通し。"""
    _set_caps(monkeypatch, known=True, vision=False)
    chat = _make_chat()
    tool_calls = [{"function": {"name": "add_note", "arguments": {"text": "x"}}}]

    converted = chat._convert_messages([
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        {"role": "tool", "content": "saved", "tool_name": "add_note"},
    ])

    assert converted[0] == {"role": "assistant", "content": "",
                            "tool_calls": tool_calls}
    assert converted[1] == {"role": "tool", "content": "saved",
                            "tool_name": "add_note"}
