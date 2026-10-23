#!/usr/bin/env python3
"""Publish a DSH review only after verifying its independent signature and Git refs.

Normal runs load this from the trusted default branch. The initial migration
may use the workflow's independently reviewed, hash-pinned verifier snapshot;
product candidate code is never executed with the review token.
Each private key stays on its review device; collaborators receive only public
keys. A signature authenticates a report, not the truth of its reasoning: complete
source inspection, actual checks and a separate reviewer remain required.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DEVICES = {'Mac-DSH', 'Mac-Claude', 'NAS-DSH'}
KEYS = ROOT / 'scripts/ci/reviewers'
CONTEXT = 'agent-review'


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def render_report(report):
    """The exact public body; local preparation scans this before any upload."""
    return (report['reviewer_device'] + ' 完整代码审查（已验证签名）\n\n```json\n'
            + json.dumps(report, ensure_ascii=False, indent=2) + '\n```\n')


def validate(raw: bytes, signature: str, pr: dict, files: list[str], key=None):
    if len(raw) > 24000:
        raise ValueError('review report too large')
    report = json.loads(raw)
    reviewer = report.get('reviewer_device')
    if reviewer not in DEVICES:
        raise ValueError('unknown review device')
    key = key or KEYS / (reviewer + '.pub')
    with tempfile.TemporaryDirectory() as folder:
        data = Path(folder) / 'report'; sig = Path(folder) / 'signature'
        data.write_bytes(raw); sig.write_bytes(base64.b64decode(signature, validate=True))
        subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(key),
                        '-rawin', '-in', str(data), '-sigfile', str(sig)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if (report.get('schema') != 1 or report.get('pr') != pr['number']
            or report.get('head_sha') != pr['headRefOid']
            or report.get('base_sha') != pr['baseRefOid']):
        raise ValueError('review does not match current PR head and base')
    if (report.get('implementation_device') not in DEVICES | {'Mac-Codex'}
            or reviewer == report.get('implementation_device')):
        raise ValueError('reviewer must be independent from implementation device')
    if not isinstance(report.get('reviewer_session'), str) or not report['reviewer_session'].strip():
        raise ValueError('reviewer session is required')
    inspected = report.get('reviewed_files')
    if not isinstance(inspected, list) or not all(isinstance(x, str) for x in inspected) or not set(files) <= set(inspected):
        raise ValueError('all changed files, including deletions, must be inspected')
    for field in ('summary', 'source_inspection', 'test_evidence'):
        if not isinstance(report.get(field), str) or not report[field].strip():
            raise ValueError(f'{field} is required')
    if report.get('verdict') not in ('APPROVE', 'REQUEST_CHANGES'):
        raise ValueError('invalid verdict')
    if not isinstance(report.get('blocking_findings'), list):
        raise ValueError('blocking findings must be explicit')
    if report['verdict'] == 'APPROVE' and report['blocking_findings']:
        raise ValueError('blocking findings cannot be approved')
    return report


def main():
    repo = os.environ['GITHUB_REPOSITORY']; number = int(os.environ['PR_NUMBER'])
    def current():
        return json.loads(gh('pr', 'view', str(number), '-R', repo, '--json',
                             'number,headRefOid,baseRefOid,isCrossRepository,state,title,body'))
    pr = current(); sha = pr['headRefOid']
    def status(state, description):
        gh('api', '-X', 'POST', f'repos/{repo}/statuses/{sha}', '-f', 'state='+state,
           '-f', 'context='+CONTEXT, '-f', 'description='+description,
           '-f', 'target_url='+os.environ['RUN_URL'])
    try:
        if pr['state'] != 'OPEN' or pr['isCrossRepository']:
            raise ValueError('only open same-repository PRs are supported')
        status('pending', '等待另一执行端完整代码审查')
        files = gh('pr', 'diff', str(number), '-R', repo, '--name-only').splitlines()
        if set(files) & {'LICENSE', 'docs/LICENSES.txt', 'engine/THIRD_PARTY_NOTICES.md'}:
            raise ValueError('license changes require maintainer review')
        commits = json.loads(gh('api', f'repos/{repo}/pulls/{number}/commits', '--paginate', '--slurp'))
        commits = [commit for page in commits for commit in page]
        for commit in commits:
            for field in ('author', 'committer'):
                identity = commit['commit'][field]
                if (identity['name'], identity['email']) not in {
                        ('Chamo', 'dev@chamo.invalid')}:
                    raise ValueError('unapproved commit identity')
        # Scan before publishing any report. Never execute PR source with this token.
        patch = gh('pr', 'diff', str(number), '-R', repo)
        # Removed text/context is already public; allow repairs that remove it.
        additions = '\n'.join(line[1:] for line in patch.splitlines()
                              if line.startswith('+') and not line.startswith('+++'))
        published = '\n'.join([pr['title'], pr['body'] or '', *files, additions,
                               *[c['commit']['message'] for c in commits]])
        with tempfile.TemporaryDirectory() as folder:
            keywords = Path(folder) / 'keywords'; keywords.write_text(os.environ.get('PRIVACY_KEYWORDS', ''))
            def scan(text):
                subprocess.run(['python3', str(ROOT/'scripts/ci/privacy_scan.py'), '--keywords',
                                str(keywords), 'text'], input=text, text=True, check=True)
            scan(published)
            encoded = os.environ.get('REVIEW_REPORT', '')
            if not encoded:
                return  # A PR event requests review; it is never an approval.
            raw = base64.b64decode(encoded, validate=True)
            report = validate(raw, os.environ.get('REVIEW_SIGNATURE', ''), pr, files)
            body = render_report(report)
            # Scan the exact public rendering, including decoded JSON escapes.
            scan(body)
        latest = current()
        if (latest['headRefOid'], latest['baseRefOid']) != (sha, pr['baseRefOid']):
            raise ValueError('PR changed while verifying evidence; review again')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md') as file:
            file.write(body); file.flush()
            gh('pr', 'comment', str(number), '-R', repo, '--body-file', file.name)
        status('success' if report['verdict'] == 'APPROVE' else 'failure',
               report['reviewer_device'] + (' 审查通过' if report['verdict'] == 'APPROVE' else ' 要求修改'))
    except Exception as error:
        status('failure', '审查证据或安全检查失败；查看运行记录')
        # Library/API exceptions can carry private source. Print only their class.
        print(type(error).__name__ + ': review rejected')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
