"""Finalize PR #105 into directly committed product source.

This one-shot helper is removed by its own final commit. It exists only to turn
the already generated workspace into the exact source tree that reviewers and
users receive, while correcting the Archive/Mem0 path split and committing a
clean-head Windows runtime check.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "installer" / "start_local.py"
MEM0 = ROOT / "mem0_memory.py"
INSTALL_TEST = ROOT / "tests" / "installer" / "test_memory_default_runtime.py"
SEPARATION_TEST = ROOT / "tests" / "installer" / "test_memory_storage_separation.py"
PUBLIC_WORKFLOW = ROOT / ".github" / "workflows" / "public-smoke.yml"
MEMORY_WORKFLOW = ROOT / ".github" / "workflows" / "memory-runtime-smoke.yml"


def replace_once(value: str, old: str, new: str, label: str) -> str:
    if value.count(old) != 1:
        raise RuntimeError(f"PR105_{label}_ANCHOR_INVALID")
    return value.replace(old, new, 1)


def patch_start() -> None:
    value = START.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    enabled = _memory_enabled(environment.get("OLIVIA_MEMORY_ENABLED"))
    memory_root = data_root / "memory" / "mem0"
    model_cache = data_root / "memory" / "model-cache"
    environment.update(
        {
            "OLIVIA_MEMORY_ENABLED": enabled,
            "OLIVIA_MEMORY_ROOT": str(memory_root),
''',
        '''    enabled = _memory_enabled(environment.get("OLIVIA_MEMORY_ENABLED"))
    archive_root = data_root / "memory"
    mem0_root = archive_root / "mem0"
    model_cache = archive_root / "model-cache"
    environment.update(
        {
            "OLIVIA_MEMORY_ENABLED": enabled,
            # Existing Archive SQLite keeps its historical root.  Mem0 has a
            # separate variable so enabling or disabling it cannot move the
            # Archive database or hide previously imported letters.
            "OLIVIA_MEMORY_ROOT": str(archive_root),
            "OLIVIA_MEM0_ROOT": str(mem0_root),
''',
        "START_PATH_SPLIT",
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
        '''    configured_root = environment.get("OLIVIA_MEM0_ROOT", "").strip()
    data_root = (
        Path(configured_root).expanduser()
        if configured_root
        else root / ".olivia_data" / "memory" / "mem0"
    )
''',
        "MEM0_PATH_SPLIT",
    )
    MEM0.write_text(value, encoding="utf-8")


def patch_install_test() -> None:
    value = INSTALL_TEST.read_text(encoding="utf-8")
    value = replace_once(
        value,
        '''    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
''',
        '''    assert environment["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory"
    )
    assert environment["OLIVIA_MEM0_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
    assert environment["OLIVIA_MEMORY_ROOT"] != environment["OLIVIA_MEM0_ROOT"]
''',
        "INSTALL_TEST_PATH_SPLIT",
    )
    INSTALL_TEST.write_text(value, encoding="utf-8")


def write_storage_separation_test() -> None:
    SEPARATION_TEST.write_text(
        '''from __future__ import annotations

from pathlib import Path

from installer.start_local import _configure_memory_environment
from local_memory import create_memory_adapter
from mem0_memory import load_mem0_config
from memory_port import LEGACY_LETTERS, LegacyLetter


def _environment(data_root: Path) -> dict[str, str]:
    return _configure_memory_environment(
        {
            "OLIVIA_LLM_BASE_URL": "https://api.example.invalid",
            "OLIVIA_LLM_MODEL": "fixture-model",
            "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_API_KEY",
        },
        data_root,
    )


def test_archive_mem0_and_private_world_keep_separate_owned_paths(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    environment = _environment(data_root)
    environment["OLIVIA_PRIVATE_WORLD_DB"] = str(
        data_root / "private_world" / "private_world.sqlite3"
    )

    archive = create_memory_adapter(environ=environment)
    result = archive.import_legacy_records(
        [
            LegacyLetter(
                content="旧 Archive 记录仍然可见。",
                source_record_id="legacy.fixture.1",
                source="fixture",
            )
        ]
    )
    assert result.inserted == 1
    archive_path = Path(archive.db_path)
    archive.close()

    reopened = create_memory_adapter(environ=environment)
    records = reopened.search(
        "Archive",
        domains=(LEGACY_LETTERS,),
        limit=8,
    )
    assert [record.text for record in records] == [
        "旧 Archive 记录仍然可见。"
    ]
    reopened.close()

    mem0 = load_mem0_config(environ=environment, project_root=tmp_path)
    private_world = Path(environment["OLIVIA_PRIVATE_WORLD_DB"])
    assert archive_path == data_root / "memory" / "memory.sqlite3"
    assert mem0.data_root == data_root / "memory" / "mem0"
    assert mem0.qdrant_path == data_root / "memory" / "mem0" / "qdrant"
    assert private_world == data_root / "private_world" / "private_world.sqlite3"
    assert archive_path != mem0.history_path
    assert archive_path not in mem0.qdrant_path.parents
    assert private_world not in mem0.qdrant_path.parents


def test_explicit_mem0_disable_does_not_move_archive(tmp_path: Path) -> None:
    environment = {
        "OLIVIA_MEMORY_ENABLED": "0",
        "OLIVIA_LLM_BASE_URL": "https://api.example.invalid",
        "OLIVIA_LLM_MODEL": "fixture-model",
        "OLIVIA_LLM_API_KEY_ENV": "FIXTURE_API_KEY",
    }
    configured = _configure_memory_environment(environment, tmp_path / "data")
    assert configured["OLIVIA_MEMORY_ENABLED"] == "0"
    assert configured["OLIVIA_MEMORY_ROOT"] == str(
        tmp_path / "data" / "memory"
    )
    assert configured["OLIVIA_MEM0_ROOT"] == str(
        tmp_path / "data" / "memory" / "mem0"
    )
''',
        encoding="utf-8",
    )


def write_workflows() -> None:
    PUBLIC_WORKFLOW.write_text(
        '''name: public-smoke

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  public-smoke:
    name: Public smoke (Windows / Python 3.12)
    runs-on: windows-latest
    timeout-minutes: 15
    steps:
      - name: Check out source
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install public development dependencies
        run: python -m pip install -e ".[dev]"

      - name: Run public smoke tests
        run: python -m pytest -q

      - name: Run repository hardening scan
        run: python baseline_hardening_scan.py --mode all

      - name: Check whitespace and clean checkout
        shell: pwsh
        run: |
          git diff --check --exit-code
          if (git status --porcelain) {
            throw 'tests modified the frozen checkout'
          }
''',
        encoding="utf-8",
    )
    MEMORY_WORKFLOW.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_WORKFLOW.write_text(
        '''name: memory-runtime-smoke

on:
  push:
  pull_request:
    paths:
      - "mem0_memory.py"
      - "memory_model.py"
      - "conversation_memory_*.py"
      - "memory_prompt.py"
      - "local_memory.py"
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
    timeout-minutes: 30
    steps:
      - name: Check out frozen source
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
          "DO_NOT_TRACK=1" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append

      - name: Run real offline embedding and Mem0/Qdrant test
        shell: pwsh
        run: |
          python -m pytest -q -rs tests/memory/test_mem0_installed_runtime.py 2>&1 | Tee-Object -FilePath real-memory-test.log
          if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
          if (Select-String -LiteralPath real-memory-test.log -Pattern 'skipped') {
            throw 'real memory runtime test was skipped'
          }

      - name: Run focused integration suite
        run: >-
          python -m pytest -q
          tests/installer/test_memory_model_provisioning.py
          tests/installer/test_memory_default_runtime.py
          tests/installer/test_memory_storage_separation.py
          tests/memory/test_mem0_memory.py
          tests/memory/test_memory_prompt_mem0_wiring.py
          tests/memory/test_conversation_memory_runtime.py

      - name: Re-verify model in offline mode
        run: >-
          python tools/provision_memory_model.py
          --manifest installer/memory-model-manifest.json
          --cache-root ${{ env.OLIVIA_TEST_MODEL_CACHE }}
          --verify-only

      - name: Check frozen checkout remains clean
        shell: pwsh
        run: |
          git diff --check --exit-code
          if (git status --porcelain) {
            throw 'runtime validation modified the frozen checkout'
          }
''',
        encoding="utf-8",
    )


def remove_one_shot_files() -> None:
    for relative in (
        ".github/workflows/build-memory-default-runtime.yml",
        ".github/workflows/probe-memory-model.yml",
        ".github/workflows/finalize-pr105.yml",
        "tools/prepare_memory_default_runtime.py",
        "tools/finalize_memory_default_runtime.py",
        "tools/finalize_pr105_review_blockers.py",
        "installed_memory_runtime.py",
        "tests/memory/test_installed_memory_runtime.py",
        "tests/memory/test_memory_prompt_installed_runtime.py",
    ):
        path = ROOT / relative
        if path.exists():
            path.unlink()


def main() -> None:
    patch_start()
    patch_mem0()
    patch_install_test()
    write_storage_separation_test()
    write_workflows()
    remove_one_shot_files()


if __name__ == "__main__":
    main()
