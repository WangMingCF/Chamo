"""Reversible OCR-tree merge with manifest and local MinerU-state migration.

Existing source trees are archived intact, including AppleDouble sidecars. Only
new copies are removed on rollback; existing destination files are never replaced.
Different-content collisions preserve the complete OCR business tree as a version,
so relative Markdown assets and cross-file navigation keep their original context.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import zipfile

from .layout import remap, load
from .output import digest, copy_digest, dump


def files_under(base):
    stack = [base]
    while stack:
        parent = stack.pop()
        with os.scandir(parent) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.name.startswith('.'):
                    continue
                if entry.is_symlink():
                    raise ValueError('归并范围内存在符号链接：' + entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.merge-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_plan(root, workers):
    source = root / 'OCR'
    if not source.is_dir():
        raise ValueError('没有待归并的 OCR 目录')
    paths = list(files_under(source))
    def inspect(path):
        stat = path.stat()
        sha = digest(path)
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('源文件在盘点期间变化：' + str(path))
        return {'old': path.relative_to(root).as_posix(), 'sha256': sha,
                'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    print(f'盘点 {len(paths)} 个 OCR 成品，计算 SHA-256', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(inspect, paths))
    # Cache directory listings; thousands of failed path stats are particularly
    # expensive on external exFAT volumes with deep course trees.
    directory_cache = {}
    def names(parent):
        if parent in directory_cache:
            return directory_cache[parent]
        if parent != root and parent.name not in names(parent.parent):
            directory_cache[parent] = set()
        elif parent.is_symlink():
            raise ValueError('目标目录是符号链接：' + str(parent))
        elif parent.is_dir():
            directory_cache[parent] = {e.name for e in os.scandir(parent)}
        else:
            raise ValueError('目标目录位置已有普通文件：' + str(parent))
        return directory_cache[parent]
    prefixes = {'OCR': '.'}
    versioned = set()
    for record in records:
        relative = Path(record['old']).relative_to('OCR')
        target = root / relative
        if target.name in names(target.parent):
            if not target.is_file() or target.is_symlink():
                raise ValueError('目标文件位置不是普通文件：' + str(target))
            if digest(target) != record['sha256']:
                versioned.add(relative.parts[0])
    for group in versioned:
        target = Path(group) / ('OCR历史版本-' + uuid.uuid4().hex[:8])
        prefixes[str(Path('OCR')/group)] = target.as_posix()
    for record in records:
        target = remap(record['old'], prefixes)
        record['new'] = target.as_posix()
        record['reuse_existing'] = target.name in names((root/target).parent)
    return {'root': str(root), 'prefixes': prefixes, 'files': records,
            'versioned_business_groups': sorted(versioned), 'file_count': len(records)}


def metadata_edits(root, prefixes, workers, planned_files=None):
    meta = root / '_处理记录'
    candidates = []
    if (meta/'任务').is_dir():
        # Reuse directory-entry type information instead of issuing thousands of
        # serialized Path.stat calls against an external exFAT volume.
        with os.scandir(meta/'任务') as entries:
            candidates = [Path(e.path)/'parts.jsonl' for e in entries
                          if not e.name.startswith('.') and e.is_dir(follow_symlinks=False)]
    candidates += [p for p in meta.iterdir() if p.is_file() and not p.name.startswith('.')
                   and (p.name.startswith('upload-batches-') or p.name == '第一阶段可上传批次.jsonl')]
    def edit(path):
        old = path.read_bytes(); rows = [json.loads(line) for line in old.splitlines()]
        changed = False
        for row in rows:
            if 'file' in row:
                mapped = remap(row['file'], prefixes).as_posix()
                if mapped != row['file']:
                    if 'sha256' in row:
                        expected = (planned_files or {}).get(mapped)
                        if expected is None:
                            target = root/mapped
                            if not target.is_file() or digest(target)!=row['sha256']:
                                raise ValueError('先前已移动成品与活动清单不符：'+mapped)
                        elif expected != row['sha256']:
                            raise ValueError('待归并文件与活动清单不符：'+mapped)
                    row['file'] = mapped; changed = True
        if changed:
            new = ''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows).encode()
            return path, old, new
    with ThreadPoolExecutor(max_workers=workers) as pool:
        edits = [item for item in pool.map(edit, candidates) if item]
    handoff = meta/'WorkBuddy续处理文件映射.json'
    if handoff.exists():
        old = handoff.read_bytes(); data = json.loads(old)
        for entry in data['entries']:
            for part in entry['parts']:
                part['file'] = remap(part['file'], prefixes).as_posix()
                part['absolute_path'] = str(root/part['file'])
        data['layout_migration'] = {'prefixes':prefixes, 'old_state_snapshots_are_historical':True}
        new = (json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
        if new != old: edits.append((handoff, old, new))
    for name in ['前置处理总汇.json', 'OCR1355处理汇总.json']:
        path = meta/name
        if not path.exists():continue
        old = path.read_bytes(); data = json.loads(old)
        def visit(obj):
            if isinstance(obj,dict):
                for key,value in obj.items():
                    if key=='output_directory' and isinstance(value,str) and Path(value).is_relative_to(root):
                        obj[key]=str(root/remap(Path(value).relative_to(root),prefixes))
                    else:visit(value)
            elif isinstance(obj,list):
                for value in obj:visit(value)
        visit(data)
        data['output_layout'] = {'prefixes':prefixes,'canonical_business_directories':True}
        new=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
        if new!=old:edits.append((path,old,new))
    return edits


def migrate_database(connection, prefixes):
    # Preserve every status, input hash, retry and batch identity. Only path columns
    # change, so a directory merge does not trigger quota-consuming reprocessing.
    rows=connection.execute('SELECT rel,md_rel FROM files').fetchall()
    old_keys={r[0] for r in rows}; new_keys=set(); changes=[]
    for old,md in rows:
        new=remap(old,prefixes).as_posix()
        new_md=remap(md,prefixes).as_posix() if md else md
        if new in new_keys:
            raise ValueError('云解析状态主键归并冲突，停止保留现场：'+new)
        new_keys.add(new)
        if old!=new and new in old_keys:
            raise ValueError('云解析目标主键已存在，停止避免覆盖：'+new)
        if (old,md)!=(new,new_md):changes.append((old,new,md,new_md))
    for old,new,md,new_md in changes:
        connection.execute('UPDATE files SET rel=?,md_rel=? WHERE rel=?',(new,new_md,old))
    return changes


def run(root, apply=False, workers=4):
    root=root.resolve();meta=root/'_处理记录'
    if not 1<=workers<=8:raise ValueError('归并 workers 必须为 1..8')
    lock=meta/'.layout-migration.lock'
    if (meta/'.running').exists():raise ValueError('前置处理仍在运行，暂不归并')
    with lock.open('x') as handle:json.dump({'pid':os.getpid()},handle)
    connection=None;created=[];written=[];archived=False;backup=None;committed=False
    try:
        plan=inspect_plan(root,workers)
        print(json.dumps({k:v for k,v in plan.items() if k!='files'},ensure_ascii=False),flush=True)
        if not apply:return plan
        backup=meta/('目录归并-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        backup.mkdir()
        dump(backup/'plan.json',plan)
        db=meta/'云解析/state.db'
        if db.exists():
            connection=sqlite3.connect(db,timeout=30)
            if connection.execute("SELECT COUNT(*) FROM files WHERE status IN ('queued','uploading')").fetchone()[0]:
                raise ValueError('云解析仍有在途任务，暂不迁移路径')
            with sqlite3.connect(backup/'state-before.sqlite') as target:connection.backup(target)
            connection.execute('BEGIN IMMEDIATE')
            before_status=dict(connection.execute('SELECT status,COUNT(*) FROM files GROUP BY status'))
            db_changes=migrate_database(connection,plan['prefixes'])
        else:before_status={};db_changes=[]
        print('核对并备份活动清单',flush=True)
        edits=metadata_edits(root,plan['prefixes'],workers,{r['new']:r['sha256'] for r in plan['files']})
        layout=meta/'output-layout.json'
        if layout.exists():
            prior=load(root)
            if prior and prior!=plan['prefixes']:raise ValueError('存在不同布局映射，需要单独核对')
            layout_old=layout.read_bytes()
        else:layout_old=None
        layout_new=(json.dumps({'prefixes':plan['prefixes'],'migration':str(backup),'source_provenance_unchanged':True},ensure_ascii=False,indent=2)+'\n').encode()
        edits.append((layout,layout_old,layout_new))
        with zipfile.ZipFile(backup/'metadata-before.zip','x',compression=zipfile.ZIP_DEFLATED) as archive:
            for path,old,_ in edits:
                if old is not None:archive.writestr(path.relative_to(root).as_posix(),old)
        with zipfile.ZipFile(backup/'metadata-before.zip') as archive:
            if archive.testzip() is not None:raise ValueError('活动清单备份归档校验失败')
        # Every copy has exclusive creation and a streaming checksum. The source
        # forest stays untouched until all destinations have been verified.
        journal=backup/'journal.jsonl'
        dump(backup/'status.json',{'status':'copying','files':len(plan['files'])})
        with journal.open('x',encoding='utf-8') as log:
            for index,record in enumerate(plan['files'],1):
                source=root/record['old'];target=root/record['new']
                stat=source.stat()
                if (stat.st_size,stat.st_mtime_ns)!=(record['bytes'],record['mtime_ns']):
                    raise ValueError('源文件在执行前变化：'+record['old'])
                if record['reuse_existing']:
                    if digest(target)!=record['sha256']:raise ValueError('目标在执行前变化')
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    try:
                        actual=copy_digest(source,target)
                    except BaseException as error:
                        # copy_digest uses xb; never remove somebody else's preexisting file.
                        if target.exists() and not isinstance(error,FileExistsError):created.append(target)
                        raise
                    created.append(target)
                    if actual!=record['sha256'] or digest(target)!=record['sha256']:
                        raise ValueError('目标复制或落盘校验失败：'+record['new'])
                log.write(json.dumps(record,ensure_ascii=False)+'\n');log.flush()
                if index%100==0:print(f'已归并校验 {index}/{len(plan["files"])}',flush=True)
        current={p.relative_to(root).as_posix() for p in files_under(root/'OCR')}
        if current!={r['old'] for r in plan['files']}:
            raise ValueError('归并期间源目录文件集合变化，停止避免遗漏')
        for record in plan['files']:
            stat=(root/record['old']).stat()
            if (stat.st_size,stat.st_mtime_ns)!=(record['bytes'],record['mtime_ns']):
                raise ValueError('归并期间源文件变化，停止避免遗漏')
        for path,old,new in edits:
            if (path.read_bytes() if path.exists() else None)!=old:raise ValueError('活动清单被其他进程修改，停止')
            atomic_bytes(path,new);written.append((path,old))
        (root/'OCR').rename(backup/'原OCR目录');archived=True
        if connection:
            after_status=dict(connection.execute('SELECT status,COUNT(*) FROM files GROUP BY status'))
            if before_status!=after_status:raise ValueError('归并不应改变云解析任务状态')
        result={'status':'complete','files':len(plan['files']),'reused_identical':sum(r['reuse_existing'] for r in plan['files']),
                'versioned_business_groups':plan['versioned_business_groups'],'metadata_files_updated':len(edits),
                'database_paths_updated':len(db_changes),'cloud_status_counts':before_status,'archive':str(backup),
                'all_output_sha256_verified':True,'ocr_root_archived':True}
        dump(backup/'database-path-changes.json',[{'old':a,'new':b,'old_md':c,'new_md':d} for a,b,c,d in db_changes])
        dump(backup/'status.json',{**result,'status':'committing'})
        if connection:connection.commit()
        committed=True
        dump(backup/'status.json',result);dump(meta/'目录归并结果.json',result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return result
    except BaseException:
        # Once SQLite commits, keep the matching filesystem/manifest state even
        # if writing the final report fails. Never roll back just half the system.
        if committed:raise
        if connection:connection.rollback()
        if archived:(backup/'原OCR目录').rename(root/'OCR')
        for path,old in reversed(written):
            if old is None:path.unlink(missing_ok=True)
            else:atomic_bytes(path,old)
        for path in reversed(created):path.unlink(missing_ok=True)
        if backup:dump(backup/'status.json',{'status':'rolled_back'})
        raise
    finally:
        if connection:connection.close()
        lock.unlink(missing_ok=True)
