"""Locate the Office renderer on the current platform.

The engine takes the executable path from the request so the app can ship its
own copy, but tests and the CLI need a sensible default. Keeping the search in
one place is what lets the same test suite run on macOS and Linux instead of
hard-coding a macOS bundle path.

Search order: explicit request/env path, then the app bundle layout, then the
project layout used by tests and the CLI, then PATH.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _layouts(engine_dir: Path) -> list[Path]:
    """Candidate renderer locations relative to a directory.

    Both the packaged layout (Resources/engine next to Helpers) and the project
    checkout (engine/ next to third_party) are searched, and the same layouts are
    also tried directly under the given directory so callers can point at an
    arbitrary root (tests, portable installs).
    """
    # The packaged engine lives at Contents/Resources/engine, so the bundle root
    # (Contents) is two levels up, not one. Missing this made the shipped app
    # unable to find its own LibreOffice and fall through to a system install.
    base = engine_dir.resolve()
    roots = []
    for root in (base, base.parent, base.parent.parent):
        if root not in roots:
            roots.append(root)
    candidates: list[Path] = []
    for root in roots:
        candidates += [
            root / "Helpers/LibreOffice.app/Contents/MacOS/soffice",
            root / "third_party/LibreOffice.app/Contents/MacOS/soffice",
            root / "Helpers/LibreOffice/program/soffice",
            root / "third_party/LibreOffice/program/soffice",
        ]
    return candidates


def _system_layouts() -> list[Path]:
    """Where a normal LibreOffice installation puts its executable on this platform.

    Discovered the hard way: the CI runner installs LibreOffice for every platform
    and then reported "not found" on both Windows and macOS, so every Office test
    was skipped while the workflow believed Office was covered.
      * Windows installs into Program Files (64-bit), Program Files (x86) (32-bit) or,
        for a per-user install, LOCALAPPDATA\\Programs - none of them on PATH. The UNO
        InstallPath registry value is the authoritative answer when it exists.
      * macOS installs /Applications/LibreOffice.app, and a user copy can live in
        ~/Applications.
      * A Debian system without the wrapper package has the real binary under
        /usr/lib/libreoffice/program (Fedora: /usr/lib64); only the wrapper ships
        soffice on PATH, which is why a minimal install looked like "no renderer".
    """
    candidates: list[Path] = [
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice",
        Path("/usr/lib/libreoffice/program/soffice"),
        Path("/usr/lib64/libreoffice/program/soffice"),
    ]
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "LibreOffice/program/soffice.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs/LibreOffice/program/soffice.exe")
    candidates += _registry_install_paths()
    return candidates


def _registry_install_paths(values: list[str] | None = None) -> list[Path]:
    """Candidates from LibreOffice's UNO InstallPath registry value (Windows only).

    That value is the *program* folder - "C:\\Program Files\\LibreOffice\\program" - not
    the install root. Treating it as a root produced "...\\program\\program\\soffice.exe",
    which never exists, so this branch was dead for anyone whose install is outside
    Program Files (the default install was found through the environment variables
    first, which is exactly why the mistake went unnoticed). Both shapes are accepted,
    in that order.

    `values` exists so the mapping can be tested without a Windows registry.
    """
    if values is None:
        values = _read_registry_install_paths()
    candidates: list[Path] = []
    for value in values:
        base = Path(value)
        if base.suffix.lower() in (".exe", ".com"):
            candidates.append(base)                            # a value that names the binary
            continue
        candidates.append(base / "soffice.exe")                # the program folder itself
        candidates.append(base / "program/soffice.exe")        # an install root
    return candidates


def _read_registry_install_paths() -> list[str]:
    """Read the value a real Windows install writes; empty everywhere else.

    LibreOffice itself looks in HKCU before HKLM, so a per-user install wins here too.
    """
    try:
        import winreg  # only importable on Windows
    except ImportError:
        return []
    values: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\LibreOffice\UNO\InstallPath") as handle:
                value, _ = winreg.QueryValueEx(handle, None)
        except OSError:
            continue
        if value:
            values.append(str(value))
    return values


def _runnable(path: Path) -> bool:
    """True when this file can actually be executed here.

    Two cases made a naive existence check wrong:
      * a macOS bundle checked out on Linux has soffice at the expected path but
        is a Mach-O binary, so the Office test exec'd it and failed;
      * distro LibreOffice ships /usr/bin/soffice as a symlink to a shell
        wrapper, not to an ELF file, so a bare magic check rejected a working
        install.
    Scripts (shebang) and symlinks are therefore accepted on POSIX, while a
    binary whose format does not match this platform is rejected.
    """
    if not path.exists() or not os.access(path, os.X_OK):
        return False
    if path.is_dir():
        # A directory named soffice is not a renderer. Returning it made find() hand a
        # directory to the converter, which then failed with a confusing exec error.
        return False
    if path.is_symlink():
        return path.exists() and os.access(path, os.X_OK)
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    if head[:2] == b"#!":                      # shell/Python wrapper
        return True
    if sys.platform == "darwin":
        return head in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                        b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")
    if sys.platform.startswith("linux"):
        return head == b"\x7fELF"
    if sys.platform == "win32":
        return head[:2] == b"MZ"
    return True


def find(engine_dir: Path | None = None) -> str | None:
    """Return a usable soffice path, or None when Office conversion is unavailable."""
    env = os.environ.get("CHAMO_SOFFICE")
    if env and _runnable(Path(env)):
        return env
    root = Path(engine_dir or Path(__file__).resolve().parent)
    for candidate in _layouts(root) + _system_layouts():
        if _runnable(candidate):
            return str(candidate)
    # Fall back to PATH so a Linux/Windows build can use a system LibreOffice.
    for name in (["soffice", "libreoffice"] if sys.platform != "win32" else ["soffice.exe"]):
        found = shutil.which(name)
        if found and _runnable(Path(found)):
            return found
    return None


def runnable(path) -> bool:
    """Public check: can this exact path be executed on this platform?"""
    return _runnable(Path(path))


def available(engine_dir: Path | None = None) -> bool:
    return find(engine_dir) is not None


if __name__ == "__main__":
    print(find() or "not found")
