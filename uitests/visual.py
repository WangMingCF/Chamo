"""Screenshot comparison against per-platform baselines.

Baselines live in uitests/baselines/<platform>/<name>.png and are only ever
replaced by a person (scripts/ui_baselines.sh), never by a test run. Every
screenshot, and a diff image on failure, is copied to the artifacts folder
($CHAMO_UI_ARTIFACTS, default build/ui-artifacts) so CI uploads it.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
BASELINES = Path(__file__).resolve().parent / "baselines"


def platform_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def artifacts() -> Path:
    path = Path(os.environ.get("CHAMO_UI_ARTIFACTS", ROOT / "build" / "ui-artifacts"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def keep(image: Path, name: str, folder: str = "kept") -> Path:
    """Copy a screenshot to the artifacts folder. "kept" ones are for people to
    look at; "compared" ones are what scripts/ui_baselines.sh turns into baselines."""
    target = artifacts() / folder / platform_name() / f"{name}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, target)
    return target


def compare(image: Path, name: str, *, max_ratio: float = 0.005, channel_threshold: int = 32) -> None:
    """Fail unless image matches the baseline: at most max_ratio of the pixels may
    differ by more than channel_threshold in any channel (absorbs antialiasing)."""
    kept = keep(image, name, "compared")
    baseline = BASELINES / platform_name() / f"{name}.png"
    if not baseline.is_file():
        raise AssertionError(f"no baseline {baseline.relative_to(ROOT)}; review {kept} and add it with scripts/ui_baselines.sh")
    actual, expected = Image.open(image).convert("RGB"), Image.open(baseline).convert("RGB")
    if actual.size != expected.size:
        raise AssertionError(f"{name}: size {actual.size} differs from baseline {expected.size}")
    diff = ImageChops.difference(actual, expected)
    mask = diff.point(lambda v: 255 if v > channel_threshold else 0).convert("L")
    changed = sum(1 for v in mask.getdata() if v)
    ratio = changed / (actual.size[0] * actual.size[1])
    if ratio > max_ratio:
        diff_path = artifacts() / "diff" / platform_name() / f"{name}.png"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        Image.composite(Image.new("RGB", actual.size, (255, 0, 0)), actual, mask).save(diff_path)
        raise AssertionError(f"{name}: {ratio:.2%} of pixels differ from the baseline (allowed {max_ratio:.2%}); see {diff_path}")
