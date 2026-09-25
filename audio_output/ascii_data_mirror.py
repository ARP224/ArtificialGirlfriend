"""
ascii_data_mirror.py

Windows only: keep the TTS data that C code opens with a `char*` reachable
from an ASCII-only path when the install path contains non-ASCII characters
(e.g. `OneDrive\\デスクトップ\\ArtificialGirlfriend`).

Two libraries hand a data-directory path from Python (UTF-8 bytes) to C code
that opens it with the ANSI code page (cp932 on Japanese Windows), so any
non-ASCII character becomes mojibake (spaces are harmless):

- pyopenjtalk (MeCab dictionary, SBV2 Japanese TTS): `Failed to initalize Mecab`
- espeak-ng via phonemizer (Kokoro English TTS): falls back to its compile-time
  default data dir and calls `exit(1)` - the whole backend process dies.

Strategy (2026-09-05 実測で唯一成立した案): copy the data directory to
`%ProgramData%\\ArtificialGirlfriend\\data_mirror\\<name>` (always ASCII on
Windows) and point the library there. Everything is a no-op when the source
path is ASCII (the documented `C:\\ArtificialGirlfriend` install) and on
non-Windows (C libraries there take UTF-8 paths natively), so the normal
install never touches this module's disk logic. The installer's fetch scripts
create the mirror ahead of time; the runtime hooks re-validate it (content
fingerprint) and re-create it if missing or stale.

Uninstall: `Uninstall Artificial Girlfriend (Windows).bat` removes
`%ProgramData%\\ArtificialGirlfriend`.
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile
from importlib.util import find_spec
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MARKER_NAME = ".mirror.json"
OPENJTALK_MIRROR_NAME = "open_jtalk_dic"
ESPEAK_MIRROR_NAME = "espeak-ng-data"
# Deliberately nonexistent: see apply_espeak_data_path().
ESPEAK_UNAVAILABLE_NAME = "espeak-ng-data.unavailable"

# Module-level so tests can flip it without touching os.name (pathlib reads that).
_IS_WINDOWS = os.name == "nt"


def mirror_root() -> Path:
    """`%ProgramData%\\ArtificialGirlfriend\\data_mirror` (ASCII on every Windows
    locale; `%APPDATA%` / `%TEMP%` are NOT - they contain the user name)."""
    base = os.environ.get("ProgramData") or r"C:\ProgramData"
    return Path(base) / "ArtificialGirlfriend" / "data_mirror"


def needs_mirror(path) -> bool:
    """True only on Windows and only when the path has a non-ASCII character."""
    return _IS_WINDOWS and not str(path).isascii()


def _fingerprint(src: Path) -> str:
    """Content fingerprint: relative paths + sizes (no source path, so two installs
    sharing one mirror agree; version bumps of the pinned packages change it)."""
    entries = []
    for p in sorted(src.rglob("*")):
        if p.is_file() and p.name != MARKER_NAME:
            entries.append((p.relative_to(src).as_posix(), p.stat().st_size))
    return hashlib.sha1(json.dumps(entries).encode("utf-8")).hexdigest()


def _marker_matches(dst: Path, fingerprint: str) -> bool:
    try:
        data = json.loads((dst / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("fingerprint") == fingerprint


def ensure_ascii_dir(src, name: str, root=None) -> Optional[Path]:
    """Return an ASCII-only directory holding the contents of `src`.

    - `src` ASCII, or non-Windows: returns `src` itself and touches nothing.
    - otherwise: `<root>/<name>`, copied on first use; re-copied when the marker
      is missing (interrupted copy) or the content fingerprint changed.
      The copy is staged in a temp dir and renamed into place, so a concurrent
      second process either finds a complete mirror or none.
    - None when no mirror can be produced (root not ASCII, not writable, disk
      full, `src` missing). Callers fall back to their library-specific plan B.
    """
    src = Path(src)
    if not needs_mirror(src):
        return src
    root = Path(root) if root is not None else mirror_root()
    if not str(root).isascii() or not str(name).isascii():
        logger.error(f"TTS data mirror root is not ASCII, cannot mirror {name}: {root}")
        return None
    if not src.is_dir():
        logger.error(f"TTS data source missing, cannot mirror {name}: {src}")
        return None
    dst = root / name
    tmp: Optional[Path] = None
    try:
        fingerprint = _fingerprint(src)
        if dst.is_dir() and _marker_matches(dst, fingerprint):
            return dst
        root.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f"{name}.tmp-", dir=root))
        staged = tmp / name
        shutil.copytree(src, staged)
        (staged / MARKER_NAME).write_text(
            json.dumps({"fingerprint": fingerprint}), encoding="utf-8"
        )
        if dst.exists():
            if _marker_matches(dst, fingerprint):
                # another process finished the same copy first
                return dst
            shutil.rmtree(dst)
        os.replace(staged, dst)
        logger.info(f"TTS data mirrored to ASCII path (non-ASCII install path): {src} -> {dst}")
        return dst
    except OSError as e:
        logger.error(f"TTS data mirror failed for {name} ({src} -> {dst}): {e}")
        return None
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _package_dir_needs_mirror(package: str) -> bool:
    """Gate without importing the package: its install directory is where the data lives."""
    spec = find_spec(package)
    if spec is None or spec.origin is None:
        return False
    return needs_mirror(Path(spec.origin).parent)


def prepare_openjtalk_dict() -> None:
    """Point pyopenjtalk at an ASCII copy of its MeCab dictionary when the venv
    path is non-ASCII. Call before the first Japanese g2p (AG: `_ensure_sbv2()`
    and the installer's fetch_openjtalk_dict). ASCII install / non-Windows:
    returns without even importing pyopenjtalk."""
    if not _package_dir_needs_mirror("pyopenjtalk"):
        return
    import pyopenjtalk

    def _current_dir() -> Path:
        d = pyopenjtalk.OPEN_JTALK_DICT_DIR
        return Path(d.decode("utf-8") if isinstance(d, bytes) else d)

    src = _current_dir()
    if not needs_mirror(src):
        return  # OPEN_JTALK_DICT_DIR env override already points at an ASCII dir
    if not src.is_dir():
        # Dictionary not fetched at install time (offline install): let pyopenjtalk
        # download it now (g2p -> _lazy_init). MeCab init then fails on the
        # non-ASCII path, which is expected; the mirror below takes over.
        try:
            pyopenjtalk.g2p("あ")
        except Exception as e:
            logger.info(f"pyopenjtalk dictionary fetch attempt ended with: {e}")
        src = _current_dir()
    dst = ensure_ascii_dir(src, OPENJTALK_MIRROR_NAME)
    if dst is not None:
        # Module global read at every OpenJTalk() construction (pyopenjtalk-dict pinned).
        pyopenjtalk.OPEN_JTALK_DICT_DIR = str(dst).encode("utf-8")
        logger.info(f"OpenJTalk dictionary path set to {dst}")
        return
    # Plan B: hand MeCab the path in the ANSI code page (what its fopen expects).
    # Only works when every character is representable there (Japanese path on
    # Japanese Windows). The singleton is pre-created because _lazy_init()'s
    # exists() would misread mbcs bytes as UTF-8 and start a re-download.
    try:
        raw = str(src).encode("mbcs")
        pyopenjtalk._global_jtalk = pyopenjtalk.OpenJTalk(dn_mecab=raw)
        logger.warning(f"OpenJTalk dictionary mirror unavailable; using ANSI path bytes for {src}")
    except Exception as e:
        logger.error(f"OpenJTalk dictionary unusable on non-ASCII path {src}: {e}")


def apply_espeak_data_path() -> Optional[Path]:
    """Point phonemizer/espeak-ng at an ASCII copy of espeak-ng-data when the venv
    path is non-ASCII. Call after `import kokoro` (misaki.espeak sets the default
    at import) and before a KPipeline is created. Returns the mirror path when one
    was applied, None otherwise (no-op or plan B)."""
    if not _package_dir_needs_mirror("espeakng_loader"):
        return None
    import espeakng_loader
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    src = Path(espeakng_loader.get_data_path())
    dst = ensure_ascii_dir(src, ESPEAK_MIRROR_NAME)
    if dst is not None:
        EspeakWrapper.set_data_path(str(dst))
        logger.info(f"espeak-ng data path set to {dst}")
        return dst
    # Plan B: a deliberately nonexistent directory. phonemizer validates the data
    # path in Python ("is not a readable directory" RuntimeError) before espeak-ng's
    # C init, which would otherwise exit(1) and take the backend process down.
    # Kokoro catches that and runs without its espeak fallback (words outside its
    # dictionary are skipped) - verified 2026-09-05.
    sentinel = mirror_root() / ESPEAK_UNAVAILABLE_NAME
    EspeakWrapper.set_data_path(str(sentinel))
    logger.warning(
        "espeak-ng data mirror unavailable on non-ASCII install path; English TTS "
        "continues without the espeak fallback (out-of-dictionary words are skipped)"
    )
    return None
