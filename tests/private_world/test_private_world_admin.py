import json
from pathlib import Path

import pytest

import private_world_admin
from private_world_admin import (
    AdminConfirmationRequired,
    AdminOperationError,
    PrivateWorldAdmin,
    main,
)
from private_world_ledger import LedgerEvent, SQLitePrivateWorldLedger
from private_world_port import PrivateWorldSnapshot


def _seed(database: Path) -> None:
    SQLitePrivateWorldLedger(database).apply_once(
        LedgerEvent(
            event_id="event-1",
            delivery_id="delivery-1",
            event_type="reply_accepted",
            payload={"trust_delta": 1},
            occurred_at="2026-08-22T00:00:00+00:00",
        ),
        PrivateWorldSnapshot(version=1, trust=1),
    )


def test_export_requires_confirmation_and_writes_atomically(tmp_path: Path) -> None:
    database = tmp_path / "private.sqlite3"
    destination = tmp_path / "exports" / "private-world.json"
    _seed(database)
    admin = PrivateWorldAdmin(database)

    with pytest.raises(AdminConfirmationRequired):
        admin.export(destination)

    admin.export(destination, confirmed=True)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["snapshot"]["trust"] == 1
    assert payload["events"][0]["event_id"] == "event-1"
    assert list(destination.parent.glob(".private-world-*.tmp")) == []


def test_failed_export_preserves_existing_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "private.sqlite3"
    destination = tmp_path / "private-world.json"
    _seed(database)
    destination.write_text("existing", encoding="utf-8")

    def fail_replace(source: object, target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(private_world_admin.os, "replace", fail_replace)
    with pytest.raises(AdminOperationError):
        PrivateWorldAdmin(database).export(destination, confirmed=True)

    assert destination.read_text(encoding="utf-8") == "existing"
    assert list(tmp_path.glob(".private-world-*.tmp")) == []


def test_reset_requires_confirmation_and_keeps_empty_database(tmp_path: Path) -> None:
    database = tmp_path / "private.sqlite3"
    _seed(database)
    admin = PrivateWorldAdmin(database)

    with pytest.raises(AdminConfirmationRequired):
        admin.reset()
    admin.reset(confirmed=True)

    ledger = SQLitePrivateWorldLedger(database)
    assert ledger.snapshot() == PrivateWorldSnapshot()
    assert ledger.health()["event_count"] == 0
    assert database.is_file()


def test_delete_removes_database_and_sidecars_only_after_confirmation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private.sqlite3"
    _seed(database)
    sidecars = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"synthetic")
    admin = PrivateWorldAdmin(database)

    with pytest.raises(AdminConfirmationRequired):
        admin.delete()
    admin.delete(confirmed=True)

    assert not database.exists()
    assert all(not sidecar.exists() for sidecar in sidecars)


def test_cli_returns_stable_codes_without_printing_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "private-name.sqlite3"
    _seed(database)

    assert main(["--database", str(database), "reset"]) == 2
    assert main(["--database", str(database), "reset", "--yes"]) == 0
    output = capsys.readouterr().out
    assert "CONFIRMATION_REQUIRED" in output
    assert "OK" in output
    assert "private-name" not in output
