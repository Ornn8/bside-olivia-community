from __future__ import annotations

from pathlib import Path


TARGET = Path(__file__).with_name(
    "prepare_original_private_world_direct_management.py"
)


OLD = '''    path, _reason, enabled = resolve_private_world_database(environ)
    if not enabled or path is None or not path.is_file():
        return port, None, None, None
    ledger = getattr(committer, "ledger", None)
    if not isinstance(ledger, SQLitePrivateWorldLedger):
        return port, None, None, None
    try:
        command_service = PrivateWorldCommandService(ledger)
        private_world_commands = PrivateWorldControlAPI(
            ledger,
            command_service,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return port, None, None, None

    try:
        candidates = SQLitePrivateWorldCandidateStore(path)
    except (
        PrivateWorldCandidateError,
        LedgerWriteError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return port, None, private_world_commands, None
'''

NEW = '''    path, _reason, enabled = resolve_private_world_database(environ)
    if not enabled or path is None or not path.is_file():
        return port, None, None, None
    try:
        candidates = SQLitePrivateWorldCandidateStore(path)
    except (
        PrivateWorldCandidateError,
        LedgerWriteError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return port, None, None, None

    ledger = getattr(committer, "ledger", None)
    if not isinstance(ledger, SQLitePrivateWorldLedger):
        return port, candidates, None, None
    try:
        command_service = PrivateWorldCommandService(ledger)
        private_world_commands = PrivateWorldControlAPI(
            ledger,
            command_service,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return port, candidates, None, None
'''


def main() -> None:
    value = TARGET.read_text(encoding="utf-8")
    if value.count(OLD) != 1:
        raise RuntimeError("PRIVATE_WORLD_PREPARER_COMPAT_ANCHOR_INVALID")
    TARGET.write_text(value.replace(OLD, NEW, 1), encoding="utf-8")
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
