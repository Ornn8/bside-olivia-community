"""Exercise the public API as two isolated users; SSH only retrieves test keys."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit


async def main(args):
    if args.certificate:
        os.environ['SSL_CERT_FILE'] = str(args.certificate.resolve())
    else:
        os.environ.pop('SSL_CERT_FILE',None)
    from aiohttp import ClientSession, ClientTimeout
    from runtime.remote_generation import RemoteGeneration
    raw = subprocess.check_output(['ssh','-i',str(args.ssh_key),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
        '-o','StrictHostKeyChecking=yes',f'ubuntu@{args.host}','cat /etc/olivia-gpu/config.json'],
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    config = json.loads(raw)
    url = 'https://' + args.host
    report = {'endpoint': url, 'transport': 'public HTTPS, certificate verified',
              'certificate_trust':'explicit test certificate' if args.certificate else 'system default', 'passed': False}
    async with ClientSession(timeout=ClientTimeout(total=20),trust_env=False) as session:
        async with session.get(url+'/v1/capabilities',headers={'Authorization':'Bearer invalid-test-key'},allow_redirects=False) as response:
            report['invalid_key_status'] = response.status
            assert response.status == 401
        api = RemoteGeneration(url,config['tokens']['acceptance-user-a'])
        capabilities = await api.request('capabilities',{})
        report['kinds'] = capabilities['kinds']
        if not args.probe_only:
            start=time.monotonic()
            payload={'text':'今天的任务已经完成了，记得休息一下。'}
            assets={}
            if args.kind == 'video':
                if not args.scene: raise ValueError('--scene is required for video acceptance')
                payload['adaptive_delivery']=True
                assets={'scene_asset':args.scene}
            elif args.kind in ('original','cover'):
                if args.kind == 'original' and not args.lyrics: raise ValueError('--lyrics is required for original acceptance')
                payload={'lyrics':args.lyrics.read_text(encoding='utf-8') if args.lyrics else ''}
                if args.kind == 'cover':
                    if not args.source: raise ValueError('--source is required for cover acceptance')
                    payload['language']='zh';assets={'source_asset':args.source}
            elif args.kind == 'separate':
                if not args.source: raise ValueError('--source is required for separation acceptance')
                payload={};assets={'audio_asset':args.source}
            artifact=args.output/('cloud-video.mp4' if args.kind == 'video' else 'cloud-'+args.kind+'.wav')
            if args.task_id:
                task=await api.download_task(args.task_id,artifact)
                report['recovered_existing_task']=True
            else:
                task=await api.generate(args.kind,payload,artifact,timeout=1200,assets=assets)
            report['task_id']=task['task_id'];report['wall_seconds']=round(time.monotonic()-start,2)
            report['result_host']=urlsplit(task['outputs'][0]['url']).hostname
            if report['result_host'].endswith('.r2.cloudflarestorage.com'):
                unsigned=task['outputs'][0]['url'].split('?',1)[0]
                async with session.get(unsigned,allow_redirects=False) as response:
                    report['unsigned_result_status']=response.status
                    error=await response.text()
                    assert response.status in (401,403) or (response.status==400 and '<Message>Authorization</Message>' in error)
            from runtime.media.music_reply import _media_duration_seconds
            if args.kind != 'video':
                report['audio_seconds']=_media_duration_seconds(artifact,required_streams=('0:a:0',))
                assert report['audio_seconds'] and report['audio_seconds']>1
            else:
                report['video_seconds']=_media_duration_seconds(artifact,required_streams=('0:a:0','0:v:0'))
                assert report['video_seconds'] and report['video_seconds']>1
            async with session.get(url+'/v1/tasks/'+task['task_id'],headers={'Authorization':'Bearer '+config['tokens']['acceptance-user-b']}) as response:
                report['other_user_status']=response.status
                assert response.status==404
        report['passed']=True
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/('probe.json' if args.probe_only else args.kind+'-result.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('--ssh-key',type=Path,required=True)
    p.add_argument('--certificate',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--probe-only',action='store_true')
    p.add_argument('--kind',choices=('tts','video','original','cover','separate'),default='tts');p.add_argument('--scene',type=Path)
    p.add_argument('--source',type=Path);p.add_argument('--lyrics',type=Path)
    p.add_argument('--task-id')
    asyncio.run(main(p.parse_args()))
