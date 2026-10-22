#!/usr/bin/env python3
"""Stop the maintainer's private identifiers from reaching anything public.

The keyword file holds only hashes, so the VPS, the agent's context and the
model provider never see the real values. Hashes stop accidental exposure, not a
determined attacker: short values such as phone numbers can be brute-forced.

Keyword file format (one entry per line, '#' starts a comment):
  sha256:<hex digest of the casefolded value>:<length in characters>
  allow:<regex>     text matching this is removed before scanning, so the GitHub
                    handle may appear as the repository owner and nowhere else

Commands:
  hash               read one value per line on stdin, print keyword lines
  push               pre-push hook mode: read git's ref lines on stdin
  range BASE..HEAD   scan commits in a range (identity, message, added lines)
  text               scan stdin
  files PATH...      scan files, directories and zip archives

Hits are reported by entry number only; the value never goes to the output,
because the output may land in an agent's context or a public comment.
Exit codes: 0 clean, 1 hit, 2 configuration or usage error (fail closed).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

DEFAULT_KEYWORDS = Path.home() / ".config/chamo-agent/private-keywords.txt"
DEFAULT_IDENTITY = "Chamo <dev@chamo.invalid>"
ZERO = "0" * 40


class Rules:
    def __init__(self, path: Path):
        if not path.is_file():
            raise SystemExit(f"privacy_scan: keyword file missing: {path} (refusing to pass without it)")
        self.hashes: dict[int, dict[str, int]] = {}
        self.allow: list[re.Pattern[str]] = []
        number = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("allow:"):
                self.allow.append(re.compile(line[6:].strip(), re.IGNORECASE))
                continue
            m = re.fullmatch(r"sha256:([0-9a-f]{64}):(\d+)", line)
            if not m:
                raise SystemExit(f"privacy_scan: bad keyword line in {path}: entries must be sha256:<hex>:<len> or allow:<regex>")
            number += 1
            self.hashes.setdefault(int(m.group(2)), {})[m.group(1)] = number
        if not self.hashes:
            raise SystemExit(f"privacy_scan: no keyword hashes in {path} (refusing to pass without them)")

    def hits(self, text: str) -> list[int]:
        for pattern in self.allow:
            text = pattern.sub(" ", text)
        text = text.casefold()
        found: set[int] = set()
        for length, table in self.hashes.items():
            for i in range(len(text) - length + 1):
                digest = hashlib.sha256(text[i:i + length].encode("utf-8")).hexdigest()
                if digest in table:
                    found.add(table[digest])
        return sorted(found)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def added_lines(sha: str) -> str:
    # Only what a commit adds can leak; removing an identifier is the fix.
    patch = git("show", "--format=", "--no-color", "--unified=0", sha)
    return "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))


def scan_commits(rules: Rules, shas: list[str], identity: str) -> list[str]:
    problems = []
    for sha in shas:
        short = sha[:9]
        author, committer = git("show", "-s", "--format=%an <%ae>%n%cn <%ce>", sha).splitlines()[:2]
        for role, who in (("author", author), ("committer", committer)):
            if who != identity:
                problems.append(f"{short}: {role} is not the repository identity {identity}")
        names = git("show", "--format=", "--name-only", sha)
        for where, text in (("identity", author + "\n" + committer),
                            ("message", git("show", "-s", "--format=%B", sha)),
                            ("file names", names), ("added lines", added_lines(sha))):
            for n in rules.hits(text):
                problems.append(f"{short}: private keyword #{n} in {where}")
    return problems


def scan_tag(rules: Rules, sha: str, identity: str) -> list[str]:
    if git("cat-file", "-t", sha).strip() != "tag":
        return []
    body = git("cat-file", "-p", sha)
    problems = [f"tag {sha[:9]}: private keyword #{n}" for n in rules.hits(body)]
    tagger = next((l for l in body.splitlines() if l.startswith("tagger ")), "")
    if tagger and not tagger[7:].startswith(identity):
        problems.append(f"tag {sha[:9]}: tagger is not the repository identity {identity}")
    return problems


def push_mode(rules: Rules, identity: str) -> list[str]:
    problems = []
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha == ZERO:  # deleting a remote ref publishes nothing
            continue
        if local_ref.startswith("refs/tags/"):
            problems += scan_tag(rules, local_sha, identity)
            local_sha = git("rev-parse", f"{local_sha}^{{commit}}").strip()
        if remote_sha == ZERO:
            shas = git("rev-list", local_sha, "--not", "--remotes=origin").split()
        else:
            shas = git("rev-list", f"{remote_sha}..{local_sha}").split()
        problems += scan_commits(rules, shas, identity)
    return problems


def scan_path(rules: Rules, path: Path) -> list[str]:
    problems = []
    if path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            problems += scan_path(rules, child)
        return problems
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not info.is_dir():
                    text = z.read(info).decode("utf-8", "ignore")
                    problems += [f"{path}!{info.filename}: private keyword #{n}" for n in rules.hits(text)]
        return problems
    text = path.read_bytes().decode("utf-8", "ignore")
    return [f"{path}: private keyword #{n}" for n in rules.hits(text + "\n" + str(path))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["hash", "push", "range", "text", "files"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--keywords", type=Path,
                    default=Path(os.environ.get("CHAMO_PRIVATE_KEYWORDS", DEFAULT_KEYWORDS)))
    ap.add_argument("--identity", default=os.environ.get("CHAMO_GIT_IDENTITY", DEFAULT_IDENTITY))
    a = ap.parse_args()

    if a.command == "hash":
        for value in sys.stdin.read().splitlines():
            value = value.strip().casefold()
            if value:
                print(f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}:{len(value)}")
        return 0

    try:
        rules = Rules(a.keywords)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    if a.command == "push":
        problems = push_mode(rules, a.identity)
    elif a.command == "range":
        if len(a.args) != 1 or ".." not in a.args[0]:
            print("privacy_scan: range needs BASE..HEAD", file=sys.stderr)
            return 2
        problems = scan_commits(rules, git("rev-list", a.args[0]).split(), a.identity)
    elif a.command == "text":
        problems = [f"stdin: private keyword #{n}" for n in rules.hits(sys.stdin.read())]
    else:
        if not a.args:
            print("privacy_scan: files needs at least one path", file=sys.stderr)
            return 2
        problems = [p for arg in a.args for p in scan_path(rules, Path(arg))]

    for p in problems:
        print("PRIVACY:", p, file=sys.stderr)
    if problems:
        print(f"privacy_scan: {len(problems)} problem(s); nothing may be published until they are fixed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
