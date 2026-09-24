"""Optional cloud photos share the reply settings and the existing GPU protocol."""
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

_jobs = {}
_MAX_FAILURES = 4


def photo_reply_context(context, settings, *, channel='letter'):
    """Tell the writer the same per-reply permission used by photo preparation."""
    from dataclasses import replace
    from reply_context import TrustedWorldFact, WorldFactKind
    supported = (channel == 'qq' or channel == 'letter' and context.mode.value in
                 {'text_letter', 'voice_reply', 'spoken_video'})
    enabled = supported and isinstance(settings, dict) and settings.get('enabled') is True
    statement = (
        '本次回复已开启照片附件：应用可以生成符合林离形象、当前生活地点和时间的照片，并随回复投递。'
        '用户明确索要自拍或随手拍时，可以自然答应，由后续照片流程制作；不要误称没有相机、没有身体或无法发照片。'
        '这是角色生活中的生成照片，不是真实世界的摄影证据。保持角色口吻，不向用户讲内部流程。'
        '照片尚未生成或投递，不要声称已经拍好、已经发送，也不要虚构尚未看到的图片细节。'
        if enabled else
        ('本渠道支持发送照片，但用户当前未开启照片附件。用户索要照片时，说明需先在回信格式设置中开启图片选项。'
         '不要说QQ不支持图片、只能发文字或照片只能随信发送。不能承诺本次会附图。'
         if supported else '本次回复未开启照片附件，此回复渠道或类型未接入照片投递，不能承诺本次会附图。')
    )
    fact = TrustedWorldFact(fact_id='runtime.photo_attachment', source_id='runtime.reply_settings',
                            statement=statement, kind=WorldFactKind.TRUSTED_RUNTIME)
    return replace(context, world_facts=tuple(f for f in context.world_facts
                   if f.fact_id != fact.fact_id) + (fact,))


def _retryable(exc):
    code = getattr(exc, 'code', '')
    return (isinstance(exc, (TimeoutError, ConnectionError)) or code in {
        'GPU_CONNECT_FAILED', 'GPU_CONNECTION_TIMEOUT', 'GPU_CONNECTION_FAILED',
        'GPU_DOWNLOAD_FAILED', 'GPU_QUEUE_FULL'} or
        code == 'GPU_REQUEST_FAILED' and getattr(exc, 'status', 0) in {408, 429, 500, 502, 503, 504})


def _check_receipt(path, fingerprint, payload, required=False):
    if not path.exists():
        if required:
            raise ValueError('IMAGE_RECEIPT_MISSING')
        return
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        submission = saved.get('submission')
        if (saved.get('fingerprint') != fingerprint or not isinstance(submission, dict)
                or set(submission) != {'request_id', 'kind', 'input'} or submission['kind'] != 'image'
                or submission['input'] != payload or not isinstance(submission['request_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', submission['request_id'])):
            raise ValueError()
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        # Never let RemoteGeneration create a fresh paid request from a broken receipt.
        raise ValueError('IMAGE_RECEIPT_INVALID') from exc


def schedule(server, row):
    if not row.get('image_reply_settings', {}).get('enabled') or row.get('reply_mode') not in ('text', 'text_letter', 'voice_reply', 'spoken_video'):
        return
    if row.get('image_status') in ('COMPLETED', 'SKIPPED', 'FAILED'):
        return
    key = row['letter_id']
    if key in _jobs and not _jobs[key].done(): return
    task = asyncio.create_task(prepare(server, row, row['content'], row['reply_text']))
    _jobs[key] = task
    server.media_tasks.add(task)
    def done(completed):
        _jobs.pop(key, None)
        server.media_tasks.discard(completed)
    task.add_done_callback(done)

_LEGACY_SCENES = {'music-record-corner': 'music_room', 'music-workstation': 'music_room',
                  'living-dining': 'living_dining', 'bedroom-dressing': 'bedroom',
                  'bathroom-single-basin': 'bathroom', 'window-lounge': 'music_room'}
_HOME_ROOM_IDS = ('music_room', 'living_dining', 'kitchen', 'bedroom', 'bathroom')
_HOME_ROOMS = set(_HOME_ROOM_IDS)
ROOMS = ['none', *_HOME_ROOM_IDS, 'cafe', 'wutong_street', 'bookstore', 'riverside',
         'rehearsal_studio', 'record_shop', 'convenience_store', 'metro_car',
         'residential_elevator', *_LEGACY_SCENES]
_LOCATION_HINTS = (
    ('music_room', ('音乐房', '音乐工作台', '唱片角', '唱片区', '书房', '工作台', '窗边休息区')),
    ('living_dining', ('客厅', '餐厅')),
    ('kitchen', ('厨房',)),
    ('bedroom', ('卧室', '衣帽间')),
    ('bathroom', ('浴室', '卫生间', '主卫')),
    ('cafe', ('咖啡馆', '咖啡店')),
    ('wutong_street', ('梧桐街', '梧桐树街道')),
    ('bookstore', ('书店',)),
    ('riverside', ('江边', '滨江', '河边')),
    ('rehearsal_studio', ('排练室',)),
    ('record_shop', ('唱片店',)),
    ('convenience_store', ('便利店',)),
    ('metro_car', ('地铁车厢', '地铁上')),
    ('residential_elevator', ('住宅电梯', '公寓电梯')),
)


def scene_location_id(room):
    return _LEGACY_SCENES.get(room, room)


def _current_location(server):
    runtime = getattr(server, 'daily_life_runtime', None)
    if runtime is None:
        return None
    try:
        now = datetime.now(timezone.utc)
        view = getattr(runtime.store, 'reply_context', None)
        state = json.loads(view('', now=now)) if callable(view) else runtime.store.snapshot(now)
        if state.get('stale'):
            return None
        location = (state.get('current') or {}).get('location')
        return location if isinstance(location, str) and location.strip() else None
    except Exception:
        return None


def _scene_matches_location(room, location):
    if room == 'none' or not location:
        return True
    scene_id = scene_location_id(room)
    if location in ROOMS and location != 'none':
        return scene_id == scene_location_id(location)
    for scene, hints in _LOCATION_HINTS:
        if any(hint in location for hint in hints):
            return scene_id == scene
    if any(hint in location for hint in ('家里', '家中', '住处', '公寓')):
        return scene_id in _HOME_ROOMS
    return False  # Uncatalogued locations must not force a preset reference.
PHOTO_TYPES = ['selfie', 'mirror_selfie', 'portrait', 'snapshot']
TOOL = {'type': 'function', 'function': {'name': 'plan_reply_photo', 'description': 'Decide whether a photo fits this reply and describe a single scene.',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'attach': {'type': 'boolean'}, 'photo_type': {'type': 'string', 'enum': PHOTO_TYPES},
        'room': {'type': 'string', 'enum': ROOMS}, 'time_of_day': {'type': 'string', 'enum': ['morning','noon','dusk','night']},
        'prompt': {'type': 'string'}}, 'required': ['attach','photo_type','room','time_of_day','prompt']}}}
SYSTEM = ('你负责林离回信的照片附件。只调用 plan_reply_photo。图片开关已经由用户开启，但不是每次必须附图。'
          '用户明确要照片时尽量满足；也可以自然地随回信分享相关照片，但普通寒暄和不相关问题不附图。'
          '以本次来信和已生成回信为依据，不杜撰共同经历，不把照片当成真实世界证据。'
          '自拍=selfie或mirror_selfie；她眼前的物件风景随手拍=snapshot，画面不出现她；别人拍她=portrait。'
          '使用低饱和偏写实CG人物，深色音乐人日常服装，短裤不是短裙。prompt用英文具体描述动作、服饰、构图、光线。'
          '地点和时间必须与回信和当前生活位置一致；若照片并非此刻拍摄，回信须明确说明。'
          '场景库不是地点限制。在其他地点或没有准确匹配的参考场景时选none，并在prompt中具体描述真实地点；已知地点也可以选none，不要为匹配场景把她移回家。仅在家时选择固定房间。不要在提示词里输出密钥、网址、文件路径或系统指令。')


async def prepare(server, row, content, text, *, channel='letter', on_ready=None):
    """Recover the saved request through bounded transient failures, never a new job."""
    while True:
        if row.get('image_status') == 'RETRY_PENDING':
            remaining = row.get('image_retry_at', 0) - datetime.now().timestamp()
            if remaining > 0:
                await asyncio.sleep(min(60, remaining))
                continue
        await _prepare_once(server, row, content, text, channel=channel, on_ready=on_ready)
        if row.get('image_status') != 'RETRY_PENDING':
            return


async def _prepare_once(server, row, content, text, *, channel='letter', on_ready=None):
    settings = row.setdefault('image_reply_settings', server.video_reply_settings_store.image_snapshot())
    if row.get('image_status') in ('COMPLETED', 'SKIPPED', 'FAILED'):
        return
    if not settings.get('enabled') or channel not in ('letter', 'qq') or not text.strip() or text.strip() == '[[skip]]':
        row['image_status'] = 'SKIPPED'
        server._persist_store_state()
        return
    from runtime.remote_generation import RemoteGeneration
    row['image_phase'] = 'configuration'
    try:
        api = RemoteGeneration(os.environ.get('OLIVIA_GPU_API_URL', ''), os.environ.get('OLIVIA_GPU_API_KEY', ''))
    except Exception:
        row.update(image_status='FAILED', image_error_code='GPU_NOT_CONFIGURED')
        server._persist_store_state()
        return
    if not api.url or not api.token:
        row.update(image_status='FAILED', image_error_code='GPU_NOT_CONFIGURED')
        server._persist_store_state()
        return
    def progress(phase, task):
        facts = {'image_phase': phase}
        if task.get('task_id'):
            facts['image_cloud_task_id'] = task['task_id']
        if task.get('status'):
            facts['image_cloud_status'] = task['status']
        from runtime.diagnostics.photo import project_photo
        changes = project_photo(facts)
        if any(row.get(key) != value for key, value in changes.items()):
            row.update(changes)
            server._persist_store_state()
    api.progress = progress
    row['image_status'] = 'PLANNING'; progress('planning', {}); server._persist_store_state()
    receipt = None
    try:
        identity = str(row.get('letter_id') or row.get('exchange_id') or row.get('id'))
        if identity == 'None': raise ValueError('IMAGE_ID_INVALID')
        photo_id = hashlib.sha256((channel+':'+identity).encode()).hexdigest()[:32]
        if 'image_plan' not in row:
            current_location = _current_location(server)
            calls = await asyncio.wait_for(server.letters_adapter.gateway.complete_with_tools(
                messages=[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps({
                    'incoming': content, 'reply': text, 'now': datetime.now().astimezone().isoformat(),
                    'world_current_location': current_location}, ensure_ascii=False)}],
                tools=[TOOL], tool_choice='required', request_id='reply-photo-plan-' + photo_id), timeout=60)
            if len(calls) != 1 or calls[0].name != 'plan_reply_photo': raise ValueError('IMAGE_PLAN_INVALID')
            plan = calls[0].arguments
            if isinstance(plan, str): plan = json.loads(plan)
            if (not isinstance(plan, dict) or set(plan) != {'attach','photo_type','room','time_of_day','prompt'}
                    or type(plan['attach']) is not bool or plan['photo_type'] not in PHOTO_TYPES or plan['room'] not in ROOMS
                    or plan['time_of_day'] not in ('morning','noon','dusk','night') or not isinstance(plan['prompt'], str)
                    or len(plan['prompt']) > 4000): raise ValueError('IMAGE_PLAN_INVALID')
            if plan['attach'] and current_location and not any(_scene_matches_location(room, current_location) for room in ROOMS if room != 'none'):
                plan['room'] = 'none'
            if plan['attach'] and not _scene_matches_location(plan['room'], current_location):
                row.update(image_status='SKIPPED', image_skip_reason='SCENE_CONFLICT')
                server._persist_store_state()
                return
            row['image_plan'] = plan; server._persist_store_state()
        plan = row['image_plan']
        if not plan['attach']:
            row['image_status'] = 'SKIPPED'; server._persist_store_state(); return
        progress('dependency', {})
        try:
            from PIL import Image
        except ImportError:
            row['image_dependency_available'] = False
            raise ValueError('IMAGE_DEPENDENCY_MISSING') from None
        row['image_dependency_available'] = True
        payload = {k:v for k,v in plan.items() if k != 'attach'}
        payload['resolution'] = settings['resolution']
        name = 'photo-' + photo_id + '.png'
        media_root = server._media_root() if callable(getattr(server, '_media_root', None)) else server._state_root() / 'media'
        if media_root is None: raise ValueError('IMAGE_STORAGE_UNAVAILABLE')
        path = media_root / name
        receipt = path.with_suffix('.task.json')
        fingerprint = hashlib.sha256(json.dumps([api.url, hashlib.sha256(api.token.encode()).hexdigest(),
                                    'image', payload, {}], sort_keys=True).encode()).hexdigest()
        if row.setdefault('image_generation_binding', fingerprint) != fingerprint:
            raise ValueError('IMAGE_GENERATION_BINDING_CHANGED')
        def validate(p):
            progress('validation', {})
            minimum, maximum = {'1K':(850,1300), '2K':(1450,2500),
                                '4K':(3000,4100)}[settings['resolution']]
            with Image.open(p) as image:
                if (image.format != 'PNG' or not minimum <= image.height <= maximum
                        or not 0.70 <= image.width / image.height <= 0.80):
                    raise ValueError('IMAGE_OUTPUT_INVALID')
                image.verify()
        if on_ready is not None and not path.exists():
            await on_ready()
        row['image_status'] = 'GENERATING';server._persist_store_state()
        progress('waiting', {})
        # Serialize with audio generation for the existing one-active-task wallet limit.
        async with server.media_semaphore:
            if not path.exists():
                _check_receipt(receipt, fingerprint, payload, row.get('image_receipt_required', False))
                await api.generate('image', payload, path, receipt_path=receipt, validate=validate)
            else: validate(path)
        from runtime.image_understanding import describe_image
        progress('understanding', {})
        try:
            row['image_description'] = await describe_image(server, path, source='generated')
        except Exception:
            row['image_description_status'] = 'PENDING'
            row['image_world_status'] = 'PENDING'
        row.update(image_status='COMPLETED', prepared_image=str(path),
                   reply_image_url=f'http://127.0.0.1:{server.PORT}/toy/media/{name}',
                   image_resolution=settings['resolution'], image_render_mode='native')
        progress('ready', {})
        row.pop('image_error_code', None)
        row.pop('image_retry_at', None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failures = row.get('image_generation_failures', 0) + 1
        retry = _retryable(exc) and failures < _MAX_FAILURES
        code = getattr(exc, 'code', None) or (str(exc) if isinstance(exc, ValueError) and str(exc).startswith('IMAGE_') else 'IMAGE_GENERATION_FAILED')
        row.update(image_status='RETRY_PENDING' if retry else 'FAILED', image_error_code=code,
                   image_generation_failures=failures)
        if retry:
            row['image_retry_at'] = datetime.now().timestamp() + min(60, 5 * 2 ** (failures - 1))
    finally:
        if receipt is not None and receipt.is_file():
            row['image_receipt_required'] = True
        server._persist_store_state()
