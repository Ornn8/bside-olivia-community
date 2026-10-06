"""Prepare a cloud source tree. Does not restart services, install deps or deploy."""
import argparse
import hashlib
from pathlib import Path

BASELINE = '557cc7a029a634e8cd3dbb760fd45ab1acc9cab4790cb76137895cf9cee0a7c7'


def apply(root):
    target = root / 'qwen/async_relay.py'
    original = target.read_text(encoding='utf-8').encode('utf-8')
    if hashlib.sha256(original).hexdigest() != BASELINE:
        raise SystemExit('Cloud relay changed; review a fresh baseline before applying')
    source = original.decode('utf-8')
    def change(before, after):
        nonlocal source
        if source.count(before) != 1:
            raise SystemExit('Unexpected relay source')
        source = source.replace(before, after)
    change("scope['path'] != '/v1/chat/completions'", "scope['path'] not in ('/v1/chat/completions', '/v1/media/observations')")
    change("            if len(raw) > MAX_REQUEST_BODY:",
           "            if len(raw) > (6 * 1024 * 1024 if scope['path'] == '/v1/media/observations' else MAX_REQUEST_BODY):")
    change("            data, budget = validate(catalog.chat(json.loads(raw), headers.get(b'x-request-id', b'').decode('ascii')))\n            trace['policy_version'] = catalog.load()['version']\n            explicit_cache = uses_explicit_cache(data)",
           "            if scope['path'] == '/v1/media/observations':\n                from .native_media import prepare\n                data, budget = await asyncio.to_thread(prepare, json.loads(raw))\n                trace['policy_version'] = 'native-media-v1'\n                explicit_cache = False\n            else:\n                data, budget = validate(catalog.chat(json.loads(raw), headers.get(b'x-request-id', b'').decode('ascii')))\n                trace['policy_version'] = catalog.load()['version']\n                explicit_cache = uses_explicit_cache(data)")
    change("            if data.get('enable_search') or data['model'] in RESPONSE_MODELS:",
           "            if '_native_media' in data or data.get('enable_search') or data['model'] in RESPONSE_MODELS:")
    change("                        if data['model'] in RESPONSE_MODELS:",
           "                        if '_native_media' in data:\n                            from .native_media import complete as media_complete\n                            output = await media_complete(self.client, upstream_base, upstream_key, data, timeout_seconds=deadline)\n                        elif data['model'] in RESPONSE_MODELS:")
    change("                trace.update(search_count=searches, search_source_count=source_count)",
           "                if '_native_media' in data:\n                    from .native_media import observation\n                    try:\n                        output = observation(output, data)\n                    except ValueError:\n                        return await fail('media_observation_incomplete', 502)\n                trace.update(search_count=searches, search_source_count=source_count)")
    compile(source, str(target), 'exec')
    (root / 'qwen/native_media.py').write_bytes(Path(__file__).with_name('native_media.py').read_bytes())
    target.write_bytes(source.encode('utf-8'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    apply(parser.parse_args().root)
