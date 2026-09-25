# memory_manager_revised.py
"""
Memory management module for conversation history and summaries.

This module provides persistent storage and retrieval of conversation history,
with support for short-term memory (recent messages) and long-term memory (summaries).
It uses SQLite for persistence and implements semantic search capabilities.

REVISED VERSION: Includes fixes for concurrent access, proper error handling,
embedding validation, transaction rollback, and atomic operations.
"""

import logging
import re
import os
import time
import shutil
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from typing import List, Dict, Any, Optional, Set, Tuple, Union
from threading import RLock, Lock
from pathlib import Path

# Protect critical imports with helpful error messages
try:
    import numpy as np
except ImportError as e:
    raise ImportError(
        "Memory manager requires numpy for embeddings support. "
        "Please install with: pip install numpy"
    )

try:
    import sqlite3
except ImportError as e:
    raise ImportError(
        "Memory manager requires sqlite3 for database support. "
        "This should be included with Python. If missing, please reinstall Python."
    )

# Import constants from centralized location
from backend.shared.constants import (
    MEMORY_DIR,
    resolve_data_path, to_repo_relative,
    EXTRACTION_THRESHOLD_OLLAMA, EXTRACTION_THRESHOLD_API,
    EXTRACTION_MAX_FAILURES, EXTRACTION_FAILURE_COOLDOWN,
    MAX_MEMORY_ENTRIES,
    MEMORY_TOKEN_BUDGET_OLLAMA, MEMORY_TOKEN_BUDGET_API,
    MEMORY_SEARCH_TOP_K, MEMORY_RELATED_EXISTING_TOP_K,
    MAX_MESSAGE_RETENTION, MEMORY_CATEGORIES,
    DEFAULT_EMBEDDING_SIZE,
    EMBEDDING_RETRY_DELAY, EMBEDDING_RETRY_LIMIT,
    LOCK_TIMEOUT, USE_UTC_TIMESTAMPS,
    OLLAMA_GENERATION_TIMEOUT
)

# Optional imports - will be lazy-loaded when needed
# from backend.llm.ollama_integration import create_embedding_ollama, create_chat_ollama, cosine_similarity

# Timing logger for performance measurement
from backend.shared.timing_logger import timing_log


# Language-specific extraction prompts
# Helper function for consistent timestamps
def _get_timestamp() -> str:
    """Get current timestamp in ISO format, using UTC if configured."""
    if USE_UTC_TIMESTAMPS:
        return datetime.now(timezone.utc).isoformat()
    else:
        return datetime.now().isoformat()


# Custom exceptions for better error handling
class ExtractionError(Exception):
    """Raised when memory extraction fails."""
    pass


class EmbeddingError(Exception):
    """Raised when embedding generation or validation fails."""
    pass


# Integrated storage adapter classes
class Item:
    """Simple item wrapper to match expected API."""
    def __init__(self, key: str, value: Dict[str, Any]):
        self.key = key
        self.value = value


class MemoryStore:
    """
    Optimized SQLite-backed key-value store with namespace support.
    
    This provides thread-safe persistence with proper error handling,
    optimized for the MemoryManager's usage patterns.
    
    Key optimizations:
    - Thread-local connections to avoid lock contention
    - Connection pooling to reduce overhead
    - Prepared statements for better performance
    - Automatic retry on transient errors
    """
    
    def __init__(self, db_path: str):
        """Initialize the optimized SQLite store."""
        self.db_path = db_path
        self._local = threading.local()
        # RLock: transaction() がブロック全体で保持したまま、内側の put/delete
        # が再取得するため(plain Lock だと自己デッドロック)
        self._write_lock = RLock()  # Only for write operations
        # 接続台帳 [(所有スレッド, 接続)]。スレッドローカル接続は「作ったスレッド
        # しか閉じられない」ため、終了時/キャラ削除時に全接続を閉じて SQLite に
        # WAL 付随ファイル(-wal/-shm)を片付けさせるには台帳が要る(close_all)。
        # 強参照なので、新規接続時に所有スレッドが終了した接続を閉じて外す
        # (prune)＝従来の「スレッド終了→GC 解放」と同じ寿命を保ち、抽出タスク
        # ごとの短命スレッドが接続を溜め込まない。
        self._conns: List[Tuple[threading.Thread, sqlite3.Connection]] = []
        self._conns_lock = Lock()
        self._closed = False
        
        # Ensure directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database
        self._init_db()
        
        # Pre-compile namespace serialization for common cases
        self._ns_cache: Dict[Tuple[str, ...], str] = {}
        
    def _init_db(self):
        """Initialize the database schema with optimizations."""
        conn = self._get_conn()
        with conn:
            # Create the key-value store table with optimized indexes
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_store (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
            """)
            # Create index for namespace queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_namespace 
                ON kv_store(namespace)
            """)
            # Enable optimizations
            conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging
            conn.execute("PRAGMA synchronous=NORMAL")  # Balanced durability
            conn.execute("PRAGMA cache_size=10000")  # Larger cache
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection with retry logic."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            if self._closed:
                raise sqlite3.ProgrammingError("MemoryStore is closed")
            # Create new connection with optimizations
            for attempt in range(3):
                try:
                    conn = sqlite3.connect(
                        self.db_path,
                        isolation_level=None,  # Autocommit mode
                        timeout=30.0,
                        # 台帳経由で他スレッドから close_all/prune するための許可。
                        # 使い方は従来どおりスレッドローカル(1スレッド1接続・共有しない)
                        check_same_thread=False,
                    )
                    # Enable query optimizations
                    conn.execute("PRAGMA temp_store=MEMORY")
                    conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory map
                    self._register_conn(conn)
                    self._local.conn = conn
                    break
                except sqlite3.OperationalError as e:
                    if attempt == 2:
                        raise
                    time.sleep(0.1 * (attempt + 1))
        return self._local.conn

    def _register_conn(self, conn: sqlite3.Connection) -> None:
        """台帳へ登録し、ついでに所有スレッドが終了した接続を閉じて外す(prune)。"""
        me = threading.current_thread()
        with self._conns_lock:
            if self._closed:
                conn.close()
                raise sqlite3.ProgrammingError("MemoryStore is closed")
            live = []
            for owner, c in self._conns:
                if owner.is_alive():
                    live.append((owner, c))
                else:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
            live.append((me, conn))
            self._conns = live
    
    def _serialize_namespace(self, namespace: Tuple[str, ...]) -> str:
        """Serialize namespace with caching for common cases."""
        if namespace not in self._ns_cache:
            if len(self._ns_cache) > 100:  # Limit cache size
                self._ns_cache.clear()
            self._ns_cache[namespace] = json.dumps(namespace, separators=(',', ':'))
        return self._ns_cache[namespace]
    
    def _get_alternate_namespace(self, namespace: Tuple[str, ...]) -> str:
        """Get alternate namespace format for backward compatibility.
        
        Returns the namespace with default JSON formatting (spaces after commas)
        to handle legacy data that might have been stored with different formatting.
        """
        return json.dumps(namespace)  # Default format with spaces
    
    def _execute_with_retry(self, conn: sqlite3.Connection, query: str, params: tuple, 
                          max_retries: int = 3) -> sqlite3.Cursor:
        """Execute query with automatic retry on lock errors."""
        for attempt in range(max_retries):
            try:
                return conn.execute(query, params)
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.05 * (attempt + 1))  # Exponential backoff
                else:
                    raise
    
    def put(self, namespace: Tuple[str, ...], key: str, value: Dict[str, Any]) -> None:
        """Store a value with optimized write path."""
        ns_str = self._serialize_namespace(namespace)
        value_str = json.dumps(value, separators=(',', ':'))  # Compact JSON
        
        with self._write_lock:  # Only lock for writes
            conn = self._get_conn()
            self._execute_with_retry(
                conn,
                "INSERT OR REPLACE INTO kv_store (namespace, key, value) VALUES (?, ?, ?)",
                (ns_str, key, value_str)
            )
    
    def get(self, namespace: Tuple[str, ...], key: str) -> Optional[Item]:
        """Retrieve a value with read optimization and backward compatibility."""
        ns_str = self._serialize_namespace(namespace)
        conn = self._get_conn()  # No lock needed for reads
        
        cursor = self._execute_with_retry(
            conn,
            "SELECT value FROM kv_store WHERE namespace = ? AND key = ?",
            (ns_str, key)
        )
        
        row = cursor.fetchone()
        if row:
            value = json.loads(row[0])
            return Item(key, value)
        
        # Try alternate namespace format for backward compatibility
        ns_alt = self._get_alternate_namespace(namespace)
        if ns_alt != ns_str:  # Only try if formats differ
            cursor = self._execute_with_retry(
                conn,
                "SELECT value FROM kv_store WHERE namespace = ? AND key = ?",
                (ns_alt, key)
            )
            row = cursor.fetchone()
            if row:
                value = json.loads(row[0])
                return Item(key, value)
        
        return None
    
    def delete(self, namespace: Tuple[str, ...], key: str) -> None:
        """Delete a value with write lock and backward compatibility."""
        ns_str = self._serialize_namespace(namespace)
        
        with self._write_lock:
            conn = self._get_conn()
            # Delete from primary namespace format
            self._execute_with_retry(
                conn,
                "DELETE FROM kv_store WHERE namespace = ? AND key = ?",
                (ns_str, key)
            )
            
            # Also delete from alternate namespace format for cleanup
            ns_alt = self._get_alternate_namespace(namespace)
            if ns_alt != ns_str:
                self._execute_with_retry(
                    conn,
                    "DELETE FROM kv_store WHERE namespace = ? AND key = ?",
                    (ns_alt, key)
                )
    
    def has(self, namespace: Tuple[str, ...], key: str) -> bool:
        """Check existence with optimized query and backward compatibility."""
        ns_str = self._serialize_namespace(namespace)
        conn = self._get_conn()
        
        cursor = self._execute_with_retry(
            conn,
            "SELECT 1 FROM kv_store WHERE namespace = ? AND key = ? LIMIT 1",
            (ns_str, key)
        )
        
        if cursor.fetchone() is not None:
            return True
        
        # Try alternate namespace format for backward compatibility
        ns_alt = self._get_alternate_namespace(namespace)
        if ns_alt != ns_str:  # Only try if formats differ
            cursor = self._execute_with_retry(
                conn,
                "SELECT 1 FROM kv_store WHERE namespace = ? AND key = ? LIMIT 1",
                (ns_alt, key)
            )
            return cursor.fetchone() is not None
        
        return False
    
    def list(self, namespace: Tuple[str, ...]) -> List[Item]:
        """List items with streaming for large namespaces and backward compatibility."""
        ns_str = self._serialize_namespace(namespace)
        conn = self._get_conn()
        
        cursor = self._execute_with_retry(
            conn,
            "SELECT key, value FROM kv_store WHERE namespace = ? ORDER BY key",
            (ns_str,)
        )
        
        items = []
        for row in cursor:
            key, value_str = row
            value = json.loads(value_str)
            items.append(Item(key, value))
        
        # If no items found, try alternate namespace format for backward compatibility
        if not items:
            ns_alt = self._get_alternate_namespace(namespace)
            if ns_alt != ns_str:  # Only try if formats differ
                cursor = self._execute_with_retry(
                    conn,
                    "SELECT key, value FROM kv_store WHERE namespace = ? ORDER BY key",
                    (ns_alt,)
                )
                
                for row in cursor:
                    key, value_str = row
                    value = json.loads(value_str)
                    items.append(Item(key, value))
        
        return items
    
    def list_keys(self, namespace: Tuple[str, ...]) -> List[str]:
        """List only the keys in a namespace (no value decode).

        メッセージ値は embedding JSON 込みで重い。件数選別だけならキーで済む
        (msg_ キーの数値順ソートは呼び出し側の責務: 'msg_10' < 'msg_2' となる
        ため SQL の ORDER BY key は数値順にならない)。
        """
        ns_str = self._serialize_namespace(namespace)
        conn = self._get_conn()

        cursor = self._execute_with_retry(
            conn,
            "SELECT key FROM kv_store WHERE namespace = ?",
            (ns_str,)
        )
        keys = [row[0] for row in cursor]

        # If no keys found, try alternate namespace format for backward compatibility
        if not keys:
            ns_alt = self._get_alternate_namespace(namespace)
            if ns_alt != ns_str:
                cursor = self._execute_with_retry(
                    conn,
                    "SELECT key FROM kv_store WHERE namespace = ?",
                    (ns_alt,)
                )
                keys = [row[0] for row in cursor]

        return keys

    def commit(self) -> None:
        """Force a checkpoint in WAL mode."""
        conn = self._get_conn()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def begin_transaction(self) -> str:
        """Begin a new transaction and return transaction ID."""
        return str(uuid4())

    @contextmanager
    def transaction(self):
        """Run the enclosed writes as one atomic SQLite transaction.

        接続は autocommit(isolation_level=None)だが、手動 BEGIN で明示
        トランザクションになる。ブロック中は _write_lock を保持(RLock
        なので内側の put/delete は再入可)。例外時は ROLLBACK して再送出。
        同スレッドの read は自分の未コミット書き込みを見る(同一接続)。
        トランザクション内で commit()(WALチェックポイント)は呼ばないこと。
        """
        with self._write_lock:
            conn = self._get_conn()
            self._execute_with_retry(conn, "BEGIN IMMEDIATE", ())
            try:
                yield
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # 元例外を隠さない
                raise
    
    
    def close(self):
        """Close the thread-local connection if it exists."""
        conn = getattr(self._local, 'conn', None)
        if conn:
            with self._conns_lock:
                self._conns = [(o, c) for o, c in self._conns if c is not conn]
            conn.close()
            self._local.conn = None

    def close_all(self) -> int:
        """Close every connection this store opened (all threads) and mark the
        store closed; further use raises sqlite3.ProgrammingError.

        Purpose: SQLite retires the WAL side files (-wal/-shm) only when the
        LAST connection to the DB closes — so this is what makes them vanish
        at shutdown, and what lets remove_character delete the .db on Windows
        (an open file cannot be deleted there). Returns the number closed.
        """
        with self._conns_lock:
            self._closed = True
            conns = [c for _, c in self._conns]
            self._conns = []
        closed = 0
        for c in conns:
            try:
                c.close()
                closed += 1
            except sqlite3.Error as e:
                logging.warning(f"[MemoryStore] close_all failed for a connection: {e}")
        self._local.conn = None
        return closed


class MemoryManager:
    """
    Manages conversation memory for a character with both short-term (recent messages)
    and long-term (summarized) memory capabilities.
    
    Features:
    - SQLite-based persistent storage
    - Automatic summarization after message threshold
    - Semantic search using embeddings
    - Thread-safe operations
    - Transaction support with rollback
    """
    
    # Allow disabling embeddings for testing
    DISABLE_EMBEDDINGS = False

    def __init__(self, db_file_path: str, character_id: str):
        """
        Initialize the MemoryManager for a specific character and .db file.

        Args:
            db_file_path: Path to the SQLite database file (e.g. "memory/girlfriend1.db")
            character_id: Unique ID for the character (matches config)
            
        Raises:
            ValueError: If db_file_path is empty or character_id is invalid
            RuntimeError: If database initialization fails critically
        """
        if not db_file_path:
            raise ValueError("Database file path cannot be empty")
        if not character_id:
            raise ValueError("Character ID cannot be empty")
            
        self.character_id = character_id
        self.db_file_path = db_file_path
        
        # CRITICAL: Set expected_embedding_size FIRST before any potential errors
        self.expected_embedding_size = DEFAULT_EMBEDDING_SIZE
        
        # Add a reentrant lock for thread safety during critical operations
        self.db_lock = RLock()
        self.extraction_lock = RLock()

        # Circuit breaker for extraction failures
        self.extraction_failures = 0
        self.last_extraction_failure = 0

        # Check if database exists and verify its integrity
        self._check_db_integrity()

        try:
            # Initialize our optimized SQLite-backed store
            # This provides a thread-safe key-value interface with SQLite persistence
            self.store = MemoryStore(db_file_path)

            # Initialize embedding model (will be lazy-loaded when needed)
            self.embedding_model = None
            self._embedding_model_name = None  # Track which model is loaded
            
            # expected_embedding_size is already set at the beginning of __init__
            
            # Ensure meta data is initialized (uses expected_embedding_size)
            self._init_meta()
            
        except Exception as e:
            logging.error(f"[MemoryManager] Failed to initialize memory manager: {e}")
            # Check if database might be corrupted
            if "database disk image is malformed" in str(e) or "database or disk is full" in str(e):
                logging.warning(f"[MemoryManager] Database corruption detected during initialization: {e}")
                # Try to create a backup and reinitialize
                # UI should show a user-friendly "recovering memory" message during recovery
                self._backup_and_reinitialize()
            else:
                raise RuntimeError(f"Failed to initialize MemoryManager: {e}")

    def _check_db_integrity(self):
        """Check database integrity and attempt repairs if needed."""
        if not os.path.exists(self.db_file_path):
            logging.info(f"[MemoryManager] Creating new database at {self.db_file_path}")
            return
            
        corruption_confirmed = False
        try:
            conn = sqlite3.connect(self.db_file_path)
            try:
                # Run integrity check
                cursor = conn.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                if result and result[0] != "ok":
                    logging.error(f"[MemoryManager] Database integrity check failed: {result}")
                    corruption_confirmed = True
            finally:
                conn.close()
        except Exception as e:
            # 破損確定(integrity_check NG / ファイルがDBでない / malformed)のときだけ
            # 隔離する。"database is locked" 等の一時障害で本番DBを消さない。
            msg = str(e).lower()
            if isinstance(e, sqlite3.DatabaseError) and (
                "malformed" in msg or "not a database" in msg
            ):
                logging.error(f"[MemoryManager] Database corruption detected: {e}")
                corruption_confirmed = True
            else:
                logging.warning(
                    f"[MemoryManager] Integrity check skipped (transient error, DB kept): {e}"
                )
                return

        if corruption_confirmed:
            # Backup corrupted database
            backup_path = f"{self.db_file_path}.corrupted.{int(time.time())}"
            shutil.copy2(self.db_file_path, backup_path)
            logging.warning(f"[MemoryManager] Backed up corrupted database to {backup_path}")
            # Remove corrupted database to start fresh
            os.remove(self.db_file_path)

    def _backup_and_reinitialize(self):
        """Backup corrupted database and create a new one."""
        
        # CRITICAL FIX: Set expected_embedding_size FIRST
        if not hasattr(self, 'expected_embedding_size'):
            self.expected_embedding_size = DEFAULT_EMBEDDING_SIZE
        
        timestamp = int(time.time())
        backup_path = f"{self.db_file_path}.backup.{timestamp}"
        
        try:
            if os.path.exists(self.db_file_path):
                shutil.copy2(self.db_file_path, backup_path)
                logging.info(f"[MemoryManager] Backed up database to {backup_path}")
                # 旧ストアの接続を全部閉じてから消す(Windows は開いたままの
                # ファイルを削除できず、旧 .db が孤児化していた)
                old_store = getattr(self, 'store', None)
                if old_store is not None:
                    old_store.close_all()
                os.remove(self.db_file_path)
            
            # Reinitialize with fresh database
            self.store = MemoryStore(self.db_file_path)
            self._init_meta()
            
        except Exception as e:
            logging.error(f"[MemoryManager] Failed to backup and reinitialize: {e}")
            raise RuntimeError(f"Critical database failure: {e}")

    def _init_meta(self):
        """Initialize metadata for the character if not present."""
        meta_ns = (self.character_id, "meta")

        # Initialize schema version for consistency checking
        if not self.store.has(meta_ns, "schema_version"):
            self.store.put(meta_ns, "schema_version", {"version": "2.0"})
            self.store.commit()

        # Initialize message count
        if not self.store.has(meta_ns, "msg_count"):
            self.store.put(meta_ns, "msg_count", {"count": 0})
            self.store.commit()

        # Initialize memory count
        if not self.store.has(meta_ns, "memory_count"):
            self.store.put(meta_ns, "memory_count", {"count": 0})
            self.store.commit()

        # Initialize monotonic memory key allocator.
        # memory_count は削除/cleanup で減算される「件数」なので mem_ キーの採番には
        # 使えない(count から採番すると INSERT OR REPLACE が既存記憶を上書きする)。
        # 既存DBの移行: 現存する最大 mem_ 番号から導出する。
        if not self.store.has(meta_ns, "next_memory_id"):
            max_index = 0
            for item in self.store.list((self.character_id, "memories")):
                idx = self._extract_mem_index(item.key)
                if idx is not None and idx > max_index:
                    max_index = idx
            self.store.put(meta_ns, "next_memory_id", {"index": max_index + 1})
            self.store.commit()

        # Initialize last extraction index
        if not self.store.has(meta_ns, "last_extracted"):
            self.store.put(meta_ns, "last_extracted", {"index": 0})
            self.store.commit()

        # Initialize deleted_up_to watermark
        if not self.store.has(meta_ns, "deleted_up_to"):
            self.store.put(meta_ns, "deleted_up_to", {"index": 0})
            self.store.commit()

        # Initialize embedding_stripped_up_to watermark
        if not self.store.has(meta_ns, "embedding_stripped_up_to"):
            self.store.put(meta_ns, "embedding_stripped_up_to", {"index": 0})
            self.store.commit()

        # Initialize embedding configuration tracking
        if not self.store.has(meta_ns, "embedding_config"):
            self.store.put(meta_ns, "embedding_config", {
                "model": "unknown",
                "dimension": self.expected_embedding_size,
                "version": 1,
                "last_updated": _get_timestamp()
            })
            self.store.commit()

        # Run migration if needed
        self._migrate_to_v2()

    def _migrate_to_v2(self):
        """Migrate from schema v1.0 (summarization) to v2.0 (memory extraction).

        Idempotent: only runs when schema_version is "1.0".
        """
        meta_ns = (self.character_id, "meta")
        version_item = self.store.get(meta_ns, "schema_version")
        if not version_item:
            return
        current_version = version_item.value.get("version", "1.0")
        if current_version != "1.0":
            return

        logging.info(f"[MemoryManager] Migrating character {self.character_id} from schema v1.0 to v2.0")

        try:
            # 1. Delete all old summaries
            summary_ns = (self.character_id, "summaries")
            all_summaries = self.store.list(summary_ns)
            for item in all_summaries:
                self.store.delete(summary_ns, item.key)

            # 2. Delete chunk_count metadata
            if self.store.has(meta_ns, "chunk_count"):
                self.store.delete(meta_ns, "chunk_count")

            # 3. Delete last_summarized
            if self.store.has(meta_ns, "last_summarized"):
                self.store.delete(meta_ns, "last_summarized")

            # 4. Set last_extracted to current msg_count (avoid mass re-extraction)
            msg_count = self.get_message_count()
            self.store.put(meta_ns, "last_extracted", {"index": msg_count})

            # 5. Initialize memory_count
            self.store.put(meta_ns, "memory_count", {"count": 0})

            # 6. Initialize deleted_up_to watermark
            self.store.put(meta_ns, "deleted_up_to", {"index": 0})

            # 7. Update schema version
            self.store.put(meta_ns, "schema_version", {"version": "2.0"})

            self.store.commit()
            logging.info(f"[MemoryManager] Migration to v2.0 completed for character {self.character_id}")

        except Exception as e:
            logging.error(f"[MemoryManager] Migration to v2.0 failed for character {self.character_id}: {e}")
            # Don't update schema_version — next startup will retry
            try:
                # Attempt to rollback by recommitting unchanged state
                self.store.commit()
            except Exception:
                pass

    def get_message_count(self) -> int:
        """Get the total number of messages for this character."""
        meta_ns = (self.character_id, "meta")
        
        with self.db_lock:
            msg_count_item = self.store.get(meta_ns, "msg_count")
            stored_count = msg_count_item.value.get("count", 0) if msg_count_item else 0
            
            # Periodic consistency check (every 100 calls).
            # msg_count はキー採番(最大インデックス)であり実件数ではない。
            # cleanup_old_messages が古いメッセージを物理削除するため
            # 実件数 = msg_count - deleted_up_to が正常な関係。ここで msg_count を
            # 実件数へ書き戻すと次の add_message が既存キーを INSERT OR REPLACE で
            # 上書きするため、検知しても警告のみで修正しない。
            if stored_count > 0 and stored_count % 100 == 0:
                try:
                    message_ns = (self.character_id, "messages")
                    actual_count = len(self.store.list(message_ns))

                    deleted_item = self.store.get(meta_ns, "deleted_up_to")
                    deleted_up_to = deleted_item.value.get("index", 0) if deleted_item else 0
                    expected_count = stored_count - deleted_up_to

                    if actual_count != expected_count:
                        logging.warning(
                            f"[MemoryManager] Message count mismatch detected: "
                            f"stored={stored_count}, deleted_up_to={deleted_up_to}, "
                            f"expected={expected_count}, actual={actual_count}"
                        )
                except Exception as e:
                    logging.error(f"[MemoryManager] Error during consistency check: {e}")

            return stored_count


    def _extract_msg_index(self, key: str) -> Optional[int]:
        """Extract the numeric index from a message key like 'msg_123'."""
        match = re.match(r"msg_(\d+)", key)
        if match:
            return int(match.group(1))
        return None

    def _extract_mem_index(self, key: str) -> Optional[int]:
        """Extract the numeric index from a memory key like 'mem_123'."""
        match = re.match(r"mem_(\d+)", key)
        if match:
            return int(match.group(1))
        return None

    def _allocate_memory_index(self) -> int:
        """Allocate the next mem_ key index. Caller must hold db_lock.

        採番は単調増加の next_memory_id を使う。件数(memory_count)は削除で
        減算されるため採番に流用すると既存キーと衝突する。
        """
        meta_ns = (self.character_id, "meta")
        item = self.store.get(meta_ns, "next_memory_id")
        if item:
            next_index = item.value.get("index", 1)
        else:
            # _init_meta 以前のDBに対する保険: 現存最大番号から導出
            max_index = 0
            for it in self.store.list((self.character_id, "memories")):
                idx = self._extract_mem_index(it.key)
                if idx is not None and idx > max_index:
                    max_index = idx
            next_index = max_index + 1
        self.store.put(meta_ns, "next_memory_id", {"index": next_index + 1})
        return next_index

    def get_recent_messages(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Retrieve recent messages regardless of any flags.

        Args:
            limit: Maximum number of messages to return

        Returns:
            List of message dictionaries in chronological order
        """
        if not isinstance(limit, int) or limit <= 0:
            logging.error(f"[MemoryManager] Invalid limit parameter: {limit}. Must be a positive integer.")
            return []

        message_ns = (self.character_id, "messages")

        with self.db_lock:
            try:
                # キーだけ先に取り、末尾 limit 件の値のみロードする
                # (全値ロードは embedding JSON 込み最大5000件のデコードになる)。
                indexed_keys = []
                for key in self.store.list_keys(message_ns):
                    idx = self._extract_msg_index(key)
                    if idx is not None:
                        indexed_keys.append((idx, key))

                indexed_keys.sort(key=lambda x: x[0])

                if limit < len(indexed_keys):
                    indexed_keys = indexed_keys[-limit:]

                messages = []
                for _, key in indexed_keys:
                    item = self.store.get(message_ns, key)
                    if item is not None:
                        # Stored attachment paths are repo-relative (add_message);
                        # consumers (prompt_builder / UI history) expect absolute.
                        # item.value is a fresh dict per get — in-place is safe.
                        value = item.value
                        if value.get("images"):
                            value["images"] = [resolve_data_path(p) for p in value["images"]]
                        for doc in value.get("documents") or []:
                            if isinstance(doc, dict) and doc.get("file_path"):
                                doc["file_path"] = resolve_data_path(doc["file_path"])
                        messages.append(value)
                return messages

            except Exception as e:
                logging.error(f"[MemoryManager] Error retrieving recent messages: {e}")
                return []

    def get_messages_for_prompt(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get messages specifically for prompt building.

        Args:
            limit: Maximum number of recent messages to return (default: 20)

        Returns:
            List of most recent messages in chronological order
        """
        return self.get_recent_messages(limit=limit)
    
    # ----------------------------------------------------------------
    # Memory retrieval for prompt (replaces get_relevant_summaries_for_prompt)
    # ----------------------------------------------------------------

    def get_memories_for_prompt(self, user_text: str, provider: str) -> List[Dict[str, Any]]:
        """
        Get relevant memory entries for prompt inclusion via semantic search.

        Args:
            user_text: Current user input for semantic search
            provider: Model provider ('ollama', 'openai', etc.)

        Returns:
            List of dicts: [{"category": str, "content": str, "id": str}]
        """
        if not user_text or not user_text.strip():
            return []

        try:
            results = self.search_memories_by_similarity(user_text, top_k=MEMORY_SEARCH_TOP_K)

            # Filter by minimum relevance, calibrated per embedding model
            # (a fixed threshold silently disabled retrieval on models whose
            # cosine scale differs from nomic-embed-text — ST6 §7-4)
            from backend.shared.api_settings import get_memory_relevance_threshold
            threshold = get_memory_relevance_threshold()
            relevant = [r for r in results if r.get("similarity", 0) >= threshold]

            # Determine token budget
            from backend.shared.token_manager import estimate_token_count
            is_api = provider != 'ollama'
            token_budget = MEMORY_TOKEN_BUDGET_API if is_api else MEMORY_TOKEN_BUDGET_OLLAMA

            selected = []
            used_tokens = 0
            selected_ids = []

            for entry in relevant:
                formatted = f"[{entry.get('category', 'unknown')}] {entry.get('content', '')}"
                entry_tokens = estimate_token_count(formatted)
                if used_tokens + entry_tokens > token_budget:
                    break
                selected.append({
                    "category": entry.get("category", "unknown"),
                    "content": entry.get("content", ""),
                    "id": entry.get("id", "")
                })
                selected_ids.append(entry.get("id", ""))
                used_tokens += entry_tokens

            # Update last_retrieved timestamps
            if selected_ids:
                self.update_last_retrieved(selected_ids)

            return selected

        except Exception as e:
            logging.error(f"[MemoryManager] Error getting memories for prompt: {e}")
            return []

    def search_memories_by_similarity(self, query, top_k: int = 50) -> List[Dict[str, Any]]:
        """
        Search memory entries by semantic similarity.

        Args:
            query: Search query — either a text string or an embedding vector (list/ndarray)
            top_k: Number of top results to return

        Returns:
            List of memory entries with similarity scores, sorted descending
        """
        try:
            # Get query embedding
            if isinstance(query, str):
                if not query.strip():
                    return []
                query_embedding = self._get_embedding(query)
            elif isinstance(query, (list, np.ndarray)):
                query_embedding = np.array(query, dtype=np.float32)
            else:
                logging.error(f"[MemoryManager] Invalid query type for similarity search: {type(query)}")
                return []

            if query_embedding is None or np.isnan(query_embedding).any():
                return []

            memory_ns = (self.character_id, "memories")
            all_memories = self.store.list(memory_ns)
            current_model_key = self._current_embedding_key()

            results = []
            for item in all_memories:
                emb = item.value.get("embedding")
                if emb is None:
                    continue
                # Unmigrated stamp (missing, or from another embedding model)
                # -> skip: cross-model cosine is garbage even when dimensions
                # match (ST6 §7-4 sequel; the migration runner re-embeds and
                # stamps, after which the memory rejoins the candidates).
                if item.value.get("embedding_model") != current_model_key:
                    continue
                mem_embedding = np.array(emb, dtype=np.float32)
                if len(mem_embedding) != len(query_embedding):
                    continue  # defensive: malformed stored embedding

                similarity = self._cosine_similarity(query_embedding, mem_embedding)
                entry = item.value.copy()
                entry["id"] = item.key
                entry["similarity"] = float(similarity)
                results.append(entry)

            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:top_k]

        except Exception as e:
            logging.error(f"[MemoryManager] Error in memory similarity search: {e}")
            return []

    def multi_query_search(
        self,
        queries: List[str],
        threshold: Optional[float] = None,
        token_budget: int = MEMORY_TOKEN_BUDGET_API,
        categories: Optional[List[str]] = None,
    ) -> List[str]:
        """Per-query semantic search over memories, merged by max similarity.

        Designed for ELYTH per-post RAG: each query (one post/notification body)
        is embedded, every memory scores against ALL queries, and each memory's
        retained score is the MAX across queries (so an unrelated post can't
        dilute a strong single-post match). Memories with max-similarity >=
        threshold are returned (highest first) up to token_budget, formatted as
        "[category] content".

        Args:
            queries: List of query texts (post/notification bodies).
            threshold: Minimum cosine similarity to retain a memory. None
                (default) resolves the calibrated threshold for the configured
                embedding model (ST6 §7-4).
            token_budget: Max tokens of formatted memory text to return.
            categories: When given, only memories whose `category` is in this
                collection are searched (ELYTH "activity_only" RAG mode,
                ELYTH integration spec v6 §8, internal design doc). None = all memories
                (behavior unchanged for every other caller).

        Returns:
            List of formatted "[category] content" strings (possibly empty).
            Returns [] on any error.
        """
        try:
            if threshold is None:
                from backend.shared.api_settings import get_memory_relevance_threshold
                threshold = get_memory_relevance_threshold()

            clean = [q.strip() for q in queries if q and q.strip()]
            if not clean:
                return []

            # Defensive cap to avoid pathological batch sizes (logged, not silent)
            MAX_QUERIES = 512
            if len(clean) > MAX_QUERIES:
                logging.warning(
                    f"[MemoryManager] multi_query_search: capping queries {len(clean)} -> {MAX_QUERIES}"
                )
                clean = clean[:MAX_QUERIES]

            query_embs = self._get_embeddings_batch(clean)
            if not query_embs:
                return []
            query_dim = len(query_embs[0])

            memory_ns = (self.character_id, "memories")
            all_memories = self.store.list(memory_ns)
            current_model_key = self._current_embedding_key()

            mem_rows = []
            mem_entries = []  # parallel: (id, category, content)
            for item in all_memories:
                if (categories is not None
                        and item.value.get("category", "unknown") not in categories):
                    continue
                emb = item.value.get("embedding")
                if emb is None:
                    continue
                # Unmigrated stamp -> skip (see search_memories_by_similarity)
                if item.value.get("embedding_model") != current_model_key:
                    continue
                arr = np.array(emb, dtype=np.float32)
                if len(arr) != query_dim:
                    continue  # defensive: malformed stored embedding
                mem_rows.append(arr)
                mem_entries.append((
                    item.key,
                    item.value.get("category", "unknown"),
                    item.value.get("content", ""),
                ))

            if not mem_rows:
                return []

            mem_mat = np.vstack(mem_rows)        # (N_mem, dim)
            q_mat = np.vstack(query_embs)        # (N_query, dim)

            # L2-normalize rows, then cosine = normalized dot product
            mem_norm = mem_mat / np.maximum(
                np.linalg.norm(mem_mat, axis=1, keepdims=True), 1e-12
            )
            q_norm = q_mat / np.maximum(
                np.linalg.norm(q_mat, axis=1, keepdims=True), 1e-12
            )
            sims = mem_norm @ q_norm.T            # (N_mem, N_query)
            max_sims = sims.max(axis=1)           # (N_mem,)

            order = np.argsort(max_sims)[::-1]
            kept = [(int(i), float(max_sims[i])) for i in order
                    if float(max_sims[i]) >= threshold]
            candidates = len(kept)

            from backend.shared.token_manager import estimate_token_count
            selected = []
            selected_ids = []
            used_tokens = 0
            for i, _score in kept:
                mem_id, category, content = mem_entries[i]
                formatted = f"[{category}] {content}"
                entry_tokens = estimate_token_count(formatted)
                if used_tokens + entry_tokens > token_budget:
                    break
                selected.append(formatted)
                selected_ids.append(mem_id)
                used_tokens += entry_tokens

            if selected_ids:
                self.update_last_retrieved(selected_ids)

            logging.info(
                f"[MemoryManager] multi_query_search: queries={len(clean)} "
                f"candidates={candidates} selected={len(selected)} tokens={used_tokens}"
            )
            return selected

        except Exception as e:
            logging.warning(f"[MemoryManager] multi_query_search failed: {e}")
            return []

    def update_last_retrieved(self, memory_ids: List[str]):
        """Update last_retrieved timestamp for specified memory entries."""
        if not memory_ids:
            return
        memory_ns = (self.character_id, "memories")
        now = _get_timestamp()

        with self.db_lock:
            try:
                for mem_id in memory_ids:
                    item = self.store.get(memory_ns, mem_id)
                    if item:
                        data = item.value.copy()
                        data["last_retrieved"] = now
                        self.store.put(memory_ns, mem_id, data)
                # No store.commit() here: the store is autocommit (each put is
                # durable) and this runs on every prompt build — a TRUNCATE WAL
                # checkpoint per turn is pure I/O overhead.
            except Exception as e:
                logging.error(f"[MemoryManager] Error updating last_retrieved: {e}")

    def get_all_memories(self) -> List[Dict[str, Any]]:
        """
        Get all memory entries for UI display.

        Returns:
            List of memory entry dicts sorted by creation order
        """
        memory_ns = (self.character_id, "memories")

        with self.db_lock:
            try:
                all_items = self.store.list(memory_ns)
                entries = []
                for item in all_items:
                    idx = self._extract_mem_index(item.key)
                    if idx is not None:
                        entry = {
                            "id": item.key,
                            "category": item.value.get("category", "unknown"),
                            "content": item.value.get("content", ""),
                            "created_at": item.value.get("created_at", ""),
                            "updated_at": item.value.get("updated_at", ""),
                            "last_retrieved": item.value.get("last_retrieved", ""),
                            "pinned": item.value.get("pinned", False),
                            "source_range": item.value.get("source_range", []),
                        }
                        entries.append((idx, entry))

                entries.sort(key=lambda x: x[0])
                return [e for _, e in entries]

            except Exception as e:
                logging.error(f"[MemoryManager] Error retrieving all memories: {e}")
                return []

    # --- Manual memory CRUD operations (Phase 4 UI) ---

    def add_memory_manual(self, category: str, content: str) -> Dict[str, Any]:
        """
        Manually add a memory entry (from UI).

        Args:
            category: Memory category (must be in MEMORY_CATEGORIES)
            content: Memory content text

        Returns:
            dict with success status and new memory id
        """
        from backend.shared.constants import MEMORY_CATEGORIES
        if category not in MEMORY_CATEGORIES:
            return {"success": False, "error": f"Invalid category: {category}"}
        if not content or not content.strip():
            return {"success": False, "error": "Content cannot be empty"}

        meta_ns = (self.character_id, "meta")
        memory_ns = (self.character_id, "memories")
        now = _get_timestamp()

        # Embedding is a network call — generate before taking db_lock
        # (see add_message).
        embedding = None
        embedding_model = None
        if not self.DISABLE_EMBEDDINGS:
            try:
                emb = self._get_embedding(content.strip())
                embedding = self._validate_and_convert_embedding(emb)
                embedding_model = self._current_embedding_key()
            except Exception as e:
                logging.warning(f"[MemoryManager] Embedding failed for manual memory: {e}")

        with self.db_lock:
            try:
                count_item = self.store.get(meta_ns, "memory_count")
                current_count = count_item.value.get("count", 0) if count_item else 0
                current_count += 1
                mem_key = f"mem_{self._allocate_memory_index()}"

                entry = {
                    "category": category,
                    "content": content.strip(),
                    "embedding": embedding,
                    "embedding_model": embedding_model,
                    "source_range": [],
                    "created_at": now,
                    "updated_at": now,
                    "last_retrieved": now,
                    "pinned": False
                }
                self.store.put(memory_ns, mem_key, entry)
                self.store.put(meta_ns, "memory_count", {"count": current_count})
                self.store.commit()

                logging.info(f"[MemoryManager] Manual memory added: {mem_key} [{category}]")

                # Cleanup old entries if over limit
                self.cleanup_old_entries()

                return {"success": True, "id": mem_key}

            except Exception as e:
                logging.error(f"[MemoryManager] Error adding manual memory: {e}")
                # NOTE: MemoryStore は autocommit(isolation_level=None)で rollback API を持たない。
                # 旧 self.store.rollback() 呼出は AttributeError を二次発生させ本来のエラーを潰していた。
                return {"success": False, "error": str(e)}

    def edit_memory(self, memory_id: str, content: str, category: str = None) -> Dict[str, Any]:
        """
        Edit an existing memory entry's content (and optionally category).

        Args:
            memory_id: The memory key (e.g. "mem_42")
            content: New content text
            category: New category (optional, keeps existing if None)

        Returns:
            dict with success status
        """
        if not content or not content.strip():
            return {"success": False, "error": "Content cannot be empty"}

        from backend.shared.constants import MEMORY_CATEGORIES
        if category and category not in MEMORY_CATEGORIES:
            return {"success": False, "error": f"Invalid category: {category}"}

        memory_ns = (self.character_id, "memories")
        now = _get_timestamp()

        # Re-generate embedding before taking db_lock (network call — see
        # add_message). Runs even if memory_id turns out not to exist; that
        # only costs one wasted call on an error path.
        new_embedding = None
        new_embedding_model = None
        if not self.DISABLE_EMBEDDINGS:
            try:
                emb = self._get_embedding(content.strip())
                new_embedding = self._validate_and_convert_embedding(emb)
                new_embedding_model = self._current_embedding_key()
            except Exception as e:
                logging.warning(f"[MemoryManager] Embedding regen failed for edit: {e}")

        with self.db_lock:
            try:
                existing = self.store.get(memory_ns, memory_id)
                if not existing:
                    return {"success": False, "error": f"Memory {memory_id} not found"}

                data = existing.value.copy()
                data["content"] = content.strip()
                if category:
                    data["category"] = category
                data["updated_at"] = now

                if not self.DISABLE_EMBEDDINGS and new_embedding is not None:
                    data["embedding"] = new_embedding
                    data["embedding_model"] = new_embedding_model

                self.store.put(memory_ns, memory_id, data)
                self.store.commit()

                logging.info(f"[MemoryManager] Memory edited: {memory_id}")
                return {"success": True}

            except Exception as e:
                logging.error(f"[MemoryManager] Error editing memory: {e}")
                # NOTE: MemoryStore は autocommit(isolation_level=None)で rollback API を持たない。
                # 旧 self.store.rollback() 呼出は AttributeError を二次発生させ本来のエラーを潰していた。
                return {"success": False, "error": str(e)}

    def delete_memory(self, memory_id: str) -> Dict[str, Any]:
        """
        Delete a memory entry.

        Args:
            memory_id: The memory key (e.g. "mem_42")

        Returns:
            dict with success status
        """
        meta_ns = (self.character_id, "meta")
        memory_ns = (self.character_id, "memories")

        with self.db_lock:
            try:
                existing = self.store.get(memory_ns, memory_id)
                if not existing:
                    return {"success": False, "error": f"Memory {memory_id} not found"}

                self.store.delete(memory_ns, memory_id)

                # Decrement memory count
                count_item = self.store.get(meta_ns, "memory_count")
                current_count = count_item.value.get("count", 0) if count_item else 0
                if current_count > 0:
                    self.store.put(meta_ns, "memory_count", {"count": current_count - 1})

                self.store.commit()

                logging.info(f"[MemoryManager] Memory deleted: {memory_id}")
                return {"success": True}

            except Exception as e:
                logging.error(f"[MemoryManager] Error deleting memory: {e}")
                # NOTE: MemoryStore は autocommit(isolation_level=None)で rollback API を持たない。
                # 旧 self.store.rollback() 呼出は AttributeError を二次発生させ本来のエラーを潰していた。
                return {"success": False, "error": str(e)}

    def pin_memory(self, memory_id: str, pinned: bool) -> Dict[str, Any]:
        """
        Set pinned status of a memory entry.

        Args:
            memory_id: The memory key (e.g. "mem_42")
            pinned: True to pin, False to unpin

        Returns:
            dict with success status
        """
        memory_ns = (self.character_id, "memories")

        with self.db_lock:
            try:
                existing = self.store.get(memory_ns, memory_id)
                if not existing:
                    return {"success": False, "error": f"Memory {memory_id} not found"}

                data = existing.value.copy()
                data["pinned"] = bool(pinned)
                self.store.put(memory_ns, memory_id, data)
                self.store.commit()

                logging.info(f"[MemoryManager] Memory {memory_id} pinned={pinned}")
                return {"success": True}

            except Exception as e:
                logging.error(f"[MemoryManager] Error pinning memory: {e}")
                # NOTE: MemoryStore は autocommit(isolation_level=None)で rollback API を持たない。
                # 旧 self.store.rollback() 呼出は AttributeError を二次発生させ本来のエラーを潰していた。
                return {"success": False, "error": str(e)}

    def add_message(self, role: str, content: str, metadata: Optional[Dict[str, Any]] = None,
                    images: Optional[list] = None, documents: Optional[list] = None) -> Dict[str, Any]:
        """
        Add a new message to the conversation history.

        Args:
            role: 'user' or 'assistant'
            content: The message content
            metadata: Optional metadata (emotion, context, etc.)
            images: Optional list of image file paths attached to this message
            documents: Optional list of document dicts [{filename, text, char_count}]

        Returns:
            dict: {"success": bool, "needs_extraction": bool, "unprocessed_count": int}
        """
        fail_result = {"success": False, "needs_extraction": False, "unprocessed_count": 0}

        if role not in ["user", "assistant", "system"]:
            logging.error(f"[MemoryManager] Invalid role: {role}")
            return fail_result

        if not content or not isinstance(content, str):
            logging.error(f"[MemoryManager] Message content cannot be empty or non-string. Got type: {type(content)}")
            return fail_result

        # Persist attachment paths repo-relative so the DB stays portable across
        # install moves/renames (same canonical form as character configs —
        # _map_config_paths). Callers keep their absolute paths: copies only.
        # get_recent_messages resolves back to absolute on the way out.
        if images:
            images = [to_repo_relative(p) for p in images]
        if documents:
            documents = [
                {**d, "file_path": to_repo_relative(d["file_path"])}
                if isinstance(d, dict) and d.get("file_path") else d
                for d in documents
            ]

        transaction_id = self.store.begin_transaction()

        # Generate embedding BEFORE taking db_lock: this is a network call
        # (up to 30s per attempt, with retries). Holding db_lock through it
        # blocked prompt building (get_recent_messages) for the whole wait.
        embedding_value = None
        if not self.DISABLE_EMBEDDINGS:
            try:
                embedding = self._get_embedding(content)
                embedding_value = self._validate_and_convert_embedding(embedding)
            except Exception as e:
                logging.error(f"[MemoryManager] Embedding generation failed but continuing message storage: {e}")

        with self.db_lock:
            try:
                meta_ns = (self.character_id, "meta")
                message_ns = (self.character_id, "messages")

                msg_count_item = self.store.get(meta_ns, "msg_count")
                current_count = msg_count_item.value.get("count", 0) if msg_count_item else 0
                new_count = current_count + 1

                key = f"msg_{new_count}"
                record = {
                    "role": role,
                    "content": content,
                    "timestamp": _get_timestamp(),
                    "metadata": metadata or {},
                    "images": images or [],
                    "documents": documents or [],
                    "transaction_id": transaction_id,
                    "transaction_complete": False
                }

                if not self.DISABLE_EMBEDDINGS:
                    record["embedding"] = embedding_value

                self.store.put(message_ns, key, record)
                self.store.put(meta_ns, "msg_count", {"count": new_count})

                record["transaction_complete"] = True
                self.store.put(message_ns, key, record)
                # No store.commit() here: autocommit already made the puts
                # durable; checkpointing the WAL on every saved message is
                # unnecessary I/O. Periodic checkpoints still happen on
                # extraction apply and shutdown (store.commit there).

                # Check extraction threshold
                last_extracted_item = self.store.get(meta_ns, "last_extracted")
                last_extracted_index = last_extracted_item.value.get("index", 0) if last_extracted_item else 0
                unprocessed = new_count - last_extracted_index

                provider = self._get_character_provider()
                is_api = provider != 'ollama'
                threshold = EXTRACTION_THRESHOLD_API if is_api else EXTRACTION_THRESHOLD_OLLAMA

                return {
                    "success": True,
                    "needs_extraction": unprocessed >= threshold,
                    "unprocessed_count": unprocessed
                }

            except Exception as e:
                logging.error(f"[MemoryManager] Error adding message (transaction {transaction_id}): {e}")
                self._rollback_message_transaction(transaction_id)
                return fail_result

    def _rollback_message_transaction(self, transaction_id: str):
        """Rollback a failed message transaction."""
        try:
            message_ns = (self.character_id, "messages")
            meta_ns = (self.character_id, "meta")
            
            # Find and remove any messages with this transaction ID
            all_messages = self.store.list(message_ns)
            for item in all_messages:
                if item.value.get("transaction_id") == transaction_id and not item.value.get("transaction_complete", False):
                    logging.warning(f"[MemoryManager] Rolling back incomplete message: {item.key}")
                    self.store.delete(message_ns, item.key)
            
            # Note: We don't rollback the message count as it might have been 
            # incremented by other successful transactions
            
            self.store.commit()
            
        except Exception as e:
            logging.error(f"[MemoryManager] Failed to rollback message transaction {transaction_id}: {e}")

    # (_enforce_short_term_limit removed — no longer needed with index-based tracking)


    # ----------------------------------------------------------------
    # Old summarization functions removed — replaced by extraction system
    # summarize_last_block, _rollback_summarization_transaction,
    # _convert_to_bullet_list, _create_fallback_summary removed
    # ----------------------------------------------------------------

    # (Old summarization functions removed)

    def _get_character_language(self) -> str:
        """
        Get the current language setting for this character.
        
        Returns:
            Language code (e.g., 'en', 'ja'). Defaults to 'en' if not found.
        """
        try:
            # Import here to avoid circular dependency at module level
            from backend.conversation.character_manager import load_character_config
            
            config = load_character_config(self.character_id)
            if isinstance(config, dict):
                # Handle both response formats
                if 'success' in config and not config.get('success'):
                    logging.warning(f"Failed to load character config: {config.get('error', 'Unknown error')}")
                    return 'en'
                elif 'result' in config:
                    config = config.get('result', {})
                    
                # Extract language from faster_whisper_config
                from backend.shared.prompt_i18n import get_prompt_language
                language = get_prompt_language(config)
                logging.debug(f"[MemoryManager] Character {self.character_id} language: {language}")
                return language
            else:
                logging.warning(f"[MemoryManager] Invalid config format for character {self.character_id}")
                return 'en'
                
        except Exception as e:
            logging.warning(f"[MemoryManager] Failed to get character language, defaulting to English: {e}")
            return 'en'

    def _get_character_model(self) -> str:
        """
        Get the Ollama model configured for this character.
        
        Returns:
            Model name (e.g., 'llama3.1:8b'). Defaults to 'llama3.1:8b' if not found.
        """
        try:
            # Import here to avoid circular dependency at module level
            from backend.conversation.character_manager import load_character_config
            
            config = load_character_config(self.character_id)
            if isinstance(config, dict):
                # Handle both response formats
                if 'success' in config and not config.get('success'):
                    logging.warning(f"Failed to load character config: {config.get('error', 'Unknown error')}")
                    return 'llama3.1:8b'
                elif 'result' in config:
                    config = config.get('result', {})
                    
                # Extract model name
                model_name = config.get('model_name', '') or config.get('ollama_model_name', 'llama3.1:8b')
                logging.debug(f"[MemoryManager] Character {self.character_id} model: {model_name}")
                return model_name
            else:
                logging.warning(f"[MemoryManager] Invalid config format for character {self.character_id}")
                return 'llama3.1:8b'
                
        except Exception as e:
            logging.warning(f"[MemoryManager] Failed to get character model, defaulting to llama3.1:8b: {e}")
            return 'llama3.1:8b'

    def _get_character_provider(self) -> str:
        """
        Get the model provider configured for this character.

        Returns:
            Provider identifier (e.g., 'ollama', 'openai', 'anthropic').
            Defaults to 'ollama' if not found.
        """
        try:
            from backend.conversation.character_manager import load_character_config

            config = load_character_config(self.character_id)
            if isinstance(config, dict):
                if 'success' in config and not config.get('success'):
                    return 'ollama'
                elif 'result' in config:
                    config = config.get('result', {})

                provider = config.get('model_provider', 'ollama')
                logging.debug(f"[MemoryManager] Character {self.character_id} provider: {provider}")
                return provider
            else:
                return 'ollama'

        except Exception as e:
            logging.warning(f"[MemoryManager] Failed to get character provider, defaulting to ollama: {e}")
            return 'ollama'

    def _get_character_model_name(self) -> str:
        """このキャラのモデル名（num_ctxクランプ用=C4.6）。不明は空文字。

        _get_character_provider と同じ config 読み形。空文字なら
        get_effective_num_ctx は設定値をそのまま使う。
        """
        try:
            from backend.conversation.character_manager import load_character_config

            config = load_character_config(self.character_id)
            if isinstance(config, dict):
                if 'success' in config and not config.get('success'):
                    return ''
                elif 'result' in config:
                    config = config.get('result', {})
                return (config.get('model_name', '')
                        or config.get('ollama_model_name', ''))
            return ''
        except Exception as e:
            logging.warning(f"[MemoryManager] Failed to get character model name: {e}")
            return ''

    # (_call_summarization_llm removed)

    # (_shrink_summaries and increment_summary_usage_counts removed)

    def _get_embedding(self, text: str) -> np.ndarray:
        """
        Generate embedding vector for the given text using direct Ollama API.

        Args:
            text: Text to generate embedding for

        Returns:
            numpy array of embedding values

        Raises:
            EmbeddingError: If embedding generation fails
        """
        if self.DISABLE_EMBEDDINGS:
            # Return zero vector for testing
            return np.zeros(self.expected_embedding_size)

        try:
            # Determine embedding provider and model from settings
            from backend.shared.api_settings import get_embedding_model
            embedding_model = get_embedding_model()
            if embedding_model is None:
                # 未設定(デフォルト廃止 2026-07-25): 会話開始はガード済みだが、
                # 記憶の手動編集等の非会話経路は従来通りgraceful degrade。
                raise EmbeddingError("Embedding model is not configured")
            provider, model_name = embedding_model

            # Retry logic for transient failures
            last_error = None
            for attempt in range(EMBEDDING_RETRY_LIMIT):
                try:
                    # Generate embedding via configured provider
                    if provider == "ollama":
                        from backend.llm.ollama_integration import call_ollama_embedding
                        result = call_ollama_embedding(
                            model=model_name,
                            text=text,
                            timeout=30.0
                        )
                    else:
                        from backend.llm.api_integration import call_api_embedding
                        result = call_api_embedding(
                            provider=provider,
                            model=model_name,
                            text=text,
                            timeout=30.0
                        )

                    if not result["success"]:
                        raise EmbeddingError(f"Embedding failed: {result.get('error', 'Unknown error')}")

                    # Convert to numpy array
                    embedding_array = np.array(result["embedding"], dtype=np.float32)
                    actual_model = result.get("model", model_name)

                    # Validate embedding quality
                    if np.isnan(embedding_array).any():
                        raise EmbeddingError("Generated embedding contains NaN values")

                    if np.count_nonzero(embedding_array) < 10:
                        raise EmbeddingError("Generated embedding appears to be low quality (too many zeros)")

                    # Update expected size and config if dimension changed
                    if self.expected_embedding_size != len(embedding_array):
                        logging.info(f"[MemoryManager] Updating expected embedding size from {self.expected_embedding_size} to {len(embedding_array)}")
                        self.expected_embedding_size = len(embedding_array)
                        self._update_embedding_config(actual_model, len(embedding_array))

                    timing_log("embedding_generated")
                    return embedding_array

                except Exception as e:
                    last_error = e
                    if attempt < EMBEDDING_RETRY_LIMIT - 1:
                        logging.warning(f"[MemoryManager] Embedding attempt {attempt + 1} failed: {e}, retrying...")
                        time.sleep(EMBEDDING_RETRY_DELAY * (attempt + 1))  # Exponential backoff
                    else:
                        logging.error(f"[MemoryManager] All embedding attempts failed: {e}")

            raise EmbeddingError(f"Failed to generate embedding after {EMBEDDING_RETRY_LIMIT} attempts: {last_error}")

        except ImportError as e:
            logging.error(f"[MemoryManager] Failed to import embedding module: {e}")
            raise EmbeddingError(f"Import error: {e}")

    def _get_embeddings_batch(self, texts: List[str]) -> List[np.ndarray]:
        """Generate embeddings for multiple texts.

        Uses a single batched API call for OpenAI-compatible providers
        (openai/xai); falls back to sequential `_get_embedding` for any other
        provider (google/ollama). Returned arrays are in the same order as
        `texts`.

        Raises:
            EmbeddingError: If embedding generation fails.
        """
        if not texts:
            return []

        if self.DISABLE_EMBEDDINGS:
            return [np.zeros(self.expected_embedding_size) for _ in texts]

        from backend.shared.api_settings import get_embedding_model
        embedding_model = get_embedding_model()
        if embedding_model is None:
            raise EmbeddingError("Embedding model is not configured")
        provider, model_name = embedding_model

        # Providers without array-input batch support: sequential fallback.
        if provider not in ("openai", "xai"):
            return [self._get_embedding(t) for t in texts]

        from backend.llm.api_integration import call_api_embedding_batch

        last_error = None
        for attempt in range(EMBEDDING_RETRY_LIMIT):
            try:
                result = call_api_embedding_batch(
                    provider=provider,
                    model=model_name,
                    texts=texts,
                    timeout=30.0,
                )
                if not result["success"]:
                    raise EmbeddingError(
                        f"Batch embedding failed: {result.get('error', 'Unknown error')}"
                    )

                raw = result["embeddings"]
                if len(raw) != len(texts):
                    raise EmbeddingError(
                        f"Batch embedding count mismatch: {len(raw)} vs {len(texts)}"
                    )

                arrays = []
                for emb in raw:
                    arr = np.array(emb, dtype=np.float32)
                    if np.isnan(arr).any():
                        raise EmbeddingError("Generated embedding contains NaN values")
                    if np.count_nonzero(arr) < 10:
                        raise EmbeddingError(
                            "Generated embedding appears to be low quality (too many zeros)"
                        )
                    arrays.append(arr)

                # Keep expected size in sync (mirrors _get_embedding; not relied
                # upon by search, which compares query/memory dims at runtime).
                if arrays and self.expected_embedding_size != len(arrays[0]):
                    logging.info(
                        f"[MemoryManager] Updating expected embedding size from "
                        f"{self.expected_embedding_size} to {len(arrays[0])}"
                    )
                    self.expected_embedding_size = len(arrays[0])
                    self._update_embedding_config(model_name, len(arrays[0]))

                return arrays

            except Exception as e:
                last_error = e
                if attempt < EMBEDDING_RETRY_LIMIT - 1:
                    logging.warning(
                        f"[MemoryManager] Batch embedding attempt {attempt + 1} failed: {e}, retrying..."
                    )
                    time.sleep(EMBEDDING_RETRY_DELAY * (attempt + 1))
                else:
                    logging.error(f"[MemoryManager] All batch embedding attempts failed: {e}")

        raise EmbeddingError(
            f"Failed to generate batch embeddings after {EMBEDDING_RETRY_LIMIT} attempts: {last_error}"
        )

    def _update_embedding_config(self, model_name: str, dimension: int):
        """
        Update embedding configuration in storage.

        Args:
            model_name: Name of the embedding model
            dimension: Embedding dimension
        """
        meta_ns = (self.character_id, "meta")
        config_item = self.store.get(meta_ns, "embedding_config")
        if config_item:
            config = config_item.value
            old_model = config.get("model", "unknown")
            old_dimension = config.get("dimension", self.expected_embedding_size)

            # Check if model or dimension changed
            if old_model != model_name or old_dimension != dimension:
                if old_model != "unknown" and old_model != model_name:
                    logging.warning(
                        f"[MemoryManager] Embedding model changed from {old_model} to {model_name}. "
                        "Existing embeddings may have reduced search accuracy."
                    )
                    config["needs_migration"] = True
                    config["previous_model"] = old_model
                    config["version"] = config.get("version", 1) + 1
                elif old_dimension != dimension:
                    logging.warning(f"[MemoryManager] Embedding dimension changed from {old_dimension} to {dimension}")
                    config["needs_migration"] = True
                    config["version"] = config.get("version", 1) + 1

                config["model"] = model_name
                config["dimension"] = dimension
                config["last_updated"] = _get_timestamp()
                self.store.put(meta_ns, "embedding_config", config)
                self.store.commit()

                logging.info(f"[MemoryManager] Embedding config updated for character {self.character_id}")

    def _current_embedding_key(self) -> Optional[str]:
        """Return "provider::model" for the configured embedding model.

        Written next to each memory embedding as its provenance stamp and
        compared at search time: a memory whose stamp differs from the current
        model is unmigrated and skipped (a dimension check alone cannot catch
        a same-dimension model switch, whose cosine scores are silent garbage
        — ST6 §7-4 sequel). Returns None when embeddings are disabled or the
        settings lookup fails (entry stays unstamped -> treated as unmigrated).
        """
        if self.DISABLE_EMBEDDINGS:
            return None
        try:
            from backend.shared.api_settings import get_embedding_model, encode_model_value
            embedding_model = get_embedding_model()
            if embedding_model is None:
                return None
            provider, model_name = embedding_model
            return encode_model_value(provider, model_name)
        except Exception:
            return None

    def _validate_and_convert_embedding(self, embedding: np.ndarray) -> List[float]:
        """
        Validate embedding dimensions and convert to list for storage.
        
        Args:
            embedding: Numpy array of embedding values
            
        Returns:
            List of float values
            
        Raises:
            EmbeddingError: If validation fails
        """
        if embedding is None:
            raise EmbeddingError("Embedding is None")
        
        # Check dimensions
        if len(embedding) != self.expected_embedding_size:
            # Log warning but try to handle gracefully
            logging.warning(f"[MemoryManager] Embedding dimension mismatch: got {len(embedding)}, expected {self.expected_embedding_size}")
            
            # Resize embedding to match expected size
            if len(embedding) < self.expected_embedding_size:
                # Pad with zeros
                padded = np.zeros(self.expected_embedding_size)
                padded[:len(embedding)] = embedding
                embedding = padded
                logging.info(f"[MemoryManager] Padded embedding from {len(embedding)} to {self.expected_embedding_size}")
            else:
                # Truncate
                embedding = embedding[:self.expected_embedding_size]
                logging.info(f"[MemoryManager] Truncated embedding from {len(embedding)} to {self.expected_embedding_size}")
        
        # Convert to list for JSON serialization
        return embedding.tolist()

    # (search_by_similarity removed — replaced by search_memories_by_similarity)

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """
        Calculate cosine similarity between two vectors with dimension validation.
        
        Args:
            a: First vector
            b: Second vector
            
        Returns:
            Cosine similarity score between -1 and 1
        """
        # Validate input dimensions
        if a.shape != b.shape:
            logging.warning(f"[MemoryManager] Vector dimension mismatch in similarity calculation: {a.shape} vs {b.shape}")
            # Attempt to handle gracefully by padding/truncating
            target_size = max(len(a), len(b))
            
            if len(a) < target_size:
                a_resized = np.zeros(target_size)
                a_resized[:len(a)] = a
                a = a_resized
                
            if len(b) < target_size:
                b_resized = np.zeros(target_size)
                b_resized[:len(b)] = b
                b = b_resized
            
            # If still mismatched, truncate to smaller size
            if len(a) != len(b):
                min_size = min(len(a), len(b))
                a = a[:min_size]
                b = b[:min_size]
        
        # Calculate cosine similarity
        dot_product = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        
        similarity = dot_product / (norm_a * norm_b)
        
        # Ensure result is in valid range
        return float(np.clip(similarity, -1.0, 1.0))

    # ----------------------------------------------------------------
    # Memory extraction system (replaces old summarization)
    # ----------------------------------------------------------------

    def extract_memories(self):
        """
        Main extraction orchestrator. Collects unprocessed messages,
        calls LLM for fact extraction, and applies results to DB.
        """
        # Circuit breaker check
        if self.extraction_failures >= EXTRACTION_MAX_FAILURES:
            if time.time() - self.last_extraction_failure < EXTRACTION_FAILURE_COOLDOWN:
                logging.warning(f"[MemoryManager] Extraction circuit breaker active (failures: {self.extraction_failures})")
                return
            else:
                logging.info("[MemoryManager] Extraction circuit breaker cooldown expired, resetting")
                self.extraction_failures = 0

        if not self.extraction_lock.acquire(blocking=True, timeout=LOCK_TIMEOUT):
            logging.warning("[MemoryManager] Could not acquire extraction lock, skipping")
            return

        try:
            meta_ns = (self.character_id, "meta")
            message_ns = (self.character_id, "messages")

            with self.db_lock:
                last_extracted_item = self.store.get(meta_ns, "last_extracted")
                last_extracted = last_extracted_item.value.get("index", 0) if last_extracted_item else 0
                msg_count = self.get_message_count()

            if msg_count <= last_extracted:
                logging.info("[MemoryManager] No unprocessed messages for extraction")
                return

            # Collect unprocessed messages (キー選別→該当分のみ値ロード)
            with self.db_lock:
                unprocessed_keys = []
                for key in self.store.list_keys(message_ns):
                    idx = self._extract_msg_index(key)
                    if idx is not None and idx > last_extracted:
                        unprocessed_keys.append((idx, key))
                unprocessed_keys.sort(key=lambda x: x[0])

                unprocessed = []
                for idx, key in unprocessed_keys:
                    item = self.store.get(message_ns, key)
                    if item is not None:
                        unprocessed.append((idx, item.value))

            if not unprocessed:
                return

            messages_data = [m for _, m in unprocessed]
            source_start = unprocessed[0][0]
            source_end = unprocessed[-1][0]

            # Check if Ollama context would be exceeded → split if needed
            from backend.shared.token_manager import estimate_token_count
            provider = self._get_character_provider()

            # Get related existing memories
            existing_memories = self._get_related_existing_memories(messages_data)

            total_text = "\n".join(m.get("content", "") for m in messages_data)
            est_tokens = estimate_token_count(total_text)
            if provider == 'ollama':
                # 実効num_ctx(設定+モデル別クランプ=C4.6)から prompt+output 分を予約
                from backend.llm.ollama_capabilities import get_effective_num_ctx
                available_ctx = get_effective_num_ctx(
                    self._get_character_model_name()) - 4500
            else:
                available_ctx = None  # API providers: 分割不要(128K)

            # 失敗ラウンドの区間まで last_extracted を前進させると、その区間の
            # メッセージは cleanup で削除され記憶が永久欠落する。成功した区間の
            # 末尾までしか前進させない(失敗分は次回リトライ)。
            extracted_up_to = None
            if provider == 'ollama' and est_tokens > available_ctx:
                # Split into two halves
                mid = len(messages_data) // 2
                logging.info(f"[MemoryManager] Splitting extraction: {len(messages_data)} messages ({est_tokens} tokens > {available_ctx})")

                first_half = messages_data[:mid]
                first_source = (source_start, unprocessed[mid - 1][0])
                if self._run_extraction_round(first_half, existing_memories, first_source):
                    extracted_up_to = first_source[1]

                    # Re-fetch existing memories for second half
                    second_half = messages_data[mid:]
                    second_source = (unprocessed[mid][0], source_end)
                    updated_memories = self._get_related_existing_memories(second_half)
                    if self._run_extraction_round(second_half, updated_memories, second_source):
                        extracted_up_to = source_end
            else:
                if self._run_extraction_round(messages_data, existing_memories, (source_start, source_end)):
                    extracted_up_to = source_end

            if extracted_up_to is None:
                logging.warning(
                    f"[MemoryManager] Extraction round failed; last_extracted not advanced "
                    f"(messages {source_start}-{source_end} will be retried)"
                )
                return

            # last_extracted の前進はラウンド内(_apply_extraction_results)で
            # 断片と同一トランザクションで書き込み済み

            # Cleanup old messages
            self.cleanup_old_messages()

            # 抽出済みメッセージの埋め込みはもう読まれない(読み手は未処理分の
            # 重心計算のみ)＝ここで剥がす。エラーは内部で隔離され抽出の成否に
            # 影響しない
            self._strip_extracted_embeddings()

            logging.info(f"[MemoryManager] Extraction completed for messages {source_start}-{extracted_up_to}")

        except Exception as e:
            logging.error(f"[MemoryManager] Error during memory extraction: {e}")
            self.extraction_failures += 1
            self.last_extraction_failure = time.time()
        finally:
            self.extraction_lock.release()

    def _run_extraction_round(self, messages: List[Dict], existing_memories: List[Dict], source_range: Tuple[int, int]) -> bool:
        """Run a single extraction round: call LLM and apply results.

        The ids the model may legitimately reference in ``updates`` are exactly
        the ids of ``existing_memories`` (the only ids it was shown); that set is
        handed to ``_apply_extraction_results`` as the update whitelist.

        Returns:
            True if the round succeeded (caller may advance last_extracted).
        """
        try:
            allowed_update_ids = {m.get("id") for m in existing_memories if m.get("id")}
            response_text = self._call_extraction_llm(messages, existing_memories)
            if not response_text:
                logging.warning("[MemoryManager] Empty extraction LLM response")
                self.extraction_failures += 1
                self.last_extraction_failure = time.time()
                return False

            results = self._parse_extraction_response(response_text)
            if results is None:
                logging.warning("[MemoryManager] Failed to parse extraction response")
                self.extraction_failures += 1
                self.last_extraction_failure = time.time()
                return False

            if not self._apply_extraction_results(results, list(source_range), allowed_update_ids):
                self.extraction_failures += 1
                self.last_extraction_failure = time.time()
                return False

            # Reset failure count on success
            self.extraction_failures = 0
            return True

        except Exception as e:
            logging.error(f"[MemoryManager] Extraction round failed: {e}")
            self.extraction_failures += 1
            self.last_extraction_failure = time.time()
            return False

    def _call_extraction_llm(self, messages: List[Dict], existing_memories: List[Dict]) -> Optional[str]:
        """Call the LLM for memory extraction."""
        try:
            from backend.llm.api_integration import create_llm_client

            from backend.shared.prompt_i18n import prompt_section, prompt_text
            language = self._get_character_language()
            system_prompt_text = prompt_section('memory_extraction_system', language)
            user_prompt_template = prompt_section('memory_extraction_user', language)

            # Build conversation text
            conversation_lines = []
            for msg in messages:
                role = "User" if msg.get("role") == "user" else "Character"
                conversation_lines.append(f"{role}: {msg.get('content', '')}")
            conversation_text = "\n".join(conversation_lines)

            # Build existing memories section
            if existing_memories:
                mem_lines = []
                for mem in existing_memories:
                    mem_lines.append(f"[{mem.get('id', '')}] {mem.get('category', '')}: {mem.get('content', '')}")
                existing_section = prompt_text('memory_extraction.existing_heading', language) + "\n" + "\n".join(mem_lines)
            else:
                existing_section = ""

            user_prompt = user_prompt_template.replace('{conversation}', conversation_text).replace('{existing_memories_section}', existing_section)

            model_provider = self._get_character_provider()
            model_name = self._get_character_model()

            logging.info(f"[MemoryManager] Calling {model_provider}/{model_name} for extraction ({len(messages)} messages)")

            llm_result = create_llm_client(
                model_provider=model_provider,
                model_name=model_name,
                usage_type='extraction',
                timeout=OLLAMA_GENERATION_TIMEOUT
            )

            if not llm_result.get("success", False):
                raise ExtractionError(f"Failed to create LLM: {llm_result.get('error', 'Unknown')}")

            llm = llm_result["response"]
            llm_messages = [
                {"role": "system", "content": system_prompt_text},
                {"role": "user", "content": user_prompt}
            ]

            response = llm.invoke(llm_messages)
            response_text = response.content if hasattr(response, 'content') else response[-1].content

            logging.info(f"[MemoryManager] Extraction LLM response: {len(response_text)} chars")
            return response_text

        except Exception as e:
            logging.error(f"[MemoryManager] Extraction LLM call failed: {e}")
            return None

    def _parse_extraction_response(self, response_text: str) -> Optional[Dict[str, list]]:
        """
        Parse extraction LLM response with 3-stage fallback:
        1. JSON parse
        2. Regex parse (- [category] content)
        3. None (skip)
        """
        if not response_text or not response_text.strip():
            return None

        # Stage 1: Try JSON parse
        try:
            # Find JSON in the response (might be wrapped in markdown code blocks)
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                data = json.loads(json_match.group())
                additions = data.get("additions", [])
                updates = data.get("updates", [])

                # Validate structure
                valid_additions = []
                for a in additions:
                    if isinstance(a, dict) and "content" in a and "category" in a:
                        if a["category"] in MEMORY_CATEGORIES:
                            valid_additions.append(a)
                        else:
                            valid_additions.append({"content": a["content"], "category": "user_fact"})

                valid_updates = []
                for u in updates:
                    if isinstance(u, dict) and "id" in u and "content" in u:
                        cat = u.get("category", "user_fact")
                        if cat not in MEMORY_CATEGORIES:
                            cat = "user_fact"
                        valid_updates.append({"id": u["id"], "content": u["content"], "category": cat})

                logging.info(f"[MemoryManager] Parsed JSON: {len(valid_additions)} additions, {len(valid_updates)} updates")
                return {"additions": valid_additions, "updates": valid_updates}
        except (json.JSONDecodeError, ValueError) as e:
            logging.debug(f"[MemoryManager] JSON parse failed, trying regex: {e}")

        # Stage 2: Regex fallback — "- [category] content"
        try:
            pattern = r'-\s*\[(\w+)\]\s*(.+)'
            matches = re.findall(pattern, response_text)
            if matches:
                additions = []
                for category, content in matches:
                    cat = category.strip().lower()
                    if cat not in MEMORY_CATEGORIES:
                        cat = "user_fact"
                    additions.append({"content": content.strip(), "category": cat})
                logging.info(f"[MemoryManager] Regex parsed: {len(additions)} additions (updates skipped)")
                return {"additions": additions, "updates": []}
        except Exception as e:
            logging.debug(f"[MemoryManager] Regex parse also failed: {e}")

        # Stage 3: Give up
        logging.warning("[MemoryManager] Could not parse extraction response")
        return None

    def _apply_extraction_results(self, results: Dict[str, list], source_range: List[int],
                                  allowed_update_ids: Set[str]) -> bool:
        """Apply extraction results (additions and updates) to the database.

        断片・memory_count・last_extracted 前進(source_range[1] まで)を単一
        トランザクションで書く。killがどこで挟まっても「全て残る/全て残らない」
        の二値になり、断片だけ残って last_extracted 未前進→次回再抽出=重複記憶、
        という窓が閉じる。

        ``allowed_update_ids`` = このラウンドでモデルに見せた既存記憶の id 集合。
        updates の id がここに無ければ、モデルが知り得ない id(幻覚)なので上書き
        せず addition として保存する。additions を先に書く構造上、幻覚 id
        (例: プロンプト例文の "mem_3")が今回採番した mem_N と一致すると
        「見つからないので新規追加」の安全弁を素通りして生まれたての記憶を
        上書きしていた(2026-08-16 Mac 実機: 19 additions + 2 updates → 19件)。

        Args:
            results: {"additions": [...], "updates": [...]} from the parser.
            source_range: [first_msg_index, last_msg_index] this round covers.
            allowed_update_ids: ids offered to the model (update whitelist).

        Returns:
            True if the results were persisted (False = DB error; nothing was
            written, the round will be retried).
        """
        meta_ns = (self.character_id, "meta")
        memory_ns = (self.character_id, "memories")
        now = _get_timestamp()

        additions = results.get("additions", [])
        updates = results.get("updates", [])

        # Generate ALL embeddings BEFORE taking db_lock. Extraction runs on a
        # background thread and each embedding is a network call (up to 30s
        # per attempt, with retries); holding db_lock through them blocked
        # conversation prompt builds for the whole duration.
        addition_embeddings = []
        for addition in additions:
            embedding = None
            embedding_model = None
            if not self.DISABLE_EMBEDDINGS:
                try:
                    emb = self._get_embedding(addition["content"])
                    embedding = self._validate_and_convert_embedding(emb)
                    embedding_model = self._current_embedding_key()
                except Exception as e:
                    logging.warning(f"[MemoryManager] Embedding failed for new memory: {e}")
            addition_embeddings.append((embedding, embedding_model))

        update_embeddings = []
        for update in updates:
            embedding = None
            embedding_model = None
            if update.get("id", "") and not self.DISABLE_EMBEDDINGS:
                try:
                    emb = self._get_embedding(update["content"])
                    embedding = self._validate_and_convert_embedding(emb)
                    embedding_model = self._current_embedding_key()
                except Exception:
                    pass
            update_embeddings.append((embedding, embedding_model))

        with self.db_lock:
            try:
                with self.store.transaction():
                    # Get current memory count
                    count_item = self.store.get(meta_ns, "memory_count")
                    current_count = count_item.value.get("count", 0) if count_item else 0

                    new_count = 0

                    # Process additions
                    for addition, (embedding, embedding_model) in zip(additions, addition_embeddings):
                        current_count += 1
                        new_count += 1
                        mem_key = f"mem_{self._allocate_memory_index()}"

                        entry = {
                            "category": addition["category"],
                            "content": addition["content"],
                            "embedding": embedding,
                            "embedding_model": embedding_model,
                            "source_range": source_range,
                            "created_at": now,
                            "updated_at": now,
                            "last_retrieved": now,
                            "pinned": False
                        }
                        self.store.put(memory_ns, mem_key, entry)

                    # Process updates
                    redirected_updates = 0
                    for update, (embedding, embedding_model) in zip(updates, update_embeddings):
                        mem_id = update.get("id", "")
                        if not mem_id:
                            continue
                        existing = None
                        if mem_id not in allowed_update_ids:
                            # 見せていない id = 幻覚。上書きせず新規として保存
                            logging.warning(
                                f"[MemoryManager] Update target {mem_id} was not offered to the model "
                                f"(hallucinated id), adding as new"
                            )
                            redirected_updates += 1
                        else:
                            existing = self.store.get(memory_ns, mem_id)
                            if not existing:
                                logging.warning(f"[MemoryManager] Update target {mem_id} not found, adding as new")
                        if not existing:
                            current_count += 1
                            new_count += 1
                            mem_key = f"mem_{self._allocate_memory_index()}"
                            entry = {
                                "category": update.get("category", "user_fact"),
                                "content": update["content"],
                                "embedding": embedding,
                                "embedding_model": embedding_model,
                                "source_range": source_range,
                                "created_at": now,
                                "updated_at": now,
                                "last_retrieved": now,
                                "pinned": False
                            }
                            self.store.put(memory_ns, mem_key, entry)
                            continue

                        data = existing.value.copy()
                        data["content"] = update["content"]
                        data["category"] = update.get("category", data.get("category", "user_fact"))
                        data["updated_at"] = now

                        # Pre-generated embedding (None = disabled or failed;
                        # keep the existing stored embedding in that case, same
                        # as the old in-lock regen behavior)
                        if embedding is not None:
                            data["embedding"] = embedding
                            data["embedding_model"] = embedding_model

                        self.store.put(memory_ns, mem_id, data)

                    # Update memory count
                    self.store.put(meta_ns, "memory_count", {"count": current_count})

                    # Advance last_extracted atomically with the fragments
                    self.store.put(meta_ns, "last_extracted", {"index": source_range[1]})

                self.store.commit()

                logging.info(
                    f"[MemoryManager] Applied: {len(results.get('additions', []))} additions, "
                    f"{len(results.get('updates', []))} updates"
                    + (f" ({redirected_updates} redirected to additions: id not offered to model)"
                       if redirected_updates else "")
                )

                # Cleanup old entries if over limit
                if new_count > 0:
                    self.cleanup_old_entries()

                return True

            except Exception as e:
                logging.error(f"[MemoryManager] Error applying extraction results: {e}")
                return False

    def _get_related_existing_memories(self, messages: List[Dict]) -> List[Dict]:
        """Get existing memories related to the given messages using centroid search."""
        try:
            # Compute centroid of message embeddings
            valid_embeddings = []
            for msg in messages:
                emb = msg.get("embedding")
                if emb is not None:
                    arr = np.array(emb, dtype=np.float32)
                    if len(arr) == self.expected_embedding_size:
                        valid_embeddings.append(arr)

            if not valid_embeddings:
                return []

            centroid = np.mean(valid_embeddings, axis=0)
            results = self.search_memories_by_similarity(centroid, top_k=MEMORY_RELATED_EXISTING_TOP_K)

            return [{"id": r["id"], "category": r.get("category", ""), "content": r.get("content", "")} for r in results]

        except Exception as e:
            logging.error(f"[MemoryManager] Error getting related memories: {e}")
            return []

    def cleanup_old_entries(self):
        """Remove oldest unpinned memories when over MAX_MEMORY_ENTRIES."""
        memory_ns = (self.character_id, "memories")
        meta_ns = (self.character_id, "meta")

        with self.db_lock:
            try:
                count_item = self.store.get(meta_ns, "memory_count")
                total = count_item.value.get("count", 0) if count_item else 0

                if total <= MAX_MEMORY_ENTRIES:
                    return

                excess = total - MAX_MEMORY_ENTRIES
                all_items = self.store.list(memory_ns)

                # Build candidate list (unpinned only)
                candidates = []
                for item in all_items:
                    if not item.value.get("pinned", False):
                        candidates.append((item.key, item.value.get("last_retrieved", "")))

                # Sort by last_retrieved ascending (oldest first)
                candidates.sort(key=lambda x: x[1])

                removed = 0
                for key, _ in candidates[:excess]:
                    self.store.delete(memory_ns, key)
                    removed += 1

                if removed > 0:
                    # Update memory count
                    new_total = total - removed
                    self.store.put(meta_ns, "memory_count", {"count": new_total})
                    self.store.commit()
                    logging.info(f"[MemoryManager] Cleaned up {removed} old memory entries")

            except Exception as e:
                logging.error(f"[MemoryManager] Error cleaning up old entries: {e}")

    def cleanup_old_messages(self):
        """Delete extraction-processed messages beyond MAX_MESSAGE_RETENTION."""
        meta_ns = (self.character_id, "meta")
        message_ns = (self.character_id, "messages")

        with self.db_lock:
            try:
                total = self.get_message_count()
                if total <= MAX_MESSAGE_RETENTION:
                    return

                last_extracted_item = self.store.get(meta_ns, "last_extracted")
                last_extracted = last_extracted_item.value.get("index", 0) if last_extracted_item else 0

                deleted_up_to_item = self.store.get(meta_ns, "deleted_up_to")
                deleted_up_to = deleted_up_to_item.value.get("index", 0) if deleted_up_to_item else 0

                # Only delete extracted messages
                delete_target = min(total - MAX_MESSAGE_RETENTION, last_extracted)
                if delete_target <= deleted_up_to:
                    return

                deleted = 0
                for i in range(deleted_up_to + 1, delete_target + 1):
                    key = f"msg_{i}"
                    if self.store.has(message_ns, key):
                        self.store.delete(message_ns, key)
                        deleted += 1

                self.store.put(meta_ns, "deleted_up_to", {"index": delete_target})
                self.store.commit()

                if deleted > 0:
                    logging.info(f"[MemoryManager] Cleaned up {deleted} old messages (up to index {delete_target})")

            except Exception as e:
                logging.error(f"[MemoryManager] Error cleaning up old messages: {e}")

    def _strip_extracted_embeddings(self):
        """Strip embeddings from extraction-processed messages.

        メッセージ埋め込みの唯一の読み手は抽出時の重心計算
        (_get_related_existing_memories)＝抽出済み分は以後どこからも読まれない
        (1件あたり本文の約200倍の死蔵データ)。last_extracted まで剥がし、進捗を
        embedding_stripped_up_to に記録する(deleted_up_to と同型の watermark)。

        冪等: 剥がしは何度実行しても本文を壊さないので、途中で落ちても次回の
        抽出後に印の位置からやり直すだけでよい。db_lock は1件ずつ取り、会話の
        プロンプト構築を長時間待たせない(embedding_migration と同じ様式)。
        エラーは記録して続行＝抽出のサーキットブレーカーに計上しない。
        """
        meta_ns = (self.character_id, "meta")
        message_ns = (self.character_id, "messages")

        try:
            with self.db_lock:
                last_item = self.store.get(meta_ns, "last_extracted")
                last_extracted = last_item.value.get("index", 0) if last_item else 0

                stripped_item = self.store.get(meta_ns, "embedding_stripped_up_to")
                stripped_up_to = stripped_item.value.get("index", 0) if stripped_item else 0

                deleted_item = self.store.get(meta_ns, "deleted_up_to")
                deleted_up_to = deleted_item.value.get("index", 0) if deleted_item else 0

            # deleted_up_to 以前は行ごと消えている＝読む必要がない
            start = max(stripped_up_to, deleted_up_to)
            if last_extracted <= start:
                return

            stripped = 0
            for i in range(start + 1, last_extracted + 1):
                key = f"msg_{i}"
                with self.db_lock:
                    item = self.store.get(message_ns, key)
                    if item is None or "embedding" not in item.value:
                        continue
                    value = item.value
                    value.pop("embedding", None)
                    self.store.put(message_ns, key, value)
                    stripped += 1

            with self.db_lock:
                self.store.put(meta_ns, "embedding_stripped_up_to", {"index": last_extracted})
                self.store.commit()

            if stripped > 0:
                logging.info(
                    f"[MemoryManager] Stripped embeddings from {stripped} extracted "
                    f"messages (up to index {last_extracted})"
                )

        except Exception as e:
            logging.error(f"[MemoryManager] Error stripping extracted embeddings: {e}")

    def prune_missing_attachment_refs(self) -> int:
        """Remove attachment references whose files no longer exist on disk.

        ファイルを消す側(添付の上限クリーンアップ・会話終了時のカメラ一時
        画像全消し)が、メッセージに残る参照も一緒に畳むための口
        (稜裁定 2026-08-20「捨てるときに住所メモも一緒に消す」)。
        実在確認ベース＝呼出元は消したパスを列挙しなくてよい。
        プロンプト/UI は元々 exists() で不在参照を黙って捨てるため、
        見た目・プロンプトのバイトは不変(整合だけが変わる)。

        文書は file_path キーだけ落とし、プロンプトが使う text/filename は
        温存する。db_lock は1件ずつ取り会話を待たせない
        (_strip_extracted_embeddings と同じ様式)。エラーは記録して続行＝
        呼出元(会話終了処理等)に漏らさない。

        Returns:
            int: 参照を除去したメッセージ数(エラー時は途中までの数)
        """
        message_ns = (self.character_id, "messages")
        changed = 0
        try:
            with self.db_lock:
                keys = [k for k in self.store.list_keys(message_ns)
                        if self._extract_msg_index(k) is not None]

            for key in keys:
                with self.db_lock:
                    item = self.store.get(message_ns, key)
                    if item is None:
                        continue
                    value = item.value
                    images = value.get("images") or []
                    kept_images = [p for p in images
                                   if Path(resolve_data_path(p)).exists()]
                    docs_changed = False
                    for doc in value.get("documents") or []:
                        if (isinstance(doc, dict) and doc.get("file_path")
                                and not Path(resolve_data_path(doc["file_path"])).exists()):
                            doc.pop("file_path", None)
                            docs_changed = True
                    if len(kept_images) == len(images) and not docs_changed:
                        continue
                    value["images"] = kept_images
                    self.store.put(message_ns, key, value)
                    changed += 1

            if changed > 0:
                logging.info(
                    f"[MemoryManager] Pruned dead attachment references from {changed} messages"
                )
        except Exception as e:
            logging.error(f"[MemoryManager] Error pruning attachment references: {e}")
        return changed

    def clear_all_memory(self) -> bool:
        """
        Clear all memory for this character (messages and memories).

        Returns:
            bool: True if successful, False otherwise
        """
        with self.db_lock:
            try:
                # Clear messages
                message_ns = (self.character_id, "messages")
                all_messages = self.store.list(message_ns)
                for item in all_messages:
                    self.store.delete(message_ns, item.key)

                # Clear memories
                memory_ns = (self.character_id, "memories")
                all_memories = self.store.list(memory_ns)
                for item in all_memories:
                    self.store.delete(memory_ns, item.key)

                # Reset metadata
                meta_ns = (self.character_id, "meta")
                self.store.put(meta_ns, "msg_count", {"count": 0})
                self.store.put(meta_ns, "memory_count", {"count": 0})
                self.store.put(meta_ns, "next_memory_id", {"index": 1})
                self.store.put(meta_ns, "last_extracted", {"index": 0})
                self.store.put(meta_ns, "deleted_up_to", {"index": 0})
                self.store.put(meta_ns, "embedding_stripped_up_to", {"index": 0})

                self.store.commit()

                logging.info(f"[MemoryManager] Cleared all memory for character {self.character_id}")
                return True

            except Exception as e:
                logging.error(f"[MemoryManager] Error clearing memory: {e}")
                return False

    def clear_short_term_only(self) -> bool:
        """
        Clear only short-term messages while preserving long-term memory entries.

        Clears:
        - All messages in the messages table
        - msg_count (reset to 0)
        - last_extracted (reset to 0)
        - deleted_up_to (reset to 0)
        - embedding_stripped_up_to (reset to 0)

        Preserves:
        - All entries in the memories table
        - memory_count

        Returns:
            bool: True if successful, False otherwise
        """
        with self.db_lock:
            try:
                message_ns = (self.character_id, "messages")
                all_messages = self.store.list(message_ns)
                for item in all_messages:
                    self.store.delete(message_ns, item.key)

                meta_ns = (self.character_id, "meta")
                self.store.put(meta_ns, "msg_count", {"count": 0})
                self.store.put(meta_ns, "last_extracted", {"index": 0})
                self.store.put(meta_ns, "deleted_up_to", {"index": 0})
                self.store.put(meta_ns, "embedding_stripped_up_to", {"index": 0})

                self.store.commit()

                logging.info(f"[MemoryManager] Cleared short-term memory for character {self.character_id}")
                return True

            except Exception as e:
                logging.error(f"[MemoryManager] Error clearing short-term memory: {e}")
                return False


# ---------------------------------------------------------------------------
# Background worker (moved from conversation_manager — B10 / SL3 Memory)
# ---------------------------------------------------------------------------


def run_memory_extraction(state, character_id: str) -> None:
    """
    Background task to extract long-term memories from unprocessed messages.
    Called when unprocessed message count reaches the extraction threshold.

    Moved verbatim from ConversationManager._extract_memories_task (B10):
    app-layer state (shutdown flag + memory_managers registry) is injected so the
    worker lives in its SL3 Memory home while the spine keeps only a thin delegate.
    """
    if state.shutdown_flag.is_set():
        logging.info(f"Skipping memory extraction for {character_id} - shutdown in progress")
        return

    memory_manager = state.memory_managers.get(character_id)
    if not memory_manager:
        logging.warning(f"No memory manager for {character_id}, skipping extraction")
        return

    if state.shutdown_flag.is_set():
        logging.info(f"Aborting memory extraction for {character_id} before LLM call - shutdown in progress")
        return

    try:
        memory_manager.extract_memories()
        logging.info(f"Memory extraction complete for {character_id}")
    except Exception as e:
        logging.error(f"Memory extraction failed for {character_id}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Memory data + CRUD API (moved from backend.py — B10 / SL3 Memory)
# ---------------------------------------------------------------------------


def _unwrap_character_config(config_response):
    """注入される load_character_config の戻り値から生 config を取り出す。

    注入元により2形がある:
    - 生ローダ(character_manager.load_character_config): config dict / {} (エラー時)
    - 公開API境界(backend.load_character_config): @standardize_response 済み
      {"success": bool, "result": config dict, ...}

    旧実装は後者を生 config 扱いして db_file_path を常に取りこぼし(常にデフォルトDBを
    開く)、失敗レスポンス(truthy な dict)も if config: ガードを素通りしていた。
    """
    if not isinstance(config_response, dict) or not config_response:
        return None
    if "success" in config_response:
        if not config_response.get("success"):
            return None
        result = config_response.get("result")
        return result if isinstance(result, dict) and result else None
    return config_response


def get_or_create_memory_manager(state, character_id: str, load_character_config):
    """Get or initialize the MemoryManager for a character.

    Moved verbatim from backend._get_memory_manager (B10): the app-layer state
    (the memory_managers registry) and the character-config loader are injected
    so the helper lives in its SL3 Memory home (domain <- injected app).
    """
    if character_id not in state.memory_managers:
        config = _unwrap_character_config(load_character_config(character_id))
        if config:
            db_path = config.get('db_file_path', f"{MEMORY_DIR}/{character_id}.db")
            state.memory_managers[character_id] = MemoryManager(db_path, character_id)
    return state.memory_managers.get(character_id)


def get_character_memory_data(state, character_id: str, load_character_config, load_character_list) -> Dict[str, Any]:
    """
    Get all memory data for a character for the History page.

    Moved verbatim from backend.get_character_memory_data (B10): app-layer state
    plus the character-config/list loaders are injected so the SL3 Memory home
    owns the data-shaping logic while the spine keeps only a thin delegate.

    Args:
        state: The conversation manager state (owns memory_managers registry)
        character_id: The character ID to get memory for
        load_character_config: Loader for a single character config (injected)
        load_character_list: Loader for the character list (injected)

    Returns:
        Dict containing:
        - short_term: List of recent messages
        - long_term: List of extracted memory entries
        - countdown: Messages until next extraction
        - stats: Memory statistics
    """
    try:
        # Validate character exists
        char_list_response = load_character_list()
        if not char_list_response.get('success'):
            return {"success": False, "error": "Failed to load character list"}

        char_list = char_list_response.get('result', [])
        if not any(c['id'] == character_id for c in char_list):
            return {"success": False, "error": "Character not found"}

        # Initialize memory manager if needed
        if character_id not in state.memory_managers:
            config = _unwrap_character_config(load_character_config(character_id))
            if config:
                db_path = config.get('db_file_path', f"{MEMORY_DIR}/{character_id}.db")
                try:
                    state.memory_managers[character_id] = MemoryManager(db_path, character_id)
                except Exception as e:
                    logging.error(f"Failed to initialize memory manager for {character_id}: {e}")
                    return {"success": False, "error": f"Failed to load memory: {str(e)}"}
            else:
                return {"success": False, "error": "Failed to load character configuration"}

        memory_manager = state.memory_managers.get(character_id)
        if not memory_manager:
            return {"success": False, "error": "Memory not available"}

        # Get all data
        try:
            short_term = memory_manager.get_recent_messages()
            long_term = memory_manager.get_all_memories()
        except Exception as e:
            logging.error(f"Error retrieving memory data: {e}")
            return {"success": False, "error": f"Failed to retrieve memory data: {str(e)}"}

        # Calculate countdown to next extraction
        try:
            total_messages = memory_manager.get_message_count()
            meta_ns = (character_id, "meta")
            last_extracted_item = memory_manager.store.get(meta_ns, "last_extracted")

            if last_extracted_item and hasattr(last_extracted_item, 'value'):
                if isinstance(last_extracted_item.value, dict):
                    last_extracted_index = last_extracted_item.value.get("index", 0)
                else:
                    last_extracted_index = 0
            else:
                last_extracted_index = 0

            # Determine threshold based on provider
            provider = memory_manager._get_character_provider()
            if provider == "ollama":
                threshold = EXTRACTION_THRESHOLD_OLLAMA
            else:
                threshold = EXTRACTION_THRESHOLD_API

            unprocessed_count = total_messages - last_extracted_index
            countdown = max(0, threshold - unprocessed_count)
        except Exception as e:
            logging.error(f"Error calculating countdown: {e}")
            total_messages = len(short_term)
            unprocessed_count = total_messages
            countdown = 0

        return {
            "short_term": short_term,
            "long_term": long_term,
            "countdown": countdown,
            "stats": {
                "total_messages": total_messages,
                "total_memories": len(long_term),
                "unprocessed": unprocessed_count
            }
        }
    except Exception as e:
        logging.error(f"Error getting memory data for {character_id}: {e}")
        return {"success": False, "error": str(e)}


def add_memory_entry(state, character_id: str, category: str, content: str, load_character_config) -> Dict[str, Any]:
    """Manually add a memory entry for a character (moved from backend.add_memory — B10 / SL3)."""
    mm = get_or_create_memory_manager(state, character_id, load_character_config)
    if not mm:
        return {"success": False, "error": "Memory not available"}
    return mm.add_memory_manual(category, content)


def edit_memory_entry(state, character_id: str, memory_id: str, content: str, category: str = None, *, load_character_config) -> Dict[str, Any]:
    """Edit a memory entry (moved from backend.edit_memory — B10 / SL3)."""
    mm = get_or_create_memory_manager(state, character_id, load_character_config)
    if not mm:
        return {"success": False, "error": "Memory not available"}
    return mm.edit_memory(memory_id, content, category)


def delete_memory_entry(state, character_id: str, memory_id: str, load_character_config) -> Dict[str, Any]:
    """Delete a memory entry (moved from backend.delete_memory — B10 / SL3)."""
    mm = get_or_create_memory_manager(state, character_id, load_character_config)
    if not mm:
        return {"success": False, "error": "Memory not available"}
    return mm.delete_memory(memory_id)


def pin_memory_entry(state, character_id: str, memory_id: str, pinned: bool, load_character_config) -> Dict[str, Any]:
    """Set pinned status of a memory entry (moved from backend.pin_memory — B10 / SL3)."""
    mm = get_or_create_memory_manager(state, character_id, load_character_config)
    if not mm:
        return {"success": False, "error": "Memory not available"}
    return mm.pin_memory(memory_id, pinned)