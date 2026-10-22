"""The classifier probe exists to settle a hypothesis, so it must measure honestly.

Issue #10 asks whether the 5-6% gap in the macOS text-timing gate can be the cost
of classifying the document. The probe answers with two numbers measured on the
same file, on two clocks, and with the classifier's own verdict.

These tests check that the measurement is valid and published - not that the
answer comes out a particular way. The ratio is a result of running the probe,
not a property CI should enforce: a change to the classifier or to the page setup
is allowed to move it, and a probe that fails CI because its own answer changed
would be a gate, not a measurement.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe_text_classify.py"


def test_the_probe_measures_both_clocks_and_publishes_the_numbers(tmp_path):
    report_path = tmp_path / "classify.json"
    completed = subprocess.run(
        [sys.executable, str(PROBE), "--pages", "20", "--reps", "3",
         "--iterations", "20", "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["kind"] == "text", report
    assert report["problems"] == [], report["problems"]
    # Averaged readings on both sides: this is what makes the number survive a
    # 15.6 ms process clock on Windows.
    assert report["classify"]["cpu"]["median"] > 0
    assert report["render"]["cpu"]["median"] > 0
    assert report["ratio"]["cpu"] is not None
    assert report["ratio"]["wall"] is not None
    _publish(report)


def _publish(report: dict) -> None:
    """Numbers nobody can read are not evidence: surface them where people look."""
    import os

    table = ("### Text classifier cost probe\n\n"
             "| measurement | clock | median s | min s | max s |\n"
             "| --- | --- | --- | --- | --- |\n"
             "| classify (page objects) | cpu | %s | %s | %s |\n"
             "| classify (page objects) | wall | %s | %s | %s |\n"
             "| render %s pages @ %s dpi | cpu | %s | %s | %s |\n"
             "| render %s pages @ %s dpi | wall | %s | %s | %s |\n\n"
             "classify/render = **cpu %s**, **wall %s** "
             "(pages=%s, repetitions=%s, iterations=%s)\n\n"
             "> 单次 CI 运行的数字，只用于判断「判定成本能不能解释 5%% 的差值」，"
             "不作为性能结论。门禁判的是 CPU 秒，所以 cpu 那一列才是同口径。\n"
             % (report["classify"]["cpu"]["median"], report["classify"]["cpu"]["min"],
                report["classify"]["cpu"]["max"], report["classify"]["wall"]["median"],
                report["classify"]["wall"]["min"], report["classify"]["wall"]["max"],
                report["pages"], report["dpi"], report["render"]["cpu"]["median"],
                report["render"]["cpu"]["min"], report["render"]["cpu"]["max"],
                report["pages"], report["dpi"], report["render"]["wall"]["median"],
                report["render"]["wall"]["min"], report["render"]["wall"]["max"],
                report["ratio"]["cpu"], report["ratio"]["wall"], report["pages"],
                report["reps"], report["iterations"]))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        artifacts = Path("results")
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "text-classify-probe.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(table)
    except OSError as error:
        # Do not swallow it: a summary that was not written must be visible.
        print("could not append to GITHUB_STEP_SUMMARY (%s); table follows:" % error)
        print(table)
