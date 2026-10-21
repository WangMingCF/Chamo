#!/usr/bin/env python3
"""Build a self-contained Windows package for the command-line engine.

Windows has no package manager equivalent to dpkg that would let us declare a
python3 dependency sanely, and asking users to install Python first is a poor
first-run experience for a converter. The official "embeddable package" is
designed exactly for this: an ~11 MB redistributable CPython with no installer.
That makes the ZIP self-contained, which is the property that matters here.

The engine is written in Python and stays that way: this repackages the existing
engine, it does not rewrite it.

Usage (on Windows, or any host that can run pip for win_amd64):
    python scripts/build_windows.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_deps import RUNTIME, WINDOWS_PYTHON  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EMBED_URL = ("https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
             % (WINDOWS_PYTHON, WINDOWS_PYTHON))


def release() -> dict:
    return json.loads((ROOT / "config/release.json").read_text(encoding="utf-8"))


def download(url: str, target: Path, cache: Path) -> Path:
    """Fetch the embeddable CPython once and reuse it across builds."""
    if cache.is_file():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    print("downloading", url)
    urllib.request.urlretrieve(url, cache)
    return cache


def prepare_python(pkg: Path, cache: Path) -> Path:
    archive = download(EMBED_URL, pkg / "python-embed.zip", cache)
    py = pkg / "python"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(py)
    (pkg / "python-embed.zip").unlink(missing_ok=True)
    # The embeddable distribution ships a ._pth file that deliberately disables
    # site-packages. Adding third-party wheels needs it enabled, otherwise the
    # engine imports fail even though the packages are present.
    pth = next(py.glob("python*._pth"))
    lines = [line for line in pth.read_text(encoding="utf-8").splitlines()]
    if "import site" not in [line.strip() for line in lines]:
        lines.append("import site")
    if "Lib\\site-packages" not in [line.strip() for line in lines]:
        lines.append("Lib\\site-packages")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return py


def install_wheels(py: Path) -> None:
    site = py / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    # Install with the build interpreter pip into the target prefix: the embeddable
    # distribution has no pip, and adding one would put a tool inside the package
    # that the user has no reason to run.
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
           "--target", str(site), *RUNTIME]
    subprocess.run(cmd, check=True)
    for cache in site.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def copy_engine(pkg: Path) -> None:
    shutil.copytree(ROOT / "engine", pkg / "engine",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def launcher(pkg: Path) -> None:
    """A .cmd wrapper so the package is usable without knowing the interpreter path."""
    body = "\r\n".join([
        "@echo off",
        "rem Command-line front end: same engine, same rules as the app.",
        "setlocal",
        'set "HERE=%~dp0"',
        '"%HERE%python\\python.exe" -I -B "%HERE%engine\\chamo.py" %*',
        "exit /b %ERRORLEVEL%",
        "",
    ])
    # write_bytes: text mode on Windows would turn the \n inside the already-CRLF
    # body into \r\r\n (cmd.exe tolerates it, but the shipped file was wrong).
    (pkg / "chamo.cmd").write_bytes(body.encode("utf-8"))
    for name in ("USER_GUIDE.txt", "RELEASE_NOTES.txt", "LICENSES.txt"):
        source = ROOT / "docs" / name
        if source.exists():
            shutil.copy2(source, pkg / name)
    shutil.copy2(ROOT / "LICENSE", pkg / "LICENSE.txt")  # MIT: the notice ships with every copy
    shutil.copy2(ROOT / "engine/THIRD_PARTY_NOTICES.md", pkg / "THIRD_PARTY_NOTICES.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Windows portable package")
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--cache", default=str(ROOT / "build/cache"))
    args = parser.parse_args()

    version = release()["version"]
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="chamo-win-"))
    try:
        pkg = stage / "chamo"
        pkg.mkdir(parents=True)
        py = prepare_python(pkg, Path(args.cache) / Path(EMBED_URL).name)
        install_wheels(py)
        copy_engine(pkg)
        launcher(pkg)
        target = out / ("chamo-cli_%s_windows-x64.zip" % version)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(pkg.rglob("*")):
                if path.is_file():
                    zf.write(path, Path("chamo") / path.relative_to(pkg))
        digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
        print(json.dumps({"zip": str(target), "bytes": target.stat().st_size,
                          "sha256": digest, "version": version,
                          "python": WINDOWS_PYTHON}, ensure_ascii=False))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
