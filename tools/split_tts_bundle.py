"""Separate public model downloads from private voice/source transport."""
import argparse
import json
from pathlib import Path
import tarfile

p=argparse.ArgumentParser();p.add_argument('root',type=Path);args=p.parse_args()
root=args.root
rows=json.loads((root/'tts-manifest.json').read_text('utf-8'))
prefix='breeze/model/drbaph_Breeze-TTS-2-comfyui/'
downloads=[{**row,'url':'https://hf-mirror.com/drbaph/Breeze-TTS-2-comfyui/resolve/a6bfd9d0d4e8b3b61c3a5ed3ecf55468c8ab88e4/'+row['path'][len(prefix):]} for row in rows if row['path'].startswith(prefix)]
(root/'model-downloads.json').write_text(json.dumps(downloads),encoding='utf-8')
with tarfile.open(root/'tts-assets.tar') as source,tarfile.open(root/'tts-private.tar.gz','w:gz') as dest:
    for member in source:
        if member.name.startswith(prefix):continue
        dest.addfile(member,source.extractfile(member))
print(json.dumps({'public_files':len(downloads),'private_archive_bytes':(root/'tts-private.tar.gz').stat().st_size}))
