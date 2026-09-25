"""Layer 1 — ascii_data_mirror: non-ASCII install path → ASCII copy of TTS data.

Covers the generic helper's contract with a temp root standing in for
%ProgramData%. The library-specific hooks (pyopenjtalk / phonemizer) only
act on a non-ASCII venv path, so they are proven by the Japanese-path
integration run (`_scripts/jp_path_checks.py` in the test clone); here we
only assert they stay no-ops on this ASCII venv (no library import).
"""

import json
import os
import sys

import pytest

from audio_output import ascii_data_mirror as m

JP = "日本語"

windows_only = pytest.mark.skipif(os.name != "nt", reason="mirror logic is Windows-only")


@pytest.fixture
def ascii_root(tmp_path):
    root = tmp_path / "root"
    if not str(root).isascii():
        pytest.skip("temp dir itself is non-ASCII on this machine")
    return root


def _make_src(tmp_path, name=JP):
    src = tmp_path / name / "data"
    (src / "sub").mkdir(parents=True)
    (src / "phontab").write_bytes(b"x" * 10)
    (src / "sub" / "a.txt").write_text("hello", encoding="utf-8")
    return src


def test_ascii_source_is_returned_untouched(tmp_path, ascii_root):
    src = tmp_path / "ascii_src"
    src.mkdir()
    (src / "f").write_bytes(b"1")
    assert m.ensure_ascii_dir(src, "x", root=ascii_root) == src
    assert not ascii_root.exists()


@windows_only
def test_non_ascii_source_is_copied_with_marker(tmp_path, ascii_root):
    src = _make_src(tmp_path)
    dst = m.ensure_ascii_dir(src, "x", root=ascii_root)
    assert dst == ascii_root / "x"
    assert (dst / "phontab").read_bytes() == b"x" * 10
    assert (dst / "sub" / "a.txt").read_text(encoding="utf-8") == "hello"
    marker = json.loads((dst / m.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["fingerprint"]
    # staging dir removed
    assert [p.name for p in ascii_root.iterdir()] == ["x"]


@windows_only
def test_second_call_reuses_existing_mirror(tmp_path, ascii_root, monkeypatch):
    src = _make_src(tmp_path)
    dst = m.ensure_ascii_dir(src, "x", root=ascii_root)
    calls = []
    monkeypatch.setattr(m.shutil, "copytree", lambda *a, **k: calls.append(a))
    assert m.ensure_ascii_dir(src, "x", root=ascii_root) == dst
    assert calls == []


@windows_only
def test_missing_marker_triggers_recopy(tmp_path, ascii_root):
    """An interrupted first copy (no marker) must not be trusted."""
    src = _make_src(tmp_path)
    dst = m.ensure_ascii_dir(src, "x", root=ascii_root)
    (dst / m.MARKER_NAME).unlink()
    (dst / "phontab").unlink()
    assert m.ensure_ascii_dir(src, "x", root=ascii_root) == dst
    assert (dst / "phontab").read_bytes() == b"x" * 10
    assert (dst / m.MARKER_NAME).exists()


@windows_only
def test_changed_content_triggers_recopy(tmp_path, ascii_root):
    """A version bump of the pinned package (different sizes) refreshes the mirror."""
    src = _make_src(tmp_path)
    dst = m.ensure_ascii_dir(src, "x", root=ascii_root)
    (src / "phontab").write_bytes(b"y" * 20)
    assert m.ensure_ascii_dir(src, "x", root=ascii_root) == dst
    assert (dst / "phontab").read_bytes() == b"y" * 20


@windows_only
def test_missing_source_returns_none(tmp_path, ascii_root):
    assert m.ensure_ascii_dir(tmp_path / JP / "nope", "x", root=ascii_root) is None
    assert not ascii_root.exists()


@windows_only
def test_non_ascii_root_returns_none(tmp_path):
    src = _make_src(tmp_path)
    assert m.ensure_ascii_dir(src, "x", root=tmp_path / JP / "root") is None


@windows_only
def test_copy_failure_returns_none_and_cleans_staging(tmp_path, ascii_root, monkeypatch):
    src = _make_src(tmp_path)

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(m.shutil, "copytree", boom)
    assert m.ensure_ascii_dir(src, "x", root=ascii_root) is None
    assert not (ascii_root / "x").exists()
    assert not ascii_root.exists() or list(ascii_root.iterdir()) == []


def test_non_windows_is_noop(tmp_path, ascii_root, monkeypatch):
    monkeypatch.setattr(m, "_IS_WINDOWS", False)
    src = _make_src(tmp_path)
    assert m.ensure_ascii_dir(src, "x", root=ascii_root) == src
    assert not ascii_root.exists()


@windows_only
def test_needs_mirror_spaces_are_fine_non_ascii_is_not():
    assert m.needs_mirror(r"C:\AG space test") is False
    assert m.needs_mirror(r"C:\AG日本語テスト") is True


def test_library_hooks_are_noops_on_ascii_venv():
    """The documented install (ASCII path) must not even import the libraries."""
    if not str(sys.prefix).isascii():
        pytest.skip("venv path is non-ASCII; hooks are active here by design")
    before = set(sys.modules)
    m.prepare_openjtalk_dict()
    assert m.apply_espeak_data_path() is None
    newly = set(sys.modules) - before
    assert "pyopenjtalk" not in newly
    assert "phonemizer" not in newly
    assert "espeakng_loader" not in newly
