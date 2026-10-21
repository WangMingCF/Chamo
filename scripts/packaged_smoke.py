from pathlib import Path
import json,subprocess,time,sys,hashlib,platform,shutil
ROOT=Path(__file__).resolve().parents[1]
APP=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT/'build/Chamo.app'
python=APP/'Contents/Resources/python/bin/python3';engine=APP/'Contents/Resources/engine/workbench.py'
fixtures=ROOT/'tests/fixtures';out=ROOT/'artifacts/packaged-output'
# Acceptance must always start from an empty output tree. A leftover manifest from a
# previous run makes the engine resume and finish in ~0.1 s, which silently turns a
# real packaging check into a no-op that still reports success.
shutil.rmtree(out,ignore_errors=True);out.mkdir(parents=True)
rows=[]
for op,names in [('pdf_images',['示例讲义.pdf']),('images_pdf',['透明图片.png']),('office_pdf',['示例课件.pptx']),('office_images',['示例课件.pptx']),('image_compress',['透明图片.png']),('rag',['示例讲义.pdf','示例课件.pptx','透明图片.png']),('pdf_split',['示例讲义.pdf'])]:
    cfg={'operation':op,'dpi':144,'format':'png','quality':90,'pdf_pages':2,'pdf_bytes':100000000,'workers':2,'soffice':str(APP/'Contents/Helpers/LibreOffice.app/Contents/MacOS/soffice')}
    req={'inputs':[str(fixtures/n) for n in names],'output':str(out/op),'config':cfg}
    request=out/(op+'.json');request.write_text(json.dumps(req))
    t=time.monotonic();p=subprocess.run(['/usr/bin/time','-l',str(python),'-I','-B',str(engine),'--request',str(request)],input='',capture_output=True,text=True,timeout=240,env={'PATH':'/usr/bin:/bin','HOME':str(out),'TMPDIR':'/tmp'})
    (out/(op+'.log')).write_text(p.stdout+'\n'+p.stderr)
    events=[json.loads(x) for x in p.stdout.splitlines()];final=events[-1]
    assert p.returncode==0 and final['type']=='finished' and final['failed']==0,(op,p.stdout,p.stderr)
    rows.append({'operation':op,'wall_seconds':round(time.monotonic()-t,2),'summary':final,'input_bytes':sum((fixtures/n).stat().st_size for n in names),'max_rss_note':'time -l logs contain parent peak RSS, not aggregate process tree'})
    print(op,final['outputs'],rows[-1]['wall_seconds'],flush=True)
subprocess.run(['codesign','--verify','--deep','--strict',str(APP)],check=True)
(ROOT/'artifacts/packaged-smoke.json').write_text(json.dumps({'app':str(APP),'platform':platform.platform(),'machine':platform.machine(),'rows':rows},ensure_ascii=False,indent=2))
