"""The classifier probe exists to settle a hypothesis, so it must not lie.

Issue #10 asks whether the 5-6% gap in the macOS text-timing gate can be the
cost of classifying the document. The probe answers with two numbers measured on
the same file, and with the classifier's own verdict: if it does not call the
document "text", the probe is measuring something else and says so instead of
reporting a ratio.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_text_classify.py"


def test_the_probe_measures_both_sides_and_publishes_the_numbers(tmp_path):
    report_path = tmp_path / "classify.json"
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--pages", "20", "--reps", "2",
         "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "text", report
    assert report["problems"] == [], report["problems"]
    assert report["classify"]["median"] > 0 and report["render"]["median"] > 0
    # The claim under test is that the classifier is a small fraction of the render.
    # The probe reports the ratio; this test only requires that it is a fraction of
    # the work at all, which the hypothesis needs to be worth discussing.
    assert report["ratio"] is not None and report["ratio"] < 1.0, report
    _publish(report)


def _publish(report: dict) -> None:
    """Numbers nobody can read are not evidence: surface them where people look."""
    import os

    text = ("### Text classifier cost probe\n\n"
            "| measurement | median s | min s | max s |\n| --- | --- | --- | --- |\n"
            "| classify (page objects) | %s | %s | %s |\n"
            "| render %s pages at %s dpi | %s | %s | %s |\n\n"
            "classify/render = **%s** (pages=%s, repetitions=%s)\n\n"
            "> 单次 CI 运行的数字，只用于判断「判定成本能不能解释 5%% 的差值」，"
            "不作为性能结论。\n"
            % (report["classify"]["median"], report["classify"]["min"],
               report["classify"]["max"], report["pages"], report["dpi"],
               report["render"]["median"], report["render"]["min"],
               report["render"]["max"], report["ratio"], report["pages"], report["reps"]))
    artifacts = Path("results")
    if artifacts.is_dir():
        (artifacts / "text-classify-probe.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
