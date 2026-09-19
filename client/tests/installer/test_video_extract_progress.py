import zipfile

from video_capability_install import _extract_zip_safely


def test_zip_extraction_reports_bytes_then_verification(tmp_path):
    archive = tmp_path / 'runtime.zip'
    payload = b'x' * (2 * 1024 * 1024 + 3)
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('runtime/model.bin', payload)
    events = []
    _extract_zip_safely(archive, tmp_path / 'output', strip_components=1,
                        progress=lambda phase, done, total: events.append((phase, done, total)))
    assert ('extracting', len(payload), len(payload)) in events
    assert events[-1] == ('verifying', len(payload), len(payload))
    assert any(phase == 'extracting' and 0 < done < total for phase, done, total in events)
    assert (tmp_path / 'output/model.bin').read_bytes() == payload
