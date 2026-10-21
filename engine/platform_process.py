"""Suspend, resume and terminate a worker process tree on every platform.

Pause must actually stop work (not just stop scheduling), and cancel must reach
grandchildren so a LibreOffice child is not left running. POSIX gives that with
process groups and SIGSTOP/SIGCONT; Windows needs a Job Object plus NtSuspendProcess.

Keeping this in one module means the supervisor calls intent-level functions
(`pause_tree`, `resume_tree`, `terminate_tree`) instead of POSIX signals.

Windows history: assign_to_job() existed from the start but nothing ever called it,
so terminate_tree/kill_tree only ran TerminateProcess on the worker and pause_tree
only suspended that one PID. Grandchildren — LibreOffice, and now the page shard
renderers — survived cancel and kept writing. This file now binds every child to a
kill-on-close Job Object, and enumerates the job for suspend/resume.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import weakref

POSIX = sys.platform != "win32"

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_PROGRESS_SUSPEND_RESUME = 0x0800

_kernel32 = None


def _k32():
    """kernel32 with real 64-bit signatures.

    ctypes assumes an undeclared return value is c_int, which silently truncates a
    64-bit HANDLE. Handles are usually small so the bug hides until it does not.
    """
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    import ctypes
    from ctypes import wintypes

    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateJobObjectW.restype = wintypes.HANDLE
    lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    lib.CloseHandle.restype = wintypes.BOOL
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    lib.AssignProcessToJobObject.restype = wintypes.BOOL
    lib.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    lib.SetInformationJobObject.restype = wintypes.BOOL
    lib.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    lib.QueryInformationJobObject.restype = wintypes.BOOL
    lib.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    lib.OpenProcess.restype = wintypes.HANDLE
    lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32 = lib
    return lib


def spawn_group(argv: list[str], **kwargs) -> subprocess.Popen:
    """Start a child that can be controlled together with its descendants.

    POSIX: a new session makes the child its own process group, so killpg reaches
    every descendant. Children it spawns must use plain Popen to stay in the group;
    spawn_group on a grandchild would put it out of reach.

    Windows: a job object with kill-on-close. Descendants inherit job membership,
    so closing the handle removes grandchildren even though they were never named.

    stdin is detached to the null device unless the caller passes one. A controlled
    child has no business reading the parent's control channel, and on Windows
    inheriting it can freeze the child before its first line of user code runs: for
    synchronous pipes, Windows serializes every operation on a pipe while a
    ReadFile() is outstanding, and CPython calls lseek() on stdin while building
    sys.stdin in its startup (CPython gh-78961 / bpo-34780). The supervisor keeps a
    control thread blocked reading stdin, so a worker that inherited that same pipe
    deadlocked inside interpreter startup — a UI conversion stuck at "running"
    forever with an empty worker log (three-platform CI, 2026-10-20). POSIX has no
    such serialization, but detaching is the same no-op there and keeps one
    behaviour on every platform.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if POSIX:
        kwargs.setdefault("start_new_session", True)
        return subprocess.Popen(argv, **kwargs)
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    proc = subprocess.Popen(argv, creationflags=CREATE_NEW_PROCESS_GROUP, **kwargs)
    job = assign_to_job(proc)
    if job:
        proc._chamo_job = job  # noqa: SLF001
        # A long batch spawns many workers. Reclaim the handle when the Popen is
        # collected; closing it also removes any descendant still alive.
        #
        # The finalizer is kept so the explicit close in _windows_terminate can
        # detach it first. Closing the same HANDLE twice is not harmless: Windows
        # reuses handle values, so the second CloseHandle can hit an unrelated
        # object that has since taken that value.
        try:
            proc._chamo_job_finalizer = weakref.finalize(proc, _close_job, job)  # noqa: SLF001
        except TypeError:
            proc._chamo_job_finalizer = None  # noqa: SLF001
    return proc


def _limit_structures():
    """ctypes mirror of JOBOBJECT_EXTENDED_LIMIT_INFORMATION.

    The size matters. SetInformationJobObject is told how long the buffer is, and
    the kernel rejects a length shorter than the structure it is expecting
    (STATUS_INFO_LENGTH_MISMATCH, surfaced as ERROR_BAD_LENGTH). Passing just the
    LimitFlags DWORD used to silently fail here, which meant no job was ever bound
    and kill-on-close never applied -- the same no-op the module was fixing. Let
    ctypes derive the size per architecture instead of hard-coding 144.
    """
    import ctypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return ctypes, _ExtendedLimitInformation


def assign_to_job(proc: subprocess.Popen) -> object | None:
    """Windows: bind the child to a kill-on-close job so cancel kills grandchildren."""
    if POSIX:
        return None
    import ctypes
    from ctypes import wintypes

    k32 = _k32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    ctypes_mod, extended = _limit_structures()
    info = extended()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes_mod.byref(info), ctypes_mod.sizeof(info))
    if not ok:
        _close_job(job)
        raise OSError(
            "SetInformationJobObject failed: kill-on-close cannot be enabled, so a "
            "cancelled run would leave grandchildren writing. "
            f"WinError={ctypes_mod.get_last_error()}")
    if not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):  # noqa: SLF001
        error = ctypes_mod.get_last_error()
        _close_job(job)
        raise OSError(
            "AssignProcessToJobObject failed, so this child cannot be cancelled as a "
            f"tree. WinError={error}")
    return job


_JOB_LOCK = threading.Lock()


def _close_job(job) -> None:
    """Close a job handle. With kill-on-close this terminates the remaining members."""
    try:
        _k32().CloseHandle(job)
    except Exception:
        pass


def close_job_once(proc: subprocess.Popen) -> None:
    """Close this child's job handle exactly once, from whichever thread asks first.

    Two closers can exist: an explicit terminate and the weakref finalizer that
    fires when the Popen is collected, and Supervisor.control can call terminate
    while a worker thread calls kill. Closing the same HANDLE twice is not benign --
    Windows recycles handle values, so the second CloseHandle can close an
    unrelated object that has since taken that value. Ownership is decided by
    finalize.detach(): only the caller it hands the arguments to may close, and a
    finalizer that already fired means the handle is already gone.
    """
    with _JOB_LOCK:
        job = getattr(proc, "_chamo_job", None)
        if job is None:
            return
        proc._chamo_job = None  # noqa: SLF001
        finalizer = getattr(proc, "_chamo_job_finalizer", None)
        owns = True
        if finalizer is not None:
            try:
                owns = finalizer.detach() is not None
            except Exception:
                # Detaching failed, so the finalizer may still close later. Closing
                # here would then happen twice; leaving the handle to the finalizer
                # is late but correct.
                owns = False
        if owns:
            _close_job(job)


def _job_pids(job) -> list[int]:
    """PIDs currently in the job, grandchildren included.

    A child assigned to a job passes membership on to everything it starts, so this
    list is the whole tree rather than one named process.
    """
    import ctypes

    k32 = _k32()
    capacity = 64
    while capacity <= 65536:
        size = 8 + capacity * ctypes.sizeof(ctypes.c_size_t)
        buf = ctypes.create_string_buffer(size)
        if not k32.QueryInformationJobObject(
                job, _JOB_OBJECT_BASIC_PROCESS_ID_LIST, buf, size, None):
            return []
        listed = ctypes.c_ulong.from_buffer(buf, 4).value
        if listed < capacity:
            array = (ctypes.c_size_t * listed).from_buffer(buf, 8)
            return [int(array[i]) for i in range(listed)]
        capacity = max(capacity * 4, listed + 64)
    return []


def _signal_group(proc: subprocess.Popen, sig) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def pause_tree(proc: subprocess.Popen) -> None:
    """Stop the process tree without losing its state."""
    if POSIX:
        _signal_group(proc, signal.SIGSTOP)
        return
    _windows_suspend(proc)


def resume_tree(proc: subprocess.Popen) -> None:
    if POSIX:
        _signal_group(proc, signal.SIGCONT)
        return
    _windows_resume(proc)


def terminate_tree(proc: subprocess.Popen) -> None:
    """Stop the tree, then kill it. Safe to call on an already dead process."""
    if POSIX:
        _signal_group(proc, signal.SIGCONT)   # a stopped process ignores SIGTERM
        _signal_group(proc, signal.SIGTERM)
        return
    _windows_terminate(proc)


def kill_tree(proc: subprocess.Popen) -> None:
    if POSIX:
        _signal_group(proc, signal.SIGCONT)
        _signal_group(proc, signal.SIGKILL)
        return
    _windows_terminate(proc)


_ntdll_lib = None


def _ntdll():
    global _ntdll_lib
    if _ntdll_lib is not None:
        return _ntdll_lib
    import ctypes

    lib = ctypes.WinDLL("ntdll", use_last_error=True)
    lib.NtSuspendProcess.restype = ctypes.c_long
    lib.NtSuspendProcess.argtypes = [ctypes.c_void_p]
    lib.NtResumeProcess.restype = ctypes.c_long
    lib.NtResumeProcess.argtypes = [ctypes.c_void_p]
    _ntdll_lib = lib
    return lib


def _windows_apply(proc: subprocess.Popen, suspend: bool) -> None:
    """Suspend or resume every process in the job, not just the worker."""
    from ctypes import wintypes

    job = getattr(proc, "_chamo_job", None)
    pids = _job_pids(job) if job else []
    if not pids:
        pids = [proc.pid]
    k32 = _k32()
    lib = _ntdll()
    fn = lib.NtSuspendProcess if suspend else lib.NtResumeProcess
    for pid in pids:
        handle = k32.OpenProcess(_PROGRESS_SUSPEND_RESUME, False, pid)
        if not handle:
            continue  # the process may have exited between listing and opening
        try:
            fn(wintypes.HANDLE(handle))
        finally:
            k32.CloseHandle(handle)


def _windows_suspend(proc: subprocess.Popen) -> None:
    _windows_apply(proc, True)


def _windows_resume(proc: subprocess.Popen) -> None:
    _windows_apply(proc, False)


def _windows_terminate(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except Exception:
        pass
    # Kill-on-close removes everything still in the job: LibreOffice, and the page
    # shard renderers a worker spawns for a large image-dominant PDF.
    close_job_once(proc)


def reap_leftovers(proc: subprocess.Popen) -> None:
    """Kill anything still alive in a worker's tree after the worker itself is gone.

    A worker that dies abnormally cannot clean up the processes it started; the
    page shard renderers are the case that matters, because they keep writing into
    the staging folder. They share the worker's process group (POSIX) or its job
    object (Windows), so the tree can still be reached after the worker is dead.

    This must not depend on the worker's pid still resolving to a process: poll()
    has already reaped it. spawn_group starts a new session, so the group id equals
    the worker's pid and killpg works on the surviving members alone.
    """
    if POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    close_job_once(proc)


def on_terminate(callback) -> bool:
    """Run callback when the process is asked to stop.

    POSIX delivers SIGTERM, which can be caught. Windows has no catchable SIGTERM
    for a console process: registering the handler can fail outright, and a
    termination request arrives as a console control event. Registering the same
    handler unconditionally is what made the supervisor exit with a bare "fatal"
    before doing any work, which is how the three-platform CI first failed.

    Returns True when a handler was installed.
    """
    if POSIX:
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: callback())
            return True
        except (ValueError, OSError, RuntimeError):
            return False
    # Windows: Ctrl+Break / console close arrive as control events; Ctrl+C is
    # SIGINT. A Job Object with kill-on-close covers what signals cannot.
    try:
        signal.signal(signal.SIGINT, lambda signum, frame: callback())
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, lambda signum, frame: callback())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def describe() -> str:
    return "posix-signals" if POSIX else "windows-job"


if __name__ == "__main__":
    print(describe())
