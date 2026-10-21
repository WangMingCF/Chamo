#!/usr/bin/env python3
"""Build an offline Apple Silicon bundle using pinned, project-local runtimes."""
from pathlib import Path
import subprocess,shutil,plistlib,json,hashlib,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import prune_runtime,prune_python,restore_libreoffice
# CHAMO_NO_PRUNE=1 builds the unpruned bundle, so size and speed claims can be
# compared under identical conditions instead of across different days.
import os
PRUNE=os.environ.get('CHAMO_NO_PRUNE')!='1'
RELEASE=json.loads((ROOT/'config/release.json').read_text())
APP=ROOT/'build/Chamo.app'
# Runtimes cannot be un-pruned in place, so an unpruned control build must start
# from an empty app directory; otherwise it would silently mix a pruned Python
# runtime with a full LibreOffice.
if not PRUNE and (APP/'Contents').exists():
    print('CHAMO_NO_PRUNE=1: discarding the existing bundle to build a clean control')
    shutil.rmtree(APP)
def run(args):subprocess.run([str(x) for x in args],check=True,cwd=ROOT)
def copy(src,dest):
    if not dest.exists():shutil.copytree(src,dest,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
(APP/'Contents/MacOS').mkdir(parents=True,exist_ok=True)
res=APP/'Contents/Resources';res.mkdir(exist_ok=True)
helpers=APP/'Contents/Helpers';helpers.mkdir(exist_ok=True)
# Every Sources/*.swift file is UI except ImageDecode.swift, the HEIC helper built below.
UI=sorted(x for x in (ROOT/'Sources').glob('*.swift') if x.name!='ImageDecode.swift')
run(['swiftc','-swift-version','5','-parse-as-library','-O','-target','arm64-apple-macosx15.0',*UI,'-o',APP/'Contents/MacOS/Chamo','-framework','SwiftUI','-framework','AppKit','-framework','PDFKit','-framework','UniformTypeIdentifiers','-framework','ImageIO','-framework','QuickLookThumbnailing','-framework','Quartz','-framework','UserNotifications'])
copy(ROOT/'third_party/python',res/'python')
# The bundle ships only what the engine needs at run time: build/test tooling,
# GUI stdlib modules and headers stay out (verified by scripts/prune_python.py).
# A leftover pruned runtime cannot be un-pruned in place, so CHAMO_NO_PRUNE requires
# a clean build/ rather than silently shipping half a control bundle.
# pending() means "still has prunable parts", so a normal build prunes and a
# control build requires exactly that state (a pruned runtime cannot be restored).
if PRUNE:
    if prune_python.pending(res/'python'):
        print('python pruned:',prune_python.prune(res/'python'),'MB')
elif not prune_python.pending(res/'python'):
    raise SystemExit('CHAMO_NO_PRUNE=1 needs a clean build/: the Python runtime is already pruned')
# pip console scripts otherwise retain the builder's absolute interpreter path.
# A shell/Python polyglot resolves the bundled interpreter even with spaces in
# the installed app path, without depending on system Python or leaking HOME.
for entry in (res/'python/bin').iterdir():
    if entry.is_symlink() or not entry.is_file():continue
    data=(ROOT/'third_party/python/bin'/entry.name).read_bytes()
    first,separator,rest=data.partition(b'\n')
    if first.startswith(b'#!') and b'python' in first and b'/Users/' in first:
        entry.write_bytes(b'#!/bin/sh\n\'\'\'exec\' "$(dirname "$0")/python3" -I -B "$0" "$@"\n\' \'\'\'\n'+rest)
# Bytecode created by local smoke tests must not be shipped or invalidate seals.
for cache in (res/'python').rglob('__pycache__'):
    if cache.is_dir():shutil.rmtree(cache)
# LibreOffice ships without help, gallery, Java bridge and its own Python:
# none are used for headless document conversion (verified byte-identical output).
# Pruning runs on every build, not only on the first copy: a bundle left over from
# an unpruned or CHAMO_NO_PRUNE build would otherwise be shipped at full size.
if not (helpers/'LibreOffice.app').exists():
    # Running LibreOffice from the checked-in copy makes it write __pycache__ into
    # its own framework, which breaks the vendor seal. Restore from the pinned DMG
    # rather than deleting caches: TDF ships some caches itself.
    restore_libreoffice.ensure_sealed()
    shutil.copytree(ROOT/'third_party/LibreOffice.app',helpers/'LibreOffice.app',symlinks=True)
if PRUNE:
    if prune_runtime.pending(helpers/'LibreOffice.app'):
        print('LibreOffice pruned:',prune_runtime.prune_and_seal(helpers/'LibreOffice.app',run),'MB')
elif not prune_runtime.pending(helpers/'LibreOffice.app'):
    raise SystemExit('CHAMO_NO_PRUNE=1 needs a clean build/: LibreOffice is already pruned')
if not PRUNE:
    # A control bundle keeps the vendor layout, so it must keep the vendor seal.
    run(['codesign','--verify','--deep','--strict',helpers/'LibreOffice.app'])
run(['swiftc','-O','-target','arm64-apple-macosx15.0',ROOT/'Sources/ImageDecode.swift','-o',helpers/'dwnative','-framework','ImageIO','-framework','UniformTypeIdentifiers'])
run(['codesign','--force','--sign','-',helpers/'dwnative'])
# Our authored engine is refreshed on every build, unlike immutable runtimes.
shutil.copytree(ROOT/'engine',res/'engine',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
for cache in (res/'engine').rglob('__pycache__'):
    if cache.is_dir():shutil.rmtree(cache)
# Every file runs in a fresh `python -I -B` worker, which cannot write bytecode. Without
# shipped .pyc files each worker recompiled the stdlib, Pillow and the engine from source
# (~0.25 s CPU per file; 2000 small images took 139 s instead of ~4 s). Compile before
# signing so the bytecode is sealed; unchecked-hash pycs never depend on source mtimes.
# olefile2.py is a never-imported Python 2 leftover that cannot compile.
# .pyc files record their source path for tracebacks; strip the builder's path (it
# contains the local user name) and record the standard install location instead.
for cache in [*(res/'python').rglob('__pycache__'),*(res/'engine').rglob('__pycache__')]:
    if cache.is_dir():shutil.rmtree(cache)
# -B: the compiler process must not cache its own imports with the builder's path; -f recompiles them all.
run([res/'python/bin/python3','-I','-B','-m','compileall','-q','-f','-j','0','--invalidation-mode','unchecked-hash','-x',r'olefile2\.py$',
     '-s',res,'-p','/Applications/Chamo.app/Contents/Resources',res/'python/lib/python3.12',res/'engine'])
# App icon (generated by assets/brand/build.sh).
shutil.copy2(ROOT/'assets/brand/Chamo.icns',res/'Chamo.icns')
doc=res/'Documentation';doc.mkdir(exist_ok=True)
# A launcher so the shipped CLI is usable without knowing the interpreter path.
# It ships at Contents/Resources/bin/chamo and is documented in USER_GUIDE.txt.
# Reasons for the shape it takes:
#   * it cannot live in Contents/MacOS: codesign treats every file there as a
#     candidate main executable and rejected the script with "code has no
#     resources but signature indicates they must be present";
#   * it resolves symlinks, because linking it into PATH is how a bundled CLI is
#     normally exposed and $(dirname "$0") would otherwise be the link's folder;
#   * bin/ is inside Contents/Resources, so the interpreter is its sibling.
bin=res/'bin';bin.mkdir(parents=True,exist_ok=True)
chamo=bin/'chamo'
LAUNCHER = (
    '#!/bin/sh',
    '# Command-line front end: same engine, same rules as the app.',
    'SELF=$0',
    'while [ -L "$SELF" ]; do',
    '  LINK=$(readlink "$SELF")',
    '  case $LINK in',
    '    /*) SELF=$LINK ;;',
    '    *) SELF=$(dirname "$SELF")/$LINK ;;',
    '  esac',
    'done',
    'RES=$(cd "$(dirname "$SELF")/.." && pwd)',
    'exec "$RES/python/bin/python3" -I -B "$RES/engine/chamo.py" "$@"',
    '',
)
chamo.write_text(chr(10).join(LAUNCHER),encoding='utf-8')
chamo.chmod(0o755)
for name in ['USER_GUIDE.txt','RELEASE_NOTES.txt','LICENSES.txt']:
    if (ROOT/'docs'/name).exists():shutil.copy2(ROOT/'docs'/name,doc/name)
shutil.copy2(ROOT/'LICENSE',doc/'LICENSE.txt')  # MIT: the notice ships with every copy
shutil.copy2(ROOT/'engine/THIRD_PARTY_NOTICES.md',doc/'RAG-THIRD-PARTY-NOTICES.md')
shutil.copy2(ROOT/'third_party/requirements.lock',doc/'requirements.lock')
info={'CFBundleName':'Chamo','CFBundleDisplayName':'Chamo','CFBundleIdentifier':'io.github.wangmingcf.chamo','CFBundleExecutable':'Chamo','CFBundleIconFile':'Chamo','CFBundlePackageType':'APPL','CFBundleShortVersionString':RELEASE['version'],'CFBundleVersion':RELEASE['build'],'GitHubRepositoryURL':RELEASE['github_repository_url'],'LSMinimumSystemVersion':'15.0','NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication','NSHumanReadableCopyright':'Chamo. Third-party components retain their respective licenses.','CFBundleDocumentTypes':[{'CFBundleTypeName':'Documents','CFBundleTypeRole':'Viewer','LSHandlerRank':'Alternate','LSItemContentTypes':['public.data','public.folder']} ],
      # Finder > Services: send selected files/folders to the start page.
      'NSServices':[{'NSMenuItem':{'default':'用 Chamo 处理'},'NSMessage':'openFiles','NSPortName':'Chamo','NSRequiredContext':{},'NSSendFileTypes':['public.item'],'NSSendTypes':['NSFilenamesPboardType','public.file-url']}]}
with (APP/'Contents/Info.plist').open('wb') as f:plistlib.dump(info,f)
# Python wheels can have unsigned native binaries, so sign inside-out. Note that
# the bundled LibreOffice no longer carries its vendor signature: pruning its
# resources invalidates it, and prune_runtime.resign() re-seals it ad-hoc.
for p in (res/'python').rglob('*'):
    if not p.is_file() or p.is_symlink():continue
    with p.open('rb') as f:magic=f.read(4)
    if magic in [b'\xcf\xfa\xed\xfe',b'\xce\xfa\xed\xfe',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca']:
        run(['codesign','--force','--sign','-',p])
run(['codesign','--force','--sign','-',APP])
run(['codesign','--verify','--deep','--strict',APP])
print(APP)
