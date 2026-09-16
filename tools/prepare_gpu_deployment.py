"""Prepare a private deployment bundle from an installed, working TTS profile."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def main(args):
    repo = Path(__file__).resolve().parents[1]
    dest = args.output.resolve(); dest.mkdir(parents=True, exist_ok=True)
    settings = json.loads(args.config.read_text('utf-8'))['settings']
    files = subprocess.check_output(['git', 'ls-files'], cwd=repo, text=True).splitlines()
    files += ['runtime/cloud_service.py', 'runtime/remote_generation.py', 'runtime/remote_pipeline.py', 'original_client_cloud_api.py']
    files += [str(p.relative_to(repo)).replace('\\','/') for p in (repo/'gpu_service').glob('*.py')]
    files += ['gpu_service/requirements.txt']
    with tarfile.open(dest/'service.tar.gz', 'w:gz') as archive:
        for name in sorted(set(files)):
            path = repo/name
            if path.is_file() and path.suffix in ('.py','.json','.toml','.txt','.yaml','.yml') and not name.startswith(('tests/','.','docs/')):
                archive.add(path, arcname=name, recursive=False)
    mapping = {'runtime_root': 'breeze/runtime', 'model_dir': 'breeze/model', 'reference_audio': 'shared/reference.wav'}
    roots = [(Path(settings[key]), relative) for key,relative in mapping.items()]
    options = dict(settings.get('provider_options', {}))
    if options.get('adapter_dir'): roots.append((Path(options['adapter_dir']), 'breeze/adapter'))
    manifest = []
    excluded = {'.git','__pycache__','.venv','venv','python','wheels','.cache','node_modules'}
    with tarfile.open(dest/'tts-assets.tar', 'w') as archive:
        for source, target in roots:
            if not source.exists(): raise FileNotFoundError(source)
            candidates = sorted(source.rglob('*')) if source.is_dir() else [source]
            for path in candidates:
                relative = path.relative_to(source) if source.is_dir() else Path()
                if not path.is_file() or excluded.intersection(relative.parts) or path.suffix in ('.pyc','.pyd','.exe','.dll'): continue
                name = str(Path(target)/relative).replace('\\','/')
                with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                manifest.append({'path': name, 'bytes': path.stat().st_size, 'sha256': digest})
                archive.add(path, arcname=name, recursive=False)
    for key,relative in mapping.items(): settings[key] = '/srv/olivia-assets/'+relative
    options['external_python'] = '/opt/olivia-gpu/breeze-env/bin/python'
    if options.get('adapter_dir'): options['adapter_dir'] = '/srv/olivia-assets/breeze/adapter'
    if options.get('model_license_path'): options['model_license_path'] = '/srv/olivia-assets/breeze/model/LICENSE'
    for key in ('temp_root','numba_cache_dir','quality_gate_cache_root','quality_gate_python','wetext_fst_root'):
        options.pop(key, None)
    settings['provider_options'] = options
    (dest/'tts.json').write_text(json.dumps({'settings':settings},ensure_ascii=False,indent=2),encoding='utf-8')
    (dest/'tts-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(manifest),'asset_gib':round(sum(x['bytes'] for x in manifest)/2**30,2),'bundle':str(dest/'tts-assets.tar')}))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--config',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args())
