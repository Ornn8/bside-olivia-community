"""A finished song a contact names by its task reaches QQ as an mp3 file through the cover delivery (2026-11-05)."""
import asyncio

import pytest

from runtime import cloud_contact, remote_generation
from runtime.cloud_service import CloudError
from runtime.personal_chat import cover_chat, cover_delivery
from tests.http.test_cloud_contact_qq import TEXT, built_in_config, carry_out, contact, world  # noqa: F401
from tests.http.test_personal_chat_decision import envelope

TASK = 'babb21828c300a09f9a25cc668215781'
SONG = [{'type': 'song', 'task_id': TASK, 'brief': '什么'}]


@pytest.fixture
def song_world(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    monkeypatch.setenv('OLIVIA_GPU_API_URL', 'https://gpu.example')
    monkeypatch.setenv('OLIVIA_GPU_API_KEY', 'synthetic-key')
    monkeypatch.setattr(cloud_contact, 'SONG_WAIT_SECONDS', 5)
    env.server._local_data_root = lambda: tmp_path
    env.fetched, env.files, env.library = [], [], []

    async def file(path, name):
        env.files.append((name, path.read_bytes()))
        return 'file-ack'
    env.sender.file = file
    env.service.cover = lambda row, send: cover_delivery.deliver(env.server, row, send)

    async def download_task(self, task_id, output, *, validate=None):
        env.fetched.append(task_id)
        output.write_bytes(b'RIFFsong')
    monkeypatch.setattr(remote_generation.RemoteGeneration, 'download_task', download_task)
    monkeypatch.setattr(cover_delivery, 'to_mp3', lambda wav, mp3: mp3.write_bytes(b'ID3' + wav.read_bytes()))
    monkeypatch.setattr(cover_delivery, '_order', lambda *a: pytest.fail('a contact song is never ordered'))

    class Library:
        def __init__(self, root, environment):
            pass

        def import_audio(self, source, title):
            env.library.append((title, source.read_bytes()))
    monkeypatch.setattr('runtime.media.local_song_library.LocalSongLibrary', Library)
    return env


def test_a_contact_song_is_fetched_by_its_task_and_sent_as_an_mp3_file_then_filed(song_world):
    env = song_world
    outcome = carry_out(env, contact(media=SONG))
    assert outcome.status == 'delivered' and outcome.error_code is None
    row = env.rows[0]
    assert env.fetched == [TASK]
    assert env.sent == [('text', TEXT)]                                   # only her own words; no cover closing line
    assert env.files == [('什么（翻唱）.mp3', b'ID3RIFFsong')]
    assert row['cover_status'] == 'DELIVERED' and row['cover_finished'] is True
    assert env.library == [('什么（翻唱）', b'RIFFsong')]                   # in the music library
    assert not cover_delivery.work_folder(env.server._local_data_root(), row).exists()
    [memory] = list(cover_chat.memory_rows(row))                           # and a memory of hers
    assert memory['letter_status'] == 'COMPLETED'


def test_a_song_that_cannot_be_fetched_is_told_and_reported_with_its_code(song_world, monkeypatch):
    env = song_world

    async def missing(self, task_id, output, *, validate=None):
        raise CloudError('GPU_TASK_NOT_FOUND', 404)
    monkeypatch.setattr(remote_generation.RemoteGeneration, 'download_task', missing)
    outcome = carry_out(env, contact(media=SONG))
    row = env.rows[0]
    assert outcome.status == 'delivered' and outcome.error_code == 'GPU_TASK_NOT_FOUND'
    assert row['cover_status'] == 'FAILED' and env.files == []
    assert 'GPU_TASK_NOT_FOUND' in env.sent[-1][1]


def test_a_song_event_without_a_task_is_still_reported_unavailable(monkeypatch, tmp_path):
    env = world(monkeypatch, tmp_path, body=envelope(text=TEXT))
    assert cloud_contact.qq_song_fields([{'type': 'song'}]) == {}
    assert cloud_contact.qq_song_fields(SONG) == {'cover_request': {'task_id': TASK, 'title': '什么'}, 'cover_status': 'PENDING'}
    assert env.rows == []
