"""The .deb must declare the Python version its bundled wheels actually need.

The package ships a venv whose C extensions are cpython-3X-*.so, but the control
file used to say "Depends: python3 (>= 3.10)". On a python3.11 host that
installs cleanly and then fails on first use:

    ImportError: cannot import name '_imaging' from 'PIL'

(reproduced from the real 0.1.0 .deb unpacked on a Debian 12 host). The version
is derived from the staged site-packages path so the two cannot drift apart
again.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("chamo_build_linux",
                                                  ROOT / "scripts" / "build_linux.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_linux = _load()


def _stage(tmp_path: Path, minor: str = "3.12") -> Path:
    stage = tmp_path / "stage"
    site = stage / "opt" / "chamo" / "venv" / "lib" / ("python" + minor) / "site-packages"
    site.mkdir(parents=True)
    (site / "PIL").mkdir()
    (site / "PIL" / "_imaging.cpython-%s-x86_64-linux-gnu.so" % minor.replace(".", "")).
        write_bytes(b"\x7fELF synthetic")
    return stage


def test_the_declared_python_version_matches_the_shipped_extensions(tmp_path):
    stage = _stage(tmp_path)
    build_linux.control(stage, "0.1.0", "amd64")
    text = (stage / "DEBIAN" / "control").read_text(encoding="utf-8")
    assert "Depends: python3 (>= 3.12), python3 (<< 3.13)" in text, text


def test_a_different_minor_version_changes_the_dependency(tmp_path):
    stage = _stage(tmp_path, "3.13")
    build_linux.control(stage, "0.1.0", "amd64")
    text = (stage / "DEBIAN" / "control").read_text(encoding="utf-8")
    assert "python3 (>= 3.13), python3 (<< 3.14)" in text, text


def test_a_stage_without_a_venv_fails_instead_of_guessing(tmp_path):
    stage = tmp_path / "empty"
    (stage / "opt").mkdir(parents=True)
    try:
        build_linux.required_python_minor(stage)
    except SystemExit as error:
        assert "cannot derive the Python version" in str(error), error
    else:
        raise AssertionError("a stage without site-packages must not produce a guess")
