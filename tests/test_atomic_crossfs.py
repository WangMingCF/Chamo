"""Publication diagnostics and cross-filesystem behaviour.

These cover the two things a naive implementation gets wrong: reporting which
primitive actually ran, and moving between filesystems where a rename cannot
work but an exclusive copy can.
"""
import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_atomic as atomic


def test_last_strategy_records_what_ran(tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    atomic.publish_exclusive(source, tmp_path / "b.bin")
    assert atomic.last_strategy() is not None
    assert atomic.last_strategy() in [name for name, _ in atomic._candidates()]


def test_strategy_reports_a_known_primitive():
    assert atomic.strategy() in ("renamex_np", "renameat2", "MoveFileExW", "link", "copy")


def test_exdev_falls_back_to_copy(tmp_path, monkeypatch):
    """Different filesystems: rename cannot work, an exclusive copy can."""
    source = tmp_path / "staged.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target.bin"

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(atomic, "_candidates",
                        lambda: [("rename", cross_device), ("copy", atomic._copy)])
    atomic.publish_exclusive(source, target)
    assert target.read_bytes() == b"payload"
    assert atomic.last_strategy() == "copy"


def test_windows_cross_volume_is_not_mistaken_for_exists():
    """ERROR_NOT_SAME_DEVICE is 17, the same number as errno.EEXIST.

    Building OSError(17) would produce a FileExistsError and a cross-volume move
    would be reported as "file exists" instead of falling back to a copy. The
    Windows branch therefore raises ctypes.WinError, which sets winerror and
    leaves errno unset.
    """
    assert atomic._WIN_NOT_SAME_DEVICE == 17
    assert atomic._WIN_NOT_SAME_DEVICE == errno.EEXIST, "the collision this guards against"
    collision = OSError(errno.EEXIST, "x")
    assert isinstance(collision, FileExistsError), (
        "precondition: a bare errno 17 already looks like FileExistsError")
    # The classifier must treat a real cross-device winerror as "try another way".
    fake = OSError(errno.EEXIST, "x")
    fake.winerror = atomic._WIN_NOT_SAME_DEVICE
    assert atomic._unsupported(fake) is True


def test_real_cross_filesystem_publish(tmp_path):
    """End-to-end on a real second filesystem when one is available."""
    if sys.platform == "win32":
        pytest.skip("no portable second filesystem")
    # /dev/shm is tmpfs on Linux; macOS has no equivalent we can rely on.
    other = Path("/dev/shm")
    if not other.is_dir() or os.stat(other).st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("no second filesystem available")
    import tempfile
    staging = Path(tempfile.mkdtemp(dir=other))
    try:
        source = staging / "staged.bin"
        source.write_bytes(b"cross-fs payload")
        target = tmp_path / "target.bin"
        atomic.publish_exclusive(source, target)
        assert target.read_bytes() == b"cross-fs payload"
        assert not source.exists()
    finally:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
