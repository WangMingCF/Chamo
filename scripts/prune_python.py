"""Remove parts of the bundled Python runtime that this product never uses.

Removed: build/test tooling (pip, pytest, pygments, ...), fixture-generation
packages (reportlab, python-pptx, XlsxWriter), GUI/IDE stdlib modules, headers
and console scripts for tools we do not ship.

NOT removed: packaging. pikepdf declares it as a runtime dependency and
pikepdf/jbig2.py imports it at module scope, so dropping it breaks a real
code path (JBIG2-encoded PDFs) even though the current engine never reaches it.

Verified with a benchmark that runs the engine and every conversion operation
against a pruned copy of the runtime.
"""
from pathlib import Path
import shutil

SITE_PACKAGES = ["pip", "_pytest", "pytest", "pygments", "iniconfig", "pluggy",
                 "reportlab", "pptx", "python_pptx", "xlsxwriter",
                 "charset_normalizer", "typing_extensions"]
STDLIB = ["idlelib", "ensurepip", "tkinter", "lib2to3", "pydoc_data",
          "turtledemo", "test", "distutils"]
BIN_SCRIPTS = ["pip", "pip3", "pip3.12", "pytest", "py.test", "pygmentize",
               "2to3", "2to3-3.12", "idle3", "idle3.12", "pydoc3", "pydoc3.12",
               "python3-config", "python3.12-config", "runxlrd.py", "vba_extract.py",
               "normalizer", "img2pdf-gui"]
# A directory whose absence proves pruning finished. Checking only the first
# removed directory would let an interrupted prune look complete forever.
TERMINAL_MARKER = "include"


def _size(target: Path) -> int:
    if target.is_dir() and not target.is_symlink():
        return sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    return target.stat().st_size if target.exists() else 0


def pending(runtime: Path) -> bool:
    """True when the runtime still contains prunable parts."""
    lib = runtime / "lib/python3.12"
    if any((lib / name).exists() for name in STDLIB):
        return True
    if (runtime / TERMINAL_MARKER).exists():
        return True
    # any() over the generators themselves is always true (a generator object is
    # truthy), so this asked "were any globs created", not "did any match", and
    # pending() answered True even for a fully pruned runtime. That in turn made
    # CHAMO_NO_PRUNE unable to ever accept a control build.
    if any(match for name in SITE_PACKAGES
           for match in (lib / "site-packages").glob(name + "*")):
        return True
    return any((runtime / "bin" / s).exists() for s in BIN_SCRIPTS)


def prune(runtime: Path) -> int:
    """Remove unused runtime parts; return megabytes reclaimed."""
    freed = 0
    lib = runtime / "lib/python3.12"
    for name in SITE_PACKAGES:
        for target in (lib / "site-packages").glob(name + "*"):
            freed += _size(target)
            shutil.rmtree(target) if target.is_dir() else target.unlink()
    for name in STDLIB:
        target = lib / name
        if target.exists():
            freed += _size(target)
            shutil.rmtree(target)
    for script in BIN_SCRIPTS:
        target = runtime / "bin" / script
        if target.exists() or target.is_symlink():
            freed += _size(target)
            target.unlink()
    inc = runtime / TERMINAL_MARKER
    if inc.exists():
        freed += _size(inc)
        shutil.rmtree(inc)
    return freed // (1024 * 1024)


if __name__ == "__main__":
    import sys
    print(prune(Path(sys.argv[1])), "MB reclaimed")
