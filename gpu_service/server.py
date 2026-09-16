"""Durable task API for one Compshare host; bind loopback behind HTTPS."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from contextlib import contextmanager
from aiohttp import web

ID = re.compile(r'[A-Za-z0-9_-]{1,100}')


def kill_posix_tree(pid):
    # Model runners create their own sessions, so killing only the API worker's
    # process group leaves those GPU processes alive.
    import psutil
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try: child.kill()
            except psutil.NoSuchProcess: pass
        parent.kill()
    except psutil.NoSuchProcess:
        pass


SCHEMAS = {
    'tts': ({'text', 'voice_plan'}, {'text'}),
    'cover': ({'source_asset','lyrics','language'}, {'source_asset'}),
    'original': ({'lyrics'}, {'lyrics'}),
    'video': ({'text','voice_plan','scene_asset','adaptive_delivery','enforce_content_gate'}, {'text','scene_asset'}),
    'lipsync': ({'audio_asset','scene_asset'}, {'audio_asset','scene_asset'}),
    'separate': ({'audio_asset'}, {'audio_asset'}),
}

def build_app(config, root):
    root=Path(root).resolve(); root.mkdir(parents=True,exist_ok=True)
    for name in ('jobs','uploads'): (root/name).mkdir(exist_ok=True)
    @contextmanager
    def db():
        c=sqlite3.connect(root/'tasks.sqlite3',timeout=15);c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally: c.close()
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
          digest TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL, elapsed REAL,
          UNIQUE(owner,request_id));
          CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, owner TEXT NOT NULL, path TEXT NOT NULL);''')
    public=config['public_url'].rstrip('/')
    parsed=urlsplit(public)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path or not (parsed.scheme=='https' or (parsed.scheme=='http' and parsed.hostname=='127.0.0.1')):
        raise ValueError('HTTPS required')
    signing=config['signing_key'].encode()
    if len(signing)<32 or not config['tokens']: raise ValueError('Secrets required')
    wake=asyncio.Event(); processes={}
    upload_locks={name:asyncio.Lock() for name in config['tokens']}
    profiles=config.get('profiles',{})
    store=None
    if config.get('result_store'):
        from gpu_service.result_store import ResultStore
        store=ResultStore(config['result_store'])
    if set(profiles)-SCHEMAS.keys(): raise ValueError('Unsupported profile')
    def owner(request):
        value=request.headers.get('Authorization','')
        for name,token in config['tokens'].items():
            if len(token)>=32 and hmac.compare_digest(value,'Bearer '+token):return name
        raise web.HTTPUnauthorized()
    def asset(owner_id,aid):
        if not isinstance(aid,str) or not ID.fullmatch(aid):raise web.HTTPBadRequest(text='ASSET_INVALID')
        shared=config.get('shared_assets',{}).get(aid)
        if shared:
            path=Path(shared['path']).resolve()
            # Content fingerprint is pinned by the deployment manifest, not by clients.
            return str(path)
        with db() as c:r=c.execute('SELECT path FROM assets WHERE id=? AND owner=?',(aid,owner_id)).fetchone()
        if not r:raise web.HTTPNotFound(text='ASSET_NOT_FOUND')
        return r['path']
    def projection(r):
        out=[]
        if r['status']=='succeeded':
            expires=int(time.time())+900
            sig=hmac.new(signing,f"{r['id']}:{expires}".encode(),hashlib.sha256).hexdigest()
            out=[{'url':f"{public}/v1/results/{r['id']}?expires={expires}&signature={sig}"}]
            if store and (root/'jobs'/r['id']/'r2-result.json').is_file():
                out=[{'url':store.url(root/'jobs'/r['id'])}]
        return {'task_id':r['id'],'status':r['status'],'outputs':out}
    async def capabilities(request):
        owner(request)
        return web.json_response({'kinds':list(profiles),'shared_assets':[
            {'asset_id':k,'sha256':v['sha256']} for k,v in config.get('shared_assets',{}).items()],
            'billing_enabled':False,'max_upload_bytes':config.get('max_upload_bytes',268435456)})
    async def upload(request):
        who=owner(request)
        async with upload_locks[who]:return await store_upload(request,who)
    async def store_upload(request,who):
        suffix=request.headers.get('X-Asset-Suffix','')
        if suffix not in ('.wav','.mp3','.flac','.mp4','.png','.jpg'):raise web.HTTPBadRequest()
        limit=config.get('max_upload_bytes',268435456)
        with db() as c:
            paths=[r[0] for r in c.execute('SELECT path FROM assets WHERE owner=?',(who,))]
        remaining=config.get('upload_quota_bytes',2147483648)-sum(Path(p).stat().st_size for p in paths if Path(p).exists())
        if len(paths)>=100 or remaining<=0:raise web.HTTPRequestEntityTooLarge(max_size=limit,actual_size=limit+1)
        limit=min(limit,remaining)
        aid=secrets.token_hex(16);path=root/'uploads'/(aid+suffix);size=0
        try:
            with path.open('xb') as f:
                async for chunk in request.content.iter_chunked(65536):
                    size+=len(chunk)
                    if size>limit:raise web.HTTPRequestEntityTooLarge(max_size=limit,actual_size=size)
                    f.write(chunk)
            if not size:raise web.HTTPBadRequest()
            with db() as c:c.execute('INSERT INTO assets VALUES(?,?,?)',(aid,who,str(path)))
        except BaseException:
            path.unlink(missing_ok=True);raise
        return web.json_response({'asset_id':aid},status=201)
    async def submit(request):
        who=owner(request)
        try: body=await request.json()
        except (ValueError,UnicodeError):raise web.HTTPBadRequest()
        if not isinstance(body,dict) or set(body)!={'request_id','kind','input'}:raise web.HTTPBadRequest()
        rid,kind,data=body['request_id'],body['kind'],body['input']
        if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',rid) or request.headers.get('Idempotency-Key')!=rid:raise web.HTTPBadRequest()
        if not isinstance(kind,str) or kind not in profiles:raise web.HTTPBadRequest(text='CAPABILITY_UNAVAILABLE')
        allowed,required=SCHEMAS[kind]
        if not isinstance(data,dict) or set(data)-allowed or required-set(data):raise web.HTTPBadRequest()
        for key,value in data.items():
            if key in ('adaptive_delivery','enforce_content_gate'):
                if not isinstance(value,bool):raise web.HTTPBadRequest()
            elif key=='voice_plan':
                if not isinstance(value,dict):raise web.HTTPBadRequest()
            elif not isinstance(value,str) or len(value)>16000:raise web.HTTPBadRequest()
            if key.endswith('_asset'):asset(who,value)
        try:canonical=json.dumps(body,sort_keys=True,allow_nan=False)
        except (ValueError,TypeError):raise web.HTTPBadRequest()
        digest=hashlib.sha256(canonical.encode()).hexdigest()
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM tasks WHERE owner=? AND request_id=?',(who,rid)).fetchone()
            if r:
                if r['digest']!=digest:raise web.HTTPConflict(text='IDEMPOTENCY_CONFLICT')
                return web.json_response(projection(r))
            if c.execute("SELECT count(*) FROM tasks WHERE owner=? AND status IN ('queued','running')",(who,)).fetchone()[0]>=5:raise web.HTTPTooManyRequests()
            tid=secrets.token_hex(16)
            c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,NULL)',(tid,who,rid,digest,kind,json.dumps(data),'queued',time.time()))
            r=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        wake.set();return web.json_response(projection(r),status=202)
    async def task(request):
        who=owner(request);tid=request.match_info['tid']
        with db() as c:r=c.execute('SELECT * FROM tasks WHERE id=? AND owner=?',(tid,who)).fetchone()
        if not r:raise web.HTTPNotFound()
        if request.method=='POST' and r['status'] in ('queued','running'):
            with db() as c:
                c.execute("UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('queued','running')",(tid,))
                r=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            proc=processes.get(tid)
            if proc:await stop(proc)
        return web.json_response(projection(r))
    async def result(request):
        tid=request.match_info['tid']
        try:expires=int(request.query['expires'])
        except (KeyError,ValueError):raise web.HTTPForbidden()
        expected=hmac.new(signing,f'{tid}:{expires}'.encode(),hashlib.sha256).hexdigest()
        if not ID.fullmatch(tid) or expires<time.time() or not hmac.compare_digest(expected,request.query.get('signature','')):raise web.HTTPForbidden()
        with db() as c:r=c.execute('SELECT status FROM tasks WHERE id=?',(tid,)).fetchone()
        if not r or r['status']!='succeeded':raise web.HTTPNotFound()
        return web.FileResponse(root/'jobs'/tid/'output.bin',headers={'Cache-Control':'private, no-store','Content-Disposition':'attachment'})
    async def stop(proc):
        if proc.returncode is not None:return
        if os.name=='nt':
            killer=await asyncio.create_subprocess_exec('taskkill','/PID',str(proc.pid),'/T','/F',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,creationflags=0x08000000)
            await killer.wait()
        else:
            kill_posix_tree(proc.pid)
        await proc.wait()
    async def work(gpu):
        while True:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if r:c.execute("UPDATE tasks SET status='running' WHERE id=?",(r['id'],))
            if not r:
                wake.clear()
                try:await asyncio.wait_for(wake.wait(),2)
                except asyncio.TimeoutError:pass
                continue
            job=root/'jobs'/r['id'];job.mkdir(exist_ok=True);started=time.monotonic();proc=None
            try:
                data=json.loads(r['payload'])
                for key in list(data):
                    if key.endswith('_asset'):data[key]=asset(r['owner'],data[key])
                spec={'kind':r['kind'],'input':data,'profile':profiles[r['kind']],'output':str(job/'output.bin'),'job':str(job)}
                (job/'request.json').write_text(json.dumps(spec),encoding='utf-8')
                env={**os.environ,'CUDA_VISIBLE_DEVICES':str(gpu),'OLIVIA_GPU_ROUTE':'local','PYTHONIOENCODING':'utf-8'}
                kwargs={'creationflags':0x08000000} if os.name=='nt' else {'start_new_session':True}
                with (job/'worker.log').open('wb') as log:
                    proc=await asyncio.create_subprocess_exec(config.get('python',sys.executable),'-m','gpu_service.worker',str(job/'request.json'),cwd=Path(__file__).resolve().parents[1],env=env,stdout=log,stderr=log,**kwargs)
                    processes[r['id']]=proc
                    # Cancellation can occur between claiming and starting the worker.
                    with db() as c:state=c.execute('SELECT status FROM tasks WHERE id=?',(r['id'],)).fetchone()[0]
                    if state=='cancelled':await stop(proc)
                    await asyncio.wait_for(proc.wait(),config.get('task_timeout_seconds',3600))
                success=proc.returncode==0 and (job/'output.bin').is_file() and (job/'output.bin').stat().st_size>0
                if success and store:
                    try:await asyncio.to_thread(store.publish,r['id'],r['kind'],job)
                    except Exception:
                        # Preserve the completed generation and direct-download
                        # fallback; a storage outage must not trigger regeneration.
                        logging.warning('RESULT_STORE_UPLOAD_FAILED task=%s',r['id'])
                with db() as c:c.execute("UPDATE tasks SET status=?,elapsed=? WHERE id=? AND status='running'",('succeeded' if success else 'failed',time.monotonic()-started,r['id']))
            except asyncio.CancelledError:
                if proc:await stop(proc)
                raise
            except Exception:
                if proc:await stop(proc)
                with db() as c:c.execute("UPDATE tasks SET status='failed',elapsed=? WHERE id=? AND status='running'",(time.monotonic()-started,r['id']))
            finally:processes.pop(r['id'],None)
    async def lifecycle(app):
        # Fail interrupted jobs instead of silently regenerating potentially costly outputs.
        with db() as c:c.execute("UPDATE tasks SET status='failed' WHERE status='running'")
        for shared in config.get('shared_assets',{}).values():
            with Path(shared['path']).open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=shared['sha256']:raise ValueError('Shared asset hash mismatch')
        workers=[asyncio.create_task(work(gpu)) for gpu in config.get('gpus',['0'])]
        yield
        for w in workers:w.cancel()
        await asyncio.gather(*workers,return_exceptions=True)
    app=web.Application(client_max_size=32768)
    app.router.add_get('/v1/capabilities',capabilities)
    app.router.add_post('/v1/assets',upload)
    app.router.add_post('/v1/tasks',submit)
    app.router.add_get('/v1/tasks/{tid}',task)
    app.router.add_post('/v1/tasks/{tid}/cancel',task)
    app.router.add_get('/v1/results/{tid}',result)
    app.cleanup_ctx.append(lifecycle)
    return app

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--data',type=Path,required=True);p.add_argument('--port',type=int,default=18880)
    a=p.parse_args();web.run_app(build_app(json.loads(a.config.read_text(encoding='utf-8')),a.data),host='127.0.0.1',port=a.port,access_log=None)
