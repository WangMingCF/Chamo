"""timeout is a number the request controls; it must be checked like the others.

dpi, quality, max_edge and workers are all range-checked in validate(). timeout was
not, and it is the one whose bad values are silent: subprocess.run(timeout=0) raises
TimeoutExpired before the child does anything, and timeout=None waits forever. A
request carrying either made every Office conversion fail with a Python traceback
(or never finish) instead of something the caller can act on.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench

MESSAGE = "timeout 必须是大于 0 的秒数"


def config(**extra):
    return {"operation": "office_pdf", "workers": 1, **extra}


def test_zero_really_does_time_out_immediately():
    # The premise of the check, measured rather than assumed.
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-c", "pass"], timeout=0)


def test_values_that_cannot_bound_a_conversion_are_refused():
    for value in (0, -5, "abc", None, True):
        with pytest.raises(ValueError) as caught:
            workbench.validate(config(timeout=value))
        assert MESSAGE in str(caught.value), (value, str(caught.value))


def test_a_real_bound_is_still_accepted():
    for value in (1.5, 600):
        cfg = config(timeout=value)
        workbench.validate(cfg)
        assert cfg["timeout"] == value


def test_an_absent_timeout_keeps_the_documented_default():
    cfg = config()
    workbench.validate(cfg)
    assert "timeout" not in cfg, "validate must not invent a timeout the caller did not send"
