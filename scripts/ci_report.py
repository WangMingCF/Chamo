"""Turn pytest JUnit XML from a CI job into a verdict and a GitHub job summary.

A green pytest exit code is not enough on CI: a test that silently skips on the
platform it exists for looks exactly like a pass. This script reads one or more
JUnit files (several when the suite was repeated to look for flaky tests) and
fails the job when:

  * any test failed or errored in every run          -> "failed"
  * a test failed in some runs and passed in others  -> "flaky"
  * a test skipped for a reason not listed for this platform in
    tests/ci_allowed_skips.json                      -> "unexpected skip"
  * no test ran at all                               -> "empty"

The Markdown summary goes to $GITHUB_STEP_SUMMARY (or --summary) and stdout.

    python scripts/ci_report.py --platform linux --skips tests/ci_allowed_skips.json results/*.xml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

PLATFORMS = ("linux", "windows", "macos")
SKIP_TEXT = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<reason>.*)$", re.S)


def load_allowed(path: Path | None, platform: str) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("all", []) + data.get(platform, [])
    for e in entries:
        if not {"file", "reason", "why"} <= e.keys():
            raise SystemExit(f"ci_report: every allowed skip needs file, reason and why: {e}")
    return entries


def read_runs(files: list[Path]) -> list[dict[str, dict]]:
    """One dict per JUnit file: test id -> {outcome, message, seconds, file, reason}."""
    runs = []
    for f in files:
        cases: dict[str, dict] = {}
        for tc in ET.parse(f).getroot().iter("testcase"):
            tid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
            info = {"outcome": "passed", "message": "", "seconds": float(tc.get("time") or 0)}
            for tag in ("failure", "error", "skipped"):
                el = tc.find(tag)
                if el is None:
                    continue
                info["outcome"] = {"failure": "failed", "error": "failed", "skipped": "skipped"}[tag]
                info["message"] = (el.get("message") or "").strip()
                if tag == "skipped":
                    # The text is "<absolute path>:<line>: <reason>"; keep the path
                    # from the repository's tests/ folder so runners agree.
                    m = SKIP_TEXT.match((el.text or "").strip())
                    path = (m.group("file") if m else tc.get("classname", "").replace(".", "/") + ".py").replace("\\", "/")
                    cut = path.rfind("/tests/")
                    info["file"] = path[cut + 1:] if cut >= 0 else path
                    info["reason"] = info["message"] or (m.group("reason").strip() if m else "")
                break
            cases[tid] = info
        runs.append(cases)
    return runs


def analyse(runs: list[dict[str, dict]], allowed: list[dict]) -> dict:
    outcomes: dict[str, list[str]] = defaultdict(list)
    for run in runs:
        for tid, info in run.items():
            outcomes[tid].append(info["outcome"])
    failed = sorted(t for t, o in outcomes.items() if o and all(x == "failed" for x in o))
    flaky = sorted(t for t, o in outcomes.items() if "failed" in o and any(x != "failed" for x in o))

    unexpected, allowed_hits = [], 0
    for run in runs:
        for tid, info in run.items():
            if info["outcome"] != "skipped" or info["reason"].startswith("xfail"):
                continue
            if any(e["file"] == info["file"] and e["reason"] == info["reason"] for e in allowed):
                allowed_hits += 1
            else:
                unexpected.append((tid, info["file"], info["reason"]))
    unexpected = sorted(set(unexpected))

    messages = {}
    for run in runs:
        for tid, info in run.items():
            if info["outcome"] == "failed" and tid not in messages:
                messages[tid] = info["message"].splitlines()[0][:200] if info["message"] else ""
    seconds: dict[str, float] = defaultdict(float)
    for run in runs:
        for tid, info in run.items():
            seconds[tid] = max(seconds[tid], info["seconds"])
    return {"failed": failed, "flaky": flaky, "unexpected": unexpected, "allowed_hits": allowed_hits,
            "messages": messages, "slowest": sorted(seconds.items(), key=lambda kv: -kv[1])[:8],
            "empty": not outcomes}


def render(title: str, runs: list[dict[str, dict]], result: dict) -> str:
    ok = not (result["failed"] or result["flaky"] or result["unexpected"] or result["empty"])
    lines = [f"### {'✅' if ok else '❌'} {title}", "",
             "| 第几次运行 | 通过 | 失败 | 跳过 | 用时（秒） |", "| --- | --- | --- | --- | --- |"]
    for i, run in enumerate(runs, 1):
        count = defaultdict(int)
        for info in run.values():
            count[info["outcome"]] += 1
        total = sum(info["seconds"] for info in run.values())
        lines.append(f"| {i} | {count['passed']} | {count['failed']} | {count['skipped']} | {total:.0f} |")
    if result["empty"]:
        lines += ["", "**没有任何测试运行**：空结果不算通过。"]
    for key, label in (("failed", "失败（每次运行都失败）"), ("flaky", "不稳定（有的运行失败、有的通过）")):
        if result[key]:
            lines += ["", f"**{label}**", ""]
            lines += [f"- `{t}` {result['messages'].get(t, '')}" for t in result[key]]
    if result["unexpected"]:
        lines += ["", "**未登记的跳过**（跳过不等于通过；确属合理的，登记到 `tests/ci_allowed_skips.json` 并写明原因）", ""]
        lines += [f"- `{t}` — {r}" for t, _f, r in result["unexpected"]]
    lines += ["", f"已登记的跳过：{result['allowed_hits']} 次。", "",
              "<details><summary>最慢的测试</summary>", ""]
    lines += [f"- {s:.1f}s `{t}`" for t, s in result["slowest"]]
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", required=True, choices=PLATFORMS)
    ap.add_argument("--skips", type=Path, help="allowed-skips JSON (tests/ci_allowed_skips.json)")
    ap.add_argument("--title", default="")
    ap.add_argument("--summary", type=Path, default=os.environ.get("GITHUB_STEP_SUMMARY") or None)
    ap.add_argument("xml", nargs="+", type=Path)
    a = ap.parse_args()

    present = [p for p in a.xml if p.is_file()]
    runs = read_runs(present)
    result = analyse(runs, load_allowed(a.skips, a.platform))
    text = render(a.title or f"测试结果（{a.platform}）", runs, result)
    print(text)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if (result["failed"] or result["flaky"] or result["unexpected"] or result["empty"]) else 0


if __name__ == "__main__":
    sys.exit(main())
