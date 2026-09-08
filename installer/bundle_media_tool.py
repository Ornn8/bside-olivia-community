"""Bundle the pinned Windows FFmpeg for offline backend upgrades."""
import hashlib
import io
from pathlib import Path
import urllib.request
import zipfile

WHEEL_SHA256 = '02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a'
WHEEL_URL = 'https://files.pythonhosted.org/packages/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl'


def bundle_media_tool(payload: Path, *, wheel: Path | None = None) -> None:
    # Download at build time only. Installed applications never fetch dependencies here.
    if wheel is None:
        with urllib.request.urlopen(WHEEL_URL, timeout=120) as response:
            content = response.read()
    else:
        content = wheel.read_bytes()
    if hashlib.sha256(content).hexdigest() != WHEEL_SHA256:
        raise RuntimeError('MEDIA_TOOL_WHEEL_HASH_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        binary = [name for name in archive.namelist()
                  if name.startswith('imageio_ffmpeg/binaries/') and name.endswith('.exe')]
        licenses = [name for name in archive.namelist()
                    if 'license' in name.lower() and not name.endswith('/')]
        if len(binary) != 1 or not licenses:
            raise RuntimeError('MEDIA_TOOL_WHEEL_INVALID')
        root = payload / 'media-tools'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'ffmpeg.exe').write_bytes(archive.read(binary[0]))
        for index, name in enumerate(licenses):
            (root / f'LICENSE-{index}.txt').write_bytes(archive.read(name))
        (root / 'SOURCE.txt').write_text(WHEEL_URL + '\nsha256: ' + WHEEL_SHA256 + '\n', encoding='utf-8')
