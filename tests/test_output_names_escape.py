"""Output names must stay inside the folder the user chose.

A real file may be called `...pdf`; `Path("...pdf").stem` is "..". The multi-page
image naming joins the stem as a folder, so the result was `../.._001.png` and the
converted pages were written next to the output folder instead of inside it. The
source file was never touched, but the user did not choose that destination.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench

SOURCES = ["...pdf", "..pdf", "normal.pdf", "讲义.pdf"]
OPERATIONS = ["pdf_images", "office_images", "pdf_compress", "pdf_merge"]


def relative_for(source_name: str, operation: str) -> Path:
    source = Path("/inputs") / source_name
    files = [Path("/work/page_page1.png")]
    return workbench.clean_names(files, [source], {"operation": operation})[0]


def test_every_operation_keeps_its_output_inside_the_chosen_folder(tmp_path):
    for source_name in SOURCES:
        for operation in OPERATIONS:
            relative = relative_for(source_name, operation)
            target = (tmp_path / relative).resolve()
            # is_relative_to, not a string prefix: Windows joins with a backslash,
            # so a "/"-suffixed prefix check silently fails there.
            assert target.is_relative_to(tmp_path.resolve()), (
                f"{source_name!r} with {operation} escaped to {relative}")
            assert ".." not in relative.parts, relative


def test_a_dot_stem_is_not_used_as_a_folder():
    relative = relative_for("...pdf", "pdf_images")
    assert relative.parts[0] not in (".", ".."), relative
    assert relative.parts[0].strip(), relative
