"""Exclusive file publication, without overwriting an existing file.

Every converted artifact is written to a staging directory on the output volume
and then moved into place. The move must be atomic (a reader never sees a
half-written file) and must fail rather than replace a file that already exists,
because the product promise is that originals and previous results are never
silently overwritten.

Three platform primitives give that guarantee; the first available one wins:

  renamex_np(RENAME_EXCL)  macOS. Atomic and exclusive, and it also works on
                           volumes without hard-link support (exFAT, SMB).
  renameat2(RENAME_NOREPLACE)  Linux. Same semantics as macOS.
  MoveFileExW(MOVEFILE_WRITE_THROUGH) without MOVEFILE_REPLACE_EXISTING
                           Windows. Fails if the destination exists.

A hard-link-plus-unlink fallback covers anything else, and the last resort is a
checked copy. Both are weaker (a crash can leave a partial file, and the check
is not atomic), so they are only used when no atomic primitive exists, and the
caller is told which strategy ran through `strategy()`.
"""
from __future__ import annotations

import errno
import os
import shutil
import sys
from pathlib import Path


def _macos(source: Path, target: Path) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # RENAME_EXCL = 4: fail with EEXIST instead of replacing the destination.
    if rename(os.fsencode(source), os.fsencode(target), 4):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


def _linux(source: Path, target: Path) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    AT_FDCWD = -100
    RENAME_NOREPLACE = 1
    if renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(target),
                 RENAME_NOREPLACE):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


# Windows reports "already exists" through GetLastError, not errno. Python only
# maps errno values to FileExistsError, so without translation OSError(183)
# stays a plain OSError and the caller's never-overwrite branch never fires.
_WIN_ALREADY_EXISTS = 183   # ERROR_ALREADY_EXISTS
_WIN_FILE_EXISTS = 80       # ERROR_FILE_EXISTS


def _windows(source: Path, target: Path) -> None:
    import ctypes
    from ctypes import wintypes

    MOVEFILE_WRITE_THROUGH = 0x8
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    # Omitting MOVEFILE_REPLACE_EXISTING makes the call fail if the target exists.
    if not kernel32.MoveFileExW(str(source), str(target), MOVEFILE_WRITE_THROUGH):
        # ctypes.WinError sets winerror and leaves errno alone. Building the
        # exception from the raw code instead would be wrong: Windows
        # ERROR_NOT_SAME_DEVICE is 17, which equals errno.EEXIST, so OSError(17)
        # is already a FileExistsError and a cross-volume move would be
        # misreported as "file exists" and never retried as a copy.
        error = ctypes.WinError(ctypes.get_last_error())
        if getattr(error, "winerror", None) in (_WIN_ALREADY_EXISTS, _WIN_FILE_EXISTS):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        raise error


def _link(source: Path, target: Path) -> None:
    # Non-atomic but exclusive: os.link fails if the target exists. A crash
    # between link and unlink leaves both names pointing at the same inode, which
    # is harmless (same bytes) but means the staged file lingers.
    os.link(source, target)
    try:
        os.unlink(source)
    except OSError:
        pass


def _copy(source: Path, target: Path) -> None:
    # Last resort: exclusive create, then stream the bytes. A failure part-way
    # must not leave a truncated file behind: the resume logic hashes outputs and
    # would either adopt a corrupt file or report the run as complete with a
    # short result. Clean up before propagating.
    created = False
    try:
        with open(source, "rb") as src, open(target, "xb") as dst:
            created = True
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
    except BaseException:
        if created:
            try:
                target.unlink()
            except OSError:
                pass
        raise
    os.unlink(source)


def _candidates():
    if sys.platform == "darwin":
        return [("renamex_np", _macos), ("link", _link), ("copy", _copy)]
    if sys.platform.startswith("linux"):
        return [("renameat2", _linux), ("link", _link), ("copy", _copy)]
    if sys.platform == "win32":
        return [("MoveFileExW", _windows), ("copy", _copy)]
    return [("link", _link), ("copy", _copy)]


# Cross-device means the primitive cannot act on this pair of paths, so a
# strategy that copies is the right answer. It is not "target exists".
_WIN_NOT_SAME_DEVICE = 17   # ERROR_NOT_SAME_DEVICE


def _unsupported(error: BaseException) -> bool:
    """True when this primitive cannot be used for these paths, not "target exists"."""
    if isinstance(error, AttributeError):
        return True                       # symbol not in libc
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) == _WIN_NOT_SAME_DEVICE:
            return True
        # ENOSYS/ENOTSUP/EINVAL mean the kernel or filesystem cannot do it;
        # EXDEV means staging and output are on different filesystems, which the
        # copy fallback handles; EEXIST means it worked and refused to overwrite.
        return error.errno in (errno.ENOSYS, errno.ENOTSUP, errno.EINVAL,
                               errno.EOPNOTSUPP, errno.EXDEV)
    return False


# Set by the last successful publish so diagnostics and tests can report which
# primitive actually ran, instead of guessing from the candidate list.
_last_used: str | None = None


def last_strategy() -> str | None:
    """Name of the primitive that performed the most recent publish, if any."""
    return _last_used


def strategy() -> str:
    """Preferred primitive for this platform.

    This is the first candidate, which may still fail at run time (for example a
    filesystem without renameat2 support); use last_strategy() to learn what
    actually ran.
    """
    candidates = _candidates()
    return candidates[0][0] if candidates else "none"


def publish_exclusive(source, target) -> None:
    """Move `source` to `target`, failing with FileExistsError if `target` exists."""
    global _last_used
    source, target = Path(source), Path(target)
    last = None
    for name, move in _candidates():
        try:
            move(source, target)
            _last_used = name
            return
        except FileExistsError:
            raise
        except OSError as error:
            # Normalise "destination exists" from any primitive into the one type
            # callers catch; Windows reports it as winerror rather than errno.
            if getattr(error, "winerror", None) in (_WIN_ALREADY_EXISTS, _WIN_FILE_EXISTS):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
            if not _unsupported(error):
                raise
            last = error
        except AttributeError as error:
            last = error
    raise OSError(errno.ENOTSUP, f"no exclusive publish primitive available: {last}")


if __name__ == "__main__":
    print(strategy())
