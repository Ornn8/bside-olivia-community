from __future__ import annotations

from pathlib import Path

from installer import start_local
from runtime import mac_keychain
from runtime.media import local_song_library
from runtime.reply import proactive_letters
from runtime.video_reply_settings import VideoReplySettingsStore
from original_client_letter_contract import _safe_local_media_url


def test_mac_keychain_reference_never_contains_the_secret(monkeypatch):
    values = {}

    class FakeKeyring:
        def set_password(self, service, name, value):
            values[(service, name)] = value

        def get_password(self, service, name):
            return values.get((service, name))

        def delete_password(self, service, name):
            del values[(service, name)]

    monkeypatch.setattr(mac_keychain, "_backend", FakeKeyring)
    reference = mac_keychain.protect("olivia-test-secret")
    assert reference.startswith("keychain-v1:")
    assert "olivia-test-secret" not in reference
    assert mac_keychain.unprotect(reference) == "olivia-test-secret"
    mac_keychain.discard(reference)
    try:
        mac_keychain.unprotect(reference)
    except ValueError as exc:
        assert str(exc) == "KEYCHAIN_SECRET_UNAVAILABLE"
    else:
        raise AssertionError("discarded secret remained available")


def test_mac_installer_loads_keychain_reference(tmp_path: Path, monkeypatch):
    path = tmp_path / "key.dpapi"
    path.write_text("keychain-v1:" + "a" * 32, encoding="utf-8")
    monkeypatch.setattr(start_local.sys, "platform", "darwin")
    monkeypatch.setattr(mac_keychain, "unprotect", lambda value: "olivia-restored" if value == path.read_text() else "")
    assert start_local._load_dpapi_key(path) == "olivia-restored"


def test_mac_installer_treats_keychain_failure_as_missing_key(tmp_path: Path, monkeypatch):
    path = tmp_path / "key.dpapi"
    path.write_text("keychain-v1:" + "b" * 32, encoding="utf-8")
    monkeypatch.setattr(start_local.sys, "platform", "darwin")

    def unavailable(_value):
        raise RuntimeError("keychain unavailable")

    monkeypatch.setattr(mac_keychain, "unprotect", unavailable)
    assert start_local._load_dpapi_key(path) == ""
    assert start_local.keychain_status() == "UNAVAILABLE"


def test_mac_proactive_contact_requires_opt_in(monkeypatch):
    monkeypatch.setenv("OLIVIA_DESKTOP_PROACTIVE_OPT_IN", "1")
    assert proactive_letters.settings({})["enabled"] is False
    assert proactive_letters.settings({"enabled": True})["enabled"] is True
    monkeypatch.delenv("OLIVIA_DESKTOP_PROACTIVE_OPT_IN")
    assert proactive_letters.settings({"enabled": False})["enabled"] is True


def test_mac_media_defaults_require_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OLIVIA_DESKTOP_MEDIA_OPT_IN", "1")
    store = VideoReplySettingsStore.initialize(tmp_path)
    assert store.routes_snapshot() == {"voice_reply": False, "singing_video": False, "voice_song_video": False}
    assert store.image_snapshot()["enabled"] is False
    monkeypatch.delenv("OLIVIA_DESKTOP_MEDIA_OPT_IN")
    assert store.routes_snapshot()["voice_reply"] is True
    assert store.image_snapshot()["enabled"] is True


def test_mac_media_url_uses_current_port_after_restart(monkeypatch):
    monkeypatch.setenv("OLIVIA_DESKTOP_DYNAMIC_PORT", "1")
    monkeypatch.setenv("OLIVIA_DESKTOP_BOUND_PORT", "61234")
    old = "http://127.0.0.1:0/toy/media/letter.wav"
    assert _safe_local_media_url(old) == "http://127.0.0.1:61234/toy/media/letter.wav"
    monkeypatch.delenv("OLIVIA_DESKTOP_DYNAMIC_PORT")
    assert _safe_local_media_url(old) == ""


def test_mac_song_reveal_uses_finder(tmp_path: Path, monkeypatch):
    song_id = "a" * 64
    library = local_song_library.LocalSongLibrary(tmp_path, {})
    library.root.mkdir(parents=True)
    library._write([{"id": song_id, "name": "Test", "media_type": "audio"}])
    media = library.root / (song_id + ".wav")
    media.write_bytes(b"RIFF")
    commands = []
    monkeypatch.setattr(local_song_library.sys, "platform", "darwin")
    monkeypatch.setattr(local_song_library.subprocess, "Popen", lambda command, **kwargs: commands.append(command))
    library.reveal(song_id)
    assert commands == [["/usr/bin/open", "-R", str(media)]]
