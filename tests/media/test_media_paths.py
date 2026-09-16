from __future__ import annotations

from pathlib import Path
import pytest

from runtime.media import media_paths as runtime_media_paths


def test_executable_path_preserves_virtual_environment_symlink(tmp_path: Path) -> None:
    base = tmp_path / 'base-python'
    base.touch()
    interpreter = tmp_path / 'venv-python'
    try:
        interpreter.symlink_to(base)
    except OSError:
        pytest.skip('Creating symlinks requires host permission')
    assert runtime_media_paths.resolve_media_path(interpreter, {}, follow_symlinks=False) == interpreter
    assert runtime_media_paths.resolve_media_path(interpreter, {}) == base
    assert runtime_media_paths.configured_media_path({'OLIVIA_ROFORMER_PYTHON': str(interpreter)}, 'OLIVIA_ROFORMER_PYTHON') == interpreter


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
