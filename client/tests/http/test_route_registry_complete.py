"""Every local route the server handles is registered; an unregistered one answers 501."""
import re
from pathlib import Path

import http_contract

ROOT = Path(__file__).resolve().parents[2]


def test_every_handled_local_route_is_registered():
    source = (ROOT / 'local_server.py').read_text(encoding='utf-8')
    handled = set(re.findall(r"p == ['\"](/toy/[^'\"]+)['\"]", source))
    assert '/toy/world/pets' in handled
    assert sorted(path for path in handled if path not in http_contract.ROUTES) == []
