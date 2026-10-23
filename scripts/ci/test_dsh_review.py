"""Exercise the trust boundary with real signatures, stale refs and forged reports."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import pytest
import hashlib

spec = importlib.util.spec_from_file_location('review', Path(__file__).with_name('dsh_review.py'))
review = importlib.util.module_from_spec(spec); spec.loader.exec_module(review)


@pytest.fixture
def signed(tmp_path):
    key=tmp_path/'key'; pub=tmp_path/'pub'
    subprocess.run(['openssl','genpkey','-algorithm','Ed25519','-out',str(key)],check=True)
    subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(pub)],check=True)
    pr={'number':3,'headRefOid':'a'*40,'baseRefOid':'b'*40}
    report={'schema':1,'pr':3,'head_sha':'a'*40,'base_sha':'b'*40,
            'reviewer_device':'Mac-DSH','implementation_device':'NAS-DSH','reviewer_session':'review-session',
            'reviewed_files':['engine/a.py','tests/a.py'], 'summary':'Checked complete implementation.',
            'source_inspection':'Read callers and error paths.', 'test_evidence':'pytest: 3 passed; run URL',
            'verdict':'APPROVE','blocking_findings':[]}
    def sign(data):
        raw=json.dumps(data).encode(); path=tmp_path/'data';sig=tmp_path/'sig';path.write_bytes(raw)
        subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(path),'-out',str(sig)],check=True)
        return raw,base64.b64encode(sig.read_bytes()).decode()
    return report,pr,pub,sign


def test_valid_independent_report(signed):
    report,pr,key,sign=signed
    assert review.validate(*sign(report),pr,['engine/a.py'],key)['verdict']=='APPROVE'


def test_signature_rejects_tampering(signed):
    report,pr,key,sign=signed;raw,sig=sign(report)
    with pytest.raises(subprocess.CalledProcessError): review.validate(raw+b' ',sig,pr,[],key)


@pytest.mark.parametrize('field,value',[
    ('head_sha','c'*40),('base_sha','c'*40),('pr',4),('reviewer_device','unknown'),
    ('implementation_device','Mac-DSH'),('reviewed_files',[]),('test_evidence',''),
    ('source_inspection',''),('blocking_findings',['data loss']),('verdict','MAYBE')])
def test_invalid_signed_claim_is_not_an_approval(signed,field,value):
    report,pr,key,sign=signed;report[field]=value
    with pytest.raises(ValueError): review.validate(*sign(report),pr,['engine/a.py'],key)


def test_another_registered_device_can_review(signed):
    report, pr, key, sign = signed
    report['reviewer_device'] = 'Mac-Claude'
    assert review.validate(*sign(report), pr, ['engine/a.py'], key)['verdict'] == 'APPROVE'
    report['reviewer_device'] = 'NAS-DSH'
    with pytest.raises(ValueError, match='independent'):
        review.validate(*sign(report), pr, ['engine/a.py'], key)


def publication(signed, monkeypatch, report, patch, secret, files=None, escaped_ascii=False):
    """Only GitHub transport is replaced; signature and privacy scan are real."""
    _, pr, key, sign = signed
    raw, signature = sign(report)
    if escaped_ascii:
        # Produce an authentic signature over escaped ASCII as well as Unicode.
        report_path = key.parent/'data'; signature_path = key.parent/'sig'
        raw = raw.replace(secret.encode(), ''.join('\\u%04x' % ord(c) for c in secret).encode())
        report_path.write_bytes(raw)
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key.parent/'key'),
                        '-rawin', '-in', str(report_path), '-out', str(signature_path)], check=True)
        signature = base64.b64encode(signature_path.read_bytes()).decode()
    monkeypatch.setattr(review, 'KEYS', key.parent)
    (key.parent/'Mac-DSH.pub').write_bytes(key.read_bytes())
    for name, value in {'GITHUB_REPOSITORY':'example/project','PR_NUMBER':'3','RUN_URL':'https://example.invalid/run',
                        'REVIEW_REPORT':base64.b64encode(raw).decode(),'REVIEW_SIGNATURE':signature,
                        'PRIVACY_KEYWORDS':f'sha256:{hashlib.sha256(secret.casefold().encode()).hexdigest()}:{len(secret)}'}.items():
        monkeypatch.setenv(name,value)
    published=[]; states=[]
    def fake_gh(*args):
        if args[:2]==('pr','view'):
            return json.dumps({**pr,'state':'OPEN','isCrossRepository':False,'title':'Repair','body':''})
        if args[:2]==('pr','diff'):
            return '\n'.join(files or ['engine/a.py']) if '--name-only' in args else patch
        if args[:2]==('pr','comment'):
            published.append(Path(args[args.index('--body-file')+1]).read_text()); return ''
        if args[0]=='api' and '/commits' in args[1]: return '[[]]'
        if args[0]=='api' and 'POST' in args:
            states.append(next(a for a in args if a.startswith('state='))); return ''
        raise AssertionError(args)
    monkeypatch.setattr(review,'gh',fake_gh)
    return published,states


@pytest.mark.parametrize('secret,ascii_escape', [('SyntheticPrivate',True),('合成隐私标识',False)])
@pytest.mark.parametrize('verdict', ['APPROVE','REQUEST_CHANGES'])
def test_exact_public_body_is_scanned(signed,monkeypatch,secret,ascii_escape,verdict):
    report=signed[0];report.update(summary=secret,verdict=verdict)
    published,states=publication(signed,monkeypatch,report,'+safe',secret,escaped_ascii=ascii_escape)
    with pytest.raises(SystemExit):review.main()
    assert not published and states[-1]=='state=failure'


def test_removing_private_text_and_unchanged_context_is_allowed(signed,monkeypatch):
    report=signed[0]
    published,states=publication(signed,monkeypatch,report,'-SyntheticPrivate\n SyntheticPrivate\n+removed','SyntheticPrivate')
    review.main()
    assert len(published)==1 and states[-1]=='state=success'


@pytest.mark.parametrize('patch,files', [('+SyntheticPrivate',None),('+safe',['SyntheticPrivate.py'])])
def test_new_text_and_paths_remain_protected(signed,monkeypatch,patch,files):
    published,states=publication(signed,monkeypatch,signed[0],patch,'SyntheticPrivate',files=files)
    with pytest.raises(SystemExit):review.main()
    assert not published and states[-1]=='state=failure'
