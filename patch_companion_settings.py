"""Compatibility-preserving companion settings patch with personal-chat UI."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from runtime.personal_chat import _patch_companion_settings_base as _base
from runtime.personal_chat.settings_patch import (
    PersonalChatSettingsPatchError,
    patch_personal_chat_settings,
)


BOOTSTRAP_MEMBER = _base.BOOTSTRAP_MEMBER
CompanionSettingsPatchError = _base.CompanionSettingsPatchError
INDEX_MEMBER = _base.INDEX_MEMBER
MAIN_MODULE_MEMBER = _base.MAIN_MODULE_MEMBER
PATCH_MARKER = _base.PATCH_MARKER
PATCH_SCHEMA_VERSION = _base.PATCH_SCHEMA_VERSION
sha256_file = _base.sha256_file
validate_api_base = _base.validate_api_base


def patch_companion_settings(
    feapp_path: str | os.PathLike[str],
    api_base: str | None,
    *,
    work_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Patch the existing settings shell, then add the isolated chat binding UI.

    The mature settings bootstrap remains byte-for-byte unchanged.  A second,
    repository-owned inline script is inserted transactionally into index.html.
    """

    normalized_api_base = _base.validate_api_base(api_base)
    feapp = Path(feapp_path).expanduser().resolve()
    if not feapp.is_file():
        raise CompanionSettingsPatchError("COMPANION_ARCHIVE_NOT_FOUND")
    _base._validate_archive(feapp)

    descriptor, rollback_name = tempfile.mkstemp(
        prefix=f".{feapp.name}.personal-chat-",
        suffix=".rollback",
        dir=feapp.parent,
    )
    os.close(descriptor)
    rollback = Path(rollback_name)
    try:
        shutil.copy2(feapp, rollback)
        result = _base.patch_companion_settings(
            feapp,
            normalized_api_base,
            work_root=work_root,
        )
        personal_status = patch_personal_chat_settings(
            feapp,
            normalized_api_base,
        )
        value = dict(result)
        if personal_status == "PATCHED":
            value["status"] = "PATCHED"
        value["patched_sha256"] = _base.sha256_file(feapp)
        return value
    except PersonalChatSettingsPatchError as exc:
        _base._atomic_copy(rollback, feapp)
        raise CompanionSettingsPatchError(exc.code) from exc
    except Exception:
        _base._atomic_copy(rollback, feapp)
        raise
    finally:
        rollback.unlink(missing_ok=True)


def __getattr__(name: str):
    """Keep private helper imports used by existing tests/tools compatible."""

    return getattr(_base, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_base)))


__all__ = [
    name for name in _base.__all__ if name != "patch_companion_settings"
] + ["patch_companion_settings"]
