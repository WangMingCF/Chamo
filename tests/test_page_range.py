"""The page-range hint must be the message the user gets.

Pasting "¹" looked like a digit to str.isdigit(), but int() refuses it, so the
parser leaked "invalid literal for int() with base 10: '¹'" instead of the
documented "页码格式应为 1-5,8,12-20". A reversed range also reported "超出范围",
which sends the user looking at the page count rather than at their own typing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench

FORMAT = "页码格式应为 1-5,8,12-20"


def test_numeric_characters_that_are_not_decimal_digits_get_the_format_hint():
    # isdigit() accepts these; int() does not. The hint must win, not a crash.
    for spec in ("\u00b9", "\u00b2\u00b3", "1-\u00b9"):
        with pytest.raises(ValueError) as caught:
            workbench.pages(spec, 10)
        assert str(caught.value) == FORMAT, (spec, str(caught.value))


def test_a_reversed_range_says_so_and_suggests_the_swap():
    with pytest.raises(ValueError) as caught:
        workbench.pages("5-1", 10)
    message = str(caught.value)
    assert "起止颠倒" in message and "1-5" in message, message


def test_valid_specs_still_parse():
    assert workbench.pages("", 3) == [0, 1, 2]
    assert workbench.pages("1-3,5", 6) == [0, 1, 2, 4]
    assert workbench.pages(" 1 , 2 ", 3) == [0, 1]
    assert workbench.pages("2-2", 3) == [1]


def test_out_of_range_and_duplicates_keep_their_own_messages():
    with pytest.raises(ValueError, match="超出范围"):
        workbench.pages("99", 10)
    with pytest.raises(ValueError, match="超出范围"):
        workbench.pages("0", 10)
    with pytest.raises(ValueError, match="重复"):
        workbench.pages("1-3,2-4", 10)
    with pytest.raises(ValueError) as caught:
        workbench.pages("1-", 10)
    assert str(caught.value) == FORMAT
