"""The benchmark runner must not report a number it cannot stand behind.

Three things are checked here, because each of them was a way for a benchmark to
lie while looking green:
  * a case whose output is wrong is FAILED and its timing is not adopted;
  * a command that times out is a failure, not a pass without a number;
  * the case names carry the corpus's own page counts, so a holdout report cannot
    be mistaken for a representative one.

The last two tests drive the real script end to end on the smoke corpus. They
assert correctness and plumbing, never a duration: shared runners are not a
benchmark.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load("chamo_bench", "scripts/bench.py")
corpus = _load("chamo_bench_corpus", "scripts/bench_corpus.py")


def test_holdout_cases_are_named_for_their_own_corpus():
    """A "text120" case that runs on a 37-page document is a wrong label."""
    holdout = corpus.SUITES["holdout"]
    representative = corpus.SUITES["representative"]
    names = {case["name"] for case in bench.CASES["holdout"]}
    assert "pdf-images.text%d" % holdout["text_pages"] in names
    assert "pdf-images.scan%d" % holdout["scan_pages"] in names
    assert "pdf-images.text%d" % representative["text_pages"] not in names


def test_a_timed_out_command_is_a_failure(monkeypatch):
    """run_command reports rc=None when it gives up; that must not read as a pass."""
    def fake_run_command(argv, timeout):
        return {"seconds": timeout, "timed_out": True, "rc": None, "stderr": "",
                "output_bytes": 0, "outputs": 0}

    monkeypatch.setattr(bench, "run_command", fake_run_command)
    record = bench.run_case({"name": "engine.probe", "kind": "command", "argv": ["x"]},
                            Path("/nonexistent"), Path("/nonexistent"), 1, 5, False)
    assert record["status"] == "FAIL"
    assert record["timing_adopted"] is False
    assert any("timed out" in issue for issue in record["quality"]["issues"]), record


def test_a_wrong_output_fails_the_case_and_its_timing_is_not_adopted(monkeypatch, tmp_path):
    """The chain "quality finds a problem -> case FAIL -> timing not adopted"."""
    def fake_run_engine(inputs, out_dir, operation, config, timeout):
        return {"seconds": 1.0, "timed_out": False, "rc": 0, "stderr": "",
                "output_bytes": 0, "outputs": 0, "report": {}}

    monkeypatch.setattr(bench, "run_engine", fake_run_engine)
    monkeypatch.setattr(bench, "quality_issues", lambda *args, **kwargs: ["page 1: differs"])
    case = {"name": "pdf-images.text", "operation": "pdf_images", "input": "text.pdf",
            "config": {}, "quality": {}}
    record = bench.run_case(case, tmp_path, tmp_path, 1, 5, False)
    assert record["status"] == "FAIL"
    assert record["timing_adopted"] is False
    assert record["quality"]["issues"]


def test_summarise_reports_median_and_spread_and_ignores_timeouts():
    runs = [{"seconds": 2.0}, {"seconds": 2.2}, {"seconds": 2.4}]
    summary = bench.summarise(runs)
    assert summary["median_seconds"] == 2.2
    assert summary["min_seconds"] == 2.0 and summary["max_seconds"] == 2.4
    assert summary["spread"] == 0.1818
    assert bench.summarise([{"seconds": 9.0, "timed_out": True}])["median_seconds"] is None


def test_summarise_ignores_a_run_that_exited_non_zero():
    """A crashed run measured the crash, not the work; it must not move the median."""
    runs = [{"seconds": 2.0, "rc": 0}, {"seconds": 2.2, "rc": 0},
            {"seconds": 40.0, "rc": 1}]
    summary = bench.summarise(runs)
    assert summary["median_seconds"] == 2.1, summary
    assert summary["max_seconds"] == 2.2, summary


def test_the_smoke_suite_runs_and_reports(tmp_path):
    report_path = tmp_path / "bench.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench.py"), "--suite", "smoke",
         "--reps", "1", "--only", "pdf-images.text", "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["environment"]["CHAMO_PAGE_CACHE"] == "0"
    case = report["cases"][0]
    assert case["name"].startswith("pdf-images.text") and case["status"] == "PASS"
    assert case["timing_adopted"] is True
    assert case["median_seconds"] and case["median_seconds"] > 0
    assert case["quality"]["issues"] == []
    assert report["corpus"]["manifest_sha256"]


def test_the_office_fixtures_really_render(tmp_path):
    """The corpus's .pptx/.docx are real documents, not just bytes.

    Every office benchmark case would silently measure a failed conversion if a
    fixture were malformed, so the smoke suite converts them and requires the
    rendered PDF to carry their words. The .xlsx case is not here: the Linux CI
    image installs LibreOffice Writer and Impress but not Calc, and that is a CI
    environment gap to fix on its own, not a reason for this suite to fail on
    Linux while passing on Windows and macOS.
    """
    report_path = tmp_path / "office.json"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench.py"), "--suite", "smoke",
         "--reps", "1", "--only", "office-pdf", "--json", str(report_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert {case["name"] for case in report["cases"]} == {
        "office-pdf.deck", "office-pdf.memo"}
    for case in report["cases"]:
        assert case["status"] == "PASS", (case["name"], case["quality"]["issues"])


def test_image_quality_takes_lossiness_from_the_run_config():
    """The case table must not be able to declare a run lossy in two places.

    image-compress used to read config["lossy"] for the mode but keep a second
    quality["lossy"] copy that nothing read; a case that set only the second one
    would be judged pixel-exact and could never pass. This pins the decision to
    the run's own config, for both image operations.
    """
    seen = []
    original = bench.quality.check_image_pairs

    def record(pairs, mode="lossless", lossy_target=False):
        seen.append((mode, lossy_target))
        return []

    bench.quality.check_image_pairs = record
    try:
        for operation, config in (("image_compress", {"format": "jpeg", "lossy": True}),
                                  ("image_compress", {"format": "png", "lossy": False}),
                                  ("image_convert", {"format": "jpeg"}),
                                  ("image_convert", {"format": "png"})):
            bench.quality_issues({"operation": operation, "config": config},
                                 ROOT, ROOT, {})
    finally:
        bench.quality.check_image_pairs = original

    assert seen == [("lossy", True), ("lossless", False),
                    ("convert", True), ("convert", False)], seen


def test_a_timed_out_run_really_ends_instead_of_hanging():
    """A killed child that left a grandchild holding the pipe must not block us.

    subprocess.run(timeout=...) kills only the direct child; the grandchild keeps
    the write end of the pipe open and the drain waits for an EOF that never
    comes. The engine does exactly this whenever an office conversion has started
    LibreOffice, so the timeout has to take the whole tree down.
    """
    script = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
              "time.sleep(30)")
    started = time.monotonic()
    result = bench._run([sys.executable, "-c", script], timeout=3)
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert result["rc"] is None
    # The reported seconds equal the timeout by construction, so asserting them
    # proves nothing. The wall clock is what shows the call came back instead of
    # waiting for the grandchild to finish (the test itself would otherwise only
    # be bounded by the outer pytest timeout).
    assert elapsed < 15, "the timeout path took %.1fs" % elapsed


def test_a_run_that_skipped_a_broken_file_is_not_a_pass():
    """The engine exits 0 with a per-file failure; the case must still fail.

    Reproduced by Mac-Claude on the smoke corpus: corrupt one of eight images,
    the engine returns rc=0 with seven recorded outputs and state 'failed' for
    the broken one, and run_case used to report PASS with the timing adopted.
    """
    report = {
        "results": [
            {"state": "complete", "sources": ["ok-1.png"],
             "outputs": [{"path": "o1.png"}]},
            {"state": "failed", "sources": ["broken.png"],
             "outputs": [], "error": "图片无法解码"},
        ]
    }
    problems = bench.unaccounted_sources(report)
    assert len(problems) == 1, problems
    assert "broken.png" in problems[0], problems[0]
    assert "failed" in problems[0], problems[0]


def test_a_cancelled_source_is_also_unaccounted():
    report = {"results": [
        {"state": "cancelled", "sources": ["a.png"],
         "outputs": [], "error": "任务已取消"},
    ]}
    assert bench.unaccounted_sources(report), "cancelled must not count as done"


def test_every_source_completing_is_clean():
    report = {"results": [
        {"state": "complete", "sources": ["a.png"], "outputs": [{"path": "a"}]},
        {"state": "resumed", "sources": ["b.png"], "outputs": [{"path": "b"}]},
    ]}
    assert bench.unaccounted_sources(report) == []


def test_the_child_environment_redirects_state_into_the_run(tmp_path):
    env = bench.child_environment(state_dir=tmp_path)
    assert env["CHAMO_STATE_DIR"] == str(tmp_path), env.get("CHAMO_STATE_DIR")
    assert env["CHAMO_PAGE_CACHE"] == "0"
