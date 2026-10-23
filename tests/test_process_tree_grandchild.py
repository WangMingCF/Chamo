"""Grandchild process-tree gate: pause/resume/terminate must reach a GRANDCHILD.

Why this file exists
--------------------
tests/test_process_control.py puts the counter in the DIRECT child of the process
it controls. A "tree" implementation that only kills the direct child (a plain
proc.kill()/proc.terminate()) passes those tests too, so the old name
"test_terminate_kills_the_tree" never verified tree semantics. The grandchild
counter in this file is the process class that actually leaked: LibreOffice
children and the page-shard renderers a pdf_images worker spawns. A
mutation experiment on macOS showed
the two implementations are distinguishable only with a grandchild writer:
  terminate_tree -> counter 1 -> 1 -> 1     (grandchild killed)
  kill() only    -> counter 1 -> 18 -> 69   (grandchild survives, keeps writing)

Assertions (each one is capable of failing):
  * pause: the GRANDCHILD counter must stop advancing for a full window;
  * resume: it must advance again after resume;
  * terminate_tree: the worker must exit, the counter must stay frozen, AND the
    grandchild PID must be gone from the process table. The PID check is what
    turns "not merely paused" into a real assertion: SIGSTOP also freezes the
    counter, but a stopped process is still alive;
  * kill_tree: same permanent-stop assertion.
  * Windows only: spawning/exiting 30 workers must not accumulate Job Object
    handles (the long-batch leak the Job Object fix addresses).

Platform status
---------------
  * POSIX: measured green locally on macOS (process-group semantics).
  * Windows: expected green with the kill-on-close Job Object implementation in
    engine/platform_process.py, but as of this writing it is NOT yet measured.
    The first real evidence is the windows-latest CI job. A failure there is a
    real finding, not a flake; no platform skip hides it.

Mutation hook
-------------
Set CHAMO_PLATFORM_PROCESS_DIR to a directory holding a COPY of
platform_process.py to run this gate against a mutated implementation
(scripts/process_tree_mutation_check.py does that). Unset in every normal run,
so the gate always tests the repository's engine/.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
MODULE_DIR = Path(os.environ.get("CHAMO_PLATFORM_PROCESS_DIR") or ENGINE).resolve()
sys.path.insert(0, str(MODULE_DIR))
import platform_process as pp  # noqa: E402

# The grandchild writes an append-only counter AND its own PID. The PID is what
# makes "terminated, not merely paused" observable.
#
# The counter used to be temp-file + os.replace, described as "atomic". It was not,
# from the reader's side: a read racing the truncate saw an empty file (0), and on
# Windows a read landing inside os.replace found the target missing. That is the
# same file-protocol defect tests/test_process_control.py had (fixed in e12c9d5 by
# appending instead); this file kept the old protocol as known debt. Appending
# removes the window rather than retrying around it.
GRANDCHILD = """
import os, sys, time
counter_path = sys.argv[1]
try:
    with open(counter_path + ".pid", "w") as handle:
        handle.write(str(os.getpid()))
except OSError:
    pass
with open(counter_path, "a", buffering=1) as handle:
    value = 0
    while True:
        value += 1
        handle.write(str(value) + chr(10))
        time.sleep(0.02)
"""

# The worker only starts the grandchild and stays alive: the only thing under
# test is whether process-tree control reaches one level further down.
WORKER = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-u", "-c", sys.argv[2], sys.argv[1]])
while True:
    time.sleep(0.05)
"""


def read(path: Path) -> int:
    """The last finished value, or 0 before the grandchild has written one.

    The counter appends one number per line and never truncates or renames, so the
    last complete line is always readable - before, during and after a pause. A
    trailing fragment (a write caught mid-line) is not a finished value and is
    skipped rather than parsed.
    """
    try:
        text = path.read_text()
    except OSError:
        return 0
    lines = [line for line in text.split(chr(10))[:-1] if line.strip()]
    try:
        return int(lines[-1])
    except (IndexError, ValueError):
        return 0


def settled(path: Path, timeout: float = 15.0, minimum: int = 3) -> int:
    """Wait until the counter has really advanced, not merely reported once.

    Returning the first non-zero value made the caller race the grandchild's
    first write (read 1, assert >= 3); the gate has to observe an advancing
    counter before it may conclude anything about pause/terminate.
    """
    deadline = time.time() + timeout
    value = read(path)
    while value < minimum and time.time() < deadline:
        time.sleep(0.02)
        value = read(path)
    return value


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def advances(path: Path, window: float = 1.2) -> bool:
    before = read(path)
    time.sleep(window)
    return read(path) > before


def grandchild_pid(counter: Path, timeout: float = 10.0) -> int:
    pid_file = Path(str(counter) + ".pid")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return int(pid_file.read_text().strip())
        except (OSError, ValueError):
            time.sleep(0.02)
    pytest.fail("grandchild never wrote its PID; the fixture is broken, not the engine")


def pid_alive(pid: int) -> bool:
    """True when the OS still has this PID as a live (possibly stopped) process."""
    if pid <= 0:
        return False
    if pp.POSIX:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_OBJECT_0 = 0x00000000
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False  # cannot open it: it is gone
    try:
        # Signalled means the process has exited; a suspended process is NOT signalled.
        return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def start_tree(tmp_path: Path):
    counter = tmp_path / "grandchild-counter"
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    proc = pp.spawn_group([sys.executable, "-u", str(worker), str(counter), GRANDCHILD])
    return proc, counter


def stop(proc) -> None:
    """Cleanup that must never mask an assertion failure."""
    try:
        pp.kill_tree(proc)
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def assert_dead_after_tree_control(proc, counter: Path, action, action_name: str) -> None:
    """Shared assertion: the worker exits and the grandchild is permanently gone."""
    pid = grandchild_pid(counter)
    action()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("%s did not make the worker exit (it is still running)" % action_name)
    if not wait_until(lambda: not pid_alive(pid), timeout=5.0, interval=0.05):
        pytest.fail(
            "grandchild pid %d is STILL ALIVE after %s. A bare kill/terminate of the "
            "direct child passes the old direct-child test but leaks this process; "
            "the grandchild counter would keep writing (lead probe: 1 -> 18 -> 69)."
            % (pid, action_name))
    frozen = read(counter)
    time.sleep(1.5)
    assert read(counter) == frozen, (
        "grandchild counter advanced after %s even though its PID is gone "
        "(%d -> %d); a leftover process is still writing" % (action_name, frozen, read(counter)))


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------
def test_pause_and_resume_reach_the_grandchild(tmp_path):
    """pause_tree must stop the grandchild, resume_tree must restart it."""
    proc, counter = start_tree(tmp_path)
    try:
        started = settled(counter)
        assert started >= 3, "grandchild never started counting (counter=%s)" % started
        pp.pause_tree(proc)
        time.sleep(0.4)  # let an in-flight write land
        frozen = read(counter)
        assert frozen >= started - 1, (
            "counter went backwards while pausing (%s -> %s)" % (started, frozen))
        time.sleep(1.2)
        after = read(counter)
        assert after == frozen, (
            "GRANDCHILD kept running while the tree was paused (%s -> %s): pause_tree "
            "did not reach it (only the direct child was stopped)" % (frozen, after))
        pp.resume_tree(proc)
        assert wait_until(lambda: read(counter) > frozen, timeout=10.0), (
            "grandchild did not resume after resume_tree (counter stayed at %s)" % frozen)
    finally:
        stop(proc)


def test_terminate_tree_kills_the_grandchild_not_merely_pauses_it(tmp_path):
    """terminate_tree must leave the grandchild dead, not merely stopped."""
    proc, counter = start_tree(tmp_path)
    try:
        assert settled(counter) >= 3, "grandchild never started counting"
        assert_dead_after_tree_control(proc, counter, lambda: pp.terminate_tree(proc),
                                       "terminate_tree")
    finally:
        stop(proc)


def test_kill_tree_kills_the_grandchild_permanently(tmp_path):
    """kill_tree must leave the grandchild dead as well."""
    proc, counter = start_tree(tmp_path)
    try:
        assert settled(counter) >= 3, "grandchild never started counting"
        assert_dead_after_tree_control(proc, counter, lambda: pp.kill_tree(proc),
                                       "kill_tree")
    finally:
        stop(proc)


@pytest.mark.skipif(pp.POSIX,
                    reason="Job Objects exist only on Windows; POSIX uses process "
                           "groups. This skip is printed, not silent - the grandchild "
                           "assertions above still run on POSIX.")
def test_windows_child_is_actually_bound_to_a_job():
    """Assert the binding exists, do not infer it from behaviour.

    This module has now failed twice in the same silent way: first assign_to_job()
    was never called at all, then it passed an 8-byte structure to
    SetInformationJobObject, which requires the full 144-byte
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION and rejects a shorter buffer. Both times
    it returned "no job", both times terminate/pause quietly fell back to a single
    PID. A behavioural test can be satisfied by luck; this one cannot.
    """
    proc = pp.spawn_group([sys.executable, "-c", "pass"])
    try:
        handle = getattr(proc, "_chamo_job", None)
        assert handle is not None, (
            "spawn_group did not bind a Job Object, so cancelling this child would "
            "not reach its grandchildren")
    finally:
        pp.terminate_tree(proc)
        proc.wait(timeout=15)


@pytest.mark.skipif(pp.POSIX,
                    reason="Job Object handles exist only on Windows; POSIX uses "
                           "process groups. This skip is printed, not silent - the "
                           "grandchild assertions above still run on POSIX.")
def test_windows_job_handles_do_not_accumulate(tmp_path):
    """Long batches must not leak one Job Object handle per worker."""
    import ctypes
    import gc
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    process = kernel32.GetCurrentProcess()

    def handle_count() -> int:
        count = wintypes.DWORD()
        kernel32.GetProcessHandleCount(process, ctypes.byref(count))
        return count.value

    for _ in range(3):  # warm up one-time allocations
        warm = pp.spawn_group([sys.executable, "-c", "pass"])
        warm.wait(timeout=10)
    gc.collect()
    before = handle_count()
    spawns = 30
    for _ in range(spawns):
        child = pp.spawn_group([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        del child
    gc.collect()
    after = handle_count()
    assert after - before <= 10, (
        "Job Object handles accumulated: %d -> %d over %d workers (a long batch "
        "would exhaust the process handle table)" % (before, after, spawns))

def test_the_counter_reads_the_last_finished_value(tmp_path):
    """A read must never mistake a half-written line for a value.

    The counter is append-only. A reader can therefore catch a write mid-line; the
    fragment is not a finished value and must be ignored, and an empty or missing
    file reads 0. Under the old temp-file + os.replace protocol the equivalent
    moment produced 0 *after* real values had been written, which looked like the
    counter resetting and made the pause assertion flaky for a reason unrelated to
    process control.
    """
    counter = tmp_path / "counter"
    cases = [
        ("", 0),
        ("1\n", 1),
        ("1\n2\n3\n", 3),
        ("1\n2\n3\n4", 3),        # a write caught mid-line: 4 is not finished
        ("45", 0),                  # nothing finished yet
        ("1\n45", 1),
    ]
    for content, expected in cases:
        counter.write_text(content, encoding="utf-8")
        assert read(counter) == expected, (content, read(counter), expected)
    counter.unlink()
    assert read(counter) == 0, "a missing counter must read 0, not raise"


def test_the_counter_keeps_its_value_when_the_writer_is_killed(tmp_path):
    """Killing the writer must not lose the last value (the old rename could)."""
    counter = tmp_path / "counter"
    child = subprocess.Popen([sys.executable, "-u", "-c", GRANDCHILD, str(counter)])
    try:
        value = settled(counter, timeout=15, minimum=3)
        assert value >= 3, "the counter never advanced: %r" % value
        child.kill()
        child.wait(timeout=10)
        after = read(counter)
        assert after >= value, (
            "the counter lost its last value after the writer was killed: %d -> %d"
            % (value, after))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
