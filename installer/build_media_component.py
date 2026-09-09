"""Build one independent offline ZIP from explicit local source mounts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.media.component_packages import COMPONENTS, _component
from video_capability_install import _safe_relative, _is_reparse_point, _sha256_file


def build(plan: dict, output: Path) -> dict:
    component = plan['component']
    if component not in COMPONENTS or output.exists():
        raise ValueError('MEDIA_COMPONENT_BUILD_INVALID')
    manifest = {'schema_version': 'olivia.video-runtime-root.v1',
                'version': f"component.{component}.{plan['version']}",
                'environment': plan['environment'], 'files': []}
    _component(manifest)
    files = {}
    for mount in plan['mounts']:
        source = Path(mount['source']).resolve(strict=True)
        target = _safe_relative(mount['target'])
        for path in sorted(source.rglob('*')) if source.is_dir() else [source]:
            if any(part in {'.git', '__pycache__', '.cache', '.pytest_cache', *mount.get('exclude', [])} for part in path.relative_to(source.parent if source.is_file() else source).parts):
                continue
            if _is_reparse_point(path):
                raise ValueError('MEDIA_COMPONENT_SOURCE_REPARSE')
            if not path.is_file() or path.suffix.lower() in {'.pyc', '.pyo', '.partial', '.incomplete', '.tmp'}:
                continue
            relative = target if source.is_file() else target + '/' + path.relative_to(source).as_posix()
            relative = _safe_relative(relative)
            if relative.casefold() in files:
                raise ValueError('MEDIA_COMPONENT_DUPLICATE_FILE')
            files[relative.casefold()] = (relative, path)
    for value in manifest['environment'].values():
        value = _safe_relative(value).casefold()
        if value not in files and not any(key.startswith(value + '/') for key in files):
            raise ValueError('MEDIA_COMPONENT_ENVIRONMENT_MISSING')
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix('.partial')
    total = sum(path.stat().st_size for _, path in files.values())
    done = 0
    with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        for relative, source in files.values():
            digest = hashlib.sha256()
            size = 0
            info = zipfile.ZipInfo(relative)
            info._compresslevel = 1
            # Large tensor weights gain little from compression; avoid lengthy CPU work.
            info.compress_type = zipfile.ZIP_STORED if source.suffix.lower() in {'.safetensors', '.ckpt', '.pt', '.pth'} else zipfile.ZIP_DEFLATED
            with source.open('rb') as reader, archive.open(info, 'w', force_zip64=True) as writer:
                while chunk := reader.read(4 * 1024 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            manifest['files'].append({'path': relative, 'size_bytes': size, 'sha256': digest.hexdigest()})
            done += size
            if size > 100 * 1024 * 1024:
                print(f'{component}: {done}/{total}', flush=True)
        archive.writestr('runtime-manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    os.replace(partial, output)
    size, digest = _sha256_file(output)
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '\n', encoding='ascii')
    return {'id': component, 'file': output.name, 'size_bytes': size, 'expanded_bytes': done, 'sha256': digest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(json.loads(args.plan.read_text(encoding='utf-8')), args.output)))
