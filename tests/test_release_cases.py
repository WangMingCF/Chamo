from test_engine import cfg, pdf, run, w, Image, Path, pytest

def test_a4_and_output_collision(tmp_path,cfg):
    src=tmp_path/'wide.png';Image.new('RGB',(300,100),'blue').save(src)
    cfg.update(operation='images_pdf',paper='a4',margin_mm=10)
    result=run(src,tmp_path/'out',cfg)
    page=w.reader(result['outputs'][0]['path']).pages[0]
    assert abs(float(page.mediabox.width)-841.89)<0.1
    assert abs(float(page.mediabox.height)-595.276)<0.1
    cfg.update(operation='rag');out=tmp_path/'rag';out.mkdir()
    # Collision is tested using deterministic native RAG naming discovered once.
    r=run(src,out,cfg);dest=Path(r['outputs'][0]['path']);dest.write_bytes(b'user content')
    with pytest.raises(ValueError):run(src,out,cfg)
    assert dest.read_bytes()==b'user content'

def test_final_strict_audit(tmp_path,cfg):
    from cloud_kb_pack.output import verify_job
    src=pdf(tmp_path/'audit.pdf',5);cfg.update(operation='pdf_split',pdf_pages=2)
    r=run(src,tmp_path/'out',cfg)
    assert r['fidelity']=='verified_content'
    for meta in Path(r['audit']).rglob('job.json'):
        assert verify_job(meta.parent)['status']=='verified'

def test_webp_default_lossless(tmp_path,cfg):
    src=tmp_path/'rgb.png';Image.new('RGB',(100,100),(37,86,129)).save(src)
    cfg.update(operation='image_convert',format='webp')
    r=run(src,tmp_path/'out',cfg)
    with Image.open(src) as a,Image.open(r['outputs'][0]['path']) as b:assert a.tobytes()==b.tobytes()

def test_gui_disconnection_stops_workers(tmp_path,cfg):
    import subprocess,sys,json
    src=pdf(tmp_path/'disconnect.pdf',20)
    req=tmp_path/'request.json';req.write_text(json.dumps({'gui_session':True,'inputs':[str(src)],'output':str(tmp_path/'out'),'config':cfg}))
    p=subprocess.run([sys.executable,'-I',str(w.BASE/'workbench.py'),'--request',str(req)],input='',capture_output=True,text=True,encoding='utf-8',timeout=120)
    assert json.loads(p.stdout.splitlines()[-1])['state']=='cancelled'
    assert src.exists()

def test_low_space_preflight(tmp_path,cfg,monkeypatch):
    from collections import namedtuple
    usage=namedtuple('usage','total used free')
    monkeypatch.setattr(w.shutil,'disk_usage',lambda p:usage(1000,999,1))
    with pytest.raises(ValueError,match='2GB'):w.Supervisor({'inputs':[],'output':str(tmp_path/'out'),'config':cfg}).run()
