"""Fingerprint files already copied to the GPU host; never upload per task."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def manifest(root, entries):
    root = Path(root).resolve()
    result = {}
    for entry in entries:
        aid, relative = entry.split('=', 1)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', aid) or aid in result:
            raise ValueError('Invalid or duplicate asset ID')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Asset must be a file inside root')
        with path.open('rb') as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
        result[aid] = {'path': str(path), 'sha256': digest}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--asset', action='append', required=True, help='ID=relative/path')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(manifest(args.root, args.asset), ensure_ascii=False, indent=2), encoding='utf-8')
