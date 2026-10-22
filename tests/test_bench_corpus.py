"""The benchmark corpus has to be reproducible, or the benchmark is not one.

A comparison is only meaningful if the input did not change between two runs. The
generator claims exactly that, so this file builds the same suite twice and
requires byte equality - and names the zip member that differs when it does not,
because "somewhere in the .xlsx" is not a diagnosis.
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


corpus = _load("chamo_bench_corpus", "scripts/bench_corpus.py")


def _member_differences(left: Path, right: Path) -> list:
    """Which parts inside two OOXML packages differ (empty for a non-zip file).

    A byte difference in a .xlsx or .pptx says "something changed" but not what,
    and the writer that changed it is what the next fix has to target. Naming the
    members turns a red test into a diagnosis.
    """
    import zipfile

    try:
        with zipfile.ZipFile(left) as first, zipfile.ZipFile(right) as second:
            names = sorted(set(first.namelist()) | set(second.namelist()))
            changed = []
            for name in names:
                if name not in first.namelist() or name not in second.namelist():
                    changed.append(name + " (present on one side only)")
                elif first.read(name) != second.read(name):
                    changed.append(name)
            return changed
    except zipfile.BadZipFile:
        return []


def test_the_same_suite_builds_the_same_bytes(tmp_path):
    """A benchmark comparison is only meaningful if the input did not change."""
    first = corpus.build(tmp_path / "a", "smoke")
    second = corpus.build(tmp_path / "b", "smoke")
    differing = [entry["path"] for entry, other in zip(first["files"], second["files"])
                 if entry["sha256"] != other["sha256"]]
    detail = {name: _member_differences(tmp_path / "a" / name, tmp_path / "b" / name)
              for name in differing}
    assert differing == [], "the smoke corpus is not reproducible: %s" % detail
    assert corpus.check(tmp_path / "a") == []


def test_a_changed_or_extra_file_is_reported(tmp_path):
    corpus.build(tmp_path / "c", "smoke")
    assert corpus.check(tmp_path / "c") == []
    (tmp_path / "c" / "text.pdf").write_bytes(b"not the same pdf")
    (tmp_path / "c" / "stray.bin").write_bytes(b"x")
    issues = corpus.check(tmp_path / "c")
    assert any("text.pdf" in issue and "changed" in issue for issue in issues), issues
    assert any("stray.bin" in issue for issue in issues), issues


def test_refuses_to_write_over_a_previous_corpus(tmp_path):
    corpus.build(tmp_path / "d", "smoke")
    try:
        corpus.build(tmp_path / "d", "smoke")
    except SystemExit as error:
        assert "non-empty" in str(error)
    else:
        raise AssertionError("building into a used directory must be refused")
