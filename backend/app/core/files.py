"""Safe file handling: content sniffing (never trust extensions or client MIME types)."""

from __future__ import annotations

import re
import unicodedata


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def looks_like_text(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    try:
        data[:65536].decode("utf-8")
        return True
    except UnicodeDecodeError:
        # tolerate a truncated multi-byte char at the boundary
        try:
            data[:65532].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False


def decode_text(data: bytes) -> str:
    text = data.decode("utf-8-sig", errors="strict")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sniff_document(data: bytes, filename: str) -> str | None:
    """Return 'pdf' | 'text' | 'csv' | None (unsupported)."""
    name = filename.lower()
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data[:4] in (b"PK\x03\x04",) or data[:2] == b"MZ" or data[:4] == b"\x7fELF":
        return None  # archives / executables / office zips are not accepted
    if looks_like_text(data):
        return "csv" if name.endswith(".csv") else "text"
    return None


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str, default: str = "file") -> str:
    name = unicodedata.normalize("NFKC", name).replace("\\", "/").split("/")[-1]
    name = _SAFE.sub("_", name).strip(" .")
    return (name or default)[:120]
