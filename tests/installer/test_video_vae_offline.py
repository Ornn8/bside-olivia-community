from pathlib import Path
import hashlib
import zipfile
import pytest

from video_capability_install import VideoBundle, VideoCapabilityInstaller, VideoFile, VideoManifest, load_video_manifest


def test_manifest_pins_complete_vae_beside_latentsync_runtime():
    manifest = load_video_manifest(Path(__file__).resolve().parents[2] / "installer/video-capability-manifest.json")
    bundle = next(b for b in manifest.bundles if b.identifier == "ordinary_video")
    files = {item.relative_path: item for item in bundle.files}
    expected = {
        "config.json": (547, "92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e"),
        "diffusion_pytorch_model.safetensors": (334643276, "a1d993488569e928462932c8c38a0760b874d166399b14414135bd9c42df5815"),
    }
    for name, (size, digest) in expected.items():
        item = files[f"latentsync/runtime/stabilityai/sd-vae-ft-mse/{name}"]
        assert (item.size_bytes, item.sha256) == (size, digest)
        assert item.sources["official"] == f"https://huggingface.co/stabilityai/sd-vae-ft-mse/resolve/31f26fdeee1355a5c34592e401dd41e45d25a493/{name}"


@pytest.mark.parametrize("corrupt", [False, True])
def test_old_offline_zip_uses_sibling_vae_supplement_without_network(tmp_path, corrupt):
    model = b"synthetic weights"
    relative = "latentsync/runtime/stabilityai/sd-vae-ft-mse/diffusion_pytorch_model.safetensors"
    item = VideoFile("vae", relative, len(model), hashlib.sha256(model).hexdigest(), "MIT", {})
    bundle = VideoBundle("ordinary_video", "video", "MIT", False, (), (item,))
    archive = tmp_path / "Olivia-video-offline-private.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("old-package.txt", b"old")
    with zipfile.ZipFile(tmp_path / "Olivia-latentsync-vae-offline.zip", "w") as package:
        package.writestr(relative, b"x" * len(model) if corrupt else model)
    network_calls = []
    def no_network(*args, **kwargs):
        network_calls.append(1)
        raise AssertionError("offline import must not download")
    installer = VideoCapabilityInstaller(data_root=tmp_path / "data", manifest=VideoManifest("fixture", (bundle,)), opener=no_network)
    assert installer.import_offline(bundle_id="ordinary_video", offline_root=archive) == "APPLIED"
    installer._threads["ordinary_video"].join(5)
    assert installer.status()["bundles"][0]["state"] == ("failed" if corrupt else "ready")
    target = tmp_path / "data/capabilities/video/ordinary_video" / relative
    if corrupt:
        assert not target.exists()
    else:
        assert target.read_bytes() == model
    assert not network_calls


def test_added_vae_updates_existing_runtime_without_restaging(tmp_path):
    data = tmp_path / "data"
    source = tmp_path / "source"
    source.mkdir()
    (source / "base.bin").write_bytes(b"base")
    base = VideoFile("base", "base.bin", 4, hashlib.sha256(b"base").hexdigest(), "MIT", {})
    old_bundle = VideoBundle("ordinary_video", "video", "MIT", False, (), (base,))
    old = VideoCapabilityInstaller(data_root=data, manifest=VideoManifest("old", (old_bundle,)))
    old.import_offline(bundle_id="ordinary_video", offline_root=source)
    old._threads["ordinary_video"].join(5)
    installed = data / "capabilities/video/ordinary_video"
    (installed / "existing-runtime-sentinel").write_bytes(b"keep")
    relative = "latentsync/runtime/stabilityai/sd-vae-ft-mse/config.json"
    config = b"{}"
    vae = VideoFile("vae-config", relative, 2, hashlib.sha256(config).hexdigest(), "MIT", {})
    bundle = VideoBundle("ordinary_video", "video", "MIT", False, (), (base, vae))
    archive = tmp_path / "Olivia-video-offline-private.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("base.bin", b"base")
    with zipfile.ZipFile(tmp_path / "Olivia-latentsync-vae-offline.zip", "w") as package:
        package.writestr(relative, config)
    new = VideoCapabilityInstaller(data_root=data, manifest=VideoManifest("new", (bundle,)))
    new.import_offline(bundle_id="ordinary_video", offline_root=archive)
    new._threads["ordinary_video"].join(5)
    assert new.status()["bundles"][0]["state"] == "ready"
    assert (installed / relative).read_bytes() == config
    assert (installed / "existing-runtime-sentinel").read_bytes() == b"keep"
