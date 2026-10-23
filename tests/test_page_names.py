"""Page file names must be creatable for every legal source name.

A PDF called with 81 Chinese characters is 243 bytes and the filesystem accepts it,
but the render names each page `<stem>_page00001.jpeg`, which reached 258 bytes and
failed with ENAMETOOLONG: the export died on a file the user had every right to have.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_names
import workbench

LONG = "讲" * 81          # 243 UTF-8 bytes, legal as a single component


def test_expected_page_files_are_creatable_for_a_long_source_name(tmp_path):
    for fmt in ("jpeg", "png"):
        for name in workbench._expected_page_files(LONG, [0, 1], {"format": fmt}):
            assert len(name.encode("utf-8")) <= platform_names.NAME_MAX_BYTES, name
            (tmp_path / name).write_bytes(b"x")     # the filesystem must accept it


def test_the_render_name_and_the_verifier_agree(tmp_path):
    # render() builds the same name; if the two ever disagree the export reports a
    # missing page for every page it just wrote.
    expected = workbench._expected_page_files(LONG, [0], {"format": "jpeg"})[0]
    produced = f"{platform_names.page_stem(LONG)}_page{0 + 1:05d}.jpeg"
    assert produced == expected
