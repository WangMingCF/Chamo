"""Read and write text as UTF-8 regardless of the platform default.

Every JSON file this engine writes contains non-ASCII text (Chinese source names,
Chinese error messages). Opening them without an explicit encoding uses the
locale default, which is cp1252 or cp936 on Chinese/European Windows, so the
supervisor fails to parse its own worker results and the whole run collapses with
"fatal" — observed on windows-latest in CI.

These helpers exist so no call site has to remember.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENCODING = "utf-8"


def read_text(path) -> str:
    return Path(path).read_text(encoding=ENCODING)


def write_text(path, text: str) -> None:
    Path(path).write_text(text, encoding=ENCODING)


def read_json(path):
    return json.loads(read_text(path))


def write_json(path, data) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def open_text(path, mode: str = "r", **kwargs):
    """Open a text file as UTF-8; `newline` stays caller-controlled."""
    return open(path, mode, encoding=ENCODING, **kwargs)


def decode_stdout(data: bytes) -> str:
    """Decode a child process stream that this engine always writes as UTF-8."""
    return data.decode(ENCODING, "replace")
