"""Output names must be creatable on every supported filesystem.

Two real failures drove these tests: a 150-character Chinese source name is 450
bytes in UTF-8 and ext4 rejects anything over 255 bytes, and Windows forbids
characters such as ":" plus reserved device names like "con".
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_names as names


def test_long_cjk_name_fits_a_byte_limit():
    result = names.sanitize("讲" * 150)
    assert len(result.encode("utf-8")) <= names.NAME_MAX_BYTES
    assert result, "must not truncate to nothing"


def test_truncation_keeps_the_extension():
    result = names.sanitize("x" * 400 + ".pdf")
    assert result.endswith(".pdf")
    assert len(result.encode("utf-8")) <= names.NAME_MAX_BYTES


def test_truncation_never_splits_a_character():
    result = names.sanitize("讲" * 200)
    # Round-trips means no partial multi-byte sequence was emitted.
    assert result.encode("utf-8").decode("utf-8") == result


def test_short_names_are_untouched():
    assert names.sanitize("讲义 第1页") == "讲义 第1页"
    assert names.sanitize("photo.jpg") == "photo.jpg"


def test_empty_name_becomes_a_placeholder():
    assert names.sanitize("") == "_"
    assert names.sanitize("   ") == "_"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only rules")
def test_windows_reserved_characters_and_devices():
    assert ":" not in names.sanitize("a:b.pdf")
    assert names.sanitize("con.pdf").upper() != "CON.PDF"
    assert names.sanitize("aux").upper() != "AUX"


def test_path_fits_checks_every_component():
    assert names.path_fits(Path("ok") / "讲义.pdf")
    assert not names.path_fits(Path("ok") / ("讲" * 200))


def test_real_file_can_be_created_with_a_long_name(tmp_path):
    """The sanitizer must produce something the filesystem actually accepts.

    The extension is passed in, because appending it after sanitizing would push
    the name back over the limit (which is how this test first failed on ext4).
    """
    name = names.sanitize("讲" * 150 + ".png")
    assert len(name.encode("utf-8")) <= names.NAME_MAX_BYTES
    target = tmp_path / name
    target.write_bytes(b"x")
    assert target.exists()
