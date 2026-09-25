"""
tests/meta/test_installer_model_pin_sync.py

en-core-web-sm 冪等ゲート(2026-08-05)の版同期ガード。
URL(=版の真実源)は requirements-*.txt の URL 直指定行 1 箇所だが、
両OSインストーラーのスキップ判定には版番号がハードコードされている。
将来 requirements 側だけ版を上げるとガードが旧版を見て新版を永遠に
入れない潜伏バグになるため、ズレをここで赤にする。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_URL_LINE = re.compile(
    r"^en-core-web-sm @ .*en_core_web_sm-([0-9][0-9.]*)-py3-none-any\.whl\s*$",
    re.M,
)


def _req_version(req_name: str) -> str:
    text = (ROOT / req_name).read_text(encoding="utf-8")
    m = _URL_LINE.search(text)
    assert m, f"{req_name}: 'en-core-web-sm @ <URL>' line not found"
    return m.group(1)


def test_requirements_versions_match_each_other():
    assert _req_version("requirements-mac.txt") == _req_version("requirements-windows.txt")


def test_mac_installer_version_matches_requirements():
    ver = _req_version("requirements-mac.txt")
    installer = (ROOT / "Install Artificial Girlfriend (Mac).command").read_text(encoding="utf-8")
    m = re.search(r'^EN_CORE_WEB_SM_VER="([0-9][0-9.]*)"', installer, re.M)
    assert m, "Mac installer: EN_CORE_WEB_SM_VER assignment not found"
    assert m.group(1) == ver, (
        f"Mac installer guards en-core-web-sm {m.group(1)} but "
        f"requirements-mac.txt pins {ver}"
    )


def test_windows_installer_version_matches_requirements():
    ver = _req_version("requirements-windows.txt")
    installer = (ROOT / "Install Artificial Girlfriend (Windows).bat").read_text(encoding="utf-8")
    m = re.search(r'^set "EN_CORE_WEB_SM_VER=([0-9][0-9.]*)"', installer, re.M)
    assert m, "Windows installer: EN_CORE_WEB_SM_VER assignment not found"
    assert m.group(1) == ver, (
        f"Windows installer guards en-core-web-sm {m.group(1)} but "
        f"requirements-windows.txt pins {ver}"
    )
