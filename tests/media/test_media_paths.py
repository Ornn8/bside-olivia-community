from __future__ import annotations

from pathlib import Path

import media_paths
from runtime.media import media_paths as runtime_media_paths


def test_root_module_reexports_runtime_media_path_api() -> None:
    assert media_paths.resolve_media_path is runtime_media_paths.resolve_media_path
    assert media_paths.configured_media_path is runtime_media_paths.configured_media_path


def test_relative_media_path_resolves_from_explicit_project_root(tmp_path: Path) -> None:
    assert runtime_media_paths.resolve_media_path(
        "media/reply.mp4",
        {"OLIVIA_PROJECT_ROOT": str(tmp_path)},
    ) == (tmp_path / "media" / "reply.mp4").resolve()


def test_relative_media_path_without_absolute_project_root_is_rejected() -> None:
    assert runtime_media_paths.resolve_media_path(
        "media/reply.mp4",
        {"OLIVIA_PROJECT_ROOT": "relative-root"},
    ) is None
