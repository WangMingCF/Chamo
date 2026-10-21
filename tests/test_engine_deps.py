"""The packaged artifacts must carry every third-party import the engine uses.

build_linux.py and build_windows.py install exactly scripts/engine_deps.py's
RUNTIME list into the shipped venv / embeddable interpreter. A source-tree test
run imports whatever the developer's environment has, so a wheel missing from
RUNTIME stays green here while a freshly installed .deb or ZIP fails with
ImportError the first time the operation runs. That is how the pikepdf
dependency (pdf_compress lossless) could ship broken: the source tree
had pikepdf, the package would not have installed it.

These checks are fail-capable:
  * test_runtime_covers_every_third_party_import fails if an import is missing;
  * test_the_scan_can_fail_when_a_dependency_is_missing feeds the scanner a
    RUNTIME without pikepdf and requires it to name the miss, so a scanner that
    silently returned "nothing missing" cannot make the gate above pass.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ROOT / "scripts"))

import engine_deps  # noqa: E402

# Import name -> PyPI distribution, for the third-party modules the engine imports.
DISTRIBUTIONS = {
    "PIL": "pillow",
    "img2pdf": "img2pdf",
    "lxml": "lxml",
    "olefile": "olefile",
    "openpyxl": "openpyxl",
    "pikepdf": "pikepdf",
    "pypdf": "pypdf",
    "pypdfium2": "pypdfium2",
    "xlrd": "xlrd",
}
# Modules shipped inside engine/ (cloud_kb_pack is vendored here) are not wheels.
LOCAL = {path.stem for path in ENGINE.glob("*.py")} | {
    path.name for path in ENGINE.iterdir() if path.is_dir()}
STDLIB = set(sys.stdlib_module_names)


def runtime_distributions(runtime):
    """{normalised distribution: pinned version} for a RUNTIME-style list."""
    pins = {}
    for pin in runtime:
        name, _, version = pin.partition("==")
        pins[name.lower().replace("-", "_")] = version
    return pins


def third_party_imports(directory=ENGINE):
    """{top-level module: [files importing it]} for engine/*.py, stdlib/local removed.

    Lazy imports are included on purpose: pikepdf is imported inside the
    pdf_compress code path, and a lazy import fails in the packaged artifact just
    like a top-level one.
    """
    found = {}
    for path in sorted(Path(directory).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top in STDLIB or top in LOCAL or top == "__future__":
                    continue
                found.setdefault(top, set()).add(path.name)
    return found


def missing_dependencies(imports, runtime):
    """Human-readable list of imports the RUNTIME pins do not cover."""
    installed = runtime_distributions(runtime)
    missing = []
    for module, files in sorted(imports.items()):
        distribution = DISTRIBUTIONS.get(module)
        imported_by = ", ".join(sorted(files))
        if distribution is None:
            missing.append("%s (no distribution mapping; imported by %s)"
                           % (module, imported_by))
        elif distribution.lower().replace("-", "_") not in installed:
            missing.append("%s -> %s (not in RUNTIME; imported by %s)"
                           % (module, distribution, imported_by))
    return missing


def test_runtime_covers_every_third_party_import():
    imports = third_party_imports()
    assert "pikepdf" in imports, (
        "扫描器没有在 engine/*.py 里找到 pikepdf：pdf_compress 的无损档应 import pikepdf，"
        "扫描逻辑或该功能已偏离预期")
    missing = missing_dependencies(imports, engine_deps.RUNTIME)
    assert not missing, (
        "scripts/engine_deps.py 的 RUNTIME 没有覆盖引擎的第三方 import，"
        "build_linux.py / build_windows.py 装出的包会 ImportError：%r；RUNTIME=%r"
        % (missing, engine_deps.RUNTIME))


def test_pikepdf_is_a_runtime_dependency():
    """pdf_compress 无损档依赖 pikepdf；缺它是“本地绿、装包坏”的发布级缺口。"""
    pins = runtime_distributions(engine_deps.RUNTIME)
    assert "pikepdf" in pins, (
        "scripts/engine_deps.py 的 RUNTIME 缺少 pikepdf==...；"
        "装包后的 pdf_compress 无损档会 ImportError。当前 RUNTIME=%r"
        % engine_deps.RUNTIME)
    assert pins["pikepdf"], "pikepdf 在 RUNTIME 中必须带版本号（形如 pikepdf==10.17.0）"


def test_runtime_pins_match_the_requirements_lock():
    lock_text = (ROOT / "third_party" / "requirements.lock").read_text(encoding="utf-8")
    locked = {}
    for line in lock_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        locked[name.lower().replace("-", "_")] = version
    problems = []
    for pin in engine_deps.RUNTIME:
        name, _, version = pin.partition("==")
        key = name.lower().replace("-", "_")
        if key not in locked:
            problems.append("%s 不在 third_party/requirements.lock 里" % pin)
        elif locked[key] != version:
            problems.append("%s 与 requirements.lock 的 %s==%s 不一致"
                            % (pin, name, locked[key]))
    assert not problems, "RUNTIME 与 requirements.lock 漂移：%r" % problems


def test_the_scan_can_fail_when_a_dependency_is_missing():
    """Negative control: the gate must notice a RUNTIME that lost pikepdf."""
    imports = third_party_imports()
    without_pikepdf = [pin for pin in engine_deps.RUNTIME
                       if not pin.lower().startswith("pikepdf==")]
    missing = missing_dependencies(imports, without_pikepdf)
    assert any("pikepdf" in item for item in missing), (
        "负控制失败：故意去掉 pikepdf 的 RUNTIME 后扫描器没有报缺失，"
        "说明上面的依赖门可能永远绿；missing=%r" % missing)
    # Synthetic unknown third-party import: also caught instead of silently ignored.
    missing_unknown = missing_dependencies({"totally_new_module": {"workbench.py"}},
                                           engine_deps.RUNTIME)
    assert missing_unknown, "负控制失败：未知第三方 import 没有被报出"
