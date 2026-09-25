"""
backend/document_extractor.py

Text extraction from document files for the document attachment feature.
Supports plain text/code files only (PDF/DOCX は動作検証未了のため公開範囲
から除外=稜裁定 2026-08-15。pypdf / python-docx 依存も撤去済み)。
"""

import logging
from pathlib import Path
from typing import Dict, Any

from backend.shared.constants import (
    VALID_ATTACHMENT_DOCUMENT_EXTENSIONS,
    MAX_DOCUMENT_CHARS_IN_PROMPT,
)

logger = logging.getLogger(__name__)


def extract_text(file_path: str) -> Dict[str, Any]:
    """
    Extract text content from a document file.

    Args:
        file_path: Path to the document file

    Returns:
        {
            "success": bool,
            "text": str,          # extracted text (on success)
            "char_count": int,    # length of extracted text
            "filename": str,      # original filename
            "error": str,         # error message (on failure)
            "error_code": str,    # 機械可読の失敗理由(失敗時のみ):
                                  # not_found / unsupported / read_failed /
                                  # empty / too_large。UI側のi18n通知が使う
        }
    """
    path = Path(file_path)
    filename = path.name
    suffix = path.suffix.lower()

    if not path.exists():
        return _error(filename, f"File not found: {file_path}", "not_found")

    if suffix not in VALID_ATTACHMENT_DOCUMENT_EXTENSIONS:
        return _error(filename, f"Unsupported file type: {suffix}", "unsupported")

    try:
        text = _extract_plain_text(path)
    except Exception as e:
        logger.error(f"[DocumentExtractor] Failed to extract text from {filename}: {e}")
        return _error(filename, f"Failed to read file: {e}", "read_failed")

    char_count = len(text)

    if char_count == 0:
        return _error(filename, "File is empty or contains no extractable text", "empty")

    if char_count > MAX_DOCUMENT_CHARS_IN_PROMPT:
        return _error(
            filename,
            f"File too large: {char_count:,} chars (max {MAX_DOCUMENT_CHARS_IN_PROMPT:,})",
            "too_large",
        )

    logger.info(f"[DocumentExtractor] Extracted {char_count:,} chars from {filename}")
    return {
        "success": True,
        "text": text,
        "char_count": char_count,
        "filename": filename,
        "error": "",
        "error_code": "",
    }


def _extract_plain_text(path: Path) -> str:
    """Read plain text/code files with encoding fallback."""
    raw = path.read_bytes()

    # Try encodings in order of likelihood for Japanese users.
    # utf-8-sig must precede utf-8: a BOM-prefixed UTF-8 file decodes fine as
    # plain utf-8 but leaves a stray U+FEFF at the start of the text (which then
    # leaks into the prompt). utf-8-sig strips the BOM; on BOM-less files it
    # behaves identically to utf-8.
    for encoding in ("utf-8-sig", "utf-8", "shift_jis", "cp932", "euc-jp", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # latin-1 never fails, so we should never reach here,
    # but just in case:
    return raw.decode("latin-1", errors="replace")


def _error(filename: str, message: str, error_code: str) -> Dict[str, Any]:
    """Build an error result."""
    return {
        "success": False,
        "text": "",
        "char_count": 0,
        "filename": filename,
        "error": message,
        "error_code": error_code,
    }
