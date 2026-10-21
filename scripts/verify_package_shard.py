#!/usr/bin/env python3
"""Acceptance for a PACKAGED build: does sharding actually work in the artifact?

The pytest gates run against the checkout, and the macOS DMG has its own script.
A .deb or a Windows ZIP adds failure modes those never touch: a pruned runtime
that lost a module the shard child imports, a launcher that cannot re-execute the
engine (`sys.executable -I -B engine/workbench.py --shard`), a path layout where
the shard result file cannot be written, or a content classifier whose pdfium
wheel behaves differently on that platform. A green build is not evidence that
any of that works in the shipped bytes, so this runs the packaged launcher.

It is deliberately runnable on Linux, Windows and macOS from any interpreter that
has Pillow, because the same file must serve every packaging job.

What it proves:
  * the shipped launcher renders a 96-page A5 scan (~104 MPix) with shard=auto
    and shard=off;
  * both arms publish the same relative paths with identical SHA-256 (a sharded
    run that loses or re-renders a page cannot pass);
  * the auto arm really took the sharded path, and the off arm really did not -
    otherwise a "sharding works" claim would be vacuous.

Wall times are printed but NOT gated: a shared CI runner cannot support a speed
threshold. The gated speed numbers come from scripts/verify_shard.py on a quiet
machine and from the packaged DMG acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PAGES = 96
RENDER_DPI = 150
WORKERS = 4
PAGE_PT = (420, 595)   # A5, 150 dpi -> 875x1240 px per page, ~1.08 MPix
SOURCE_DPI = 150       # the page raster is a real 150 dpi JPEG, not an upscaled thumbnail

# Why the source resolution matters: the acceptance fixture must be a scan the
# way a scanner produces one. A 72 dpi raster stretched to 150 dpi costs a
# different amount of work per page, so a fixture like that could "verify"
# sharding with a document whose measured gain is not the one the cost model
# predicted. With genuine 150 dpi JPEG rasters the packaged CLI measures
# 1.3-2.2x.
#
# Why A5: Pillow keeps every frame handed to a multi-page save (it builds a list
# internally), so the fixture generator's peak memory is pages x frame bytes.
# 96 A4 sheets at 150 dpi measured 879 MB of resident memory; A5 keeps the same
# page count, dpi and JPEG structure at ~1/3 of that. Page size and render DPI
# are what the engine's cost model measures (96 x 1.08 = 104 MPix). The model
# predicts 1.147x for 2 shards and 1.159x for 3, so any runner with >=2 cores
# must take the sharded path; forced measurement of the same raster work was
# 1.21-1.31x. Note this gate verifies the packaged report and the output bytes,
# not the process table (speed is printed, not gated, on a shared runner).



def _scan_sheet(index: int):
    from PIL import Image, ImageDraw
    width = int(PAGE_PT[0] / 72 * SOURCE_DPI)
    height = int(PAGE_PT[1] / 72 * SOURCE_DPI)
    image = Image.new("RGB", (width, height), (246, 246, 244))
    draw = ImageDraw.Draw(image)
    for row in range(34):
        draw.text((90, 140 + row * 60), "scan %03d line %02d" % (index + 1, row),
                  fill=(30, 30, 30))
    for column in range(0, width, 97):
        draw.line([(column, 0), (column, height)], fill=(210, 210, 210))
    return image


def make_scan(work: Path, path: Path, pages: int = PAGES) -> None:
    """A raster-only multi-page PDF: one full-page JPEG raster per page.

    Built with Pillow (shipped inside every package) so this file needs no extra
    dependency. The "text" is drawn into the bitmap, so pdfium must classify the
    document as a scan; if the classifier ever stops doing that, the checks below
    fail instead of quietly measuring an unsharded run.

    The sheets are written as JPEG files first and then opened one at a time:
    Pillow keeps every frame it is asked to save, and holding 96 A4 sheets at
    150 dpi in memory measured 869 MB (too much for a CI runner), while the
    file-backed path decodes each sheet only when its page is written.
    """
    from PIL import Image
    sheets = []
    for index in range(pages):
        sheet = _scan_sheet(index)
        item = work / ("sheet-%03d.jpg" % index)
        if not item.is_file():
            sheet.save(str(item), "JPEG", quality=85)
        sheet.close()
        sheets.append(item)

    def frames():
        for item in sheets:
            with Image.open(item) as handle:
                yield handle.convert("RGB")  # copy, so the file handle can close

    stream = frames()
    first = next(stream)
    first.save(str(path), "PDF", save_all=True, append_images=stream,
               resolution=float(SOURCE_DPI))
    first.close()


def sha256_tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.suffix == ".png"}


def latest_report(state: Path) -> dict:
    """The shard decision of the run that just finished, from the state folder.

    The engine does not write its report into the output folder, so an
    acceptance that looked for report.json next to the outputs would silently
    read nothing - and "no shard evidence" must never pass as "sharded".
    """
    reports = [p for p in state.glob("outputs/*/records/run-*/report.json") if p.is_file()]
    if not reports:
        return {}
    newest = max(reports, key=lambda p: p.stat().st_mtime)
    data = json.loads(newest.read_text(encoding="utf-8"))
    results = data.get("results") or [{}]
    return results[0].get("shard") or {}


def run(chamo: Path, pdf: Path, out: Path, shard: str, state: Path) -> tuple:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    command = [str(chamo), str(pdf), "-o", str(out), "-t", "pdf-images",
               "--dpi", str(RENDER_DPI), "--workers", str(WORKERS),
               "--shard", shard, "--quiet"]
    # Keep the acceptance hermetic: its bookkeeping goes into the work directory
    # instead of the real user's Application Support folder.
    environment = dict(os.environ)
    environment["CHAMO_STATE_DIR"] = str(state)
    # The auto arm runs first and would fill the page cache; the off arm would
    # then read those pages back and its wall time would stop being comparable.
    # This gate verifies sharding, not resume, so the page cache stays off for
    # both arms (tests/test_page_resume.py and scripts/verify_page_resume.py are
    # the acceptance for the cache itself).
    environment.setdefault("CHAMO_PAGE_CACHE", "0")
    started = time.monotonic()
    done = subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1800,
                          env=environment)
    wall = time.monotonic() - started
    if done.returncode != 0:
        raise SystemExit("packaged run --shard %s exited %d: %s"
                         % (shard, done.returncode,
                            (done.stdout + done.stderr)[-1200:]))
    return wall, sha256_tree(out), latest_report(state)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chamo", required=True,
                        help="packaged launcher (chamo, chamo.cmd or the .app binary)")
    parser.add_argument("--pages", type=int, default=PAGES)
    parser.add_argument("--work", default="",
                        help="work directory (default: a fresh temporary one)")
    parser.add_argument("--json", default="", help="write the result here")
    args = parser.parse_args(argv)
    chamo = Path(args.chamo).resolve()
    if not chamo.is_file():
        raise SystemExit("no packaged launcher at %s" % chamo)

    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="dw-pkg-shard-"))
    work.mkdir(parents=True, exist_ok=True)
    pdf = work / ("scan-%d.pdf" % args.pages)
    if not pdf.is_file():
        make_scan(work, pdf, args.pages)
    print("fixture: %s (%.1f MB, %d pages %dx%d pt, source %d dpi, render %d dpi)"
          % (pdf, pdf.stat().st_size / 1e6, args.pages, PAGE_PT[0], PAGE_PT[1],
             SOURCE_DPI, RENDER_DPI), flush=True)

    state = work / "state"
    auto_wall, auto_tree, auto_shard = run(chamo, pdf, work / "auto", "auto", state)
    off_wall, off_tree, off_shard = run(chamo, pdf, work / "off", "off", state)
    speedup = off_wall / auto_wall if auto_wall else 0.0

    failures = []
    def check(name, actual, expected):
        if actual == expected:
            print("PASS %s (%s)" % (name, actual))
        else:
            print("FAIL %s: got %r want %r" % (name, actual, expected))
            failures.append(name)

    check("auto page files", len(auto_tree), args.pages)
    check("off page files", len(off_tree), args.pages)
    check("auto/off identical bytes", auto_tree == off_tree, True)
    check("auto took the sharded path", bool(auto_shard.get("used")), True)
    check("auto classified as a scan", auto_shard.get("kind"), "image")
    check("auto used >= 2 shards", (auto_shard.get("shards") or 0) >= 2, True)
    # The decision must be the cost model's, not a lucky guess: the packaged
    # report has to carry the model inputs and a predicted gain above the floor
    # it reports. An artifact that lost the model fields cannot pass.
    check("auto report carries the model fields",
          all(key in auto_shard for key in
              ("total_pixels", "cores", "predicted_gain", "gain_floor")), True)
    check("auto predicted gain >= reported floor",
          (auto_shard.get("predicted_gain") or 0) >= (auto_shard.get("gain_floor") or 0), True)
    check("off did not shard", bool(off_shard.get("used")), False)
    # The output folder must contain the page folder and nothing else.
    for label, out_dir in (("auto", work / "auto"), ("off", work / "off")):
        roots = sorted(item.name for item in out_dir.iterdir())
        check("%s output folder has only the pages" % label, roots, [pdf.stem])
    check("bookkeeping went to the state folder",
          len(list(state.glob("outputs/*/records/run-*/report.json"))), 2)
    if auto_tree != off_tree:
        missing = sorted(set(off_tree) - set(auto_tree))[:5]
        changed = sorted(k for k in set(auto_tree) & set(off_tree)
                         if auto_tree[k] != off_tree[k])[:5]
        print("   missing from auto: %s" % missing)
        print("   differing bytes:   %s" % changed)
    print("measured (not gated on a shared runner): auto %.2fs / off %.2fs = %.3fx"
          % (auto_wall, off_wall, speedup))
    print("auto shard decision: %s" % json.dumps(auto_shard, ensure_ascii=False))

    result = {"launcher": str(chamo), "pages": args.pages, "dpi": RENDER_DPI,
              "workers": WORKERS, "auto_wall": round(auto_wall, 3),
              "off_wall": round(off_wall, 3), "speedup": round(speedup, 3),
              "auto_shard": auto_shard, "off_shard": off_shard,
              "files": [len(auto_tree), len(off_tree)],
              "identical": auto_tree == off_tree,
              "failures": failures, "result": "PASS" if not failures else "FAIL"}
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    print("PACKAGE SHARD RESULT: %s" % result["result"])
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
