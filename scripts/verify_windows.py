#!/usr/bin/env python3
"""Verify the built Windows package by extracting and converting with it.

Run with the package interpreter, so the checks also prove the bundled wheels are
complete:

    <pkg>\\python\\python.exe scripts/verify_windows.py --package <pkg>

Every claim is checked against the produced files (pages, sizes, exit codes), not
against the exit status alone. A package that converts nothing would pass a
status-only check, which is how the Linux job first failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

FAILURES = []


def check(name: str, actual, expected) -> None:
    if actual == expected:
        print("PASS %s (%s)" % (name, actual))
    else:
        print("FAIL %s: got %r want %r" % (name, actual, expected))
        FAILURES.append(name)


def chamo(pkg: Path, *args) -> int:
    """Run the packaged launcher and return its exit code."""
    result = subprocess.run([str(pkg / "chamo.cmd"), *args],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode not in (0, 1, 2, 3, 4):
        print("unexpected exit %d for %s" % (result.returncode, args))
        print((result.stdout + result.stderr)[-1500:])
    return result.returncode


def chamo_capture(pkg: Path, *args):
    """Run the packaged launcher and return (exit code, combined output)."""
    result = subprocess.run([str(pkg / "chamo.cmd"), *args],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode not in (0, 1, 2, 3, 4):
        print("unexpected exit %d for %s" % (result.returncode, args))
        print((result.stdout + result.stderr)[-1500:])
    return result.returncode, (result.stdout or "")


def pinned_version(distribution: str) -> str:
    """The version pinned in scripts/engine_deps.py, the list both packagers install."""
    text = (ROOT / "scripts" / "engine_deps.py").read_text(encoding="utf-8")
    match = re.search(r'"' + re.escape(distribution) + r'==([0-9][^"]*)"', text)
    return match.group(1) if match else ""


def page_count(pdf: Path) -> int:
    import pypdf
    return len(pypdf.PdfReader(str(pdf)).pages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    args = parser.parse_args()
    pkg = Path(args.package).resolve()

    check("launcher present", (pkg / "chamo.cmd").is_file(), True)
    check("engine present", (pkg / "engine" / "chamo.py").is_file(), True)

    docs = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_docs_version.py"),
         "--packaged-guide", str(pkg / "USER_GUIDE.txt"),
         "--packaged-notes", str(pkg / "RELEASE_NOTES.txt")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(docs.stdout[-1500:], flush=True)
    if docs.stderr:
        print(docs.stderr[-500:], flush=True)
    check("packaged documents name this version", docs.returncode, 0)

    work = Path(tempfile.mkdtemp(prefix="chamo-verify-"))

    out = work / "pdf"
    code = chamo(pkg, str(FIXTURES / "示例讲义.pdf"), "-o", str(out),
               "-t", "pdf-images", "--range", "1-3", "--quiet")
    check("pdf to images exit", code, 0)
    pngs = sorted(out.rglob("*.png"))
    check("rendered pages", len(pngs), 3)
    if pngs:
        from PIL import Image
        with Image.open(pngs[0]) as im:
            check("first page has size", im.size[0] > 100 and im.size[1] > 100, True)
            check("first page is not blank", len(im.tobytes()) > 0, True)

    out = work / "ipdf"
    code = chamo(pkg, str(FIXTURES / "透明图片.png"), "-o", str(out),
               "-t", "images-pdf", "--quiet")
    check("images to pdf exit", code, 0)
    pdfs = sorted(out.rglob("*.pdf"))
    check("merged pdf created", len(pdfs), 1)
    if pdfs:
        check("merged pdf pages", page_count(pdfs[0]), 1)

    out = work / "rag"
    code = chamo(pkg, str(FIXTURES / "示例讲义.pdf"), "-o", str(out), "-t", "rag", "--quiet")
    check("rag preprocess exit", code, 0)

    # PDF compression: the packaged interpreter must carry pikepdf (installed
    # from scripts/engine_deps.py) and the lossless rewrite must publish the product.
    # A source-tree-only run would import the developer's pikepdf and stay green while
    # the ZIP's pdf_compress failed with ImportError.
    probe = subprocess.run(
        [str(pkg / "python" / "python.exe"), "-c",
         "import pikepdf;print(pikepdf.__version__)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("pikepdf importable in package", probe.returncode, 0)
    check("pikepdf version matches engine_deps", (probe.stdout or "").strip(),
          pinned_version("pikepdf"))
    compress_out = work / "compress"
    code, stream = chamo_capture(
        pkg, str(FIXTURES / "示例讲义.pdf"), "-o", str(compress_out),
        "-t", "pdf-compress", "--json")
    check("pdf compress lossless exit", code, 0)
    compress_products = (sorted(path.name for path in compress_out.glob("*_压缩.pdf"))
                         if compress_out.is_dir() else [])
    check("pdf compress product name", compress_products, ["示例讲义_压缩.pdf"])
    if compress_products:
        product = compress_out / compress_products[0]
        source = FIXTURES / "示例讲义.pdf"
        check("pdf compress pages", page_count(product), 6)
        check("pdf compress not larger",
              product.stat().st_size <= source.stat().st_size, True)
        events = [json.loads(line) for line in stream.splitlines()
                  if line.strip().startswith("{")]
        scan = next((event for event in events if event.get("type") == "scan"), None)
        finished = next((event for event in reversed(events)
                         if event.get("type") == "finished"), None)
        check("pdf compress finished state", (finished or {}).get("state"), "complete")
        record = {}
        if scan and scan.get("report"):
            report_path = Path(scan["report"]) / "report.json"
            if report_path.is_file():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                record = (report.get("results") or [{}])[0]
        info = record.get("compress") or {}
        check("pdf compress byte record",
              (info.get("source_bytes"), info.get("output_bytes")),
              (source.stat().st_size, product.stat().st_size))
        check("pdf compress lossless fidelity",
              record.get("fidelity") != "lossy_user_selected", True)

    code = chamo(pkg, str(FIXTURES / "透明图片.png"), "-o", str(work / "u"),
               "-t", "image-convert", "--dpi", "9999", "--quiet")
    check("usage error exit", code, 2)
    code = chamo(pkg, str(work / "missing.pdf"), "-o", str(work / "m"),
               "-t", "pdf-images", "--quiet")
    check("missing input exit", code, 2)

    # A path with spaces and non-ASCII characters, which is where quoting bugs show
    # up on Windows: the launcher passes "%HERE%" and %* through cmd.exe.
    spaced = work / "输出 目录" / "with space"
    code = chamo(pkg, str(FIXTURES / "透明图片.png"), "-o", str(spaced),
               "-t", "image-convert", "--quiet")
    check("non-ascii output path exit", code, 0)
    check("non-ascii output produced", len(list(spaced.rglob("*.png"))) >= 1, True)

    # LibreOffice is not bundled with the Windows package, so two paths matter and
    # both must be real. Accepting "0 or 1" only proved the degrade path: a package
    # whose discovery is broken passed while Office conversion was never exercised.
    office = work / "office"
    probe = subprocess.run(
        [str(pkg / "python" / "python.exe"), "-c",
         "import sys;sys.path.insert(0,r'%s');import platform_office;print(platform_office.find() or '')"
         % (pkg / "engine")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    # A probe that itself failed (missing interpreter, import error) must not be read
    # as "no renderer installed" - that would silently turn this into the degrade check.
    check("renderer probe ran", probe.returncode, 0)
    renderer = (probe.stdout or "").strip().splitlines()[-1] if probe.stdout and probe.stdout.strip() else ""
    print("renderer: %s" % (renderer or "not found"))
    code = chamo(pkg, str(FIXTURES / "示例课件.pptx"), "-o", str(office),
               "-t", "office-pdf", "--quiet")
    if renderer:
        check("office converts with the installed renderer", code, 0)
        check("office output produced", len(list(office.rglob("*.pdf"))), 1)
    else:
        # The explicit missing-renderer path: a clear failure, and no half output.
        check("office fails clearly without a renderer", code, 1)
        check("office produced nothing without a renderer", len(list(office.rglob("*.pdf"))), 0)

    # Sharding runs the engine a second time per shard (`python -I -B engine/workbench.py
    # --shard`), which a pruned embeddable runtime or a launcher that mangles
    # arguments would break while every check above still passed. Run the shared
    # packaged-artifact acceptance so the ZIP is verified, not just built.
    print("-- packaged sharding acceptance --", flush=True)
    shard = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_package_shard.py"),
         "--chamo", str(pkg / "chamo.cmd"), "--work", str(work / "shard")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(shard.stdout[-3000:], flush=True)
    if shard.stderr:
        print(shard.stderr[-1000:], flush=True)
    check("packaged sharding acceptance", shard.returncode, 0)

    # Page-level resume adds cache writes/renames on Windows; cancel/crash
    # gates print SKIP there (POSIX signals) but a complete run, cache-hit rerun and
    # byte equivalence still run, which is what a Windows user path depends on.
    print("-- packaged page-resume acceptance --", flush=True)
    resume = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_page_resume.py"),
         "--chamo", str(pkg / "chamo.cmd"), "--work", str(work / "page-resume")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(resume.stdout[-3000:], flush=True)
    if resume.stderr:
        print(resume.stderr[-1000:], flush=True)
    check("packaged page-resume acceptance", resume.returncode, 0)

    print()
    if FAILURES:
        print("Windows package FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("Windows package verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
