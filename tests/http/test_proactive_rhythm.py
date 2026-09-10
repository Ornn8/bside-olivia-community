import ast
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime.private_world.life_rhythm import rhythm


@pytest.mark.parametrize('stamp,shifts,expected', [
    ('2026-09-10T05:30:00+00:00', {}, True),  # 13:30 Shanghai
    ('2026-09-10T15:30:00+00:00', {}, False), # 23:30 Shanghai
    ('2026-09-10T00:30:00+00:00', {'2026-09-09': 120}, False),
    ('2026-09-12T00:30:00+00:00', {}, False), # weekend breakfast
    ('2026-09-10T06:00:00+00:00', {}, False), # practice
])
def test_proactive_uses_current_character_rhythm(stamp, shifts, expected):
    source = Path(__file__).resolve().parents[2] / 'local_server.py'
    nodes = [n for n in ast.parse(source.read_text(encoding='utf-8')).body
             if isinstance(n, ast.FunctionDef) and n.name in {'_proactive_ready', '_current_life_rhythm'}]
    class Clock:
        @staticmethod
        def now(tz=None):
            return datetime.fromisoformat(stamp).astimezone(tz or timezone.utc)
    namespace = dict(datetime=Clock, timezone=timezone, sqlite3=sqlite3,
        _proactive_settings=lambda: {'enabled': True},
        _history_memory_admin_gate=SimpleNamespace(locked=lambda: False),
        _conversation_memory_ready_for_reply=lambda: True,
        _active_undelivered_letter=lambda: None,
        _refresh_proactive_context=lambda: {'remaining': 3, 'blocked': False, 'unread': False},
        _safe_log=lambda *_: None,
        daily_life_runtime=SimpleNamespace(store=SimpleNamespace(
            snapshot=lambda now: {'rhythm': rhythm(now, [], shifts)})))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    assert namespace['_proactive_ready']() is expected
