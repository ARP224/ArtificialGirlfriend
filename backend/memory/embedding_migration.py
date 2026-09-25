# embedding_migration.py
"""
Re-embedding migration for long-term memories after an embedding-model change.

Each memory entry carries an ``embedding_model`` stamp ("provider::model",
ST6 §7-4 sequel). Search skips entries whose stamp differs from the currently
configured model (cross-model cosine similarity is silent garbage even when
the dimensions happen to match), so after a model switch the old memories are
dormant until they are re-embedded with the new model. Embeddings are derived
data — the content text is untouched — so the migration is lossless and
idempotent.

This module owns that migration:

- ``start_embedding_migration(state)`` scans every character's memory DB for
  entries whose stamp differs from the current model and, if any, launches a
  single background migration run. Called when the embedding model is saved
  in the settings UI and once at backend startup (resume after interruption —
  the stamp itself is the persisted migration state, so no extra bookkeeping
  is needed).

Safety design (agreed 2026-07-04):
- Writes take the character's OWN MemoryManager ``db_lock`` per item (read
  back -> verify -> write), so a running conversation waits at most one
  ms-level item and a user edit/delete during migration is never overwritten.
- Per-character work runs in a short-lived worker thread registered in
  ``state.background_tasks`` (task_type ``embedding_migration``), so the
  existing shutdown join and the remove_character in-progress guard apply.
- At most one migration run at a time; saving a different model mid-run
  cancels the current run and restarts toward the newest model (stamps make
  cancel/restart safe at any point).
- Conversation history messages are NOT migrated: their embeddings only feed
  the extraction centroid, new messages embed with the new model naturally,
  and the mismatch window self-resolves within one extraction cycle.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Optional

from backend.memory.db_housekeeping import iter_character_dbs

logger = logging.getLogger(__name__)

# Chunk size per embedding batch call (openai/xai embed a whole chunk per
# HTTP call inside MemoryManager._get_embeddings_batch; other providers fall
# back to sequential calls). Also the cancel/shutdown check granularity for
# the embedding phase.
_EMBED_CHUNK = 100

# Bounded politeness wait for a cancelled previous run before starting the
# next one. Overlap is safe regardless (per-item stamp/model re-verification),
# so this only avoids pointless duplicate embedding calls.
_PREV_RUN_JOIN_TIMEOUT = 120.0

# Single-runner state (module-level singleton).
_runner_lock = threading.Lock()
_runner: Optional[Dict[str, Any]] = None  # {"thread": Thread, "cancel": Event, "target": str}


def _count_pending_in_db(db_path: str, character_id: str, target_key: str) -> int:
    """Count memories not yet stamped with ``target_key`` (SELECT-only scan).

    Reads the kv_store directly instead of constructing a MemoryManager so the
    startup scan stays cheap (no store init / meta writes for idle characters).
    Mirrors SQLiteStore's two namespace serializations (compact + legacy
    spaced). Entries without usable content are excluded (nothing to re-embed).
    Opened as a normal (not ``mode=ro``) connection on purpose: a read-only
    connection creates the WAL side files (-wal/-shm) but can never retire
    them, which left 2 stray files per character after every startup.
    """
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            for ns in (
                json.dumps((character_id, "memories"), separators=(",", ":")),
                json.dumps((character_id, "memories")),
            ):
                try:
                    rows = conn.execute(
                        "SELECT value FROM kv_store WHERE namespace = ?", (ns,)
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                if not rows:
                    continue
                count = 0
                for (raw,) in rows:
                    try:
                        value = json.loads(raw)
                    except Exception:
                        continue
                    content = value.get("content")
                    if not (isinstance(content, str) and content.strip()):
                        continue
                    if value.get("embedding_model") != target_key:
                        count += 1
                return count
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[EmbeddingMigration] Pending scan failed for {character_id}: {e}")
    return 0


def _scan_pending(target_key: str) -> Dict[str, int]:
    """Map character_id -> count of memories awaiting migration to target_key."""
    pending = {}
    for character_id, db_path in iter_character_dbs():
        count = _count_pending_in_db(db_path, character_id, target_key)
        if count:
            pending[character_id] = count
    return pending


def _migrate_character(state, character_id: str, target_key: str,
                       cancel: threading.Event, totals: Dict[str, int]) -> None:
    """Re-embed and stamp one character's unmigrated memories (worker thread).

    Embedding calls run OUTSIDE the db_lock; each write re-acquires the lock,
    re-reads the entry and verifies it still exists, its content is unchanged
    and it is still unmigrated before overwriting — a user delete/edit during
    migration always wins.
    """
    from backend.conversation.character_manager import load_character_config
    from backend.memory.memory_manager import get_or_create_memory_manager

    try:
        memory_manager = get_or_create_memory_manager(state, character_id, load_character_config)
    except Exception as e:
        logger.warning(f"[EmbeddingMigration] MemoryManager init failed for {character_id}: {e}")
        totals["failed_characters"] += 1
        return
    if memory_manager is None or memory_manager.DISABLE_EMBEDDINGS:
        return

    memory_ns = (character_id, "memories")
    with memory_manager.db_lock:
        items = memory_manager.store.list(memory_ns)
        todo = [
            (item.key, item.value.get("content", ""))
            for item in items
            if item.value.get("embedding_model") != target_key
        ]
    todo = [(key, content) for key, content in todo
            if isinstance(content, str) and content.strip()]
    if not todo:
        return

    logger.info(
        f"[EmbeddingMigration] {character_id}: migrating {len(todo)} memories to {target_key}"
    )

    for start in range(0, len(todo), _EMBED_CHUNK):
        chunk = todo[start:start + _EMBED_CHUNK]
        if cancel.is_set() or state.shutdown_flag.is_set():
            logger.info(f"[EmbeddingMigration] {character_id}: stopped (cancel/shutdown)")
            return
        # Settings may have changed without going through start_embedding_migration
        # (belt to the cancel-event braces); the startup rescan finishes the rest.
        if memory_manager._current_embedding_key() != target_key:
            logger.info(
                f"[EmbeddingMigration] {character_id}: embedding model changed mid-run, stopping"
            )
            cancel.set()
            return

        try:
            embeddings = memory_manager._get_embeddings_batch([c for _, c in chunk])
        except Exception as e:
            logger.warning(
                f"[EmbeddingMigration] {character_id}: embedding batch failed "
                f"({len(chunk)} entries): {e}"
            )
            totals["failed"] += len(chunk)
            continue

        for (key, content), embedding in zip(chunk, embeddings):
            if cancel.is_set() or state.shutdown_flag.is_set():
                logger.info(f"[EmbeddingMigration] {character_id}: stopped (cancel/shutdown)")
                return
            try:
                embedding_list = memory_manager._validate_and_convert_embedding(embedding)
            except Exception as e:
                logger.warning(f"[EmbeddingMigration] {character_id}/{key}: invalid embedding: {e}")
                totals["failed"] += 1
                continue

            with memory_manager.db_lock:
                existing = memory_manager.store.get(memory_ns, key)
                if existing is None:
                    totals["skipped"] += 1  # deleted during migration — do not resurrect
                    continue
                value = existing.value
                if value.get("content") != content:
                    totals["skipped"] += 1  # edited during migration — edit re-embeds itself
                    continue
                if value.get("embedding_model") == target_key:
                    totals["skipped"] += 1  # already migrated (e.g. overlapping run)
                    continue
                data = value.copy()
                data["embedding"] = embedding_list
                data["embedding_model"] = target_key
                memory_manager.store.put(memory_ns, key, data)
                memory_manager.store.commit()
            totals["migrated"] += 1


def _run_migration(state, target_key: str, cancel: threading.Event,
                   prev_thread: Optional[threading.Thread]) -> None:
    """Coordinator thread: migrate characters one at a time."""
    if prev_thread is not None:
        prev_thread.join(timeout=_PREV_RUN_JOIN_TIMEOUT)

    totals = {"migrated": 0, "failed": 0, "skipped": 0, "failed_characters": 0}
    characters = 0
    try:
        for character_id, db_path in iter_character_dbs():
            if cancel.is_set() or state.shutdown_flag.is_set():
                logger.info("[EmbeddingMigration] Run stopped (cancel/shutdown)")
                return
            if _count_pending_in_db(db_path, character_id, target_key) == 0:
                continue
            characters += 1
            worker = threading.Thread(
                target=_migrate_character,
                args=(state, character_id, target_key, cancel, totals),
                name=f"embedding-migration-{character_id}",
                daemon=False,
            )
            # Registered before start so the remove_character guard and the
            # shutdown join see it; the entry dies with the worker thread.
            state.add_background_task({
                'thread': worker,
                'character_id': character_id,
                'task_type': 'embedding_migration',
                'start_time': time.time(),
            })
            worker.start()
            worker.join()
    finally:
        level = logging.WARNING if (totals["failed"] or totals["failed_characters"]) else logging.INFO
        logger.log(
            level,
            f"[EmbeddingMigration] Run finished: model={target_key} "
            f"characters={characters} migrated={totals['migrated']} "
            f"skipped={totals['skipped']} failed={totals['failed']} "
            f"failed_characters={totals['failed_characters']}"
            + (" — failures retry at next startup or model re-save"
               if (totals["failed"] or totals["failed_characters"]) else "")
        )


def start_embedding_migration(state) -> Dict[str, Any]:
    """Scan for unmigrated memories and start/restart the background migration.

    Returns {"success": True, "pending": N, "started": bool, "model": key}
    (pending counted synchronously — cheap read-only DB scan — so the settings
    UI can report "migration started (N entries)"). At most one run at a time:
    a run toward a different model is cancelled and superseded; a run already
    toward the current model is left alone.
    """
    from backend.shared.api_settings import get_embedding_model, encode_model_value

    embedding_model = get_embedding_model()
    if embedding_model is None:
        # 未設定(デフォルト廃止 2026-07-25): 移行先が無いのでno-op。
        return {"success": True, "pending": 0, "started": False, "model": None}
    provider, model_name = embedding_model
    target_key = encode_model_value(provider, model_name)

    pending = _scan_pending(target_key)
    total = sum(pending.values())

    global _runner
    with _runner_lock:
        prev = _runner
        prev_alive = prev is not None and prev["thread"].is_alive()
        if prev_alive and prev["target"] == target_key:
            return {"success": True, "pending": total, "started": False,
                    "already_running": True, "model": target_key}
        if prev_alive:
            prev["cancel"].set()
        if total == 0:
            return {"success": True, "pending": 0, "started": False, "model": target_key}

        cancel = threading.Event()
        thread = threading.Thread(
            target=_run_migration,
            args=(state, target_key, cancel, prev["thread"] if prev_alive else None),
            name="embedding-migration",
            daemon=False,
        )
        _runner = {"thread": thread, "cancel": cancel, "target": target_key}
        thread.start()

    logger.info(
        f"[EmbeddingMigration] Started: model={target_key} pending={total} "
        f"characters={len(pending)}"
    )
    return {"success": True, "pending": total, "started": True, "model": target_key}
