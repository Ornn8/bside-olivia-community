import hashlib

from video_capability_install import VideoBundle, VideoCapabilityInstaller, VideoFile, VideoManifest


def test_pending_runtime_probe_is_checking_until_result_arrives(tmp_path):
    payload=b'synthetic model'
    source=tmp_path/'offline'
    source.mkdir()
    (source/'model.bin').write_bytes(payload)
    result={'runtime_probe_pending': True}
    bundle=VideoBundle('ordinary_video','ordinary','MIT',False,(),(
        VideoFile('model','model.bin',len(payload),hashlib.sha256(payload).hexdigest(),'MIT',{}),
    ))
    installer=VideoCapabilityInstaller(data_root=tmp_path/'data',manifest=VideoManifest('fixture',(bundle,)),readiness_probe=lambda env:dict(result))
    assert installer.import_offline(bundle_id='ordinary_video',offline_root=source)=='APPLIED'
    installer._threads['ordinary_video'].join(5)
    assert not installer._threads['ordinary_video'].is_alive()
    assert installer.status()['bundles'][0]['state']=='verifying'
    result.clear()
    result.update(ordinary_missing_dependencies=[],music_ready=True)
    assert installer.status()['bundles'][0]['state']=='ready'
