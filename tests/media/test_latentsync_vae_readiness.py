from runtime.media.music_reply import video_reply_dependency_status


def test_latentsync_readiness_requires_local_vae_closure(tmp_path):
    root = tmp_path / 'latentsync'
    for name in ('python.exe', 'scripts/inference.py', 'configs/unet/stage2_efficient.yaml', 'checkpoints/latentsync_unet.pt'):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fixture')
    env = {'OLIVIA_LATENTSYNC_ROOT': str(root), 'OLIVIA_LATENTSYNC_PYTHON': str(root / 'python.exe')}
    def state():
        result = video_reply_dependency_status(env, performance_video_path=None, probe_runtime=False)
        return next(item['state'] for item in result['dependencies'] if item['id'] == 'latentsync')
    assert state() == 'missing'
    vae = root / 'stabilityai/sd-vae-ft-mse'
    vae.mkdir(parents=True)
    (vae / 'config.json').write_text('{}')
    assert state() == 'missing'
    weights = vae / 'diffusion_pytorch_model.safetensors'
    weights.touch()
    assert state() == 'missing'
    weights.write_bytes(b'fixture')
    assert state() == 'ready'
