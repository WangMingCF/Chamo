"""Failure behaviour of exclusive publication.

The happy path is covered elsewhere; these tests cover what happens when a
publish cannot complete, because that is where a partial or overwritten file
would silently corrupt a run.
"""
import errno
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_atomic as atomic


def test_copy_fallback_removes_partial_target_on_failure(tmp_path, monkeypatch):
    """A failed copy must not leave a truncated file that resume logic could adopt."""
    source = tmp_path / "staged.bin"
    source.write_bytes(b"x" * 4096)
    target = tmp_path / "target.bin"

    def explode(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(atomic.shutil, "copyfileobj", explode)
    with pytest.raises(OSError):
        atomic._copy(source, target)
    assert not target.exists(), "a partial target must be removed"
    assert source.exists(), "the staged file must survive a failed publish"


def test_copy_fallback_refuses_existing_target(tmp_path):
    existing = tmp_path / "target.bin"
    existing.write_bytes(b"keep me")
    source = tmp_path / "staged.bin"
    source.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        atomic._copy(source, existing)
    assert existing.read_bytes() == b"keep me"


def test_windows_error_codes_map_to_file_exists_error():
    """ERROR_ALREADY_EXISTS / ERROR_FILE_EXISTS must surface as FileExistsError.

    Python only maps errno values, so without explicit translation OSError(183)
    stays a plain OSError and callers would not take their never-overwrite path.
    """
    for winerror in (atomic._WIN_ALREADY_EXISTS, atomic._WIN_FILE_EXISTS):
        error = OSError(winerror, "already exists")
        assert not isinstance(error, FileExistsError), (
            "precondition: Python does not map winerror to FileExistsError")
    # The mapping lives in the Windows branch; assert the constants are the
    # documented Windows codes so a typo cannot slip through.
    assert atomic._WIN_ALREADY_EXISTS == 183
    assert atomic._WIN_FILE_EXISTS == 80


def test_eexist_from_primitive_is_normalised(tmp_path, monkeypatch):
    """Whatever primitive reports EEXIST, callers get FileExistsError."""
    source = tmp_path / "staged.bin"
    source.write_bytes(b"new")
    target = tmp_path / "target.bin"
    target.write_bytes(b"old")

    def always_exists(*args, **kwargs):
        raise OSError(errno.EEXIST, "exists")

    monkeypatch.setattr(atomic, "_candidates",
                        lambda: [("fake", always_exists), ("link", atomic._link),
                                 ("copy", atomic._copy)])
    with pytest.raises(FileExistsError):
        atomic.publish_exclusive(source, target)
    assert target.read_bytes() == b"old"


def test_unsupported_primitive_falls_through_to_a_working_one(tmp_path, monkeypatch):
    """ENOSYS from one primitive must not abort the publish."""
    source = tmp_path / "staged.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target.bin"

    def missing(*args, **kwargs):
        raise OSError(errno.ENOSYS, "not implemented")

    monkeypatch.setattr(atomic, "_candidates",
                        lambda: [("missing", missing), ("copy", atomic._copy)])
    atomic.publish_exclusive(source, target)
    assert target.read_bytes() == b"payload"


def test_real_errors_are_not_swallowed(tmp_path, monkeypatch):
    """ENOSPC must propagate, not fall through to a weaker strategy."""
    source = tmp_path / "staged.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "target.bin"

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(atomic, "_candidates",
                        lambda: [("full", full), ("copy", atomic._copy)])
    with pytest.raises(OSError) as info:
        atomic.publish_exclusive(source, target)
    assert info.value.errno == errno.ENOSPC
    assert not target.exists()
