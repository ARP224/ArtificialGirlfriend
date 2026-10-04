"""
backend/shared/atomic_io.py

The last step of an atomic write (tmp file + os.replace), hardened for Windows.

On Windows os.replace fails with PermissionError (WinError 5) while ANY handle
is open on the target: an antivirus scan, the search indexer, an editor, or a
reader in another thread. Without a retry the write is simply lost (実機
2026-07-17: 設定の保存が消えた / 2026-10-04: YouTube の投稿ワーカーが落ちた).

A retry only covers holders that let go on their own. Readers inside this
process that can overlap a write must also share a lock with the writer
(先例: settings_store._settings_lock / youtube_store._store_lock) — a read
that lands mid-replace fails too and is easily mistaken for "no data".
"""

import os
import time
from pathlib import Path
from typing import Union

_ATTEMPTS = 10
_RETRY_DELAY = 0.05  # seconds


def replace_with_retry(src: Union[str, Path], dst: Union[str, Path]) -> None:
    """os.replace(src, dst), retrying briefly on PermissionError.

    Gives up after ~0.5s and re-raises the PermissionError; any other error
    propagates immediately. The caller still owns the tmp-file cleanup.
    """
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_DELAY)
