"""Portable output names.

Truncating by characters is not enough: a 150-character Chinese name is 450
bytes in UTF-8, and ext4 rejects names over 255 bytes with ENAMETOOLONG. Windows
additionally forbids a set of characters and reserves device names, so a source
file called "a:b.pdf" or "con.pdf" would produce an output that cannot be created.
"""
from __future__ import annotations

import sys

# Longest single path component allowed by common filesystems (bytes).
NAME_MAX_BYTES = 255

# Reserved on Windows regardless of extension, case-insensitively.
_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_WINDOWS_INVALID = set('<>:"/\\|?*')


def sanitize(name: str, max_bytes: int = NAME_MAX_BYTES) -> str:
    """Make one path component safe on this platform, keeping it recognisable."""
    if sys.platform == "win32":
        name = "".join("_" if ch in _WINDOWS_INVALID or ord(ch) < 32 else ch for ch in name)
        name = name.rstrip(" .")          # Windows silently strips these
        stem, dot, ext = name.partition(".")
        if stem.upper() in _WINDOWS_DEVICES:
            name = "_" + name
    else:
        name = name.replace("/", "_").replace("\x00", "_")
    if not name.strip():
        return "_"
    return truncate(name, max_bytes)


def truncate(name: str, max_bytes: int = NAME_MAX_BYTES) -> str:
    """Shorten to fit the byte limit without splitting a character or the suffix."""
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    # Keep the extension so the file still opens in the right application.
    dot = name.rfind(".")
    ext = name[dot:] if dot > 0 and len(name) - dot <= 16 else ""
    budget = max_bytes - len(ext.encode("utf-8"))
    out = []
    used = 0
    for ch in name[: len(name) - len(ext)]:
        size = len(ch.encode("utf-8"))
        if used + size > budget:
            break
        out.append(ch)
        used += size
    return "".join(out) + ext


# The longest suffix the per-page render appends: "_page00001.jpeg".
_PAGE_SUFFIX_BYTES = len(b"_page00001.jpeg")


def page_stem(stem: str) -> str:
    """The stem for intermediate page files, shortened to leave room for the suffix.

    A PDF may legally be named with 243 UTF-8 bytes (81 Chinese characters), yet
    appending "_page00001.jpeg" reaches 258 bytes: rendering then failed with
    ENAMETOOLONG before a single page was written, so the whole export died on a
    file the filesystem had happily accepted. Truncate here, on a character
    boundary, and keep every caller using this one function so the names the
    renderer writes and the names the verifier expects cannot drift apart.
    """
    limit = NAME_MAX_BYTES - _PAGE_SUFFIX_BYTES
    if len(stem.encode("utf-8")) <= limit:
        return stem
    kept, used = [], 0
    for ch in stem:
        size = len(ch.encode("utf-8"))
        if used + size > limit:
            break
        kept.append(ch)
        used += size
    return "".join(kept) or "_"


def path_fits(relative) -> bool:
    """True when every component of a relative path fits the byte limit."""
    return all(len(part.encode("utf-8")) <= NAME_MAX_BYTES for part in relative.parts)
