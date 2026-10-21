"""Local conversion engine. Every worker is isolated; originals are read-only."""
from __future__ import annotations
import argparse, concurrent.futures, contextlib, datetime, io, queue, hashlib, json, os, signal, shutil, subprocess, sys, tempfile, threading, time, uuid
from pathlib import Path
from PIL import Image, ImageOps
Image.MAX_IMAGE_PIXELS = 80_000_000
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# The protocol is UTF-8 JSONL. Windows consoles default to a legacy code page, so
# reconfigure before anything can be printed; stdin carries the control commands.
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError, OSError):
        pass
MEDIA = {'.ts','.mp4','.mov','.mkv','.avi','.flv','.wmv','.mpg','.mpeg','.webm','.mp3','.wav','.aac','.amr','.flac','.m4a','.ogg','.opus','.wma'}
IMAGES = {'.jpg','.jpeg','.jfif','.png','.bmp','.gif','.tif','.tiff','.webp','.heic','.heif','.avif'}
# LibreOffice-readable documents, including WPS Office (.wps/.et/.dps), templates,
# slide shows and macro-enabled files. Headless LibreOffice never runs macros.
OFFICE = {'.doc','.docx','.docm','.dot','.dotx','.ppt','.pptx','.pptm','.pps','.ppsx','.pot','.potx',
          '.xls','.xlsx','.xlsm','.xlsb','.xlt','.xltx','.odt','.odp','.ods','.odg','.rtf','.csv','.txt',
          '.wps','.wpt','.et','.ett','.dps','.dpt'}
OPS = {'pdf_images','images_pdf','pdf_merge','pdf_split','office_pdf','office_images','image_convert','image_compress','pdf_compress','rag'}

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

# Exclusive, non-overwriting publication is platform specific; see
# platform_atomic.py for the macOS/Linux/Windows primitives and fallbacks.
from platform_atomic import publish_exclusive  # noqa: E402
from platform_process import spawn_group, pause_tree, resume_tree, terminate_tree, kill_tree  # noqa: E402
import platform_process  # noqa: E402
import platform_office  # noqa: E402
import platform_names  # noqa: E402
import platform_state  # noqa: E402
import platform_text  # noqa: E402
import protocol  # noqa: E402

def atomic_json(path, data, durable=True):
    # durable=False skips fsync for hand-off files that only live within one run
    # (per-item request/result); receipts and reports stay durable.
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        with temp.open('x',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2);f.flush()
            if durable:os.fsync(f.fileno())
        replace_with_retry(temp,path)
    except BaseException:
        # A failure between "write temp" and "publish" (or a killed process) must not
        # leave a half-written .tmp- file next to the user's converted files.
        temp.unlink(missing_ok=True)
        raise

def atomic_text(path,text,durable=True):
    """Publish text atomically, keeping "\n" as "\n" on every platform.

    The manifest's expected bytes are hashed before writing (so an identical file
    can be adopted and a different one is never overwritten), and text mode would
    turn every line ending into "\r\n" on Windows and change that hash.
    """
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        with temp.open('x',encoding='utf-8',newline='') as f:
            f.write(text);f.flush()
            if durable:os.fsync(f.fileno())
        replace_with_retry(temp,path)
    except BaseException:
        # The manifest lives in the user's output folder: never leave a temp file there.
        temp.unlink(missing_ok=True)
        raise

def event(kind, **kw):
    """Emit one JSONL event.

    The stream must be UTF-8 regardless of the platform default. On a Chinese or
    European Windows console, sys.stdout is cp1252/cp936, and printing an event
    that carries a Chinese path or message raises UnicodeEncodeError inside the
    event emitter itself — which is how windows-latest failed with a bare "fatal"
    before converting anything. The stream is reconfigured once at start-up; this
    is the fallback for a stream that cannot be reconfigured.
    """
    # Every event carries the protocol version so a consumer can refuse a stream
    # it does not understand instead of silently misreading it.
    line=json.dumps({'protocol':protocol.PROTOCOL,'type':kind,**kw},ensure_ascii=False)
    try:
        print(line,flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line+'\n').encode('utf-8'))
        sys.stdout.buffer.flush()

def pages(spec,total):
    if not spec.strip(): return list(range(total))
    values=[]
    for part in spec.split(','):
        nums=part.strip().split('-')
        if len(nums)>2 or not all(x.strip().isdigit() for x in nums): raise ValueError('页码格式应为 1-5,8,12-20')
        a=int(nums[0]);b=int(nums[-1])
        if not 1<=a<=b<=total: raise ValueError(f'页码超出范围 1–{total}')
        values.extend(range(a-1,b))
    if len(set(values))!=len(values): raise ValueError('页码范围重复')
    return values

def reader(path):
    from pypdf import PdfReader
    r=PdfReader(path)
    if r.is_encrypted and not r.decrypt(''):raise ValueError('PDF 已加密：请先在原软件中用密码另存副本')
    return r

def safe_pdf(r):
    root=r.trailer['/Root']
    for key in ['/AcroForm','/Names','/StructTreeRoot','/Collection']:
        if key in root:raise ValueError('该 PDF 包含表单、附件、命名目标或结构标签；请使用严格 RAG 模式或原软件处理')
    if any(p.get('/Annots') for p in r.pages):raise ValueError('PDF 含交互批注/链接，普通合并不能验证保留；原件保留')

# Set per job by one(): encoder threads for page export, and a reusable
# LibreOffice profile directory owned by this worker slot.
THREADS=1
OFFICE_PROFILE=None
PROGRESS=None  # per-item progress file polled by the supervisor
_last_progress=0.0

STAGING_MAX_AGE=24*3600

def sweep_staging(root):
    """Remove staging folders a previous run left behind when it could not clean up.

    one() removes its own staging directory on every path it can observe, but a
    crash, an OOM kill or a power loss leaves it behind, and a 1200-page export can
    leave hundreds of megabytes of intermediate pages there. Only folders that have
    not been written to for STAGING_MAX_AGE are removed, so a long job running in
    another process keeps updating its own folder's mtime and is never touched.
    """
    try:
        if not root.is_dir():return 0
    except OSError:return 0
    cutoff=time.time()-STAGING_MAX_AGE;removed=0
    try:entries=list(root.iterdir())
    except OSError:return 0
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime>=cutoff:continue
            shutil.rmtree(entry,ignore_errors=True)
            removed+=1
        except OSError:pass
    return removed

def replace_with_retry(source,target,attempts=6):
    """os.replace that tolerates a reader holding the target open.

    POSIX lets a rename through while another process reads the old file; Windows
    does not, and the writer gets WinError 5 (access denied). The readers here are
    this engine's own parent process (progress) and the acceptance harness (result
    files), so a short retry turns an intermittent failure into a non-event instead
    of a failed conversion. This was a real Windows failure: the parent polls a
    shard's progress file while the shard replaces it, and the shard then died.
    """
    delay=0.01
    for attempt in range(1,attempts+1):
        try:
            os.replace(source,target);return
        except OSError:
            if attempt==attempts:raise
            time.sleep(delay);delay=min(delay*2,0.2)

def report_progress(done,total,force=False):
    global _last_progress
    if not PROGRESS:return
    now=time.monotonic()
    if not force and now-_last_progress<0.25:return
    _last_progress=now
    tmp=Path(PROGRESS+'.tmp')
    try:
        platform_text.write_text(tmp,json.dumps({'done':done,'total':total}))
        replace_with_retry(tmp,PROGRESS)
    except OSError:
        # Progress is advisory: the parent and the GUI read it to draw a bar. A
        # stalled bar is a cosmetic problem; letting this escape fails the job.
        try:tmp.unlink()
        except OSError:pass

def encode_page(im,ext,cfg):
    """Encode one finished page to the exact bytes save_page writes to disk.

    The bytes are produced once and then used for both the page cache and this
    run's staging file, so a cache write failure can still produce a normal
    export without rendering the page a second time.
    """
    buffer=io.BytesIO()
    try:
        if ext=='jpeg':im.convert('RGB').save(buffer,'JPEG',quality=cfg['quality'],dpi=(cfg['dpi'],cfg['dpi']))
        else:im.save(buffer,'PNG',dpi=(cfg['dpi'],cfg['dpi']))
        return buffer.getvalue()
    finally:buffer.close()

def _test_slow_publish():
    """Test-only delay inside the publish loop (default off).

    The publish section blocks SIGTERM/SIGINT on purpose, so the only thing that
    keeps cancellation fast is the safe point between files and the marker-based
    cancel grace period. Both are easy to break silently, so
    tests/test_cli.py::test_ctrl_c_during_a_slow_publish_exits_quickly sets
    CHAMO_TEST_SLOW_PUBLISH to widen that window on a fast machine. Production never
    sets it, and the sleep is outside the file/rename bookkeeping, so it cannot
    change ordering, naming or rollback behaviour.
    """
    delay=os.environ.get('CHAMO_TEST_SLOW_PUBLISH')
    if delay:
        try:time.sleep(float(delay))
        except (TypeError,ValueError):pass

def _test_slow_encode():
    """Test-only delay/trace for the cancel-latency gate (default off).

    A real CI runner encodes pages far slower than this machine, which is what
    made "Ctrl-C must exit quickly" fail there. The gate needs that slowness to be
    reproducible, so it sets CHAMO_TEST_SLOW_ENCODE; production never sets it. The
    optional trace file records when each encode starts, so the gate can prove that
    queued encodes are dropped on cancellation instead of only measuring a wall
    clock on one particular machine.
    """
    delay=os.environ.get('CHAMO_TEST_SLOW_ENCODE')
    if not delay:return
    trace=os.environ.get('CHAMO_TEST_ENCODE_LOG')
    if trace:
        try:
            # Wall clock on purpose: the gate compares these stamps with the time
            # it sent SIGINT, which lives in the test process, not in this one.
            with open(trace,'a',encoding='utf-8') as handle:handle.write('%.6f\n'%time.time())
        except OSError:pass
    try:time.sleep(float(delay))
    except (TypeError,ValueError):pass

def save_page(im,dest,ext,cfg):
    _test_slow_encode()
    # Cache-less path (office_images, pdf_compress): write the staging page
    # directly and prove it decodes, exactly like the pre-cache engine.
    if ext=='jpeg':im.convert('RGB').save(dest,quality=cfg['quality'],dpi=(cfg['dpi'],cfg['dpi']))
    else:im.save(dest,dpi=(cfg['dpi'],cfg['dpi']))
    with Image.open(dest) as check:check.load()
    im.close()

# ---- page-level resume cache (pdf_images) ----------------------------------
# Every finished page is committed to the state directory before it is copied
# into this run's staging folder. The next run verifies a page's sidecar
# (sha256 + bytes + render settings) and copies a hit instead of rendering it,
# so a cancelled or killed export resumes page by page. There is deliberately no
# global manifest: each page file is committed atomically and its sidecar after
# it, which is why a kill -9 loses at most the page that was being written and a
# half-written page is ignored forever. Cache problems are never fatal: any
# miss, corrupt entry or unwritable cache just renders the page the old way.
PAGE_CACHE_VERSION='page-cache-v1'
# Set by convert() and read by one(); a worker process handles its jobs
# sequentially, so a module slot is enough (same pattern as COMPRESS_INFO).
PAGE_RESUME={}

def _render_ext(cfg):
    ext=cfg.get('format','png')
    return ext if ext in ('png','jpeg') else 'png'

def page_cache_key(cfg):
    """Identity of the rendered bytes: format, DPI, and quality for JPEG only.

    range/stem/output folder are deliberately absent: they do not change a page's
    bytes, and a page cached by one selection must be reusable by another.
    """
    ext=_render_ext(cfg)
    parts=[PAGE_CACHE_VERSION,ext,int(cfg.get('dpi',150)),
           int(cfg.get('quality',90)) if ext=='jpeg' else None]
    return hashlib.sha256(json.dumps(parts).encode('utf-8')).hexdigest()[:20]

def _cache_private_dirs(directory,root):
    """Best-effort 0700 on the cache folders below the cache root."""
    try:
        current=directory
        while current==root or current.is_relative_to(root):
            try:os.chmod(current,0o700)
            except OSError:pass
            if current==root:break
            current=current.parent
    except (OSError,AttributeError):pass

def _cache_touch_marker(cache,force=False):
    """Record that a live process is writing this cache set.

    The marker carries the owner pid and a wall-clock timestamp. A sweep in
    another process skips the set while the marker is fresh; a marker left behind
    by a killed writer is dropped by platform_state.sweep_page_cache once it is
    older than PAGE_CACHE_ACTIVE_WINDOW, so the set becomes governable again.
    """
    marker=cache.get('marker')
    if not marker:return
    now=time.monotonic()
    if not force and now-(cache.get('marker_at') or 0.0)<60:return
    cache['marker_at']=now
    try:
        marker.parent.mkdir(parents=True,exist_ok=True)
        platform_text.write_text(marker,json.dumps({'pid':os.getpid(),'role':'page-cache-writer',
                                                    'updated_at':round(time.time(),3)}))
    except OSError:pass

def page_cache_open(cfg,source_sha,request=None):
    """Per-job cache context, or None when the cache does not apply.

    None means "behave exactly like the pre-cache engine" (CHAMO_PAGE_CACHE=0 or a
    non-pdf_images operation): render() writes pages straight to staging and the
    job record gains no new fields.
    """
    if cfg.get('operation')!='pdf_images':return None
    if not platform_state.page_cache_enabled():return None
    cache={'enabled':True,'usable':False,'dir':None,'marker':None,
           'reused':0,'rendered':0,'unavailable':0,'owner':True,'marker_at':0.0}
    try:
        root=platform_state.page_cache_root(request)
        directory=root/'pdf_images'/source_sha/page_cache_key(cfg)
        cache['dir']=directory;cache['marker']=directory/platform_state.PAGE_CACHE_MARKER
        directory.mkdir(parents=True,exist_ok=True)
        _cache_private_dirs(directory,root)
        _cache_touch_marker(cache,force=True)
        cache['usable']=True
    except Exception:
        # A cache that cannot be prepared costs one optimisation, not the export.
        cache['unavailable']=1
    return cache

def page_cache_adopt(directory):
    """Child-process view of a cache folder its parent already validated."""
    if not directory:return None
    try:base=Path(directory)
    except (TypeError,ValueError):return None
    return {'enabled':True,'usable':True,'dir':base,
            'marker':base/platform_state.PAGE_CACHE_MARKER,
            'reused':0,'rendered':0,'unavailable':0,'owner':False,'marker_at':0.0}

def page_cache_close(cache):
    """Drop this job's marker once its pages are written (best effort)."""
    if not cache or not cache.get('owner'):return
    try:
        if cache.get('marker'):cache['marker'].unlink(missing_ok=True)
    except OSError:pass

def _cache_page_paths(cache,page,ext):
    directory=cache['dir']
    return directory/f'page{page:05d}.{ext}',directory/f'page{page:05d}.json'

def _cache_store_page(cache,page,ext,cfg,data):
    """Atomically publish one page and its sidecar; raises when unwritable."""
    directory=cache['dir'];directory.mkdir(parents=True,exist_ok=True)
    page_path,sidecar=_cache_page_paths(cache,page,ext)
    temp=page_path.with_name(page_path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        with temp.open('xb') as handle:handle.write(data)
        replace_with_retry(temp,page_path)
    finally:
        try:temp.unlink(missing_ok=True)
        except OSError:pass
    # The sidecar is written after the page: a reader that finds a sidecar can
    # trust the bytes, and a kill between the two writes only wastes one page.
    atomic_json(sidecar,{'version':PAGE_CACHE_VERSION,'page':page,'format':ext,
                         'dpi':int(cfg['dpi']),'quality':int(cfg['quality']) if ext=='jpeg' else None,
                         'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                         'created_at':time.time()},durable=False)
    _cache_touch_marker(cache)
    return True

def save_page_cached(im,dest,ext,cfg,cache,page):
    """Encode once, commit to the cache, then publish this run's staging page.

    Returns True when the cache commit succeeded, False when the page fell back
    to a cache-less write: a cache problem must never fail the export.
    """
    _test_slow_encode()
    data=encode_page(im,ext,cfg);im.close()
    stored=False
    try:stored=_cache_store_page(cache,page,ext,cfg,data)
    except Exception:stored=False
    if stored:
        try:
            page_path,_=_cache_page_paths(cache,page,ext)
            shutil.copyfile(page_path,dest)
            with Image.open(dest) as check:check.load()
            return True
        except OSError:
            pass  # copy failed: the bytes already in memory are authoritative
    dest.write_bytes(data)
    with Image.open(dest) as check:check.load()
    return False

def _page_cache_reuse(cache,page,ext,cfg,dest):
    """Copy one verified cache page into staging; False means "render it"."""
    try:
        page_path,sidecar=_cache_page_paths(cache,page,ext)
        meta=platform_text.read_json(sidecar)
        if not isinstance(meta,dict):return False
        digest=meta.get('sha256')
        if not isinstance(digest,str) or len(digest)!=64:return False
        if meta.get('version')!=PAGE_CACHE_VERSION:return False
        if int(meta.get('page',0))!=page or meta.get('format')!=ext:return False
        if int(meta.get('dpi',-1))!=int(cfg['dpi']):return False
        if ext=='jpeg' and int(meta.get('quality',-1))!=int(cfg['quality']):return False
        if page_path.stat().st_size!=int(meta.get('bytes',-1)):return False
        shutil.copyfile(page_path,dest)
        # Hash what was copied, not the source: this also catches a corrupt copy.
        if sha(dest)!=digest:
            dest.unlink(missing_ok=True)
            return False
        return True
    except (OSError,ValueError,TypeError):
        try:dest.unlink(missing_ok=True)
        except OSError:pass
        return False

def render(path, work, cfg, stem, selected=None, cache=None):
    """Render the selected pages into work, reusing verified cache pages.

    A cache hit counts toward progress exactly like a rendered page, so the event
    stream keeps its meaning (done/total are pages of this job). Any cache error
    only costs a re-render; it is reported through cache_unavailable and never
    fails the export.
    """
    import pypdfium2 as pdfium
    # pdfium is not thread-safe, so pages render on this thread; PNG/JPEG
    # encoding releases the GIL and runs on a pool. In-flight pages are capped
    # by a ~512 MB pixel budget so high-DPI exports cannot exhaust memory.
    ext=_render_ext(cfg)
    stats={'enabled':bool(cache and cache.get('enabled')),
           'usable':bool(cache and cache.get('usable')),
           'reused':0,'rendered':0,
           'unavailable':int((cache or {}).get('unavailable',0) or 0)}
    # The pool is not a "with" block: on cancellation the queued encodes must be
    # dropped instead of drained. Draining them made Ctrl-C wait for every page
    # still in the queue (2 * THREADS encodes), which on a slow CI runner is the
    # difference between a ~1 s cancel and a 30 s timeout. Only the encodes already
    # running are waited for, so no thread can write into the staging folder while
    # the cancelled job is rolled back.
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=THREADS)
    try:
      with pdfium.PdfDocument(str(path)) as doc:
        pending=[];budget=512_000_000;used=0
        # selected is the 0-based page index list to render. A shard worker passes
        # its own slice of the same list, so page numbering (and therefore the
        # output file names) stays absolute: shards never collide, and the
        # unsharded run produces the identical names.
        if selected is None:selected=pages(cfg.get('range',''),len(doc))
        finished=0
        report_progress(0,len(selected),True)
        def settle(item):
            future,cost,cached=item
            cache_ok=future.result()
            if cached and cache_ok is False and cache is not None:
                cache['unavailable']=cache.get('unavailable',0)+1
            return cost
        for n in selected:
            dest=work/f'{stem}_page{n+1:05d}.{ext}'
            # Only a page whose sidecar and bytes both verify is reused; anything
            # missing, stale or corrupt falls through to a normal render, which
            # then replaces the bad cache entry.
            if cache and cache.get('usable') and _page_cache_reuse(cache,n+1,ext,cfg,dest):
                cache['reused']=cache.get('reused',0)+1
                finished+=1;report_progress(finished,len(selected));continue
            page=doc[n];scale=cfg['dpi']/72
            w,h=page.get_size()
            if w*h*scale*scale>64_000_000:raise ValueError('单页渲染超过 6400 万像素，请主动降低 DPI')
            bitmap=page.render(scale=scale);im=bitmap.to_pil();cost=im.width*im.height*4
            bitmap.close();page.close()
            while pending and (used+cost>budget or len(pending)>=THREADS*2):
                used-=settle(pending.pop(0));finished+=1;report_progress(finished,len(selected))
            if cache and cache.get('usable'):
                pending.append((pool.submit(save_page_cached,im,dest,ext,cfg,cache,n+1),cost,True))
            else:
                pending.append((pool.submit(save_page,im,dest,ext,cfg),cost,False))
            if cache is not None:cache['rendered']=cache.get('rendered',0)+1
            used+=cost
        for item in pending:
            settle(item);finished+=1;report_progress(finished,len(selected))
        report_progress(len(selected),len(selected),True)
    finally:
        # Queued encodes are dropped on the way out (a cancel must not wait for
        # pages that never started), while the encodes already running are still
        # waited for: they write into the staging folder that is about to be
        # removed, and leaving a writer behind is exactly the residue the cancel
        # contract forbids. shutdown(cancel_futures=True) does both, and on a
        # normal run the queue is already empty, so it changes nothing there.
        pool.shutdown(wait=True,cancel_futures=True)
    if cache is not None:
        stats['reused']=cache.get('reused',0)
        stats['rendered']=cache.get('rendered',0)
        stats['unavailable']=cache.get('unavailable',0)
        stats['usable']=cache.get('usable',False)
    else:
        stats['rendered']=len(selected)
    return stats

# Content-aware page-range sharding for pdf_images. A single-file export is
# capped at one worker process (workers=max(1,min(workers,len(jobs)))), so one
# big scanned PDF cannot use more than one process however many cores are free.
# Splitting a scanned PDF by page range recovers that parallelism; splitting a
# text/vector PDF is a measured net loss (0.857x on a text benchmark), which is why the decision is content-aware and every doubt or
# failure falls back to the existing single-process path.
#
# Whether sharding pays is decided by a cost model, not by page-count constants.
# An earlier rule used two fixed thresholds (96 pages, 60 MPix) tuned on one
# page-count sweep. They miss in both directions: a 48-page high-DPI scan has the
# raster work for several processes but is refused by the page count, while a
# 96-page low-DPI document sits at the pixel threshold without the work to repay
# the processes.
#
# The model compares the predicted one-process time with the predicted sharded
# time. Both paths pay the same engine fixed cost F (worker start, report,
# publish), but F does not cancel out of the user-visible ratio: F inside a 1 s
# export is a different deal from F inside a 10 s one. Every constant below was
# measured end-to-end on the shipped CLI with forced sharding (min of two
# repetitions per arm):
#
#   F = 0.33 s      engine fixed cost, measured on a one-page PDF
#   u = 0.10 s      effective cost of one more shard process (interpreter, engine
#                   import, PDF open). A standalone one-page child measured
#                   0.129 s; u fits the multi-page arms because the parent starts
#                   the children back to back, so a later child's start-up
#                   overlaps the earlier ones' rendering.
#   R = 160 MPix/s  single-process raster throughput (measured 163-171 MPix/s
#                   over four 48-240 page A4 scans at 150 dpi).
#
# Predicted vs measured end-to-end speed-up (same host, 2/3/4 shards forced):
#   104 MPix, 48 pages : predicted 1.16x at 3 shards; measured 1.20/1.35/1.27x
#   104 MPix, 96 pages : predicted 1.16x at 3 shards; measured 1.21/1.31/1.30x
#   209 MPix, 96 pages : predicted 1.55x at 4 shards; measured 1.37/1.67/1.66x
#   522 MPix, 240 pages: predicted 2.44x at 6 shards; measured 1.64/2.24/2.24x
#   small pages (25 MPix, 96 pages): predicted 0.80x, refused; the old rule sharded them
#                        anyway and they measured 0.74x (old sweep)
# The table is warm-cache, and the host sat around loadavg 7-10. The 1.10 floor
# leaves about 10% headroom, which is smaller than the wall-clock spread a busy
# host can show: the same 104 MPix fixture measured 0.976x/0.963x/0.881x at 2-4
# shards under loadavg 16, so a boundary document can still lose a few percent
# on a loaded machine. --shard off remains the escape hatch. u is an effective
# fitted value, not a directly measurable start-up: standalone children started in
# 0.119-0.141 s, a least-squares fit over the arms gave 0.078-0.088 s, and 0.10
# sits between the two.
#
# Two rules keep the model from being the only guard:
#   * SHARD_MIN_GAIN: predicted end-to-end gain must be at least 10%. A
#     "speed-up" smaller than that is not worth extra processes or the risk.
#   * MIN_PAGES_PER_SHARD=16: a shard must hold at least 16 pages. The per-process
#     cost is measured on multi-page shards, and this also keeps a 20-page
#     document of huge pages from being split 4 ways.
# --workers stays the ceiling (and shard_budgets keeps the core cap); the model
# may choose fewer processes than requested: on the 522 MPix case six shards
# measured 2.075x against four shards' 2.24x on a loaded host (a quieter run had
# them level, 2.229x vs 2.203x), because each extra process pays its cost without
# adding throughput. Choosing the smaller fleet on a tie bounds that risk.
SHARD_ENGINE_FIXED=0.33
SHARD_PROCESS_COST=0.10
SHARD_PIXEL_RATE=160_000_000
SHARD_MIN_GAIN=1.10
MIN_PAGES_PER_SHARD=16

def shard_gain(shards,cores,total_pixels):
    """Predicted end-to-end speed-up of `shards` processes over one process.

    one = F + W/R; sharded = F + shards*u + W/(R*parallel), with parallel =
    min(shards,cores): processes beyond the core count queue for a core instead
    of adding throughput. F is the engine's own fixed cost, paid once either way,
    and it is why a 1.2x on a small document is not the same as a 1.2x on a big
    one: it shrinks the ratio and does not cancel from it.
    """
    if total_pixels<=0 or shards<2:return 1.0
    work=total_pixels/SHARD_PIXEL_RATE
    parallel=min(shards,max(1,int(cores or 1)))
    return (SHARD_ENGINE_FIXED+work)/(SHARD_ENGINE_FIXED+shards*SHARD_PROCESS_COST+work/parallel)

def shard_count(selected_pages,total_pixels,budget,cores):
    """Best predicted shard count for this job, or 1 to keep the existing path.

    Returns (shards,predicted_gain). The gain is returned even on a refusal so the
    job report can say why in numbers instead of adjectives. The search is
    exhaustive over the few legal counts (<= budget, <= cores, <= pages/16) and
    prefers FEWER processes on a tie: the gain is a model, and the smaller fleet
    is the one with less start-up to lose if the model is wrong.
    """
    pages=max(0,int(selected_pages or 0))
    limit=min(int(budget or 1),pages//MIN_PAGES_PER_SHARD,max(1,int(cores or 1)))
    candidates=[(shards,shard_gain(shards,cores,total_pixels)) for shards in range(2,limit+1)]
    if not candidates:return 1,1.0
    # max() keeps the first maximum, i.e. the smaller fleet on a tie (see above).
    shards,gain=max(candidates,key=lambda item:item[1])
    return (shards,gain) if gain>=SHARD_MIN_GAIN else (1,gain)

def shard_budgets(operation,requested,jobs):
    """Worker processes each job may use when its pages are sharded (1 = none).

    The supervisor caps workers at the job count, which is exactly the cap that
    keeps a single file on one process. The requested count beyond that cap is
    the spare capacity sharding may claim; it is split evenly so a two-file
    batch does not start two full-sized shard fleets.

    The count is also capped by the machine's cores. Sharding pays by running
    pages in parallel, so processes beyond the core count only add the fixed
    per-shard start-up. Measured on the packaged CLI, one 96-page 150 dpi scan
    (104 MPix): 4 shards on a 2-core CI runner 0.949x/0.941x (slower), the same
    4 shards on a 10-core machine 1.52x, 6 shards on 240 pages 2.24x. The user's
    --workers still decides how much of the machine to use; it can no longer ask
    for more shard processes than there are cores to run them on.
    """
    cores=os.cpu_count() or requested
    requested=min(requested,max(1,cores))
    if operation!='pdf_images' or requested<=jobs:return [1]*jobs
    extra=requested-jobs
    return [1+extra//jobs+(1 if index<extra%jobs else 0) for index in range(jobs)]

def _expected_page_files(stem,selected,cfg):
    ext=cfg.get('format','png');ext=ext if ext in ['png','jpeg'] else 'png'
    return [f'{stem}_page{n+1:05d}.{ext}' for n in selected]

def _read_json_file(path):
    try:return platform_text.read_json(path)
    except (OSError,ValueError,TypeError):return None

def _read_child_result(path,timeout=10.0):
    """Read a shard's result file, tolerating a transient failure.

    A child that has exited is not guaranteed to have a readable result file yet:
    on Windows an antivirus scan can hold a freshly written file briefly, and the
    read then fails with a permission error even though the write succeeded. Treating
    that as "the shard process died" failed whole exports intermittently, so retry
    for a short window before giving up. Returning None here is the real
    "no result" answer.
    """
    deadline=time.monotonic()+timeout
    while True:
        value=_read_json_file(path)
        if value is not None:return value
        if time.monotonic()>=deadline:return None
        time.sleep(.02)

def _stop_children(children):
    """Stop shard processes by pid, individually.

    They share this process group so that the supervisor's pause/resume/cancel
    reach them together. That also means platform_process.kill_tree() would
    signal the group this process belongs to and kill the caller, so the parent
    signals each child directly instead.
    """
    for proc in children:
        if proc.poll() is not None:continue
        try:proc.terminate()
        except OSError:pass
    for proc in children:
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:proc.kill()
            except OSError:pass
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:pass

def render_sharded(path,work,cfg,stem,selected,shards,cache=None):
    """Render the selected pages with several worker processes.

    Every shard opens the same original file and renders only its own slice of
    the selected page list into the shared staging directory, using the same
    absolute page numbering as the unsharded path. The caller publishes the
    union exactly once, so naming and folder allocation stay job-level.

    Each child receives the page-cache folder (never a manifest) and writes only
    its own pages: two children never share a file, so there is nothing to
    corrupt, and a cancelled child leaves at most the page it was writing. The
    parent sums the children's reused/rendered counters for the job record.
    """
    expected=_expected_page_files(stem,selected,cfg)
    cache_dir=str(cache['dir']) if cache and cache.get('enabled') and cache.get('usable') else ''
    cache_stats={'enabled':bool(cache is not None),'usable':bool(cache and cache.get('usable')),
                 'reused':0,'rendered':0,
                 'unavailable':int((cache or {}).get('unavailable',0) or 0)}
    ctrl=work/'.shard';ctrl.mkdir(exist_ok=True)
    step=-(-len(selected)//shards)
    parts=[selected[k*step:(k+1)*step] for k in range(shards)]
    parts=[part for part in parts if part]
    threads=max(1,min(8,(os.cpu_count() or 4)//len(parts)))
    engine=str(Path(__file__).resolve())
    children=[];logs=[];results=[];progress=[]
    try:
        for index,part in enumerate(parts):
            spec={'index':index,'source':str(path),'work':str(work),'stem':stem,
                  'selected':part,'threads':threads,
                  'config':{key:cfg[key] for key in ('format','dpi','quality') if key in cfg},
                  'cache':cache_dir,
                  'progress':str(ctrl/f'child-{index}.progress'),
                  'result':str(ctrl/f'child-{index}.result.json')}
            spec_path=ctrl/f'child-{index}.json';atomic_json(spec_path,spec,durable=False)
            results.append(Path(spec['result']));progress.append(Path(spec['progress']))
            # Children are spawned without a new session so pause/resume/cancel
            # sent to this process group reaches them too. Their stdout/stderr
            # go to this job's own log, which the supervisor already captures.
            log=platform_text.open_text(ctrl/f'child-{index}.log','w');logs.append(log)
            children.append(subprocess.Popen([sys.executable,'-I','-B',engine,'--shard',str(spec_path)],stdout=log,stderr=log))
        finished_total=len(selected)
        while True:
            for index,proc in enumerate(children):
                if proc.poll() is None:continue
                result=_read_child_result(results[index])
                if result is None:
                    tail=''
                    try:tail=platform_text.read_text(ctrl/f'child-{index}.log')[-800:]
                    except OSError:pass
                    raise ValueError(f'分片渲染进程异常退出（分片 {index+1}/{len(parts)}，退出码 {proc.returncode}）；该作业未完成，原件保留。'+tail)
                if result.get('state')!='ok':
                    raise ValueError(f'分片渲染失败（分片 {index+1}/{len(parts)}）：{result.get("error") or "未知错误"}；该作业未完成，原件保留')
            done=0
            for index,progress_path in enumerate(progress):
                state=_read_json_file(progress_path)
                if not isinstance(state,dict):state={}
                done+=max(0,min(int(state.get('done',0)),len(parts[index])))
            report_progress(done,finished_total)
            if all(proc.poll() is not None for proc in children):break
            time.sleep(.05)
        files=[]
        for index,result_path in enumerate(results):
            result=_read_child_result(result_path)
            if not result or result.get('state')!='ok':
                raise ValueError(f'分片渲染失败（分片 {index+1}/{len(parts)}）；该作业未完成，原件保留')
            files.extend(result.get('files') or [])
            child_stats=result.get('cache') or {}
            for field in ('reused','rendered','unavailable'):
                cache_stats[field]+=int(child_stats.get(field,0) or 0)
        # Anti-cheat invariant: the union must be exactly the selected page set
        # -- no page rendered twice, no page missing. Nothing is published until
        # this holds.
        if len(files)!=len(set(files)) or set(files)!=set(expected):
            missing=sorted(set(expected)-set(files))[:5];extra=sorted(set(files)-set(expected))[:5]
            raise ValueError(f'分片渲染页集合不完整（缺 {len(set(expected)-set(files))} 页，多 {len(set(files)-set(expected))} 页）{missing}{extra}；该作业未完成，原件保留')
        for name in expected:
            if not (work/name).is_file():
                raise ValueError(f'分片渲染缺少输出文件 {name}；该作业未完成，原件保留')
        report_progress(finished_total,finished_total,True)
        return cache_stats
    finally:
        _stop_children(children)
        for log in logs:
            try:log.close()
            except OSError:pass

def shard_render(path,work,cfg,stem,budget,info,cache=None):
    """Decide whether this pdf_images job is worth sharding, then render it.

    All frozen trigger conditions are checked here; the fallback is the existing
    single-process render, including for every classifier failure. The info dict
    is filled in for the job record so an acceptance run can prove from the
    report whether sharding ran, and why not when it did not.

    Returns the page-cache accounting dict for the job (reused/rendered counts);
    it is aggregated from the shard children when sharding is used.
    """
    mode=cfg.get('shard','auto');info.update(mode=mode,used=False,shards=1)
    if mode=='off':
        info['reason']='分片已关闭（shard=off）';return render(path,work,cfg,stem,cache=cache)
    if budget<=1:
        info['reason']=f'可用工作进程数不足（{budget} < 2）；按单进程渲染'
        return render(path,work,cfg,stem,cache=cache)
    try:
        import pageclass
        decision=pageclass.classify(path)
    except Exception as error:
        # Even an unimportable classifier must not change the conversion: this is
        # the same "never shard on doubt" rule that classify() itself follows.
        info['reason']=f'内容判定不可用，按现有行为处理：{type(error).__name__}: {error}'
        return render(path,work,cfg,stem,cache=cache)
    info.update(kind=decision['kind'],pages=decision['pages'],sampled=decision['sampled'],
                image_pages=decision['image_pages'],decision=decision['reason'])
    if decision['kind']!='image' or not decision['pages']:
        info['reason']='内容判定未通过：'+decision['reason']
        return render(path,work,cfg,stem,cache=cache)
    total=decision['pages']
    selected=pages(cfg.get('range',''),total)
    # Raster work, not page count: small pages are cheap to render, so extra
    # processes cost more than the parallelism saves (measured 0.74x on 96 small
    # pages). Rendered pixels are the work unit the cost model compares with the
    # per-process start-up.
    area=decision.get('area_pt2')
    per_page=(area or 0.0)*(cfg['dpi']/72.0)**2
    total_pixels=per_page*len(selected)
    cores=os.cpu_count() or 1
    shards,gain=shard_count(len(selected),total_pixels,budget,cores)
    info.update(area_pt2=area,pixels_per_page=round(per_page),total_pixels=round(total_pixels),
                cores=cores,gain_floor=SHARD_MIN_GAIN,predicted_gain=round(gain,3))
    if shards<=1:
        # Two different things can forbid sharding, and the report must not blame
        # the gain when the legal limits are the cause: e.g. a 20-page document of
        # huge pages has plenty of raster work, but every shard needs 16 pages.
        limits=[]
        if int(budget or 1)<2:limits.append(f'可用进程数 {budget} < 2')
        if int(cores or 1)<2:limits.append(f'可用核数 {cores} < 2')
        if len(selected)//MIN_PAGES_PER_SHARD<2:
            limits.append(f'选中 {len(selected)} 页，不足 2 片 × 每片最少 '
                          f'{MIN_PAGES_PER_SHARD} 页')
        if limits:
            info['reason']=f'当前上限不允许分片（{"；".join(limits)}）；按单进程渲染'
        else:
            info['reason']=(f'预计分片收益 {gain:.3f}×，低于 {SHARD_MIN_GAIN:.2f}× 门槛'
                            f'（选中 {len(selected)} 页约 {total_pixels/1e6:.0f} MPix，'
                            f'可用 {cores} 核、{budget} 个进程）；按单进程渲染')
        return render(path,work,cfg,stem,cache=cache)
    info.update(used=True,shards=shards,selected=len(selected),
                reason=(f'整页扫描件：选中 {len(selected)} 页约 {total_pixels/1e6:.0f} MPix，'
                        f'预计 {gain:.3f}×，分给 {shards} 个进程'))
    return render_sharded(path,work,cfg,stem,selected,shards,cache=cache)

def office_pdf(path, work, cfg):
    # The request carries the renderer the app wants to use. When it is absent or
    # not executable here (a macOS bundle on Linux, a moved install), fall back to
    # platform discovery instead of failing on a stale path.
    exe=Path(cfg['soffice']) if cfg.get('soffice') else None
    if exe is None or not platform_office.runnable(exe):
        discovered=platform_office.find(BASE)
        if not discovered:raise ValueError('缺少 Office 渲染引擎：未找到可执行的 LibreOffice')
        exe=Path(discovered)
    target=work/'office';target.mkdir()
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    pdf=target/(path.stem+'.pdf')
    # A warm profile saves 1–2 s of first-start setup per document. Profiles are
    # per worker slot, so concurrent LibreOffice processes never share one; a
    # failure with a reused profile is retried once with a throwaway profile.
    profiles=[Path(OFFICE_PROFILE)] if OFFICE_PROFILE else []
    profiles.append(work/'profile')
    for profile in profiles:
        result=subprocess.run([str(exe),'-env:UserInstallation='+profile.as_uri(),'--headless','--nologo','--nodefault','--nofirststartwizard','--norestore','--convert-to','pdf','--outdir',str(target),str(path)],capture_output=True,timeout=cfg.get('timeout',600),env=env)
        if not result.returncode and pdf.is_file():break
    if result.returncode or not pdf.is_file():raise ValueError('Office 渲染失败：文件损坏、加密或格式不支持。'+result.stderr.decode(errors='replace')[-300:])
    reader(pdf)
    return pdf

def image_output(source, target, cfg, compress=False):
    with Image.open(source) as raw:
        if getattr(raw,'n_frames',1)>1:raise ValueError('多帧/动画图片需要保留所有帧，本操作暂不支持')
        raw.load()
        im=ImageOps.exif_transpose(raw)
        ext=cfg.get('format','png')
        opts={}
        if raw.info.get('icc_profile'):opts['icc_profile']=raw.info['icc_profile']
        if im.getexif():opts['exif']=im.getexif().tobytes()
        if cfg.get('max_edge',0)>0:
            if not cfg.get('lossy'):raise ValueError('缩放需要主动启用质量/尺寸调整')
            im.thumbnail((cfg['max_edge'],cfg['max_edge']),Image.Resampling.LANCZOS)
        if ext=='jpeg':
            if 'A' in im.getbands() or im.mode=='P':
                rgba=im.convert('RGBA');background=Image.new('RGB',im.size,'white');background.paste(rgba,mask=rgba.getchannel('A'));im=background
            else:im=im.convert('RGB')
            opts['quality']=cfg['quality'];opts['optimize']=True
        if ext=='png':opts['optimize']=True
        if ext=='webp':opts.update(lossless=not cfg.get('lossy'),quality=cfg['quality'])
        if ext=='bmp':opts={}
        if ext=='tiff':opts['compression']='tiff_deflate'
        im.save(target,format={'jpeg':'JPEG','png':'PNG','tiff':'TIFF','bmp':'BMP','webp':'WEBP'}[ext],**opts)
        with Image.open(target) as check:check.load()
        im.close()

def convert(sources, work, cfg, shard_budget=1, shard_info=None, cache=None):
    op=cfg['operation'];s=sources[0];stem=s.name
    if op in ('image_convert','image_compress','images_pdf'):
        normalized=[]
        for index,original in enumerate(sources):
            if original.suffix.lower() in ('.heic','.heif'):
                helper=BASE.parent.parent/'Helpers/dwnative'
                if not helper.exists():helper=BASE.parent/'build/dwnative'
                converted=work/'decoded'/f'{index}.tiff';converted.parent.mkdir(exist_ok=True)
                p=subprocess.run([str(helper),str(original),str(converted)],capture_output=True,timeout=120)
                if p.returncode:raise ValueError(p.stderr.decode(errors='replace') or 'HEIC 解码失败')
                normalized.append(converted)
            else:normalized.append(original)
        sources=normalized;s=sources[0]
    if op=='pdf_images':
        reader(s)
        # Content-aware page-range sharding applies to this operation only. The
        # page cache context is handed down so single-process and sharded
        # rendering share the same read/write protocol (shard children receive
        # the cache folder in their spec).
        PAGE_RESUME.update(shard_render(s,work,cfg,stem,shard_budget,
                                        shard_info if shard_info is not None else {},cache=cache))
    elif op in ('office_pdf','office_images'):
        if s.suffix.lower() not in OFFICE:raise ValueError('请选择 Office 文档')
        pdf=office_pdf(s,work,cfg)
        if op=='office_images':render(pdf,work,cfg,stem)
        elif cfg.get('range',''):
            from pypdf import PdfWriter
            r=reader(pdf);w=PdfWriter()
            for n in pages(cfg['range'],len(r.pages)):w.add_page(r.pages[n])
            w.write(work/(stem+'.pdf'))
        else:shutil.copy2(pdf,work/(stem+'.pdf'))
    elif op=='images_pdf':
        import img2pdf
        # Reject silent loss of animation, alpha, EXIF orientation or colour data.
        for x in sources:
            with Image.open(x) as im:
                if getattr(im,'n_frames',1)>1:raise ValueError('多帧图片不能静默只导出首帧')
        # img2pdf embeds JPEG/PNG/TIFF directly but rejects alpha in other formats
        # (WebP, AVIF, BMP, GIF). Re-encode those losslessly to PNG, keeping alpha.
        embedded=[]
        for index,x in enumerate(sources):
            if x.suffix.lower() in ('.jpg','.jpeg','.jfif','.png','.tif','.tiff'):embedded.append(x);continue
            target=work/'decoded'/f'{index}.png';target.parent.mkdir(exist_ok=True)
            with Image.open(x) as im:
                im=ImageOps.exif_transpose(im)
                if im.mode not in ('1','L','LA','RGB','RGBA','P'):im=im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
                im.save(target,'PNG')
            embedded.append(target)
        sources=embedded
        paper=cfg.get('paper','original')
        layout={}
        if paper!='original':
            dimensions={'a4':(595.276,841.890),'a3':(841.890,1190.551),'letter':(612,792)}
            margin=cfg.get('margin_mm',10)*72/25.4
            layout['layout_fun']=img2pdf.get_layout_fun(pagesize=dimensions[paper],border=(margin,margin),fit=img2pdf.FitMode.into,auto_orient=True)
        with (work/(stem+'_merged.pdf')).open('wb') as out:
            img2pdf.convert(*[str(x) for x in sources],outputstream=out,rotation=img2pdf.Rotation.ifvalid,**layout)
    elif op=='pdf_merge':
        from pypdf import PdfWriter
        writer=PdfWriter();expected=0
        for x in sources:
            r=reader(x);safe_pdf(r);expected+=len(r.pages);writer.append(r)
        target=work/(stem+'_merged.pdf');writer.write(target)
        if len(reader(target).pages)!=expected:raise ValueError('合并页数校验失败')
    elif op in ('pdf_split','rag'):
        from cloud_kb_pack.cli import process
        from cloud_kb_pack.config import Limits
        lim=Limits(pdf_pages=cfg['pdf_pages'],pdf_bytes=cfg['pdf_bytes'],image_bytes=cfg.get('image_bytes',20_000_000),document_bytes=cfg.get('document_bytes',150_000_000),text_bytes=cfg.get('text_bytes',10_000_000),table_bytes=cfg.get('table_bytes',10_000_000),table_rows=cfg.get('table_rows',100000),batch_files=cfg.get('batch_files',50))
        store=work/'audit';store.mkdir();upload=work/'upload';upload.mkdir()
        process(s,store,lim,{'policy':'strict','pdf_mode':'split','encoding':'utf-8-sig','page_window':100,'timeout':600,'header_rows':1,'csv_field_bytes':10_000_000},False,tree_relative=Path(s.name),upload_root=upload,work_root=work)
        for x in upload.iterdir():
            if x.is_file():shutil.move(x,work/x.name)
    elif op in ('image_convert','image_compress'):
        if op=='image_compress' and not cfg.get('lossy'):
            target=work/(stem+'_optimized.png')
            lossless_png(s,target)
        else:
            ext=cfg['format'];image_output(s,work/(stem+'.'+ext),cfg,op=='image_compress')
    elif op=='pdf_compress':
        compress_pdf(s,work,cfg)
    else:raise ValueError('未知操作')
    files=sorted((x for x in work.iterdir() if x.is_file() and x.name!='result.json'),key=lambda x:x.name)
    if not files:raise ValueError('未生成输出文件')
    for x in files:
        if x.suffix.lower()=='.pdf':reader(x)
    return files

def lossless_png(source,target):
    import struct,zlib,binascii
    if source.stat().st_size>400_000_000:raise ValueError('PNG 超过本次无损优化内存预算，原件保留')
    data=source.read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('无损优化仅支持 PNG，不自动有损压缩')
    chunks=[];pos=8;compressed=b'';iend=False
    while pos+12<=len(data):
        n=struct.unpack('>I',data[pos:pos+4])[0];tag=data[pos+4:pos+8];body=data[pos+8:pos+8+n]
        if pos+n+12>len(data):raise ValueError('PNG 损坏')
        crc=struct.unpack('>I',data[pos+8+n:pos+12+n])[0]
        if binascii.crc32(tag+body)&0xffffffff!=crc:raise ValueError('PNG CRC 校验失败')
        if tag==b'IDAT':compressed+=body
        chunks.append((tag,body));pos+=n+12
        if tag==b'IEND':iend=True;break
    if not iend:raise ValueError('PNG 缺少结尾')
    if any(t in (b'acTL',b'fcTL',b'fdAT') for t,b in chunks):raise ValueError('动画 PNG 暂不优化')
    # Decode under the same image budget before allocating deflated scanlines.
    with Image.open(source) as im:im.load()
    dec=zlib.decompressobj();raw=dec.decompress(compressed,400_000_001)
    if len(raw)>400_000_000 or not dec.eof:raise ValueError('PNG 解压数据超出内存预算')
    rebuilt=zlib.compress(raw,9);out=bytearray(data[:8]);inserted=False
    for tag,body in chunks:
        if tag==b'IDAT':
            if inserted:continue
            body=rebuilt;inserted=True
        out+=struct.pack('>I',len(body))+tag+body+struct.pack('>I',binascii.crc32(tag+body)&0xffffffff)
    out+=data[pos:]
    target.write_bytes(out if len(out)<len(data) else data)
    with Image.open(source) as a,Image.open(target) as b:
        if a.mode!=b.mode or a.size!=b.size or a.tobytes()!=b.tobytes():raise ValueError('PNG 像素校验不一致')

# PDF compression (pdf_compress). Two explicit modes, never mixed.
#
# Lossless is a structural rewrite: object streams + Flate recompression. The
# gain was measured on synthetic documents:
# a plain 4,767 B PDF -> 4,584 B, a PDF re-embedding the same PNG six times
# 1,301,459 B -> 1,026,693 B. cleanup-attribution-*.json shows the saving is
# Flate recompression, not image de-duplication: the JPEG version does not
# shrink (1.0x), so a file whose streams are already Flate-optimal has nothing
# to gain. Such a file is still delivered, as a byte-identical copy of the
# original (the image_compress precedent: a file is always produced, and the
# report says honestly whether the rewrite helped).
#
# Lossy is explicit only. Every page is rendered with the packaged PDFium at
# --dpi, encoded as JPEG at --quality, and rebuilt into a PDF; the page boxes
# are then set back to the original physical sizes. raster_150_q75 measured
# 0.251x on a 200 dpi synthetic scan.
COMPRESS_DPI=(72,600)
# convert() reports per-item byte counts to one(); a worker process handles its
# jobs sequentially, so a module slot is enough (same pattern as THREADS/PROGRESS).
COMPRESS_INFO=None

def _compress_target(source):
    # The clean naming rule maps this to <stem>_压缩.pdf (see clean_names).
    return source.stem+'_压缩.pdf'

def _sample_pages(total,count=3):
    if total<=count:return list(range(total))
    return sorted({0,total//2,total-1})

def _render_signature(document,index):
    """Bounded, deterministic fingerprint of one rendered page.

    Rendering is the only direct proof that "the page content is unchanged";
    sampling three pages at ~400 px costs milliseconds. Both sides are rendered
    by the same packaged PDFium build, so equal content gives equal bytes.
    """
    page=document[index]
    try:
        width,height=page.get_size()
        bitmap=page.render(scale=min(0.5,400/max(width,height,1.0)))
        try:
            image=bitmap.to_pil()
            try:return (image.width,image.height,image.tobytes())
            finally:image.close()
        finally:bitmap.close()
    finally:page.close()

def _verify_lossless_rewrite(source,rewritten,r):
    """Page count, sampled text and sampled rendering must match the original.

    A pikepdf rewrite is expected to be content-preserving, but "lossless" is a
    promise about the page, not about qpdf's behaviour on this particular file,
    so the published bytes are checked before any product is named.
    """
    import pypdfium2 as pdfium
    check=reader(rewritten)
    if len(check.pages)!=len(r.pages):raise ValueError('无损重写后页数不一致，已保留原件')
    indexes=_sample_pages(len(r.pages))
    if [check.pages[n].extract_text() or '' for n in indexes]!=[r.pages[n].extract_text() or '' for n in indexes]:
        raise ValueError('无损重写后文本抽取与原件不一致，已保留原件')
    with pdfium.PdfDocument(str(source)) as before,pdfium.PdfDocument(str(rewritten)) as after:
        if len(before)!=len(after):raise ValueError('无损重写后页数不一致，已保留原件')
        for index in indexes:
            try:same=_render_signature(before,index)==_render_signature(after,index)
            except Exception as error:same=False
            if not same:raise ValueError(f'无损重写后第 {index+1} 页渲染与原件不一致，已保留原件')

def _has_signature(pdf):
    """True when the document carries a real signature or a certification.

    A form field with /FT /Sig but no /V is an empty placeholder - common in
    fillable forms - and losing it certifies nothing, so only a field with a
    value (or a document certification signature under /Perms /DocMDP) counts.
    Both compression modes refuse real signatures: a rewrite invalidates the
    signature and a raster rebuild erases it while the report would still claim
    success.
    """
    def walk(node,depth):
        if depth>8 or node is None:return False
        try:
            if str(node.get('/FT',''))=='/Sig' and node.get('/V') is not None:return True
            for kid in node.get('/Kids') or []:
                if walk(kid,depth+1):return True
        except (AttributeError,TypeError):return False
        return False
    try:
        form=pdf.Root.get('/AcroForm')
        if form is not None and any(walk(field,0) for field in form.get('/Fields') or []):
            return True
        perms=pdf.Root.get('/Perms')
        return perms is not None and perms.get('/DocMDP') is not None
    except (AttributeError,TypeError):return False

def _refuse_signed(source):
    """Raise when the source carries a signature, for both compression modes."""
    import pikepdf
    try:
        with pikepdf.open(str(source)) as pdf:
            signed=_has_signature(pdf)
    except pikepdf.PdfError:
        return  # the mode's own error path explains an unreadable structure
    if signed:raise ValueError('该 PDF 含数字签名，压缩会使签名失效；原件保留')

def compress_lossless(source,work,r):
    global COMPRESS_INFO
    import pikepdf
    original=source.stat().st_size
    rewritten_path=work/'lossless.pdf'
    try:
        with pikepdf.open(str(source)) as pdf:
            if pdf.is_encrypted:raise ValueError('PDF 已加密：请先在原软件中用密码另存为未加密副本')
            if _has_signature(pdf):
                raise ValueError('该 PDF 含数字签名，压缩会使签名失效；原件保留')
            if len(pdf.pages)!=len(r.pages):raise ValueError('PDF 页数读取不一致，已保留原件')
            # deterministic_id makes two runs on the same input byte-identical,
            # so a rerun can adopt its earlier product instead of writing "(2)".
            pdf.save(str(rewritten_path),compress_streams=True,recompress_flate=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate,deterministic_id=True)
    except pikepdf.PasswordError:
        raise ValueError('PDF 已加密：请先在原软件中用密码另存为未加密副本')
    except pikepdf.PdfError as error:
        first_line=str(error).splitlines()[0] if str(error) else '未知错误'
        raise ValueError('PDF 结构无法安全重写：'+first_line[:200])
    _verify_lossless_rewrite(source,rewritten_path,r)
    rewritten=rewritten_path.stat().st_size
    target=work/_compress_target(source)
    if rewritten<original:
        os.replace(rewritten_path,target)
        output=rewritten
        note=f'无损重写：{original} 字节 → {rewritten} 字节（省 {original-rewritten} 字节）'
    else:
        # No measurable gain: publish the original bytes so the user gets the
        # file they asked for and the report states the rewrite did not help.
        rewritten_path.unlink(missing_ok=True)
        shutil.copyfile(source,target)
        output=original
        note=f'重写后没有变小（原 {original} 字节 → 重写 {rewritten} 字节），已发布与原件逐字节相同的副本'
    COMPRESS_INFO={'mode':'lossless','source_bytes':original,'output_bytes':output,
                   'rewrite_bytes':rewritten,'pages':len(r.pages),'note':note}

def _page_geometry(r):
    """Original page sizes plus reasons a lossy rebuild cannot reproduce them.

    A rendered-image rebuild can only reproduce a page whose visible area is
    exactly its MediaBox: /Rotate or a smaller CropBox changes what the viewer
    shows, and rebuilding would silently re-lay-out the page. Those files are
    refused for the lossy mode only - the lossless rewrite keeps every box.
    """
    sizes=[];problems=[];units=[]
    for number,page in enumerate(r.pages,1):
        box=page.mediabox
        width,height=float(box.width),float(box.height)
        sizes.append((width,height))
        # /UserUnit scales how many points one printed unit covers (large-format
        # drawings). PDFium's get_size ignores it, so carry it and re-check it.
        try:units.append(float(page.get('/UserUnit',1) or 1))
        except (TypeError,ValueError):units.append(1.0)
        if width<=0 or height<=0:
            problems.append(f'第 {number} 页尺寸无效');continue
        try:rotate=int(page.get('/Rotate',0) or 0)%360
        except (TypeError,ValueError):rotate=0
        if rotate:problems.append(f'第 {number} 页带页面旋转，有损重建会改变版式')
        if '/CropBox' in page:
            crop=page.cropbox
            if any(abs(float(a)-float(b))>0.5 for a,b in ((crop.left,box.left),(crop.bottom,box.bottom),
                                                          (crop.right,box.right),(crop.top,box.top))):
                problems.append(f'第 {number} 页的裁切框与页面尺寸不一致，有损重建会改变版式')
    return sizes,problems,units

def _verify_lossy_product(source,product,sizes,units):
    """Page count, physical size (±1 pt, /UserUnit aware) and re-renderability.

    PDFium's get_size ignores /UserUnit, so a page whose unit was dropped would
    still measure "correctly" there. Physical size is therefore compared with
    pikepdf using MediaBox * /UserUnit, and the unit itself must survive.
    """
    import pikepdf
    import pypdfium2 as pdfium
    try:
        with pikepdf.open(str(product)) as pdf:
            if len(pdf.pages)!=len(sizes):raise ValueError('有损重建后页数不一致，已保留原件')
            for index,(page,(width,height),unit) in enumerate(zip(pdf.pages,sizes,units),1):
                got_unit=float(page.obj.get('/UserUnit',1) or 1)
                box=[float(value) for value in page.obj.MediaBox]
                physical=((box[2]-box[0])*got_unit,(box[3]-box[1])*got_unit)
                expected=(width*unit,height*unit)
                if abs(got_unit-unit)>1e-6 or abs(physical[0]-expected[0])>1.0 or abs(physical[1]-expected[1])>1.0:
                    raise ValueError(f'有损重建后第 {index} 页物理尺寸误差超过 1pt，已保留原件')
    except ValueError:raise
    except Exception as error:raise ValueError(f'有损重建后成品无法重新打开核对尺寸（{error}），已保留原件')
    try:
        with pdfium.PdfDocument(str(source)) as before,pdfium.PdfDocument(str(product)) as after:
            if len(before)!=len(after):raise ValueError('有损重建后页数不一致，已保留原件')
            for index in _sample_pages(len(sizes)):_render_signature(after,index)
    except ValueError:raise
    except Exception as error:raise ValueError(f'有损重建后成品无法被 PDFium 重新渲染（{error}），已保留原件')

def compress_lossy(source,work,cfg,r):
    global COMPRESS_INFO
    import img2pdf
    import pikepdf
    original=source.stat().st_size
    sizes,problems,units=_page_geometry(r)
    if not sizes:raise ValueError('PDF 没有页面')
    if problems:raise ValueError('有损压缩暂不支持该 PDF：'+problems[0]+'。请改用无损档')
    page_dir=work/'pages';page_dir.mkdir()
    # Same per-page progress path as pdf_images, so a long render is observable.
    # Defaults are repeated here so a direct one() call without validate() (the
    # engine test surface) still gets the documented 150 dpi / JPEG 75 resample.
    render_cfg={**cfg,'format':'jpeg','dpi':cfg.get('dpi',150),'quality':cfg.get('quality',75)}
    render(source,page_dir,render_cfg,source.name)
    images=[page_dir/f'{source.name}_page{index+1:05d}.jpeg' for index in range(len(sizes))]
    if any(not image.is_file() for image in images):raise ValueError('有损渲染缺少页面文件，已保留原件')
    # img2pdf builds the whole PDF in memory; bound what one job may allocate.
    buffer_bytes=sum(image.stat().st_size for image in images)
    if buffer_bytes>1_000_000_000:raise ValueError('有损压缩的中间图片超过 1GB 内存预算，请降低 DPI 后重试')
    rebuilt=page_dir/'rebuilt.pdf'
    with rebuilt.open('wb') as stream:
        img2pdf.convert(*[str(image) for image in images],outputstream=stream,rotation=img2pdf.Rotation.ifvalid)
    # Guard against a bad product before it can be published: the no-gain rule
    # is a failure of this item, not a silently missing file.
    if rebuilt.stat().st_size>=original:
        raise ValueError(f'有损压缩后体积未变小（原 {original} 字节 → 重建 {rebuilt.stat().st_size} 字节），'
                         '该文件本身已是压缩图片或页面内容不适合重采样；原件保留，未生成成品')
    target=work/_compress_target(source)
    with pikepdf.open(str(rebuilt)) as pdf:
        if len(pdf.pages)!=len(sizes):raise ValueError('有损重建后页数不一致，已保留原件')
        for page,(width,height),unit in zip(pdf.pages,sizes,units):
            # img2pdf derives the box from pixel/DPI rounding (0.24 pt off at
            # 100 dpi in a measured case); the contract is the original physical
            # size, so set it exactly and let the image stretch by that <0.5 pt.
            page.MediaBox=[0,0,width,height];page.CropBox=[0,0,width,height]
            # Dropping /UserUnit would silently halve a large-format page's
            # printed size; keep the unit on the rebuilt page.
            if unit!=1:page.UserUnit=unit
        pdf.save(str(target),compress_streams=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate,deterministic_id=True)
    output=target.stat().st_size
    if output>=original:
        raise ValueError(f'有损压缩后体积未变小（原 {original} 字节 → 成品 {output} 字节），'
                         '该文件本身已是压缩图片或页面内容不适合重采样；原件保留，未生成成品')
    _verify_lossy_product(source,target,sizes,units)
    COMPRESS_INFO={'mode':'lossy','source_bytes':original,'output_bytes':output,'pages':len(sizes),
                   'dpi':render_cfg['dpi'],'quality':render_cfg['quality'],
                   'note':f'有损重采样：{original} 字节 → {output} 字节（省 {original-output} 字节）；每页文字层已转为图片'}

def compress_pdf(source,work,cfg):
    r=reader(source)
    # An encrypted PDF must never be decrypted silently by a compressor: the user
    # asked for a smaller file, not for the protection to be removed.
    if r.is_encrypted:raise ValueError('PDF 已加密：请先在原软件中用密码另存为未加密副本')
    if not len(r.pages):raise ValueError('PDF 没有页面')
    # Signed PDFs are refused before either mode starts (see _has_signature):
    # publishing a compressed file whose signature no longer verifies would be
    # a silent downgrade of the user's trust artefact.
    _refuse_signed(source)
    if cfg.get('lossy'):compress_lossy(source,work,cfg,r)
    else:compress_lossless(source,work,r)

def clean_names(files, sources, cfg):
    """Human-readable output names (WPS/Adobe style): 讲义/讲义_001.png, 课件.pdf,
    照片.jpg, 讲义_合并.pdf. Returns paths relative to the destination folder."""
    import re
    # Truncate by bytes, not characters: 150 Chinese characters are 450 bytes and
    # ext4 rejects any component over 255 bytes with ENAMETOOLONG.
    op=cfg['operation'];base=platform_names.sanitize(sources[0].stem) or 'output'
    ext=lambda f:'.jpg' if f.suffix.lower() in ('.jpeg','.jpg') else f.suffix.lower()
    if op in ('pdf_images','office_images'):
        numbers=[int(m.group(1)) if (m:=re.search(r'_page(\d+)\.',f.name)) else i+1 for i,f in enumerate(files)]
        width=max(3,len(str(max(numbers))))
        return [Path(base)/f'{base}_{n:0{width}d}{ext(f)}' for n,f in zip(numbers,files)]
    if op in ('pdf_merge','images_pdf'):return [Path(f'{base}_合并.pdf') for f in files]
    if op=='image_compress' and not cfg.get('lossy'):return [Path(sources[0].name) for f in files]
    if op=='pdf_compress':return [Path(base+'_压缩.pdf') for f in files]
    return [Path(base+ext(f)) for f in files]

def unique_target(dest, relative, expected, taken):
    """First free name: 名称.ext, 名称 (2).ext, … A byte-identical existing file is
    adopted (crash between publish and receipt); different content is never touched."""
    folder,name=relative.parent,relative.name
    stem,suffix=Path(name).stem,Path(name).suffix
    for n in range(1,10000):
        candidate=dest/folder/(name if n==1 else f'{stem} ({n}){suffix}')
        if candidate in taken:continue
        if not candidate.exists() and not candidate.is_symlink():return candidate,False
        if candidate.is_file() and not candidate.is_symlink() and sha(candidate)==expected:return candidate,True
    raise ValueError('输出目录中同名文件过多，请选择新的输出目录')

def page_folder(dest, relative, files_dir_taken):
    """A new folder for multi-page output: 讲义, 讲义 (2), … never mixing with older runs."""
    for n in range(1,10000):
        name=relative.parts[0] if n==1 else f'{relative.parts[0]} ({n})'
        if name in files_dir_taken or not (dest/name).exists():return name
    raise ValueError('输出目录中同名文件夹过多，请选择新的输出目录')

def identity(p):
    st=p.stat();return [st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns]

def _defer_termination():
    """Block termination signals across one publish/mkdir bookkeeping window.

    A cancel can be delivered between publish_exclusive()'s rename and the
    published.append() that makes rollback aware of the file, or between mkdir and
    the created-dirs append. The signal handler raises in the main thread, so the
    rollback would miss exactly that entry: a CI run once left one page and its folder
    behind that way. POSIX pthread_sigmask holds SIGTERM/SIGINT until the window
    closes; the pending handler then runs at the next bytecode, after the append.
    Windows cancellation is a job-object kill that cannot be deferred (and cannot
    run Python cleanup either), so this is a no-op there.
    """
    if not hasattr(signal,'pthread_sigmask'):return None
    blocked={signal.SIGTERM}
    for name in ('SIGINT','SIGBREAK'):
        if hasattr(signal,name):blocked.add(getattr(signal,name))
    try:return signal.pthread_sigmask(signal.SIG_BLOCK,blocked)
    except (OSError,ValueError):return None

def _restore_termination(previous):
    if previous is None:return
    try:signal.pthread_sigmask(signal.SIG_SETMASK,previous)
    except (OSError,ValueError):pass

def _termination_pending():
    """True when a cancel signal is blocked and waiting to be delivered.

    _defer_termination() holds SIGTERM/SIGINT for the publish section so a rename
    cannot be interrupted mid-bookkeeping. Without this check the user had to wait
    for the whole publish to finish before the cancel was noticed; polling the
    pending set at a safe point between files lets the rollback start immediately
    and still keep every file/folder accounted for.
    """
    if not hasattr(signal,'sigpending'):return False
    blocked={signal.SIGTERM}
    for name in ('SIGINT','SIGBREAK'):
        if hasattr(signal,name):blocked.add(getattr(signal,name))
    try:return bool(signal.sigpending()&blocked)
    except (OSError,ValueError):return False

@contextlib.contextmanager
def _run_staging(staging_root, fingerprint, record_dir=None):
    """A per-job staging directory that survives a hard worker death as evidence.

    The directory normally removes itself when the job ends, but the cleanup can
    itself be interrupted by the cancel signal (the SIGTERM handler raises in the
    main thread) or skipped entirely when a worker is SIGKILLed. That left the
    "Chamo暂存/<fingerprint>-..." directory in the output folder after a cancel.
    The register file lives in this run's record folder (state folder,
    not the user's output folder) and names the exact path, so the supervisor can
    remove it without guessing and without touching a concurrent run's staging.
    The path is registered before the directory is created: a kill between the two
    writes leaves nothing to clean in the output folder.
    """
    register=None;directory=None
    mask=_defer_termination()
    try:
        try:
            name=fingerprint+'-'+uuid.uuid4().hex[:8]
            directory=Path(staging_root)/name
            if record_dir:
                try:
                    register=Path(record_dir)/('staging-'+fingerprint+'.json')
                    atomic_json(register,{'path':str(directory),'fingerprint':fingerprint,
                                          'pid':os.getpid(),'created_at':time.time()},durable=False)
                except OSError:
                    register=None
            directory.mkdir()
        finally:
            # A pending cancel is raised here, after the directory is registered.
            _restore_termination(mask)
    except BaseException:
        _drop_staging_directory(directory,register)
        raise
    try:
        yield directory
    finally:
        # Defer the cancel handler across the cleanup itself: a SIGTERM that lands
        # inside rmtree used to interrupt it and leave the staging folder behind.
        mask=_defer_termination()
        try:
            _drop_staging_directory(directory,register)
        finally:
            _restore_termination(mask)

def _drop_staging_directory(directory,register):
    if directory is not None:
        try:shutil.rmtree(directory,ignore_errors=True)
        except OSError:pass
    if register is not None:
        try:register.unlink(missing_ok=True)
        except OSError:pass

# Cancel grace periods: the short one is what a cancelled run must respect (Ctrl-C
# has to feel immediate), the long one is reserved for the publish/receipt section
# where a hard kill would leave renamed files that no receipt covers.
CANCEL_GRACE_SHORT=5.0
CANCEL_GRACE_PUBLISH=30.0

def _cancel_grace(entries):
    """Seconds to wait for a cancelled worker before the supervisor kills it.

    The mapping is the whole point of the publish marker: while any of this unit's
    workers is inside its publish/receipt section the supervisor waits the long
    grace period (a SIGKILL there leaves published files behind), and everywhere
    else it waits the short one (a stuck worker must never make Ctrl-C look like a
    hang). tests/test_cli.py pins this mapping and the marker semantics.
    """
    return CANCEL_GRACE_PUBLISH if any(_publish_in_progress(e) for e in entries) else CANCEL_GRACE_SHORT

def _publish_in_progress(entry):
    """True while this job's worker is inside the publish/receipt section.

    The worker writes item-<n>-publish.lock before publishing and removes it once
    the receipt is committed (see one()). Only that window needs the long cancel
    grace period; everywhere else a stuck worker must not delay cancellation.
    A missing or empty path means "no marker", never the current directory.
    """
    marker=entry.get('job',{}).get('critical')
    if not marker:return False
    try:return Path(marker).exists()
    except OSError:return False

def _cleanup_run_staging(out, record):
    """Remove staging directories this run registered but did not clean itself.

    Only paths named by this run's own register files are touched: the register
    folder belongs to one run, so two concurrent conversions to the same output
    folder never delete each other's staging (and the whole staging root is never
    removed blindly). An empty staging root is dropped by platform_state.drop_staging.
    """
    root=platform_state.staging(out)
    for register in list(record.glob('staging-*.json')):
        data={}
        try:data=platform_text.read_json(register)
        except (OSError,ValueError):data={}
        directory=None
        try:
            path=data.get('path')
            fingerprint=str(data.get('fingerprint') or '')
            if path:
                candidate=Path(path)
                if (fingerprint and candidate.name.startswith(fingerprint+'-')
                        and candidate.parent.resolve()==root.resolve()):
                    directory=candidate
        except OSError:
            directory=None
        _drop_staging_directory(directory,register)

def _make_output_dirs(path, out, created):
    """Create a product folder, recording only the folders this run created.

    One mkdir per path component: a successful mkdir proves this run created the
    folder, while FileExistsError (the user's folder, or a concurrent job's)
    leaves it untouched and unrecorded. Rollback can then remove exactly the
    empty folders this run introduced and never a folder the user already had.
    """
    try:relative=path.relative_to(out)
    except ValueError:raise ValueError('输出目录符号链接越界')
    current=out
    for part in relative.parts:
        current=current/part
        # mkdir and "remember that we created it" are one unit: a cancel between
        # them would leave an untracked folder behind on rollback.
        previous=_defer_termination()
        try:
            try:
                os.mkdir(current)
                created.append(current)
            except FileExistsError:
                if not current.is_dir():raise
        finally:_restore_termination(previous)

def _remove_created_dirs(created):
    """Remove folders this run created that are still empty, deepest first.

    A folder that still holds a file (a product of a completed batch item, or a
    file the user put there) is deliberately kept: rollback removes only residue.
    """
    for directory in reversed(created):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass

def _publish_file(source, target, out, created):
    """Publish one file, keeping a batch sibling's rollback from breaking us.

    Jobs from one folder input mirror the same nested destination folder. A
    sibling that fails can roll back and remove that shared folder while this job
    has already created (but not yet published into) it. Recreating the folder
    once makes that race a retry instead of a lost job; a real failure still
    propagates.
    """
    _make_output_dirs(target.parent, out, created)
    try:
        publish_exclusive(source, target)
    except FileNotFoundError:
        _make_output_dirs(target.parent, out, created)
        publish_exclusive(source, target)

def one(job):
    global THREADS,OFFICE_PROFILE,PROGRESS,COMPRESS_INFO
    THREADS=max(1,min(8,int(job.get('threads',1))));OFFICE_PROFILE=job.get('office_profile');PROGRESS=job.get('progress')
    # Test-only cap so a cancel-latency gate can simulate a low-core CI runner on a
    # many-core machine (the page-encode pool size is derived from the CPU count).
    _test_threads=os.environ.get('CHAMO_TEST_ENCODE_THREADS')
    if _test_threads:
        try:THREADS=max(1,min(THREADS,int(_test_threads)))
        except ValueError:pass
    COMPRESS_INFO=None
    PAGE_RESUME.clear()
    sources=[Path(x).resolve(strict=True) for x in job['sources']];cfg=job['config'];out=Path(job['output']).resolve();out.mkdir(parents=True,exist_ok=True)
    identities=[identity(s) for s in sources];hashes=[sha(s) for s in sources]
    fingerprint=hashlib.sha256(json.dumps([str(s) for s in sources]+hashes+[cfg],sort_keys=True).encode()).hexdigest()[:20]
    # Resume receipts live in the user state folder, never in the output folder:
    # after a conversion the output folder must contain only the converted files.
    receipt=platform_state.receipt(out,fingerprint,job.get('state_root'))
    if receipt.exists():
        old=platform_text.read_json(receipt);previous=old.get('outputs') or []
        if previous and all(Path(x['path']).is_file() and sha(x['path'])==x['sha256'] for x in previous):return {**old,'state':'resumed'}
        # Records live outside the output folder, so a user who deletes the
        # converted files no longer deletes the record with them. Nothing is left to
        # protect when every recorded output is gone: convert again instead of
        # refusing forever. A partly changed output keeps the hard stop below.
        if not any(Path(x['path']).exists() for x in previous):
            receipt.unlink(missing_ok=True)
        else:
            raise ValueError('已完成输出被改变，请选择新的输出目录，避免覆盖')
    rel=Path(job.get('relative','')).parent
    if rel.is_absolute() or '..' in rel.parts:raise ValueError('无效相对目录')
    # The folder is created when the first converted file is published, not here:
    # an empty mirror folder for a job that fails (or is skipped) is exactly the
    # clutter "the output folder holds only the converted files" forbids.
    dest=out/rel
    if not dest.resolve().is_relative_to(out):raise ValueError('输出目录符号链接越界')
    outputs=[];published=[];shard_info={};created_dirs=[]
    staging_root=platform_state.staging(out);staging_root.mkdir(exist_ok=True)
    with _run_staging(staging_root,fingerprint,job.get('record_dir')) as tmp:
        work=Path(tmp)
        # Page-level resume cache: pdf_images only, keyed by source content and
        # the render config. Its .active marker is dropped once every page is
        # written, so a sweep in another process can collect an idle page set.
        page_cache=page_cache_open(cfg,hashes[0],job.get('state_root'))
        try:
            files=convert(sources,work,cfg,int(job.get('shard_budget',1) or 1),shard_info,page_cache)
        finally:
            page_cache_close(page_cache)
        if identities != [identity(s) for s in sources] or hashes != [sha(s) for s in sources]:raise ValueError('处理期间原件发生变化，拒绝发布')
        # Requests without 'naming' (older runs being resumed) keep the tagged scheme.
        clean=cfg.get('naming')=='clean' and cfg['operation'] not in ('rag','pdf_split')
        names=clean_names(files,sources,cfg) if clean else None
        if clean and names and len(names[0].parts)>1:
            folder=page_folder(dest,names[0],set())
            names=[Path(folder)/n.name for n in names]
        taken=set()
        # Tell the supervisor this worker is now publishing. It is the one window
        # where SIGKILLing the worker leaves renames behind that no receipt covers,
        # so the supervisor grants a longer grace period only while this marker
        # exists (see Supervisor._execute). The marker is written before the signal
        # mask and removed after the receipt is committed.
        critical=job.get('critical')
        if critical:
            try:Path(critical).write_text(str(os.getpid()),encoding='utf-8')
            except OSError:critical=None
        # Defer SIGTERM/SIGINT for the whole publish section, not just each single
        # rename: the per-file mask left the gaps between files (sha, unique_target,
        # mkdir, row building) unmasked, and a cancel landing in one of them could
        # leave published files behind. The pending signal is
        # released just before the receipt commit so one()'s except BaseException
        # still undoes every file and folder it created; a signal that lands during
        # the receipt write hits the same rollback, which now also removes the
        # just-committed receipt.
        publish_mask=_defer_termination()
        try:
            for i,f in enumerate(files):
                # Safe point between files: a cancel that arrived while signals were
                # blocked is honoured here, with the published-so-far list intact, so
                # Ctrl-C during a long publish still exits in about a second.
                if i and _termination_pending():raise RuntimeError('任务已取消，原件保留，可重新核验并恢复')
                expected=sha(f)
                if clean:
                    target,adopt=unique_target(dest,names[i],expected,taken);taken.add(target)
                    _make_output_dirs(target.parent,out,created_dirs)
                    if not target.parent.resolve().is_relative_to(out):raise ValueError('输出目录符号链接越界')
                    if adopt:f.unlink()
                    else:
                        # rename and "remember the new path" are one unit, see
                        # _defer_termination: a cancel in between left a page behind.
                        previous=_defer_termination()
                        try:_publish_file(f,target,out,created_dirs);published.append(target)
                        finally:_restore_termination(previous)
                else:
                    # Strict PDF adapters may reference sibling part names through
                    # GoToR links. Preserve those names exactly, including suffixes.
                    target=dest/(f.name if cfg['operation'] in ('rag','pdf_split') else platform_names.sanitize(f.stem+'_'+fingerprint[:8])+f.suffix)
                    _make_output_dirs(target.parent,out,created_dirs)
                    if not target.parent.resolve().is_relative_to(out):raise ValueError('输出目录符号链接越界')
                    try:
                        previous=_defer_termination()
                        try:_publish_file(f,target,out,created_dirs);published.append(target)
                        finally:_restore_termination(previous)
                    except FileExistsError:
                        # A crash may happen between publishing a part and committing
                        # its receipt. Adopt only byte-identical regenerated content.
                        if target.is_symlink() or not target.is_file() or sha(target)!=expected:raise
                # publish_exclusive is a rename, so the bytes hashed above are the published bytes.
                row={'path':str(target),'bytes':target.stat().st_size,'sha256':expected}
                if target.suffix.lower()=='.pdf':row['pages']=len(reader(target).pages)
                outputs.append(row)
                _test_slow_publish()
            record={'sources':[str(s) for s in sources],'source_hashes':hashes,'outputs':outputs,'config':cfg,'state':'complete','fingerprint':fingerprint,'verified_at':time.time()}
            # Folders this run created in the output folder. Kept when the job
            # commits; a rollback (or a cancel that lands after the publish) uses
            # only this list, so a pre-existing user folder is never removed.
            if created_dirs:record['created_dirs']=[str(directory) for directory in created_dirs]
            # The files this run actually renamed into the output folder. Withdraw
            # uses only this list: a row in outputs can be a byte-identical file
            # unique_target adopted (a file the user already had), and that one must
            # never be deleted by a cancellation rollback.
            if published:record['published_paths']=[str(target) for target in published]
            # Original/result byte counts for a compression job, so report.json
            # can prove the outcome even when the published file is a copy.
            if COMPRESS_INFO:record['compress']=dict(COMPRESS_INFO)
            # Page-level resume accounting for pdf_images. The fields are absent
            # when the cache is disabled (CHAMO_PAGE_CACHE=0), which keeps that mode
            # byte-for-byte and record-for-record identical to a run without the page cache.
            if cfg['operation']=='pdf_images' and PAGE_RESUME.get('enabled'):
                record['pages_reused']=int(PAGE_RESUME.get('reused',0) or 0)
                record['pages_rendered']=int(PAGE_RESUME.get('rendered',0) or 0)
                if PAGE_RESUME.get('unavailable'):record['cache_unavailable']=int(PAGE_RESUME['unavailable'])
            # Distinguish content verification from merely readable derived exports.
            op=cfg['operation']
            record['fidelity']='verified_content' if op in ('rag','pdf_split') or (op=='image_compress' and not cfg.get('lossy')) else 'rendered_static' if op in ('pdf_images','office_pdf','office_images') else 'derived_export'
            if cfg.get('lossy') or (op=='image_convert' and cfg.get('format')=='jpeg'):record['fidelity']='lossy_user_selected'
            for row in outputs:row['fidelity']=record['fidelity']
            # Why this pdf_images job did or did not shard; printed into report.json
            # and results.jsonl so acceptance can prove the decision instead of
            # inferring it from timing.
            if shard_info:record['shard']=shard_info
            if (work/'audit').exists():
                for meta in (work/'audit').rglob('job.json'):
                    info=platform_text.read_json(meta);info['upload_root']=str(dest);atomic_json(meta,info)
                # Audit evidence for RAG batches is bookkeeping as well: it belongs
                # in the state folder, not next to the user's converted files.
                state_slot=receipt.parent.parent
                audit_target=state_slot/(fingerprint+'-engine')
                if audit_target.exists():
                    # An earlier interrupted receipt commit may leave audit evidence.
                    audit_target=state_slot/(fingerprint+'-engine-'+uuid.uuid4().hex[:8])
                shutil.copytree(work/'audit',audit_target)
                from cloud_kb_pack.output import verify_job
                for meta in audit_target.rglob('job.json'):verify_job(meta.parent)
                record['audit']=str(audit_target)
            # Release the deferred cancel before committing: a pending signal now
            # raises inside this try and the rollback below withdraws everything
            # instead of leaving a half-published folder behind.
            _restore_termination(publish_mask);publish_mask=None
            atomic_json(receipt,record)
            if critical:
                try:Path(critical).unlink(missing_ok=True)
                except OSError:pass
                critical=None
        except BaseException:
            # Roll back with the mask still in place: restoring it first would let a
            # pending cancel handler raise in the middle of the cleanup and leave
            # exactly the residue this block exists to remove.
            rollback_mask=_defer_termination()
            if critical:
                try:Path(critical).unlink(missing_ok=True)
                except OSError:pass
                critical=None
            for target in published:target.unlink(missing_ok=True)
            # Empty folders created by a publish that was rolled back are residue
            # too: a rerun must find the same folder name, not "名称 (2)".
            _remove_created_dirs(created_dirs)
            # A cancel that lands during the receipt commit can already have
            # renamed the receipt into place; a rollback must not leave a receipt
            # that claims outputs which were just withdrawn.
            try:receipt.unlink(missing_ok=True)
            except OSError:pass
            _restore_termination(rollback_mask)
            raise
    return record

# Files found inside a folder that the chosen operation cannot read are skipped
# (and counted) instead of being queued only to fail. RAG accepts every type.
ACCEPTS = {'pdf_images':{'.pdf'},'pdf_split':{'.pdf'},'pdf_compress':{'.pdf'},'pdf_merge':{'.pdf'},'office_pdf':OFFICE,'office_images':OFFICE,
           'images_pdf':IMAGES,'image_convert':IMAGES,'image_compress':IMAGES}

def scan(inputs,out,operation,stats=None):
    result=[];seen=set();excluded=0;skipped=0;accepts=ACCEPTS.get(operation)
    for name in inputs:
        p=Path(name).absolute()
        if p.is_symlink():continue
        if p.is_dir():
            if p.resolve()==out or p.resolve().is_relative_to(out):raise ValueError('输出目录不能等于或包含输入目录')
            entries=[]
            for root,dirs,files in os.walk(p):
                dirs[:]=[d for d in sorted(dirs) if not d.startswith('.') and not (Path(root)/d).is_symlink() and not (Path(root)/d).resolve().is_relative_to(out)]
                # No folder is mirrored here: a folder holding nothing this operation
                # can convert used to leave an empty folder behind in the output tree.
                for f in sorted(files):entries.append(Path(root)/f)
            base=p.parent
        else:entries=[p];base=p.parent
        folder=p.is_dir()
        for x in entries:
            if x.name.startswith('.') or x.is_symlink() or not x.is_file():continue
            if x.resolve().is_relative_to(out):raise ValueError('输入不能位于输出目录内')
            if x.resolve() in seen:continue
            seen.add(x.resolve())
            if x.suffix.lower() in MEDIA:excluded+=1;continue
            if folder and accepts is not None and x.suffix.lower() not in accepts:skipped+=1;continue
            # Always POSIX separators: this relative path travels in JSON and is
            # used as a key by the GUI and by tests, so it must not differ between
            # platforms (Windows would otherwise produce 资料\a.txt).
            result.append({'sources':[str(x.resolve())],'relative':x.relative_to(base).as_posix()})
    if operation in ['pdf_merge','images_pdf'] and result:
        result=[{'sources':[x['sources'][0] for x in result],'relative':Path(result[0]['sources'][0]).name}]
    if stats is not None:stats['skipped']=skipped
    return result,excluded

def auto_workers():
    """Automatic worker count.

    Must match Workbench.autoWorkers in Sources/Model.swift, or "automatic"
    means one number in the GUI and another in the CLI for the same request.
    Leave two cores for the system and the UI, at most six, at least two.
    """
    return min(6,max(2,(os.cpu_count() or 4)-2))

def validate(cfg):
    if cfg.get('operation') not in OPS:raise ValueError('未知转换类型')
    # workers=0 means "choose automatically". The GUI resolves it in Swift, but a
    # CLI or another front end sends the 0 straight through, so the engine must
    # understand it too or the two front ends disagree about the same request.
    if cfg.get('workers')==0:cfg['workers']=auto_workers()
    # pdf_compress owns its defaults: lossless (the default) has no DPI/quality,
    # and the lossy resample is defined at 150 dpi / JPEG 75 instead of the image
    # defaults (the CLI refuses those flags unless --lossy).
    # image_compress: a lossy request without a format is JPEG (the CLI and the
    # browser page both default to it). quality is an encoder knob of the two
    # lossy image formats this project exposes: JPEG and WebP (it is passed
    # to Pillow's WebP encoder, and the native "极致压缩" preset is
    # WebP 70). PNG has no quality knob, so a PNG request carrying quality would
    # silently ignore it and is refused instead. max_edge keeps working for every
    # format; it is a resize, not an encoder knob.
    if cfg.get('operation')=='image_compress':
        if cfg.get('lossy'):
            if 'format' not in cfg:cfg['format']='jpeg'
            if 'quality' in cfg and str(cfg.get('format','jpeg')).lower() not in ('jpeg','webp'):
                raise protocol.ProtocolError('invalid_request','有损图片压缩的 quality 仅对 JPEG / WebP 生效：请改用 format=jpeg 或 format=webp，或改用无损优化')
        elif int(cfg.get('max_edge',0) or 0)>0:
            raise protocol.ProtocolError('invalid_request','无损图片优化不接受缩放（max_edge）；请开启有损压缩（--lossy）')
    if cfg.get('operation')=='pdf_compress':
        cfg.setdefault('lossy',False)
        if cfg.get('lossy'):
            cfg.setdefault('dpi',150);cfg.setdefault('quality',75)
    for k,lo,hi in [('dpi',36,600),('quality',1,100),('pdf_pages',1,10000),('pdf_bytes',1000,10**12),('workers',1,8),('max_edge',0,16000),('batch_files',1,1000)]:
        value=cfg.get(k,{'dpi':144,'quality':90,'pdf_pages':100,'pdf_bytes':100000000,'workers':2,'max_edge':0,'batch_files':50}[k])
        if not isinstance(value,int) or not lo<=value<=hi:raise ValueError(f'{k} 参数超出范围')
        cfg[k]=value
    if cfg.get('paper','original') not in ('original','a4','a3','letter'):raise ValueError('无效纸张类型')
    if not isinstance(cfg.get('margin_mm',10),int) or not 0<=cfg.get('margin_mm',10)<=50:raise ValueError('边距应为 0–50 mm')
    for key in ('image_bytes','document_bytes','text_bytes','table_bytes','table_rows'):
        if key in cfg and (not isinstance(cfg[key],int) or cfg[key]<=0):raise ValueError(key+' 必须大于零')
    if cfg.get('format','png') not in ['png','jpeg','tiff','bmp','webp']:raise ValueError('不支持的输出格式')
    if cfg.get('naming','tagged') not in ('clean','tagged'):raise ValueError('无效命名方式')
    if cfg.get('operation')=='pdf_compress':
        # The documented lossy range is 72-600 (the render path itself allows
        # 36-600 for page export); the whole document is always processed.
        if cfg.get('lossy') and not COMPRESS_DPI[0]<=cfg['dpi']<=COMPRESS_DPI[1]:raise ValueError('dpi 参数超出范围')
        if cfg.get('range'):raise ValueError('页范围参数超出范围：PDF 压缩必须处理全部页面')
    if cfg.get('shard','auto') not in ('auto','off'):raise ValueError('无效分片模式')
    if cfg.get('range') and cfg['operation'] in ['pdf_split','rag']:raise ValueError('严格拆分必须保留全部页面；页范围仅用于图片/Office 导出')

SMALL_IMAGE_BYTES=8_000_000

def plan_units(jobs,workers):
    """Groups small image jobs into chunks so one worker process handles several files
    (process start-up dominated large batches); everything else runs one file per process.
    Chunks stay small (≤16) and plentiful (~3 per worker) to keep the load balanced."""
    small=[];units=[]
    for i,j in enumerate(jobs):
        if j['config']['operation'] in ('image_convert','image_compress') and Path(j['sources'][0]).stat().st_size<SMALL_IMAGE_BYTES:small.append((i,j))
        else:units.append([(i,j)])
    if small:
        size=max(1,min(16,-(-len(small)//(workers*3))))
        units+= [small[k:k+size] for k in range(0,len(small),size)]
    return sorted(units,key=lambda u:u[0][0])

def is_our_manifest(path):
    """True when this file is a manifest this engine wrote, not the user's own file."""
    try:data=platform_text.read_json(path)
    except (OSError,ValueError):return False
    return isinstance(data,dict) and {'生成时间','明细','任务数'} <= set(data)

def manifest_target(out):
    """Where this batch's manifest goes, without ever taking a name the user owns.

    Our own earlier manifest is rotated in place: the folder keeps one current
    record, and the older batch's converted files stay where they are. If the user's
    own "清单.json" is sitting there, our record lives in "清单 (2).json" and later
    batches keep updating that same file instead of climbing to "清单 (3).json".
    """
    plain=out/platform_state.MANIFEST_NAME
    if is_our_manifest(plain) or not (plain.exists() or plain.is_symlink()):return plain
    for n in range(2,10000):
        candidate=out/f'清单 ({n}).json'
        if is_our_manifest(candidate):return candidate
        if not (candidate.exists() or candidate.is_symlink()):return candidate
    raise ValueError('输出目录中清单文件过多，请选择新的输出目录')

def write_manifest(out,results,operation):
    """The one extra file a BATCH conversion may leave in the output folder.

    A folder of files converted together is only useful if the user can see what
    each source produced, so that case writes one manifest next to the outputs (see
    manifest_target for the name). A single-file conversion writes nothing extra:
    the outputs are the answer.
    """
    out=Path(out)
    items=[]
    for r in results:
        # Relative paths always use "/" so a manifest means the same thing when the
        # folder is read on another platform: os.sep would write "\" on Windows and
        # break tooling (and the user's expectations) that reads the same file on macOS.
        entry={'源文件':[str(x) for x in r.get('sources') or []],
               '状态':r.get('state'),
               '成品':[Path(os.path.relpath(x['path'],out)).as_posix() for x in r.get('outputs') or []]}
        if r.get('error'):entry['错误']=r['error']
        items.append(entry)
    data={'生成时间':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
          '操作':operation,
          '任务数':len(results),
          '成功':sum(1 for r in results if r.get('state') in ('complete','resumed')),
          '异常':sum(1 for r in results if r.get('state')=='failed'),
          '成品数':sum(len(r.get('outputs') or []) for r in results),
          '明细':items}
    payload=json.dumps(data,ensure_ascii=False,indent=2)
    target=manifest_target(out)
    atomic_text(target,payload)
    return target

def _committed_record(job, out, request=None, newer_than=None):
    """This job's resume receipt, if it managed to commit one before dying.

    A cancel can arrive after one() published every page and committed its receipt
    but before the worker wrote its per-item result file. The supervisor then sees
    no result and would report a bare "cancelled" while the products stay on disk.
    The receipt is the worker's durable evidence, so the cancellation rollback can
    still withdraw exactly those files. Matching on sources + config keeps this
    from touching any other job's receipt in the same output folder, and
    newer_than (the run start time) keeps a previous completed run's receipt from
    being mistaken for this run's commit.
    """
    try:
        folder=platform_state.slot(Path(out),request)/'receipts'
        paths=sorted(folder.glob('*.json'),key=lambda path:path.stat().st_mtime,reverse=True)
    except OSError:
        return None
    for path in paths:
        try:
            if newer_than is not None and path.stat().st_mtime<newer_than:continue
            record=platform_text.read_json(path)
        except (OSError,ValueError):continue
        if not isinstance(record,dict):continue
        if record.get('sources')==job.get('sources') and record.get('config')==job.get('config'):
            return record
    return None

def _withdraw_product(result, out, request=None):
    """Undo a single-file publish whose run was cancelled before it finished.

    Cancellation is delivered asynchronously: a progress event can lag the
    renderer, so a cancel that the user pressed mid-run can arrive just after a
    fast single-file job published. A cancelled single-file conversion must not
    leave a product behind. The worker recorded the folders it created in the
    output folder, so withdrawing the outputs and only those folders (when they
    are now empty) restores the folder to exactly what it was - a folder the user
    already had, even an empty one, is never removed.

    The resume receipt is dropped too: it would otherwise make a rerun either
    resume from a product that no longer exists or refuse with "completed output
    changed". A batch keeps the files finished before the cancel, which is the
    documented behaviour for "已完成" items; only a one-file conversion is
    all-or-nothing.
    """
    # Withdraw only the files this job renamed into place itself. A row in
    # outputs can be a byte-identical file unique_target adopted, i.e. a file the
    # user already had; deleting it would destroy something this run did not
    # create. Each path is also checked to resolve under the output folder and to
    # still hold the published bytes, so a cancellation can never reach outside
    # the output folder or remove a file somebody changed after publication.
    expected={}
    for row in result.get('outputs') or []:
        try:expected[str(Path(row.get('path','')))]=row.get('sha256')
        except (TypeError,ValueError):pass
    root=Path(out).resolve()
    for item in result.get('published_paths') or []:
        try:
            path=Path(item)
            resolved=path.resolve()
            if not resolved.is_relative_to(root):continue
            if not resolved.is_file():continue
            digest=expected.get(str(path))
            if digest and sha(resolved)!=digest:continue
            path.unlink()
        except OSError:continue
    _remove_created_dirs([Path(item) for item in result.get('created_dirs') or [] if item])
    result.pop('created_dirs',None);result.pop('published_paths',None)
    fingerprint=result.get('fingerprint')
    if fingerprint:
        try:platform_state.receipt(out,fingerprint,request).unlink(missing_ok=True)
        except OSError:pass
    result['outputs']=[]
    result['state']='cancelled'
    result['error']='任务已取消；本次已发布的成品已撤回，可重新运行'

class Supervisor:
    def __init__(self,req):
        self.req=req;self.lock=threading.RLock();self.children={};self.paused=False;self.cancelled=False;self.low=False
        self.results=[];self.done=0;self.started=time.monotonic();self.started_wall=time.time();self.last_progress=0
    def stop(self):
        with self.lock:
            self.cancelled=True;self.paused=False
            for proc in list(self.children.values()):
                platform_process.terminate_tree(proc)
    def control(self):
        for line in sys.stdin:
            try:cmd=json.loads(line).get('command')
            except:continue
            with self.lock:
                if cmd=='resume' and shutil.disk_usage(self.req['output']).free<2_000_000_000:
                    event('control',state='paused',detail='空间仍不足 2GB，请腾出空间后继续');continue
                if cmd=='pause':self.paused=True
                elif cmd=='resume':self.paused=False;self.low=False
                elif cmd=='cancel':self.cancelled=True;self.paused=False
                action={'pause':platform_process.pause_tree,
                        'resume':platform_process.resume_tree,
                        'cancel':platform_process.terminate_tree}.get(cmd)
                if action:
                    for proc in list(self.children.values()):
                        action(proc)
                event('control',state='cancelled' if self.cancelled else 'paused' if self.paused else 'running')
        # A closed GUI control pipe means its owner crashed or quit; do not leave
        # detached converters writing indefinitely. CLI EOF remains valid.
        if self.req.get("gui_session") and not self.cancelled:self.stop()
    def guard(self,out):
        while not self.cancelled:
            time.sleep(1)
            try:free=shutil.disk_usage(out).free
            except OSError:free=0
            if free<2_000_000_000:
                with self.lock:
                    if not self.paused:
                        self.paused=True;self.low=True
                        for p in self.children.values():
                            platform_process.pause_tree(p)
                        event('control',state='paused',detail='磁盘可用空间不足 2GB，已暂停；腾出空间后继续')
    def execute(self,unit,record):
        """Runs one unit — a single file, or a chunk of small images — in one worker process."""
        while self.paused and not self.cancelled:time.sleep(.2)
        if self.cancelled:return
        slot=self.slots.get()
        try:
            extra={'threads':self.threads,'office_profile':str(self.profiles/f'slot-{slot}')}
            entries=[{'index':i,'job':{**job,**extra,'progress':str(record/f'item-{i}.progress') if len(unit)==1 else None,
                                      'critical':str(record/f'item-{i}-publish.lock')},'result':str(record/f'item-{i}-result.json')} for i,job in unit]
            return self._execute(entries,record)
        finally:self.slots.put(slot)
    def _execute(self,entries,record):
        first=entries[0]['index'];single=len(entries)==1
        for e in entries:event('item',index=e['index'],source=e['job']['sources'][0],state='running')
        chunk=record/f'chunk-{first}.json';progress=record/f'chunk-{first}.progress'
        atomic_json(chunk,{'entries':entries,'progress':str(progress)},durable=False)
        log=record/f'item-{first}.log'
        with self.lock:
            if self.cancelled:return
            with platform_text.open_text(log,'w') as err:
                proc=platform_process.spawn_group([sys.executable,'-I','-B',str(Path(__file__).resolve()),'--chunk',str(chunk)],stdout=err,stderr=err)
            self.children[first]=proc
            if self.paused:platform_process.pause_tree(proc)
        # Timeout counts only unpaused time without progress, so a long but
        # advancing export (thousands of pages) is never killed as "stuck".
        stalled=0;last=time.monotonic();cancel_time=None;seen=None;seen_chunk=None;reported=set()
        page_progress=Path(entries[0]['job']['progress']) if single else None
        def collect():
            # Report each finished file as soon as its result appears, not when the chunk ends.
            for e in entries:
                if e['index'] in reported:continue
                path=Path(e['result'])
                if not path.exists():continue
                try:result=platform_text.read_json(path)
                except ValueError:continue
                reported.add(e['index']);self.finish(e,result,record)
        while proc.poll() is None:
            # wait() returns as soon as the worker exits; a fixed sleep here added
            # up to 0.2 s per file and dominated large batches of small files.
            try:proc.wait(timeout=.2)
            except subprocess.TimeoutExpired:pass
            now=time.monotonic()
            if not self.paused:stalled+=now-last
            last=now
            if page_progress:
                try:state=platform_text.read_text(page_progress)
                except OSError:state=None
                if state and state!=seen:
                    seen=state;stalled=0
                    try:p=json.loads(state);event('progress',index=first,done=p['done'],total=p['total'])
                    except ValueError:pass
            try:state=platform_text.read_text(progress)
            except OSError:state=None
            if state and state!=seen_chunk:seen_chunk=state;stalled=0
            if not single:collect()
            if self.cancelled and cancel_time is None:cancel_time=now
            # Cancellation must feel immediate: a worker that is rendering stops on
            # its own within a second or two (the cancel handler raises, and render()
            # drops the encodes that have not started). The one window where a hard
            # kill would leave published files behind is the publish/receipt section,
            # which the worker marks in item-<n>-publish.lock. Only that marker buys
            # the long grace period; everywhere else the 5 s cap applies, so a stuck
            # worker can never make Ctrl-C look like a hang.
            grace=_cancel_grace(entries)
            if stalled>1200 or (cancel_time is not None and now-cancel_time>grace):
                platform_process.kill_tree(proc)
                proc.wait();break
        # A worker that died abnormally (crash, SIGKILL, OOM) never got to stop the
        # shard renderers it started. They are still in its process group / job, so
        # clean them up here instead of leaving them writing into the staging area.
        platform_process.reap_leftovers(proc)
        with self.lock:self.children.pop(first,None)
        collect()
        for e in entries:
            if e['index'] not in reported:
                if self.cancelled:
                    # The worker may have committed the job (files + receipt) and
                    # died before handing its result back. A cancelled run must
                    # leave nothing, so withdraw the committed product using the
                    # receipt's own published-path list.
                    try:
                        committed=_committed_record(e['job'],Path(self.req['output']).resolve(),self.req,newer_than=self.started_wall)
                        if committed:_withdraw_product(committed,Path(self.req['output']).resolve(),self.req)
                    except Exception:
                        pass
                self.finish(e,{'state':'cancelled' if self.cancelled else 'failed','error':'任务取消、超时或工作进程退出；原件保留','sources':e['job']['sources'],'outputs':[]},record)
    def finish(self,entry,result,record):
        index=entry['index'];source=entry['job']['sources'][0]
        with self.lock:
            self.results.append(result);self.done+=1
            shown={'index':index,'source':source,'state':result['state'],'detail':result.get('error',''),'done':self.done,'total':self.total,'outputs':len(result.get('outputs',[])),'output_bytes':sum(x.get('bytes',0) for x in result.get('outputs',[])),'output_paths':[x['path'] for x in result.get('outputs',[])]}
            # The shard decision, when this operation makes one; an added field is
            # backward compatible with the versioned protocol.
            if result.get('shard'):shown['shard']=result['shard']
            event('item',**shown)
            # Append each receipt once; rewriting the entire result set per file
            # makes large batches quadratic in disk I/O. The live snapshot is bounded.
            with platform_text.open_text(record/'results.jsonl','a') as journal:journal.write(json.dumps(result,ensure_ascii=False)+'\n')
            if time.monotonic()-self.last_progress>=0.5 or self.done==self.total:
                atomic_json(record/'progress.json',{'done':self.done,'total':self.total,'updated_at':time.time()},durable=False)
                self.last_progress=time.monotonic()
    def run(self):
        req=self.req
        # Validate the envelope before touching the filesystem: an unknown field
        # or an unsupported protocol version must fail loudly, not be ignored.
        protocol.check_request(req)
        cfg=req['config'];validate(cfg);out=Path(req['output']).expanduser().resolve()
        # Two runs may start for the same output folder at the same time: one
        # creates it, the other must adopt it instead of failing on the mkdir race.
        try:out.mkdir(parents=True,exist_ok=True)
        except FileExistsError:raise ValueError('输出路径已存在且不是文件夹')
        if shutil.disk_usage(out).free<2_000_000_000:raise ValueError('输出盘不足 2GB，请先腾出空间')
        # Reclaim staging folders from runs that died without cleaning up; see
        # sweep_staging for why only clearly stale folders are touched.
        swept=sweep_staging(platform_state.staging(out))
        # Page-level resume cache: one sweep per run, before any worker starts.
        # Every page set a running export writes is protected by its .active
        # marker, so this sweep cannot collect pages being produced right now.
        # A sweep failure is cosmetic and must never fail the conversion.
        cache_swept={}
        if cfg['operation']=='pdf_images':
            try:cache_swept=platform_state.sweep_page_cache(req)
            except Exception:cache_swept={}
        stats={};jobs,excluded=scan(req['inputs'],out,cfg['operation'],stats);self.total=len(jobs);skipped=stats['skipped']
        if not jobs:raise ValueError('没有可处理文件：音视频默认排除'+(f'，文件夹中有 {skipped} 个文件不适用当前操作' if skipped else ''))
        # Internal run bookkeeping lives in the state folder (see platform_state):
        # the output folder must end up with the converted files and nothing else.
        record=platform_state.new_record(out,req)
        atomic_json(record/'request.json',req)
        event('scan',total=len(jobs),excluded=excluded,skipped=skipped,report=str(record),sources=[j['sources'][0] for j in jobs],sizes=[Path(j['sources'][0]).stat().st_size for j in jobs],staging_reclaimed=swept,
              **({'page_cache_reclaimed':cache_swept} if cache_swept else {}))
        threading.Thread(target=self.control,daemon=True).start();threading.Thread(target=self.guard,args=(out,),daemon=True).start()
        # LibreOffice instances are memory-heavy; cap them lower than image workers.
        workers=min(cfg['workers'],3) if cfg['operation'].startswith('office') else cfg['workers']
        workers=max(1,min(workers,len(jobs)))
        # Spare worker slots a sharded job may claim; pdf_images only.
        shard_plan=shard_budgets(cfg['operation'],cfg['workers'],len(jobs))
        self.slots=queue.Queue()
        for n in range(workers):self.slots.put(n)
        # Spare cores go to page encoding when there are fewer jobs than cores.
        self.threads=max(1,min(8,(os.cpu_count() or 4)//workers))
        self.profiles=Path(req.get('office_profile_root') or platform_state.office_profiles(req))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                units=[{**j,'config':cfg,'output':str(out),'shard_budget':shard_plan[index],
                        'state_root':req.get('state_root'),'record_dir':str(record)}
                       for index,j in enumerate(jobs)]
                futures=[pool.submit(self.execute,unit,record) for unit in plan_units(units,workers)]
                for future in futures:future.result()
            # Snapshot the cancel flag once: withdrawal, summary, manifest and the
            # finished event all use this one value. A cancel that arrives after the
            # snapshot loses to the completed run, so the report and the disk stay
            # consistent instead of "cancelled" with complete products left behind.
            #
            # Test-only hook (default off): the deterministic late-cancel/withdraw
            # gate cannot win this millisecond race under load, so
            # CHAMO_TEST_LATE_CANCEL=1 simulates a cancel arriving exactly here, in
            # the finalize window. Read only at this point; see the verifier's
            # tests/test_page_resume.py late-cancel/withdraw gates.
            if os.environ.get('CHAMO_TEST_LATE_CANCEL')=='1' and not self.cancelled:
                self.cancelled=True
            cancelled_now=self.cancelled
            if cancelled_now and len(units)==1 and not any(r.get('state')=='resumed' for r in self.results):
                # Defense in depth for the publish window: withdraw whatever this
                # single-file job actually committed, whatever state its result
                # recorded. A cancel can arrive after one() committed the receipt
                # but before the worker handed its result back; the chunk then
                # records that job as failed, and the old "state == complete"
                # condition left every published page and its folder behind
                # The receipt is the worker's durable evidence
                # of the published paths, so withdraw by receipt first, then by
                # result (idempotent, limited to this run's own outputs). A resumed
                # result means the files pre-date this run: never withdraw those.
                try:
                    committed=_committed_record(units[0],out,self.req,newer_than=self.started_wall)
                    if committed:_withdraw_product(committed,out,self.req)
                except Exception:
                    pass
                for result in self.results:
                    if result.get('state')=='complete':_withdraw_product(result,out,self.req)
                try:
                    with platform_text.open_text(record/'results.jsonl','w') as journal:
                        for item in self.results:journal.write(json.dumps(item,ensure_ascii=False)+'\n')
                except OSError:pass
            outputs=[x for r in self.results if r['state'] in ('complete','resumed') for x in r['outputs']]
            with platform_text.open_text(record/'upload-manifest.jsonl','w') as f:
                for x in outputs:f.write(json.dumps(x,ensure_ascii=False)+'\n')
            for i in range(0,len(outputs),cfg['batch_files']):atomic_json(record/f'batch-{i//cfg["batch_files"]+1:04d}.json',outputs[i:i+cfg['batch_files']])
            summary={'state':'cancelled' if cancelled_now else 'complete_with_exceptions' if any(r['state']=='failed' for r in self.results) else 'complete','success':sum(r['state'] in ('complete','resumed') for r in self.results),'failed':sum(r['state']=='failed' for r in self.results),'total':self.total,'outputs':len(outputs),'seconds':round(time.monotonic()-self.started,2),'report':str(record),'results':self.results}
            atomic_json(record/'report.json',summary)
            lines=['Chamo 处理报告',f'成功 {summary["success"]} / 异常 {summary["failed"]} / 输入任务 {self.total} / 成品 {len(outputs)}',f'排除音视频 {excluded}；跳过不适用文件 {skipped}；用时 {summary["seconds"]} 秒',f'状态：{summary["state"]}','原件保留在原位置。upload-manifest.jsonl 为成功输出清单；batch-*.json 为上传批次索引。','保真标记：verified_content 内容校验；rendered_static 静态渲染；derived_export 派生导出；lossy_user_selected 主动选择质量损失。','']
            for r in self.results:
                if r['state'] not in ('complete','resumed'):lines.extend(['异常：'+ '；'.join(r['sources']),r.get('error','未完成'),''])
                elif r.get('compress'):
                    info=r['compress']
                    lines.extend(['压缩：'+'；'.join(r['sources']),
                                  f"  {info.get('mode','')}：{info.get('source_bytes')} 字节 → {info.get('output_bytes')} 字节；{info.get('note','')}"])
                elif r.get('pages_reused'):
                    lines.extend(['页级续跑：'+'；'.join(r['sources']),
                                  f"  复用 {r['pages_reused']} 页 / 重渲染 {r.get('pages_rendered',0)} 页"+
                                  (f"；缓存不可用 {r['cache_unavailable']} 次" if r.get('cache_unavailable') else '')])
            (record/'处理报告.txt').write_text('\n'.join(lines),encoding='utf-8')
            # A batch gets exactly one manifest in the output folder; a single-file
            # conversion gets none. See write_manifest. A cancelled batch gets none
            # either: it lists only the tasks that started, so 成功+异常 would not add
            # up to 任务数 and the file would describe a run that never finished.
            manifest=write_manifest(out,self.results,cfg['operation']) if len(self.results)>1 and not cancelled_now else None
            event('finished',**{k:v for k,v in summary.items() if k!='results'},
                  **({'manifest':str(manifest)} if manifest else {}))
            self.cancelled=True
            return summary
        finally:
            # A worker can die (or its cancel handler can interrupt the cleanup)
            # between "staging directory created" and "staging directory removed".
            # Remove only the directories this run registered - never a concurrent
            # run's staging and never the whole staging root - then drop the root if
            # it is now empty.
            _cleanup_run_staging(out,record)
            # Bounded bookkeeping, and the dot-staging folder only exists while a
            # run is in flight. Both live outside the user's outputs by design.
            platform_state.prune(out,req)
            platform_state.drop_staging(out)

def main():
    p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--one');p.add_argument('--result');p.add_argument('--chunk');p.add_argument('--shard');a=p.parse_args()
    if a.shard:
        # One page-range shard of a pdf_images job, started by the job's own
        # worker process (--shard <spec>). It only renders pages into the job's
        # staging directory and reports a machine-readable result; the parent
        # verifies the union and publishes exactly once. It never writes to the
        # destination folder and never names or folds output.
        global THREADS,PROGRESS
        spec_path=Path(a.shard)
        started=time.monotonic()
        result={'state':'failed','files':[],'error':'分片渲染没有开始','seconds':0.0}
        # Report through the path the parent expects; if the task order itself cannot
        # be read there is nothing to read the path from, so fall back to a name the
        # parent can derive. A shard that exits without any result is indistinguishable
        # from a crash and fails the whole job, so never leave without writing one.
        result_path=spec_path.with_name(spec_path.stem+'.result.json')
        def shard_interrupted():
            raise RuntimeError('任务已取消，原件保留，可重新核验并恢复')
        platform_process.on_terminate(shard_interrupted)
        try:
            # The parent has just written this file. On Windows an antivirus scan can
            # hold a new file briefly, so a single failed read must not kill the shard.
            spec=_read_child_result(spec_path)
            if spec is None:raise ValueError(f'读不到分片任务单 {spec_path.name}')
            result_path=Path(spec['result'])
            THREADS=max(1,min(8,int(spec.get('threads',1))));PROGRESS=spec.get('progress')
            source=Path(spec['source']);work=Path(spec['work']);work.mkdir(parents=True,exist_ok=True)
            selected=list(spec['selected'])
            # The parent already opened the cache set and passes its folder here;
            # this child writes only its own pages and reports its counters back.
            cache_stats=render(source,work,spec['config'],spec['stem'],selected,
                               cache=page_cache_adopt(spec.get('cache')))
            result={'state':'ok','files':_expected_page_files(spec['stem'],selected,spec['config']),
                    'cache':cache_stats,'seconds':round(time.monotonic()-started,3)}
        except Exception as error:
            result={'state':'failed','files':[],'error':f'{type(error).__name__}: {error}',
                    'seconds':round(time.monotonic()-started,3)}
        atomic_json(result_path,result,durable=False)
        return
    if a.chunk:
        # Several files in one process; each still gets its own result file, and a
        # failure in one never affects the others. Cancellation stops the chunk.
        cancelled=[]
        def stop():
            cancelled.append(True)
            raise RuntimeError('任务已取消，原件保留，可重新核验并恢复')
        platform_process.on_terminate(stop)
        chunk=platform_text.read_json(Path(a.chunk));total=len(chunk['entries'])
        for k,entry in enumerate(chunk['entries']):
            if cancelled:break
            job=entry['job']
            try:r=one(job)
            except Exception as e:r={'state':'failed','error':str(e),'sources':job['sources'],'outputs':[]}
            atomic_json(entry['result'],r,durable=False)
            tmp=Path(chunk['progress']+'.tmp');platform_text.write_text(tmp,json.dumps({'done':k+1,'total':total}));os.replace(tmp,chunk['progress'])
        return
    if a.one:
        def interrupted():
            raise RuntimeError('任务已取消，原件保留，可重新核验并恢复')
        platform_process.on_terminate(interrupted)
        job=platform_text.read_json(Path(a.one))
        try:r=one(job)
        except Exception as e:r={'state':'failed','error':str(e),'sources':job['sources'],'outputs':[]}
        atomic_json(a.result,r,durable=False);return
    try:
        supervisor=Supervisor(platform_text.read_json(Path(a.request)))
        platform_process.on_terminate(supervisor.stop)
        supervisor.run()
    except Exception as e:
        # A protocol error carries a stable code; anything else is unexpected and
        # must still name the failing call, because a bare "fatal" cannot be
        # diagnosed on a platform that was never run before.
        if isinstance(e, protocol.ProtocolError):
            event('fatal',**e.as_event())
        else:
            # Classify so callers can branch on a stable code instead of
            # parsing a Chinese message that may be reworded.
            event('fatal',code=protocol.classify(e),detail=f'{type(e).__name__}: {e}')
        sys.exit(1)
if __name__=='__main__':main()
