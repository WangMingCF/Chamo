"""Direct-child process control: pause must stop work, cancel must reach the child.

SCOPE (learned from a mutation experiment): the counter in this file runs in the
DIRECT child of the process under control, so an implementation that only kills
the direct child (a bare proc.kill()/proc.terminate()) passes every test here.
These tests therefore do NOT prove tree semantics; their old name
"test_terminate_kills_the_tree" claimed more than it verified. Tree semantics are
verified by tests/test_process_tree_grandchild.py, which puts the counter in a
grandchild (the process class that actually leaked: LibreOffice children and the
page shard renderers a worker spawns).

They assert observable behaviour (a counter stops advancing) rather than which
signal was used, so they run unchanged on POSIX and Windows.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import platform_process as pp

# The counter appends one number per line and never truncates or renames.
# Earlier versions used temp-file + os.replace, described here as "atomic". It
# was not, from the reader's side: a read racing the truncate saw an empty file
# (0), and on Windows a pause landing inside os.replace left the target missing
# until resume. Both showed up as "counter unreadable or reset before pause" and
# were artefacts of the file protocol, not of process control. Appending removes
# the window instead of papering over it with a retry: the last complete line is
# always readable, before, during and after a pause.
COUNTER = """
import sys, time
path = sys.argv[1]
value = 0
while True:
    value += 1
    with open(path, "a") as handle:
        handle.write(str(value) + chr(10))
    time.sleep(0.02)
"""


def read(path: Path) -> int:
    """Last value reported by the worker, or 0 before it has reported once.

    The worker appends one number per line and never truncates or renames the
    file. A truncate+rename scheme looked atomic but was not, for two reasons:
    a reader racing the truncate saw an empty file (value 0), and pausing the
    process inside os.replace left the target missing until resume - the pause
    test then read 0 and failed with "counter unreadable or reset before pause",
    which is a property of the file protocol, not of process control.
    """
    try:
        numbers = path.read_text().split()
        return int(numbers[-1]) if numbers else 0
    except (OSError, ValueError):
        return 0


def test_pause_stops_progress_and_resume_continues(tmp_path):
    script = tmp_path / "counter.py"
    script.write_text(COUNTER, encoding="utf-8")
    counter = tmp_path / "count.txt"
    proc = pp.spawn_group([sys.executable, "-u", str(script), str(counter)])
    try:
        deadline = time.time() + 10
        while read(counter) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert read(counter) >= 3, "worker never started counting"

        pp.pause_tree(proc)
        time.sleep(0.15)          # let an in-flight write land
        frozen = read(counter)
        assert frozen >= 3, f"counter unreadable or reset before pause ({frozen})"
        time.sleep(0.5)
        after = read(counter)
        assert after == frozen, f"process kept running while paused ({frozen} -> {after})"

        pp.resume_tree(proc)
        deadline = time.time() + 10
        while read(counter) <= frozen and time.time() < deadline:
            time.sleep(0.05)
        assert read(counter) > frozen, "process did not resume"
    finally:
        pp.kill_tree(proc)
        proc.wait(timeout=10)


def test_terminate_stops_the_direct_child(tmp_path):
    """terminate_tree stops the direct child. Tree semantics are NOT covered here.

    A bare proc.kill() also passes this test; the grandchild gate is
    tests/test_process_tree_grandchild.py. Kept because stopping even the direct
    child is still a necessary (just not sufficient) property.
    """
    script = tmp_path / "counter.py"
    script.write_text(COUNTER, encoding="utf-8")
    counter = tmp_path / "count.txt"
    proc = pp.spawn_group([sys.executable, "-u", str(script), str(counter)])
    deadline = time.time() + 10
    while read(counter) < 1 and time.time() < deadline:
        time.sleep(0.05)
    pp.terminate_tree(proc)
    if pp.POSIX:
        proc.wait(timeout=10)
        assert proc.poll() is not None
    else:
        time.sleep(0.5)
    frozen = read(counter)
    time.sleep(0.4)
    assert read(counter) == frozen, "process still running after terminate"


def test_spawn_group_child_cannot_read_the_parent_stdin(tmp_path):
    """A controlled child must not inherit the parent's stdin, only a null device.

    On Windows a synchronous pipe serializes operations: while the parent has a
    ReadFile() outstanding on its stdin, a child that inherited that pipe blocks
    inside CPython's startup lseek() on stdin (gh-78961). The supervisor keeps its
    control thread reading stdin, so an inheriting worker froze before running any
    of its code — the UI conversion that never left "running". This test does not
    need Windows: the parent's stdin carries a marker, and a child that inherited
    it would read the marker instead of EOF.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import subprocess, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import platform_process\n"
        "child = platform_process.spawn_group(\n"
        "    [sys.executable, '-c', 'import sys; sys.stdout.write(repr(sys.stdin.read()))'],\n"
        "    stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
        "out, err = child.communicate(timeout=60)\n"
        "sys.stdout.write(out.decode('utf-8', 'replace'))\n",
        encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(probe), str(ENGINE)],
        input="SECRET-MARKER\n", capture_output=True, text=True, encoding="utf-8",
        timeout=120)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "''", (
        "the child read the parent's stdin; on Windows that deadlocks the worker "
        "inside interpreter startup (CPython gh-78961)")


def test_control_helpers_are_safe_on_dead_process(tmp_path):
    proc = pp.spawn_group([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    # Repeated control calls after exit must not raise: the supervisor races
    # with worker exit all the time.
    pp.pause_tree(proc)
    pp.resume_tree(proc)
    pp.terminate_tree(proc)
    pp.kill_tree(proc)


def test_reports_a_strategy():
    assert pp.describe() in ("posix-signals", "windows-job")
