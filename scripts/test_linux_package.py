#!/usr/bin/env python3
"""Build the Linux package and verify it by actually installing it.

CI passing on the source tree says nothing about the artifact users download, so
this runs the real path: build the .deb, install it into a clean container, and
convert with it. The checks live in scripts/verify_deb.sh so this wrapper and the
GitHub Actions job cannot drift apart.

Requires Docker (or colima) and the dw-engine-linux image.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "dw-engine-linux:2"
VERIFY = "scripts/verify_deb.sh"


def in_container(command):
    return subprocess.run(
        ["docker", "run", "--rm", "-v", str(ROOT) + ":/w", "-w", "/w", IMAGE, *command],
        capture_output=True, text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and verify the Linux package")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if not (ROOT / VERIFY).is_file():
        print("missing " + VERIFY, file=sys.stderr)
        return 1

    if not args.skip_build:
        # dpkg-deb only exists on Debian-family hosts, so the build runs in the same
        # container that installs the result; building on macOS would also tag the
        # package with the wrong architecture.
        built = in_container(["python3", "scripts/build_linux.py"])
        if built.returncode != 0:
            print(built.stdout[-2000:])
            print(built.stderr[-2000:], file=sys.stderr)
            return 1
        lines = [l for l in built.stdout.strip().splitlines() if l.startswith("{")]
        print(lines[-1] if lines else built.stdout.strip())

    result = in_container(["bash", "/w/" + VERIFY])
    print((result.stdout + result.stderr).strip()[-4000:])
    if result.returncode == 0:
        print("\nLinux package verified")
    else:
        print("\nLinux package FAILED", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
