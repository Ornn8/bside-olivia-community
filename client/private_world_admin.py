"""Explicit local administration for the PrivateWorld SQLite ledger."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Mapping, Sequence

from runtime.memory.conversation_memory_identity import normalize_conversation_memory_user_id
from private_world_ledger import SQLitePrivateWorldLedger
from runtime.memory.private_world_runtime import resolve_private_world_database


class AdminConfirmationRequired(RuntimeError):
    code = "CONFIRMATION_REQUIRED"


class AdminOperationError(RuntimeError):
    code = "PRIVATE_WORLD_ADMIN_FAILED"


class AdminRequestConflict(AdminOperationError):
    code = "PRIVATE_WORLD_ADMIN_REQUEST_CONFLICT"


@dataclass(frozen=True)
class CurrentUserResetResult:
    status: str
    affected_event_count: int


class PrivateWorldAdmin:
    def __init__(self, database_path: Path, *, user_id: str = "local-user") -> None:
        path = Path(database_path)
        if str(path) in {"", "."} or path.exists() and path.is_dir():
            raise ValueError("an explicit database file path is required")
        try:
            user_id = normalize_conversation_memory_user_id(user_id)
        except ValueError as exc:
            raise ValueError("private world user_id is invalid") from exc
        self._database_path = path
        self._user_id = user_id

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

    @classmethod
    def reset_current_user(
        cls,
        *,
        environ: Mapping[str, str],
        user_id: str,
        request_id: str,
        reason: str,
        confirmed: bool = False,
    ) -> CurrentUserResetResult:
        """Resolve and reset only the normalized current-user ledger."""

        try:
            database_path, resolution_reason, enabled = resolve_private_world_database(
                environ,
                user_id=user_id,
            )
        except (OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise AdminOperationError("private world current-user scope is unavailable") from exc
        if not enabled or resolution_reason is not None or database_path is None:
            raise AdminOperationError("private world current-user scope is unavailable")
        return cls(database_path, user_id=user_id)._reset_selected_user(
            request_id=request_id,
            reason=reason,
            confirmed=confirmed,
        )

    def _reset_selected_user(
        self,
        *,
        request_id: str,
        reason: str,
        confirmed: bool,
    ) -> CurrentUserResetResult:
        """Reset the ledger selected by reset_current_user."""

        self._confirm(confirmed)
        self._require_database()
        if (
            not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id)
        ):
            raise ValueError("request_id is invalid")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("reason is invalid")
        normalized_reason = reason.strip()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": "reset_current_user", "reason": normalized_reason},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            with closing(sqlite3.connect(self._database_path, timeout=5)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS private_world_admin_operations (
                    user_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    affected_event_count INTEGER NOT NULL,
                    PRIMARY KEY (user_id, request_id)
                    )"""
                )
                existing = connection.execute(
                    """SELECT payload_fingerprint, affected_event_count
                    FROM private_world_admin_operations
                    WHERE user_id = ? AND request_id = ?""",
                    (self._user_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing[0] != fingerprint:
                        connection.rollback()
                        raise AdminRequestConflict("reset request conflicts")
                    connection.commit()
                    return CurrentUserResetResult("DUPLICATE", int(existing[1]))
                affected_event_count = int(
                    connection.execute("SELECT COUNT(*) FROM private_world_events").fetchone()[0]
                )
                connection.execute("DELETE FROM private_world_snapshots")
                connection.execute("DELETE FROM private_world_events")
                connection.execute(
                    """INSERT INTO private_world_admin_operations
                    (user_id, request_id, payload_fingerprint, affected_event_count)
                    VALUES (?, ?, ?, ?)""",
                    (self._user_id, request_id, fingerprint, affected_event_count),
                )
                connection.commit()
        except AdminRequestConflict:
            raise
        except sqlite3.Error as exc:
            raise AdminOperationError("private world current-user reset failed") from exc
        return CurrentUserResetResult("APPLIED", affected_event_count)

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
