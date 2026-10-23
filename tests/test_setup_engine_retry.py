"""The LibreOffice install step must survive both failure and hanging.

Two shapes of outage were measured on the same upstream: a quick HTTP 500, and a
download that hung for 30-40 minutes with no output, leaving the ui job sitting in
setup-engine until the runner cancelled it (PR #44 a72b9e5, PR #47 a240980).

A retry loop alone only handles the first shape: when the command hangs it never
returns, so the loop never reaches its next iteration and behaves exactly like no
retry at all. These tests run the step's actual shell body against a stub apt-get
that can fail, hang or succeed.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# These tests run the step's real shell body under bash, which is how the Linux
# runner executes it. Windows uses pwsh, and macOS has no GNU timeout(1), so the
# same body cannot be exercised there: the Linux behaviour is covered on the Linux
# job, while the Windows and macOS bounds are asserted from the action source by
# the last two tests.
# The behavioural tests execute the LINUX step body under bash: they stub apt-get,
# sudo and sleep, and they assert on /usr/bin/soffice. That body is Linux-specific,
# so they run on the Linux job only. Windows runs the pwsh variant, and macOS runs a
# bash variant with no GNU timeout(1); both bounds are asserted from the action
# source by the source-level tests, which are platform independent.
LINUX_ONLY = pytest.mark.skipif(
    platform.system() != "Linux",
    reason="executes the Linux step body; other platforms assert the action source instead")
ACTION = ROOT / ".github" / "actions" / "setup-engine" / "action.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "cross-platform.yml"


def linux_step_body() -> str:
    """The run: block of the Linux LibreOffice step, read from the action."""
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "- name: Install LibreOffice (Linux)":
            start = index
            break
    assert start is not None, "the Linux LibreOffice step is missing"
    body = []
    in_run = False
    for line in lines[start + 1:]:
        if line.strip().startswith("- name: Install LibreOffice (Windows)"):
            break
        if line.strip() == "run: |":
            in_run = True
            continue
        if in_run:
            body.append(line)
    assert body, "the Linux step has no run: block"
    indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return textwrap.dedent("\n".join(line[indent:] for line in body))


def run_step(tmp_path: Path, stub: str, installs_soffice: bool = True,
             extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the step body with apt-get, sudo and sleep replaced by scripted stubs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "apt-get").write_text(stub, encoding="utf-8")
    (bin_dir / "apt-get").chmod(0o755)
    # sudo is dropped so the test does not need root; timeout(1) stays real, so the
    # bound is exercised exactly as it is in CI.
    (bin_dir / "sudo").write_text("#!/bin/sh\nshift 0\nexec \"$@\"\n", encoding="utf-8")
    (bin_dir / "sudo").chmod(0o755)
    # Only the retry backoff is instant. A deliberate long sleep must stay real,
    # otherwise the hang scenario would not hang and the timeout would have
    # nothing to kill - which would make the test prove nothing.
    (bin_dir / "sleep").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" -gt 300 ] 2>/dev/null; then exec /bin/sleep \"$1\"; fi\n"
        "exit 0\n", encoding="utf-8")
    (bin_dir / "sleep").chmod(0o755)
    soffice = tmp_path / "soffice"
    if installs_soffice:
        soffice.write_text("#!/bin/sh\n", encoding="utf-8")
        soffice.chmod(0o755)
    body = linux_step_body().replace("/usr/bin/soffice", str(soffice))
    environment = dict(os.environ, PATH=bin_dir.as_posix() + ":" + os.environ["PATH"],
                       APT_TIMEOUT="10", APT_STALL="2", APT_RETRIES="1",
                       APT_BUDGET="30",
                       LOG=str(tmp_path / "calls.log"))
    environment.update(extra_env or {})
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                          env=environment, timeout=120)


def attempts(tmp_path: Path) -> int:
    log = tmp_path / "calls.log"
    if not log.exists():
        return 0
    return len([line for line in log.read_text().splitlines() if line.strip()])


NEWLINE = chr(10)
HEADER = "#!/bin/sh\n"
RECORD = 'echo "$*" >> "$LOG"\n'
SUCCESS = HEADER + RECORD + "exit 0\n"
FAIL_THEN_SUCCEED = (HEADER + RECORD + 'count=$(wc -l < "$LOG")\n'
                     + 'if [ "$count" -le 2 ]; then exit 100; fi\n' + "exit 0\n")
HANG_THEN_SUCCEED = (HEADER + RECORD + 'count=$(wc -l < "$LOG")\n'
                     + 'if [ "$count" -le 2 ]; then sleep 600; fi\n' + "exit 0\n")
ALWAYS_FAIL = HEADER + RECORD + "exit 100\n"


@LINUX_ONLY
def test_a_healthy_mirror_installs_on_the_first_attempt(tmp_path):
    result = run_step(tmp_path, SUCCESS)
    assert result.returncode == 0, result.stderr
    assert attempts(tmp_path) == 2, attempts(tmp_path)


@LINUX_ONLY
def test_a_quick_failure_is_retried_and_then_succeeds(tmp_path):
    result = run_step(tmp_path, FAIL_THEN_SUCCEED)
    assert result.returncode == 0, result.stderr
    assert attempts(tmp_path) >= 4, attempts(tmp_path)


@LINUX_ONLY
def test_a_hanging_attempt_is_bounded_and_then_retried(tmp_path):
    """The shape that broke the ui job: the command never returns."""
    result = run_step(tmp_path, HANG_THEN_SUCCEED)
    assert result.returncode == 0, result.stderr
    assert attempts(tmp_path) >= 4, attempts(tmp_path)


@LINUX_ONLY
def test_three_failures_still_fail_the_step(tmp_path):
    """Retrying must not hide a real breakage."""
    result = run_step(tmp_path, ALWAYS_FAIL, installs_soffice=False)
    assert result.returncode != 0
    assert "soffice missing after 3 attempts" in (result.stdout + result.stderr)


def test_every_attempt_is_bounded_in_the_action_source():
    """A guard so the timeout cannot be dropped again without a failing test."""
    body = linux_step_body()
    # The bound must be a STALL timeout, not a total-time cap: a total cap killed
    # a slow-but-progressing download and turned it into a red job.
    assert "Acquire::http::Timeout" in body, "apt needs an inactivity timeout"
    assert "APT_STALL" in body and "APT_RETRIES" in body
    # A per-attempt cap is not a bound on the step: three attempts plus backoff can
    # far exceed the intended limit, and the engine/package jobs carry no
    # timeout-minutes. A whole-step budget must exist and be enforced.
    assert "APT_BUDGET" in body and "deadline" in body, "the step needs a total budget"
    assert "budget of" in body, "an exhausted budget must be reported, not silently looped"
    source = ACTION.read_text(encoding="utf-8")
    assert "WaitForExit" in source, "the Windows attempt must be bounded too"
    assert "BREW_TIMEOUT" in source, "the macOS attempt must be bounded too"

@LINUX_ONLY
def test_an_exhausted_budget_stops_the_loop_instead_of_retrying_forever(tmp_path):
    """The whole step must end near its budget, not after every attempt.

    Per-attempt caps alone let a slow mirror run for attempt x cap x backoff, which
    on the engine and package jobs (no timeout-minutes) can be over an hour. With a
    budget the step gives up once the deadline passes.
    """
    # apt always fails; the sleep stub makes each call instant, so the only
    # thing that can stretch the step is the retry backoff, which the budget cuts.
    # A real 30 s call: without a budget each attempt gets APT_TIMEOUT (60 s) from
    # the step, so three attempts would take 90 s. With a 1 s budget the first is
    # cut at 1 s and the second is never started.
    failing = (HEADER + RECORD + "exec /bin/sleep 30" + NEWLINE)
    started = time.monotonic()
    result = run_step(tmp_path, failing, installs_soffice=False,
                      extra_env={"APT_BUDGET": "1", "APT_TIMEOUT": "60"})
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    # Without a budget this would run all three attempts plus 20/40/60 s backoff.
    assert elapsed < 20, "the step ignored its budget: %.1fs" % elapsed
    assert attempts(tmp_path) <= 3, attempts(tmp_path)


def test_the_macos_attempt_does_not_require_gnu_timeout():
    """macOS ships no GNU timeout(1); calling it failed the job outright.

    The bound must be done in the shell so it works on a stock macOS runner.
    Only gtimeout from coreutils would provide that binary, and this runner does
    not install it, so a bare "timeout" call on the macOS path is a hard failure.
    """
    source = ACTION.read_text(encoding="utf-8")
    macos = source.split("- name: Install LibreOffice (macOS)", 1)[1]
    for line in macos.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("if timeout "), "macOS has no timeout(1): " + stripped
    assert "bounded_brew" in macos, "the macOS bound must be done in the shell"


@LINUX_ONLY
def test_the_macos_bound_terminates_a_hung_child():
    """The shell bound must kill an overrunning child and report it as a failure."""
    source = ACTION.read_text(encoding="utf-8")
    macos = source.split("- name: Install LibreOffice (macOS)", 1)[1]
    start = macos.index("bounded_brew()")
    end = macos.index("for attempt in 1 2 3")
    function = macos[start:end]
    # Dedent by the block's own indent rather than a guessed 8 spaces: the YAML
    # body sits at 10, and leaving two spaces behind broke "local" out of the
    # function, which failed on macOS just as it would anywhere else.
    body = [line for line in function.splitlines() if line.strip()]
    indent = min(len(line) - len(line.lstrip()) for line in body[1:])
    script = "\n".join(line[indent:] if line.startswith(" " * indent) else line
                        for line in function.splitlines())
    modified = script.replace("brew install --cask libreoffice", "sleep 600")
    modified = modified.replace('"$BREW_TIMEOUT"', "2").replace("sleep 5", "sleep 0.2")
    result = subprocess.run(["bash", "-c", modified + "\nbounded_brew; echo rc=$?"],
                            capture_output=True, text=True, timeout=90)
    assert "rc=124" in result.stdout, result.stdout + result.stderr
    assert "terminating" in (result.stdout + result.stderr)

def test_every_long_job_has_a_timeout_and_a_bounded_install():
    """No job may run on GitHub's 360-minute default, and no apt call may be unbounded.

    Measured: the linux package job normally installs LibreOffice in 85 s, but on
    2026-10-23 it sat in its own apt-get for 47 minutes. That job does not use
    setup-engine, so the bounds added there never reached it, and it carried no
    timeout-minutes - so one slow mirror blocked every PR's all checks.
    """
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Every job that runs a package/engine/ui workload must declare a timeout.
    for job in ('engine', 'package', 'ui'):
        block = job_block(source, job)
        assert "timeout-minutes:" in block, job + " has no timeout-minutes"
    # And the package job's apt calls must be bounded like setup-engine's.
    package = job_block(source, "package")
    assert "Acquire::http::Timeout" in package, "package apt needs a stall timeout"
    assert "timeout \"$APT_TIMEOUT\"" in package, "package apt needs a per-attempt cap"
    # Every apt-get invocation must sit behind the timeout command; the dpkg
    # cleanup also uses sudo timeout, so count only the apt-get lines.
    apt_lines = [line for line in package.splitlines() if "apt-get" in line]
    assert apt_lines, "the package job should install LibreOffice"
    for line in apt_lines:
        assert "timeout" in line or "timeout" in package.splitlines()[
            package.splitlines().index(line) - 1], "unbounded apt-get: " + line.strip()


def job_block(source: str, job: str) -> str:
    """The text of one job in the workflow, up to the next top-level job."""
    start = source.index(chr(10) + "  " + job + ":" + chr(10))
    rest = source[start + 1:]
    following = rest.find(chr(10) + "  ")
    # Walk forward to the next line that starts a new job (two spaces, name, colon).
    lines = rest.splitlines(keepends=True)
    collected = []
    for index, line in enumerate(lines):
        if index > 0 and line.startswith('  ') and not line.startswith('    ') and line.rstrip().endswith(':'):
            break
        collected.append(line)
    return "".join(collected)
