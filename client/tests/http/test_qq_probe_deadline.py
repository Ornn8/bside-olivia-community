import asyncio
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from runtime.personal_chat import setup


@pytest.mark.parametrize('probe', [setup._qq_probe, setup._qq_online])
def test_probe_deadline_includes_websocket_receive(monkeypatch, probe):
    timeout = asyncio.timeout
    monkeypatch.setattr(setup.asyncio, 'timeout', lambda seconds: timeout(.01))

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_json(self, value):
            pass

        async def receive(self):
            await asyncio.sleep(.02)
            return SimpleNamespace(type=setup.aiohttp.WSMsgType.TEXT, data='{}')

    class Session(Socket):
        def ws_connect(self, *args, **kwargs):
            return Socket()

    monkeypatch.setattr(setup.aiohttp, 'ClientSession', lambda **kwargs: Session())
    with pytest.raises(TimeoutError):
        asyncio.run(probe('ws://127.0.0.1:3001', 'synthetic-token'))


@pytest.mark.parametrize('retcode', [None, False, 1])
def test_status_requires_successful_action_before_reporting_offline(monkeypatch, retcode):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            return self

        async def send_json(self, value):
            pass

        async def receive(self):
            return SimpleNamespace(type=setup.aiohttp.WSMsgType.TEXT, data=json.dumps({
                'echo': 'olivia-status', 'status': 'ok', 'retcode': retcode, 'data': {'online': False}}))

    monkeypatch.setattr(setup.aiohttp, 'ClientSession', lambda **kwargs: Session())
    with pytest.raises(RuntimeError, match='QQ_STATUS_UNAVAILABLE'):
        asyncio.run(setup._qq_online('ws://127.0.0.1:3001', 'synthetic-token'))


def test_napcat_transition_survives_bundle_projection_without_private_fields():
    from runtime.diagnostics.support_bundle import _project_tail_record
    records = []
    server = SimpleNamespace(_safe_log=lambda event, **fields: records.append(dict(event=event, **fields)))
    setup._napcat_diagnostic(server, status='AWAITING_QQ_LOGIN', reason='probe')
    record = _project_tail_record({**records[0], 'token': 'private-token', 'account': 'private-id'}, runtime=True)
    assert record['channel'] == 'qq'
    assert record['status'] == 'awaiting_qq_login'
    assert record['reason'] == 'probe'
    assert 'private' not in json.dumps(record)
    server._safe_log = lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
    setup._napcat_diagnostic(server, status='STARTING', reason='confirmed_offline_restart')


@pytest.mark.parametrize('answers,restarts', [
    ([False, TimeoutError(), False], 0), ([False, 'probe_gap', False], 0), ([False, False], 1),
])
def test_watchdog_requires_consecutive_confirmed_offline_answers(monkeypatch, answers, restarts):
    from runtime.personal_chat import napcat_installer
    monkeypatch.setattr(setup, '_read_config', lambda server: {'qq': {'managed': True}})
    monkeypatch.setattr(setup, '_NAPCAT_WATCHDOG_SECONDS', .001)
    monkeypatch.setattr(setup, '_NAPCAT_ONLINE_CHECK_SECONDS', 0)
    process = SimpleNamespace(poll=lambda: None)
    stopped, records = [], []
    remaining = list(answers)
    monkeypatch.setattr(napcat_installer, 'ensure_shell', lambda root: process)
    monkeypatch.setattr(napcat_installer, 'stop_shell', lambda root, child: stopped.append(child))
    monkeypatch.setattr(napcat_installer, 'onebot_available', lambda: True)
    monkeypatch.setattr(napcat_installer, 'managed_connection', lambda root: ('ws://127.0.0.1:3001', 'synthetic-token'))
    monkeypatch.setattr(napcat_installer, 'account_config_ready', lambda *args: True)
    monkeypatch.setattr(napcat_installer, 'remember_account', lambda *args: None)

    async def probe(*args):
        if remaining and remaining[0] == 'probe_gap':
            remaining.pop(0)
            raise RuntimeError('QQ_LOGIN_UNAVAILABLE')
        return '123456'

    monkeypatch.setattr(setup, '_qq_probe', probe)

    async def scenario():
        checked = asyncio.Event()

        async def online(*args):
            if not remaining:
                checked.set()
                await asyncio.Event().wait()
            answer = remaining.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(setup, '_qq_online', online)
        server = SimpleNamespace(_state_root=lambda: Path(__file__).resolve().parent,
                                 _safe_log=lambda event, **fields: records.append(fields))
        app = setup.web.Application()
        setup.install_setup_routes(app, server)
        app.freeze()
        await app.startup()
        try:
            await asyncio.wait_for(checked.wait(), 2)
            assert len(stopped) == restarts
            assert sum(r['reason'] == 'confirmed_offline_restart' for r in records) == restarts
        finally:
            await app.cleanup()

    asyncio.run(scenario())
