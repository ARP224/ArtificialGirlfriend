"""
backend/shared/memory_caches.py

Per-character LLM/memory caches for Artificial Girlfriend (shared/state layer).

This is the home for the *runtime* per-character caches that the conversation /
memory subsystem keeps in memory: the LRU-ish cache of live LLM chat instances,
the per-character MemoryManager instances, the compiled memory graphs, the
running message counters, the short-term message buffers, and the lock that
guards mutations of those buffers/counters. It came out of the
BackendState god-object split: these fields used to live
directly on ``backend/backend.py``'s ``BackendState`` and are read/written in
place by ``conversation_manager`` (chat loop / memory persistence),
``backend/conversation/character_manager.py`` (resource creation / activation /
LLM-cache eviction), ``backend/memory`` workers, ``backend.py`` (shutdown
salvage / clear), and the ELYTH session/relationship managers.

Note on placement: conceptually this is "memory domain" state, but it was placed
in ``backend/shared/`` alongside the other state carve-outs (CommandState /
ActivationRegistry) at a time when a bare ``memory/`` .gitignore rule still
shadowed ``backend/memory/`` (any new file there was silently untracked; the rule
has since been root-anchored). It is pure stdlib and has no layering dependency
on its physical location.

The ``active_llm_cache`` is an ``OrderedDict`` used as an LRU: callers
``move_to_end`` on reuse and ``popitem(last=False)`` to evict the oldest once
``MAX_LLM_CACHE_SIZE`` is reached. The cap is owned here alongside the cache it
governs.

Layering: this module owns no behavior — only the state container (the lock
accessors / cache eviction logic stay with their callers and reach these fields
via the backward-compat properties on BackendState). So this module has no
backend/ui imports — only the standard library — and depends downward only
(mirrors FeatureState / CommandState / ActivationRegistry).
"""

import threading
from collections import OrderedDict
from typing import Any, Dict, List


class MemoryCaches:
    """Owns the per-character LLM/memory caches carved out of BackendState.

    BackendState holds a single instance as ``_backend_state.memory_caches`` and
    keeps backward-compat properties for the legacy ``_backend_state.<field>``
    access paths, so existing call sites are unchanged while ownership now lives
    here. Defaults mirror the values that used to be set directly in
    ``BackendState.__init__``.
    """

    def __init__(self) -> None:
        # Cache of live DirectOllamaChat / API chat instances, keyed by
        # character_id. Used as an LRU: move_to_end on reuse, popitem(last=False)
        # to evict once MAX_LLM_CACHE_SIZE is reached.
        self.active_llm_cache: "OrderedDict[str, Any]" = OrderedDict()
        self.MAX_LLM_CACHE_SIZE = 5  # Keep max 5 LLM instances to prevent memory leak

        # Total messages seen so far per character.
        self.message_count_cache: Dict[str, int] = {}  # Tracks how many total messages so far
        # The last ~100 messages per character.
        self.short_term_buffer: Dict[str, List[Dict[str, Any]]] = {}  # For the last ~100 messages

        # MemoryManager instances, one per character.
        self.memory_managers: Dict[str, Any] = {}  # Will store MemoryManager instances

        # Guards short_term_buffer / message_count_cache mutations.
        self.memory_lock = threading.Lock()
