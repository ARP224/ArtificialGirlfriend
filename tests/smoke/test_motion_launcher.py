"""
tests/smoke/test_motion_launcher.py

motion_pngtuber_launcher の Electron 実バイナリ解決の契約テスト(両OS共通)。
node_modules/.bin/electron (env node シバンのシム) を使うと GUI 起動の
PATH に node が無い Mac で即死する(2026-07-23 実踏)ため、
path.txt → dist/ の実バイナリ解決が正であることを固定する。
Windows も同じ解決で dist/electron.exe を直接叩く(npx 不使用)。
"""

from backend.tools.motion_pngtuber_launcher import _electron_binary


def _make_pkg(tmp_path, path_txt=None, dist_files=()):
    pkg = tmp_path / "node_modules" / "electron"
    (pkg / "dist").mkdir(parents=True)
    if path_txt is not None:
        (pkg / "path.txt").write_text(path_txt, encoding="utf-8")
    for rel in dist_files:
        f = pkg / "dist" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")
    return tmp_path / "node_modules"


def test_resolves_via_path_txt_mac_layout(tmp_path):
    rel = "Electron.app/Contents/MacOS/Electron"
    nm = _make_pkg(tmp_path, path_txt=rel + "\n", dist_files=[rel])
    result = _electron_binary(nm)
    assert result is not None
    assert result == nm / "electron" / "dist" / rel


def test_falls_back_to_known_layout_without_path_txt(tmp_path):
    rel = "Electron.app/Contents/MacOS/Electron"
    nm = _make_pkg(tmp_path, path_txt=None, dist_files=[rel])
    assert _electron_binary(nm) == nm / "electron" / "dist" / rel


def test_linux_layout_fallback(tmp_path):
    nm = _make_pkg(tmp_path, path_txt=None, dist_files=["electron"])
    assert _electron_binary(nm) == nm / "electron" / "dist" / "electron"


def test_resolves_via_path_txt_windows_layout(tmp_path):
    nm = _make_pkg(tmp_path, path_txt="electron.exe\n", dist_files=["electron.exe"])
    assert _electron_binary(nm) == nm / "electron" / "dist" / "electron.exe"


def test_windows_layout_fallback_without_path_txt(tmp_path):
    nm = _make_pkg(tmp_path, path_txt=None, dist_files=["electron.exe"])
    assert _electron_binary(nm) == nm / "electron" / "dist" / "electron.exe"


def test_missing_binary_returns_none(tmp_path):
    # path.txt はあるが実体が無い(壊れたインストール)
    nm = _make_pkg(tmp_path, path_txt="Electron.app/Contents/MacOS/Electron")
    assert _electron_binary(nm) is None


def test_missing_package_returns_none(tmp_path):
    nm = tmp_path / "node_modules"
    nm.mkdir()
    assert _electron_binary(nm) is None
