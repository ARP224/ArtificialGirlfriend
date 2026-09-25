# db_housekeeping.py
"""
Per-character memory DB housekeeping shared by the memory domain.

Owns the "which SQLite file belongs to which character" enumeration that
several startup-time walks need (embedding-migration scan, WAL sweep), and
the startup WAL sweep itself.

WAL side files (-wal/-shm): SQLite creates them while a WAL-mode DB is open
and retires them only when the LAST connection closes cleanly. AG's process
ends with os._exit and a crash leaves them too, so at startup — before
anything holds a DB open — every character DB is opened normally, its WAL
checkpointed into the main file (this IS SQLite's crash recovery: a leftover
-wal holds committed writes not yet in the .db) and closed. Never delete the
side files by hand.
"""

import logging
import os
import sqlite3
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Busy wait per DB for the sweep. Another process holding the DB (a second
# instance) just keeps its side files — nothing is forced.
_SWEEP_BUSY_TIMEOUT = 2.0


def _default_db_path(character_id: str) -> str:
    from backend.shared.constants import MEMORY_DIR
    return f"{MEMORY_DIR}/{character_id}.db"


def iter_character_dbs() -> List[Tuple[str, str]]:
    """Return [(character_id, db_path)] for all characters (raw configs).

    Uses the raw config loader (same db_file_path resolution as
    activate_character) so the scan and the workers hit the exact DB the
    conversation path uses.
    """
    from backend.conversation.character_manager import (
        load_character_list,
        load_character_config,
    )

    result = []
    for char in load_character_list():
        character_id = char.get("id")
        if not character_id:
            continue
        try:
            config = load_character_config(character_id) or {}
        except Exception as e:
            logger.warning(f"[DB housekeeping] Config load failed for {character_id}: {e}")
            continue
        db_path = config.get("db_file_path") or _default_db_path(character_id)
        result.append((character_id, db_path))
    return result


def release_wal_side_files(db_path: str) -> str:
    """Open ``db_path`` normally, checkpoint its WAL into the main file, close.

    Returns "released" (opened+checkpointed+closed — side files retired if
    this was the last connection), "missing" (no such file; nothing created)
    or "error" (not a database / locked past the busy timeout; file untouched).
    A 0-byte .db (character created, never activated) stays 0 bytes.
    """
    if not db_path or not os.path.exists(db_path):
        return "missing"
    try:
        conn = sqlite3.connect(db_path, timeout=_SWEEP_BUSY_TIMEOUT)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        return "released"
    except sqlite3.Error as e:
        logger.warning(
            f"[DB housekeeping] WAL release skipped for {os.path.basename(db_path)}: {e}")
        return "error"


def sweep_wal_side_files() -> Dict[str, int]:
    """Startup sweep: release WAL side files of every character DB.

    Called once from initialize_backend after config migration (db_file_path
    final) and before anything opens a DB for real, so each close is the last
    connection. Returns the per-outcome counts (also logged as one line).
    """
    counts = {"released": 0, "missing": 0, "error": 0}
    t0 = time.monotonic()
    for _character_id, db_path in iter_character_dbs():
        counts[release_wal_side_files(db_path)] += 1
    elapsed_ms = (time.monotonic() - t0) * 1000
    logger.info(
        f"[DB housekeeping] WAL sweep: {counts['released']} released, "
        f"{counts['missing']} missing, {counts['error']} errors ({elapsed_ms:.0f} ms)")
    return counts
