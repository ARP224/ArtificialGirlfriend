"""Attachment handlers for the desktop conversation UI.

Extracted from ui/app.py (B11m). Document attachment ingestion and the
combined text+attachment clear action. Images are handled out-of-band via
WebSocket; _handle_attach keeps a defensive fallback that ignores them.
"""
import logging
import os

from ..conversation import clear_text_input

logger = logging.getLogger(__name__)


def notify_attach_rejected(filename: str, error_code: str) -> None:
    """添付の抽出失敗/非対応形式をトーストで通知する(デスクトップ/モバイル共用)。

    旧実装は失敗を無言でスキップし「PDFを送っても何も起きない=非対応に
    見える」体験を生んでいた(稜裁定 2026-08-15: 無言スキップ廃止)。
    配送はWSの attach_image_rejected アクション流用=モバイル(gr toast非表示)
    にも届く。error_code は document_extractor.extract_text の機械可読理由。
    """
    from backend.shared.i18n import t
    key = {
        "too_large": "attach.doc_too_large",
        "empty": "attach.doc_no_text",
        "unsupported": "attach.doc_unsupported",
    }.get(error_code, "attach.doc_read_failed")
    kwargs = {"name": filename}
    if error_code == "too_large":
        from backend.shared.constants import MAX_DOCUMENT_CHARS_IN_PROMPT
        kwargs["max"] = f"{MAX_DOCUMENT_CHARS_IN_PROMPT:,}"
    try:
        from backend.server.websocket_server import get_websocket_manager
        get_websocket_manager().broadcast_attach_rejected_sync(
            t("attach.rejected_title"), t(key, **kwargs))
    except Exception:
        logger.warning(f"Failed to deliver attach-rejected toast for {filename}")


# --- Attachment handler (documents only) ---
# Phase 1A: Images are filtered out by the JS attach hook and sent via
# WebSocket (`attach_image` action) — they never reach this handler.
# As a defensive fallback, image-extension files are silently ignored
# here so the buffer cannot be polluted if DataTransfer unavailability
# lets one through.
#
# The `attach_status` Markdown is no longer updated here. The server-side
# `_broadcast_image_slot_update` is the single source of truth — it
# reads both image_buffer and document_buffer counts and pushes one
# combined text to all clients via WebSocket.
def _handle_attach(files, current_images, current_docs):
    from backend.shared.constants import (
        VALID_ATTACHMENT_IMAGE_EXTENSIONS,
        VALID_ATTACHMENT_DOCUMENT_EXTENSIONS,
    )
    if not files:
        return [], current_docs or [], ""

    docs = list(current_docs or [])
    doc_added = False

    for f in files:
        path = f if isinstance(f, str) else getattr(f, 'name', str(f))
        ext = os.path.splitext(path)[1].lower()

        if ext in VALID_ATTACHMENT_IMAGE_EXTENSIONS or ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            # JS hook should have intercepted; ignore as defensive fallback
            continue

        elif ext in VALID_ATTACHMENT_DOCUMENT_EXTENSIONS:
            # Document file — extract text
            from backend.shared.document_extractor import extract_text
            result = extract_text(path)
            if not result["success"]:
                logger.warning(f"Document extraction failed: {result['error']}")
                notify_attach_rejected(result["filename"], result.get("error_code", ""))
                continue
            doc_info = {
                "filename": result["filename"],
                "text": result["text"],
                "char_count": result["char_count"],
                "file_path": path,
            }
            docs.append(doc_info)
            try:
                from backend.backend import _backend_state
                if _backend_state:
                    _backend_state.document_buffer.add_document(
                        file_path=path,
                        filename=result["filename"],
                        text=result["text"],
                        char_count=result["char_count"],
                        source="desktop",
                    )
                    doc_added = True
            except Exception:
                pass
        else:
            # Unsupported extension — 無言スキップせず通知(稜裁定 2026-08-15)
            logger.warning(f"Unsupported attachment extension: {path}")
            notify_attach_rejected(os.path.basename(path), "unsupported")

    # Trigger broadcast so JS-managed attach-status reflects new doc count
    if doc_added:
        try:
            from backend.backend import _backend_state
            from backend.server.websocket_server import get_websocket_manager
            if _backend_state:
                get_websocket_manager().broadcast_image_slot_update_sync(
                    _backend_state.image_buffer.get_count()
                )
        except Exception:
            pass

    return [], docs, ""


def _clear_text_and_attachments():
    result = clear_text_input()
    # Clear image buffer and document buffer, notify all clients
    try:
        from backend.backend import _backend_state
        if _backend_state:
            _backend_state.image_buffer.clear()
            _backend_state.document_buffer.clear()
            from backend.server.websocket_server import get_websocket_manager
            get_websocket_manager().broadcast_image_slot_update_sync(0)
    except Exception:
        pass
    # result is (text, status) - add empty images, empty docs, and attach status
    return result[0], result[1], [], [], ""
