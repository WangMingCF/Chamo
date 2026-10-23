#!/usr/bin/env python3
"""Mutation check for the grandchild process-tree gate.

The gate is only worth something if removing tree semantics turns it red, so
this script copies engine/platform_process.py, applies one defect at a time, and
runs tests/test_process_tree_grandchild.py against the copy through the
CHAMO_PLATFORM_PROCESS_DIR hook. The repository's engine/ is never modified.

Mutations:
  tree_control_direct_child_only
      pause AND terminate/kill touch only the direct child. On POSIX the signal
      goes to proc.pid instead of the process group; on Windows the kill-on-close
      Job Object is never closed and suspend enumerates only the worker PID.
      This is how Windows behaved before the Job Object fix (measured on a
      macOS proxy: grandchild counter 1 -> 18 -> 69, difference_detected: true).
  pause_direct_child_only
      only pause/resume miss the tree; terminate/kill still reach it. Proves the
      PAUSE assertion fails on its own rather than only riding on terminate.

For every mutation the script checks:
  1. the unmutated copy makes the gate PASS,
  2. the mutated copy makes the gate FAIL,
  3. the failure output names the expected defect.
Exit code 0 only when all mutations were caught and the baseline is green.

Usage:
  ./third_party/python/bin/python3 scripts/process_tree_mutation_check.py
  ./third_party/python/bin/python3 scripts/process_tree_mutation_check.py --only pause_direct_child_only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_TEST = ROOT / "tests" / "test_process_tree_grandchild.py"

MUTATIONS = [
    {
        "name": "tree_control_direct_child_only",
        "why": "pause and terminate/kill reach only the direct child (behaviour before the Job Object fix)",
        "expect": "kept running while the tree was paused",
        "patches": [
            # POSIX: signal the worker PID, not its process group.
            ("_POSIX_SIGNAL_GROUP",
             "        os.killpg(os.getpgid(proc.pid), sig)",
             "        proc.send_signal(sig)  # MUTATION: direct child only"),
            # Windows: never close the kill-on-close Job Object.
            ("_WINDOWS_TERMINATE",
             "    # shard renderers a worker spawns for a large image-dominant PDF.\n"
             "    close_job_once(proc)\n",
             "    # shard renderers a worker spawns for a large image-dominant PDF.\n"
             "    pass  # MUTATION: never close the job (direct child only)\n"),
            # Windows: suspend/resume the worker PID only.
            ("_WINDOWS_APPLY",
             "    pids = _job_pids(job) if job else []\n    if not pids:\n        pids = [proc.pid]",
             "    pids = [proc.pid]  # MUTATION: direct child only"),
        ],
    },
    {
        "name": "pause_direct_child_only",
        "why": "only pause/resume miss the tree; terminate/kill still reach it",
        "expect": "kept running while the tree was paused",
        "patches": [
            # POSIX: pause the worker PID instead of the process group.
            ("_POSIX_PAUSE",
             "        _signal_group(proc, signal.SIGSTOP)",
             "        proc.send_signal(signal.SIGSTOP)  # MUTATION: direct child only"),
            # Windows: suspend/resume the worker PID only.
            ("_WINDOWS_APPLY",
             "    pids = _job_pids(job) if job else []\n    if not pids:\n        pids = [proc.pid]",
             "    pids = [proc.pid]  # MUTATION: direct child only"),
        ],
    },
]


def apply_mutation(source: Path, target: Path, mutation: dict) -> None:
    """Copy source to target with every patch applied exactly once."""
    text = source.read_text(encoding="utf-8")
    for label, old, new in mutation["patches"]:
        count = text.count(old)
        if count != 1:
            raise SystemExit("mutation %s (%s): expected exactly 1 occurrence, found %d"
                             % (mutation["name"], label, count))
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")


def run_gate(module_dir: Path) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["CHAMO_PLATFORM_PROCESS_DIR"] = str(module_dir)
    try:
        import pytest  # noqa: F401
    except Exception:
        pass
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(GATE_TEST), "-q", "-rf", "--tb=short",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment, cwd=str(ROOT), timeout=600)


def tail(result: subprocess.CompletedProcess) -> str:
    lines = (result.stdout or "").splitlines()
    return "\n".join(lines[-18:])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="process_tree_mutation_check.py",
        description="Prove the grandchild tree-control gate can fail.")
    parser.add_argument("--only", default="",
                        help="run only this mutation (%s)"
                             % ", ".join(item["name"] for item in MUTATIONS))
    parser.add_argument("--json", default="", help="write a machine-readable report here")
    args = parser.parse_args(argv)
    selected = MUTATIONS
    if args.only:
        selected = [item for item in MUTATIONS if item["name"] == args.only]
        if not selected:
            print("unknown mutation %r; choose from %s"
                  % (args.only, ", ".join(item["name"] for item in MUTATIONS)),
                  file=sys.stderr)
            return 2

    work = Path(tempfile.mkdtemp(prefix="dw-tree-mutations-"))
    source = ROOT / "engine" / "platform_process.py"
    results = []
    print("source module: %s" % source, flush=True)
    print("the repository's engine/ is never modified by this script.\n", flush=True)

    # 1. Baseline: an unmutated COPY must pass the gate.
    baseline_dir = work / "baseline"
    baseline_dir.mkdir()
    shutil.copy2(source, baseline_dir / "platform_process.py")
    baseline = run_gate(baseline_dir)
    baseline_ok = baseline.returncode == 0
    print("== baseline (unmutated copy) ==", flush=True)
    print(tail(baseline), flush=True)
    print("[%s] baseline gate must PASS -> %s\n"
          % ("OK" if baseline_ok else "BROKEN", "PASS" if baseline_ok else "NOT PASS"),
          flush=True)

    # 2. Mutants must fail the gate with the expected defect named.
    caught_all = True
    for mutation in selected:
        print("-- mutation %s: %s" % (mutation["name"], mutation["why"]), flush=True)
        mutant_dir = work / ("mutant-" + mutation["name"])
        mutant_dir.mkdir()
        apply_mutation(source, mutant_dir / "platform_process.py", mutation)
        for label, old, _new in mutation["patches"]:
            print("   patch %s: %r" % (label, old.strip()[:110]), flush=True)
        result = run_gate(mutant_dir)
        failed = result.returncode != 0
        named = mutation["expect"] in (result.stdout or "")
        caught = failed and named
        caught_all = caught_all and caught
        print(tail(result), flush=True)
        print("   -> %s (expected failure text %r; pytest rc=%s)"
              % ("CAUGHT (gate went red)" if caught else "NOT CAUGHT",
                 mutation["expect"], result.returncode), flush=True)
        failing_tests = [line for line in (result.stdout or "").splitlines()
                         if line.startswith("FAILED ")]
        results.append({
            "mutation": mutation["name"], "why": mutation["why"],
            "patches": [label for label, _old, _new in mutation["patches"]],
            "expect": mutation["expect"], "caught": caught,
            "pytest_rc": result.returncode,
            "failing_tests": failing_tests,
        })
        print(flush=True)

    print("RESULT: %s - %d/%d mutations caught, baseline %s"
          % ("PASS" if caught_all and baseline_ok else "FAIL",
             sum(1 for item in results if item["caught"]), len(results),
             "green" if baseline_ok else "NOT green"), flush=True)
    print("work directory: %s" % work, flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "baseline_green": baseline_ok,
            "baseline_pytest_rc": baseline.returncode,
            "results": results, "caught_all": caught_all,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("report: %s" % args.json, flush=True)
    return 0 if caught_all and baseline_ok else 1


if __name__ == "__main__":
    sys.exit(main())
