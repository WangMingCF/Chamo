"""Restore third_party/LibreOffice.app from the pinned DMG when its seal breaks.

LibreOffice writes __pycache__ next to its bundled UNO modules whenever it runs
from a directory, which invalidates The Document Foundation signature of that
copy. The checked-in copy is a build input, so it must stay pristine; this
restorer re-extracts it from the SHA-256 verified DMG instead of guessing which
caches were originally shipped (TDF ships some itself, so deleting them all is
wrong and breaks the seal just as badly).
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DMG = ROOT / "third_party/downloads/LibreOffice_26.2.6_MacOS_aarch64.dmg"
TARGET = ROOT / "third_party/LibreOffice.app"


def sealed(bundle: Path) -> bool:
    if not bundle.exists():
        return False
    proc = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)],
                          capture_output=True, text=True)
    return proc.returncode == 0


def restore() -> bool:
    """Re-extract the pinned LibreOffice. Returns True when a restore happened."""
    if not DMG.is_file():
        sys.exit(f"cannot restore LibreOffice: {DMG} is missing")
    mount = Path(tempfile.mkdtemp(prefix="dw-lo-restore-")) / "mnt"
    mount.mkdir()
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly",
                    "-mountpoint", str(mount), str(DMG)],
                   check=True, capture_output=True)
    try:
        staging = mount / "LibreOffice.app"
        if not sealed(staging):
            sys.exit("pinned LibreOffice DMG does not contain a sealed app")
        if TARGET.exists():
            shutil.rmtree(TARGET)
        subprocess.run(["ditto", str(staging), str(TARGET)], check=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True)
    return True


def ensure_sealed() -> None:
    """Make third_party/LibreOffice.app pristine, restoring it when necessary."""
    if sealed(TARGET):
        return
    print("LibreOffice build input lost its vendor seal; restoring from the pinned DMG")
    restore()
    if not sealed(TARGET):
        sys.exit("restored LibreOffice still fails codesign verification")


if __name__ == "__main__":
    ensure_sealed()
    print("third_party/LibreOffice.app is sealed")
