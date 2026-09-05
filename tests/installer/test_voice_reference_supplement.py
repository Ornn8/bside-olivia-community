import zipfile
import pytest
import video_capability_install as video


def test_supplement_restores_only_validated_reference(tmp_path, monkeypatch):
    archive = tmp_path / 'Olivia-voice-reference-offline.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for name in ('linli-reference.wav', 'linli-reference.json', 'linli-reference.txt'):
            z.writestr(name, 'synthetic')
    def validate(root):
        reference = root / 'capabilities/video/shared/linli-reference.wav'
        assert reference.read_text() == 'synthetic'
        return reference
    monkeypatch.setattr(video, 'resolve_managed_voice_reference', validate)
    monkeypatch.setattr(video, 'resolve_managed_voice_reference_transcript', lambda root: 'synthetic')
    data = tmp_path / 'data'
    video.restore_voice_reference_supplement(data, archive)
    assert (data / 'capabilities/video/shared/linli-reference.wav').read_text() == 'synthetic'


def test_invalid_supplement_does_not_publish_files(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('../outside', 'unsafe')
    with pytest.raises(video.VideoCapabilityError):
        video.restore_voice_reference_supplement(tmp_path / 'data', archive)
    assert not (tmp_path / 'data/capabilities/video/shared/linli-reference.wav').exists()
