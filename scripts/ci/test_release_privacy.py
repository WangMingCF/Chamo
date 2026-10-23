"""Exercise the release scan against unpacked payloads, not installed copies."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('dirty', [None, 'data', 'control'])
def test_linux_release_scans_archive_text(tmp_path, dirty):
    secret = 'SyntheticPrivate'
    payload = tmp_path/'archive-data'; control = tmp_path/'archive-control'
    for path in ['opt/chamo/engine', 'usr/share/doc/chamo-cli', 'usr/bin']:
        (payload/path).mkdir(parents=True)
    (payload/'opt/chamo/engine/main.py').write_text('print("safe")')
    (payload/'usr/bin/chamo').write_text('#!/bin/sh\n')
    (payload/'usr/share/doc/chamo-cli/guide.txt').write_text(secret if dirty=='data' else 'safe')
    control.mkdir(); (control/'control').write_text(secret if dirty=='control' else 'Package: chamo-cli')
    # Emulate only dpkg extraction; execute the actual workflow shell and scanner.
    # No /usr installation tree exists in this fixture and no host paths are changed.
    tools = tmp_path/'bin'; tools.mkdir()
    extract = tools/'dpkg-deb'
    extract.write_text(f'#!{sys.executable}\nimport os,shutil,sys\n'
                       'shutil.copytree(os.environ["TEST_CONTROL" if sys.argv[1]=="-e" else "TEST_DATA"],sys.argv[-1])\n')
    extract.chmod(0o755)
    (tools/'python').symlink_to(sys.executable)
    workflow = (ROOT/'.github/workflows/release.yml').read_text()
    step = workflow.split('      - name: Scan exact Linux package payload\n',1)[1].split('\n      - uses:',1)[0]
    script = textwrap.dedent(step.split('        run: |\n',1)[1])
    runner = tmp_path/'runner'; runner.mkdir()
    result = subprocess.run(['bash','-e','-o','pipefail','-c',script],cwd=ROOT,
                            env={**os.environ,'PATH':str(tools)+os.pathsep+os.environ['PATH'],
                                 'RUNNER_TEMP':str(runner),'TEST_DATA':str(payload),'TEST_CONTROL':str(control),
                                 'RELEASE_TAG':'v-test','PRIVACY_KEYWORDS':
                                 f'sha256:{hashlib.sha256(secret.casefold().encode()).hexdigest()}:{len(secret)}'},
                            capture_output=True,text=True)
    assert result.returncode == (1 if dirty else 0), result.stderr
