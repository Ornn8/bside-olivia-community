"""Small JSON CLI for the B10B declarative lifecycle manager."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .errors import B10BError
from .manager import B10BManager
from .security import redact


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _changes(values: list[str]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise B10BError("CUSTOMIZATION_FORMAT", "Customization values must use KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise B10BError("CUSTOMIZATION_FORMAT", "Customization keys must not be empty.")
        if key in changes:
            raise B10BError("CUSTOMIZATION_DUPLICATE", "A customization key was provided more than once.", {"field": key})
        changes[key] = _json_value(value)
    return changes


def _result(value: Any) -> None:
    print(json.dumps(redact(value), ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b10b",
        description="Manage B10B-owned local module metadata without copying external assets.",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("manifest", help="show the validated declarative module manifest")

    for name in ("status", "health", "doctor"):
        command = sub.add_parser(name, help="show truthful module and provider health")
        command.add_argument("--strict", action="store_true", help="return non-zero unless aggregate health is HEALTHY")
        command.set_defaults(normalized_command="health")

    install = sub.add_parser("install", help="install available module metadata")
    install.add_argument("--module", action="append", dest="modules")
    install.add_argument("--all", action="store_true", dest="all_modules")
    install.add_argument("--profile", help="install one verified external-runtime profile")
    install.add_argument("--reinstall", action="store_true", help="refresh profile metadata and routing without touching external assets")
    install.add_argument("--dry-run", action="store_true")

    reinstall = sub.add_parser("reinstall", help="refresh one verified external-runtime profile")
    reinstall.add_argument("--profile", required=True)
    reinstall.add_argument("--dry-run", action="store_true")

    for name, enabled in (("enable", True), ("disable", False)):
        command = sub.add_parser(name, help=f"{name} a module route")
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--module")
        target.add_argument("--profile")
        command.add_argument("--dry-run", action="store_true")
        command.set_defaults(normalized_command=name, enabled=enabled)

    uninstall = sub.add_parser("uninstall", help="show or apply an exact B10B-owned uninstall plan")
    uninstall.add_argument("--module", action="append", dest="modules")
    uninstall.add_argument("--all", action="store_true", dest="all_modules")
    uninstall.add_argument("--profile", help="disable and uninstall one profile's B10B metadata only")
    apply_group = uninstall.add_mutually_exclusive_group()
    apply_group.add_argument("--apply", action="store_true", help="apply the plan; otherwise uninstall is a dry-run")
    apply_group.add_argument("--dry-run", action="store_true", help="explicitly keep uninstall in dry-run mode")

    rollback = sub.add_parser("rollback", help="restore the latest reversible module transaction")
    target = rollback.add_mutually_exclusive_group(required=True)
    target.add_argument("--module")
    target.add_argument("--profile")
    rollback.add_argument("--dry-run", action="store_true")

    customize = sub.add_parser("customize", help="write declared module settings and external references")
    customize.add_argument("--module", required=True)
    customize.add_argument("--set", dest="changes", action="append", default=[], metavar="KEY=VALUE")
    customize.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "normalized_command", args.command)
    try:
        manager = B10BManager(
            project_root=args.project_root,
            data_root=args.data_root,
            manifest_path=args.manifest,
        )
        if command == "manifest":
            _result(manager.manifest_view())
            return 0
        if command == "health":
            report = manager.health()
            _result(report)
            return 0 if not getattr(args, "strict", False) or report["status"] == "HEALTHY" else 1
        if command == "install":
            if args.profile:
                if args.modules or args.all_modules:
                    raise B10BError("CLI_INPUT_INVALID", "--profile cannot be combined with --module or --all.")
                _result(manager.install_profile(args.profile, dry_run=args.dry_run, reinstall=args.reinstall))
                return 0
            _result(manager.install(args.modules, all_modules=args.all_modules, dry_run=args.dry_run))
            return 0
        if command == "reinstall":
            _result(manager.install_profile(args.profile, dry_run=args.dry_run, reinstall=True))
            return 0
        if command in {"enable", "disable"}:
            if args.profile:
                if command == "disable":
                    _result(manager.disable_profile(args.profile, dry_run=args.dry_run))
                    return 0
                raise B10BError("CLI_INPUT_INVALID", "Profiles are enabled by install/reinstall; use disable --profile to stop routing.")
            operation = manager.enable if command == "enable" else manager.disable
            _result(operation(args.module, dry_run=args.dry_run))
            return 0
        if command == "uninstall":
            if args.profile:
                if args.modules or args.all_modules:
                    raise B10BError("CLI_INPUT_INVALID", "--profile cannot be combined with --module or --all.")
                _result(manager.uninstall_profile(args.profile, dry_run=not args.apply))
                return 0
            _result(manager.uninstall(args.modules, all_modules=args.all_modules, dry_run=not args.apply))
            return 0
        if command == "rollback":
            if args.profile:
                _result(manager.rollback_profile(args.profile, dry_run=args.dry_run))
                return 0
            _result(manager.rollback(args.module, dry_run=args.dry_run))
            return 0
        if command == "customize":
            _result(manager.customize(args.module, _changes(args.changes), dry_run=args.dry_run))
            return 0
        parser.error(f"unknown command: {args.command}")
    except B10BError as exc:
        _result({"status": "ERROR", "code": exc.code, "message": exc.message, "details": exc.details})
        return exc.exit_code
    except (OSError, ValueError) as exc:
        _result({"status": "ERROR", "code": "CLI_INPUT_INVALID", "message": str(exc)})
        return 2
    except KeyboardInterrupt:
        _result({"status": "ERROR", "code": "CANCELED", "message": "Operation canceled."})
        return 130
    return 2
