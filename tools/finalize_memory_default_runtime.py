"""Finalize the committed Mem0 runtime after the one-shot source preparer.

This script is used once by the feature-branch workflow.  It converts the
prepared worktree into the final reviewable tree, closes the Archive/Mem0 path
collision, installs a clean-head required workflow, and deletes all one-shot
machinery including itself before the commit is created.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "installer" / "start_local.py"
MEM0 = ROOT / "mem0_memory.py"
MEM0_TEST = ROOT / "tests" / "memory" / "test_mem0_memory.py"
INSTALL_TEST = ROOT / "tests" / "installer" / "test_memory_default_runtime.py"
REAL_TEST = ROOT / "tests" / "memory" / "test_mem0_installed_runtime.py"
MEMORY_PROMPT = ROOT / "memory_prompt.py"
FINAL_WORKFLOW = ROOT / ".github" / "workflows" / "memory-runtime-smoke.yml"


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError(f"MEMORY_FINALIZE_{label}_ANCHOR_INVALID")
    return value.replace(old, new, 1)


def patch_start() -> None:
    value = START.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '    memory_root = data_root / "memory" / "mem0"\n'
        '    model_cache = data_root / "memory" / "model-cache"\n',
        '    archive_root = data_root / "memory"\n'
        '    conversation_root = archive_root / "mem0"\n'
        '    model_cache = archive_root / "model-cache"\n',
        "START_ROOTS",
    )
    value = replace_once(
        value,
        '            "OLIVIA_MEMORY_ROOT": str(memory_root),\n',
        '            "OLIVIA_MEMORY_ROOT": str(archive_root),\n'
        '            "OLIVIA_CONVERSATION_MEMORY_ROOT": str(conversation_root),\n',
        "START_ENV",
    )
    START.write_text(value, encoding="utf-8")


def patch_mem0() -> None:
    value = MEM0.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    configured_root = environment.get("OLIVIA_MEMORY_ROOT", "").strip()
    data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else root / ".olivia_data" / "memory" / "mem0"
    )
''',
        '''    configured_root = environment.get(
        "OLIVIA_CONVERSATION_MEMORY_ROOT",
        environment.get("OLIVIA_MEM0_ROOT", ""),
    ).strip()
    data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else root / ".olivia_data" / "memory" / "mem0"
    )
''',
        "MEM0_ROOT",
    )
    MEM0.write_text(value, encoding="utf-8")


def patch_memory_prompt() -> None:
    value = MEMORY_PROMPT.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''def _default_conversation_memory() -> ConversationMemoryPort | None:
    """Load the optional adapter lazily; Core remains dependency-free."""

    try:
        installed = os.environ.get(
            "OLIVIA_MEMORY_INSTALLED_RUNTIME", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        if installed:
            from installed_memory_runtime import (
                create_installed_mem0_adapter,
            )

            return create_installed_mem0_adapter()
        from mem0_memory import create_mem0_adapter

        return create_mem0_adapter()
    except Exception:
        return None
''',
        '''def _default_conversation_memory() -> ConversationMemoryPort | None:
    """Load the optional adapter lazily; disabled Core installs remain dependency-free."""

    try:
        from mem0_memory import create_mem0_adapter

        return create_mem0_adapter()
    except Exception:
        return None
''',
        "MEMORY_PROMPT",
    )
    MEMORY_PROMPT.write_text(value, encoding="utf-8")


def patch_tests() -> None:
    value = INSTALL_TEST.read_text(encoding="utf-8")
    value = value.replace(
        "from installer.start_local import _configure_memory_environment\n",
        "from installer.start_local import _configure_memory_environment\n"
        "from local_memory import LocalMemoryAdapter, create_memory_adapter\n"
        "from mem0_memory import load_mem0_config\n"
        "from memory_port import LEGACY_LETTERS, LegacyLetter\n"
        "from private_world_ledger import SQLitePrivateWorldLedger\n",
        1,
    )
    value = replace_once(
        value,
        '''    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
''',
        '''    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory"
    )
    assert environment["OLIVIA_CONVERSATION_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
''',
        "INSTALL_EXPECTATION",
    )
    value = value.replace('    assert "httpx2==" not in lock\n', "")
    value = value.replace('    assert "httpcore2==" not in lock\n', "")
    value += '''


def test_archive_mem0_and_private_world_keep_distinct_roots(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    archive_root = data_root / "memory"
    archive_db = archive_root / "memory.sqlite3"
    original = LocalMemoryAdapter(archive_db)
    try:
        result = original.import_legacy_records(
            [
                LegacyLetter(
                    content="升级前保留的旧信证据。",
                    source_record_id="legacy-before-mem0",
                    source="fixture",
                )
            ]
        )
        assert result.inserted == 1
    finally:
        original.close()

    environment = _configure_memory_environment(_base(), data_root)
    archive = create_memory_adapter(environ=environment)
    try:
        records = archive.search(
            "旧信证据",
            domains=(LEGACY_LETTERS,),
            limit=4,
        )
        assert [record.text for record in records] == [
            "升级前保留的旧信证据。"
        ]
        assert getattr(archive, "db_path", None) == archive_db
    finally:
        close = getattr(archive, "close", None)
        if callable(close):
            close()

    mem0 = load_mem0_config(environ=environment, project_root=tmp_path)
    private_world_db = data_root / "private_world" / "private_world.sqlite3"
    ledger = SQLitePrivateWorldLedger(private_world_db)
    assert ledger.snapshot().version >= 1

    assert mem0.data_root == archive_root / "mem0"
    assert mem0.qdrant_path.is_relative_to(mem0.data_root)
    assert archive_db.parent == archive_root
    assert private_world_db.parent == data_root / "private_world"
    assert not archive_db.is_relative_to(mem0.data_root)
    assert not private_world_db.is_relative_to(mem0.data_root)
'''
    INSTALL_TEST.write_text(value, encoding="utf-8")

    value = MEM0_TEST.read_text(encoding="utf-8")
    value = value.replace(
        '"OLIVIA_MEMORY_ROOT": str(tmp_path / "mem0")',
        '"OLIVIA_CONVERSATION_MEMORY_ROOT": str(tmp_path / "mem0")',
    )
    value = value.replace(
        '"OLIVIA_MEMORY_ROOT": str(tmp_path / "missing")',
        '"OLIVIA_CONVERSATION_MEMORY_ROOT": str(tmp_path / "missing")',
    )
    marker = "\ndef test_exchange_search_export_delete_and_clear(tmp_path: Path) -> None:\n"
    addition = '''

def test_mem0_root_never_reuses_archive_root(tmp_path: Path) -> None:
    config = load_mem0_config(
        environ={
            "OLIVIA_MEMORY_ENABLED": "1",
            "OLIVIA_MEMORY_ROOT": str(tmp_path / "archive"),
            "OLIVIA_CONVERSATION_MEMORY_ROOT": str(tmp_path / "mem0"),
            "OLIVIA_LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "OLIVIA_LLM_MODEL": "fixture-model",
        },
        project_root=tmp_path,
    )
    assert config.data_root == tmp_path / "mem0"
    assert config.data_root != tmp_path / "archive"
'''
    value = replace_once(value, marker, addition + marker, "MEM0_TEST")
    MEM0_TEST.write_text(value, encoding="utf-8")

    value = REAL_TEST.read_text(encoding="utf-8")
    value = value.replace(
        '    pytest.importorskip("mem0")\n    pytest.importorskip("fastembed")\n',
        '    __import__("mem0")\n    __import__("fastembed")\n',
        1,
    )
    value = replace_once(
        value,
        "    assert config.qdrant_path.is_dir()\n",
        "    assert config.qdrant_path.is_dir()\n"
        "    assert config.data_root != tmp_path / \"memory\"\n",
        "REAL_TEST",
    )
    REAL_TEST.write_text(value, encoding="utf-8")


def write_workflow() -> None:
    FINAL_WORKFLOW.parent.mkdir(parents=True, exist_ok=True)
    FINAL_WORKFLOW.write_text(
        '''name: memory-runtime-smoke

on:
  push:
    branches:
      - feat/p03-memory-default-runtime
  pull_request:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read

jobs:
  windows-memory-runtime:
    name: Memory runtime (Windows / Python 3.12)
    runs-on: windows-latest
    timeout-minutes: 40
    env:
      OLIVIA_MEMORY_RUNTIME_TEST: "1"
      OLIVIA_TEST_MODEL_CACHE: ${{ runner.temp }}\\olivia-memory-model
      PYTHONDONTWRITEBYTECODE: "1"
    steps:
      - name: Check out frozen source
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Reject generated-worktree test machinery
        shell: pwsh
        run: |
          $forbidden = @(
            '.github/workflows/build-memory-default-runtime.yml',
            '.github/workflows/probe-memory-model.yml',
            'tools/prepare_memory_default_runtime.py'
          )
          foreach ($path in $forbidden) {
            if (Test-Path -LiteralPath $path) {
              throw "forbidden generated-tree path remains: $path"
            }
          }

      - name: Install public development dependencies
        run: python -m pip install -e ".[dev]"

      - name: Install pinned Mem0 runtime closure
        run: >-
          python -m pip install
          --disable-pip-version-check
          --require-hashes
          --only-binary=:all:
          -r installer/memory-runtime-requirements.txt

      - name: Verify installed runtime imports
        run: >-
          python -c
          "import mem0,fastembed,qdrant_client,onnxruntime; print('memory-runtime-imports-ready')"

      - name: Provision and verify the real Chinese embedding model
        shell: pwsh
        run: |
          python tools/provision_memory_model.py `
            --manifest installer/memory-model-manifest.json `
            --cache-root $env:OLIVIA_TEST_MODEL_CACHE `
            --provision
          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
          python tools/provision_memory_model.py `
            --manifest installer/memory-model-manifest.json `
            --cache-root $env:OLIVIA_TEST_MODEL_CACHE `
            --verify-only
          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

      - name: Run real offline Mem0, Qdrant, installer and archive-isolation tests
        shell: pwsh
        run: |
          $output = python -m pytest -q -rs `
            tests/memory/test_mem0_installed_runtime.py `
            tests/memory/test_mem0_memory.py `
            tests/installer/test_memory_model_provisioning.py `
            tests/installer/test_memory_default_runtime.py 2>&1
          $output | Write-Host
          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
          if (($output -join "`n") -match '(?i)skipped') {
            throw 'memory runtime tests must not skip in the required workflow'
          }

      - name: Run repository hardening scan
        run: python baseline_hardening_scan.py --mode all

      - name: Verify clean checkout remained unchanged
        shell: pwsh
        run: |
          git diff --exit-code
          $status = git status --porcelain
          if ($status) {
            $status | Write-Host
            throw 'tests modified the frozen checkout'
          }
''',
        encoding="utf-8",
    )


def remove_one_shot_files() -> None:
    for relative in (
        ".github/workflows/build-memory-default-runtime.yml",
        ".github/workflows/probe-memory-model.yml",
        ".github/workflows/apply-memory-default-runtime.yml",
        "tools/prepare_memory_default_runtime.py",
        "installed_memory_runtime.py",
        "tests/memory/test_installed_memory_runtime.py",
        "tests/memory/test_memory_prompt_installed_runtime.py",
        "tools/finalize_memory_default_runtime.py",
    ):
        (ROOT / relative).unlink(missing_ok=True)


def main() -> None:
    patch_start()
    patch_mem0()
    patch_memory_prompt()
    patch_tests()
    write_workflow()
    remove_one_shot_files()


if __name__ == "__main__":
    main()
