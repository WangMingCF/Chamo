"""Cleanup of the staging folder must be safe to run twice.

`finish()` removes this run's staging folders *before* it emits the finished
event, and the surrounding finally removes them again. If either call were not
idempotent the retry would raise after the run had already finished and turn a
completed job into an error - the exact failure #52 exists to prevent. The
reviewer of that PR asked for this guarantee; here it is as a test instead of as
prose in a comment.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import platform_state  # noqa: E402
import workbench  # noqa: E402

FINGERPRINT = "abcd1234"


def _layout(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    record = tmp_path / "record"
    record.mkdir()
    root = platform_state.staging(out)
    # The staging root must live under the output folder the caller owns:
    # drop_staging calls rmdir on it, so a test pointed at a user-level state
    # directory could delete real data. Pinned here so a future change to
    # platform_state cannot quietly widen this test's blast radius.
    assert root.parent == out, "staging root is not derived from the output folder"
    staged = root / (FINGERPRINT + "-deadbeef")
    staged.mkdir(parents=True)
    (staged / "page0001.png").write_bytes(b"x")
    (record / ("staging-" + FINGERPRINT + ".json")).write_text(
        json.dumps({"path": str(staged), "fingerprint": FINGERPRINT}), encoding="utf-8")
    return out, record, staged, root


def test_cleanup_is_safe_to_run_twice(tmp_path):
    out, record, staged, root = _layout(tmp_path)

    workbench._cleanup_run_staging(out, record)
    workbench._cleanup_run_staging(out, record)      # the retry in the finally block

    assert not staged.exists(), "the staging folder survived the first call"
    assert not (record / ("staging-" + FINGERPRINT + ".json")).exists(), \
        "the register file survived"


def test_dropping_the_staging_root_is_safe_to_run_twice(tmp_path):
    out, record, staged, root = _layout(tmp_path)

    workbench._cleanup_run_staging(out, record)
    platform_state.drop_staging(out)
    platform_state.drop_staging(out)                 # rmdir on a missing path

    assert not root.exists(), "an empty staging root was left behind"


def test_cleanup_leaves_a_folder_it_did_not_register(tmp_path):
    """A concurrent run's staging folder must survive this run's cleanup."""
    out, record, staged, root = _layout(tmp_path)
    other = root / "ffff0000-keepme"
    other.mkdir()

    workbench._cleanup_run_staging(out, record)

    assert other.is_dir(), "cleanup removed a staging folder this run never registered"


def test_retry_when_the_register_survived_but_the_directory_is_gone(tmp_path):
    """The retry that matters: the folder is gone but its register file is not.

    The earlier version of this file called the cleanup twice in a row and called
    that a retry. It was not: the first call deletes the register file too, so the
    second call finds nothing and never reaches the removal code. The case that
    actually happens in production is an unlink that fails - the folder is gone,
    the register survives, and the retry must not raise. Written by the reviewer
    (Mac-Claude) as a counter-test; the mutation that removes ignore_errors/
    missing_ok survives the old version and fails this one.
    """
    out, record, staged, root = _layout(tmp_path)
    workbench._cleanup_run_staging(out, record)
    # Put the register back: this is what an unlink failure leaves behind.
    (record / ("staging-" + FINGERPRINT + ".json")).write_text(
        json.dumps({"path": str(staged), "fingerprint": FINGERPRINT}),
        encoding="utf-8")

    workbench._cleanup_run_staging(out, record)      # must not raise

    assert not (record / ("staging-" + FINGERPRINT + ".json")).exists()


def test_drop_staging_keeps_a_root_a_concurrent_run_still_uses(tmp_path):
    """drop_staging must not remove the whole root while someone else is in it.

    A mutation that turns drop_staging into rmtree(root) passes the old tests and
    deletes another run's staging folder. This one fails under that mutation.
    """
    out, record, staged, root = _layout(tmp_path)
    workbench._cleanup_run_staging(out, record)
    other = root / "ffff0000-keepme"
    other.mkdir()
    (other / "f").write_bytes(b"y")

    platform_state.drop_staging(out)

    assert (other / "f").is_file(), \
        "drop_staging removed a concurrent run's staging folder"


def test_a_register_pointing_outside_the_staging_root_deletes_nothing(tmp_path):
    """A forged register must not let cleanup delete a folder elsewhere.

    The path check (candidate.parent == staging root) is the only thing standing
    between a corrupt register file and deleting an unrelated directory with a
    matching name prefix. Removing that check survives the old tests and fails
    this one.
    """
    out, record, staged, root = _layout(tmp_path)
    victim = tmp_path / (FINGERPRINT + "-victim")
    victim.mkdir()
    (victim / "precious").write_bytes(b"z")
    (record / ("staging-" + FINGERPRINT + ".json")).write_text(
        json.dumps({"path": str(victim), "fingerprint": FINGERPRINT}),
        encoding="utf-8")

    workbench._cleanup_run_staging(out, record)

    assert (victim / "precious").is_file(), \
        "cleanup deleted a folder outside the staging root"


def test_a_symlink_inside_the_staging_root_is_not_followed(tmp_path):
    """A symlinked staging entry must not lead cleanup out of the root.

    POSIX only: creating symlinks on the Windows runner needs privileges this
    test does not have, and skipping on one platform must be registered in
    tests/ci_allowed_skips.json with a reason (docs/CI_TESTING.md section 5).
    """
    if sys.platform == "win32":
        pytest.skip("symlink creation needs privileges on Windows; see ci_allowed_skips.json")
    out, record, staged, root = _layout(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious").write_bytes(b"z")
    link = root / (FINGERPRINT + "-link")
    link.symlink_to(victim, target_is_directory=True)
    (record / ("staging-" + FINGERPRINT + ".json")).write_text(
        json.dumps({"path": str(link), "fingerprint": FINGERPRINT}),
        encoding="utf-8")

    workbench._cleanup_run_staging(out, record)

    assert (victim / "precious").is_file(), \
        "cleanup followed a symlink out of the staging root"
