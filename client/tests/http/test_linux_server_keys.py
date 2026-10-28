"""The client on a Linux server.

There is no DPAPI on Linux: a secret is kept in the data folder, which only the service's user can read.
"""
import os

import pytest

import original_client_setup_api as setup_api
from installer import start_local


def test_keys_round_trip_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'name', 'posix')
    protected = setup_api._dpapi_protect('olivia-test-key-value')
    assert protected.startswith('posix-v1:') and 'olivia-test-key-value' not in protected
    assert setup_api._dpapi_unprotect(protected) == 'olivia-test-key-value'
    assert setup_api._dpapi_unprotect(protected + '\n') == 'olivia-test-key-value'
    # A file protected by Windows cannot be read here, and broken text is no key.
    with pytest.raises(setup_api.LLMSetupError, match='LLM_SETUP_DPAPI_UNAVAILABLE'):
        setup_api._dpapi_unprotect('dpapi-v1:AAAA')
    with pytest.raises(setup_api.LLMSetupError, match='LLM_SETUP_KEY_UNAVAILABLE'):
        setup_api._dpapi_unprotect('posix-v1:%%%')
    path = tmp_path / 'deepseek_api_key.dpapi'
    path.write_text(protected, encoding='utf-8')
    assert start_local._load_dpapi_key(path) == 'olivia-test-key-value'
    path.write_text('dpapi-v1:AAAA', encoding='utf-8')
    assert start_local._load_dpapi_key(path) == ''
    assert start_local._load_dpapi_key(tmp_path / 'missing.dpapi') == ''


def test_a_port_left_waiting_by_a_stopped_backend_counts_as_free_on_linux(monkeypatch):
    """A restart must not see PORT_IN_USE for minutes while the old backend's connections sit in TIME_WAIT."""
    import socket
    calls = []

    class Probe:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def setsockopt(self, *args):
            calls.append(('setsockopt', args))

        def bind(self, address):
            calls.append(('bind', address))

    monkeypatch.setattr(socket, 'socket', Probe)
    monkeypatch.setattr(os, 'name', 'posix')
    assert start_local._port_is_bindable(8919) is True
    assert calls == [('setsockopt', (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)), ('bind', ('127.0.0.1', 8919))]
    calls.clear()
    monkeypatch.setattr(os, 'name', 'nt')
    assert start_local._port_is_bindable(8919) is True
    assert calls == [('bind', ('127.0.0.1', 8919))]
