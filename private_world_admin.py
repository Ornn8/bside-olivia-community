"""Explicit local administration for the PrivateWorld SQLite ledger."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Sequence

from private_world_ledger import SQLitePrivateWorldLedger


class AdminConfirmationRequired(RuntimeError):
    code = "CONFIRMATION_REQUIRED"


class AdminOperationError(RuntimeError):
    code = "PRIVATE_WORLD_ADMIN_FAILED"


class PrivateWorldAdmin:
    def __init__(self, database_path: Path) -> None:
        path = Path(database_path)
        if str(path) in {"", "."} or path.exists() and path.is_dir():
            raise ValueError("an explicit database file path is required")
        self._database_path = path

    @staticmethod
    def _confirm(confirmed: bool) -> None:
        if confirmed is not True:
            raise AdminConfirmationRequired("explicit confirmation is required")

    def _require_database(self) -> None:
        if not self._database_path.is_file():
            raise AdminOperationError("private world database is unavailable")

    def export(self, destination: Path, *, confirmed: bool = False) -> None:
        self._confirm(confirmed)
        self._require_database()
        output = Path(destination)
        if str(output) in {"", "."} or output.exists() and output.is_dir():
            raise ValueError("an explicit export file is required")
        if output.resolve() == self._database_path.resolve():
            raise ValueError("export must not replace the database")
        output.parent.mkdir(parents=True, exist_ok=True)
        ledger = SQLitePrivateWorldLedger(self._database_path)
        payload = {
            "schema": "p02.private-world-export.v1",
            "snapshot": ledger.snapshot().to_dict(),
            "events": [
                {
                    "event_id": event.event_id,
                    "delivery_id": event.delivery_id,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "occurred_at": event.occurred_at,
                }
                for event in ledger.events()
            ],
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output.parent,
                prefix=".private-world-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
        except (OSError, TypeError, ValueError) as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise AdminOperationError("private world export failed") from exc

    def reset(self, *, confirmed: bool = False) -> None:
        self._confirm(confirmed)
        self._require_database()
        try:
            with closing(sqlite3.connect(self._database_path, timeout=5)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("DELETE FROM private_world_snapshots")
                connection.execute("DELETE FROM private_world_events")
        except sqlite3.Error as exc:
            raise AdminOperationError("private world reset failed") from exc

    def delete(self, *, confirmed: bool = False) -> None:
        self._confirm(confirmed)
        self._require_database()
        try:
            for path in (
                self._database_path,
                Path(f"{self._database_path}-wal"),
                Path(f"{self._database_path}-shm"),
            ):
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise AdminOperationError("private world delete failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="private-world-admin")
    parser.add_argument("--database", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--yes", action="store_true")
    for name in ("reset", "delete"):
        command = commands.add_parser(name)
        command.add_argument("--yes", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    admin = PrivateWorldAdmin(arguments.database)
    try:
        if arguments.command == "export":
            admin.export(arguments.output, confirmed=arguments.yes)
        elif arguments.command == "reset":
            admin.reset(confirmed=arguments.yes)
        else:
            admin.delete(confirmed=arguments.yes)
    except AdminConfirmationRequired as exc:
        print(exc.code)
        return 2
    except AdminOperationError as exc:
        print(exc.code)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
