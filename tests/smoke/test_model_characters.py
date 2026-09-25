"""同梱モデルキャラクター(model_characters/ → 起動時 seed)の不変条件。

- 初回起動で3人が通常の create_character 経路で作られる(設定値・アイコン複製・
  記憶DB作成・LLM未設定・ELYTHキー空)
- 2回目は何もしない。ユーザーが削除したキャラは復活しない(マーカーが記憶する)
- マーカーを失っても既存の config は上書きしない(adopt して記録だけ足す)
- 同梱データそのものの整合(必須ファイル・Motion素材の必須ファイル・
  アイコン512px PNG・プロンプトは LF のみ・Kokoro の声名が実在)

ディレクトリ定数は tests/smoke/test_remove_character_artifacts.py と同じ流儀で
tmp_path へ差し替える(製品コードは曲げない)。
"""

import json
import uuid
from pathlib import Path

import pytest
from PIL import Image

from backend.backend import atomic_file_operation
from backend.conversation import character_manager as cm
from backend.conversation import model_characters as mc

REPO_ROOT = Path(__file__).resolve().parents[2]

# name -> (STT/prompt language, TTS provider)
EXPECTED = {
    "Momo": ("ja", "sbv2"),
    "Cecilia": ("ja", "sbv2"),
    "Stella": ("en", "kokoro"),
}


def _reset_cache():
    cm._character_file_cache.clear()
    cm._character_config_cache.clear()
    cm._last_cache_update = 0


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    dirs = {name: tmp_path / name for name in (
        "character_configs", "character_icons", "memory")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setattr(cm, "CHARACTER_CONFIGS_DIR", str(dirs["character_configs"]))
    monkeypatch.setattr(cm, "CHARACTER_ICONS_DIR", str(dirs["character_icons"]))
    monkeypatch.setattr(cm, "MEMORY_DIR", str(dirs["memory"]))
    monkeypatch.setattr(cm, "_character_file_cache", {})
    monkeypatch.setattr(cm, "_character_config_cache", {})
    monkeypatch.setattr(cm, "_last_cache_update", 0)
    monkeypatch.setattr(mc, "SEED_MARKER_FILE", tmp_path / "model_characters_seeded.json")
    dirs["marker"] = tmp_path / "model_characters_seeded.json"
    return dirs


def _bundled_by_name():
    return {info["name"]: info for info in mc.list_bundled_characters()}


def _read_config(dirs, cid):
    with open(dirs["character_configs"] / f"{cid}.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- seeding

def test_first_run_seeds_three_characters_via_create_character(data_root):
    res = mc.seed_model_characters(atomic_file_operation)
    assert res["errors"] == []
    assert sorted(res["seeded"]) == sorted(EXPECTED)

    bundled = _bundled_by_name()
    for name, (lang, provider) in EXPECTED.items():
        info = bundled[name]
        cid = info["character_id"]
        cfg = _read_config(data_root, cid)
        assert cfg["character_id"] == cid
        assert cfg["name"] == name
        assert cfg["summary_text"] == info["summary_text"]
        assert cfg["faster_whisper_config"] == {"language": lang}
        assert cfg["tts_model_config"].get("provider") == provider
        assert cfg["motion_pngtuber_folder"] == name
        assert cfg["system_prompt"] == info["system_prompt"]
        # LLM は未設定で出荷(ユーザーが「既存キャラクター編集」で選ぶ)
        assert cfg["model_provider"] == "ollama" and cfg["model_name"] == ""
        assert cfg["elyth_api_key"] == "" and cfg["elyth_system_prompt"] == ""
        # アイコンは character_icons/<id>.png へ複製され config もそこを指す
        icon = data_root["character_icons"] / f"{cid}.png"
        assert icon.is_file() and icon.stat().st_size > 0
        assert cfg["icon_path"].replace("\\", "/").endswith(f"character_icons/{cid}.png")
        assert (data_root["memory"] / f"{cid}.db").exists()
    assert cfg["tuning"]  # create_character が既定チューニングを埋める

    marker = json.loads(data_root["marker"].read_text(encoding="utf-8"))
    assert set(marker["seeded"]) == {i["character_id"] for i in bundled.values()}


def test_second_run_is_noop_and_deleted_character_stays_deleted(data_root):
    mc.seed_model_characters(atomic_file_operation)
    _reset_cache()
    res2 = mc.seed_model_characters(atomic_file_operation)
    assert res2 == {"seeded": [], "skipped": 3, "errors": []}

    momo_id = _bundled_by_name()["Momo"]["character_id"]
    momo_cfg = data_root["character_configs"] / f"{momo_id}.json"
    momo_cfg.unlink()  # ユーザーが削除した相当
    _reset_cache()
    res3 = mc.seed_model_characters(atomic_file_operation)
    assert res3 == {"seeded": [], "skipped": 3, "errors": []}
    assert not momo_cfg.exists(), "削除したモデルキャラクターが復活した"


def test_lost_marker_adopts_existing_configs_without_touching_them(data_root):
    mc.seed_model_characters(atomic_file_operation)
    before = {p.name: p.read_bytes() for p in data_root["character_configs"].iterdir()}
    data_root["marker"].unlink()
    _reset_cache()
    res = mc.seed_model_characters(atomic_file_operation)
    assert res == {"seeded": [], "skipped": 3, "errors": []}
    after = {p.name: p.read_bytes() for p in data_root["character_configs"].iterdir()}
    assert after == before
    marker = json.loads(data_root["marker"].read_text(encoding="utf-8"))
    assert set(marker["seeded"]) == {Path(n).stem for n in before}
    assert all(v.get("adopted") for v in marker["seeded"].values())


def test_missing_bundle_dir_is_a_quiet_noop(data_root, tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "MODEL_CHARACTERS_DIR", tmp_path / "no_such_dir")
    res = mc.seed_model_characters(atomic_file_operation)
    assert res == {"seeded": [], "skipped": 0, "errors": []}
    assert not data_root["marker"].exists()
    assert list(data_root["character_configs"].iterdir()) == []


# ------------------------------------------------------- bundled data itself

def test_bundled_data_is_complete_and_consistent():
    bundled = _bundled_by_name()
    assert set(bundled) == set(EXPECTED)
    ids = [i["character_id"] for i in bundled.values()]
    assert len(set(ids)) == len(ids)
    for cid in ids:
        uuid.UUID(cid)  # 固定IDは正規の UUID

    from audio_output.fetch_kokoro_models import EN_VOICES

    for name, (lang, provider) in EXPECTED.items():
        info = bundled[name]
        folder = REPO_ROOT / "model_characters" / name
        assert info["faster_whisper_config"] == {"language": lang}
        assert info["tts_model_config"].get("provider") == provider
        if provider == "kokoro":
            assert info["tts_model_config"]["voice_name"] in EN_VOICES
        # プロンプト/設定は LF のみ(.gitattributes で固定・LLM に届くバイト)
        for text_file in ("system_prompt.txt", "character.json"):
            assert b"\r" not in (folder / text_file).read_bytes(), text_file
        assert info["system_prompt"].strip()
        # アイコンは UI のアップロード処理と同じ 512px 正方形 PNG
        with Image.open(folder / "icon.png") as img:
            assert img.format == "PNG" and img.size == (512, 512)
        # Motion 素材: プレイヤー(main.js)が要求する mouth/closed.png・open.png と
        # 「動画 + 同名の位置データ .json」が最低1組
        asset = REPO_ROOT / "MotionPNGPlayer" / "Asset" / info["motion_pngtuber_folder"]
        assert (asset / "mouth" / "closed.png").is_file(), name
        assert (asset / "mouth" / "open.png").is_file(), name
        pairs = [v for v in asset.glob("*.webm") if v.with_suffix(".json").is_file()]
        assert pairs, f"{name}: no .webm with matching .json"
