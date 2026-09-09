import json
import zipfile

from installer.build_media_component import build


def test_offline_builder_keeps_complete_files_and_omits_download_residue(tmp_path):
    source=tmp_path/'source'
    source.mkdir()
    (source/'ffmpeg.exe').write_bytes(b'tool')
    (source/'model.safetensors').write_bytes(b'weights')
    (source/'model.safetensors.partial').write_bytes(b'incomplete weights')
    (source/'download.tmp').write_bytes(b'temporary')
    archive=tmp_path/'tools.zip'
    build({'component':'tools','version':'test','environment':{'OLIVIA_FFMPEG_EXE':'tools/ffmpeg.exe'},
           'mounts':[{'source':str(source),'target':'tools'}]},archive)
    with zipfile.ZipFile(archive) as result:
        assert result.testzip() is None
        names=result.namelist()
        assert not any(name.endswith(('.partial','.tmp')) for name in names)
        assert result.getinfo('tools/model.safetensors').compress_type==zipfile.ZIP_STORED
        manifest=json.loads(result.read('runtime-manifest.json'))
        assert len(manifest['files'])==2
