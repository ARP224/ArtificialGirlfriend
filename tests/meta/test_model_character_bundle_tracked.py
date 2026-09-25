"""同梱モデルキャラクターの素材が git に追跡されていることの guard。

MotionPNGPlayer/.gitignore は `Asset/*` を無視しつつ Momo/Cecilia/Stella だけを
`!Asset/<名前>/` で例外にしている。例外行が消える・名前がずれると、clone した
人にはキャラクター設定だけ届いて Motion 素材が無い(Appear で「フォルダ無し」)
状態になるが、開発機では Asset/ に全素材があるので気づけない。
tests/meta/test_script_eol.py と同じく `git ls-files` で実測する。
併せて .gitattributes(素材はバイト凍結 -text・プロンプト/設定は eol=lf)も実測。
"""

import subprocess
from pathlib import Path

from backend.conversation.model_characters import list_bundled_characters

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked(pattern):
    out = subprocess.check_output(
        ["git", "ls-files", "-z", "--", pattern], cwd=REPO_ROOT
    )
    return {p for p in out.decode("utf-8").split("\0") if p}


def _attr(path, name):
    out = subprocess.check_output(
        ["git", "check-attr", name, "--", path], cwd=REPO_ROOT
    ).decode("utf-8").strip()
    return out.rsplit(": ", 1)[-1]


def test_bundled_model_character_files_are_tracked():
    bundled = list_bundled_characters()
    assert bundled, "model_characters/ が空 = 列挙自体が壊れている"
    tracked_assets = _tracked("MotionPNGPlayer/Asset/*")
    tracked_meta = _tracked("model_characters/*")
    allowed = set()
    for info in bundled:
        name, folder = info["name"], info["motion_pngtuber_folder"]
        allowed.add(folder)
        for fname in ("character.json", "system_prompt.txt", "icon.png"):
            assert f"model_characters/{name}/{fname}" in tracked_meta, fname
        prefix = f"MotionPNGPlayer/Asset/{folder}/"
        assert f"{prefix}mouth/closed.png" in tracked_assets, name
        assert f"{prefix}mouth/open.png" in tracked_assets, name
        webms = [p for p in tracked_assets if p.startswith(prefix) and p.endswith(".webm")]
        assert webms, f"{name}: no tracked .webm (.gitignore exception missing?)"
        assert all(p[:-5] + ".json" in tracked_assets for p in webms), name
    # 例外は同梱キャラ分だけ: ユーザー素材が紛れて追跡されていない
    strays = {p for p in tracked_assets
              if p.split("/")[2] not in allowed}
    assert not strays, sorted(strays)[:5]


def test_bundle_git_attributes():
    for info in list_bundled_characters():
        name, folder = info["name"], info["motion_pngtuber_folder"]
        sample_json = next(iter(_tracked(f"MotionPNGPlayer/Asset/{folder}/*.json")))
        assert _attr(sample_json, "text") == "unset", "素材はバイト凍結(-text)のはず"
        assert _attr(f"model_characters/{name}/system_prompt.txt", "eol") == "lf"
        assert _attr(f"model_characters/{name}/character.json", "eol") == "lf"
        assert _attr(f"model_characters/{name}/icon.png", "text") == "unspecified"
