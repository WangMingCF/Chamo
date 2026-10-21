"""The release-side guard for the documents that ship inside an artifact.

A USER_GUIDE titled with an older version must not ship next to newer release
notes. The guard has to be fail-closed (a missing copy is a failure,
not a skip) and it has to look at the packaged copies too, so these tests drive the
public script against synthetic trees rather than importing its helpers.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_docs_version.py"


def make_tree(root: Path, version: str = "9.9.9", guide: str | None = None,
              notes: str | None = None) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "config/release.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    guide_first = guide if guide is not None else "Chamo %s 使用说明" % version
    notes_first = notes if notes is not None else "Chamo %s — 测试版" % version
    (root / "docs/USER_GUIDE.txt").write_text(guide_first + "\n\n正文\n", encoding="utf-8")
    (root / "docs/RELEASE_NOTES.txt").write_text(notes_first + "\n\n正文\n", encoding="utf-8")
    return root


def run(*args: str):
    result = subprocess.run([sys.executable, str(SCRIPT), *args],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    return result.returncode, result.stdout + result.stderr


def test_matching_documents_pass(tmp_path):
    make_tree(tmp_path)
    code, output = run("--root", str(tmp_path))
    assert code == 0, output
    assert "DOCS VERSION: PASS" in output


def test_stale_user_guide_fails(tmp_path):
    # The regression this guards: an older-titled guide in a newer release.
    make_tree(tmp_path, version="0.2.0", guide="Chamo 0.1.0 使用说明")
    code, output = run("--root", str(tmp_path))
    assert code == 1, output
    assert "FAIL repo USER_GUIDE first line" in output


def test_stale_release_notes_fail(tmp_path):
    make_tree(tmp_path, version="0.2.0", notes="Chamo 0.1.0 — 旧说明")
    code, output = run("--root", str(tmp_path))
    assert code == 1, output
    assert "FAIL repo RELEASE_NOTES first line" in output


def test_missing_guide_is_a_failure_not_a_skip(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "docs/USER_GUIDE.txt").unlink()
    code, output = run("--root", str(tmp_path))
    assert code == 1, output
    assert "missing or unreadable" in output


def test_packaged_copies_are_checked(tmp_path):
    make_tree(tmp_path)
    packaged = tmp_path / "artifact"
    packaged.mkdir()
    (packaged / "USER_GUIDE.txt").write_text("Chamo 9.9.8 使用说明\n", encoding="utf-8")
    (packaged / "RELEASE_NOTES.txt").write_text("Chamo 9.9.9 — 测试版\n", encoding="utf-8")
    code, output = run("--root", str(tmp_path),
                       "--packaged-guide", str(packaged / "USER_GUIDE.txt"),
                       "--packaged-notes", str(packaged / "RELEASE_NOTES.txt"))
    assert code == 1, output
    assert "FAIL packaged USER_GUIDE first line" in output

    (packaged / "USER_GUIDE.txt").write_text("Chamo 9.9.9 使用说明\n", encoding="utf-8")
    code, output = run("--root", str(tmp_path),
                       "--packaged-guide", str(packaged / "USER_GUIDE.txt"),
                       "--packaged-notes", str(packaged / "RELEASE_NOTES.txt"))
    assert code == 0, output
    assert "packaged USER_GUIDE first line" in output


def test_explicit_version_overrides_config(tmp_path):
    make_tree(tmp_path, version="9.9.8")
    code, output = run("--root", str(tmp_path), "--version", "9.9.8")
    assert code == 0, output
    code, output = run("--root", str(tmp_path), "--version", "9.9.9")
    assert code == 1, output


def test_github_release_body_is_checked_when_given(tmp_path):
    # A release can go out with the previous release's GitHub body; the publish
    # preflight asks for this file too, so a stale draft stops the release.
    make_tree(tmp_path)
    body = tmp_path / "artifacts" / "github-release-notes.md"
    body.parent.mkdir(parents=True)
    body.write_text("Chamo 9.9.8 — 上一版\n", encoding="utf-8")
    code, output = run("--root", str(tmp_path), "--release-notes", str(body))
    assert code == 1, output
    assert "FAIL github release notes first line" in output

    body.write_text("Chamo 9.9.9 — 本版\n", encoding="utf-8")
    code, output = run("--root", str(tmp_path), "--release-notes", str(body))
    assert code == 0, output
    assert "github release notes first line" in output


def test_repo_documents_match_the_configured_version():
    # Live guard: a version bump without touching the shipped documents fails here,
    # so the check cannot live only in the release scripts that nobody runs locally.
    code, output = run()
    assert code == 0, output
