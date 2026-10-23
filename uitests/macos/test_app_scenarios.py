"""End-to-end scenarios against the real macOS app (debug build).

Each test writes a JSON scenario, launches build/dev/Chamo.app (or $CHAMO_DEV_APP)
with CHAMO_SCENARIO, and then checks what the app reported (summary JSON), what it
wrote to disk, and that the originals are untouched. Build the app first:

    python scripts/build_dev_app.py --out build/dev
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import visual  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
APP = Path(os.environ.get("CHAMO_DEV_APP", ROOT / "build" / "dev" / "Chamo.app"))
BUNDLE_ID = "io.github.wangmingcf.chamo.uitest"
# Fixed paths, not random temporary folders: paths show up in the window, and
# screenshots must not change from one run to the next.
WORK = Path("/tmp/chamo-uitest")

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS app scenarios")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def work():
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "in").mkdir(parents=True)
    (WORK / "out").mkdir()
    yield WORK


def make_pdf(path: Path, pages: int) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for n in range(pages):
        c.setFont("Helvetica", 28)
        c.drawString(60, 760, f"Chamo UI test page {n + 1}")
        c.rect(60, 420, 300, 220)
        c.showPage()
    c.save()
    return path


def make_png(path: Path, color) -> Path:
    Image.new("RGB", (320, 200), color).save(path)
    return path


def run(work: Path, name: str, steps: list[dict], timeout: int = 300) -> Path:
    assert APP.is_dir(), f"{APP} is missing; run scripts/build_dev_app.py first"
    out = work / "scenario" / name
    out.mkdir(parents=True)
    script = out / "steps.json"
    script.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
    # Start from the same preferences every time; never touch the real app's domain.
    subprocess.run(["defaults", "delete", BUNDLE_ID], capture_output=True)
    env = {**os.environ, "CHAMO_SCENARIO": str(script), "CHAMO_SCENARIO_OUT": str(out),
           "CHAMO_APP_SUPPORT": str(work / "support"), "CHAMO_STATE_DIR": str(work / "state"),
           "CHAMO_PAGE_CACHE": "0"}
    proc = subprocess.run([str(APP / "Contents" / "MacOS" / "Chamo"),
                           "-whatsNewSeen", "9999", "-ApplePersistenceIgnoreState", "YES"],
                          env=env, capture_output=True, text=True, timeout=timeout)
    result = json.loads((out / "result.json").read_text(encoding="utf-8")) if (out / "result.json").exists() else {}
    assert proc.returncode == 0 and result.get("ok"), (
        f"scenario {name} failed: rc={proc.returncode} result={result}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    for png in out.glob("*.png"):
        visual.keep(png, f"{name}-{png.stem}")
    return out


def summary(out: Path, name: str) -> dict:
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


def test_home_screen_matches_baseline(work):
    out = run(work, "home", [{"route": "home"}, {"appearance": "light"}, {"sleep": 0.5}, {"snapshot": "light"},
                             {"appearance": "dark"}, {"sleep": 0.5}, {"snapshot": "dark"}])
    visual.compare(out / "light.png", "home-light")
    visual.compare(out / "dark.png", "home-dark")


def test_pdf_to_images_end_to_end(work):
    pdf = make_pdf(work / "in" / "讲义.pdf", 3)
    before = sha(pdf)
    out = run(work, "pdf-images", [
        {"tool": "pdf_images"}, {"output": str(work / "out")}, {"set": {"format": "png", "dpi": 72}},
        {"add": [str(pdf)]}, {"sleep": 4}, {"snapshot": "ready"}, {"start": True}, {"wait": "finished", "timeout": 120},
        {"summary": "done"}, {"snapshot": "done"}])
    s = summary(out, "done")
    assert [i["status"] for i in s["items"]] == ["done"], s
    outputs = [Path(p) for p in s["items"][0]["outputs"]]
    pngs = sorted(p for p in outputs if p.suffix == ".png")
    assert len(pngs) == 3, outputs
    for p in pngs:
        assert p.is_file() and p.resolve().is_relative_to((work / "out").resolve())
        with Image.open(p) as im:
            im.verify()
    assert sha(pdf) == before, "the original PDF changed"
    visual.compare(out / "ready.png", "pdf-images-ready")


def test_the_window_at_1024_keeps_every_column_readable(work):
    """At 1024 points wide (the runner's width) no column is cut off.

    What this proves: no horizontal scrollbar at 1024 and the "大小" column shown in
    full. The sample file name is long on purpose - a short one would fit even with
    a squeezed name column, and the point is that a real document name still leaves
    room for the size column. "状态" shows the queued label; "结果" is empty because
    the run is not started, so a long result text is not exercised (see the PR's
    未验证 section).
    """
    pdf = make_pdf(work / "in" / "2024年度第三季度财务报表与附录完整版-最终确认稿.pdf", 3)
    out = run(work, "narrow", [
        {"tool": "pdf_images"}, {"output": str(work / "out")},
        {"set": {"format": "png", "dpi": 150}},
        {"add": [str(pdf)]},
        {"resize": {"width": 1024, "height": 700}},
        # The "已添加" toast fades out after a few seconds; a baseline that
        # contains it would differ between runs (uitests/README.md says so).
        {"sleep": 5}, {"snapshot": "narrow-window"}])
    shot = out / "narrow-window.png"
    assert shot.is_file(), "the scenario produced no screenshot"
    # The baseline folder already carries the platform; the name stays unprefixed
    # like the other scenarios (home-light, pdf-images-ready).
    visual.compare(shot, "narrow-window")


def test_a_broken_file_fails_alone_and_says_why(work):
    good = make_pdf(work / "in" / "good.pdf", 1)
    bad = work / "in" / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    hashes = {p: sha(p) for p in (good, bad)}
    out = run(work, "broken", [
        {"tool": "pdf_images"}, {"output": str(work / "out")}, {"set": {"format": "png", "dpi": 72}},
        {"add": [str(good), str(bad)]}, {"start": True}, {"wait": "finished", "timeout": 120},
        {"summary": "done"}, {"snapshot": "done"}])
    items = {i["name"]: i for i in summary(out, "done")["items"]}
    assert items["good.pdf"]["status"] == "done", items
    assert items["broken.pdf"]["status"] == "failed" and items["broken.pdf"]["detail"].strip(), items
    assert not items["broken.pdf"]["outputs"]
    for p, h in hashes.items():
        assert sha(p) == h, f"{p.name} changed"


def test_image_convert_png_to_jpeg(work):
    srcs = [make_png(work / "in" / f"图{n}.png", c) for n, c in enumerate([(200, 40, 40), (40, 160, 90)], 1)]
    out = run(work, "image-convert", [
        {"tool": "image_convert"}, {"output": str(work / "out")}, {"set": {"format": "jpeg", "quality": 85}},
        {"add": [str(p) for p in srcs]}, {"start": True}, {"wait": "finished", "timeout": 120}, {"summary": "done"}])
    items = summary(out, "done")["items"]
    assert [i["status"] for i in items] == ["done", "done"], items
    for item in items:
        jpgs = [Path(p) for p in item["outputs"] if Path(p).suffix.lower() in (".jpg", ".jpeg")]
        assert len(jpgs) == 1 and jpgs[0].is_file(), item
        with Image.open(jpgs[0]) as im:
            assert im.format == "JPEG" and im.size == (320, 200)


def test_cancel_leaves_the_output_folder_clean(work):
    pdf = make_pdf(work / "in" / "长文档.pdf", 300)
    before = sha(pdf)
    out = run(work, "cancel", [
        {"tool": "pdf_images"}, {"output": str(work / "out")}, {"set": {"format": "png", "dpi": 200}},
        {"add": [str(pdf)]}, {"start": True}, {"sleep": 1.5}, {"cancel": True},
        {"wait": "finished", "timeout": 120}, {"summary": "done"}])
    items = summary(out, "done")["items"]
    # Cancelling is not a failure: the file that was in flight must be reported as
    # cancelled, so the window shows 已取消 and the failure count stays at zero.
    # (The engine used to write "failed" plus the cancellation text here;
    # tests/test_cancel_item_state.py locks the mapping that made that happen.)
    assert items[0]["status"] == "cancelled", (
        f"the file in flight was not reported as cancelled: {items}")
    leftovers = sorted(str(p.relative_to(work / "out")) for p in (work / "out").rglob("*"))
    assert leftovers == [], f"a cancelled run left files in the output folder: {leftovers}"
    assert sha(pdf) == before
