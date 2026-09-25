"""
backend/shared/note_io.py

Shared file-I/O for numbered-Markdown note files (B8 / ST5-S13).

Both the core note system (backend/memory/note_manager.py) and the ELYTH
note system (backend/elyth/elyth_note_manager.py) persist their entries as an
identical numbered-Markdown list ("1. item\n2. item\n"). The load/save bodies
were byte-for-byte duplicates apart from the target directory and the log
label, so this module holds the single canonical implementation. Callers pass
their own directory, logger and label, which keeps the emitted log records
(logger name + message) unchanged vs the two original implementations.

Pure stdlib. No behavior change.
"""

import os
import re
import logging
from typing import List
from pathlib import Path


def load_note_entries(
    note_dir: Path, character_id: str, logger: logging.Logger, label: str = "note"
) -> List[str]:
    """Load a numbered-Markdown note file and return its entry strings."""
    path = note_dir / f"{character_id}.md"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines:
            line = line.strip()
            if line and re.match(r"^\d+\.\s", line):
                content = re.sub(r"^\d+\.\s", "", line)
                entries.append(content)
        return entries
    except Exception as e:
        logger.error(f"Failed to load {label} for {character_id}: {e}")
        return []


def save_note_entries(
    note_dir: Path,
    character_id: str,
    entries: List[str],
    logger: logging.Logger,
    label: str = "note",
) -> None:
    """Save entries as a numbered list to a note file (atomic write)."""
    note_dir.mkdir(parents=True, exist_ok=True)
    path = note_dir / f"{character_id}.md"
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for i, entry in enumerate(entries, 1):
                f.write(f"{i}. {entry}\n")
        os.replace(str(tmp_path), str(path))
    except Exception as e:
        logger.error(f"Failed to save {label} for {character_id}: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def remove_note_entries(
    note_dir: Path, character_id: str, logger: logging.Logger, label: str = "note"
) -> None:
    """Remove a character's note file (character deletion). No error if absent."""
    path = note_dir / f"{character_id}.md"
    if not path.exists():
        return
    try:
        os.remove(path)
        logger.info(f"Removed {label} file for {character_id}")
    except Exception as e:
        logger.error(f"Failed to remove {label} for {character_id}: {e}")
