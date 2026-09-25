"""LLM request tracking carved out of BackendState (M13 §3 direct-field fix).

Owns the in-flight LLM task table (request_id -> status/timestamps) and the
lock that guards it. Requests are touched from multiple real threads (UI +
extraction / relationship background workers), so every init/iterate/mutate
of `requests` must hold `lock`.
"""

import threading
from typing import Any, Dict


class LlmRequestTracker:
    """Owning object for pending LLM request bookkeeping."""

    def __init__(self) -> None:
        self.requests: Dict[str, Dict[str, Any]] = {}
        self.lock: threading.Lock = threading.Lock()
