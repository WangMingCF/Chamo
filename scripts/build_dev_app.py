"""Assemble a debug Chamo.app for automated UI tests, without packaging.

The app expects Contents/Resources/python/bin/python3 and Contents/Resources/engine.
Instead of copying the bundled runtime and LibreOffice (what build_app.py does for
releases), this links them: engine/ from this checkout and python/ to an existing
Python prefix that already has the engine's dependencies (the CI runner's Python,
or third_party/python locally). The binary is compiled with -D CHAMO_DEBUG so the
scenario hooks in Sources/DebugHooks.swift are available. Office conversions are
not available in this bundle (no LibreOffice helper); UI scenarios use PDF and
image tools.

    python scripts/build_dev_app.py --out build/dev [--python-prefix PREFIX]
"""
from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORKS = ["SwiftUI", "AppKit", "PDFKit", "UniformTypeIdentifiers", "ImageIO",
              "QuickLookThumbnailing", "Quartz", "UserNotifications"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "dev")
    ap.add_argument("--python-prefix", type=Path, default=Path(sys.prefix),
                    help="a Python prefix with the engine dependencies (default: this interpreter)")
    a = ap.parse_args()
    prefix = a.python_prefix.resolve()
    if not (prefix / "bin" / "python3").exists():
        print(f"no bin/python3 under {prefix}", file=sys.stderr)
        return 2

    app = a.out.resolve() / "Chamo.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    res = app / "Contents" / "Resources"
    res.mkdir()
    # A directory link, so the interpreter finds its own lib/ and site-packages
    # relative to Resources/python/bin/python3 without copying anything.
    (res / "python").symlink_to(prefix, target_is_directory=True)
    (res / "engine").symlink_to(ROOT / "engine", target_is_directory=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleName": "Chamo", "CFBundleExecutable": "Chamo",
                       "CFBundleIdentifier": "io.github.wangmingcf.chamo.uitest",
                       "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.0.0-uitest",
                       "CFBundleVersion": "0", "LSMinimumSystemVersion": "15.0",
                       "NSHighResolutionCapable": True}, f)

    sources = sorted(p for p in (ROOT / "Sources").glob("*.swift") if p.name != "ImageDecode.swift")
    cache = a.out.resolve() / "module-cache"
    cmd = ["swiftc", "-swift-version", "5", "-parse-as-library", "-Onone", "-D", "CHAMO_DEBUG",
           "-module-cache-path", str(cache), "-target", "arm64-apple-macosx15.0",
           *map(str, sources), "-o", str(app / "Contents" / "MacOS" / "Chamo")]
    for fw in FRAMEWORKS:
        cmd += ["-framework", fw]
    subprocess.run(cmd, check=True)
    print(app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
