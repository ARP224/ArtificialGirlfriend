"""配布スクリプトのEOL不変条件のguard。

.command(Mac用)はLF必須: CRLFだとshebangが「/bin/bash\r」になり
zshが bad interpreter で実行を拒否する(2026-08-01 サブOSclone→Mac転送で実踏)。
.bat(Windows用)はCRLF維持: .gitattributesで-text(バイト凍結)のため
git側のEOL自動矯正が働かず、LF混入を検出できるのはこのテストだけ。
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_files(pattern):
    out = subprocess.check_output(
        ["git", "ls-files", "-z", "--", pattern], cwd=REPO_ROOT
    )
    return [REPO_ROOT / p for p in out.decode("utf-8").split("\0") if p]


def test_bat_files_are_crlf():
    files = _tracked_files("*.bat")
    assert files, "tracked .bat が0本 = 列挙自体が壊れている"
    for f in files:
        lone_lf = f.read_bytes().replace(b"\r\n", b"").count(b"\n")
        assert lone_lf == 0, (
            f"{f.relative_to(REPO_ROOT)}: 裸のLFが{lone_lf}行 "
            "(.batはCRLF必須。-textで凍結中のためgitは矯正しない)"
        )


def test_command_files_are_lf():
    files = _tracked_files("*.command")
    assert files, "tracked .command が0本 = 列挙自体が壊れている"
    for f in files:
        assert b"\r" not in f.read_bytes(), (
            f"{f.relative_to(REPO_ROOT)}: CR混入 "
            "(.commandはLF必須。CRLFはMacで bad interpreter 事故になる)"
        )
