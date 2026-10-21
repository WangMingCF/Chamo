#!/usr/bin/env python3
"""Download pinned runtime artifacts, verify them, then prepare local build tools."""
from pathlib import Path
import hashlib,json,os,platform,subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
def run(args):subprocess.run([str(x) for x in args],check=True,cwd=ROOT)
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
if platform.system()!='Darwin' or platform.machine()!='arm64':sys.exit('Requires an Apple Silicon Mac.')
third=ROOT/'third_party';downloads=third/'downloads';downloads.mkdir(parents=True,exist_ok=True)
(ROOT/'build').mkdir(exist_ok=True)
for item in json.loads((third/'dependencies.json').read_text()):
    target=downloads/item['file']
    if not target.exists():
        part=target.with_suffix(target.suffix+'.part')
        run(['curl','--fail','--location','--retry','3','--connect-timeout','30','--output',part,item['url']])
        if digest(part)!=item['expected_sha256']:sys.exit('SHA-256 mismatch: '+part.name)
        part.rename(target)
    if digest(target)!=item['expected_sha256']:sys.exit('SHA-256 mismatch: '+target.name)
    if item.get('expected_bytes') and target.stat().st_size!=item['expected_bytes']:sys.exit('Size mismatch')
    if target.name.startswith('cpython-') and not (third/'python').exists():run(['tar','-xf',target,'-C',third])
    if target.suffix=='.dmg' and not (third/'LibreOffice.app').exists():
        mount=ROOT/'build/dependency-mount'
        run(['hdiutil','attach','-readonly','-nobrowse','-mountpoint',mount,target])
        try:run(['ditto',mount/'LibreOffice.app',third/'LibreOffice.app'])
        finally:run(['hdiutil','detach',mount])
run(['codesign','--verify','--deep','--strict',third/'LibreOffice.app'])
run([third/'python/bin/python3','-m','pip','install','--only-binary=:all:','-r',third/'requirements.lock'])
print('Dependencies ready.')
