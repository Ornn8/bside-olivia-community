"""Independently installed media closures; legacy installations remain reusable."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import threading
import uuid
import zipfile
import shutil


COMPONENTS = {
    'original': ('原创歌曲音色与曲风', '1000 步林离音色与单依纯钢琴曲风，配合 ACE 唱歌组件使用',
                 ('OLIVIA_ACE_ORIGINAL_LORA',)),
    'tools': ('媒体工具', '所有音频、视频和本地演奏导入共用', ('OLIVIA_FFMPEG_EXE',)),
    'voice': ('说话语音 · 2250', '生成说话音频，包含运行环境、模型与音色', ('OLIVIA_TTS_CONFIG',)),
    'cover': ('唱歌 · ACE', '用上传音源生成唱歌音频，包含模型、运行环境与音色',
              ('OLIVIA_ACE_ROOT', 'OLIVIA_ACE_PYTHON', 'OLIVIA_ACE_VOICE_LORA', 'OLIVIA_ACE_REFERENCE')),
    'lipsync': ('口型视频', '把说话或唱歌音频制作成口型视频', ('OLIVIA_LATENTSYNC_ROOT', 'OLIVIA_LATENTSYNC_PYTHON')),
    'scenes': ('视频场景', '说话、演奏与转场素材，视频模式共用',
               ('OLIVIA_ORDINARY_ACTION_BASE', 'OLIVIA_MUSIC_PERFORMANCE_BASE', 'OLIVIA_OFFICIAL_REPLY_REFERENCE')),
    'separator': ('唱歌视频人声分离', '仅唱歌视频需要，纯音频无需安装',
                  ('OLIVIA_ROFORMER_PYTHON', 'OLIVIA_ROFORMER_MODEL_PATH', 'OLIVIA_ROFORMER_CONFIG_PATH')),
    'transcription': ('歌词识别（可选）', '自动识别上传音源的歌词；手填歌词无需安装', ('OLIVIA_ACE_ASR_MODEL',)),
}
_LOCK = threading.RLock()


def requirements_for(mode: str, video: bool) -> list[str]:
    if mode not in {'voice_reply', 'singing_video', 'voice_song_video'} or type(video) is not bool:
        raise ValueError('MEDIA_COMPONENT_MODE_INVALID')
    result = ['tools']
    if mode != 'singing_video':
        result.append('voice')
    if mode != 'voice_reply':
        result.append('cover')
    if mode == 'voice_song_video':
        result.append('original')
    if video:
        result += ['lipsync', 'scenes']
        if mode == 'singing_video':
            result.append('separator')
    return result


def _component(manifest):
    version = manifest.get('version', '')
    parts = version.split('.') if isinstance(version, str) else []
    if len(parts) < 3 or parts[0] != 'component' or parts[1] not in COMPONENTS:
        raise ValueError('MEDIA_COMPONENT_PACKAGE_INVALID')
    component = parts[1]
    if set(manifest.get('environment', {})) != set(COMPONENTS[component][2]):
        raise ValueError('MEDIA_COMPONENT_PACKAGE_INVALID')
    return component


def archive_component(archive: Path) -> str | None:
    with zipfile.ZipFile(archive) as z:
        if 'runtime-manifest.json' not in z.namelist():
            return None
        if z.getinfo('runtime-manifest.json').file_size > 32 * 1024 * 1024:
            raise ValueError('MEDIA_COMPONENT_PACKAGE_INVALID')
        manifest = json.loads(z.read('runtime-manifest.json'))
    if not str(manifest.get('version', '')).startswith('component.'):
        return None
    return _component(manifest)


class MediaComponents:
    def __init__(self, data_root: Path):
        self.root = data_root.resolve() / 'capabilities' / 'media-components'
        self.progress = {'state': 'idle', 'checked_bytes': 0, 'total_bytes': 0}
        self._thread = None
        self._start_lock = threading.Lock()

    def _registry(self):
        try:
            result = json.loads((self.root / 'installed.json').read_text(encoding='utf-8'))
            if not isinstance(result, dict) or set(result) - set(COMPONENTS):
                raise ValueError('MEDIA_COMPONENT_REGISTRY_INVALID')
            return result
        except FileNotFoundError:
            return {}

    def environment(self):
        result = {}
        for component, receipt in self._registry().items():
            try:
                result.update(self._component_environment(component, receipt))
            except (OSError, ValueError, KeyError, TypeError):
                # A missing optional component must not disable other installed modes.
                continue
        return result

    def _component_environment(self, component, receipt, *, verify=False):
        from video_capability_install import _inside, _load_runtime_root_manifest
        root = _inside(self.root, self.root / receipt['directory'])
        manifest = json.loads((root / 'runtime-manifest.json').read_text(encoding='utf-8'))
        if _component(manifest) != component:
            raise ValueError('MEDIA_COMPONENT_REGISTRY_INVALID')
        env = _load_runtime_root_manifest(root, receipt['manifest_sha256'], verify_files=verify)
        if component == 'voice':
            env['OLIVIA_TTS_CONFIG'] = str(self._voice_config(root, Path(env['OLIVIA_TTS_CONFIG'])))
        return env

    def _voice_config(self, root, template):
        from video_capability_install import _inside, _safe_relative
        config = json.loads(template.read_text(encoding='utf-8'))
        settings = config['settings']
        options = settings['provider_options']
        for container, keys in ((settings, ('model_dir', 'reference_audio', 'runtime_root')),
                                (options, ('external_python', 'model_license_path', 'adapter_dir'))):
            for key in keys:
                candidate = _inside(root, root / _safe_relative(container[key]))
                if not candidate.exists():
                    raise ValueError('MEDIA_COMPONENT_VOICE_CONFIG_INVALID')
                container[key] = str(candidate)
        destination = self.root / ('tts-' + root.name + '.json')
        content = json.dumps(config, ensure_ascii=False, sort_keys=True)
        if not destination.exists() or destination.read_text(encoding='utf-8') != content:
            temporary = destination.with_suffix('.tmp')
            temporary.write_text(content, encoding='utf-8')
            os.replace(temporary, destination)
        return destination

    def status(self, existing=None):
        environment = dict(existing or {})
        try:
            managed = self.environment()
        except (OSError, ValueError, KeyError, TypeError):
            managed = {}
        environment.update(managed)
        items = []
        for key, (label, description, keys) in COMPONENTS.items():
            installed = all(environment.get(k) and Path(environment[k]).exists() for k in keys)
            items.append({'id': key, 'label': label, 'description': description,
                          'state': 'installed' if installed else 'missing',
                          'source': ('component' if all(k in managed for k in keys) else 'existing') if installed else None})
        progress = dict(self.progress)
        if self._thread and self._thread.is_alive() and progress['state'] in {'ready', 'failed'}:
            progress['state'] = 'queued'
        return {'items': items, 'progress': progress, 'verification': 'files_only'}

    def start(self, archive: Path, expected_component: str | None = None):
        return self.start_many([archive], None if expected_component is None else [expected_component])

    def start_many(self, archives, expected_components=None):
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return 'REJECTED'
            if not 1 <= len(archives) <= len(COMPONENTS):
                raise ValueError('MEDIA_COMPONENT_PACKAGE_MISMATCH')
            queue = [(archive, archive_component(archive)) for archive in archives]
            identifiers = [component for _, component in queue]
            if None in identifiers or len(set(identifiers)) != len(identifiers) or (
                expected_components is not None and not set(identifiers) <= set(expected_components)
            ):
                raise ValueError('MEDIA_COMPONENT_PACKAGE_MISMATCH')
            self.progress = {'state': 'queued', 'component': identifiers[0], 'checked_bytes': 0, 'total_bytes': 0,
                             'completed_count': 0, 'package_count': len(queue), 'failed_components': []}
            def run():
                failed = []
                for index, (archive, component) in enumerate(queue):
                    try:
                        self.install(archive, expected_component=component)
                    except Exception as exc:
                        code = str(exc)
                        if not re.fullmatch(r'(?:MEDIA_COMPONENT|VIDEO_RUNTIME|VIDEO_ARCHIVE)_[A-Z_]{1,64}', code):
                            code = 'MEDIA_COMPONENT_INSTALL_FAILED'
                        failed.append(component)
                        self.progress.update(reason_code=code, failed_components=list(failed))
                    self.progress.update(completed_count=index + 1, state='queued')
                self.progress.update(state='failed' if failed else 'ready')
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            return 'APPLIED'

    def install(self, archive: Path, *, expected_component: str):
        from video_capability_install import (
            _extract_runtime_zip_safely, _load_runtime_root_manifest, _sha256_file,
            _inside, _reject_reparse_tree, _portable_python_runtime,
        )
        with _LOCK:
            component = archive_component(archive)
            if component != expected_component:
                raise ValueError('MEDIA_COMPONENT_PACKAGE_MISMATCH')
            self.root.mkdir(parents=True, exist_ok=True)
            _reject_reparse_tree(self.root)
            self.progress.update(state='checking', component=component, checked_bytes=0, total_bytes=archive.stat().st_size)
            archive_digest = _sha256_file(archive, progress=lambda count: self.progress.update(checked_bytes=count))[1]
            registry = self._registry()
            previous = registry.get(component)
            if previous and previous.get('archive_sha256') == archive_digest:
                try:
                    self._component_environment(component, previous, verify=True)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                else:
                    self.progress.update(state='ready')
                    return 'NOOP'
            # Immutable generations keep an existing installation intact on any failure.
            destination = _inside(self.root, self.root / (component + '-' + uuid.uuid4().hex))
            if destination.exists():
                raise ValueError('MEDIA_COMPONENT_DESTINATION_EXISTS')
            def progress(done, total):
                self.progress.update(checked_bytes=done, total_bytes=total)
            temporary = self.root / ('installed-' + uuid.uuid4().hex + '.tmp')
            try:
                self.progress.update(state='extracting')
                _extract_runtime_zip_safely(archive, destination, progress=progress)
                digest = _sha256_file(destination / 'runtime-manifest.json')[1]
                self.progress.update(state='checking')
                env = _load_runtime_root_manifest(destination, digest, verify_files=True, progress=progress)
                self.progress.update(state='testing')
                for key, value in env.items():
                    if key.endswith('_PYTHON') and not _portable_python_runtime(Path(value), destination):
                        raise ValueError('MEDIA_COMPONENT_RUNTIME_NOT_PORTABLE')
                if component == 'voice':
                    config = json.loads(self._voice_config(destination, Path(env['OLIVIA_TTS_CONFIG'])).read_text(encoding='utf-8'))
                    if not _portable_python_runtime(Path(config['settings']['provider_options']['external_python']), destination):
                        raise ValueError('MEDIA_COMPONENT_RUNTIME_NOT_PORTABLE')
                registry[component] = {'directory': destination.name, 'manifest_sha256': digest,
                                       'archive_sha256': archive_digest}
                temporary.write_text(json.dumps(registry, sort_keys=True), encoding='utf-8')
                os.replace(temporary, self.root / 'installed.json')
            except Exception:
                # Only this attempt's unpublished generation belongs to this cleanup.
                try:
                    _reject_reparse_tree(self.root)
                    _inside(self.root, destination)
                    if destination.exists():
                        shutil.rmtree(destination)
                    temporary.unlink(missing_ok=True)
                except (OSError, ValueError):
                    raise ValueError('MEDIA_COMPONENT_CLEANUP_FAILED') from None
                raise
            self.progress.update(state='ready')
            return 'APPLIED'
