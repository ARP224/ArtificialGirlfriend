"""
backend/shared/activation_registry.py

Character-activation locking registry for Artificial Girlfriend (shared/state
layer).

This is the home for the *runtime* locks that serialise character activation and
per-character operations (activate / remove) so concurrent switches cannot race.
It came out of the BackendState god-object split: these fields used to live
directly on ``backend/backend.py``'s
``BackendState`` and the per-character lock accessor was a method on it. The live
call sites are in ``backend/conversation/character_manager.py``
(``with state.activation_lock:`` and ``state.get_character_operation_lock(...)``).

Note on scope: the broader activation *transaction* cluster that used to
sit alongside these locks — the ``CharacterActivationState`` dataclass, the
``begin_activation`` / ``commit_activation`` / ``rollback_activation`` methods, and
the ``_pending_activation`` / ``_activating_character`` fields — was verified dead
(no callers anywhere in the repo, tests, or dynamic ``getattr``) and removed under
the touch-once rule during this carve-out (recorded as Scope-out). Only the locks
that are actually used survive here.

Layering: this module owns the lock container plus the one accessor that lazily
creates per-character locks. It has no backend/ui imports — only the standard
library — and depends downward only (mirrors FeatureState / CommandState).
"""

import threading
from typing import Dict


class ActivationRegistry:
    """Owns the character-activation locks carved out of BackendState.

    BackendState holds a single instance as ``_backend_state.activation`` and keeps
    backward-compat properties for the legacy ``_backend_state.<field>`` access
    paths, so existing call sites are unchanged while ownership now lives here.
    Defaults mirror the values that used to be set directly in
    ``BackendState.__init__``.
    """

    def __init__(self) -> None:
        # Serialises character activation to prevent concurrent switches.
        self.activation_lock: threading.Lock = threading.Lock()

        # Per-character operation locks (activate/remove) created on demand,
        # guarded by character_locks_lock.
        self.character_operation_locks: Dict[str, threading.RLock] = {}
        self.character_locks_lock: threading.Lock = threading.Lock()

    def get_character_operation_lock(self, character_id: str) -> threading.RLock:
        """Get lock for character operations (activate/remove) to prevent race conditions.

        Args:
            character_id: The character ID to get a lock for

        Returns:
            threading.RLock: The lock for this character's operations
        """
        with self.character_locks_lock:
            if character_id not in self.character_operation_locks:
                self.character_operation_locks[character_id] = threading.RLock()
            return self.character_operation_locks[character_id]
