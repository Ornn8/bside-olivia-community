from __future__ import annotations

import argparse
import json
from pathlib import Path

from .component_package import ComponentPackageBuildError, build_component_package
from .component_update import (
    ComponentUpdateError,
    rollback_component_update,
)
from .full_patch import PatchInstallError, discover_steam_install, install_full_patch, load_manifest, uninstall_full_patch, validate_official_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="olivia-full-patch")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--official-root", type=Path)
    install = sub.add_parser("install")
    install.add_argument("--official-root", type=Path)
    install.add_argument("--destination", type=Path, required=True)
    install.add_argument("--payload", type=Path, required=True)
    install.add_argument("--manifest", type=Path, default=Path(__file__).with_name("full-patch-manifest.json"))
    install.add_argument("--port", type=int, default=8899)
    remove = sub.add_parser("uninstall")
    remove.add_argument("--installation", type=Path, required=True)
    remove.add_argument("--apply", action="store_true")
    update = sub.add_parser("apply-update")
    update.add_argument("--installation", type=Path, required=True)
    update.add_argument("--package", type=Path, required=True)
    update.add_argument("--manifest-sha256", required=True)
    build_update = sub.add_parser("build-update")
    build_update.add_argument("--source", type=Path, required=True)
    build_update.add_argument("--output", type=Path, required=True)
    build_update.add_argument("--version", required=True)
    build_update.add_argument("--source-commit", required=True)
    rollback = sub.add_parser("rollback-update")
    rollback.add_argument("--installation", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            source = (args.official_root or discover_steam_install()).resolve()
            manifest = load_manifest(Path(__file__).with_name("full-patch-manifest.json"))
            version, feapp, webplayer = validate_official_source(source, manifest)
            result = {"status": "SUPPORTED", "official_root": str(source), "client_version": version, "feapp": str(feapp), "webplayer": str(webplayer), "live_status": manifest["live_status"], "media_status": manifest["media_status"]}
        elif args.command == "install":
            source = (args.official_root or discover_steam_install()).resolve()
            result = install_full_patch(source, args.destination, args.payload, args.manifest, port=args.port)
        elif args.command == "uninstall":
            result = uninstall_full_patch(args.installation, apply=args.apply)
        elif args.command == "apply-update":
            raise ComponentUpdateError("UPDATE_ACTION_UNAVAILABLE")
        elif args.command == "build-update":
            result = build_component_package(
                args.source,
                args.output,
                version=args.version,
                expected_source_commit=args.source_commit,
            )
        else:
            result = rollback_component_update(args.installation)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (PatchInstallError, ComponentPackageBuildError, ComponentUpdateError) as exc:
        print(json.dumps({"status": "ERROR", "code": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
