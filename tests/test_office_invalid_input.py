"""A file that is not the Office document its suffix claims must fail, not "convert".

LibreOffice imports unrecognised content as plain text and exits 0 with a PDF of that
text; the engine used to report such an item as complete. The check runs before any
renderer is looked up, so these tests need no LibreOffice.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench  # noqa: E402


def test_text_and_random_bytes_under_an_office_suffix_are_rejected(tmp_path):
    text = tmp_path / "deck.pptx"
    text.write_bytes(b"not a zip at all")
    noise = tmp_path / "memo.docx"
    noise.write_bytes(bytes(range(256)) * 12)
    for path in (text, noise):
        assert workbench.office_container_problem(path), path
        with pytest.raises(ValueError, match="不是有效的"):
            workbench.office_pdf(path, tmp_path / ("work-" + path.stem), {})


def test_a_real_zip_container_and_real_mislabelled_documents_still_pass(tmp_path):
    real = tmp_path / "sheet.xlsx"
    with zipfile.ZipFile(real, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    legacy = tmp_path / "old.docx"          # a genuine binary .doc saved under .docx
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
    rtf = tmp_path / "letter.docx"          # an RTF saved under .docx
    rtf.write_bytes(b"{\\rtf1\\ansi hello}")
    for path in (real, legacy, rtf):
        assert workbench.office_container_problem(path) is None, path


def test_plain_text_formats_are_not_touched(tmp_path):
    for name in ("notes.txt", "table.csv", "old.doc", "old.xls", "old.ppt", "x.wps"):
        path = tmp_path / name
        path.write_bytes(b"just some text")
        assert workbench.office_container_problem(path) is None, name


def test_an_unreadable_path_is_left_to_the_real_conversion(tmp_path):
    assert workbench.office_container_problem(tmp_path / "missing.docx") is None
