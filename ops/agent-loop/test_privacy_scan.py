"""Checks for privacy_scan.py. Run: python3 -m pytest ops/agent-loop -q"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCAN = Path(__file__).with_name("privacy_scan.py")
SECRET = "Zhang Synthetic"  # a made-up value standing in for a private identifier
ID = ["-c", "user.name=Chamo", "-c", "user.email=dev@chamo.invalid"]


def run(*args, stdin="", cwd=None, keywords=None):
    cmd = [sys.executable, str(SCAN), *args]
    if keywords:
        cmd += ["--keywords", str(keywords)]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=cwd)


def keywords(tmp: Path) -> Path:
    line = run("hash", stdin=SECRET + "\nExampleHandle\n").stdout
    path = tmp / "kw.txt"
    path.write_text("# test\n" + line + "allow:github\\.com/ExampleHandle/chamo\n", encoding="utf-8")
    return path


def repo(tmp: Path) -> Path:
    r = tmp / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    return r


def commit(r: Path, name: str, text: str, identity=ID):
    (r / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", *identity, "add", name], cwd=r, check=True)
    subprocess.run(["git", *identity, "commit", "-q", "-m", "c " + name], cwd=r, check=True)


def test_hash_output_never_contains_value(tmp_path):
    out = run("hash", stdin=SECRET).stdout
    assert SECRET.casefold() not in out.casefold() and out.startswith("sha256:")


def test_missing_keyword_file_fails_closed(tmp_path):
    assert run("text", stdin="hi", keywords=tmp_path / "none.txt").returncode == 2


def test_text_hit_is_case_insensitive_and_hides_value(tmp_path):
    p = run("text", stdin="by zhang synthetic.", keywords=keywords(tmp_path))
    assert p.returncode == 1 and "#1" in p.stderr and "zhang" not in p.stderr.casefold()


def test_allowed_context_passes_but_other_uses_fail(tmp_path):
    kw = keywords(tmp_path)
    assert run("text", stdin="see https://github.com/ExampleHandle/chamo", keywords=kw).returncode == 0
    assert run("text", stdin="written by ExampleHandle", keywords=kw).returncode == 1


def test_range_checks_identity_and_added_lines(tmp_path):
    kw, r = keywords(tmp_path), repo(tmp_path)
    commit(r, "a.txt", "clean\n")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()
    commit(r, "b.txt", "clean too\n")
    assert run("range", f"{base}..HEAD", cwd=r, keywords=kw).returncode == 0
    commit(r, "c.txt", "contact Zhang Synthetic\n")
    assert run("range", f"{base}..HEAD", cwd=r, keywords=kw).returncode == 1
    (tmp_path / "x").mkdir()
    r2 = repo(tmp_path / "x")
    commit(r2, "a.txt", "ok\n", identity=["-c", "user.name=Someone", "-c", "user.email=s@example.com"])
    commit(r2, "b.txt", "ok\n", identity=["-c", "user.name=Someone", "-c", "user.email=s@example.com"])
    p = run("range", "HEAD~1..HEAD", cwd=r2, keywords=kw)
    assert p.returncode == 1 and "not the repository identity" in p.stderr


def test_removing_a_value_is_allowed(tmp_path):
    kw, r = keywords(tmp_path), repo(tmp_path)
    commit(r, "a.txt", "Zhang Synthetic\n")
    commit(r, "a.txt", "removed\n")
    assert run("range", "HEAD~1..HEAD", cwd=r, keywords=kw).returncode == 0


def test_push_mode_new_branch(tmp_path):
    kw, r = keywords(tmp_path), repo(tmp_path)
    commit(r, "a.txt", "Zhang Synthetic\n")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()
    line = f"refs/heads/main {head} refs/heads/main {'0' * 40}\n"
    assert run("push", stdin=line, cwd=r, keywords=kw).returncode == 1


def test_files_scans_zip_members(tmp_path):
    import zipfile
    kw = keywords(tmp_path)
    z = tmp_path / "asset.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("doc/readme.txt", "built by zhang synthetic")
    p = run("files", str(z), keywords=kw)
    assert p.returncode == 1 and "readme.txt" in p.stderr
