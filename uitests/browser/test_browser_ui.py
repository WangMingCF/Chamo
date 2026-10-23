"""End-to-end tests of the local browser UI ("chamo ui") with a real browser.

Playwright drives headless Chromium against the page the way a user does: browse
to a folder, add files, pick a conversion, start, and wait for the result. The
assertions are on what lands on disk, not only on the page text.

The server command defaults to this checkout's engine; set CHAMO_UI_COMMAND (a
JSON list) to test an installed package instead, e.g. ["/usr/bin/chamo", "ui",
"--no-browser"].
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import visual  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
URL_LINE = re.compile(r"(http://127\.0\.0\.1:\d+/\?token=[\w-]+)")
DONE = "完成："


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def server_command() -> list[str]:
    custom = os.environ.get("CHAMO_UI_COMMAND")
    if custom:
        return json.loads(custom)
    return [sys.executable, str(ROOT / "engine" / "chamo.py"), "ui", "--no-browser"]


@pytest.fixture
def work():
    # A fixed name inside the temp folder keeps paths short; it is emptied each test.
    path = Path(tempfile.gettempdir()) / "chamo-browser-uitest"
    shutil.rmtree(path, ignore_errors=True)
    (path / "in").mkdir(parents=True)
    (path / "out").mkdir()
    yield path


@pytest.fixture
def server(work):
    env = {**os.environ, "CHAMO_STATE_DIR": str(work / "state"), "CHAMO_PAGE_CACHE": "0",
           "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(server_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, text=True, encoding="utf-8")
    found: dict = {}
    seen: list[str] = []

    def read():
        for line in proc.stdout:
            seen.append(line)
            m = URL_LINE.search(line)
            if m and "url" not in found:
                found["url"] = m.group(1)
                ready.set()

    ready = threading.Event()
    threading.Thread(target=read, daemon=True).start()
    if not ready.wait(60):
        proc.kill()
        pytest.fail("the UI server printed no URL within 60 s:\n" + "".join(seen))
    yield found["url"]
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page(server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1200, "height": 900}, device_scale_factor=1,
                                      locale="zh-CN", color_scheme="light")
        page = context.new_page()
        page.goto(server)
        expect(page.locator("#operation")).to_be_visible()
        yield page
        browser.close()


def make_pdf(path: Path, pages: int) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for n in range(pages):
        c.setFont("Helvetica", 28)
        c.drawString(60, 760, f"Chamo browser test page {n + 1}")
        c.showPage()
    c.save()
    return path


def add_files(page, folder: Path, names: list[str]) -> None:
    page.fill("#path", str(folder))
    page.click("#go")
    for name in names:
        page.locator("#entries li", has_text=name).get_by_role("button", name="加入").click()
    expect(page.locator("#count")).to_have_text(str(len(names)))


def run_conversion(page, out: Path, operation: str, timeout_s: int = 180) -> str:
    page.select_option("#operation", operation)
    page.fill("#output", str(out))
    page.click("#start")
    expect(page.locator("#status")).to_contain_text(DONE, timeout=timeout_s * 1000)
    return page.locator("#status").inner_text()


def products(out: Path, suffix: str) -> list[Path]:
    return sorted(p for p in out.rglob(f"*{suffix}") if p.is_file())


def test_initial_page_matches_baseline(page, work):
    # The page opens on the home folder, whose listing differs per machine and
    # shifts everything below it; browse to an empty folder first, take a
    # fixed-size viewport shot, and mask the fields that still hold paths.
    page.fill("#path", str(work / "in"))
    page.click("#go")
    expect(page.locator("#entries li")).to_have_count(1)  # only the "parent folder" row
    shot = work / "initial.png"
    page.screenshot(path=str(shot), mask=[page.locator(s) for s in ("#path", "#output", "#status")])
    visual.compare(shot, "browser-initial")


def test_pdf_to_images_end_to_end(page, work):
    pdf = make_pdf(work / "in" / "讲义.pdf", 3)
    before = sha(pdf)
    add_files(page, work / "in", ["讲义.pdf"])
    page.fill("#dpi", "72")
    page.select_option("#format", "png")
    status = run_conversion(page, work / "out", "pdf_images")
    assert "成功 1 / 异常 0" in status, status
    pngs = products(work / "out", ".png")
    assert len(pngs) == 3, pngs
    for p in pngs:
        with Image.open(p) as im:
            im.verify()
    assert sha(pdf) == before, "the original PDF changed"
    shot = work / "done.png"
    page.screenshot(path=str(shot), full_page=True)
    visual.keep(shot, "browser-pdf-images-done")


def test_a_broken_file_fails_alone(page, work):
    make_pdf(work / "in" / "good.pdf", 1)
    bad = work / "in" / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    before = sha(bad)
    add_files(page, work / "in", ["good.pdf", "broken.pdf"])
    page.fill("#dpi", "72")
    status = run_conversion(page, work / "out", "pdf_images")
    assert "成功 1 / 异常 1" in status, status
    assert all("broken" not in p.name for p in products(work / "out", ".png"))
    assert sha(bad) == before


def test_cancelling_shows_cancelled_not_failed(page, work):
    """Stopping a run is not an error: the page must say 已取消, not 失败.

    A 300-page PDF at 200 dpi takes long enough that the cancel lands while a file
    is being worked on, which is exactly the case that used to show up as a red
    failure in both the window and this page.
    """
    pdf = make_pdf(work / "in" / "长文档.pdf", 300)
    before = sha(pdf)
    add_files(page, work / "in", ["长文档.pdf"])
    page.fill("#dpi", "200")
    page.select_option("#format", "png")
    page.fill("#output", str(work / "out"))
    page.click("#start")
    # Wait for the page to report the file is being worked on instead of sleeping
    # a fixed time: a fixed 2 s was enough for the whole 300-page run to finish on
    # a fast runner, and then "cancel" arrived after completion - a red test that
    # says nothing about cancellation. An item or progress line proves it is in
    # flight; if the run does finish first, the 已取消 expectation below fails
    # loudly with the actual status text.
    expect(page.locator("#status")).to_contain_text(re.compile(r"\[\d+/\d+\]|进度"),
                                                    timeout=120_000)
    page.click("#cancel")
    expect(page.locator("#status")).to_contain_text("已取消", timeout=180_000)
    status = page.locator("#status").inner_text()
    assert "失败" not in status and "异常 1" not in status, status
    leftovers = sorted(str(p.relative_to(work / "out")) for p in (work / "out").rglob("*"))
    assert leftovers == [], f"a cancelled run left files in the output folder: {leftovers}"
    assert sha(pdf) == before, "the original PDF changed"


def test_lossless_image_compression_keeps_every_pixel(page, work):
    src = work / "in" / "图.png"
    Image.new("RGB", (300, 200), (30, 120, 200)).save(src, compress_level=0)
    add_files(page, work / "in", ["图.png"])
    page.select_option("#operation", "image_compress")
    expect(page.locator("#imageCompressBox")).to_be_visible()
    page.select_option("#imageCompressMode", "lossless")
    status = run_conversion(page, work / "out", "image_compress")
    assert "成功 1 / 异常 0" in status, status
    [out] = products(work / "out", ".png")
    with Image.open(src) as a, Image.open(out) as b:
        assert ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None
    assert out.stat().st_size < src.stat().st_size


def test_api_refuses_requests_without_the_token(page, server):
    base = server.split("/?")[0]
    assert page.request.get(base + "/api/status").status == 403
    assert page.request.post(base + "/api/start", data="{}").status == 403
