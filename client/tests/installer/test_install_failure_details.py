import errno
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from runtime.diagnostics.install_failure import install_failure, project_install_failure
from video_capability_install import VideoCapabilityInstaller, VideoCapabilityError, VideoFile


@pytest.mark.parametrize('error,kind', [
    (OSError(errno.ENOSPC, 'secret path'), 'disk_full'),
    (PermissionError(errno.EACCES, 'secret path'), 'permission'),
    (OSError(errno.ENAMETOOLONG, 'secret path'), 'path_too_long'),
    (TimeoutError('secret URL'), 'timeout'),
    (FileNotFoundError('secret path'), 'file_missing'),
])
def test_failure_classification_without_private_text(error, kind):
    value = install_failure(error, stage='stage_copy', source='local', file_id='weights')
    assert value['kind'] == kind
    assert 'secret' not in json.dumps(value)


def test_download_failure_keeps_actual_last_source_before_first_byte(tmp_path):
    def opener(request, **kwargs):
        raise HTTPError(request.full_url, 403, 'secret token', None, None)
    installer = SimpleNamespace(_opener=opener)
    item = VideoFile('weights', 'model.bin', 1, '0'*64, 'test',
                     {'domestic': 'https://mirror.invalid/secret', 'official': 'https://upstream.invalid/secret'})
    with pytest.raises(VideoCapabilityError) as raised:
        VideoCapabilityInstaller._download(installer, item, tmp_path/'model.bin', 'auto')
    assert str(raised.value) == 'VIDEO_DOWNLOAD_FAILED'
    assert raised.value.failure_details == {'stage': 'download', 'source': 'official', 'kind': 'http', 'file_id': 'weights', 'http_status': 403}
    assert 'secret' not in json.dumps(raised.value.failure_details)


def test_projection_drops_paths_urls_unknown_strings_and_bool_counts():
    assert project_install_failure({'stage': 'secret', 'source': 'https://secret', 'kind': [],
                                   'errno': True, 'file_id': 'C:/private/file', 'message': 'secret',
                                   'component': 'C:/private/file'}) == {}


def test_projection_keeps_known_failed_component():
    assert project_install_failure({'stage': 'activate', 'component': 'voice_reference',
                                    'kind': 'file_missing'}) == {
        'stage': 'activate', 'component': 'voice_reference', 'kind': 'file_missing'}


def test_memory_initialization_retains_windows_storage_categories():
    from runtime.memory.mem0_memory import _initialization_error_code
    for win, code in [(32, 'MEM0_STORAGE_LOCKED'), (206, 'MEM0_STORAGE_PATH_TOO_LONG'), (112, 'MEM0_STORAGE_FULL')]:
        error = OSError('private path'); error.winerror = win
        assert _initialization_error_code(error) == code
