"""Restore desktop mail visibility in the exact supported managed native copy.

Only five offline-mode calls are replaced; native account checks, settings and
the separately updated frontend are untouched. Both original DLLs are backed
up using the existing native-navigation transaction before either is changed.
"""
from __future__ import annotations

import os

from .patch_native_navigation import _managed_roots, _patch_registered_files


_ALLOW = bytes.fromhex('33c090909090')
_SPECS = {
    'studio_ui': {
        'relative_path': 'plugins/Studio/NutStudioUI.dll',
        'fingerprints': {
            'original': (1297376, '3756767fc01c2a1c034a56c1ae2920651f13021a1b4cc0c3ed291fc92a9728e1'),
            'patched': (1297376, '294dcfe023c84bc83bdd531d8431bf76b2b7a1fbc0941a250ae8f4cf2ed8fa99'),
        },
        'binary_replacements': (
            ('studio-1', bytes.fromhex('cbe8d2370800eb1eff15b2ec0800488d8fa8'), 8, _ALLOW),
            ('studio-2', bytes.fromhex('cbe872340800eb1eff1552e90800488d8fa8'), 8, _ALLOW),
            ('studio-3', bytes.fromhex('cbe8b21f0800eb2bff1592d4080084c07514'), 8, _ALLOW),
            ('studio-4', bytes.fromhex('cbe8ff1d0800eb1cff15dfd20800488d4f38'), 8, _ALLOW),
        ),
    },
    'container_plugin': {
        'relative_path': 'plugins/Container/NutContainerPlugin.dll',
        'fingerprints': {
            'original': (498144, '53b61d8e9766c5b1cf2af29ed1a4ac7985052db65c37aa1829c71416050e31d1'),
            'patched': (498144, 'd78112ca218f805d437d2b03fe4c772c7cb279848dccce16570cbd466fe66ab4'),
        },
        'binary_replacements': (
            ('container-1', bytes.fromhex('488bda488bf9ff1561a4040084c00f85'), 6, _ALLOW),
        ),
    },
}


def patch_native_mail(
    client_version_root: str | os.PathLike[str],
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    root, work = _managed_roots(client_version_root, work_root=work_root)
    return _patch_registered_files(root, work, _SPECS)
