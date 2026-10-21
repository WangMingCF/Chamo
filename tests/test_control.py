import sys,json,time,subprocess,queue,threading
from pathlib import Path
from reportlab.pdfgen import canvas
ROOT=Path(__file__).resolve().parents[1]


def paused_children(pid):
    """Children of this supervisor that are currently stopped (POSIX only).

    The process state comes from ps; Windows has no equivalent "stopped" flag,
    so there the test asserts the observable outcome instead of flags.
    """
    rows=subprocess.check_output(['ps','-axo','pid,ppid,state'],text=True).splitlines()[1:]
    return [r.split() for r in rows if len(r.split())==3 and r.split()[1]==str(pid)]


def test_pause_cancel_and_resume(tmp_path):
    src=tmp_path/'large.pdf';c=canvas.Canvas(str(src))
    for i in range(30):c.drawString(100,700,str(i));c.showPage()
    c.save()
    req={'inputs':[str(src)],'output':str(tmp_path/'out'),'config':{'operation':'pdf_images','format':'png','dpi':300,'quality':90,'workers':1}}
    f=tmp_path/'request.json';f.write_text(json.dumps(req),encoding='utf-8')
    p=subprocess.Popen([sys.executable,'-I',str(ROOT/'engine/workbench.py'),'--request',str(f)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,encoding='utf-8')
    q=queue.Queue()
    def read():
        for line in p.stdout:q.put(json.loads(line))
    threading.Thread(target=read,daemon=True).start()
    # Windows CI runners are slower to start a Python worker than a local Mac.
    while q.get(timeout=60)['type']!='item':pass
    p.stdin.write('{"command":"pause"}\n');p.stdin.flush()
    ev=q.get(timeout=30)
    while ev['type']!='control':ev=q.get(timeout=30)
    assert ev['state']=='paused'
    time.sleep(.5)
    if sys.platform != 'win32':
        # Inspect children of this exact supervisor, not arbitrary Python processes.
        children=paused_children(p.pid)
        assert children and all('T' in r[2] for r in children)
    p.stdin.write('{"command":"cancel"}\n');p.stdin.flush();p.wait(timeout=60)
    while not q.empty():ev=q.get()
    assert ev['type']=='finished' and ev['state']=='cancelled'
    assert src.is_file()
    # Restart from the saved input/config; no source or completed file overwritten.
    req['config']['dpi']=36;f.write_text(json.dumps(req),encoding='utf-8')
    r=subprocess.run([sys.executable,'-I',str(ROOT/'engine/workbench.py'),'--request',str(f)],input='',capture_output=True,text=True,encoding='utf-8',timeout=180)
    result=json.loads(r.stdout.splitlines()[-1]);assert result['success']==1 and result['outputs']==30
