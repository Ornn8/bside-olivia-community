import errno
import zipfile

import pytest
from video_capability_install import _extract_runtime_zip_safely, VideoCapabilityError


@pytest.mark.parametrize('number,code', [(errno.ENOSPC, 'VIDEO_ARCHIVE_DISK_FULL'),
    (errno.EACCES, 'VIDEO_ARCHIVE_ACCESS_DENIED'),
    (errno.ENAMETOOLONG, 'VIDEO_ARCHIVE_PATH_TOO_LONG'),
    (errno.EIO, 'VIDEO_ARCHIVE_IO_FAILED')])
def test_runtime_extraction_preserves_io_failure_category(tmp_path, monkeypatch, number, code):
    archive = tmp_path / 'runtime.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('runtime-manifest.json', '{}')
    def fail(*args, **kwargs):
        raise OSError(number, 'private-path')
    monkeypatch.setattr(zipfile.ZipFile, 'open', fail)
    with pytest.raises(VideoCapabilityError, match='^' + code + '$'):
        _extract_runtime_zip_safely(archive, tmp_path / 'extracted')


def test_bad_zip_has_distinct_corruption_code(tmp_path):
    archive = tmp_path / 'runtime.zip'
    archive.write_bytes(b'broken')
    with pytest.raises(VideoCapabilityError, match='^VIDEO_RUNTIME_ARCHIVE_CORRUPT$'):
        _extract_runtime_zip_safely(archive, tmp_path / 'extracted')
