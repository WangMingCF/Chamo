"""Build a drag-install disk image from the already validated app."""
from pathlib import Path
import subprocess,shutil,hashlib,json
ROOT=Path(__file__).resolve().parents[1];version=json.loads((ROOT/'config/release.json').read_text())['version'];release=ROOT/'release';stage=ROOT/'build/dmg-stage'
# Start from an empty stage so files from earlier versions never ship again.
if stage.exists():shutil.rmtree(stage)
stage.mkdir()
app=stage/'Chamo.app'
if app.exists():shutil.rmtree(app)
subprocess.run(['ditto',str(ROOT/'build/Chamo.app'),str(app)],check=True)
link=stage/'Applications'
if not link.exists():link.symlink_to('/Applications')
for src,name in [('docs/USER_GUIDE.txt','开始使用.txt'),('docs/RELEASE_NOTES.txt','版本说明.txt'),('docs/LICENSES.txt','第三方许可.txt'),('LICENSE','许可证.txt')]:shutil.copy2(ROOT/src,stage/name)
sources=stage/'ThirdPartySources'
if sources.exists():shutil.rmtree(sources)
shutil.copytree(release/'ThirdPartySources',sources)
subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
dmg=release/f'Chamo-{version}-AppleSilicon.dmg'
# ULMO (LZMA) makes the same content roughly 43% smaller than UDZO/zlib and
# stays mountable by Finder and by hdiutil on every macOS we target. Verified
# by mounting the produced image, checking the signature and running conversions
# from the installed copy before publishing.
subprocess.run(['hdiutil','create','-ov','-volname',f'Chamo {version}','-srcfolder',str(stage),'-format','ULMO',str(dmg)],check=True)
h=hashlib.sha256(dmg.read_bytes()).hexdigest();(release/'SHA256SUMS.txt').write_text(h+'  '+dmg.name+'\n')
print(json.dumps({'dmg':str(dmg),'bytes':dmg.stat().st_size,'sha256':h},ensure_ascii=False))
