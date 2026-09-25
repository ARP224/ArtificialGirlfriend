"""
backend/image_storage.py

Image storage management for AI-generated images.
Handles saving, thumbnail creation, FIFO cleanup, and path resolution.
"""

import os
import hashlib
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional, List

from backend.shared.constants import GENERATED_IMAGES_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_THUMBNAIL_SIZE = (512, 512)
MAX_GENERATED_IMAGES_PER_CHARACTER = 100


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _ensure_dirs(character_id: str) -> Tuple[Path, Path]:
    """Ensure full-size and thumbnail directories exist for a character."""
    full_dir = GENERATED_IMAGES_DIR / character_id
    thumb_dir = full_dir / "thumbnails"
    full_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    return full_dir, thumb_dir


# ---------------------------------------------------------------------------
# Save & thumbnail
# ---------------------------------------------------------------------------

def save_generated_image(
    character_id: str,
    image_bytes: bytes,
    prompt: str = "",
) -> Tuple[str, str]:
    """Save generated image (full-size + thumbnail).

    Args:
        character_id: Character UUID.
        image_bytes: Raw image bytes (PNG/JPEG).
        prompt: Generation prompt (used only for logging).

    Returns:
        Tuple of (full_path, thumbnail_path) as strings.

    Raises:
        RuntimeError: On save failure.
    """
    full_dir, thumb_dir = _ensure_dirs(character_id)

    # Generate unique filename
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    file_hash = hashlib.md5(image_bytes).hexdigest()[:8]
    filename = f"{ts}_{file_hash}.png"

    full_path = full_dir / filename
    thumb_path = thumb_dir / filename

    try:
        # Save full-size image
        with open(full_path, "wb") as f:
            f.write(image_bytes)

        # Create thumbnail
        _create_thumbnail(full_path, thumb_path)

        logger.info(
            f"[ImageStorage] Saved generated image for {character_id}: "
            f"{filename} ({len(image_bytes)} bytes)"
        )

        # Cleanup old images if over limit
        cleanup_old_images(character_id)

        return str(full_path), str(thumb_path)

    except Exception as e:
        # Cleanup partial writes
        for p in (full_path, thumb_path):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        logger.error(f"[ImageStorage] Failed to save image for {character_id}: {e}")
        raise RuntimeError(f"画像の保存に失敗しました: {e}") from e


def _create_thumbnail(source_path: Path, thumb_path: Path) -> None:
    """Create a resized thumbnail from the source image."""
    try:
        from PIL import Image
        with Image.open(source_path) as img:
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.thumbnail(IMAGE_THUMBNAIL_SIZE, Image.LANCZOS)
            img.save(thumb_path, format="PNG", optimize=True)
    except ImportError:
        # PIL not available — just copy the original as thumbnail
        import shutil
        shutil.copy2(str(source_path), str(thumb_path))
        logger.warning("[ImageStorage] PIL not available, using original as thumbnail")
    except Exception as e:
        # Fallback: copy original
        import shutil
        shutil.copy2(str(source_path), str(thumb_path))
        logger.warning(f"[ImageStorage] Thumbnail creation failed, using original: {e}")


# ---------------------------------------------------------------------------
# Cleanup (FIFO)
# ---------------------------------------------------------------------------

def cleanup_old_images(
    character_id: str,
    max_count: int = MAX_GENERATED_IMAGES_PER_CHARACTER,
) -> List[str]:
    """Delete oldest images when exceeding max_count.

    Also deletes corresponding thumbnails.

    Args:
        character_id: Character UUID.
        max_count: Maximum images to keep.

    Returns:
        List of deleted full-size image paths.
    """
    full_dir = GENERATED_IMAGES_DIR / character_id
    thumb_dir = full_dir / "thumbnails"

    if not full_dir.exists():
        return []

    # List full-size images (exclude thumbnails directory)
    image_files = sorted(
        [f for f in full_dir.iterdir() if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg")],
        key=lambda f: f.stat().st_mtime,
    )

    if len(image_files) <= max_count:
        return []

    deleted = []
    to_remove = image_files[:len(image_files) - max_count]

    for img_file in to_remove:
        try:
            # Delete thumbnail too
            thumb_file = thumb_dir / img_file.name
            if thumb_file.exists():
                thumb_file.unlink()

            img_file.unlink()
            deleted.append(str(img_file))
            logger.debug(f"[ImageStorage] Deleted old image: {img_file.name}")
        except Exception as e:
            logger.warning(f"[ImageStorage] Failed to delete {img_file}: {e}")

    if deleted:
        logger.info(
            f"[ImageStorage] Cleaned up {len(deleted)} old images for {character_id}"
        )

    return deleted


def remove_character_images(character_id: str) -> None:
    """Remove a character's generated-image folder (character deletion). No error if absent."""
    full_dir = GENERATED_IMAGES_DIR / character_id
    if not full_dir.exists():
        return
    try:
        shutil.rmtree(full_dir)
        logger.info(f"Removed generated images for {character_id}")
    except Exception as e:
        logger.error(f"Failed to remove generated images for {character_id}: {e}")


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def get_thumbnail_path_for(full_path: str) -> Optional[str]:
    """Derive the thumbnail path from a full-size image path.

    Args:
        full_path: Path to the full-size image.

    Returns:
        Thumbnail path if it exists, None otherwise.
    """
    p = Path(full_path)
    thumb = p.parent / "thumbnails" / p.name
    if thumb.exists():
        return str(thumb)
    return None


def is_generated_image(path: str) -> bool:
    """Check if a path belongs to generated_images directory."""
    try:
        return "generated_images" in Path(path).parts
    except Exception:
        return False


def get_placeholder_html() -> str:
    """Return HTML placeholder for a deleted image."""
    return (
        '<div style="display:inline-block; padding:12px 16px; '
        'background:#2a2a2a; border:1px dashed #555; border-radius:8px; '
        'color:#888; font-size:0.85em; margin:4px 0;">'
        'この画像は削除されました'
        '</div>'
    )
