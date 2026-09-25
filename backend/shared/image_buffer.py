"""
backend/image_buffer.py

Thread-safe image buffer for accumulating user-attached images
across multiple sources (desktop UI, mobile companion) before
they are consumed by generate_reply.
"""

import base64
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import List

from backend.shared.constants import MAX_IMAGES_PER_MESSAGE

logger = logging.getLogger(__name__)


@dataclass
class ImageSlot:
    """A single image slot in the buffer."""
    file_path: str
    source: str  # "desktop", "mobile", "companion"
    added_at: float = field(default_factory=time.time)
    is_temp: bool = False  # True if we created this temp file (companion base64)


class ImageBuffer:
    """
    Thread-safe buffer for accumulating images before LLM consumption.

    Holds up to MAX_IMAGES_PER_MESSAGE slots. When full, the oldest
    slot is evicted (FIFO). Companion-sourced temp files are cleaned
    up on eviction/clear.
    """

    def __init__(self):
        self._slots: List[ImageSlot] = []
        self._lock = threading.Lock()


    def add_image_from_base64(self, b64_data: str, content_type: str,
                              source: str = "companion") -> int:
        """Decode base64 image data, save to temp file, add to buffer.

        Returns the new total count.
        """
        try:
            image_bytes = base64.b64decode(b64_data)
        except Exception as e:
            logger.error(f"[ImageBuffer] Base64 decode failed: {e}")
            return self.get_count()

        ext = ".jpg"
        if "png" in content_type:
            ext = ".png"
        elif "webp" in content_type:
            ext = ".webp"

        try:
            fd, temp_path = tempfile.mkstemp(suffix=ext, prefix="ag_companion_")
            with os.fdopen(fd, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            logger.error(f"[ImageBuffer] Temp file write failed: {e}")
            return self.get_count()

        with self._lock:
            if len(self._slots) >= MAX_IMAGES_PER_MESSAGE:
                evicted = self._slots.pop(0)
                self._cleanup_slot(evicted)
            self._slots.append(ImageSlot(
                file_path=temp_path,
                source=source,
                added_at=time.time(),
                is_temp=True,
            ))
            count = len(self._slots)
        logger.info(f"[ImageBuffer] Added base64 image from {source}: "
                     f"{len(image_bytes)} bytes -> {temp_path} ({count}/{MAX_IMAGES_PER_MESSAGE})")
        return count

    def consume_all(self) -> List[str]:
        """Return all image paths and clear the buffer.

        Temp files are NOT deleted here — the consumer (generate_reply)
        is responsible for the files until it's done with them.
        """
        with self._lock:
            paths = [s.file_path for s in self._slots]
            self._slots.clear()
        if paths:
            logger.info(f"[ImageBuffer] Consumed {len(paths)} images")
        return paths

    def peek_all(self) -> List[str]:
        """Return all image paths without consuming them."""
        with self._lock:
            return [s.file_path for s in self._slots]

    def get_count(self) -> int:
        """Return current number of images in the buffer."""
        with self._lock:
            return len(self._slots)

    def clear(self, cleanup_files: bool = True) -> None:
        """Clear all slots. Optionally delete temp files."""
        with self._lock:
            if cleanup_files:
                for slot in self._slots:
                    self._cleanup_slot(slot)
            self._slots.clear()
        logger.debug("[ImageBuffer] Cleared")


    @staticmethod
    def _cleanup_slot(slot: ImageSlot) -> None:
        """Delete the temp file for a slot if it was created by us."""
        if slot.is_temp and slot.file_path:
            try:
                os.unlink(slot.file_path)
                logger.debug(f"[ImageBuffer] Cleaned up temp file: {slot.file_path}")
            except OSError:
                pass
