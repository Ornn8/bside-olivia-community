"""One-shot branch preparer for the default local Mem0 runtime.

The temporary workflow deletes this file and itself before committing the final
reviewable source tree.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MEM0 = ROOT / "mem0_memory.py"
MODEL = ROOT / "memory_model.py"
START = ROOT / "installer" / "start_local.py"
INSTALL = ROOT / "installer" / "Install.ps1"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
MEM0_TEST = ROOT / "tests" / "memory" / "test_mem0_memory.py"
MODEL_TEST = ROOT / "tests" / "installer" / "test_memory_model_provisioning.py"
INSTALL_TEST = ROOT / "tests" / "installer" / "test_memory_default_runtime.py"
REAL_TEST = ROOT / "tests" / "memory" / "test_mem0_installed_runtime.py"
FINAL_WORKFLOW = ROOT / ".github" / "workflows" / "memory-runtime-smoke.yml"
TEMP_WORKFLOW = ROOT / ".github" / "workflows" / "apply-memory-default-runtime.yml"
PROBE_WORKFLOW = ROOT / ".github" / "workflows" / "probe-memory-model.yml"


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError(f"MEMORY_RUNTIME_{label}_ANCHOR_INVALID")
    return value.replace(old, new, 1)


def patch_mem0() -> None:
    value = MEM0.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''from conversation_memory_port import (
    ConversationMemoryPort,
    ConversationMemoryRecord,
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)


MEM0_OSS_VERSION = "2.0.18"
''',
        '''from conversation_memory_port import (
    ConversationMemoryPort,
    ConversationMemoryRecord,
    ConversationMemoryStatus,
    MemoryWriteResult,
    MemoryWriteStatus,
    NullConversationMemoryPort,
    UnavailableConversationMemoryPort,
)
from memory_model import (
    MemoryModelError,
    configure_offline_model_environment,
    load_memory_model_manifest,
    validate_model_cache,
)


MEM0_OSS_VERSION = "2.0.18"
_MEMORY_MODEL_MANIFEST = (
    Path(__file__).resolve().parent
    / "installer"
    / "memory-model-manifest.json"
)
''',
        "MEM0_IMPORT",
    )
    value = value.replace(
        "environment = environ or os.environ",
        "environment = os.environ if environ is None else environ",
    )
    if value.count("environment = os.environ if environ is None else environ") != 2:
        raise RuntimeError("MEMORY_RUNTIME_MEM0_ENVIRONMENT_ANCHOR_INVALID")
    value = replace_once(
        value,
        '''            "embedder": {
                "provider": "huggingface",
                "config": {
                    "model": self.embedding_model,
                    "embedding_dims": self.embedding_dims,
                    "model_kwargs": {
                        "device": "cpu",
                        "cache_folder": str(self.model_cache),
                        "local_files_only": True,
                    },
                },
            },
''',
        '''            "embedder": {
                "provider": "fastembed",
                "config": {
                    "model": self.embedding_model,
                    "embedding_dims": self.embedding_dims,
                },
            },
''',
        "MEM0_PROVIDER",
    )
    old_factory = '''def create_mem0_adapter(
    config: Mem0Config | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    memory_factory: Callable[[Mapping[str, object]], Mem0Backend] | None = None,
) -> ConversationMemoryPort:
    active = config or load_mem0_config(environ=environ)
    if active.config_error:
        return UnavailableConversationMemoryPort(active.config_error)
    if not active.enabled:
        return NullConversationMemoryPort()
    try:
        active.qdrant_path.parent.mkdir(parents=True, exist_ok=True)
        active.history_path.parent.mkdir(parents=True, exist_ok=True)
        active.model_cache.mkdir(parents=True, exist_ok=True)
        backend = (memory_factory or _default_factory)(active.provider_config(environ))
        return Mem0ConversationMemoryAdapter(backend, active)
    except (ModuleNotFoundError, ImportError):
        return UnavailableConversationMemoryPort("MEM0_IMPORT_FAILED")
    except (OSError, RuntimeError, TypeError, ValueError):
        return UnavailableConversationMemoryPort("MEM0_INITIALIZATION_FAILED")
'''
    new_factory = '''def create_mem0_adapter(
    config: Mem0Config | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    memory_factory: Callable[[Mapping[str, object]], Mem0Backend] | None = None,
) -> ConversationMemoryPort:
    environment = os.environ if environ is None else environ
    active = config or load_mem0_config(environ=environment)
    if active.config_error:
        return UnavailableConversationMemoryPort(active.config_error)
    if not active.enabled:
        return NullConversationMemoryPort()

    # Production startup must be fully offline and use the exact model cache
    # installed under the local data root.  Injected factories remain a pure
    # test seam and do not require third-party model bytes.
    if memory_factory is None:
        if not str(environment.get(active.llm_api_key_env, "")).strip():
            return UnavailableConversationMemoryPort(
                "MEM0_LLM_API_KEY_MISSING"
            )
        try:
            manifest = load_memory_model_manifest(
                _MEMORY_MODEL_MANIFEST
            )
        except MemoryModelError as exc:
            return UnavailableConversationMemoryPort(exc.code)
        if (
            manifest.provider != "fastembed"
            or manifest.model != active.embedding_model
            or manifest.dimensions != active.embedding_dims
        ):
            return UnavailableConversationMemoryPort(
                "MEMORY_MODEL_CONFIG_MISMATCH"
            )
        model_status = validate_model_cache(
            active.model_cache,
            manifest,
        )
        if not model_status.ready:
            return UnavailableConversationMemoryPort(
                model_status.reason_code or "MEMORY_MODEL_NOT_READY"
            )

    configure_offline_model_environment(active.model_cache)
    try:
        active.qdrant_path.parent.mkdir(parents=True, exist_ok=True)
        active.history_path.parent.mkdir(parents=True, exist_ok=True)
        active.model_cache.mkdir(parents=True, exist_ok=True)
        backend = (memory_factory or _default_factory)(
            active.provider_config(environment)
        )
        return Mem0ConversationMemoryAdapter(backend, active)
    except (ModuleNotFoundError, ImportError):
        return UnavailableConversationMemoryPort("MEM0_IMPORT_FAILED")
    except (OSError, RuntimeError, TypeError, ValueError):
        return UnavailableConversationMemoryPort("MEM0_INITIALIZATION_FAILED")
'''
    value = replace_once(value, old_factory, new_factory, "MEM0_FACTORY")
    MEM0.write_text(value, encoding="utf-8")


def patch_model() -> None:
    value = MODEL.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    try:
        installed_version = importlib.metadata.version("fastembed")
    except importlib.metadata.PackageNotFoundError as exc:
        raise MemoryModelError("MEMORY_MODEL_PROVIDER_UNAVAILABLE") from exc
    if installed_version != manifest.provider_version:
        raise MemoryModelError("MEMORY_MODEL_PROVIDER_VERSION_MISMATCH")
    try:
        if embedding_factory is None:
            from fastembed import TextEmbedding

            embedding_factory = TextEmbedding
''',
        '''    if embedding_factory is None:
        try:
            installed_version = importlib.metadata.version("fastembed")
        except importlib.metadata.PackageNotFoundError as exc:
            raise MemoryModelError(
                "MEMORY_MODEL_PROVIDER_UNAVAILABLE"
            ) from exc
        if installed_version != manifest.provider_version:
            raise MemoryModelError(
                "MEMORY_MODEL_PROVIDER_VERSION_MISMATCH"
            )
        try:
            from fastembed import TextEmbedding

            embedding_factory = TextEmbedding
        except ImportError as exc:
            raise MemoryModelError(
                "MEMORY_MODEL_PROVIDER_UNAVAILABLE"
            ) from exc
    try:
''',
        "MODEL_FACTORY_SEAM",
    )
    MODEL.write_text(value, encoding="utf-8")


def patch_start() -> None:
    value = START.read_text(encoding="utf-8")
    anchor = '''_BACKEND_BOOTSTRAP = (
    "import runpy,sys; "
    "backend,entrypoint,*args=sys.argv[1:]; "
    "sys.path.insert(0, backend); "
    "sys.argv=[entrypoint,*args]; "
    "runpy.run_path(entrypoint,run_name='__main__')"
)


def main(argv: list[str] | None = None) -> int:
'''
    replacement = '''_BACKEND_BOOTSTRAP = (
    "import runpy,sys; "
    "backend,entrypoint,*args=sys.argv[1:]; "
    "sys.path.insert(0, backend); "
    "sys.argv=[entrypoint,*args]; "
    "runpy.run_path(entrypoint,run_name='__main__')"
)


def _memory_enabled(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return "0" if normalized in {"0", "false", "no", "off"} else "1"


def _configure_memory_environment(
    environment: dict[str, str],
    data_root: Path,
) -> dict[str, str]:
    """Confine the optional Mem0 runtime to install-owned offline paths."""

    enabled = _memory_enabled(environment.get("OLIVIA_MEMORY_ENABLED"))
    memory_root = data_root / "memory" / "mem0"
    model_cache = data_root / "memory" / "model-cache"
    environment.update(
        {
            "OLIVIA_MEMORY_ENABLED": enabled,
            "OLIVIA_MEMORY_ROOT": str(memory_root),
            "OLIVIA_MEMORY_EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
            "OLIVIA_MEMORY_EMBEDDING_DIMS": "512",
            "OLIVIA_MEMORY_EMBEDDING_CACHE": str(model_cache),
            "OLIVIA_MEMORY_LLM_BASE_URL": environment.get(
                "OLIVIA_LLM_BASE_URL", ""
            ),
            "OLIVIA_MEMORY_LLM_MODEL": environment.get(
                "OLIVIA_LLM_MODEL", ""
            ),
            "OLIVIA_MEMORY_LLM_API_KEY_ENV": environment.get(
                "OLIVIA_LLM_API_KEY_ENV", "DEEPSEEK_API_KEY"
            ),
            "OLIVIA_MEMORY_OUTBOX_ENABLED": "1",
            "FASTEMBED_CACHE_PATH": str(model_cache),
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
        }
    )
    return environment


def main(argv: list[str] | None = None) -> int:
'''
    value = replace_once(value, anchor, replacement, "START_HELPER")
    value = replace_once(
        value,
        '''            "OLIVIA_PORT": str(args.port),
        }
    )
    if not any(environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
''',
        '''            "OLIVIA_PORT": str(args.port),
        }
    )
    _configure_memory_environment(environment, data_root)
    if not any(environment.get(name) for name in ("OLIVIA_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")):
''',
        "START_APPLY",
    )
    START.write_text(value, encoding="utf-8")


def patch_install() -> None:
    value = INSTALL.read_text(encoding="utf-8-sig")
    value = replace_once(
        value,
        "$runtimeSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'\n",
        "$runtimeSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'\n$memoryDependenciesReady = $false\n",
        "INSTALL_STATE",
    )
    optional_dependencies = r'''

if ($runner.File -eq $runtimeExe) {
    try {
        & $runner.File '-c' 'import mem0,fastembed,qdrant_client,onnxruntime' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $memoryDependenciesReady = $true
        }
        else {
            Write-Host 'Long-term memory optionally installs Mem0 OSS, FastEmbed, local Qdrant, and their fixed Windows/Python 3.12 dependency closure.'
            Write-Host 'Licenses: Mem0 / FastEmbed / Qdrant client use their upstream open-source licenses; the Chinese BGE model is MIT.'
            $answer = Read-Host 'Accept these licenses and download the pinned memory runtime? [Y/N]'
            if ($answer -match '^(y|yes)$') {
                $memoryRequirements = Join-Path $PayloadRoot 'installer\memory-runtime-requirements.txt'
                & $runner.File '-m' 'pip' 'install' '--disable-pip-version-check' '--require-hashes' '--only-binary=:all:' '--upgrade' '--target' $sitePackages '-r' $memoryRequirements
                if ($LASTEXITCODE -eq 0) {
                    & $runner.File '-c' 'import mem0,fastembed,qdrant_client,onnxruntime'
                    $memoryDependenciesReady = $LASTEXITCODE -eq 0
                }
                if (-not $memoryDependenciesReady) {
                    Write-Warning 'MEMORY_DEPENDENCIES_UNAVAILABLE: Olivia will continue without long-term memory.'
                }
            }
            else {
                Write-Warning 'MEMORY_DEPENDENCIES_NOT_ACCEPTED: Olivia will continue without long-term memory.'
            }
        }
    }
    catch {
        $memoryDependenciesReady = $false
        Write-Warning 'MEMORY_DEPENDENCIES_UNAVAILABLE: Olivia will continue without long-term memory.'
    }
}
'''
    value = replace_once(
        value,
        "\n$arguments = @('install', '--payload', $PayloadRoot, '--destination', $Destination, '--manifest', (Join-Path $PayloadRoot 'installer\\full-patch-manifest.json'), '--port', $Port)\n",
        optional_dependencies
        + "\n$arguments = @('install', '--payload', $PayloadRoot, '--destination', $Destination, '--manifest', (Join-Path $PayloadRoot 'installer\\full-patch-manifest.json'), '--port', $Port)\n",
        "INSTALL_DEPENDENCIES",
    )
    provision = r'''

if ($memoryDependenciesReady) {
    $memoryManifest = Join-Path $PayloadRoot 'installer\memory-model-manifest.json'
    $memoryTool = Join-Path $PayloadRoot 'tools\provision_memory_model.py'
    $memoryCache = Join-Path $Destination 'data\memory\model-cache'
    & $runner.File $memoryTool '--manifest' $memoryManifest '--cache-root' $memoryCache '--verify-only' *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Long-term memory uses the pinned BAAI/bge-small-zh-v1.5 Chinese embedding model (MIT license).'
        Write-Host 'The verified archive is stored only inside this isolated installation data directory.'
        $answer = Read-Host 'Accept the model license and download it? [Y/N]'
        if ($answer -match '^(y|yes)$') {
            & $runner.File $memoryTool '--manifest' $memoryManifest '--cache-root' $memoryCache '--provision'
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'MEMORY_MODEL_UNAVAILABLE: Olivia will continue without long-term memory.'
            }
        }
        else {
            Write-Warning 'MEMORY_MODEL_NOT_ACCEPTED: Olivia will continue without long-term memory.'
        }
    }
}
'''
    value = replace_once(
        value,
        '''if ($installExitCode -ne 0) { exit $installExitCode }

& (Join-Path $PSScriptRoot 'Create-Shortcut.ps1') -InstallRoot $Destination
''',
        '''if ($installExitCode -ne 0) { exit $installExitCode }
'''
        + provision
        + '''
& (Join-Path $PSScriptRoot 'Create-Shortcut.ps1') -InstallRoot $Destination
''',
        "INSTALL_MODEL",
    )
    INSTALL.write_text(value, encoding="utf-8-sig")


def patch_notices() -> None:
    value = NOTICES.read_text(encoding="utf-8")
    value = replace_once(
        value,
        "| `opencv-python-headless` | 可选媒体测试依赖 | Apache-2.0 |\n",
        "| `opencv-python-headless` | 可选媒体测试依赖 | Apache-2.0 |\n"
        "| `mem0ai` | 可选本地长期记忆编排 | Apache-2.0（以固定发行包随附文本为准） |\n"
        "| `fastembed` / `onnxruntime` | 本地中文向量生成 | Apache-2.0 / MIT（以固定发行包随附文本为准） |\n"
        "| `qdrant-client` | 安装目录内的本地向量库 | Apache-2.0 |\n"
        "| `BAAI/bge-small-zh-v1.5` | 中文 embedding 模型 | MIT；安装时单独征得许可 |\n",
        "NOTICES_ROWS",
    )
    NOTICES.write_text(value, encoding="utf-8")


def patch_existing_test() -> None:
    value = MEM0_TEST.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    assert mapping["embedder"]["provider"] == "huggingface"
    assert mapping["embedder"]["config"]["model_kwargs"] == {
        "device": "cpu",
        "cache_folder": str(config.model_cache),
        "local_files_only": True,
    }
''',
        '''    assert mapping["embedder"] == {
        "provider": "fastembed",
        "config": {
            "model": "BAAI/bge-small-zh-v1.5",
            "embedding_dims": 512,
        },
    }
''',
        "TEST_PROVIDER",
    )
    MEM0_TEST.write_text(value, encoding="utf-8")


def write_tests() -> None:
    MODEL_TEST.parent.mkdir(parents=True, exist_ok=True)
    MODEL_TEST.write_text(r'''from __future__ import annotations

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
''', encoding="utf-8")

    INSTALL_TEST.write_text(r'''from __future__ import annotations

from pathlib import Path

from installer.start_local import _configure_memory_environment


def _base() -> dict[str, str]:
    return {
        "OLIVIA_LLM_BASE_URL": "https://api.example.invalid",
        "OLIVIA_LLM_MODEL": "fixture-model",
        "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_API_KEY",
    }


def test_normal_start_enables_confined_offline_memory_by_default(
    tmp_path: Path,
) -> None:
    environment = _configure_memory_environment(_base(), tmp_path / "data")
    assert environment["OLIVIA_MEMORY_ENABLED"] == "1"
    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
    assert environment["OLIVIA_MEMORY_EMBEDDING_CACHE"] == str(
        tmp_path / "data" / "memory" / "model-cache"
    )
    assert environment["FASTEMBED_CACHE_PATH"] == environment[
        "OLIVIA_MEMORY_EMBEDDING_CACHE"
    ]
    assert environment["OLIVIA_MEMORY_EMBEDDING_MODEL"] == (
        "BAAI/bge-small-zh-v1.5"
    )
    assert environment["OLIVIA_MEMORY_EMBEDDING_DIMS"] == "512"
    assert environment["OLIVIA_MEMORY_LLM_BASE_URL"] == (
        "https://api.example.invalid"
    )
    assert environment["OLIVIA_MEMORY_LLM_MODEL"] == "fixture-model"
    assert environment["OLIVIA_MEMORY_LLM_API_KEY_ENV"] == (
        "FIXTURE_API_KEY"
    )
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_explicit_memory_disable_is_preserved(tmp_path: Path) -> None:
    environment = _base()
    environment["OLIVIA_MEMORY_ENABLED"] = "0"
    configured = _configure_memory_environment(environment, tmp_path / "data")
    assert configured["OLIVIA_MEMORY_ENABLED"] == "0"


def test_windows_installer_keeps_memory_optional_and_verified() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "Install.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "memory-runtime-requirements.txt" in script
    assert "--require-hashes" in script
    assert "--only-binary=:all:" in script
    assert "memory-model-manifest.json" in script
    assert "provision_memory_model.py" in script
    assert "--verify-only" in script
    assert "--provision" in script
    assert "Olivia will continue without long-term memory" in script
    assert "MEMORY_DEPENDENCIES_UNAVAILABLE" in script
    assert "MEMORY_MODEL_UNAVAILABLE" in script

    lock = (root / "installer" / "memory-runtime-requirements.txt").read_text(
        encoding="utf-8"
    )
    package_lines = [
        line for line in lock.splitlines()
        if line and not line.startswith("#")
    ]
    assert package_lines
    assert all("==" in line and "--hash=sha256:" in line for line in package_lines)
    assert any(line.startswith("mem0ai==2.0.18 ") for line in package_lines)
    assert any(line.startswith("fastembed==0.8.0 ") for line in package_lines)
''', encoding="utf-8")

    REAL_TEST.write_text(r'''from __future__ import annotations

import os
from pathlib import Path

import pytest

from conversation_memory_port import UnavailableConversationMemoryPort
from mem0_memory import (
    Mem0Config,
    Mem0ConversationMemoryAdapter,
    create_mem0_adapter,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("OLIVIA_MEMORY_RUNTIME_TEST") != "1",
    reason="requires the installed pinned Mem0/FastEmbed runtime",
)


def test_installed_mem0_initializes_offline_with_local_qdrant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("mem0")
    pytest.importorskip("fastembed")
    cache = Path(os.environ["OLIVIA_TEST_MODEL_CACHE"]).resolve()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    config = Mem0Config(
        enabled=True,
        data_root=tmp_path / "memory" / "mem0",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="fixture-model",
        embedding_cache=cache,
    )
    adapter = create_mem0_adapter(config)
    assert not isinstance(adapter, UnavailableConversationMemoryPort), getattr(
        adapter, "reason_code", None
    )
    assert isinstance(adapter, Mem0ConversationMemoryAdapter)
    assert os.environ["FASTEMBED_CACHE_PATH"] == str(cache)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    status = adapter.status()
    assert status.status == "available"
    assert status.provider == "mem0"
    assert config.qdrant_path.is_dir()
    assert adapter.search_context(
        "合成中文检索",
        user_id="local-user",
        limit=3,
    ) == ()
''', encoding="utf-8")


def write_workflow() -> None:
    FINAL_WORKFLOW.write_text(r'''name: memory-runtime-smoke

on:
  pull_request:
    paths:
      - "mem0_memory.py"
      - "memory_model.py"
      - "conversation_memory_*.py"
      - "memory_prompt.py"
      - "installer/**"
      - "tools/provision_memory_model.py"
      - "tests/memory/**"
      - "tests/installer/**"
      - ".github/workflows/memory-runtime-smoke.yml"

permissions:
  contents: read

jobs:
  memory-runtime-smoke:
    name: Memory runtime (Windows / Python 3.12)
    runs-on: windows-latest
    timeout-minutes: 20
    steps:
      - name: Check out source
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install public test dependencies
        run: python -m pip install -e ".[dev]"

      - name: Install pinned memory runtime
        run: >-
          python -m pip install
          --disable-pip-version-check
          --require-hashes
          --only-binary=:all:
          -r installer/memory-runtime-requirements.txt

      - name: Provision pinned Chinese model
        shell: pwsh
        run: |
          $cache = Join-Path $env:RUNNER_TEMP 'olivia-memory-model-cache'
          python tools/provision_memory_model.py --manifest installer/memory-model-manifest.json --cache-root $cache --provision
          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
          "OLIVIA_TEST_MODEL_CACHE=$cache" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
          "OLIVIA_MEMORY_RUNTIME_TEST=1" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
          "HF_HUB_OFFLINE=1" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
          "HF_HUB_DISABLE_TELEMETRY=1" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append

      - name: Verify offline model and real Mem0 initialization
        run: >-
          python -m pytest -q
          tests/installer/test_memory_model_provisioning.py
          tests/installer/test_memory_default_runtime.py
          tests/memory/test_mem0_memory.py
          tests/memory/test_mem0_installed_runtime.py
          tests/memory/test_memory_prompt_mem0_wiring.py
          tests/memory/test_conversation_memory_runtime.py

      - name: Verify model without network
        shell: pwsh
        run: |
          python tools/provision_memory_model.py --manifest installer/memory-model-manifest.json --cache-root $env:OLIVIA_TEST_MODEL_CACHE --verify-only

      - name: Check whitespace
        run: git diff --check --exit-code
''', encoding="utf-8")


def cleanup() -> None:
    for path in (PROBE_WORKFLOW, TEMP_WORKFLOW, Path(__file__)):
        path.unlink(missing_ok=True)


def main() -> None:
    patch_mem0()
    patch_model()
    patch_start()
    patch_install()
    patch_notices()
    patch_existing_test()
    write_tests()
    write_workflow()
    cleanup()


if __name__ == "__main__":
    main()
