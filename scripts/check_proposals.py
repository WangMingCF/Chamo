#!/usr/bin/env python3
"""Validate the PR-first proposal catalog; this is NOT a remote claim lock.

Run from any directory: python scripts/check_proposals.py [--root REPOSITORY].
Only the shared registry branch can establish exclusive ownership.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import re
import sys

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
STATUSES = {"proposed", "claimed", "developing", "validating", "review", "blocked", "merged", "abandoned"}
ACTIVE = {"claimed", "developing", "validating", "review", "blocked"}
TERMINAL = {"merged", "abandoned"}
PLATFORMS = {"linux", "windows", "macos"}
REQUIRED = {
    "schema_version", "id", "title", "feature_key", "status", "owner", "platforms",
    "write_scopes", "dependencies", "branch", "base_sha", "acceptance", "coverage_gaps",
}


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def strings(value: object, *, nonempty: bool = False) -> bool:
    return (isinstance(value, list) and (not nonempty or bool(value))
            and all(text(item) for item in value)
            and len(value) == len(set(value)))


def safe_scope(value: object) -> bool:
    """Conservative repository-relative file/directory prefixes, never globs."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or value != value.strip():
        return False
    if any(ord(c) < 32 or ord(c) == 127 or c in "\\:*?[]{}" for c in value):
        return False
    path = value[:-1] if value.endswith("/") else value
    return all(part.casefold() != ".git" and not part.endswith(".")
               and re.fullmatch(r"[A-Za-z0-9._-]+", part)
               for part in path.split("/"))


def scopes_overlap(left: str, right: str) -> bool:
    # Case-fold deliberately: Windows and default macOS filesystems ignore case.
    left, right = left.rstrip("/").casefold(), right.rstrip("/").casefold()
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def safe_branch(value: object) -> bool:
    if (not safe_scope(value) or value in {"HEAD", "main", "master", "swarm/registry"}
            or value.startswith(("-", "refs/"))):
        return False
    if ".." in value or "@{" in value or value.endswith(("/", ".")):
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c in "~^:?*[\\" for c in value):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock")
               for part in value.split("/"))


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def validate_proposal(data: object, directory_id: str) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["proposal.json must contain an object"]
    missing = REQUIRED - data.keys()
    if missing:
        errors.append("missing fields: " + ", ".join(sorted(missing)))
    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        errors.append("schema_version must be integer 1")
    for key in ("id", "feature_key", "owner"):
        value = data.get(key)
        if not isinstance(value, str) or len(value) > 128 or not SLUG.fullmatch(value):
            errors.append(f"{key} must be a lower-kebab identifier of at most 128 characters")
    if data.get("id") != directory_id:
        errors.append("id must match its directory name")
    for key in ("title", "owner"):
        if not text(data.get(key)):
            errors.append(f"{key} must be a nonempty string")
    status = data.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        errors.append("unknown status")
    platforms = data.get("platforms")
    if not strings(platforms, nonempty=True) or not set(platforms) <= PLATFORMS:
        errors.append("platforms must be a nonempty unique list of linux/windows/macos")
    scopes = data.get("write_scopes")
    if (not strings(scopes, nonempty=True) or len(scopes) > 128
            or not all(safe_scope(s) for s in scopes)
            or len({s.rstrip("/").casefold() for s in scopes}) != len(scopes)):
        errors.append("write_scopes must contain 1-128 unique safe repository-relative prefixes")
    dependencies = data.get("dependencies")
    if not strings(dependencies) or not all(SLUG.fullmatch(d) for d in dependencies):
        errors.append("dependencies must be unique proposal ids")
    if not strings(data.get("coverage_gaps")):
        errors.append("coverage_gaps must be a unique list of nonempty strings (may be empty)")
    for key, check in (("branch", safe_branch), ("base_sha", lambda s: isinstance(s, str) and SHA.fullmatch(s))):
        value = data.get(key)
        if not (value == "" and status in ("proposed", "abandoned")) and not check(value):
            errors.append(f"{key} must be valid (only proposed/abandoned may leave it empty)")
    acceptance = data.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        errors.append("acceptance must be a nonempty object list")
    else:
        ids = set()
        for item in acceptance:
            if not isinstance(item, dict) or not all(text(item.get(k)) for k in ("id", "description")):
                errors.append("each acceptance item needs nonempty id and description")
                continue
            if item["id"] in ids:
                errors.append("acceptance ids must be unique")
            ids.add(item["id"])
            if not strings(item.get("tests")):
                errors.append("acceptance tests must be a unique list of nonempty strings (may be empty)")
    return errors


def check_catalog(root: Path) -> list[str]:
    """Return all catalog errors without trusting metadata as ownership evidence."""
    catalog = root / "proposals"
    errors: list[str] = []
    proposals: dict[str, dict] = {}
    if catalog.is_symlink() or not catalog.is_dir():
        return ["proposals must exist as a real directory, not a symlink"]
    for directory in sorted(catalog.iterdir()):
        if directory.name == "_template":
            continue
        if directory.is_symlink():
            errors.append(f"proposals/{directory.name}: symlinks are not allowed")
            continue
        if not directory.is_dir():
            # Catalog documentation is not proposal metadata.
            if directory.suffix != ".md":
                errors.append(f"proposals/{directory.name}: unexpected file")
            continue
        prefix = f"proposals/{directory.name}: "
        if not SLUG.fullmatch(directory.name):
            errors.append(prefix + "directory name must be a lower-kebab identifier")
        valid_files = True
        for name in ("proposal.json", "PR.md", "PRD.md"):
            path = directory / name
            if path.is_symlink() or not path.is_file():
                errors.append(prefix + f"{name} must be a regular file, not a symlink")
                valid_files = False
            elif name.endswith(".md"):
                try:
                    if not path.read_text(encoding="utf-8").strip():
                        errors.append(prefix + f"{name} must not be empty")
                except (OSError, UnicodeError) as exc:
                    errors.append(prefix + f"cannot read {name}: {exc}")
        if not valid_files:
            continue
        try:
            data = json.loads((directory / "proposal.json").read_text(encoding="utf-8"),
                              object_pairs_hook=unique_object)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(prefix + f"invalid proposal.json: {exc}")
            continue
        problems = validate_proposal(data, directory.name)
        errors.extend(prefix + problem for problem in problems)
        if not problems:
            proposals[directory.name] = data
    if not proposals:
        errors.append("catalog must contain at least one valid non-template proposal")

    # Kahn's algorithm checks all dependency components, not only active ones.
    incoming = {key: 0 for key in proposals}
    dependents: dict[str, list[str]] = {key: [] for key in proposals}
    for key, data in proposals.items():
        for dep in data["dependencies"]:
            if dep not in proposals:
                errors.append(f"{key}: dependency {dep} does not exist or is invalid")
            else:
                incoming[key] += 1
                dependents[dep].append(key)
    ready = deque(key for key, count in incoming.items() if count == 0)
    while ready:
        key = ready.popleft()
        for dependent in dependents[key]:
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
    if any(incoming.values()):
        errors.append("dependency cycle (or depends on cycle): " + ", ".join(sorted(k for k in incoming if incoming[k])))

    entries = list(proposals.items())
    for index, (left_id, left) in enumerate(entries):
        for right_id, right in entries[index + 1:]:
            if (left["status"] not in TERMINAL and right["status"] not in TERMINAL
                    and left["feature_key"] == right["feature_key"]):
                errors.append(f"{left_id} / {right_id}: duplicate open feature_key")
            if left["status"] in ACTIVE and right["status"] in ACTIVE:
                if any(scopes_overlap(a, b) for a in left["write_scopes"] for b in right["write_scopes"]):
                    errors.append(f"{left_id} / {right_id}: active write_scopes overlap")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        errors = check_catalog(args.root)
    except OSError as exc:
        errors = [f"cannot read proposal catalog: {exc}"]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Proposal catalog valid. Directory status is not a remote ownership lock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
