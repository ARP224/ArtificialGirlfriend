"""
backend/screen_capture.py

Multi-monitor screen capture module.
Uses mss library for reliable multi-monitor capture on Windows.
"""

import io
import logging
from typing import List

logger = logging.getLogger(__name__)

# Max width for resized screenshots (to reduce API cost)
MAX_CAPTURE_WIDTH = 1280


def capture_screenshots() -> List[bytes]:
    """
    Capture screenshots from all monitors using mss.

    mss handles multi-monitor environments correctly, including
    monitors with negative coordinates (left of primary monitor).

    Returns:
        List of PNG bytes, one per monitor (excluding the "all monitors" virtual screen).
        Empty list on error.
    """
    try:
        import mss
    except ImportError:
        logger.warning("[Screen Capture] mss not available, cannot capture screenshots")
        return []

    try:
        from PIL import Image
    except ImportError:
        logger.warning("[Screen Capture] Pillow not available, cannot resize screenshots")
        # Fall back to raw mss capture without resize
        return _capture_without_resize()

    screenshots = []
    try:
        with mss.mss() as sct:
            # sct.monitors[0] is the virtual screen (all monitors combined)
            # sct.monitors[1:] are the individual monitors
            monitors = sct.monitors[1:]

            if not monitors:
                logger.warning("[Screen Capture] No monitors detected by mss")
                return []

            # Sort by left position (left-to-right order)
            sorted_monitors = sorted(monitors, key=lambda m: (m["left"], m["top"]))

            for i, mon in enumerate(sorted_monitors):
                try:
                    # Capture this monitor
                    sct_img = sct.grab(mon)

                    # Convert to PIL Image for resize
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                    # Resize if wider than MAX_CAPTURE_WIDTH
                    if img.width > MAX_CAPTURE_WIDTH:
                        ratio = MAX_CAPTURE_WIDTH / img.width
                        new_height = int(img.height * ratio)
                        img = img.resize((MAX_CAPTURE_WIDTH, new_height), Image.LANCZOS)

                    # Convert to PNG bytes
                    buf = io.BytesIO()
                    img.save(buf, format="PNG", optimize=True)
                    png_bytes = buf.getvalue()
                    screenshots.append(png_bytes)

                    logger.debug(
                        f"[Screen Capture] Monitor {i}: {mon['width']}x{mon['height']} -> "
                        f"{img.width}x{img.height}, {len(png_bytes)} bytes"
                    )

                except Exception as e:
                    logger.error(f"[Screen Capture] Failed to capture monitor {i}: {e}")
                    continue

    except Exception as e:
        logger.error(f"[Screen Capture] Failed to initialize mss: {e}")
        return []

    logger.info(f"[Screen Capture] Captured {len(screenshots)} screenshots")
    return screenshots


def _capture_without_resize() -> List[bytes]:
    """Fallback: capture using mss only (no PIL resize)."""
    try:
        import mss
        import mss.tools
    except ImportError:
        return []

    screenshots = []
    try:
        with mss.mss() as sct:
            sorted_monitors = sorted(sct.monitors[1:], key=lambda m: (m["left"], m["top"]))
            for i, mon in enumerate(sorted_monitors):
                try:
                    sct_img = sct.grab(mon)
                    png_bytes = mss.tools.to_png(sct_img.rgb, sct_img.size)
                    screenshots.append(png_bytes)
                except Exception as e:
                    logger.error(f"[Screen Capture] Fallback capture failed for monitor {i}: {e}")
    except Exception as e:
        logger.error(f"[Screen Capture] Fallback mss init failed: {e}")

    return screenshots
