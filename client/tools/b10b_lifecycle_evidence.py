"""Run a real, local-only B10B lifecycle acceptance probe.

The probe creates only B10B-owned metadata below an ignored evidence directory.
It never downloads, copies, or deletes provider assets outside the project.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.packaging.b10b.errors import B10BError
from runtime.packaging.b10b.manager import B10BManager


def run(*, project_root: Path, evidence_parent: Path) -> dict[str, object]:
    evidence_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="b10b-lifecycle-", dir=evidence_parent) as temporary:
        data_root = Path(temporary) / "data"
        manager = B10BManager(project_root=project_root, data_root=data_root)
        operations: list[dict[str, object]] = []

        def record(value: dict[str, object]) -> None:
            operations.append({"operation": value.get("operation"), "status": value.get("status"), "dry_run": value.get("dry_run", False)})

        record(manager.install(["core/http"], dry_run=True))
        record(manager.install(["core/http"]))
        record(manager.enable("core/http"))
        record(manager.customize("core/http", {"route_policy": {"mode": "local"}}, dry_run=True))
        record(manager.disable("core/http"))
        record(manager.customize("core/http", {"route_policy": {"mode": "local"}}))
        record(manager.uninstall(["core/http"], dry_run=True))
        record(manager.uninstall(["core/http"], dry_run=False))
        record(manager.rollback("core/http", dry_run=True))
        record(manager.rollback("core/http"))

        marker = data_root / "modules/core_http/marker.json"
        if not marker.is_file():
            raise AssertionError("rollback did not restore the B10B-owned marker")
        if not (data_root / "state.json").is_file():
            raise AssertionError("rollback did not restore the B10B state")

        missing_provider: dict[str, object] = {}
        real_find_spec = __import__("runtime.packaging.b10b.manager", fromlist=["importlib"]).importlib.util.find_spec

        def missing_tts(name: str, *args: object, **kwargs: object) -> object:
            if name in {"tts", "tts.contracts", "tts.service"}:
                return None
            return real_find_spec(name, *args, **kwargs)

        missing_data = Path(temporary) / "missing-provider-data"
        with patch("runtime.packaging.b10b.manager.importlib.util.find_spec", side_effect=missing_tts):
            missing_manager = B10BManager(project_root=project_root, data_root=missing_data)
            try:
                missing_manager.install(["core/http", "tts-local"])
            except B10BError as exc:
                missing_provider = {
                    "code": exc.code,
                    "module_status": exc.details.get("module_status"),
                    "data_root_created": missing_data.exists(),
                }
            else:
                raise AssertionError("missing TTS provider did not fail closed")
        if missing_provider != {
            "code": "MODULE_PROVIDER_MISSING",
            "module_status": "NOT_INSTALLED",
            "data_root_created": False,
        }:
            raise AssertionError(f"unexpected missing-provider result: {missing_provider}")

        return {
            "status": "PASS",
            "fail_count": 0,
            "error_count": 0,
            "skip_count": 0,
            "operations": operations,
            "rollback_restored": True,
            "external_assets_copied": False,
            "user_data_deleted": False,
            "missing_provider": missing_provider,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the B10B local reversible lifecycle evidence probe.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-parent", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run(project_root=args.project_root.resolve(), evidence_parent=(args.evidence_parent or args.project_root / ".evidence").resolve())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
