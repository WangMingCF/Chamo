"""Running out of space must not strand the job for good.

The disk guard pauses every worker when free space drops below 2 GB and tells the
user to continue once they free some up. Nothing ever cleared that pause: the
engine understands a "resume" control command, but the browser UI only ever sends
"cancel", so a GUI job stayed paused until the user gave up. The CLI has no control
channel at all. The guard now lifts its own pause; a pause the user asked for is
still theirs to end.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import workbench

LOW = workbench.LOW_DISK_BYTES


def test_low_space_pauses():
    assert workbench.low_disk_action(LOW - 1, False) == "pause"
    assert workbench.low_disk_action(0, False) == "pause"


def test_space_that_comes_back_resumes_only_the_guards_own_pause():
    assert workbench.low_disk_action(LOW, True) == "resume"
    assert workbench.low_disk_action(LOW * 10, True) == "resume"


def test_a_pause_the_user_asked_for_is_not_lifted():
    assert workbench.low_disk_action(LOW, False) == "none"


def test_still_low_stays_paused_rather_than_resuming():
    assert workbench.low_disk_action(LOW - 1, True) == "pause"


def test_the_guard_actually_uses_both_branches():
    # A helper nobody calls would pass the tests above and strand the job anyway.
    source = Path(workbench.__file__).read_text(encoding="utf-8")
    guard = source.split("def guard(")[1].split("def execute(")[0]
    assert "low_disk_action" in guard, "the guard must use the shared decision"
    assert "action=='resume'" in guard.replace(" ", ""), "the guard must resume"
    assert "self.low=False" in guard.replace(" ", ""), "resuming must clear the flag"
