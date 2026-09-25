"""
backend/document_buffer.py

Thread-safe document buffer for accumulating user-attached documents
across multiple sources (desktop UI, mobile) before they are consumed
by generate_reply. Parallel to ImageBuffer but for document files.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from backend.shared.constants import MAX_DOCUMENTS_PER_MESSAGE

logger = logging.getLogger(__name__)


@dataclass
class DocumentSlot:
    """A single document slot in the buffer."""
    file_path: str
    source: str                # "desktop", "mobile"
    original_filename: str
    extracted_text: str
    char_count: int
    added_at: float = field(default_factory=time.time)
    is_temp: bool = False      # True if we created a temp file


class DocumentBuffer:
    """
    Thread-safe buffer for accumulating documents before LLM consumption.

    Holds up to MAX_DOCUMENTS_PER_MESSAGE slots. When full, the oldest
    slot is evicted (FIFO).
    """

    def __init__(self):
        self._slots: List[DocumentSlot] = []
        self._lock = threading.Lock()

    def add_document(
        self,
        file_path: str,
        filename: str,
        text: str,
        char_count: int,
        source: str = "desktop",
    ) -> int:
        """Add a pre-extracted document to the buffer.

        The caller is responsible for extracting text beforehand
        (via document_extractor.extract_text).

        Returns the new total count.
        """
        with self._lock:
            if len(self._slots) >= MAX_DOCUMENTS_PER_MESSAGE:
                evicted = self._slots.pop(0)
                self._cleanup_slot(evicted)
            self._slots.append(DocumentSlot(
                file_path=file_path,
                source=source,
                original_filename=filename,
                extracted_text=text,
                char_count=char_count,
                added_at=time.time(),
                is_temp=False,
            ))
            count = len(self._slots)
        logger.debug(
            f"[DocumentBuffer] Added '{filename}' from {source}: "
            f"{char_count} chars ({count}/{MAX_DOCUMENTS_PER_MESSAGE})"
        )
        return count

    def consume_all(self) -> List[Dict[str, Any]]:
        """Return all document info and clear the buffer.

        Returns a list of dicts with keys:
            filename, text, char_count, file_path
        """
        with self._lock:
            docs = [
                {
                    "filename": s.original_filename,
                    "text": s.extracted_text,
                    "char_count": s.char_count,
                    "file_path": s.file_path,
                }
                for s in self._slots
            ]
            self._slots.clear()
        if docs:
            logger.info(f"[DocumentBuffer] Consumed {len(docs)} documents")
        return docs

    def get_count(self) -> int:
        """Return current number of documents in the buffer."""
        with self._lock:
            return len(self._slots)

    def get_total_chars(self) -> int:
        """Return total character count of all documents in the buffer."""
        with self._lock:
            return sum(s.char_count for s in self._slots)

    def clear(self, cleanup_files: bool = True) -> None:
        """Clear all slots. Optionally delete temp files."""
        with self._lock:
            if cleanup_files:
                for slot in self._slots:
                    self._cleanup_slot(slot)
            self._slots.clear()
        logger.debug("[DocumentBuffer] Cleared")

    @staticmethod
    def _cleanup_slot(slot: DocumentSlot) -> None:
        """Delete the temp file for a slot if it was created by us."""
        if slot.is_temp and slot.file_path:
            try:
                os.unlink(slot.file_path)
                logger.debug(f"[DocumentBuffer] Cleaned up temp file: {slot.file_path}")
            except OSError:
                pass
