#!/usr/bin/env python3
"""Fail-closed check that shipped documents name the version being released.

A package can ship a USER_GUIDE whose first line names an older version while the
RELEASE_NOTES in the very same package name the new one. A user opening the guide
gets a contradiction the code cannot fix, and no other acceptance step looks at the
documents that ship inside the artifact. This is that step.

Only the first line is pinned. It is the reader's first signal of which version they
hold, and it is what went stale; comparing whole documents would fail for edits that
are not version errors. The check is fail-closed: a missing file is a FAIL, not a
skip, because a packaging change that forgets to copy the guide is the same defect.

Usage:
  scripts/verify_docs_version.py                     # repo docs vs config/release.json
  scripts/verify_docs_version.py --version 0.2.0     # explicit version
  scripts/verify_docs_version.py --packaged-guide "$MNT/开始使用.txt" \
      --packaged-notes "$MNT/版本说明.txt"           # extra copies inside an artifact
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE_TEMPLATE = "Chamo {version} 使用说明"
NOTES_PREFIX = "Chamo {version} —"

failures = []


def first_line(path: Path) -> str:
    """First non-empty line, or '' when the file is missing/unreadable."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return ""


def check(path: Path, expected: str, label: str, prefix: bool = False) -> None:
    actual = first_line(path)
    ok = actual.startswith(expected) if prefix else actual == expected
    if ok:
        print("PASS %s: %s" % (label, actual))
    elif not actual:
        print("FAIL %s: missing or unreadable %s" % (label, path))
        failures.append(label)
    else:
        print("FAIL %s: got %r want %r%s (%s)"
              % (label, actual, expected, " prefix" if prefix else "", path))
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify shipped documents name the release version")
    parser.add_argument("--root", default=str(ROOT), help="project root (default: repository root)")
    parser.add_argument("--version", default=None, help="expected version (default: config/release.json)")
    parser.add_argument("--packaged-guide", action="append", default=[],
                        help="extra USER_GUIDE copy inside an artifact (repeatable)")
    parser.add_argument("--packaged-notes", action="append", default=[],
                        help="extra RELEASE_NOTES copy inside an artifact (repeatable)")
    parser.add_argument("--release-notes", action="append", default=[],
                        help="GitHub release body draft whose first line must name the version (repeatable)")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    version = args.version
    if not version:
        try:
            config = json.loads((root / "config/release.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print("FAIL cannot read config/release.json: %s" % error)
            return 1
        version = str(config.get("version") or "").strip()
    if not version:
        print("FAIL config/release.json has no version")
        return 1

    print("checking shipped documents for version %s under %s" % (version, root))
    check(root / "docs/USER_GUIDE.txt", GUIDE_TEMPLATE.format(version=version), "repo USER_GUIDE first line")
    check(root / "docs/RELEASE_NOTES.txt", NOTES_PREFIX.format(version=version),
          "repo RELEASE_NOTES first line", prefix=True)
    for path in args.packaged_guide:
        check(Path(path), GUIDE_TEMPLATE.format(version=version), "packaged USER_GUIDE first line")
    for path in args.packaged_notes:
        check(Path(path), NOTES_PREFIX.format(version=version),
              "packaged RELEASE_NOTES first line", prefix=True)
    # The GitHub body is written by hand before publishing and can still be the
    # previous release's text. Nothing else checks it, so the publish preflight does.
    for path in args.release_notes:
        check(Path(path), NOTES_PREFIX.format(version=version),
              "github release notes first line", prefix=True)

    if failures:
        print("\nDOCS VERSION: FAIL (%d)" % len(failures))
        return 1
    print("\nDOCS VERSION: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
