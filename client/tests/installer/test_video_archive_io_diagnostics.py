import errno
import zipfile
from pathlib import Path

import pytest

from video_capability_install import VideoCapabilityError, VideoCapabilityInstaller, _extract_zip_safely


@pytest.mark.parametrize("error,code", [
    (OSError(errno.ENOSPC, "private"), "VIDEO_ARCHIVE_DISK_FULL"),
    (PermissionError(errno.EACCES, "private"), "VIDEO_ARCHIVE_ACCESS_DENIED"),
    (OSError(errno.ENAMETOOLONG, "private"), "VIDEO_ARCHIVE_PATH_TOO_LONG"),
    (OSError(errno.EIO, "private"), "VIDEO_ARCHIVE_IO_FAILED"),
])
def test_valid_archive_output_io_failure_is_not_reported_as_bad_zip(tmp_path, monkeypatch, error, code):
    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("model.bin", b"synthetic")
    original = Path.open

    def output_failure(path, mode="r", *args, **kwargs):
        if mode == "xb":
            raise error
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", output_failure)
    with pytest.raises(VideoCapabilityError) as failure:
        _extract_zip_safely(archive, tmp_path / "output", strip_components=0)
    assert str(failure.value) == code


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_breeze_classifies_long_path_without_exposing_output(tmp_path, monkeypatch, stream):
    from types import SimpleNamespace
    import video_capability_install
    from runtime.diagnostics.support_bundle import BREEZE_INSTALL_DIAGNOSTIC_CODES
    output = {"stdout": "", "stderr": "", "returncode": 1}
    output[stream] = "ERROR [WinError 206] private path"
    monkeypatch.setattr(video_capability_install.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(**output))
    with pytest.raises(VideoCapabilityError) as failure:
        VideoCapabilityInstaller._install_breeze_runtime_packages(tmp_path / "python.exe", tmp_path, tmp_path / "lock")
    assert failure.value.diagnostic_code == "BREEZE_PIP_PATH_TOO_LONG"
    assert failure.value.diagnostic_code in BREEZE_INSTALL_DIAGNOSTIC_CODES
    assert "private" not in str(failure.value)
