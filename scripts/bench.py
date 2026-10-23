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
import signal
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

def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Loaded before the case table, because the case names come from the corpus specs:
# importing these later made the module fail to import at all.
quality = load_module("chamo_bench_quality", ROOT / "scripts" / "bench_quality.py")
corpus_builder = load_module("chamo_bench_corpus", ROOT / "scripts" / "bench_corpus.py")


# Each case is one operation on one corpus item. "quality" carries the options of
# the check in scripts/bench_quality.py that has to pass before the timing counts.
def cases_for(suite: str) -> list:
    """The cases of one suite, with the corpus's own numbers in their names.

    The page counts belong in the name because a report is read out of context: a
    case called "text120" that actually ran on the 37-page holdout document would
    make two reports look comparable when their inputs differ. A full run also
    covers the extra operations; the smoke suite is the few seconds CI can afford.
    """
    spec = corpus_builder.SUITES[suite]
    text_pages, scan_pages, mixed_pages = (spec["text_pages"], spec["scan_pages"],
                                           spec["mixed_pages"])
    full = suite != "smoke"
    cases = [
        {"name": "pdf-images.text%d" % text_pages, "operation": "pdf_images",
         "input": "text.pdf", "config": {"format": "png", "dpi": 150},
         "quality": {"dpi": 150}},
        {"name": "pdf-images.scan%d" % scan_pages, "operation": "pdf_images",
         "input": "scan.pdf", "config": {"format": "png", "dpi": 150},
         "quality": {"dpi": 150}},
    ]
    if full:
        cases.append({"name": "pdf-images.mixed%d" % mixed_pages,
                      "operation": "pdf_images", "input": "mixed.pdf",
                      "config": {"format": "png", "dpi": 150}, "quality": {"dpi": 150}})
    cases += [
        {"name": "image-convert.batch", "operation": "image_convert", "inputs": "images",
         "config": {"format": "png"}},
        # The lossless case must not feed the engine JPEG: lossless compression
        # refuses JPEG by design ("无损优化仅支持 PNG"), so a mixed batch made this
        # case fail on its own corpus for a reason that is not a defect. PNG-only
        # keeps the RGBA + metadata and grayscale images that make "lossless" mean
        # something; the mixed directory still covers the lossy case below.
        {"name": "image-compress.lossless", "operation": "image_compress",
         "inputs": "images-png", "config": {"format": "png", "lossy": False}},
    ]
    if full:
        # Whether a run may lose detail is decided by its config alone; a second
        # "quality": {"lossy": ...} copy used to sit here and nothing read it, so
        # editing that one would have changed nothing.
        cases.append({"name": "image-compress.jpeg", "operation": "image_compress",
                      "inputs": "images",
                      "config": {"format": "jpeg", "lossy": True, "quality": 80}})
    cases.append({"name": "pdf-compress.lossless", "operation": "pdf_compress",
                  "input": "text.pdf", "config": {"lossy": False}, "quality": {}})
    if full:
        cases.append({"name": "pdf-split.strict", "operation": "pdf_split",
                      "input": "text.pdf", "config": {"pdf_pages": 25}, "quality": {}})
    # The office fixtures are only worth having if LibreOffice opens them, and the
    # quality check only means something if it sees the rendered words. Writer and
    # Impress are installed on every engine CI job; Calc is not (see
    # .github/actions/setup-engine), so the .xlsx case is not in the smoke suite
    # instead of failing the smoke run on Linux for a reason unrelated to the
    # benchmark.
    cases += [
        {"name": "office-pdf.deck", "operation": "office_pdf", "input": "deck.pptx",
         "config": {}, "quality": {}},
        {"name": "office-pdf.memo", "operation": "office_pdf", "input": "memo.docx",
         "config": {}, "quality": {}},
    ]
    if full:
        cases.append({"name": "office-pdf.sheet", "operation": "office_pdf",
                      "input": "sheet.xlsx", "config": {}, "quality": {}})
    cases.append({"name": "rag.strict", "operation": "rag", "input": "deck.pptx",
                  "config": {}, "quality": {}})
    cases.append({"name": "engine.cli-start", "kind": "command",
                  "argv": [sys.executable, "-I", "-B", str(CHAMO), "--help"]})
    if full:
        cases.append({"name": "engine.import", "kind": "command",
                      "argv": [sys.executable, "-I", "-B", "-c",
                               "import sys; sys.path.insert(0, sys.argv[1]); import workbench",
                               str(ROOT / "engine")]})
    return cases


CASES = {name: cases_for(name) for name in corpus_builder.SUITES}


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


def child_environment(state_dir=None) -> dict:
    """The environment every measured child gets, so the report can be honest.

    Command cases run in the same environment as engine cases; recording
    CHAMO_PAGE_CACHE=0 for the whole report while letting command cases inherit
    the caller's value would make the metadata describe something that did not
    happen.

    state_dir redirects the engine receipts/records. Without it a run writes into
    the user's real application state: pointing HOME at an empty directory and
    running the smoke suite added 44 files there, which contradicts "the corpus
    and outputs live in a temporary directory" and leaves junk on a developer's
    machine. Every measured run gets its own state folder inside the work
    directory, and the report records the variable.
    """
    environment = dict(os.environ)
    environment["CHAMO_PAGE_CACHE"] = "0"      # see the module docstring
    environment["PYTHONIOENCODING"] = "utf-8"
    if state_dir is not None:
        environment["CHAMO_STATE_DIR"] = str(state_dir)
    return environment


def unaccounted_sources(report: dict) -> list:
    """Sources the engine recorded as anything other than a successful conversion.

    The engine exits 0 when individual files fail: the per-file state is in
    report.json and the protocol-level exit code stays 0. Judging a case only by
    the exit code therefore let a run that silently skipped a broken file look
    faster - and its timing was adopted. That is the "did less work, so it was
    quicker" number this script exists to prevent.
    """
    problems = []
    for result in (report or {}).get("results") or []:
        state = str(result.get("state") or "")
        if state in ("complete", "resumed"):
            continue
        sources = [str(item) for item in result.get("sources") or []]
        detail = str(result.get("error") or "").strip()[:200]
        problems.append("%s: %s%s" % (", ".join(sources) or "<no source>",
                                      state or "no state",
                                      (" (" + detail + ")") if detail else ""))
    return problems


def _kill_tree(process) -> None:
    """Kill the child *and its descendants*.

    subprocess.run(timeout=...) kills only the direct child. Office conversions
    start LibreOffice, and soffice.exe inherits the stdout/stderr pipes; it
    survives the kill, keeps the write end open, and the follow-up drain never
    sees EOF - the bench would hang instead of recording a timeout, exactly on the
    slowest cases where a timeout is most likely.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _run(argv, timeout: int, env=None) -> dict:
    """Run argv with a timeout whose *cleanup* is bounded too."""
    started = time.monotonic()
    extra = {}
    if os.name == "nt":
        extra["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        extra["start_new_session"] = True
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", env=env, **extra)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # Something escaped the kill and still holds a pipe; report the
            # timeout instead of waiting for an EOF that will not come.
            stdout, stderr = "", ""
    seconds = time.monotonic() - started
    return {"seconds": timeout if timed_out else seconds, "timed_out": timed_out,
            "rc": None if timed_out else process.returncode,
            "stdout": stdout or "", "stderr": stderr or ""}


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
    # State lives under this run's own folder: see child_environment().
    result = _run([sys.executable, "-I", "-B", str(ENGINE), "--request", str(request_path)],
                  timeout=timeout,
                  env=child_environment(state_dir=out_dir.parent / "state"))
    if result["timed_out"]:
        return {"seconds": timeout, "timed_out": True, "rc": None, "stderr": "",
                "output_bytes": 0, "outputs": 0}
    files = quality.deliverables(out_dir)
    report = load_report(result["stdout"])
    return {"seconds": result["seconds"], "timed_out": False, "rc": result["rc"],
            "stderr": result["stderr"].strip()[-1500:],
            "output_bytes": sum(path.stat().st_size for path in files),
            "outputs": len(files), "report": report,
            "reported_outputs": len(recorded_pairs(report))}


def run_command(argv, timeout: int, env=None) -> dict:
    # Default to the measured environment here rather than at the call site: the
    # existing tests replace run_command with a two-argument stand-in, and a
    # keyword the stand-in does not accept turns a passing suite into a TypeError.
    result = _run(argv, timeout=timeout, env=env or child_environment())
    return {"seconds": result["seconds"], "timed_out": result["timed_out"],
            "rc": result["rc"], "stderr": result["stderr"].strip()[-800:],
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
        # The run's own config is the single source of truth for "may this lose
        # detail" - case["quality"] only carries checker parameters such as dpi.
        config = case.get("config") or {}
        lossy = config.get("format") in ("jpeg", "jpg") or bool(config.get("lossy"))
        if operation == "image_convert":
            mode = "convert"
        else:
            mode = "lossy" if lossy else "lossless"
        lossy_target = lossy
        return quality.check_image_pairs(recorded_pairs(report), mode=mode,
                                         lossy_target=lossy_target)
    if operation == "pdf_split":
        return quality.check_pdf_split(corpus / case["input"], out_dir)
    if operation == "pdf_compress":
        return quality.check_pdf_compress(corpus / case["input"], out_dir)
    if operation == "rag":
        # Pair by what the run recorded: the strict export keeps the source's whole
        # name and appends the part number, so matching on file name alone marked a
        # byte-identical export as missing (rag.strict could never pass).
        return quality.check_rag([corpus / case["input"]], out_dir,
                                 pairs=recorded_pairs(report))
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
    # A run that timed out or exited non-zero measured something other than the
    # work (start-up failure, a crash part-way through), so its number must not
    # reach the median even when the case-level timing is already suppressed.
    times = [run["seconds"] for run in runs
             if not run.get("timed_out") and run.get("rc", 0) == 0]
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
        # A timeout is a failure, not a pass without a number: run_command reports
        # rc=None when it gives up, so "rc not in (0, None)" would accept it. The
        # two paths of the same decision have to agree with each other.
        timed_out = any(run.get("timed_out") for run in record["runs"])
        failed = any(run.get("rc") != 0 for run in record["runs"])
        record["status"] = "FAIL" if (timed_out or failed) else "PASS"
        record["quality"] = {"issues": []}
        if timed_out:
            record["quality"]["issues"] = ["timed out after %ds" % timeout]
        elif failed:
            record["quality"]["issues"] = ["exit codes %s"
                                           % [run.get("rc") for run in record["runs"]]]
        record["timing_adopted"] = record["status"] == "PASS"
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
            # Per-file failures come back with rc=0, so the exit code alone would
            # mark a run that skipped a broken input as a clean, fast run.
            skipped = unaccounted_sources(report)
            if skipped:
                issues.extend("run %d: %s" % (rep + 1, issue) for issue in skipped)
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
    # "A timing from a wrong output is not used" has to be a fact in the report,
    # not an instruction to the reader: a consumer can filter on this flag.
    record["timing_adopted"] = record["status"] == "PASS"
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
    parser.add_argument("--reps", type=int, default=3,
                        help="repetitions per case (default 3; fewer is allowed but "
                             "the spread becomes unreliable and the report says so)")
    parser.add_argument("--corpus", default="", help="reuse a corpus instead of building one")
    parser.add_argument("--only", default="", help="run only cases whose name contains this")
    parser.add_argument("--json", default="", help="write the full report here")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--app", default="", help="also record a built .app's size breakdown")
    parser.add_argument("--keep", action="store_true", help="keep every run's output folder")
    args = parser.parse_args(argv)
    if args.reps < 1:
        parser.error("--reps must be at least 1")
    if args.app and not Path(args.app).expanduser().is_dir():
        # Sizes silently come out as total_bytes 0 for a typo'd path, and the
        # report then looks like a real measurement of an empty bundle.
        parser.error("--app is not a directory: %s" % args.app)
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
        # The suite in the report must describe the corpus that was actually used:
        # a holdout corpus labelled "representative" produces numbers nobody can
        # compare, which is the one thing this file exists to prevent.
        if manifest.get("suite") != args.suite:
            print("corpus at %s was built for suite %r, not %r"
                  % (corpus, manifest.get("suite"), args.suite), file=sys.stderr)
            return 2
    else:
        corpus = work / "corpus"
        print("[corpus] building the %s suite in %s" % (args.suite, corpus), flush=True)
        manifest = corpus_builder.build(corpus, args.suite)

    selected = [case for case in CASES[args.suite]
                if not args.only or args.only in case["name"]]
    if not selected:
        parser.error("--only %r matched no case" % args.only)

    report = {"schema": SCHEMA, "suite": args.suite, "reps": args.reps,
              # The plan requires three repetitions for a number that is quoted;
              # below that the report says so instead of leaving it to the reader.
              "reps_reported": args.reps >= 3,
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "machine": machine_info(), "dependencies": dependency_versions(),
              # Derived from the same function the children get, so the report
              # cannot claim a setting the child did not receive.
              "environment": {key: value for key, value in child_environment().items()
                              if key in ("CHAMO_PAGE_CACHE", "PYTHONIOENCODING",
                                         "CHAMO_STATE_DIR")},
              "state_dir": "per-run folder inside the work directory",
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
                 record.get("median_seconds") if record.get("timing_adopted") else "-",
                 round((record.get("spread") or 0) * 100, 1) if record.get("timing_adopted")
                 else "-",
                 "" if record["status"] == "PASS"
                 else "  <-- " + "; ".join(record["quality"]["issues"][:2])), flush=True)
    if args.app:
        report["app"] = app_sizes(Path(args.app).expanduser().resolve())

    failed = [record for record in report["cases"] if record["status"] != "PASS"]
    print("")
    print("%-26s %10s %10s %8s  %s" % ("case", "median s", "min s", "spread", "verdict"))
    for record in report["cases"]:
        adopted = record.get("timing_adopted")
        print("%-26s %10s %10s %8s  %s"
              % (record["name"],
                 record.get("median_seconds") if adopted else "-",
                 record.get("min_seconds") if adopted else "-",
                 ("%.1f%%" % (record.get("spread") * 100)) if adopted and record.get("spread") is not None else "-",
                 record["status"]))
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename: an interrupted run must not leave half a JSON file
        # that later readers would take for a complete report.
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        os.replace(temporary, target)
        print("report: %s" % args.json)
    if args.keep:
        print("work directory: %s" % work)
    elif failed:
        # run_case already keeps the output of a failed case; deleting the whole
        # work tree here would have thrown that evidence away and left only the
        # truncated stderr in the report.
        print("kept %s: %d case(s) failed" % (work, len(failed)))
    else:
        remove_tree(work)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
