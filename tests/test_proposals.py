"""Synthetic catalog checks; no Git remote, product files, or user data needed."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_proposals.py"
SPEC = importlib.util.spec_from_file_location("check_proposals", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def proposal(key="sample", **changes):
    data = {
        "schema_version": 1, "id": key, "title": "Synthetic feature",
        "feature_key": key, "status": "proposed", "owner": "agent-test",
        "platforms": ["linux", "windows", "macos"], "write_scopes": [f"engine/{key}.py"],
        "dependencies": [], "branch": "", "base_sha": "",
        "acceptance": [{"id": "A1", "description": "Preserve originals", "tests": []}],
        "coverage_gaps": ["Review must add remaining failure-path evidence"],
    }
    data.update(changes)
    return data


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chamo-proposals-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / "proposals"
        self.catalog.mkdir()

    def put(self, key="sample", **changes):
        directory = self.catalog / key
        directory.mkdir(exist_ok=True)
        data = proposal(key, **changes)
        (directory / "proposal.json").write_text(json.dumps(data), encoding="utf-8")
        for name in ("PR.md", "PRD.md"):
            (directory / name).write_text("# Synthetic requirement\n", encoding="utf-8")
        return directory

    def errors(self):
        return "\n".join(validator.check_catalog(self.root))

    def test_valid_proposed_catalog_excludes_template_and_index(self):
        self.put("20261022-sample")
        (self.catalog / "_template").mkdir()
        (self.catalog / "_template" / "proposal.json").write_text("not JSON", encoding="utf-8")
        (self.catalog / "INDEX.md").write_text("Human index, not state", encoding="utf-8")
        self.assertEqual(validator.check_catalog(self.root), [])

    def test_empty_missing_and_template_only_catalog_fail(self):
        self.assertIn("at least one", self.errors())
        (self.catalog / "_template").mkdir()
        self.assertIn("at least one", self.errors())
        self.assertIn("must exist", " ".join(validator.check_catalog(self.root / "missing")))

    def test_each_required_file_must_exist_and_markdown_must_not_be_empty(self):
        for name in ("PR.md", "PRD.md", "proposal.json"):
            with self.subTest(name=name):
                directory = self.put()
                (directory / name).unlink()
                self.assertIn(name + " must be a regular file", self.errors())
        for name in ("PR.md", "PRD.md"):
            directory = self.put()
            (directory / name).write_text(" \n", encoding="utf-8")
            self.assertIn(name + " must not be empty", self.errors())

    def test_malformed_duplicate_key_and_nonobject_json_fail(self):
        directory = self.put()
        for raw in ("{", '{"id": "a", "id": "b"}', "[]", "null"):
            with self.subTest(raw=raw):
                (directory / "proposal.json").write_text(raw, encoding="utf-8")
                self.assertTrue(validator.check_catalog(self.root))
        (directory / "proposal.json").write_bytes(b"\xff")
        self.assertIn("invalid proposal.json", self.errors())

    def test_all_required_fields_are_required(self):
        for key in validator.REQUIRED:
            with self.subTest(key=key):
                data = proposal()
                del data[key]
                self.assertIn("missing fields", " ".join(validator.validate_proposal(data, "sample")))

    def test_invalid_types_never_crash(self):
        for key in validator.REQUIRED:
            for value in (None, {}, [[]], False, 7):
                with self.subTest(key=key, value=value):
                    data = proposal(**{key: value})
                    self.assertTrue(validator.validate_proposal(data, "sample"))

    def test_specific_field_constraints(self):
        invalid = {
            "schema_version": [True, 1.0, 2], "id": ["wrong", "Bad", "../sample"],
            "feature_key": ["HasCaps", "has_underscore", "sample\n"],
            "owner": ["agent@example.invalid", "Agent Test"], "title": ["", "  "],
            "status": ["done", ""], "platforms": [[], ["linux", "linux"], ["android"]],
            "dependencies": [["Bad"], ["a", "a"]], "write_scopes": [[], ["a", "a"]],
            "coverage_gaps": [[""], ["gap", "gap"]], "acceptance": [[], ["A1"], [{}]],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertTrue(validator.validate_proposal(proposal(**{key: value}), "sample"))

    def test_acceptance_requires_ids_descriptions_and_test_lists(self):
        for item in ({"id": "A1", "description": "x"},
                     {"id": "A1", "description": "x", "tests": "tests/test_x.py"},
                     {"id": "A1", "description": "x", "tests": [""]}):
            self.assertTrue(validator.validate_proposal(proposal(acceptance=[item]), "sample"))
        item = {"id": "A1", "description": "x", "tests": ["tests/test_x.py::test_x"]}
        self.assertEqual(validator.validate_proposal(proposal(acceptance=[item]), "sample"), [])
        self.assertIn("acceptance ids", " ".join(validator.validate_proposal(proposal(acceptance=[item, item]), "sample")))

    def test_safe_scope_and_casefold_boundary_overlap(self):
        for value in ("engine", "engine/", "engine/a.py", ".github/workflows", "Sources/App.swift"):
            self.assertTrue(validator.safe_scope(value), value)
        for value in ("", "/", "/engine", "../engine", "engine/../tests", "engine//a", "./engine",
                      "engine/*", "engine/[ab]", "C:/engine", "engine\\a", "~/engine",
                      "engine\n", "engine/a b", "engine//", "engine/./x"):
            self.assertFalse(validator.safe_scope(value), value)
        for a, b in (("engine/", "ENGINE/a.py"), ("engine/a.py", "engine"), ("engine", "engine")):
            self.assertTrue(validator.scopes_overlap(a, b))
        self.assertFalse(validator.scopes_overlap("engine/a.py", "engine/ab.py"))
        self.assertFalse(validator.scopes_overlap("engine", "engine-more"))

    def test_claim_protocol_limits_and_windows_aliases(self):
        for value in (".git", ".GIT/config", "engine/.git/config", "engine./x", "engine/x.", "a" * 513):
            self.assertFalse(validator.safe_scope(value), value)
        self.assertTrue(validator.safe_scope("a" * 512))
        for scopes in (["engine", "ENGINE/"], ["engine", "engine/"], [f"p{i}" for i in range(129)]):
            self.assertTrue(validator.validate_proposal(proposal(write_scopes=scopes), "sample"))
        self.assertEqual(validator.validate_proposal(proposal(write_scopes=[f"p{i}" for i in range(128)]), "sample"), [])
        for key in ("feature_key", "owner"):
            self.assertEqual(validator.validate_proposal(proposal(**{key: "a" * 128}), "sample"), [])
            self.assertTrue(validator.validate_proposal(proposal(**{key: "a" * 129}), "sample"))
        for branch in ("main", "master", "swarm/registry", "refs/heads/feature", "feat/非ASCII", "feat/x."):
            self.assertFalse(validator.safe_branch(branch), branch)

    def test_branch_and_base_are_required_after_claim(self):
        for status in validator.ACTIVE | {"merged"}:
            self.assertTrue(validator.validate_proposal(proposal(status=status), "sample"))
            self.assertEqual(validator.validate_proposal(proposal(status=status, branch="feat/sample", base_sha="a" * 40), "sample"), [])
        self.assertEqual(validator.validate_proposal(proposal(status="abandoned"), "sample"), [])
        self.assertEqual(validator.validate_proposal(proposal(base_sha="b" * 64), "sample"), [])
        for value in ("a" * 39, "A" * 40, "g" * 40):
            self.assertTrue(validator.validate_proposal(proposal(base_sha=value), "sample"))
        for value in ("HEAD", "@", "-x", "a..b", "a@{1}", "a.lock", "a/.hidden", "a//b", "a/", "a.", "a b", "a:b", "a~b"):
            self.assertFalse(validator.safe_branch(value), value)

    def test_missing_dependency_self_cycle_and_disconnected_cycle(self):
        self.put(dependencies=["missing"])
        self.assertIn("does not exist", self.errors())
        self.put(dependencies=["sample"])
        self.assertIn("dependency cycle", self.errors())
        self.put(dependencies=[])
        self.put("cycle-a", dependencies=["cycle-b"])
        self.put("cycle-b", dependencies=["cycle-a"])
        self.assertIn("dependency cycle", self.errors())

    def test_dependency_chain_is_valid(self):
        self.put("a")
        self.put("b", dependencies=["a"])
        self.put("c", dependencies=["b", "a"])
        self.assertEqual(validator.check_catalog(self.root), [])

    def test_duplicate_open_features_rejected_even_when_proposed(self):
        self.put("a", feature_key="same")
        self.put("b", feature_key="same")
        self.assertIn("duplicate open feature_key", self.errors())
        self.put("b", feature_key="same", status="abandoned")
        self.assertEqual(validator.check_catalog(self.root), [])

    def test_active_scope_conflicts_include_blocked_but_not_proposed(self):
        self.put("a", status="developing", branch="feat/a", base_sha="a" * 40, write_scopes=["engine/"])
        for status in validator.ACTIVE:
            self.put("b", status=status, branch="feat/b", base_sha="b" * 40, write_scopes=["ENGINE/a.py"])
            self.assertIn("active write_scopes overlap", self.errors())
        for status in ("proposed", "merged", "abandoned"):
            self.put("b", status=status, branch="feat/b", base_sha="b" * 40, write_scopes=["engine/a.py"])
            self.assertEqual(validator.check_catalog(self.root), [])

    def test_symlink_metadata_never_read_even_without_windows_symlink_privilege(self):
        directory = self.put()
        # Exercise the same filesystem flag on every OS, without a privileged
        # Windows symlink operation or a skip that would weaken the CI skip gate.
        for target in (self.catalog, directory, directory / "proposal.json", directory / "PR.md", directory / "PRD.md"):
            with self.subTest(target=target), patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == target):
                self.assertIn("symlink", self.errors())

    def test_unknown_files_and_invalid_directory_names_fail(self):
        self.put()
        (self.catalog / "rogue.json").write_text("{}", encoding="utf-8")
        self.assertIn("unexpected file", self.errors())
        self.put("Bad")
        self.assertIn("directory name", self.errors())

    def test_markdown_decode_error_and_catalog_read_error_fail_closed(self):
        directory = self.put()
        (directory / "PR.md").write_bytes(b"\xff")
        self.assertIn("cannot read PR.md", self.errors())
        with patch.object(validator, "check_catalog", side_effect=OSError("unreadable")), redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(validator.main(["--root", str(self.root)]), 1)
            self.assertIn("unreadable", stderr.getvalue())

    def test_cli_exit_codes_and_no_lock_claim(self):
        bad = subprocess.run([sys.executable, "-B", str(SCRIPT), "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ERROR:", bad.stderr)
        self.put()
        good = subprocess.run([sys.executable, "-B", str(SCRIPT), "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn("not a remote ownership lock", good.stdout)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(validator.main(["--root", str(self.root)]), 0)


if __name__ == "__main__":
    unittest.main()
