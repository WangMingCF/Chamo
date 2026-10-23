"""Pruning the bundled runtime has to be decided by what is actually there.

pending() gates two things: a normal build prunes when it is true, and a
CHAMO_NO_PRUNE control build refuses to run when it is false. A pending() that is
always true makes the control build impossible; one that is always false ships an
unpruned runtime. Both directions are tested here, on hand-built runtime layouts,
because the real 200 MB runtime only exists inside a packaged build.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prune_python = _load("chamo_prune_python", "scripts/prune_python.py")


def _runtime(tmp_path: Path) -> Path:
    """A minimal runtime layout: lib/python3.12/{site-packages,stdlib} and bin."""
    runtime = tmp_path / "python"
    (runtime / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (runtime / "bin").mkdir()
    return runtime


def test_an_empty_runtime_has_nothing_to_prune(tmp_path):
    runtime = _runtime(tmp_path)
    assert prune_python.pending(runtime) is False


def test_a_site_package_marks_the_runtime_as_pending(tmp_path):
    runtime = _runtime(tmp_path)
    (runtime / "lib" / "python3.12" / "site-packages" / "pytest").mkdir()
    assert prune_python.pending(runtime) is True


def test_the_terminal_marker_marks_the_runtime_as_pending(tmp_path):
    runtime = _runtime(tmp_path)
    (runtime / "include").mkdir()
    assert prune_python.pending(runtime) is True


def test_a_stdlib_module_marks_the_runtime_as_pending(tmp_path):
    runtime = _runtime(tmp_path)
    directory = runtime / "lib" / "python3.12" / "idlelib"
    directory.mkdir()
    assert prune_python.pending(runtime) is True


def test_a_console_script_marks_the_runtime_as_pending(tmp_path):
    runtime = _runtime(tmp_path)
    (runtime / "bin" / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
    assert prune_python.pending(runtime) is True


def test_a_fully_pruned_runtime_is_not_pending(tmp_path):
    """The state CHAMO_NO_PRUNE requires: nothing left to remove."""
    runtime = _runtime(tmp_path)
    runtime.joinpath("lib/python3.12/site-packages").rmdir()
    assert prune_python.pending(runtime) is False


def test_prune_removes_every_known_part_and_reports_megabytes(tmp_path):
    runtime = _runtime(tmp_path)
    site = runtime / "lib" / "python3.12" / "site-packages"
    (site / "reportlab").mkdir()
    (site / "reportlab" / "blob").write_bytes(b"x" * (1024 * 1024))
    (runtime / "lib" / "python3.12" / "tkinter").mkdir()
    (runtime / "bin" / "pytest").write_text("#!/bin/sh\n", encoding="utf-8")
    (runtime / "include").mkdir()

    freed = prune_python.prune(runtime)

    assert freed >= 1, freed
    assert prune_python.pending(runtime) is False


def test_prune_leaves_packaging_alone(tmp_path):
    """pikepdf imports packaging at run time; removing it breaks JBIG2 PDFs."""
    runtime = _runtime(tmp_path)
    kept = runtime / "lib" / "python3.12" / "site-packages" / "packaging"
    kept.mkdir()
    prune_python.prune(runtime)
    assert kept.is_dir(), "prune removed a package the engine needs at run time"
