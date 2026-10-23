"""Exercise the actual workflow wrapper without credentials or GitHub calls."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap

import pytest

WORKFLOW = Path(__file__).resolve().parents[2]/'.github/workflows/agent-review.yml'


def wrapper():
    step = WORKFLOW.read_text().split('      - name: Verify independent DSH review\n', 1)[1]
    return textwrap.dedent(step.split('        run: |\n', 1)[1])


def test_inputs_are_not_in_step_env_or_script_interpolation():
    workflow = WORKFLOW.read_text()
    assert not re.search(r'^\s+(?:REVIEW_REPORT|REVIEW_SIGNATURE):', workflow, re.M)
    assert not re.search(r'\$\{\{[^}]*inputs\.(?:report|signature)', workflow)
    assert 'GITHUB_EVENT_PATH' in wrapper()


def run_wrapper(tmp_path, inputs):
    verifier = tmp_path/'scripts/ci/dsh_review.py'
    verifier.parent.mkdir(parents=True)
    verifier.write_text('''import json, os
from pathlib import Path
Path('received.json').write_text(json.dumps({k: os.environ[k] for k in ('REVIEW_REPORT', 'REVIEW_SIGNATURE')}))
print('verifier-called')
''')
    event = tmp_path/'event.json'
    event.write_text(json.dumps({'inputs': inputs}))
    env = {k: v for k, v in os.environ.items()
           if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'REVIEW_REPORT', 'REVIEW_SIGNATURE')}
    env['GITHUB_EVENT_PATH'] = str(event)
    return subprocess.run(['/bin/bash', '-se'], input=wrapper(), cwd=tmp_path,
                          env=env, text=True, capture_output=True)


def test_wrapper_passes_exact_inputs_privately_without_shell_execution(tmp_path):
    report = base64.b64encode(b'synthetic privacy marker\n::warning::not a command').decode()
    signature = '$(touch should-not-exist)'
    result = run_wrapper(tmp_path, {'report': report, 'signature': signature})
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path/'received.json').read_text()) == {
        'REVIEW_REPORT': report, 'REVIEW_SIGNATURE': signature}
    assert report not in result.stdout+result.stderr
    assert signature not in result.stdout+result.stderr
    assert not (tmp_path/'should-not-exist').exists()


def test_pr_event_still_reaches_pending_verifier_with_empty_inputs(tmp_path):
    result = run_wrapper(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path/'received.json').read_text()) == {
        'REVIEW_REPORT': '', 'REVIEW_SIGNATURE': ''}


@pytest.mark.parametrize('field', ['report', 'signature'])
def test_non_string_inputs_fail_before_verifier_without_echoing_content(tmp_path, field):
    result = run_wrapper(tmp_path, {field: ['synthetic-private-value']})
    assert result.returncode != 0
    assert not (tmp_path/'received.json').exists()
    assert 'synthetic-private-value' not in result.stdout+result.stderr
