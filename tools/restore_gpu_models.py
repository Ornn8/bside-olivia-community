"""Download pinned public weights, verify before promoting each file."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import time
import subprocess

root=Path('/srv/olivia-assets')
rows=json.loads(Path(sys.argv[1]).read_text('utf-8'))
def fetch(row):
    path=(root/row['path']).resolve()
    if not path.is_relative_to(root):raise ValueError('Invalid path')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file():
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()==row['sha256']:return
    part=path.with_name(path.name+'.partial')
    for attempt in range(3):
        try:
            if row['bytes'] > 100000000:
                command=['aria2c','--continue=true','--file-allocation=none','--auto-file-renaming=false',
                    '--allow-overwrite=true','--max-connection-per-server=8','--split=8','--min-split-size=8M',
                    '--max-tries=3','--retry-wait=3','--summary-interval=0','--console-log-level=warn',
                    '--dir='+str(part.parent),'--out='+part.name,row['url']]
            else:
                command=['curl','--fail','--location','--retry','3','--max-time','120','--output',str(part),row['url']]
            subprocess.run(command,check=True,capture_output=True,timeout=1800)
            size=part.stat().st_size
            with part.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if size!=row['bytes'] or digest!=row['sha256']:raise ValueError('Hash mismatch')
            part.replace(path)
            print('verified',row['path'],size,flush=True)
            return
        except Exception as exc:
            print('retry',row['path'],type(exc).__name__,flush=True)
            if attempt==2:raise
            time.sleep(3)
with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(fetch,rows))
print('ALL_MODELS_VERIFIED',flush=True)
