#!/usr/bin/env python3
"""Probe: does one LibreOffice start-up per batch beat one per file?

Why this file exists
--------------------
The iteration plan's 0.5.0 ("Office throughput") rests on one number that has
never been measured on the same machine as the alternative: LibreOffice costs
1.5-4 s per start-up, and the engine currently starts it once per input file. If
converting N files in one invocation really amortises that, a batch path is
worth building. If it does not, the plan entry is wrong and should say so.

This is a probe, not a feature: it does not touch the engine, it does not choose
a policy, and it is not part of the conversion path. It renders the same
synthetic documents both ways, checks that the outputs are equivalent, and
prints the numbers so the decision is made on evidence.

What it measures
----------------
* sequential - N separate soffice processes, one file each (what the engine does
  today, minus its own scheduling);
* batched    - one soffice process with N files on the command line.

Both arms are interleaved and repeated, so a busy CI runner cannot make one arm
look better by being measured at a quieter moment. The report carries the median,
the minimum and the spread, and the ratio is computed from the medians.

Correctness comes first: every PDF must exist, have pages, and carry the text of
its source. A faster batch that loses a document is not a result.

Usage
-----
    python scripts/probe_office_batch.py                     # 6 files, 3 reps
    python scripts/probe_office_batch.py --files 4 --reps 5 --json probe.json

Exit codes: 0 both arms produced correct output, 1 something was wrong with the
outputs or the renderer could not be found, 2 bad usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_renderer():
    import platform_office

    return platform_office.find(ENGINE)


def build_inputs(directory: Path, count: int) -> list:
    """Synthetic office files, generated here so the probe ships no binaries."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "chamo_bench_corpus", ROOT / "scripts" / "bench_corpus.py")
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for index in range(count):
        if index % 2 == 0:
            path = corpus.docx_document(directory / ("memo-%02d.docx" % (index + 1)),
                                        paragraphs=12)
        else:
            path = corpus.pptx_deck(directory / ("deck-%02d.pptx" % (index + 1)),
                                    slides=2)
        files.append(path)
    return files


def convert_one(soffice: str, source: Path, out_dir: Path, profile: Path,
                timeout: int) -> float:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    subprocess.run([soffice, "-env:UserInstallation=" + profile.as_uri(),
                    "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
                    "--norestore", "--convert-to", "pdf", "--outdir", str(out_dir),
                    str(source)],
                   capture_output=True, timeout=timeout, check=False)
    return time.monotonic() - started


def convert_batch(soffice: str, sources, out_dir: Path, profile: Path,
                  timeout: int) -> float:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    subprocess.run([soffice, "-env:UserInstallation=" + profile.as_uri(),
                    "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
                    "--norestore", "--convert-to", "pdf", "--outdir", str(out_dir)]
                   + [str(path) for path in sources],
                   capture_output=True, timeout=timeout, check=False)
    return time.monotonic() - started


def check_outputs(sources, out_dir: Path) -> list:
    """Every source produced a readable PDF that still carries its text."""
    from pypdf import PdfReader

    issues = []
    for source in sources:
        target = out_dir / (Path(source).stem + ".pdf")
        if not target.is_file():
            issues.append("%s: no PDF was produced" % Path(source).name)
            continue
        try:
            with open(target, "rb") as handle:
                reader = PdfReader(handle)
                pages = len(reader.pages)
                text = " ".join((page.extract_text() or "") for page in reader.pages)
        except Exception as error:                          # noqa: BLE001 - report it
            issues.append("%s: unreadable PDF (%s)" % (Path(source).name, error))
            continue
        if pages < 1:
            issues.append("%s: PDF has no pages" % Path(source).name)
        marker = "Paragraph 1" if Path(source).suffix == ".docx" else "Synthetic slide"
        if marker.lower() not in text.lower():
            issues.append("%s: PDF does not contain %r" % (Path(source).name, marker))
    return issues


def summary(times):
    if not times:
        return {"median": None, "min": None, "max": None, "spread": None}
    median = statistics.median(times)
    return {"median": round(median, 3), "min": round(min(times), 3),
            "max": round(max(times), 3),
            "spread": round((max(times) - min(times)) / median, 4) if median else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare one-soffice-per-batch with one-per-file")
    parser.add_argument("--files", type=int, default=6)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    if args.files < 1 or args.reps < 1:
        parser.error("--files and --reps must be at least 1")

    soffice = find_renderer()
    if not soffice:
        print("no LibreOffice renderer found: nothing to probe on this host")
        return 1

    work = Path(tempfile.mkdtemp(prefix="chamo-office-probe-"))
    sources = build_inputs(work / "inputs", args.files)
    before = {path.name: sha256_file(path) for path in sources}
    sequential, batched, issues = [], [], []
    try:
        for rep in range(args.reps):
            # Interleaved and alternating: a load spike hits both arms, and the
            # arm that runs first does not systematically pay the cold start.
            if rep % 2 == 0:
                sequential.append(convert_sequential(soffice, sources, work, rep, args.timeout))
                batched.append(convert_batched(soffice, sources, work, rep, args.timeout))
            else:
                batched.append(convert_batched(soffice, sources, work, rep, args.timeout))
                sequential.append(convert_sequential(soffice, sources, work, rep, args.timeout))
        for name in ("sequential", "batched"):
            issues += ["%s: %s" % (name, issue) for issue in
                       check_outputs(sources, work / "out-%s-%02d" % (name, args.reps - 1))]
        after = {path.name: sha256_file(path) for path in sources}
        if before != after:
            issues.append("an input file changed during the probe")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report = {"files": args.files, "reps": args.reps, "soffice": soffice,
              "sequential": summary(sequential), "batched": summary(batched),
              "ratio": None, "issues": issues}
    if report["sequential"]["median"] and report["batched"]["median"]:
        report["ratio"] = round(report["batched"]["median"] / report["sequential"]["median"], 3)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return 1 if issues or not sequential or not batched else 0


def convert_sequential(soffice, sources, work: Path, rep: int, timeout: int) -> float:
    out_dir = work / ("out-sequential-%02d" % rep)
    profile = work / ("profile-sequential-%02d" % rep)
    total = 0.0
    for source in sources:
        total += convert_one(soffice, source, out_dir, profile, timeout)
    return total


def convert_batched(soffice, sources, work: Path, rep: int, timeout: int) -> float:
    out_dir = work / ("out-batched-%02d" % rep)
    profile = work / ("profile-batched-%02d" % rep)
    return convert_batch(soffice, sources, out_dir, profile, timeout)


if __name__ == "__main__":
    sys.exit(main())
