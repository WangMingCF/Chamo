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

Both are reported on two clocks. The gate judges CPU seconds (it compares child
CPU time, see text_timing_verdict), so CPU is the comparable number; wall time is
reported because the classifier also opens the file, and a reader is entitled to
know whether I/O could hide behind the CPU number.

The measurement is averaged inside each repetition. That is not cosmetic:
Windows' process clock ticks at about 15.6 ms and one classify() takes about
1 ms, so a single reading there quantises to 0 or 15.6 ms and the median of a few
readings is 0. Calling classify K times per repetition and dividing puts the
reading far above the tick on every platform.

If the classifier is a fraction of a percent of the render, it cannot explain a
5% difference and the gate's noise has to be handled as noise. If it is several
percent, the gate's budget is comparing two genuinely different amounts of work
and should say so.

This is a probe: no engine change, no threshold, no policy. It reports numbers.

Usage
-----
    python scripts/probe_text_classify.py                    # 120 pages, 3 reps
    python scripts/probe_text_classify.py --pages 40 --iterations 30 --json out.json

Exit codes: 0 the numbers were measured on the expected kind of document,
1 the measurement or the classification was not usable (the probe says why),
2 bad usage.
"""
from __future__ import annotations

import argparse
import json
import shutil
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


def measure(action, reps: int, iterations: int) -> dict:
    """Seconds per single action on the CPU clock and on the wall clock.

    Each repetition runs the action N times and divides, so a reading is never a
    fraction of a clock tick (see the module docstring).
    """
    cpu, wall = [], []
    for _ in range(reps):
        cpu_started = time.process_time()
        wall_started = time.perf_counter()
        for _ in range(iterations):
            action()
        cpu.append((time.process_time() - cpu_started) / iterations)
        wall.append((time.perf_counter() - wall_started) / iterations)
    return {"cpu": summary(cpu), "wall": summary(wall)}


def summary(values):
    if not values:
        return {"median": None, "min": None, "max": None}
    # Six decimals: a classify call is around a millisecond, and four decimals
    # could not tell a fast measurement from a rounded one.
    return {"median": round(statistics.median(values), 6),
            "min": round(min(values), 6), "max": round(max(values), 6)}


def classify_action(path: Path):
    import pageclass

    return lambda: pageclass.classify(path)


def render_action(path: Path, dpi: int):
    import pypdfium2 as pdfium

    def render():
        with pdfium.PdfDocument(str(path)) as document:
            scale = dpi / 72.0
            for index in range(len(document)):
                page = document[index]
                bitmap = page.render(scale=scale)
                bitmap.close()
                page.close()
    return render


def ratio_of(classify: dict, render: dict, clock: str):
    high, low = classify[clock]["median"], render[clock]["median"]
    if not high or not low:
        return None
    return round(high / low, 6)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure the classifier against a real render")
    parser.add_argument("--pages", type=int, default=120)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20,
                        help="classify calls per repetition (keeps the reading above the clock tick)")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    if args.pages < 1 or args.reps < 1 or args.iterations < 1:
        parser.error("--pages, --reps and --iterations must be at least 1")

    work = Path(tempfile.mkdtemp(prefix="chamo-classify-probe-"))
    try:
        import pageclass

        document = text_pdf(work / "text.pdf", args.pages)
        decision = pageclass.classify(document)
        # Warm-up: a cold import or the first pdfium load must not be charged to
        # whichever side happens to run first.
        measure(classify_action(document), 1, args.iterations)
        measure(render_action(document, args.dpi), 1, 1)
        classify = measure(classify_action(document), args.reps, args.iterations)
        render = measure(render_action(document, args.dpi), args.reps, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report = {"pages": args.pages, "reps": args.reps, "iterations": args.iterations,
              "dpi": args.dpi, "kind": decision.get("kind"),
              "classify": classify, "render": render,
              "ratio": {"cpu": ratio_of(classify, render, "cpu"),
                        "wall": ratio_of(classify, render, "wall")},
              "problems": []}
    if report["kind"] != "text":
        report["problems"].append(
            "the classifier answered %r, not 'text': this probe would be measuring "
            "a different document than the timing gate under discussion" % report["kind"])
    if report["ratio"]["cpu"] is None or report["ratio"]["wall"] is None:
        report["problems"].append(
            "no usable ratio was produced (a measurement came out as zero or was "
            "never taken); the probe reports that instead of claiming success")
    # ascii on the console: a Windows job can have a non-UTF-8 code page.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
