"""PowerShell-friendly B10A command-line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .errors import B10AError
from .manager import B10AManager
from .security import redact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b10a",
        description="Manage the B10A local skeleton without touching original assets.",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("manifest", help="show the declarative module manifest")

    install = subparsers.add_parser("install", help="install available module markers")
    install.add_argument("--module", action="append", dest="modules")
    install.add_argument("--all", action="store_true", dest="all_modules")

    upgrade = subparsers.add_parser("upgrade", help="upgrade an installed available module")
    upgrade.add_argument("--module", action="append", dest="modules", required=True)

    rollback = subparsers.add_parser("rollback", help="restore the latest reversible module transaction")
    rollback.add_argument("--module", required=True)

    uninstall = subparsers.add_parser("uninstall", help="show or apply an exact ownership uninstall plan")
    uninstall.add_argument("--module", action="append", dest="modules")
    uninstall.add_argument("--all", action="store_true", dest="all_modules")
    uninstall.add_argument(
        "--apply",
        action="store_true",
        help="apply the plan; without this flag uninstall is always a dry-run",
    )

    doctor = subparsers.add_parser("doctor", help="aggregate module, config and process health")
    doctor.add_argument("--strict", action="store_true", help="return non-zero when the aggregate is not HEALTHY")

    start = subparsers.add_parser("start", help="start the built-in local mock service")
    start.add_argument("--service", default="mock-http")
    start.add_argument("--port", type=int, default=None)
    start.add_argument("--exit-after", type=float, default=None, help=argparse.SUPPRESS)

    stop = subparsers.add_parser("stop", help="stop a tracked built-in local mock service")
    stop.add_argument("--service", default="mock-http")
    return parser


def _result(value: Any) -> None:
    print(json.dumps(redact(value), ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manager = B10AManager(
            project_root=args.project_root,
            data_root=args.data_root,
            manifest_path=args.manifest,
        )
        if args.command == "manifest":
            _result(manager.manifest_view())
            return 0
        if args.command == "install":
            _result(manager.install(args.modules, all_modules=args.all_modules))
            return 0
        if args.command == "upgrade":
            _result(manager.upgrade(args.modules))
            return 0
        if args.command == "rollback":
            _result(manager.rollback(args.module))
            return 0
        if args.command == "uninstall":
            _result(
                manager.uninstall(
                    args.modules,
                    all_modules=args.all_modules,
                    apply=args.apply,
                )
            )
            return 0
        if args.command == "doctor":
            report = manager.doctor()
            _result(report)
            return 0 if not args.strict or report["status"] == "HEALTHY" else 1
        if args.command == "start":
            _result(manager.start(args.service, port=args.port, exit_after=args.exit_after))
            return 0
        if args.command == "stop":
            _result(manager.stop(args.service))
            return 0
        parser.error(f"unknown command: {args.command}")
    except B10AError as exc:
        _result({"status": "ERROR", "code": exc.code, "message": exc.message, "details": exc.details})
        return exc.exit_code
    except KeyboardInterrupt:
        _result({"status": "ERROR", "code": "CANCELED", "message": "Operation canceled."})
        return 130
    return 2
