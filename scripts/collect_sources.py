"""Record version-specific upstream source archives without executing setup code."""
from pathlib import Path
import importlib.metadata,json,urllib.request,hashlib
root=Path(__file__).resolve().parents[1];dest=root/'release/ThirdPartySources';dest.mkdir(parents=True,exist_ok=True)
rows=[]
for d in sorted(importlib.metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
    name=d.metadata['Name'];version=d.version
    data=json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json',timeout=30))
    item=next((x for x in data['urls'] if x['packagetype']=='sdist'),None)
    row={'name':name,'version':version,'license':d.metadata.get('License-Expression') or d.metadata.get('License'),'project_urls':data['info']['project_urls'],'source':item['url'] if item else None}
    if item:
        target=dest/item['filename']
        if not target.exists():urllib.request.urlretrieve(item['url'],target)
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        assert digest==item['digests']['sha256'];row.update(archive=target.name,sha256=digest)
    rows.append(row);print(name,version,flush=True)
(dest/'python-sources.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
(dest/'README.txt').write_text('本目录提供固定版本 Python 依赖源码发行包、校验值与来源索引。原始二进制轮包内的第三方许可保留在应用运行时中。\nLibreOffice 26.2.6 对应源码：https://download.documentfoundation.org/libreoffice/src/26.2.6/\nCPython 3.12.15：https://www.python.org/ftp/python/3.12.15/Python-3.12.15.tar.xz\n独立 Python 构建配方：https://github.com/astral-sh/python-build-standalone/tree/20261013\nLibreOffice 原始二进制内保留 LICENSE、NOTICE、CREDITS。这里的源代码仅为获取和再构建依据，不会执行。\n')
