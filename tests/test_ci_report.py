"""scripts/ci_report.py decides whether a CI job passed; check its verdicts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "scripts" / "ci_report.py"


def junit(path: Path, cases: list[tuple[str, str, str]]) -> Path:
    """cases: (name, outcome, skip text or failure message)."""
    body = []
    for name, outcome, text in cases:
        inner = ""
        if outcome == "failed":
            inner = f'<failure message="{text}">trace</failure>'
        elif outcome == "skipped":
            reason = text.split(": ", 1)[1]
            inner = f'<skipped type="pytest.skip" message="{reason}">{text}</skipped>'
        body.append(f'<testcase classname="tests.test_x" name="{name}" time="0.5">{inner}</testcase>')
    path.write_text('<?xml version="1.0"?><testsuites><testsuite name="pytest">'
                    + "".join(body) + "</testsuite></testsuites>", encoding="utf-8")
    return path


def run(tmp: Path, platform: str, *xml: Path, skips: dict | None = None):
    args = [sys.executable, str(REPORT), "--platform", platform, "--summary", str(tmp / "summary.md")]
    if skips is not None:
        (tmp / "skips.json").write_text(json.dumps(skips), encoding="utf-8")
        args += ["--skips", str(tmp / "skips.json")]
    return subprocess.run(args + [str(x) for x in xml], capture_output=True, text=True, encoding="utf-8")


ALLOW = {"windows": [{"file": "tests/test_x.py", "reason": "POSIX only", "why": "signals"}]}


def test_all_passing_is_green_and_writes_the_summary(tmp_path):
    p = run(tmp_path, "linux", junit(tmp_path / "a.xml", [("t1", "passed", ""), ("t2", "passed", "")]))
    assert p.returncode == 0
    assert "✅" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_failure_fails_the_job(tmp_path):
    p = run(tmp_path, "linux", junit(tmp_path / "a.xml", [("t1", "failed", "boom")]))
    assert p.returncode == 1 and "t1" in p.stdout and "boom" in p.stdout


def test_failure_in_one_of_several_runs_is_reported_as_flaky(tmp_path):
    a = junit(tmp_path / "a.xml", [("t1", "passed", "")])
    b = junit(tmp_path / "b.xml", [("t1", "failed", "race")])
    p = run(tmp_path, "macos", a, b)
    assert p.returncode == 1 and "不稳定" in p.stdout


def test_skip_must_be_registered_for_that_platform(tmp_path):
    xml = junit(tmp_path / "a.xml", [("t1", "skipped", r"D:\a\chamo\chamo\tests\test_x.py:12: POSIX only")])
    assert run(tmp_path, "windows", xml, skips=ALLOW).returncode == 0
    p = run(tmp_path, "linux", xml, skips=ALLOW)
    assert p.returncode == 1 and "未登记的跳过" in p.stdout


def test_a_different_reason_is_not_covered_by_the_entry(tmp_path):
    xml = junit(tmp_path / "a.xml", [("t1", "skipped", "/w/tests/test_x.py:12: dependency missing")])
    assert run(tmp_path, "windows", xml, skips=ALLOW).returncode == 1


def test_prefix_entry_matches_reasons_with_measured_values(tmp_path):
    xml = junit(tmp_path / "a.xml", [("t1", "skipped", "/w/tests/test_x.py:9: gate inconclusive: ratio 0.99x")])
    allow = {"linux": [{"file": "tests/test_x.py", "reason_prefix": "gate inconclusive: ", "why": "noise"}]}
    assert run(tmp_path, "linux", xml, skips=allow).returncode == 0
    allow["linux"][0]["reason_prefix"] = "other: "
    assert run(tmp_path, "linux", xml, skips=allow).returncode == 1


def test_no_tests_is_not_a_pass(tmp_path):
    assert run(tmp_path, "linux", junit(tmp_path / "a.xml", [])).returncode == 1
    assert run(tmp_path, "linux", tmp_path / "missing.xml").returncode == 1


def test_allowlist_entries_need_a_justification(tmp_path):
    xml = junit(tmp_path / "a.xml", [("t1", "passed", "")])
    p = run(tmp_path, "linux", xml, skips={"linux": [{"file": "tests/test_x.py", "reason": "r"}]})
    assert p.returncode != 0 and "why" in p.stderr


def test_the_repository_allowlist_is_well_formed():
    data = json.loads((ROOT / "tests" / "ci_allowed_skips.json").read_text(encoding="utf-8"))
    for platform in ("all", "linux", "windows", "macos"):
        for e in data[platform]:
            assert e["file"].startswith("tests/") and (ROOT / e["file"]).is_file(), e
            assert (e.get("reason") or e.get("reason_prefix")) and e["why"], e
