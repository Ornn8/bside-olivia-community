"""Photo support facts, shared by collection and final bundle sanitization."""
import re
from collections.abc import Mapping


def project_photo(row):
    result = {}
    fields = {
        'image_status': {'planning', 'generating', 'retry_pending', 'completed', 'failed', 'skipped', 'not_requested'},
        'image_phase': {'configuration', 'planning', 'dependency', 'waiting', 'submission', 'generation', 'download', 'validation', 'understanding', 'ready'},
        'image_cloud_status': {'queued', 'running', 'succeeded', 'failed', 'cancelled'},
        'image_delivery_status': {'pending', 'sending', 'delivered', 'unknown'},
        'image_world_status': {'pending', 'completed', 'committed', 'failed'},
    }
    for field, allowed in fields.items():
        value = row.get(field)
        if isinstance(value, str) and value.lower() in allowed:
            result[field] = value.lower()
    for field, pattern in (('image_error_code', r'[A-Z][A-Z0-9_]{0,95}'),
                           ('image_cloud_task_id', r'[a-f0-9]{32}')):
        value = row.get(field)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[field] = value
    for field in ('image_receipt_required', 'image_dependency_available', 'image_available', 'reply_published'):
        if type(row.get(field)) is bool:
            result[field] = row[field]
    count = row.get('image_generation_failures')
    if type(count) is int and 0 <= count <= 100:
        result['image_generation_failures'] = count
    settings = row.get('image_reply_settings')
    resolution = row.get('image_resolution') or (settings.get('resolution') if isinstance(settings, Mapping) else None)
    if isinstance(resolution, str) and resolution in {'1K', '2K', '4K'}:
        result['image_resolution'] = resolution
    return result
