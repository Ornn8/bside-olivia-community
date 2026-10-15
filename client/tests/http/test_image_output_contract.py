import asyncio

import pytest
from PIL import Image

from runtime import image_reply, remote_generation
from tests.http.test_image_reply_recovery import PLAN, setup


@pytest.mark.parametrize('server_planned', [False, True])
def test_native_1k_photo_above_requested_size_is_delivered(tmp_path, monkeypatch, server_planned):
    async def scenario():
        server, row, calls = setup(tmp_path, monkeypatch, failure='none')
        base = remote_generation.RemoteGeneration

        class Cloud(base):
            async def request(self, action, data):
                if action == 'capabilities':
                    return {'kinds': ['image'], 'shared_assets': [],
                            'server_media_planning': server_planned}
                task = await super().request(action, data)
                task['media_plan'] = {k: v for k, v in PLAN.items() if k != 'attach'}
                return task

            async def _download(self, task, output, *, validate=None):
                # Production accepted this native size for a 1K Sunburst request.
                Image.new('RGB', (1152, 1536), 'gray').save(output)
                validate(output)
                return task

        monkeypatch.setattr(remote_generation, 'RemoteGeneration', Cloud)
        await image_reply.prepare(server, row, 'photo', 'Okay', channel='qq')
        assert row['image_status'] == 'COMPLETED'
        assert row['image_resolution'] == '1K'
        assert 'image_error_code' not in row
        assert len(calls['submits']) == 1
        with Image.open(row['prepared_image']) as image:
            assert image.size == (1152, 1536)
    asyncio.run(scenario())


@pytest.mark.parametrize('resolution,size,format', [
    ('1K', (600, 800), 'PNG'),
    ('2K', (768, 1024), 'PNG'),
    ('4K', (1536, 2048), 'PNG'),
    ('1K', (1024, 1024), 'PNG'),
    ('1K', (3073, 4097), 'PNG'),
    ('1K', (768, 1024), 'JPEG'),
])
def test_invalid_image_output_still_rejected(tmp_path, resolution, size, format):
    output = tmp_path/'image.png'
    Image.new('RGB', size, 'gray').save(output, format=format)
    with pytest.raises(ValueError, match='IMAGE_OUTPUT_INVALID'):
        image_reply._validate_output(output, resolution)


def test_animation_is_not_a_still_photo(tmp_path):
    output = tmp_path/'image.png'
    first = Image.new('RGB', (768, 1024), 'gray')
    first.save(output, save_all=True, append_images=[Image.new('RGB', first.size, 'white')])
    with pytest.raises(ValueError, match='IMAGE_OUTPUT_INVALID'):
        image_reply._validate_output(output, '1K')
