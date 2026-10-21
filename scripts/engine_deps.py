"""Third-party wheels the engine needs at run time, pinned.

Shared by the Linux and Windows packagers so the two artifacts cannot disagree
about what the engine imports. Test-only packages (pytest, reportlab) and unused
lock entries are deliberately absent.
"""
from __future__ import annotations

RUNTIME = [
    "pillow==12.3.0",
    "pypdf==6.21.1",
    # pdf_compress lossless rewrites with pikepdf (engine/workbench.py imports it
    # lazily). Missing from this list means the installed package, not the source
    # tree, raises ImportError the first time the new operation runs.
    "pikepdf==10.17.0",
    "pypdfium2==5.14.0",
    "img2pdf==0.6.3",
    "lxml==6.1.3",
    "olefile==0.47",
    "openpyxl==3.1.5",
    "xlrd==2.0.2",
]

# Version of the Windows embeddable distribution used for the portable build. It
# must match the CPython minor version the wheels are built for.
WINDOWS_PYTHON = "3.12.7"
