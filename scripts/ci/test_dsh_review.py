"""Exercise the trust boundary with real signatures, stale refs and forged reports."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import pytest

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
