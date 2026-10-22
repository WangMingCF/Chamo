#!/usr/bin/env python3
"""One command that measures Chamo and checks that the output is still correct.

Why
---
Every "faster" and "smaller" claim in docs/ITERATION_PLAN.md has to be
reproducible with the same corpus, the same cache setting and the same number of
repetitions. Before this script the numbers came from ad-hoc runs with different
sample sets, and one of them was wrong: with the page cache on, the second run of
the same PDF took about 40% of the first, so a benchmark that did not disable the
cache measured the cache, not the renderer.

What it guarantees
------------------
* the corpus is generated from scripts/bench_corpus.py and its manifest hash is
  recorded, so two reports can be compared only when the input bytes match;
* the page cache is off (CHAMO_PAGE_CACHE=0) and the child's environment is
  recorded, because a warm cache changes the number by 2.5x;
* each case runs N times (default 3) and the report carries the median, the
  minimum, the maximum and the spread, never a single lucky run;
* the output of every case is checked for correctness before its timing is
  reported (scripts/bench_quality.py). A case that is fast because it dropped a
  page or lowered the DPI is marked FAIL and its timing is not used.

Usage
-----
    python scripts/bench.py --suite representative --reps 3 --json build/bench.json
    python scripts/bench.py --suite smoke --only pdf-images
    python scripts/bench.py --app build/Chamo.app          # packaging sizes too

Exit codes: 0 every case passed, 1 a case failed (engine error or an output that
is not correct), 2 bad usage.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine" / "workbench.py"
CHAMO = ROOT / "engine" / "chamo.py"

SCHEMA = 1

# Each case is one operation on one corpus item. "quality" names the check in
# scripts/bench_quality.py that has to pass before the timing counts.
CASES = {
    "smoke": [
        {"name": "pdf-images.text", "operation": "pdf_images", "input": "text.pdf",
         "config": {"format": "png", "dpi": 150}, "quality": {"dpi": 150}},
        {"name": "pdf-images.scan", "operation": "pdf_images", "input": "scan.pdf",
         "config": {"format": "png", "dpi": 150}, "quality": {"dpi": 150}},
        {"name": "image-convert.batch", "operation": "image_convert", "inputs": "images",
         "config": {"format": "png"}, "quality": {"lossy": False}},
        {"name": "image-compress.lossless", "operation": "image_compress", "inputs": "images",
         "config": {"format": "png", "lossy": False}, "quality": {"lossy": False}},
        {"name": "pdf-compress.lossless", "operation": "pdf_compress", "input": "text.pdf",
         "config": {"lossy": False}, "quality": {}},
        {"name": "rag.strict", "operation": "rag", "input": "deck.pptx", "config": {},
         "quality": {}},
        # The office fixtures are only worth having if LibreOffice opens them, and
        # the quality check only means something if it sees the rendered words.
        {"name": "office-pdf.deck", "operation": "office_pdf", "input": "deck.pptx",
         "config": {}, "quality": {}},
        {"name": "office-pdf.memo", "operation": "office_pdf", "input": "memo.docx",
         "config": {}, "quality": {}},
        {"name": "office-pdf.sheet", "operation": "office_pdf", "input": "sheet.xlsx",
         "config": {}, "quality": {}},
        {"name": "engine.cli-start", "kind": "command",
         "argv": [sys.executable, "-I", "-B", str(CHAMO), "--help"]},
    ],
    "representative": [
        {"name": "pdf-images.text120", "operation": "pdf_images", "input": "text.pdf",
         "config": {"format": "png", "dpi": 150}, "quality": {"dpi": 150}},
        {"name": "pdf-images.scan24", "operation": "pdf_images", "input": "scan.pdf",
         "config": {"format": "png", "dpi": 150}, "quality": {"dpi": 150}},
        {"name": "pdf-images.mixed20", "operation": "pdf_images", "input": "mixed.pdf",
         "config": {"format": "png", "dpi": 150}, "quality": {"dpi": 150}},
        {"name": "image-convert.batch", "operation": "image_convert", "inputs": "images",
         "config": {"format": "png"}, "quality": {"lossy": False}},
        {"name": "image-compress.lossless", "operation": "image_compress", "inputs": "images",
         "config": {"format": "png", "lossy": False}, "quality": {"lossy": False}},
        {"name": "image-compress.jpeg", "operation": "image_compress", "inputs": "images",
         "config": {"format": "jpeg", "lossy": True, "quality": 80},
         "quality": {"lossy": True}},
        {"name": "pdf-compress.lossless", "operation": "pdf_compress", "input": "text.pdf",
         "config": {"lossy": False}, "quality": {}},
        {"name": "pdf-split.strict", "operation": "pdf_split", "input": "text.pdf",
         "config": {"pdf_pages": 25}, "quality": {}},
        {"name": "office-pdf.deck", "operation": "office_pdf", "input": "deck.pptx",
         "config": {}, "quality": {}},
        {"name": "office-pdf.memo", "operation": "office_pdf", "input": "memo.docx",
         "config": {}, "quality": {}},
        {"name": "office-pdf.sheet", "operation": "office_pdf", "input": "sheet.xlsx",
         "config": {}, "quality": {}},
        {"name": "rag.strict", "operation": "rag", "input": "deck.pptx", "config": {},
         "quality": {}},
        {"name": "engine.cli-start", "kind": "command",
         "argv": [sys.executable, "-I", "-B", str(CHAMO), "--help"]},
        {"name": "engine.import", "kind": "command",
         "argv": [sys.executable, "-I", "-B", "-c",
                  "import sys; sys.path.insert(0, sys.argv[1]); import workbench",
                  str(ROOT / "engine")]},
    ],
}
CASES["holdout"] = [dict(case) for case in CASES["representative"]]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quality = load_module("chamo_bench_quality", ROOT / "scripts" / "bench_quality.py")
corpus_builder = load_module("chamo_bench_corpus", ROOT / "scripts" / "bench_corpus.py")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def machine_info() -> dict:
    info = {"platform": sys.platform, "python": platform.python_version(),
            "cpu_count": os.cpu_count()}
    for key in ("system", "release", "machine", "processor"):
        value = getattr(platform, key, lambda: "")()
        if value:
            info[key] = value
    return info


def dependency_versions() -> dict:
    """Versions of the libraries whose speed or output this suite measures."""
    versions = {}
    for module, name in (("PIL", "pillow"), ("pypdfium2", "pypdfium2"), ("pypdf", "pypdf"),
                         ("pikepdf", "pikepdf"), ("img2pdf", "img2pdf"),
                         ("reportlab", "reportlab"), ("pptx", "python-pptx"),
                         ("openpyxl", "openpyxl")):
        try:
            imported = __import__(module)
            versions[name] = getattr(imported, "__version__", "unknown")
        except Exception:                                   # noqa: BLE001 - optional here
            versions[name] = "missing"
    return versions


# --------------------------------------------------------------------------
# One measured run
# --------------------------------------------------------------------------
def parse_events(stdout: str) -> list:
    """The engine's machine-readable stream: one JSON object per line."""
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def load_report(stdout: str) -> dict:
    """The run's report.json: which source produced which file.

    The bench does not tell the engine where its files are and then guess; it
    reads back what the engine recorded and checks those files. A run that
    publishes something it did not record shows up as a mismatch.
    """
    for event in parse_events(stdout):
        if event.get("type") == "scan" and event.get("report"):
            try:
                return json.loads((Path(event["report"]) / "report.json")
                                  .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
    return {}


def recorded_pairs(report: dict) -> list:
    """[(source path, output path)] as the run recorded them."""
    pairs = []
    for result in (report or {}).get("results") or []:
        sources = [str(item) for item in result.get("sources") or []]
        for output in result.get("outputs") or []:
            path = output.get("path") if isinstance(output, dict) else output
            if path and sources:
                pairs.append((sources[0], str(path)))
    return pairs


def run_engine(inputs, out_dir: Path, operation: str, config: dict, timeout: int) -> dict:
    """Run engine/workbench.py as a subprocess and time it.

    The engine is started the way production starts it (a JSON request file and a
    separate interpreter) instead of being imported: an import would leave out
    interpreter start-up, which is a real part of what a user waits for.
    """
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    request = {"protocol": 1, "inputs": [str(Path(item)) for item in inputs],
               "output": str(out_dir),
               "config": {"operation": operation, "naming": "clean", "range": "",
                          "batch_files": 50, "pdf_pages": 100,
                          "pdf_bytes": 100_000_000, "max_edge": 0, "lossy": False,
                          "quality": 90, "dpi": 150, "format": "png", "workers": 0,
                          **config}}
    request_path = out_dir.parent / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    environment = dict(os.environ)
    environment["CHAMO_PAGE_CACHE"] = "0"      # see the module docstring
    environment["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    try:
        completed = subprocess.run([sys.executable, "-I", "-B", str(ENGINE),
                                    "--request", str(request_path)],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, env=environment,
                                   stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"seconds": timeout, "timed_out": True, "rc": None, "stderr": "",
                "output_bytes": 0, "outputs": 0}
    seconds = time.monotonic() - started
    files = quality.deliverables(out_dir)
    return {"seconds": seconds, "timed_out": False, "rc": completed.returncode,
            "stderr": (completed.stderr or "").strip()[-1500:],
            "output_bytes": sum(path.stat().st_size for path in files),
            "outputs": len(files), "report": load_report(completed.stdout or ""),
            "reported_outputs": len(recorded_pairs(load_report(completed.stdout or "")))}


def run_command(argv, timeout: int) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout,
                                   stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"seconds": timeout, "timed_out": True, "rc": None, "stderr": "",
                "output_bytes": 0, "outputs": 0}
    return {"seconds": time.monotonic() - started, "timed_out": False,
            "rc": completed.returncode, "stderr": (completed.stderr or "").strip()[-800:],
            "output_bytes": 0, "outputs": 0}


# --------------------------------------------------------------------------
# Quality of one case's outputs
# --------------------------------------------------------------------------
def quality_issues(case: dict, corpus: Path, out_dir: Path, report: dict) -> list:
    operation = case["operation"]
    options = dict(case.get("quality") or {})
    if operation == "pdf_images":
        return quality.check_pdf_images(corpus / case["input"], out_dir, options.get("dpi", 150))
    if operation in ("image_convert", "image_compress"):
        if operation == "image_convert":
            mode = "convert"
        else:
            mode = "lossy" if (case.get("config") or {}).get("lossy") else "lossless"
        return quality.check_image_pairs(recorded_pairs(report), mode=mode)
    if operation == "pdf_split":
        return quality.check_pdf_split(corpus / case["input"], out_dir)
    if operation == "pdf_compress":
        return quality.check_pdf_compress(corpus / case["input"], out_dir)
    if operation == "rag":
        return quality.check_rag([corpus / case["input"]], out_dir)
    if operation in ("office_pdf", "office_images"):
        return check_office_pdf(corpus / case["input"], out_dir, report)
    return ["no quality check for operation %r - the timing would be unverified" % operation]


def check_office_pdf(source: Path, out_dir: Path, report: dict) -> list:
    """The rendered PDF must exist, have pages, and carry the source's words."""
    issues = []
    pdfs = [path for path in quality.deliverables(out_dir) if path.suffix.lower() == ".pdf"]
    if len(pdfs) != 1:
        pdfs = [Path(path) for _, path in recorded_pairs(report)
                if str(path).lower().endswith(".pdf")]
    if len(pdfs) != 1:
        return ["expected one rendered PDF, found %d" % len(pdfs)]
    try:
        count = quality.pdf_page_count(pdfs[0])
        text = " ".join(quality.pdf_text_pages(pdfs[0]))
    except Exception as error:                              # noqa: BLE001
        return ["rendered PDF cannot be read: %s" % error]
    if count < 1:
        issues.append("rendered PDF has no pages")
    needles = {"deck.pptx": "Synthetic slide", "memo.docx": "Paragraph 1",
               "sheet.xlsx": "label"}.get(source.name)
    if needles and needles.lower() not in text.lower():
        issues.append("%s: the rendered PDF does not contain %r - the render lost the text"
                      % (source.name, needles))
    return issues


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def summarise(runs: list) -> dict:
    times = [run["seconds"] for run in runs if not run.get("timed_out")]
    if not times:
        return {"median_seconds": None, "min_seconds": None, "max_seconds": None,
                "spread": None}
    median = statistics.median(times)
    return {"median_seconds": round(median, 3), "min_seconds": round(min(times), 3),
            "max_seconds": round(max(times), 3),
            "spread": round((max(times) - min(times)) / median, 4) if median else None}


def run_case(case: dict, corpus: Path, work: Path, reps: int, timeout: int,
             keep_outputs: bool) -> dict:
    name = case["name"]
    record = {"name": name, "operation": case.get("operation"), "runs": []}
    if case.get("kind") == "command":
        record["runs"] = [run_command(case["argv"], timeout) for _ in range(reps)]
        record.update(summarise(record["runs"]))
        record["status"] = "FAIL" if any(run["rc"] not in (0, None) for run in record["runs"]) else "PASS"
        record["quality"] = {"issues": []}
        if record["status"] == "FAIL":
            record["quality"]["issues"] = ["command exited with %s"
                                           % [run["rc"] for run in record["runs"]]]
        return record

    inputs = ([corpus / case["input"]] if case.get("input")
              else [corpus / case["inputs"]])
    issues = []
    for rep in range(reps):
        out_dir = work / name / ("rep-%02d" % rep) / "out"
        measured = run_engine(inputs, out_dir, case["operation"], case.get("config") or {},
                              timeout)
        report = measured.pop("report", {})
        record["runs"].append(measured)
        if measured["timed_out"]:
            issues.append("run %d timed out after %ds" % (rep + 1, timeout))
        elif measured["rc"] != 0:
            issues.append("run %d exited with %s: %s"
                          % (rep + 1, measured["rc"], (measured["stderr"] or "")[-400:]))
        else:
            found = quality_issues(case, corpus, out_dir, report)
            if found:
                issues.extend("run %d: %s" % (rep + 1, issue) for issue in found)
        if rep == 0 and measured["rc"] == 0:
            record["output_bytes"] = measured["output_bytes"]
            record["outputs"] = measured["outputs"]
            if case["operation"] == "pdf_compress":
                # Reported, not judged: whether a lossless cleanup helps is a
                # property of the document, and the number belongs in the report.
                record["growth"] = quality.compressed_growth(corpus / case["input"], out_dir)
    record.update(summarise(record["runs"]))
    record["quality"] = {"issues": issues}
    record["status"] = "FAIL" if issues else "PASS"
    if not keep_outputs and record["status"] == "PASS":
        remove_tree(work / name)
    return record


def remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def app_sizes(app: Path) -> dict:
    """Size breakdown of a built .app, the numbers section 4.1 reports."""
    def size(path: Path) -> int:
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())

    result = {"total_bytes": size(app)}
    contents = app / "Contents"
    if contents.is_dir():
        for child in sorted(contents.iterdir()):
            result[child.name] = size(child)
        resources = contents / "Resources"
        frameworks = contents / "Frameworks"
        for label, root in (("resources", resources), ("frameworks", frameworks)):
            if root.is_dir():
                result[label + "_top"] = {
                    child.name: size(child) for child in
                    sorted((item for item in root.iterdir() if item.is_dir()),
                           key=lambda item: size(item), reverse=True)[:8]}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure Chamo on a fixed synthetic corpus")
    parser.add_argument("--suite", default="representative", choices=sorted(CASES))
    parser.add_argument("--reps", type=int, default=3, help="repetitions per case (>=3)")
    parser.add_argument("--corpus", default="", help="reuse a corpus instead of building one")
    parser.add_argument("--only", default="", help="run only cases whose name contains this")
    parser.add_argument("--json", default="", help="write the full report here")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--app", default="", help="also record a built .app's size breakdown")
    parser.add_argument("--keep", action="store_true", help="keep every run's output folder")
    args = parser.parse_args(argv)
    if args.reps < 1:
        parser.error("--reps must be at least 1")
    if args.reps < 3:
        print("note: --reps %d is below the 3 the plan requires for a reported number"
              % args.reps, file=sys.stderr)

    work = Path(tempfile.mkdtemp(prefix="chamo-bench-"))
    if args.corpus:
        corpus = Path(args.corpus).expanduser().resolve()
        problems = corpus_builder.check(corpus)
        if problems:
            for problem in problems:
                print("corpus problem: %s" % problem, file=sys.stderr)
            return 2
        manifest = json.loads((corpus / corpus_builder.MANIFEST_NAME).read_text(encoding="utf-8"))
    else:
        corpus = work / "corpus"
        print("[corpus] building the %s suite in %s" % (args.suite, corpus), flush=True)
        manifest = corpus_builder.build(corpus, args.suite)

    selected = [case for case in CASES[args.suite]
                if not args.only or args.only in case["name"]]
    if not selected:
        parser.error("--only %r matched no case" % args.only)

    report = {"schema": SCHEMA, "suite": args.suite, "reps": args.reps,
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "machine": machine_info(), "dependencies": dependency_versions(),
              "environment": {"CHAMO_PAGE_CACHE": "0"},
              "corpus": {"suite": manifest["suite"], "total_bytes": manifest["total_bytes"],
                         "manifest_sha256": sha256_text(json.dumps(manifest["files"],
                                                                   sort_keys=True))},
              "cases": []}
    for case in selected:
        print("[bench] %s (%d repetition(s))" % (case["name"], args.reps), flush=True)
        record = run_case(case, corpus, work, args.reps, args.timeout, args.keep)
        report["cases"].append(record)
        print("        %s median %ss spread %s%%%s"
              % (record["status"],
                 record.get("median_seconds"),
                 round((record.get("spread") or 0) * 100, 1),
                 "" if record["status"] == "PASS"
                 else "  <-- " + "; ".join(record["quality"]["issues"][:2])), flush=True)
    if args.app:
        report["app"] = app_sizes(Path(args.app).expanduser().resolve())

    failed = [record for record in report["cases"] if record["status"] != "PASS"]
    print("")
    print("%-26s %10s %10s %8s  %s" % ("case", "median s", "min s", "spread", "verdict"))
    for record in report["cases"]:
        print("%-26s %10s %10s %7s%%  %s"
              % (record["name"], record.get("median_seconds"), record.get("min_seconds"),
                 round((record.get("spread") or 0) * 100, 1), record["status"]))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        print("report: %s" % args.json)
    if not args.keep:
        remove_tree(work)
    else:
        print("work directory: %s" % work)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
