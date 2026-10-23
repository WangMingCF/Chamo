"""Pruning LibreOffice has to be decided by what is actually in the bundle.

pending() gates the same two things as the Python runtime: a normal build prunes
while it is true, and a CHAMO_NO_PRUNE control build refuses a copy that is
already pruned (pending False), because a pruned copy cannot be un-pruned in
place. The old implementation asked any() over glob *generators*, which is always
true, so every copy looked prunable and the control refusal could never fire.

The last test drives the real caller: it reads build_app.py itself, finds the two
«elif not <module>.pending(...)» guards and evaluates their conditions in both
directions. Without it the guard could be inverted and these unit tests would
still pass.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prune_runtime = _load("chamo_prune_runtime", "scripts/prune_runtime.py")


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "LibreOffice.app"
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    return bundle


def test_an_untouched_looking_bundle_is_not_pending(tmp_path):
    """Nothing removable: the control build must accept this copy."""
    assert prune_runtime.pending(_bundle(tmp_path)) is False


def test_a_removable_directory_marks_the_bundle_as_pending(tmp_path):
    bundle = _bundle(tmp_path)
    (bundle / "Contents" / "Resources" / "help").mkdir()
    assert prune_runtime.pending(bundle) is True


def test_a_glob_only_leftover_marks_the_bundle_as_pending(tmp_path):
    """The glob list is the branch that used to be always true."""
    bundle = _bundle(tmp_path)
    config = bundle / "Contents" / "Resources" / "config"
    config.mkdir(parents=True)
    (config / "images_foo.zip").write_bytes(b"synthetic")
    assert prune_runtime.pending(bundle) is True


def test_pending_is_false_after_pruning(tmp_path):
    bundle = _bundle(tmp_path)
    (bundle / "Contents" / "Resources" / "help").mkdir()
    config = bundle / "Contents" / "Resources" / "config"
    config.mkdir(parents=True)
    (config / "images_foo.zip").write_bytes(b"synthetic")

    prune_runtime.prune(bundle)

    assert prune_runtime.pending(bundle) is False


def _control_guards() -> list:
    """The «elif not <module>.pending(...)» guards of the real build script."""
    tree = ast.parse((ROOT / "scripts" / "build_app.py").read_text(encoding="utf-8"))
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        call = test.operand
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "pending"):
            continue
        raises = [n for n in ast.walk(node) if isinstance(n, ast.Raise)]
        guards.append((ast.unparse(call), bool(raises)))
    return guards


def test_the_control_build_refuses_an_already_pruned_runtime():
    """The caller direction, read from build_app.py itself.

    pending() must be False *after* pruning, and the control build must refuse in
    exactly that case. Evaluating the guard's own condition keeps this test honest
    even if the implementation changes: it fails when the condition is inverted.
    """
    guards = _control_guards()
    assert len(guards) == 2, guards          # Python runtime and LibreOffice
    for expression, raises in guards:
        assert raises, expression            # a guard that does not refuse is not a guard
        for pending_value, expected in ((False, True), (True, False)):
            class _Stub:
                def __init__(self, value):
                    self.value = value

                def pending(self, _path):
                    return self.value

            namespace = {"prune_python": _Stub(pending_value),
                         "prune_runtime": _Stub(pending_value),
                         "res": Path("/tmp/chamo-guard"),
                         "helpers": Path("/tmp/chamo-guard")}
            fires = eval("not " + expression, namespace)  # noqa: S307 - our own expression
            assert fires is expected, (expression, pending_value, fires)


def test_stock_02_boundary_is_documented():
    """An existential pending() is not proof that the copy was never touched.

    STOCK-02: one leftover item still makes the copy pending, and a dangling
    symlink is not seen by exists() while prune() removes exists() or is_symlink().
    This test only pins the documented boundary so the gap is not silently claimed
    closed; it does not assert the behaviour is complete.
    """
    note = ROOT / ".development" / "verification" / "STOCK-02-boundary.md"
    assert note.is_file(), note
    text = note.read_text(encoding="utf-8")
    assert "owner_device=NAS-DSH" in text
    assert "existential" in text or "存在性" in text
