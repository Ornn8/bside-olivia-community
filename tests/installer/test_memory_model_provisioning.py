from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path
import tarfile

import pytest

from memory_model import (
    MEMORY_MODEL_SCHEMA,
    MemoryModelError,
    MemoryModelManifest,
    extract_model_archive,
    sha256_file,
    validate_model_cache,
    verify_fastembed_model,
    write_model_marker,
)


def _manifest() -> MemoryModelManifest:
    return MemoryModelManifest(
        schema_version=MEMORY_MODEL_SCHEMA,
        provider="fastembed",
        provider_version="0.8.0",
        model="BAAI/bge-small-zh-v1.5",
        dimensions=64,
        license="mit",
        archive_url=(
            "https://storage.googleapis.com/qdrant-fastembed/fixture.tar.gz"
        ),
        archive_size=1,
        archive_sha256="0" * 64,
        archive_root="fast-bge-small-zh-v1.5",
        required_files=("config.json",),
    )


def _archive(path: Path, *, unsafe: str | None = None) -> MemoryModelManifest:
    root = "fast-bge-small-zh-v1.5"
    with tarfile.open(path, "w:gz") as archive:
        directory = tarfile.TarInfo(root + "/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        payload = b"{}"
        config = tarfile.TarInfo(root + "/config.json")
        config.size = len(payload)
        archive.addfile(config, io.BytesIO(payload))
        if unsafe == "traversal":
            escaped = tarfile.TarInfo(root + "/../escape.txt")
            escaped.size = 1
            archive.addfile(escaped, io.BytesIO(b"x"))
        elif unsafe == "symlink":
            link = tarfile.TarInfo(root + "/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            archive.addfile(link)
    return replace(
        _manifest(),
        archive_size=path.stat().st_size,
        archive_sha256=sha256_file(path),
    )


def test_verified_archive_extracts_and_marker_detects_tampering(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "model.tar.gz"
    manifest = _archive(archive)
    cache = tmp_path / "cache"
    extracted = extract_model_archive(archive, cache, manifest)
    assert extracted == cache / manifest.archive_root
    write_model_marker(cache, manifest)
    assert validate_model_cache(cache, manifest).ready is True

    (extracted / "config.json").write_text("changed", encoding="utf-8")
    invalid = validate_model_cache(cache, manifest)
    assert invalid.ready is False
    assert invalid.reason_code == "MEMORY_MODEL_CACHE_INVALID"


@pytest.mark.parametrize("unsafe", ["traversal", "symlink"])
def test_archive_links_and_traversal_are_rejected(
    tmp_path: Path,
    unsafe: str,
) -> None:
    archive = tmp_path / f"{unsafe}.tar.gz"
    manifest = _archive(archive, unsafe=unsafe)
    with pytest.raises(MemoryModelError) as error:
        extract_model_archive(archive, tmp_path / "cache", manifest)
    assert error.value.code == "MEMORY_MODEL_ARCHIVE_UNSAFE"
    assert not (tmp_path / "escape.txt").exists()


def test_offline_probe_verifies_vector_width_with_injected_provider(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    model_root = tmp_path / manifest.archive_root
    model_root.mkdir(parents=True)
    (model_root / "config.json").write_text("{}", encoding="utf-8")

    class Embedding:
        def __init__(self, **kwargs) -> None:
            assert kwargs["local_files_only"] is True
            assert kwargs["cache_dir"] == str(tmp_path)

        def embed(self, _values):
            return iter([[0.0] * 64])

    verify_fastembed_model(
        tmp_path,
        manifest,
        embedding_factory=Embedding,
    )

    class WrongWidth(Embedding):
        def embed(self, _values):
            return iter([[0.0] * 63])

    with pytest.raises(MemoryModelError) as error:
        verify_fastembed_model(
            tmp_path,
            manifest,
            embedding_factory=WrongWidth,
        )
    assert error.value.code == "MEMORY_MODEL_DIMENSION_MISMATCH"
