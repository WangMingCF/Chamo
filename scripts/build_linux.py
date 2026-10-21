#!/usr/bin/env python3
"""Build an installable Debian package for the command-line engine.

The macOS app bundles its own runtime; Linux has no equivalent, so this produces a
.deb that installs the engine and a launcher on PATH. Choices worth stating:

  * the interpreter is the system python3 and the wheels are bundled in a venv.
    Shipping a whole CPython would add tens of megabytes to every install, and the
    engine only needs a stock 3.10+.
  * LibreOffice is a Recommends, not a Depends. It is a large optional engine used
    only for Office conversion, so PDF and image work must not require it. When it
    is absent the engine reports missing_renderer instead of failing obscurely.
  * the output is a .deb rather than an AppImage: this is a command-line tool, and
    a package manager is how a CLI is normally installed and upgraded.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/opt/chamo"

# The wheel set is shared with the Windows packager so the two artifacts cannot
# disagree about what the engine imports; see scripts/engine_deps.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_deps import RUNTIME  # noqa: E402

DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def release() -> dict:
    return json.loads((ROOT / "config/release.json").read_text(encoding="utf-8"))


def run(cmd, **kw):
    result = subprocess.run(cmd, **kw)
    if result.returncode != 0:
        raise SystemExit("command failed (%d): %s" % (result.returncode, cmd))
    return result


def build_engine(stage: Path) -> None:
    target = stage / PREFIX.lstrip("/") / "engine"
    shutil.copytree(
        ROOT / "engine", target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def build_venv(stage: Path) -> None:
    venv = stage / PREFIX.lstrip("/") / "venv"
    py = "python%d.%d" % (sys.version_info.major, sys.version_info.minor)
    site = venv / "lib" / py / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    # Install into the target prefix and skip byte-compilation here; the postinst
    # step in a later revision could compile once instead of shipping stale caches.
    run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
         "--target", str(site), *RUNTIME])
    # The venv must use the system interpreter, not the one that built the package.
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n", encoding="utf-8")
    for name in ("python", "python3"):
        link = venv / "bin" / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("/usr/bin/python3")


def launcher(stage: Path) -> None:
    bin_dir = stage / "usr/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "chamo"
    body = "#!/bin/sh\n"
    body += "# Installed by chamo-cli; same engine and rules as the app.\n"
    body += "exec " + PREFIX + "/venv/bin/python3 -I -B " + PREFIX + "/engine/chamo.py "
    body += chr(34) + chr(36) + "@" + chr(34) + "\n"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)


def installed_size_kb(stage: Path) -> int:
    total = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
    return max(1, total // 1024)


def control(stage: Path, version: str, arch: str) -> None:
    meta = stage / "DEBIAN"
    meta.mkdir(parents=True, exist_ok=True)
    fields = [
        "Package: chamo-cli",
        "Version: " + version,
        "Architecture: " + arch,
        "Maintainer: Chamo <noreply@example.invalid>",
        "Installed-Size: %d" % installed_size_kb(stage),
        "Depends: python3 (>= 3.10)",
        # Office conversion is the only feature that needs it. Recommends keeps it
        # optional so PDF and image work install and run without it.
        "Recommends: libreoffice-core | libreoffice",
        "Section: utils",
        "Priority: optional",
        "Description: Offline file conversion engine with a command line",
        " Converts PDF and Office documents to images, images to PDF, splits and",
        " merges PDFs, and prepares files for RAG ingestion. Runs entirely offline;",
        " originals are never modified and differing output is never overwritten.",
        " Office conversion additionally requires LibreOffice.",
        "",
    ]
    (meta / "control").write_text("\n".join(fields), encoding="utf-8")
    doc = stage / "usr/share/doc/chamo-cli"
    doc.mkdir(parents=True, exist_ok=True)
    for name in ("USER_GUIDE.txt", "LICENSES.txt", "RELEASE_NOTES.txt"):
        source = ROOT / "docs" / name
        if source.exists():
            shutil.copy2(source, doc / name)
    shutil.copy2(ROOT / "LICENSE", doc / "LICENSE")  # MIT: the notice ships with every copy
    shutil.copy2(ROOT / "engine/THIRD_PARTY_NOTICES.md", doc / "THIRD_PARTY_NOTICES.md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Linux .deb package")
    parser.add_argument("--out", default=str(ROOT / "release"))
    args = parser.parse_args()

    version = release()["version"]
    arch = DEB_ARCH.get(platform.machine(), platform.machine())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    stage = Path(tempfile.mkdtemp(prefix="chamo-deb-"))
    try:
        build_engine(stage)
        build_venv(stage)
        launcher(stage)
        control(stage, version, arch)
        target = out / ("chamo-cli_%s_%s.deb" % (version, arch))
        run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)])
        digest = subprocess.run(["sha256sum", str(target)], capture_output=True,
                                text=True, check=True).stdout.split()[0]
        print(json.dumps({"deb": str(target), "bytes": target.stat().st_size,
                          "sha256": digest, "arch": arch, "version": version},
                         ensure_ascii=False))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
