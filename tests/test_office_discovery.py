"""Office renderer discovery: a machine with LibreOffice installed must find it.

The CI runner installs LibreOffice on every platform and still reported "not found"
on Windows and macOS, so the Office tests were skipped while the workflow looked
green. These tests pin the system install locations; the Windows-only test below
turns that class of silent skip into a failure on the platform where it happened.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

import platform_office


def lower(paths):
    return [str(p).lower() for p in paths]


def test_windows_install_locations_are_searched(tmp_path, monkeypatch):
    program_files, program_files_x86, local_appdata = (tmp_path / "PF", tmp_path / "PF86", tmp_path / "LA")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setenv("ProgramFiles(x86)", str(program_files_x86))
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.delenv("ProgramW6432", raising=False)
    candidates = lower(platform_office._system_layouts())
    for expected in (program_files / "LibreOffice/program/soffice.exe",
                     program_files_x86 / "LibreOffice/program/soffice.exe",
                     local_appdata / "Programs/LibreOffice/program/soffice.exe"):
        assert str(expected).lower() in candidates, expected


def test_application_and_distribution_folders_are_searched():
    # Build the expectation the same way the code does: on Windows Path("/Applications")
    # becomes a backslash, drive-relative path, so a hard-coded POSIX string would fail
    # there for the wrong reason (it did, in CI).
    candidates = lower(platform_office._system_layouts())
    for expected in (Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
                     Path("/usr/lib/libreoffice/program/soffice")):
        assert str(expected).lower() in candidates, expected


def test_no_renderer_anywhere_is_reported_as_none(tmp_path, monkeypatch):
    # A machine with no renderer at all: no system install, nothing next to the engine,
    # nothing usable on PATH. The answer must be None, not a bogus path. (The CI runners
    # do have one installed, so the empty system list is pinned here instead of assumed.)
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("CHAMO_SOFFICE", raising=False)
    monkeypatch.setattr(platform_office, "_system_layouts", lambda: [])
    monkeypatch.setattr(platform_office.shutil, "which", lambda name: None)
    assert platform_office.find(tmp_path / "engine") is None


def test_an_explicit_broken_path_does_not_masquerade_as_a_renderer(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "soffice"
    monkeypatch.setenv("CHAMO_SOFFICE", str(missing))
    found = platform_office.find(tmp_path / "engine")
    assert found != str(missing)
    # A machine that does have one elsewhere may still answer with it, but the answer
    # must be runnable - a stale path must never be handed to the converter.
    if found is not None:
        assert platform_office.runnable(found), found


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: asserts the CI runner finds the LibreOffice its job installed")
def test_windows_runner_finds_the_installed_libreoffice():
    found = platform_office.find()
    assert found, "LibreOffice was installed but the engine still cannot find it"
    assert found.lower().endswith(("soffice.exe", "soffice.com")), found
    assert Path(found).is_file(), found

def test_registry_install_path_is_a_program_folder():
    # The registry value points at the program folder, not at the install root:
    # appending "program" again produced a path that can never exist.
    program = "C:/Program Files/LibreOffice/program"
    candidates = platform_office._registry_install_paths([program])
    assert candidates[0] == Path(program) / "soffice.exe"
    assert Path(program) / "program/soffice.exe" in candidates


def test_a_registry_value_that_is_an_executable_is_used_directly():
    exe = "D:/Apps/LibreOffice/program/soffice.exe"
    candidates = platform_office._registry_install_paths([exe])
    assert candidates[0] == Path(exe)


def test_a_directory_is_not_a_renderer(tmp_path):
    fake = tmp_path / "Helpers/LibreOffice/program/soffice"
    fake.mkdir(parents=True)
    assert platform_office.runnable(fake) is False
    assert platform_office.find(tmp_path) != str(fake)
