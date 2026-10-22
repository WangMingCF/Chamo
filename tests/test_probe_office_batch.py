"""The office batch probe has to be correct before it is fast.

0.5.0's batch conversion is only worth building if one LibreOffice start-up per
batch really beats one per file, and the probe is what decides that. A probe that
silently loses a document, or that reports a timing for an arm which produced
nothing, would send the plan in the wrong direction - so both arms are run for
real here and their outputs are checked.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_office_batch.py"

sys.path.insert(0, str(ROOT / "engine"))


def _probe_module():
    spec = importlib.util.spec_from_file_location("chamo_probe_office_batch", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _office_available() -> bool:
    import platform_office

    return platform_office.find(ROOT / "engine") is not None


def test_a_missing_pdf_is_reported_not_timed(tmp_path):
    """The correctness gate of the probe: no output is an issue, not a cheap run."""
    probe = _probe_module()
    source = tmp_path / "memo.docx"
    source.write_bytes(b"not really a docx")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    issues = probe.check_outputs([source], out_dir)
    assert any("no PDF was produced" in issue for issue in issues), issues


@pytest.mark.skipif(not _office_available(),
                    reason="no LibreOffice renderer on this platform")
def test_the_probe_runs_both_arms_and_their_output_is_correct(tmp_path):
    report_path = tmp_path / "probe.json"
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--files", "2", "--reps", "1",
         "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["issues"] == [], report["issues"]
    assert report["sequential"]["median"] and report["sequential"]["median"] > 0
    assert report["batched"]["median"] and report["batched"]["median"] > 0
    assert report["ratio"] and report["ratio"] > 0
    _publish_numbers(report)


def _publish_numbers(report: dict) -> None:
    """Put the probe's numbers where a person can read them without a failure.

    Two places, because the step summary is only visible on the Actions page while
    the workflow artifact can be downloaded and diffed: the summary for whoever is
    reading the run, and results/office-probe.json for the next comparison.

    The probe exists to produce numbers for a decision, and a number that only
    lives in a temporary directory is not evidence. GitHub stores a step summary
    file per job; when this runs outside Actions the file is simply not there.
    One repetition is not a benchmark - the table says so.
    """
    import os

    # Only in CI: locally this would leave a results/ directory in the checkout,
    # which is exactly the kind of stray artefact a test should not create.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        artifacts = Path("results")
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "office-probe.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    table = (
        "### Office batch probe\n\n"
        "| arm | median s | min s | spread |\n| --- | --- | --- | --- |\n"
        "| one soffice per file | %s | %s | %s |\n"
        "| one soffice per batch | %s | %s | %s |\n\n"
        "files=%s, repetitions=%s, batched/sequential=%s\n\n"
        "> 单次重复，不作为性能结论；只用于确认两条路径都能跑通并给出量级。\n"
        % (report["sequential"]["median"], report["sequential"]["min"],
           report["sequential"]["spread"], report["batched"]["median"],
           report["batched"]["min"], report["batched"]["spread"],
           report["files"], report["reps"], report["ratio"]))
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(table)
    except OSError:
        pass
