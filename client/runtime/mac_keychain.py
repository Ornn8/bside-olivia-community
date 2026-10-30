"""Store Olivia credentials in the current macOS user's Keychain."""

from __future__ import annotations

import re
import secrets


_SERVICE = "community.bside.olivia.credentials"
_REFERENCE = re.compile(r"^keychain-v1:([0-9a-f]{32})$")


def _backend():
    from keyring.backends.macOS import Keyring

    return Keyring()


def protect(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("KEYCHAIN_VALUE_INVALID")
    reference = secrets.token_hex(16)
    _backend().set_password(_SERVICE, reference, value)
    return f"keychain-v1:{reference}"


def unprotect(value: str) -> str:
    match = _REFERENCE.fullmatch(value)
    if match is None:
        raise ValueError("KEYCHAIN_REFERENCE_INVALID")
    secret = _backend().get_password(_SERVICE, match.group(1))
    if not secret:
        raise ValueError("KEYCHAIN_SECRET_UNAVAILABLE")
    return secret


def discard(value: str) -> None:
    match = _REFERENCE.fullmatch(value)
    if match is not None:
        _backend().delete_password(_SERVICE, match.group(1))
