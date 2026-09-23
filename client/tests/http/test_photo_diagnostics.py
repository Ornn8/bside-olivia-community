import io
import json
import zipfile

from original_client_server import _recent_diagnostic_tasks
from runtime.diagnostics.support_bundle import build_diagnostic_bundle


def test_photo_progress_survives_export_without_private_material():
    from runtime.diagnostics.photo import project_photo
    row = dict(image_status='FAILED', image_phase='validation',
               image_error_code='IMAGE_OUTPUT_INVALID', image_generation_failures=2,
               image_cloud_task_id='a' * 32, image_cloud_status='succeeded',
               image_reply_settings={'enabled': True, 'resolution': '4K'},
               image_receipt_required=True, image_delivery_status='UNKNOWN',
               image_plan={'prompt': 'private-secret'}, prepared_image='C:/private-secret',
               reply_image_url='https://example.com/private-secret', image_description='private-secret')
    source = dict(summary={'status': 'available'}, health={'status': 'available', 'checks': {}},
                  install={'status': 'available'}, tasks={'status': 'idle', 'pending': 0, 'items': [
                      dict(status='completed', stage='image_generation', elapsed_bucket='1m_5m', **project_photo(row))]},
                  launcher_tail=[], runtime_tail=[])
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        data = archive.read('tasks.json')
    photo = json.loads(data)['items'][0]
    assert photo['image_phase'] == 'validation'
    assert photo['image_error_code'] == 'IMAGE_OUTPUT_INVALID'
    assert photo['image_cloud_task_id'] == 'a' * 32
    assert photo['image_resolution'] == '4K'
    assert photo['image_delivery_status'] == 'unknown'
    assert b'private-secret' not in data
    assert project_photo(dict(image_phase=['private-secret'], image_cloud_task_id='private-secret',
                              image_error_code='https://private-secret', image_reply_settings=None)) == {}


def test_pending_photo_is_not_evicted_by_newer_completed_letters():
    photo = dict(letter_status='COMPLETED', image_status='GENERATING', created_at=0)
    rows = [photo] + [dict(letter_status='COMPLETED', created_at=i) for i in range(1, 40)]
    assert _recent_diagnostic_tasks(rows)[0] is photo


def test_live_collector_exports_pending_photo_as_active():
    from original_client_server import _diagnostic_source
    from original_client_companion_api import CompanionCapability, CompanionReadStatus
    class Backend:
        def read_status(self):
            return CompanionReadStatus(**{name: CompanionCapability('available')
                for name in ('memory', 'private_world', 'candidates')})
        def diagnostic_status_history(self): return ()
    collect = _diagnostic_source(Backend(), setup_service=None, launcher_tail_provider=None,
        runtime_tail_provider=None, task_snapshot_provider=lambda: [dict(
            letter_id='private-secret', letter_status='COMPLETED', reply_mode='text_letter',
            image_reply_settings={'enabled': True, 'resolution': '2K'}, image_status='GENERATING',
            image_phase='download', image_cloud_status='succeeded', image_cloud_task_id='a'*32,
            reply_text='private-secret', content='private-secret')])
    source = collect()
    assert source['tasks']['pending'] == 1
    item = source['tasks']['items'][0]
    assert item['reply_published'] is False and item['text_available'] is False
    assert item['stage'] == 'image_generation'
    with zipfile.ZipFile(io.BytesIO(build_diagnostic_bundle(source))) as archive:
        raw = archive.read('tasks.json')
    assert b'private-secret' not in raw and b'download' in raw
