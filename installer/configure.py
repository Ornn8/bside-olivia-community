"""First-run local configuration without putting credentials in the install tree."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def _protect(value: str) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI_WINDOWS_ONLY")
    script = "$s=ConvertTo-SecureString ([Console]::In.ReadToEnd()) -AsPlainText -Force; ConvertFrom-SecureString $s"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        input=value + "\n", text=True, capture_output=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("DPAPI_PROTECT_FAILED")
    return result.stdout.strip()


def _copy_reference(source: Path, data_root: Path) -> dict[str, str]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise RuntimeError("REFERENCE_FILE_NOT_FOUND")
    target_dir = data_root / "third-party" / "reference"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if target.exists():
        raise RuntimeError("REFERENCE_TARGET_EXISTS")
    shutil.copy2(source, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"target": str(target), "sha256": digest, "size_bytes": str(target.stat().st_size)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure the local BSide installation")
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--reference-file", type=Path)
    parser.add_argument("--skip-key", action="store_true")
    args = parser.parse_args(argv)
    root = args.installation.expanduser().resolve()
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"status": "CONFIGURED", "key": "unchanged", "reference": None}
    if not args.skip_key:
        key = getpass.getpass("DeepSeek API key（输入不回显，留空跳过）: ")
        if key:
            config_dir = data_root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "deepseek_api_key.dpapi").write_text(_protect(key) + "\n", encoding="utf-8")
            result["key"] = "stored_dpapi"
    if args.reference_file:
        result["reference"] = _copy_reference(args.reference_file, data_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
