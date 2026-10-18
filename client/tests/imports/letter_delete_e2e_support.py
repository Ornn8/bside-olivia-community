"""Shared pieces of the letter deletion end-to-end tests. Synthetic data only.

``Backend`` starts the real local backend (``letter_delete_e2e_backend.py``) on free loopback ports with a fresh data
directory. ``Api`` calls it over HTTP exactly as the letters-and-memories window does. ``SYNTHETIC`` is the made-up
conversation the tests seed, and ``comparable`` / ``where`` / ``counts`` turn the stores read back from the backend
into the normalized rows the assertions and the evidence file use.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).with_name('letter_delete_e2e_backend.py')

# Five letters from the user, each answered. Quotes in `life` and `relationship` are exact pieces of the texts,
# as the product's validators require. The garden project is touched by two different letters on purpose: a
# record with more than one source must survive the deletion of one of them. `markers` are pieces of text that
# appear only in that letter's own side (the quotes the derived records hold are among them).
SYNTHETIC = [
    {'key': 'ice',
     'user': '我最喜欢开心果味的冰淇淋。上周在河边的小店吃过一次，绿色的，像春天刚冒出来的叶子。',
     'reply': '开心果味的，听起来很清爽。下次你再去，替我也尝一口吧。',
     'life': [],
     'relationship': {'signals': {'rel_disclosure': 'personal'}, 'moment': 'rel_disclosure:personal',
                      'user_quote': '我最喜欢开心果味的冰淇淋', 'reply_quote': '开心果味的，听起来很清爽'},
     'markers': {'user': ['开心果味的冰淇淋', '像春天刚冒出来的叶子'], 'reply': ['听起来很清爽', '替我也尝一口']}},
    {'key': 'garden-plan',
     'user': '周末我想去河边散步，也想和你一起种一盆白色的花。',
     'reply': '好呀，周末我们一起把白色的花种下去，我来挑花盆。',
     'life': [{'id': 'garden', 'title': '一起种白色的花', 'detail': '约好周末种一盆白色的花', 'status': 'planned',
               'kind': 'shared', 'actor': 'user', 'quote': '想和你一起种一盆白色的花'}],
     'relationship': {'signals': {'rel_shared': 'plan'}, 'moment': 'rel_shared:plan',
                      'user_quote': '想和你一起种一盆白色的花', 'reply_quote': '周末我们一起把白色的花种下去'},
     'markers': {'user': ['想去河边散步', '一起种一盆白色的花'], 'reply': ['我来挑花盆', '把白色的花种下去']}},
    {'key': 'garden-done',
     'user': '白色的花已经种好了，我今天给它浇了水。',
     'reply': '太好了，种好的花要记得多晒太阳。',
     'life': [{'id': 'garden', 'title': '一起种白色的花', 'detail': '花已经种好并浇了水', 'status': 'ongoing',
               'kind': 'shared', 'actor': 'user', 'quote': '白色的花已经种好了'}],
     'relationship': {'signals': {'rel_shared': 'kept'}, 'moment': 'rel_shared:kept',
                      'user_quote': '白色的花已经种好了', 'reply_quote': '种好的花要记得多晒太阳'},
     'markers': {'user': ['白色的花已经种好了', '给它浇了水'], 'reply': ['种好的花要记得多晒太阳', '太好了，种好的花']}},
    {'key': 'piano',
     'user': '最近在练一首只有十六小节的短曲子，有点累。',
     'reply': '慢慢练就好，累了就停下来休息一下。',
     'life': [],
     'candidate': {'type': 'boundary_respected', 'summary': '用户说累了，她让用户先休息'},
     'relationship': {'signals': {'rel_care': 'her_to_user'}, 'moment': 'rel_care:her_to_user',
                      'user_quote': '有点累', 'reply_quote': '累了就停下来休息一下'},
     'markers': {'user': ['十六小节的短曲子', '有点累'], 'reply': ['累了就停下来休息', '慢慢练就好']}},
    {'key': 'rain',
     'user': '傍晚常常坐在窗边听雨，心里会安静下来。',
     'reply': '听雨的时候最安静了，我也喜欢雨声。',
     'life': [],
     'relationship': {'signals': {'rel_affection': 'warm'}, 'moment': 'rel_affection:warm',
                      'user_quote': '心里会安静下来', 'reply_quote': '我也喜欢雨声'},
     'markers': {'user': ['坐在窗边听雨', '心里会安静下来'], 'reply': ['听雨的时候最安静', '我也喜欢雨声']}},
]
BY_KEY = {e['key']: e for e in SYNTHETIC}


def script_for(exchanges=SYNTHETIC) -> dict:
    return {'exchanges': {e['user']: {k: e[k] for k in ('reply', 'life', 'relationship', 'candidate') if k in e}
                          for e in exchanges}}


def mem0_site() -> str | None:
    """Where the Mem0 and Qdrant packages come from: this interpreter, or a folder named by the environment."""
    extra = os.environ.get('OLIVIA_E2E_MEM0_SITE', '')
    if extra and Path(extra).is_dir():
        return extra
    if importlib.util.find_spec('mem0') and importlib.util.find_spec('qdrant_client'):
        return ''
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Backend:
    """The real local backend in its own process."""

    def __init__(self, root: Path, script: dict, site: str):
        self.root = Path(root)
        self.site = site
        self.port, self.jev_port = free_port(), free_port()
        self.root.mkdir(parents=True, exist_ok=True)
        self.script_path = self.root / 'script.json'
        self.script_path.write_text(json.dumps(script, ensure_ascii=False), encoding='utf-8')
        self._launch()

    def _launch(self) -> None:
        command = [sys.executable, '-I', str(BACKEND), '--root', str(self.root), '--port', str(self.port),
                   '--jev-port', str(self.jev_port), '--script', str(self.script_path)]
        if self.site:
            command += ['--mem0-site', self.site]
        self.log = open(self.root / 'backend.log', 'ab')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('OLIVIA_') and 'proxy' not in key.lower()}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        self.process = subprocess.Popen(command, cwd=CLIENT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.api = Api(f'http://127.0.0.1:{self.port}')

    def wait_ready(self, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('backend exited: ' + (self.root / 'backend.log').read_text(errors='replace')[-2000:])
            try:
                last = self.api.get('/__e2e__/memory_ready')
                if last.get('ready'):
                    return
            except (OSError, urllib.error.URLError, ValueError):
                pass
            time.sleep(0.5)
        raise TimeoutError(f'backend not ready: {last}')

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.log.close()

    def restart(self) -> None:
        """Stop the process and start it again on the same data directory, as closing and reopening the app does."""
        self.stop()
        self.port, self.jev_port = free_port(), free_port()
        self._launch()
        self.wait_ready()

    @property
    def blocked_connections(self) -> list[str]:
        """Names the process tried to reach outside this computer. The backend refuses every one of them."""
        path = self.root / 'network-blocked.log'
        return sorted(set(path.read_text(encoding='utf-8').split())) if path.exists() else []


class Api:
    """The local service as the window sees it. Every call is a real HTTP request."""

    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body: dict | None = None, *, confirmed: bool = False,
                timeout: float = 180.0) -> tuple[int, dict]:
        headers = {'Origin': self.base, 'Content-Type': 'application/json', 'Accept': 'application/json'}
        if confirmed:
            headers['X-Olivia-Companion-Action'] = 'confirmed'
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode('utf-8'))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b'{}')

    def get(self, path: str) -> dict:
        return self.request('GET', path)[1]

    # --- the mailbox -------------------------------------------------------------------------------------
    def send(self, content: str) -> str:
        status, body = self.request('POST', '/toy/letter/send', {'content': content, 'material': {}})
        assert status == 200 and body['code'] == 0, body
        return body['data']['letter_id']

    def letters(self) -> list[dict]:
        return self.get('/toy/letter/list')['data']['list']

    def detail(self, letter_id: str) -> dict:
        return self.get('/toy/letter/detail?' + urllib.parse.urlencode({'letter_id': letter_id}))['data']

    def delete(self, letter_id: str, part: str, *, with_diary: bool = False) -> dict:
        payload = {'letter_id': letter_id, 'part': part, **({'with_diary': True} if with_diary else {})}
        status, body = self.request('POST', '/toy/letter/deletion/delete', payload, confirmed=True)
        body['http'] = status
        return body

    def diary_offer(self, letter_ids: list[str]) -> dict:
        """What the confirmation asks before it offers to delete the diary: the days and months that have an entry."""
        return self.request('POST', '/toy/letter/deletion/diary', {'letter_ids': letter_ids}, confirmed=True)[1]['data']

    def trash(self) -> list[dict]:
        return self.request('POST', '/toy/letter/deletion/list', {}, confirmed=True)[1]['data']['items']

    def restore(self, trash_id: str) -> dict:
        status, body = self.request('POST', '/toy/letter/deletion/restore', {'id': trash_id}, confirmed=True)
        body['http'] = status
        return body

    # --- the memory browser -----------------------------------------------------------------------------
    def memories(self, query: str = '') -> list[dict]:
        params = {'browse': 1, 'collection': 'memories', 'limit': 100, **({'query': query} if query else {})}
        return self.get('/toy/companion/memory?' + urllib.parse.urlencode(params))['memories']

    def originals(self, query: str = '', source_id: str | None = None) -> list[dict]:
        params = {'browse': 1, 'collection': 'originals', 'limit': 100,
                  **({'query': query} if query else {}), **({'source_id': source_id, 'full': 1} if source_id else {})}
        return self.get('/toy/companion/memory?' + urllib.parse.urlencode(params))['originals']

    # --- the harness -------------------------------------------------------------------------------------
    def settle(self) -> dict:
        result = self.request('POST', '/__e2e__/settle')[1]
        assert result.get('settled'), result
        return result

    def snapshot(self) -> dict:
        return self.get('/__e2e__/snapshot')

    def relationship(self) -> dict:
        return self.request('POST', '/__e2e__/relationship_tick')[1]

    def reindex(self) -> dict:
        return self.request('POST', '/__e2e__/reindex')[1]

    def diary_tick(self, days_ahead: int = 2) -> dict:
        """Run the diary writer as it runs every few minutes, as of `days_ahead` days from now: the entries then."""
        return self.request('POST', '/__e2e__/diary_tick', {'days_ahead': days_ahead})[1]['entries']

    def diary(self, add: bool, text: str = '') -> dict:
        """Write (or clear) her diary for today, the month's memoir and a comment; `text` is quoted in the entries."""
        return self.request('POST', '/__e2e__/diary_add' if add else '/__e2e__/diary_clear', {'text': text})[1]


# Two old letters of the kind the app imports from a backup: only the two texts of each pair.
OLD_LETTERS = [
    {'user': '我很喜欢在夜里听老唱片，黑胶的声音很温暖。', 'reply': '黑胶的声音确实有温度，你最近在听哪一张？'},
    {'user': '我周末准备去爬山，想拍一张日出的照片。', 'reply': '日出的照片一定很好看，记得带上外套。'},
]


def import_old_letters(backend: Backend, *, judge: bool) -> dict[str, str]:
    """Import the old letters the way the window does (archive and original text), and let the relationship
    assessment of the imported batch run when ``judge`` is set. Returns the letter ids by the first words of the user's text."""
    official = backend.root / 'official'
    official.mkdir(exist_ok=True)
    (official / 'letter_pairs.json').write_text(
        json.dumps([{'content': p['user'], 'reply': p['reply']} for p in OLD_LETTERS], ensure_ascii=False), encoding='utf-8')
    (backend.root / '.olivia-full-patch.json').write_text(json.dumps({'official_source': str(official)}), encoding='utf-8')
    if judge:
        (backend.root / 'history-judge').write_text('on', encoding='utf-8')
    api = backend.api
    status, body = api.request('POST', '/toy/letter/legacy/local-import', {'background': True, 'originals_only': True},
                               confirmed=True)
    assert status == 200, body
    for _ in range(120):
        time.sleep(0.5)
        state = api.get('/toy/letter/legacy/local-import?progress=1')['data']
        if state.get('status') in ('APPLIED', 'FAILED'):
            break
    assert state['status'] == 'APPLIED', state
    wait_for_history_batches(backend, 'done' if judge else 'pending')
    api.settle()
    letters = {row['summary'][:6]: row['letter_id'] for row in api.letters()}
    assert len(letters) == len(OLD_LETTERS), letters
    return letters


def wait_for_history_batches(backend: Backend, state: str) -> None:
    import sqlite3
    path = backend.root / 'data' / 'history-relationship.sqlite3'
    for _ in range(120):
        if path.exists():
            with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True) as db:
                rows = db.execute('SELECT state FROM batches').fetchall()
            if rows and all(row[0] == state for row in rows):
                return
        time.sleep(0.5)
    raise TimeoutError(f'the imported batches did not reach {state}')


def seed(api: Api, exchanges=SYNTHETIC, *, relationship: bool = True) -> dict[str, str]:
    """Send the letters one after the other, as a person would; let every background writer finish."""
    ids = {}
    for exchange in exchanges:
        ids[exchange['key']] = api.send(exchange['user'])
        api.settle()
    if relationship:
        assert api.relationship()['result'] == 'updated'
        api.settle()
    api.reindex()  # the semantic vectors of the originals, which the app builds in the background
    api.settle()
    return ids


# --------------------------------------------------------------------------------------------------------------
# Normalized views of a snapshot
# --------------------------------------------------------------------------------------------------------------
# Timers and static catalogues are left out of the comparison; everything else must come back exactly.
_NOT_COMPARED = {
    'originals': {'archive_scan', 'archive_targets', 'vector_models'},
    'life': {'life_settings', 'life_weather', 'life_routine_days', 'daily_life_refresh_retry',
             'character_development_topics'},
    'journal': {'canonical_memory_deliveries', 'canonical_memory_failure_budget', 'canonical_memory_retry_limit'},
    'diary': {'diary_attempts', 'diary_settings'},  # what the diary writer notes about the days it found empty
}
_VOLATILE = {'updated_at'}


def comparable(snapshot: dict) -> dict:
    """The rows that must come back exactly after a restore."""
    view = {}
    for store, database in snapshot['databases'].items():
        for table, rows in database.items():
            if table not in _NOT_COMPARED.get(store, ()):
                view[f'{store}.{table}'] = rows
    view['points'] = [{'id': p['id'], 'payload': {k: v for k, v in p['payload'].items() if k not in _VOLATILE}}
                      for p in snapshot['points']]
    view['letters'] = sorted(({**row, '_deleted_parts': row.get('_deleted_parts') or []} for row in snapshot['letters']),
                             key=lambda r: r['letter_id'])
    return {key: value for key, value in view.items() if value}


def differences(before: dict, after: dict) -> dict[str, dict[str, int]]:
    """Per table: how many normalized rows are gone and how many are new."""
    result = {}
    for key in sorted(set(before) | set(after)):
        old = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in before.get(key, [])}
        new = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in after.get(key, [])}
        if old != new:
            result[key] = {'removed': len(old - new), 'added': len(new - old)}
    return result


def where(snapshot: dict, needle: str) -> list[str]:
    """Every place in the stores that still holds this text (the letter trash is not a store: it is the backup)."""
    found = []
    for store, database in snapshot['databases'].items():
        for table, rows in database.items():
            if any(needle in json.dumps(r, ensure_ascii=False) for r in rows):
                found.append(f'{store}.{table}')
    if any(needle in json.dumps(p, ensure_ascii=False) for p in snapshot['points']):
        found.append('memory points')
    if any(needle in text for text in snapshot['letter_rows_text']):
        found.append('letter rows')
    if needle in snapshot['state_text']:
        found.append('saved mailbox state')
    return found


def relationship_levels(snapshot: dict) -> dict:
    """The current relationship numbers: the newest stored snapshot of the ledger."""
    rows = snapshot['databases']['world'].get('private_world_snapshots')
    if not rows:
        return {}
    state = json.loads(max(rows, key=lambda r: r['version'])['payload_json'])
    return {key: state.get(key) for key in ('familiarity', 'trust', 'comfort', 'closeness', 'tension',
                                            'relationship_stage', 'relationship_growth')}


def counts(snapshot: dict) -> dict:
    """A short summary per store, for the evidence file and the report."""
    originals = snapshot['databases']['originals']
    world = snapshot['databases']['world']
    life = snapshot['databases']['life']
    events = world.get('private_world_events', [])
    return {
        'letters': {'rows': len(snapshot['letters']),
                    'user_texts': sum(1 for r in snapshot['letters'] if r.get('content')),
                    'reply_texts': sum(1 for r in snapshot['letters'] if r.get('reply_text'))},
        'originals': {'user': sum(1 for r in originals.get('originals', []) if r['actor'] == 'user'),
                      'linli': sum(1 for r in originals.get('originals', []) if r['actor'] == 'linli'),
                      'search_chunks': len(originals.get('chunks', [])),
                      'vectors': len(originals.get('source_vectors', [])),
                      'relationship_quotes': len(originals.get('relationship_quotes', []))},
        'memories': {'points': len(snapshot['points']),
                     'history_rows': len(snapshot['databases']['history'].get('history', []))},
        'life': {'exchange_records': sum(1 for r in life.get('life_moments', []) if r.get('kind') == 'exchange'),
                 'shared_projects': {r['id']: json.loads(r['payload'])['status'] for r in life.get('life_projects', [])
                                     if json.loads(r['payload']).get('kind') == 'shared'}},
        'relationship': {'evidence_events': sum(1 for e in events if e['event_type'] == 'relationship_evidence'),
                         'removed_markers': sum(1 for e in events if e['event_type'] == 'letter_source_removed'),
                         'moments': len(world.get('relationship_moments', [])),
                         'candidates': len(world.get('private_world_candidates', [])),
                         'levels': relationship_levels(snapshot)},
        'diary': {'entries': len(snapshot['databases']['diary'].get('diary_entries', [])),
                  'comments': len(snapshot['databases']['diary'].get('diary_comments', [])),
                  'days_to_write_again': len(snapshot['databases']['diary'].get('diary_rewrite', []))},
        'trash_items': len(snapshot['trash']),
    }
