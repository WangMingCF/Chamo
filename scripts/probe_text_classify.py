#!/usr/bin/env python3
"""Probe: what does the content classifier cost next to a real export?

Why this file exists
--------------------
The text-PDF timing gate in tests/test_shard_equivalence.py fails on macOS
occasionally even when both arms do the same work (Issue #10). One hypothesis for
the 5-6% gap was that the classifier itself costs that much: shard=auto has to
read the document to decide, shard=off does not.

That hypothesis is checkable. This probe measures, on the same document:

  * classify cost   - engine/pageclass.classify() (it samples page objects and
                      never renders a page);
  * render cost     - rendering every page at the DPI the gate uses.

If the classifier is a fraction of a percent of the render, then it cannot
explain a 5% difference and the gate's noise has to be handled as noise, not by
modelling a fixed overhead. If it is several percent, the gate's budget is
comparing two genuinely different amounts of work and should say so.

This is a probe: no engine change, no threshold, no policy. It reports numbers.

Usage
-----
    python scripts/probe_text_classify.py                  # 120 pages, 3 reps
    python scripts/probe_text_classify.py --pages 40 --reps 5 --json probe.json

Exit codes: 0 the numbers were measured on the expected kind of document,
1 the classifier did not classify the document as text (the probe would then be
answering a different question), 2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))


def text_pdf(path: Path, pages: int, lines: int = 55) -> Path:
    """The same shape as the benchmark's text document: dense, born-digital."""
    from reportlab.pdfgen import canvas

    canvas_obj = canvas.Canvas(str(path), invariant=1, pageCompression=1)
    for index in range(pages):
        canvas_obj.setFont("Helvetica", 10)
        for row in range(lines):
            canvas_obj.drawString(
                60, 800 - row * 12.5,
                "Page %d line %d: the quick brown fox jumps over the lazy dog 0123456789"
                % (index + 1, row))
        canvas_obj.showPage()
    canvas_obj.save()
    return path


def classify_seconds(path: Path, reps: int) -> list:
    import pageclass

    times = []
    for _ in range(reps):
        started = time.process_time()
        pageclass.classify(path)
        times.append(time.process_time() - started)
    return times


def render_seconds(path: Path, dpi: int, reps: int) -> list:
    import pypdfium2 as pdfium

    times = []
    for _ in range(reps):
        started = time.process_time()
        with pdfium.PdfDocument(str(path)) as document:
            scale = dpi / 72.0
            for index in range(len(document)):
                page = document[index]
                bitmap = page.render(scale=scale)
                bitmap.close()
                page.close()
        times.append(time.process_time() - started)
    return times


def summary(values):
    if not values:
        return {"median": None, "min": None, "max": None}
    return {"median": round(statistics.median(values), 4),
            "min": round(min(values), 4), "max": round(max(values), 4)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure the classifier against a real render")
    parser.add_argument("--pages", type=int, default=120)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    if args.pages < 1 or args.reps < 1:
        parser.error("--pages and --reps must be at least 1")

    import pageclass

    work = Path(tempfile.mkdtemp(prefix="chamo-classify-probe-"))
    document = text_pdf(work / "text.pdf", args.pages)
    decision = pageclass.classify(document)
    # A warm-up read once, so the first measurement is not paying for a cold import
    # or a first pdfium load that every later run amortises.
    classify_seconds(document, 1)
    render_seconds(document, args.dpi, 1)
    classify_times = classify_seconds(document, args.reps)
    render_times = render_seconds(document, args.dpi, args.reps)

    report = {"pages": args.pages, "reps": args.reps, "dpi": args.dpi,
              "kind": decision.get("kind"), "classify": summary(classify_times),
              "render": summary(render_times), "ratio": None,
              "problems": []}
    if report["classify"]["median"] and report["render"]["median"]:
        report["ratio"] = round(report["classify"]["median"] / report["render"]["median"], 5)
    if report["kind"] != "text":
        report["problems"].append(
            "the classifier answered %r, not 'text': this probe would be measuring "
            "a different document than the timing gate under discussion" % report["kind"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
