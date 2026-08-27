"""Fail-closed install, routing, uninstall and rollback for B10B modules."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import B10BError
from .extensions import ProviderContext, provider_health
from .manifest import DEFAULT_MANIFEST_PATH, load_manifest, module_map
from .profiles import VERIFIED_LOCAL, profile_settings, verified_local_visual_settings
from .security import (
    ensure_regular_owned_file,
    is_external_reference,
    is_sensitive_key,
    managed_copy_paths_are_distinct,
    redact,
    safe_owned_path,
    ensure_safe_root,
    validate_relative_path,
)


STATE_SCHEMA_VERSION = "b10b.state.v1"
TRANSACTION_SCHEMA_VERSION = "b10b.transaction.v1"
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_STATE_KEYS = {"schema_version", "project_root", "modules", "last_transactions", "transaction_history"}
_RECORD_KEYS = {
    "module_id",
    "version",
    "availability",
    "marker_path",
    "config_path",
    "enabled",
    "lifecycle",
    "installed_at",
}
_TRANSACTION_KEYS = {
    "schema_version",
    "operation",
    "module_id",
    "created_at",
    "before_record",
    "after_record",
    "before_files",
    "after_files",
    "status",
}
_MARKER_KEYS = {"schema_version", "module_id", "version", "status", "enabled", "managed_by"}
_OPERATIONS = {"install", "enable", "disable", "customize", "uninstall"}
_HISTORY_KEYS = {"module", "operation", "transaction", "at"}
_MANAGED_COPY_KEYS = {"source", "destination", "sha256", "preserve_source"}
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise B10BError("WRITE_FAILED", "B10B local state could not be written.") from exc
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def _read_json(path: Path, *, code: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise B10BError(code, message) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise B10BError(code, message) from exc

    if not isinstance(value, dict):
        raise B10BError(code, message)
    return value


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise B10BError("INVALID_TRANSACTION", "A B10B transaction contains invalid file data.") from exc


class B10BManager:
    """Operate only on manifest-owned metadata under a dedicated data root."""

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        data_root: Path | str | None = None,
        manifest_path: Path | str | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve(strict=False)
        if not self.project_root.is_dir():
            raise B10BError("PROJECT_ROOT_INVALID", "The B10B project root must be an existing directory.")
        self.data_root = (
            Path(data_root).expanduser().absolute()
            if data_root is not None
            else (self.project_root / ".b10b").absolute()
        )
        if self.data_root == self.project_root:
            raise B10BError("DATA_ROOT_INVALID", "The B10B data root must be a dedicated directory.")
        ensure_safe_root(self.data_root)
        self.manifest_path = (
            Path(manifest_path).expanduser().resolve(strict=False)
            if manifest_path is not None
            else DEFAULT_MANIFEST_PATH
        )
        self.manifest = load_manifest(self.manifest_path)
        self.modules = module_map(self.manifest)
        self.state_path = self.data_root / "state.json"

    # ---------- state, paths and snapshots ----------

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "project_root": str(self.project_root),
            "modules": {},
            "last_transactions": {},
            "transaction_history": [],
        }
    def _state(self) -> dict[str, Any]:
        ensure_safe_root(self.data_root)
        if not os.path.lexists(self.state_path):
            return self._initial_state()
        ensure_regular_owned_file(self.state_path, field="state_path")
        state = _read_json(
            self.state_path,
            code="STATE_INVALID",
            message="B10B local state is unreadable or invalid JSON.",
        )
        if set(state) - _STATE_KEYS or state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise B10BError("STATE_INVALID", "B10B local state has an unsupported schema version.")
        if state.get("project_root") != str(self.project_root):
            raise B10BError("STATE_INVALID", "B10B local state belongs to a different project root.")
        if (
            not isinstance(state.get("modules"), dict)
            or not isinstance(state.get("last_transactions"), dict)
            or not isinstance(state.get("transaction_history"), list)
        ):
            raise B10BError("STATE_INVALID", "B10B local state has an invalid registry shape.")
        unknown = sorted(set(state["modules"]) - set(self.modules))
        if unknown:
            raise B10BError("STATE_INVALID", "B10B state references unknown modules.", {"modules": unknown})
        for module_id, record in state["modules"].items():
            self._validate_record(module_id, record, check_marker=True)
        for module_id, relative in state["last_transactions"].items():
            if module_id not in self.modules or not isinstance(relative, str):
                raise B10BError("STATE_INVALID", "B10B state has an invalid transaction reference.")
            validate_relative_path(relative, field="last_transactions")
            if not relative.startswith("transactions/"):
                raise B10BError("STATE_INVALID", "B10B state has an unsafe transaction reference.")
            try:
                _path, transaction = self._load_transaction(relative)
            except B10BError as exc:
                raise B10BError(
                    "STATE_INVALID",
                    "B10B state references a transaction that cannot be loaded.",
                    {"module": module_id, "transaction": relative},
                ) from exc
            if transaction.get("module_id") != module_id:
                raise B10BError("STATE_INVALID", "B10B state transaction ownership is inconsistent.", {"module": module_id})
            current_record = state["modules"].get(module_id)
            if transaction.get("operation") == "uninstall":

                if current_record is not None or transaction.get("after_record") is not None:
                    raise B10BError("STATE_INVALID", "B10B uninstall transaction does not match current state.", {"module": module_id})
            elif current_record is None or transaction.get("after_record") != current_record:
                raise B10BError("STATE_INVALID", "B10B transaction does not match current module state.", {"module": module_id})
        for item in state["transaction_history"]:
            if not isinstance(item, dict) or set(item) != _HISTORY_KEYS:
                raise B10BError("STATE_INVALID", "B10B state has an invalid transaction history entry.")
            if (
                not isinstance(item.get("module"), str)
                or item.get("module") not in self.modules
                or not isinstance(item.get("operation"), str)
                or item.get("operation") not in _OPERATIONS | {"rollback"}
            ):
                raise B10BError("STATE_INVALID", "B10B state has an invalid transaction history entry.")
            if (
                not isinstance(item.get("transaction"), str)
                or not isinstance(item.get("at"), str)
                or not item["at"].strip()
            ):
                raise B10BError("STATE_INVALID", "B10B state has an invalid transaction history entry.")
            validate_relative_path(item["transaction"], field="transaction_history.transaction")
            if not item["transaction"].startswith("transactions/"):
                raise B10BError("STATE_INVALID", "B10B state has an unsafe transaction history reference.")
            try:
                _path, transaction = self._load_transaction(item["transaction"])
            except B10BError as exc:
                raise B10BError(
                    "STATE_INVALID",
                    "B10B transaction history references a transaction that cannot be loaded.",
                    {"module": item["module"], "transaction": item["transaction"]},
                ) from exc
            if transaction.get("module_id") != item["module"]:
                raise B10BError("STATE_INVALID", "B10B transaction history ownership is inconsistent.")
            if item["operation"] != "rollback" and transaction.get("operation") != item["operation"]:
                raise B10BError("STATE_INVALID", "B10B transaction history operation is inconsistent.")
        return state

    def _validate_record(self, module_id: str, record: Any, *, check_marker: bool = False) -> None:
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise B10BError("STATE_INVALID", "B10B state has an invalid module record.", {"module": module_id})
        if record.get("module_id") != module_id:
            raise B10BError("STATE_INVALID", "B10B state module record identity is invalid.", {"module": module_id})
        module = self._module(module_id)
        if record.get("version") != module["version"] or record.get("availability") != module["availability"]:
            raise B10BError("STATE_INVALID", "B10B state module record does not match the manifest.", {"module": module_id})
        if not isinstance(record.get("marker_path"), str) or not isinstance(record.get("enabled"), bool):
            raise B10BError("STATE_INVALID", "B10B state module record has invalid types.", {"module": module_id})
        validate_relative_path(record["marker_path"], field=f"{module_id}.marker_path")
        if record["marker_path"] not in module["ownership"]["owned_paths"]:
            raise B10BError("STATE_INVALID", "B10B state marker path is not manifest-owned.", {"module": module_id})
        config_path = record.get("config_path")
        if config_path is not None:
            if not isinstance(config_path, str) or config_path not in module["ownership"]["owned_paths"]:
                raise B10BError("STATE_INVALID", "B10B state config path is not manifest-owned.", {"module": module_id})
        if record.get("lifecycle") not in {"installed", "enabled"}:
            raise B10BError("STATE_INVALID", "B10B state module lifecycle is invalid.", {"module": module_id})
        if record["lifecycle"] == "enabled" and not record["enabled"]:
            raise B10BError("STATE_INVALID", "B10B enabled lifecycle does not match routing state.", {"module": module_id})
        if record["lifecycle"] == "installed" and record["enabled"]:
            raise B10BError("STATE_INVALID", "B10B installed lifecycle does not match routing state.", {"module": module_id})
        if not isinstance(record.get("installed_at"), str) or not record["installed_at"]:
            raise B10BError("STATE_INVALID", "B10B state installation timestamp is invalid.", {"module": module_id})
        if check_marker:
            self._validate_marker(module_id, record)
            if record.get("config_path") is not None:
                config_path = safe_owned_path(self.data_root, record["config_path"], field=f"{module_id}.config_path")
                if os.path.lexists(config_path):
                    self._module_config(module)

    def _validate_marker(self, module_id: str, record: dict[str, Any]) -> None:
        module = self._module(module_id)
        path = safe_owned_path(self.data_root, record["marker_path"], field=f"{module_id}.marker_path")
        if not os.path.lexists(path):
            raise B10BError("STATE_INVALID", "B10B state claims an installed module whose marker is missing.", {"module": module_id})
        ensure_regular_owned_file(path, field=f"{module_id}.marker_path")
        marker = _read_json(
            path,
            code="STATE_INVALID",
            message="B10B module marker is unreadable or invalid JSON.",
        )
        if set(marker) != _MARKER_KEYS:
            raise B10BError("STATE_INVALID", "B10B module marker has an unsupported schema.", {"module": module_id})
        if (
            marker.get("schema_version") != "b10b.module-marker.v1"
            or marker.get("module_id") != module_id
            or marker.get("version") != module["version"]
            or marker.get("status") != "installed"
            or marker.get("enabled") is not False
            or marker.get("managed_by") != "B10B lifecycle"
        ):
            raise B10BError("STATE_INVALID", "B10B module marker is not associated with its manifest record.", {"module": module_id})

    def _ensure_data_root(self) -> None:
        ensure_safe_root(self.data_root)
        if self.data_root.exists() and not self.data_root.is_dir():
            raise B10BError("DATA_ROOT_INVALID", "The B10B data root is not a directory.")
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise B10BError("DATA_ROOT_INVALID", "The B10B data root could not be created.") from exc


    def _write_state(self, state: dict[str, Any]) -> None:
        self._ensure_data_root()
        if os.path.lexists(self.state_path):
            ensure_regular_owned_file(self.state_path, field="state_path")
        _atomic_json(self.state_path, state)

    def _module(self, module_id: str) -> dict[str, Any]:
        try:
            return self.modules[module_id]
        except KeyError as exc:
            raise B10BError("UNKNOWN_MODULE", "The requested B10B module is not declared.", {"module": module_id}) from exc

    def _owned_paths(self, module: dict[str, Any]) -> list[tuple[str, Path]]:
        result: list[tuple[str, Path]] = []
        for relative in module["ownership"]["owned_paths"]:
            normalized = validate_relative_path(relative, field=f"{module['id']}.owned_path")
            result.append((normalized, safe_owned_path(self.data_root, normalized, field=f"{module['id']}.owned_path")))
        return result

    def _snapshot(self, module: dict[str, Any]) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for relative, path in self._owned_paths(module):
            if not os.path.lexists(path):
                snapshot[relative] = None
                continue
            ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise B10BError("OWNERSHIP_READ_FAILED", "A B10B-owned file could not be read safely.") from exc
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise B10BError("OWNERSHIP_TOO_LARGE", "A B10B-owned file is too large for rollback.")
            snapshot[relative] = _encode(data)
        return snapshot

    def _write_owned(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as exc:
            raise B10BError("WRITE_FAILED", "A B10B-owned file could not be written.") from exc
        finally:
            if temporary:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _restore_snapshot(self, module: dict[str, Any], snapshot: dict[str, str | None]) -> None:
        self._validate_snapshot(module, snapshot, field="snapshot")
        for relative, encoded in snapshot.items():
            path = safe_owned_path(self.data_root, relative, field=f"{module['id']}.owned_path")
            if encoded is None:
                if os.path.lexists(path):
                    ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
                    try:
                        path.unlink()
                    except OSError as exc:
                        raise B10BError("DELETE_FAILED", "A B10B-owned file could not be removed.") from exc
            else:
                if os.path.lexists(path):
                    ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
                self._write_owned(path, _decode(encoded))

    def _validate_snapshot(self, module: dict[str, Any], snapshot: Any, *, field: str) -> None:
        if not isinstance(snapshot, dict):
            raise B10BError("INVALID_TRANSACTION", f"B10B {field} is not an object.")
        expected = {relative for relative, _path in self._owned_paths(module)}
        if set(snapshot) != expected:
            raise B10BError("INVALID_TRANSACTION", f"B10B {field} does not match manifest-owned paths.")
        for relative, encoded in snapshot.items():
            if encoded is None:
                continue
            if not isinstance(encoded, str):
                raise B10BError("INVALID_TRANSACTION", f"B10B {field} contains invalid file data.")
            data = _decode(encoded)
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise B10BError("INVALID_TRANSACTION", f"B10B {field} contains an oversized file snapshot.")
            validate_relative_path(relative, field=f"{field}.{relative}")

    def _assert_snapshot(self, module: dict[str, Any], expected: dict[str, str | None]) -> None:
        self._validate_snapshot(module, expected, field="expected_snapshot")
        if self._snapshot(module) != expected:
            raise B10BError(
                "DIRTY_OWNED_PATH",
                "The rollback target changed outside the B10B lifecycle.",
                {"module": module["id"]},
            )

    def _transaction_path(self, relative: str) -> Path:
        normalized = validate_relative_path(relative, field="transaction_path")
        if not normalized.startswith("transactions/"):

            raise B10BError("INVALID_TRANSACTION", "B10B rollback may only read transaction records.")
        return safe_owned_path(self.data_root, normalized, field="transaction_path")

    def _write_transaction(
        self,
        *,
        operation: str,
        module: dict[str, Any],
        before_record: dict[str, Any] | None,
        after_record: dict[str, Any] | None,
        before_files: dict[str, str | None],
        after_files: dict[str, str | None],
    ) -> str:
        if operation not in _OPERATIONS:
            raise B10BError("INVALID_TRANSACTION", "B10B cannot record an unsupported lifecycle operation.")
        if before_record is not None:
            self._validate_record(module["id"], before_record)
        if after_record is not None:
            self._validate_record(module["id"], after_record)
        self._validate_snapshot(module, before_files, field="before_files")
        self._validate_snapshot(module, after_files, field="after_files")
        relative = f"transactions/{uuid.uuid4().hex}-{module['id'].replace('/', '-')}.json"
        _atomic_json(
            self._transaction_path(relative),
            {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                "operation": operation,
                "module_id": module["id"],
                "created_at": _now(),
                "before_record": copy.deepcopy(before_record),
                "after_record": copy.deepcopy(after_record),
                "before_files": before_files,
                "after_files": after_files,
                "status": "active",
            },
        )
        return relative

    def _load_transaction(self, relative: str) -> tuple[Path, dict[str, Any]]:
        path = self._transaction_path(relative)
        ensure_regular_owned_file(path, field="transaction_path")
        transaction = _read_json(path, code="TRANSACTION_MISSING", message="The requested rollback transaction is missing.")
        if set(transaction) != _TRANSACTION_KEYS or transaction.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
            raise B10BError("INVALID_TRANSACTION", "The B10B rollback transaction schema is unsupported.")
        operation = transaction.get("operation")
        module_id = transaction.get("module_id")
        if operation not in _OPERATIONS or not isinstance(module_id, str):
            raise B10BError("INVALID_TRANSACTION", "The B10B rollback transaction identity is invalid.")
        module = self._module(module_id)
        if not isinstance(transaction.get("created_at"), str) or not transaction["created_at"]:
            raise B10BError("INVALID_TRANSACTION", "The B10B rollback transaction timestamp is invalid.")
        if transaction.get("status") != "active":
            raise B10BError("INVALID_TRANSACTION", "The B10B rollback transaction is not active.")
        before_record = transaction.get("before_record")
        after_record = transaction.get("after_record")
        if before_record is not None:
            self._validate_record(module_id, before_record)
        if after_record is not None:
            self._validate_record(module_id, after_record)
        self._validate_snapshot(module, transaction.get("before_files"), field="before_files")
        self._validate_snapshot(module, transaction.get("after_files"), field="after_files")
        return path, transaction

    # ---------- selection and basic install ----------

    def _selected(self, module_ids: list[str] | None, *, all_modules: bool = False) -> list[str]:
        selected = list(self.modules) if all_modules else list(module_ids or [])
        if not selected:
            raise B10BError("MODULE_REQUIRED", "Specify at least one --module or use --all.")
        if len(set(selected)) != len(selected):
            raise B10BError("MODULE_DUPLICATE", "A module was selected more than once.")
        for module_id in selected:
            self._module(module_id)
        return selected

    def _install_preflight(self, selected: list[str], state: dict[str, Any]) -> list[dict[str, Any]]:
        selected_set = set(selected)
        result: list[dict[str, Any]] = []
        for module_id in selected:
            module = self._module(module_id)
            if module["availability"] != "available":
                raise B10BError(
                    "MODULE_PENDING" if module["availability"] == "pending" else "MODULE_UNAVAILABLE",
                    "This module is not available for installation in the current composition.",
                    {"module": module_id, "availability": module["availability"], "implementation_batch": module.get("implementation_batch")},
                )
            if module_id == "tts-local" and not self._tts_provider_installed():
                raise B10BError(
                    "MODULE_PROVIDER_MISSING",
                    "The B06 TTS provider package is not present in this composition.",
                    {
                        "module": module_id,
                        "implementation_batch": "B06",
                        "module_status": "NOT_INSTALLED",
                        "provider_status": "UNAVAILABLE",
                        "reason_code": "MODULE_PROVIDER_MISSING",
                    },
                )
            existing = state["modules"].get(module_id)
            if existing is not None:

                if existing.get("version") != module["version"]:
                    raise B10BError("UPGRADE_REQUIRED", "The installed module has a different manifest version; use upgrade.", {"module": module_id})
                marker = self._owned_paths(module)[0][1]
                ensure_regular_owned_file(marker, field=f"{module_id}.marker_path")
                if not marker.is_file():
                    raise B10BError("OWNERSHIP_MISSING", "The installed module marker is missing.", {"module": module_id})
                result.append({"module": module_id, "status": "NO_OP"})
                continue
            missing = [dependency for dependency in module["dependencies"] if dependency not in state["modules"] and dependency not in selected_set]
            if missing:
                raise B10BError("MISSING_DEPENDENCY", "A required module is not installed or selected.", {"module": module_id, "dependencies": missing})
            for relative, path in self._owned_paths(module):
                if os.path.lexists(path):
                    ensure_regular_owned_file(path, field=f"{module_id}.owned_path")
                    raise B10BError("OWNERSHIP_CONFLICT", "A manifest-owned path already exists without state.", {"module": module_id, "path": relative})
            result.append({"module": module_id, "status": "INSTALL", "owned_paths": [relative for relative, _ in self._owned_paths(module)]})
        return result

    @staticmethod
    def _tts_provider_installed() -> bool:
        try:
            return all(importlib.util.find_spec(name) is not None for name in ("tts", "tts.contracts", "tts.service"))
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    @staticmethod
    def _record(module: dict[str, Any], marker_relative: str, *, enabled: bool = False) -> dict[str, Any]:
        return {
            "module_id": module["id"],
            "version": module["version"],
            "availability": module["availability"],
            "marker_path": marker_relative,
            "config_path": next(
                (path for path in module["ownership"]["owned_paths"] if path.endswith("/config.json")), None
            ),
            "enabled": enabled,
            "lifecycle": "enabled" if enabled else "installed",
            "installed_at": _now(),
        }

    def install(self, module_ids: list[str] | None = None, *, all_modules: bool = False, dry_run: bool = False) -> dict[str, Any]:
        selected = self._selected(module_ids, all_modules=all_modules)
        state = self._state()
        plan = self._install_preflight(selected, state)
        to_install = [item for item in plan if item["status"] == "INSTALL"]
        if dry_run:
            return {"operation": "install", "status": "DRY_RUN", "dry_run": True, "modules": plan, "external_assets_copied": False}
        if not to_install:
            return {"operation": "install", "status": "NO_OP", "dry_run": False, "modules": plan, "external_assets_copied": False}
        original_state = copy.deepcopy(state)
        state_was_present = os.path.lexists(self.state_path)
        transactions: list[str] = []
        changed: list[dict[str, Any]] = []
        try:
            for item in to_install:
                module = self._module(item["module"])
                before_files = self._snapshot(module)
                changed.append({"module": module, "before_files": before_files, "after_files": {}})
                marker_relative, marker_path = self._owned_paths(module)[0]
                _atomic_json(marker_path, {
                    "schema_version": "b10b.module-marker.v1",
                    "module_id": module["id"],
                    "version": module["version"],
                    "status": "installed",
                    "enabled": False,
                    "managed_by": "B10B lifecycle",
                })
                after_files = self._snapshot(module)
                changed[-1]["after_files"] = after_files
                record = self._record(module, marker_relative)
                transaction = self._write_transaction(
                    operation="install", module=module, before_record=None, after_record=record,
                    before_files=before_files, after_files=after_files,
                )
                transactions.append(transaction)
                state["modules"][module["id"]] = record
                state["last_transactions"][module["id"]] = transaction
                state.setdefault("transaction_history", []).append(
                    {"module": module["id"], "operation": "install", "transaction": transaction, "at": _now()}
                )
            self._write_state(state)
        except Exception as exc:
            self._recover_after_failure(
                original_state=original_state,
                state_was_present=state_was_present,
                changes=changed,
                transactions=transactions,
                primary=exc,
            )
            raise
        return {"operation": "install", "status": "INSTALLED", "dry_run": False, "modules": plan, "external_assets_copied": False}

    @staticmethod
    def _profile_module_ids(profile: str) -> list[str]:
        if profile != VERIFIED_LOCAL:
            raise B10BError("UNKNOWN_PROFILE", "The requested B10B profile is not declared.", {"profile": profile})
        return ["core/http", "asr-local", "visual-driver", "visual-livetalking", "tts-local"]

    def install_profile(self, profile: str, *, dry_run: bool = False, reinstall: bool = False) -> dict[str, Any]:
        """Compose a verified local provider profile without touching its assets."""

        modules = self._profile_module_ids(profile)
        settings = profile_settings(profile, manifest=self.manifest)  # fail before lifecycle metadata is written
        if dry_run:
            return {
                "operation": "reinstall" if reinstall else "install",
                "profile": profile,
                "status": "DRY_RUN",
                "dry_run": True,
                "modules": modules,
                "configured_modules": sorted(settings),
                "external_assets_copied": False,
                "external_assets_deleted": False,
            }
        installed = self.install(modules, dry_run=False)
        customized = [
            self._customize(module_id, values, _verified_profile=profile)
            for module_id, values in settings.items()
        ]
        enabled = [self.enable(module_id) for module_id in modules]
        return {
            "operation": "reinstall" if reinstall else "install",
            "profile": profile,
            "status": "REINSTALLED" if reinstall else "INSTALLED",
            "dry_run": False,
            "install": installed,
            "customize": customized,
            "enable": enabled,
            "external_assets_copied": False,
            "external_assets_deleted": False,
            "user_data_deleted": False,
        }

    def disable_profile(self, profile: str, *, dry_run: bool = False) -> dict[str, Any]:
        modules = list(reversed(self._profile_module_ids(profile)))
        state = self._state()
        results = [
            self.disable(module_id, dry_run=dry_run)
            for module_id in modules
            if module_id in state["modules"] and bool(state["modules"][module_id].get("enabled", False))
        ]
        return {
            "operation": "disable",
            "profile": profile,
            "status": "DRY_RUN" if dry_run else "DISABLED",
            "dry_run": dry_run,
            "modules": results,
            "routing_only": True,
            "external_assets_copied": False,
            "external_assets_deleted": False,
        }

    def uninstall_profile(self, profile: str, *, dry_run: bool = True) -> dict[str, Any]:
        modules = self._profile_module_ids(profile)
        if dry_run:
            return {
                "operation": "uninstall",
                "profile": profile,
                "status": "DRY_RUN",
                "dry_run": True,
                "modules": list(reversed(modules)),
                "would_disable": list(reversed(modules)),
                "exact_delete_policy": "Only B10B-owned metadata below data_root would be removed.",
                "external_assets_deleted": False,
                "user_data_deleted": False,
            }
        disabled = self.disable_profile(profile, dry_run=False)
        removed = self.uninstall(modules, dry_run=False)
        return {
            "operation": "uninstall",
            "profile": profile,
            "status": "UNINSTALLED",
            "dry_run": False,
            "disable": disabled,
            "uninstall": removed,
            "external_assets_deleted": False,
            "user_data_deleted": False,
        }

    def rollback_profile(self, profile: str, *, dry_run: bool = False) -> dict[str, Any]:
        modules = self._profile_module_ids(profile)
        results = [self.rollback(module_id, dry_run=dry_run) for module_id in modules]
        return {
            "operation": "rollback",
            "profile": profile,
            "status": "DRY_RUN" if dry_run else "ROLLED_BACK",
            "dry_run": dry_run,
            "modules": results,
            "external_assets_copied": False,
            "external_assets_deleted": False,
            "user_data_deleted": False,
        }

    # ---------- routing state ----------

    def _transition(self, module_id: str, *, enabled: bool, dry_run: bool) -> dict[str, Any]:
        module = self._module(module_id)
        state = self._state()
        record = state["modules"].get(module_id)
        if record is None:
            raise B10BError("NOT_INSTALLED", "Only an installed module can change routing.", {"module": module_id})

        current = bool(record.get("enabled", False))
        target_status = "ENABLED" if enabled else "DISABLED"
        if current == enabled:
            return {"operation": "enable" if enabled else "disable", "status": "NO_OP", "module": module_id, "dry_run": dry_run}
        if enabled:
            missing = [dependency for dependency in module["dependencies"] if dependency not in state["modules"]]
            if missing:
                raise B10BError("MISSING_DEPENDENCY", "A required module is not installed.", {"module": module_id, "dependencies": missing})
            disabled = [
                dependency
                for dependency in module["dependencies"]
                if not bool(state["modules"].get(dependency, {}).get("enabled", False))
            ]
            if disabled:
                raise B10BError("DEPENDENCY_DISABLED", "Enable required dependencies before this module.", {"module": module_id, "dependencies": disabled})
        else:
            dependents = [
                other["id"]
                for other in self.modules.values()
                if module_id in other["dependencies"]
                and bool(state["modules"].get(other["id"], {}).get("enabled", False))
            ]
            if dependents:
                raise B10BError("ENABLED_DEPENDENTS", "Disable dependent modules before disabling this module.", {"module": module_id, "dependents": dependents})
        if dry_run:
            return {
                "operation": "enable" if enabled else "disable",
                "status": "DRY_RUN",
                "dry_run": True,
                "module": module_id,
                "target": target_status,
                "routing_only": True,
            }
        original_state = copy.deepcopy(state)
        state_was_present = os.path.lexists(self.state_path)
        before_files = self._snapshot(module)
        transaction: str | None = None
        try:
            after_record = copy.deepcopy(record)
            after_record["enabled"] = enabled
            after_record["lifecycle"] = "enabled" if enabled else "installed"
            # Enable/disable intentionally changes state routing only.  The
            # marker and all external references remain byte-for-byte intact.
            transaction = self._write_transaction(
                operation="enable" if enabled else "disable",
                module=module,
                before_record=record,
                after_record=after_record,
                before_files=before_files,
                after_files=before_files,
            )
            state["modules"][module_id] = after_record
            state["last_transactions"][module_id] = transaction
            state.setdefault("transaction_history", []).append({"module": module_id, "operation": "enable" if enabled else "disable", "transaction": transaction, "at": _now()})
            self._write_state(state)
        except Exception as exc:
            self._recover_after_failure(
                original_state=original_state,
                state_was_present=state_was_present,
                changes=[{"module": module, "before_files": before_files, "after_files": before_files}],
                transactions=[transaction] if transaction else [],
                primary=exc,
            )
            raise
        return {
            "operation": "enable" if enabled else "disable",
            "status": target_status,
            "module": module_id,
            "dry_run": False,
            "routing_only": True,
        }

    def enable(self, module_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        return self._transition(module_id, enabled=True, dry_run=dry_run)

    def disable(self, module_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        return self._transition(module_id, enabled=False, dry_run=dry_run)

    # ---------- exact uninstall and rollback ----------

    def _restore_transition(
        self,
        module: dict[str, Any],
        before_files: dict[str, str | None],
        after_files: dict[str, str | None],
    ) -> None:
        current = self._snapshot(module)
        for relative in before_files:
            if current.get(relative) not in {before_files.get(relative), after_files.get(relative)}:
                raise B10BError("DIRTY_OWNED_PATH", "Automatic recovery refused a user-modified owned path.", {"module": module["id"], "path": relative})
        self._restore_snapshot(module, before_files)

    def _recover_after_failure(
        self,
        *,
        original_state: dict[str, Any],
        state_was_present: bool,
        changes: list[dict[str, Any]],
        transactions: list[str],
        primary: Exception,

    ) -> None:
        """Recover files and registry, or fail closed without claiming installation."""

        recovery_errors: list[dict[str, str]] = []
        for change in reversed(changes):
            try:
                self._restore_transition(
                    change["module"],
                    change["before_files"],
                    change["after_files"],
                )
            except Exception as exc:  # recovery must be observable, never silently ignored
                recovery_errors.append(
                    {
                        "module": change["module"]["id"],
                        "reason": exc.code if isinstance(exc, B10BError) else type(exc).__name__,
                    }
                )
        for relative in transactions:
            try:
                self._transaction_path(relative).unlink()
            except FileNotFoundError:
                continue
            except Exception as exc:
                recovery_errors.append(
                    {
                        "transaction": relative,
                        "reason": exc.code if isinstance(exc, B10BError) else type(exc).__name__,
                    }
                )

        if recovery_errors:
            # A failed file recovery may not leave registry records that claim
            # ownership of a marker which is absent or uncertain.  Preserve
            # unrelated module records, remove affected claims, and remove
            # references to transactions that were part of the failed change.
            safe_state = copy.deepcopy(original_state)
            affected = {change["module"]["id"] for change in changes}
            safe_state["modules"] = {
                module_id: record
                for module_id, record in safe_state.get("modules", {}).items()
                if module_id not in affected
            }
            safe_state["last_transactions"] = {
                module_id: relative
                for module_id, relative in safe_state.get("last_transactions", {}).items()
                if module_id not in affected and relative not in transactions
            }
            safe_state["transaction_history"] = [
                item
                for item in safe_state.get("transaction_history", [])
                if item.get("module") not in affected and item.get("transaction") not in transactions
            ]
            try:
                if state_was_present:
                    self._write_state(safe_state)
                elif os.path.lexists(self.state_path):
                    ensure_regular_owned_file(self.state_path, field="state_path")
                    self.state_path.unlink()
            except Exception as exc:
                recovery_errors.append(
                    {
                        "state": "state_path",
                        "reason": exc.code if isinstance(exc, B10BError) else type(exc).__name__,
                    }
                )
            raise B10BError(
                "ATOMIC_RECOVERY_FAILED",
                "B10B could not restore the failed lifecycle operation; affected module claims were failed closed.",
                {"recovery_errors": recovery_errors},
            ) from primary

        try:
            if state_was_present:
                self._write_state(original_state)
            elif os.path.lexists(self.state_path):
                ensure_regular_owned_file(self.state_path, field="state_path")
                self.state_path.unlink()
        except Exception as exc:
            raise B10BError(
                "ATOMIC_RECOVERY_FAILED",
                "B10B restored owned files but could not restore its registry atomically.",
                {"reason": exc.code if isinstance(exc, B10BError) else type(exc).__name__},
            ) from primary

    def uninstall(
        self,
        module_ids: list[str] | None = None,
        *,
        all_modules: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        selected = self._selected(module_ids, all_modules=all_modules)
        state = self._state()
        selected_set = set(selected)
        plans: list[dict[str, Any]] = []
        for module_id in selected:
            module = self._module(module_id)
            record = state["modules"].get(module_id)
            existing = [relative for relative, encoded in self._snapshot(module).items() if encoded is not None]

            if record is None:
                if existing:
                    raise B10BError("OWNERSHIP_CONFLICT", "Owned files exist without an installed state record; refusing deletion.", {"module": module_id, "paths": existing})
                plans.append({"module": module_id, "status": "NOT_INSTALLED", "owned_paths": []})
                continue
            dependents = [
                other["id"]
                for other in self.modules.values()
                if module_id in other["dependencies"]
                and other["id"] in state["modules"]
                and other["id"] not in selected_set
            ]
            if dependents:
                raise B10BError("DEPENDENTS_INSTALLED", "Uninstall the installed dependent modules first.", {"module": module_id, "dependents": dependents})
            if bool(record.get("enabled", False)):
                raise B10BError("MODULE_ENABLED", "Disable the module before uninstalling it.", {"module": module_id})
            plans.append({
                "module": module_id,
                "status": "READY",
                "owned_paths": existing,
                "preserved_boundaries": module["ownership"]["preserved_boundaries"],
                "external_assets_deleted": False,
                "managed_external_copies": self._managed_copy_plan(module),
            })
        if dry_run:
            return {
                "operation": "uninstall",
                "status": "DRY_RUN",
                "dry_run": True,
                "modules": plans,
                "exact_delete_policy": "Only listed manifest-owned regular files under data_root are deletable.",
                "external_assets_deleted": False,
                "managed_external_copies_deleted": 0,
                "user_data_deleted": False,
            }
        changed = [plan for plan in plans if plan["status"] == "READY"]
        if not changed:
            return {"operation": "uninstall", "status": "NO_OP", "dry_run": False, "modules": plans, "external_assets_deleted": False, "managed_external_copies_deleted": 0, "user_data_deleted": False}
        original_state = copy.deepcopy(state)
        state_was_present = os.path.lexists(self.state_path)
        changes: list[dict[str, Any]] = []
        transactions: list[str] = []
        deleted_managed_copies: list[dict[str, str]] = []
        try:
            for plan in changed:
                module = self._module(plan["module"])
                before_record = copy.deepcopy(state["modules"][module["id"]])
                before_files = self._snapshot(module)
                after_files = {relative: None for relative in before_files}
                change = {"module": module, "before_files": before_files, "after_files": after_files}
                changes.append(change)
                deleted_managed_copies.extend(self._delete_managed_copies(plan["managed_external_copies"]))
                for relative, path in self._owned_paths(module):
                    if os.path.lexists(path):
                        ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
                        try:
                            path.unlink()
                        except OSError as exc:
                            raise B10BError("DELETE_FAILED", "A B10B-owned file could not be removed.", {"path": relative}) from exc
                transaction = self._write_transaction(
                    operation="uninstall", module=module, before_record=before_record, after_record=None,
                    before_files=before_files, after_files=after_files,
                )
                transactions.append(transaction)
                state["modules"].pop(module["id"], None)
                state["last_transactions"][module["id"]] = transaction
                state.setdefault("transaction_history", []).append({"module": module["id"], "operation": "uninstall", "transaction": transaction, "at": _now()})
            self._write_state(state)
        except Exception as exc:
            try:
                self._restore_managed_copies(deleted_managed_copies)
            except Exception as restore_exc:
                exc = B10BError(
                    "MANAGED_COPY_RECOVERY_FAILED",
                    "B10B could not restore managed external copies after an uninstall failure.",
                    {"reason": restore_exc.code if isinstance(restore_exc, B10BError) else type(restore_exc).__name__},
                )
            self._recover_after_failure(
                original_state=original_state,
                state_was_present=state_was_present,
                changes=changes,
                transactions=transactions,
                primary=exc,
            )
            raise
        for plan in changed:
            count = sum(1 for item in plan["managed_external_copies"] if item["status"] == "READY")
            plan["managed_external_copies_deleted"] = count
            plan["external_assets_deleted"] = count > 0
        return {"operation": "uninstall", "status": "UNINSTALLED", "dry_run": False, "modules": plans, "external_assets_deleted": bool(deleted_managed_copies), "managed_external_copies_deleted": len(deleted_managed_copies), "user_data_deleted": False}

    def rollback(self, module_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        module = self._module(module_id)
        state = self._state()
        relative = state.get("last_transactions", {}).get(module_id)
        if not isinstance(relative, str):
            raise B10BError("NO_ROLLBACK", "No reversible B10B transaction is recorded for the module.", {"module": module_id})
        _path, transaction = self._load_transaction(relative)
        if transaction.get("module_id") != module_id or transaction.get("status") != "active":
            raise B10BError("INVALID_TRANSACTION", "The rollback transaction does not belong to the requested module.")

        before_files = transaction.get("before_files")
        after_files = transaction.get("after_files")
        if not isinstance(before_files, dict) or not isinstance(after_files, dict):
            raise B10BError("INVALID_TRANSACTION", "The rollback transaction has invalid file snapshots.")
        self._assert_snapshot(module, after_files)
        if dry_run:
            return {
                "operation": "rollback",
                "status": "DRY_RUN",
                "dry_run": True,
                "module": module_id,
                "transaction": relative,
                "external_assets_copied": False,
            }
        original_state = copy.deepcopy(state)
        state_was_present = os.path.lexists(self.state_path)
        try:
            self._restore_snapshot(module, before_files)
            before_record = transaction.get("before_record")
            if before_record is None:
                state["modules"].pop(module_id, None)
            elif isinstance(before_record, dict):
                state["modules"][module_id] = copy.deepcopy(before_record)
            else:
                raise B10BError("INVALID_TRANSACTION", "The rollback transaction has an invalid state record.")
            state["last_transactions"].pop(module_id, None)
            state.setdefault("transaction_history", []).append({"module": module_id, "operation": "rollback", "transaction": relative, "at": _now()})
            self._write_state(state)
        except Exception as exc:
            self._recover_after_failure(
                original_state=original_state,
                state_was_present=state_was_present,
                changes=[{"module": module, "before_files": after_files, "after_files": before_files}],
                transactions=[],
                primary=exc,
            )
            raise
        return {"operation": "rollback", "status": "ROLLED_BACK", "module": module_id, "transaction": relative}

    def customize(
        self,
        module_id: str,
        changes: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._customize(module_id, changes, dry_run=dry_run, _verified_profile=None)

    def _customize(
        self,
        module_id: str,
        changes: dict[str, Any],
        *,
        dry_run: bool = False,
        _verified_profile: str | None = None,
    ) -> dict[str, Any]:
        module = self._module(module_id)
        state = self._state()
        if module_id not in state["modules"]:
            raise B10BError("NOT_INSTALLED", "Only an installed module can be customized.", {"module": module_id})
        if not isinstance(changes, dict) or not changes:
            raise B10BError("CUSTOMIZATION_REQUIRED", "Provide at least one customization field.")
        if any(not isinstance(key, str) or not key.strip() for key in changes):
            raise B10BError("CUSTOMIZATION_INVALID", "Customization field names must be non-empty strings.")
        if _verified_profile not in {None, VERIFIED_LOCAL}:
            raise B10BError("UNKNOWN_PROFILE", "The requested B10B profile is not declared.")
        sensitive = sorted(key for key in changes if is_sensitive_key(key))
        if sensitive:
            raise B10BError("CUSTOMIZATION_SENSITIVE", "Secret-bearing customization fields are not accepted.", {"fields": sensitive})
        allowed = set(module.get("customizable", []))
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise B10BError("CUSTOMIZATION_FIELD_UNKNOWN", "The module does not declare these customization fields.", {"module": module_id, "fields": unknown})
        references = {str(item.get("name")): item for item in module.get("external_references", []) if isinstance(item, dict)}
        for key, value in changes.items():
            reference = references.get(key)
            if reference is None:
                continue
            if not isinstance(value, str):
                raise B10BError("EXTERNAL_REFERENCE_INVALID", "External references must be strings.", {"field": key})
            drive_policy = reference.get("drive_policy")
            path_policy = reference.get("path_policy")
            valid_external = is_external_reference(value)
            valid_logical = is_external_reference(value, policy="logical_asset")
            if drive_policy == "local-absolute-non-root" and not valid_external:
                raise B10BError("EXTERNAL_REFERENCE_INVALID", "External model/runtime references must be absolute local Windows paths.", {"field": key})
            if path_policy == "logical-or-external" and not (valid_external or valid_logical):
                raise B10BError("EXTERNAL_REFERENCE_INVALID", "Visual references must be logical asset IDs or absolute local Windows paths.", {"field": key})
            if drive_policy is None and path_policy is None and not valid_external:
                raise B10BError("EXTERNAL_REFERENCE_INVALID", "External asset references must be absolute local Windows paths.", {"field": key})
        current = self._module_config(module)
        settings = {**current, **changes}
        self._validate_existing_settings(module, settings)
        try:
            json.dumps(settings, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise B10BError("CUSTOMIZATION_INVALID", "Customization values must be JSON-compatible.") from exc
        config_path = next(
            (path for path in module["ownership"]["owned_paths"] if path.endswith("/config.json")), None
        )
        if config_path is None:
            raise B10BError("INVALID_MANIFEST", "The module has no owned configuration path.")
        config_target = safe_owned_path(self.data_root, config_path, field=f"{module_id}.config_path")
        result = {
            "operation": "customize",
            "module": module_id,
            "status": "DRY_RUN" if dry_run else "CUSTOMIZED",
            "dry_run": dry_run,
            "fields": sorted(changes),
            "external_assets_copied": False,
        }
        if dry_run:

            return result
        original_state = copy.deepcopy(state)
        state_was_present = os.path.lexists(self.state_path)
        before_files = self._snapshot(module)
        changes_for_recovery: list[dict[str, Any]] = [
            {"module": module, "before_files": before_files, "after_files": before_files}
        ]
        transaction: str | None = None
        try:
            document = {
                "schema_version": "b10b.module-config.v1",
                "module_id": module_id,
                "settings": settings,
                "external_assets_copied": False,
                "managed_by": "B10B lifecycle",
            }
            if _verified_profile is not None:
                document["profile"] = _verified_profile
            _atomic_json(config_target, document)
            after_files = self._snapshot(module)
            changes_for_recovery[0]["after_files"] = after_files
            record = copy.deepcopy(state["modules"][module_id])
            transaction = self._write_transaction(
                operation="customize", module=module, before_record=record, after_record=record,
                before_files=before_files, after_files=after_files,
            )
            state["last_transactions"][module_id] = transaction
            state.setdefault("transaction_history", []).append({"module": module_id, "operation": "customize", "transaction": transaction, "at": _now()})
            self._write_state(state)
        except Exception as exc:
            self._recover_after_failure(
                original_state=original_state,
                state_was_present=state_was_present,
                changes=changes_for_recovery,
                transactions=[transaction] if transaction else [],
                primary=exc,
            )
            raise
        return result

    # ---------- truthful health and manifest views ----------

    def _validate_existing_settings(self, module: dict[str, Any], settings: Any) -> None:
        if not isinstance(settings, dict):
            raise B10BError("CONFIG_INVALID", "A B10B module configuration settings object is invalid.")
        sensitive: list[str] = []

        def collect_sensitive(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    field = f"{prefix}.{key}" if prefix else str(key)
                    if is_sensitive_key(str(key)):
                        sensitive.append(field)
                    else:
                        collect_sensitive(nested, field)
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    collect_sensitive(nested, f"{prefix}[{index}]")

        collect_sensitive(settings)
        if sensitive:
            raise B10BError(
                "CONFIG_INVALID",
                "Existing B10B configuration contains secret-bearing fields.",
                {"fields": sorted(sensitive)},
            )
        allowed = set(module.get("customizable", []))
        unknown = sorted(set(settings) - allowed)
        if unknown:
            raise B10BError(
                "CONFIG_INVALID",
                "Existing B10B configuration contains undeclared fields.",
                {"module": module["id"], "fields": unknown},
            )
        references = {
            str(item.get("name")): item
            for item in module.get("external_references", [])
            if isinstance(item, dict)
        }
        for key, value in settings.items():
            reference = references.get(key)
            if reference is None:
                continue
            if not isinstance(value, str):
                raise B10BError("CONFIG_INVALID", "Existing external references must be strings.", {"field": key})
            drive_policy = reference.get("drive_policy")
            path_policy = reference.get("path_policy")
            valid_external = is_external_reference(value)
            valid_logical = is_external_reference(value, policy="logical_asset")
            if drive_policy == "local-absolute-non-root" and not valid_external:
                raise B10BError(
                    "CONFIG_INVALID",
                    "Existing external references must be absolute local Windows paths.",
                    {"field": key},
                )
            if path_policy == "logical-or-external" and not (valid_external or valid_logical):
                raise B10BError(
                    "CONFIG_INVALID",
                    "Existing visual references must be logical asset IDs or absolute local Windows paths.",
                    {"field": key},

                )
            if drive_policy is None and path_policy is None and not valid_external:
                raise B10BError(
                    "CONFIG_INVALID",
                    "Existing external references must be absolute local Windows paths.",
                    {"field": key},
                )
        self._validate_managed_external_copies(module, settings)

    @staticmethod
    def _validate_managed_external_copies(module: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
        raw = settings.get("managed_external_copies", [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise B10BError("CONFIG_INVALID", "managed_external_copies must be a list.", {"module": module["id"]})
        normalized: list[dict[str, Any]] = []
        destinations: set[str] = set()
        sources: list[Path] = []
        destination_paths: list[Path] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or set(item) != _MANAGED_COPY_KEYS:
                raise B10BError("CONFIG_INVALID", "A managed external copy declaration is invalid.", {"module": module["id"], "index": index})
            source = item["source"]
            destination = item["destination"]
            digest = item["sha256"]
            if not isinstance(source, str) or not is_external_reference(source):
                raise B10BError("CONFIG_INVALID", "Managed copy sources must be absolute local Windows paths.", {"index": index})
            if not isinstance(destination, str) or not is_external_reference(destination):
                raise B10BError("CONFIG_INVALID", "Managed copy destinations must be absolute local Windows paths.", {"index": index})
            source_key = os.path.normcase(os.path.normpath(source))
            destination_key = os.path.normcase(os.path.normpath(destination))
            if source_key == destination_key or destination_key in destinations:
                raise B10BError("CONFIG_INVALID", "Managed copy destinations must be unique and distinct from sources.", {"index": index})
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                not managed_copy_paths_are_distinct(source_path, destination_path)
                or any(not managed_copy_paths_are_distinct(source_path, prior) for prior in destination_paths)
                or any(not managed_copy_paths_are_distinct(prior, destination_path) for prior in sources)
                or any(not managed_copy_paths_are_distinct(prior, destination_path) for prior in destination_paths)
            ):
                raise B10BError("CONFIG_INVALID", "Managed copy sources and destinations must not be physical path aliases.", {"index": index})
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise B10BError("CONFIG_INVALID", "Managed copy sha256 must be a 64-character hexadecimal digest.", {"index": index})
            if item["preserve_source"] is not True:
                raise B10BError("CONFIG_INVALID", "Managed copies must preserve their external source files.", {"index": index})
            destinations.add(destination_key)
            sources.append(source_path)
            destination_paths.append(destination_path)
            normalized.append({"source": source, "destination": destination, "sha256": digest.lower(), "preserve_source": True})
        return normalized

    def _managed_copy_plan(self, module: dict[str, Any]) -> list[dict[str, Any]]:
        settings = self._module_config(module)
        declarations = self._validate_managed_external_copies(module, settings)
        plan: list[dict[str, Any]] = []
        for item in declarations:
            destination = Path(item["destination"])
            entry = dict(item)
            if not managed_copy_paths_are_distinct(source, destination):
                entry["status"] = "PATH_ALIAS"
            elif not source.is_file():
                entry["status"] = "SOURCE_MISSING"
            elif not destination.exists():
                entry["status"] = "MISSING"
            elif not destination.is_file():
                entry["status"] = "NOT_REGULAR"
            else:
                actual = self._managed_copy_sha256(destination)
                entry["actual_sha256"] = actual
                entry["status"] = "READY" if actual == item["sha256"] else "HASH_MISMATCH"
            plan.append(entry)
        return plan

    @staticmethod
    def _managed_copy_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise B10BError("MANAGED_COPY_READ_FAILED", "A managed external copy could not be read.", {"path": str(path)}) from exc
        return digest.hexdigest()

    def _delete_managed_copies(self, plan: list[dict[str, Any]]) -> list[dict[str, str]]:
        ready: list[dict[str, str]] = []
        protected_sources = tuple(Path(item["source"]) for item in plan)
        for item in plan:
            if item["status"] in {"MISSING"}:
                continue
            if item["status"] != "READY":
                raise B10BError("MANAGED_COPY_NOT_SAFE", "A managed external copy is not hash-verified and cannot be deleted.", {"destination": item["destination"], "status": item["status"]})
            source = Path(item["source"])
            destination = Path(item["destination"])
            if any(not managed_copy_paths_are_distinct(protected, destination) for protected in protected_sources):
                raise B10BError("MANAGED_COPY_NOT_SAFE", "A managed external copy is a path alias and cannot be deleted.", {"destination": item["destination"]})
            try:
                destination.unlink()
            except OSError as exc:
                raise B10BError("MANAGED_COPY_DELETE_FAILED", "A hash-verified managed external copy could not be removed.", {"destination": item["destination"]}) from exc
            ready.append({"source": item["source"], "destination": item["destination"], "sha256": item["sha256"]})
        return ready

    @staticmethod
    def _restore_managed_copies(deleted: list[dict[str, str]]) -> None:
        protected_sources = tuple(Path(item["source"]) for item in deleted)
        for item in deleted:
            source = Path(item["source"])
            destination = Path(item["destination"])
            if destination.exists():
                continue
            if any(not managed_copy_paths_are_distinct(protected, destination) for protected in protected_sources):
                raise B10BError("MANAGED_COPY_NOT_SAFE", "A managed external copy recovery path is a path alias.", {"destination": item["destination"]})
            if not source.is_file():
                raise B10BError("MANAGED_COPY_SOURCE_MISSING", "A managed copy source disappeared during recovery.", {"source": item["source"]})
            if B10BManager._managed_copy_sha256(source) != item["sha256"]:
                raise B10BError("MANAGED_COPY_SOURCE_CHANGED", "A managed copy source changed during recovery.", {"source": item["source"]})
            destination.parent.mkdir(parents=True, exist_ok=True)
            if any(not managed_copy_paths_are_distinct(protected, destination) for protected in protected_sources):
                raise B10BError("MANAGED_COPY_NOT_SAFE", "A managed external copy recovery path is a path alias.", {"destination": item["destination"]})
            shutil.copyfile(source, destination)


    def _module_config_document(self, module: dict[str, Any]) -> dict[str, Any]:
        config_path = next(
            (path for path in module["ownership"]["owned_paths"] if path.endswith("/config.json")), None
        )
        if config_path is None:
            return {}
        path = safe_owned_path(self.data_root, config_path, field=f"{module['id']}.config_path")
        if not os.path.lexists(path):
            return {}
        ensure_regular_owned_file(path, field=f"{module['id']}.config_path")
        value = _read_json(path, code="CONFIG_INVALID", message="A B10B module configuration is invalid.")
        if set(value) - {
            "schema_version",
            "module_id",
            "settings",
            "external_assets_copied",
            "managed_by",
            "profile",
        }:
            raise B10BError("CONFIG_INVALID", "A B10B module configuration contains unsupported fields.")
        if value.get("schema_version") != "b10b.module-config.v1" or value.get("module_id") != module["id"]:
            raise B10BError("CONFIG_INVALID", "A B10B module configuration identity is invalid.")
        if value.get("external_assets_copied") is not False or value.get("managed_by") != "B10B lifecycle":
            raise B10BError("CONFIG_INVALID", "A B10B module configuration ownership marker is invalid.")
        if value.get("profile") not in {None, VERIFIED_LOCAL}:
            raise B10BError("CONFIG_INVALID", "A B10B module configuration profile marker is invalid.")
        settings = value.get("settings")
        self._validate_existing_settings(module, settings)
        return value

    def _module_config(self, module: dict[str, Any]) -> dict[str, Any]:
        document = self._module_config_document(module)
        return copy.deepcopy(document.get("settings", {}))

    def active_module_settings(self, module_id: str) -> dict[str, Any] | None:
        """Return validated, non-secret settings only for an enabled module.

        This is the read-only bridge boundary for B08 composition.  It never
        writes lifecycle state or performs provider reachability checks.
        """

        module = self._module(module_id)
        record = self._state()["modules"].get(module_id)
        if record is None or not bool(record.get("enabled", False)):
            return None
        return copy.deepcopy(self._module_config(module))

    def verified_active_module_settings(
        self,
        module_id: str,
        *,
        profile: str = VERIFIED_LOCAL,
    ) -> dict[str, Any] | None:
        """Return an enabled module only while its verified profile still matches."""

        module = self._module(module_id)
        record = self._state()["modules"].get(module_id)
        if record is None or not bool(record.get("enabled", False)):
            return None
        document = self._module_config_document(module)
        if document.get("profile") != profile:
            raise B10BError(
                "VERIFIED_PROFILE_UNVERIFIED",
                "The active module was not installed by the requested verified profile.",
                {"profile": profile, "component": module_id},
            )
        current = copy.deepcopy(document.get("settings", {}))
        if module_id == "visual-livetalking":
            expected = verified_local_visual_settings(manifest=self.manifest)
        else:
            expected = profile_settings(profile, manifest=self.manifest).get(module_id)
        if current != expected:
            mismatches = sorted(set(current) | set(expected or {}))
            raise B10BError(
                "VERIFIED_PROFILE_PIN_MISMATCH",
                "The active module no longer matches its verified profile.",
                {"profile": profile, "component": module_id, "mismatches": mismatches},
            )
        return current

    def _health_asr(self, module: dict[str, Any]) -> dict[str, Any]:
        try:
            from asr.config import AsrConfig
            from asr.provider import create_provider

            raw = self._module_config(module)
            config = AsrConfig.from_mapping(raw) if raw else AsrConfig.from_env()
            provider = create_provider(config)
            provider_status = dict(provider.status())
        except B10BError:
            raise
        except Exception:
            return {"status": "UNAVAILABLE", "reason": "ASR_HEALTH_ERROR", "provider": "unknown"}
        provider_name = str(provider_status.get("provider", config.provider))
        native_provider = provider_name == "nemotron-speech-cpp"
        is_native_asr = provider_status.get("is_asr") is True or native_provider
        if (
            provider_status.get("status") == "available"
            and is_native_asr
            and provider_status.get("ready") is True
        ):
            return {"status": "HEALTHY", "provider": provider_name, "ready": bool(provider_status.get("ready")), "capability": "native.asr"}
        if provider_status.get("status") == "available" and is_native_asr:
            return {
                "status": "UNAVAILABLE",
                "provider": provider_name,
                "ready": False,
                "capability": "native.asr",
                "reason": "PROVIDER_NOT_READY",
            }
        if provider_name == "text-fallback":
            return {
                "status": "DEGRADED",
                "provider": provider_name,
                "ready": True,
                "capability": "text.input.fallback",
                "reason": "TEXT_INPUT_FALLBACK_ONLY",
                "native_asr": "UNAVAILABLE",
            }
        return {
            "status": "UNAVAILABLE",
            "provider": provider_name,
            "ready": False,
            "capability": "native.asr",
            "reason": str(provider_status.get("reason", provider_status.get("reason_code", "ASR_UNAVAILABLE"))),
        }

    @staticmethod
    def _health_visual() -> dict[str, Any]:
        try:
            module = importlib.import_module("visual_driver")
            if not hasattr(module, "VisualDriver") or not hasattr(module, "OriginalVisualFrame"):
                raise ImportError("B07 public contract missing")
        except Exception:
            return {"status": "UNAVAILABLE", "reason": "VISUAL_CONTRACT_MISSING"}
        return {
            "status": "DEGRADED",
            "contract_status": "HEALTHY",
            "backend_registered": False,
            "reason": "VISUAL_BACKEND_NOT_REGISTERED",
            "fallback": "original_frame",
        }

    def _health_live(self) -> dict[str, Any]:
        """Delegate B08 bridge health without constructing or probing it."""

        try:
            from .live_bridge import bridge_health

            return bridge_health(self)
        except B10BError:
            raise
        except Exception:
            return {"status": "UNAVAILABLE", "reason": "LIVE_BRIDGE_HEALTH_ERROR", "ready": False}

    def _health_livetalking(self, module: dict[str, Any]) -> dict[str, Any]:
        """Validate B11 references, then delegate readiness to the thin adapter."""

        try:
            from runtime.visual.livetalking import LiveTalkingConfig, runtime_health
        except (ImportError, ModuleNotFoundError):
            return {"status": "UNAVAILABLE", "reason": "B11_ADAPTER_MISSING"}
        raw = self._module_config(module)
        if not raw:
            return {
                "status": "UNAVAILABLE",
                "reason": "RUNTIME_NOT_CONFIGURED",
                "ready": False,
                "external_assets_copied": False,
                "generated_media_committed": False,
            }
        try:

            runtime_settings = {key: value for key, value in raw.items() if key != "managed_external_copies"}
            config = LiveTalkingConfig(**runtime_settings)
        except (TypeError, ValueError):
            return {
                "status": "UNAVAILABLE",
                "reason": "CONFIG_INVALID",
                "ready": False,
                "external_assets_copied": False,
                "generated_media_committed": False,
            }
        return runtime_health(config)

    @staticmethod
    def _health_memory() -> dict[str, Any]:
        try:
            memory = importlib.import_module("memory_port")
            if not hasattr(memory, "MemoryPort"):
                raise ImportError("MemoryPort contract missing")
        except Exception:
            return {"status": "UNAVAILABLE", "reason": "MEMORY_CONTRACT_MISSING"}
        return {
            "status": "DEGRADED",
            "contract_status": "HEALTHY",
            "reason": "MEMORY_PROFILE_NOT_CONFIGURED",
            "legacy_letters": "READ_ONLY",
        }

    def _health_tts(self, module: dict[str, Any]) -> dict[str, Any]:
        """Call B06's public TTS service only after it is composed into main."""

        if not self._tts_provider_installed():
            return {
                "status": "UNAVAILABLE",
                "reason": "MODULE_PROVIDER_MISSING",
                "code": "MODULE_PROVIDER_MISSING",
                "module_status": "NOT_INSTALLED",
                "b06_composed": False,
            }
        try:
            from tts.contracts import TTSConfig
            from tts.service import TTSService

            raw = self._module_config(module)
            config = TTSConfig.from_mapping({"profile": "b10b-tts", **raw})
            service = TTSService(config)
            try:
                provider_status = dict(service.health())
            finally:
                service.close()
        except (ModuleNotFoundError, ImportError):
            return {
                "status": "UNAVAILABLE",
                "reason": "MODULE_PROVIDER_MISSING",
                "code": "MODULE_PROVIDER_MISSING",
                "module_status": "NOT_INSTALLED",
                "b06_composed": False,
            }
        except B10BError:
            raise
        except Exception:
            return {"status": "UNAVAILABLE", "reason": "TTS_HEALTH_ERROR", "b06_composed": True}
        raw_status = str(provider_status.get("status", "unavailable")).lower()
        if raw_status == "available":
            result: dict[str, Any] = {"status": "HEALTHY", "b06_composed": True}
        elif raw_status == "disabled":
            result = {"status": "DISABLED", "b06_composed": True}
        else:
            result = {"status": "UNAVAILABLE", "b06_composed": True}
        for key in ("provider", "reason_code", "fallback", "license_id", "streaming", "offline_only"):
            if key in provider_status:
                result[key] = provider_status[key]
        return result

    def _health_item(self, module: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        module_id = module["id"]
        record = state["modules"].get(module_id)
        item: dict[str, Any] = {
            "id": module_id,
            "availability": module["availability"],
            "implementation_batch": module.get("implementation_batch"),
            "installed": record is not None,
            "enabled": bool(record.get("enabled", False)) if record else False,
        }
        if module["availability"] == "pending":
            item.update(status="PENDING", reason=module.get("reason", "future implementation is not composed"))
            return item
        if module["availability"] == "unavailable":
            item.update(status="UNAVAILABLE", reason=module.get("reason", "module is unavailable"))
            return item
        if record is None:
            item.update(status="NOT_INSTALLED", reason="install the module to enable it")
            return item
        if not bool(record.get("enabled", False)):
            item.update(status="DISABLED", reason="module routing is disabled")
            return item
        health = module["health"]
        kind = health.get("kind")
        if kind == "project_files":
            missing = [
                required

                for required in health.get("required_files", [])
                if not (self.project_root / Path(*required.split("/"))).is_file()
            ]
            item.update(status="UNAVAILABLE" if missing else "HEALTHY")
            if missing:
                item["reason"] = "PROJECT_FILES_MISSING"
                item["missing_files"] = missing
            return item
        adapter = health.get("adapter")
        if adapter == "asr":
            item["provider_health"] = self._health_asr(module)
        elif adapter == "visual":
            item["provider_health"] = self._health_visual()
        elif adapter == "live":
            item["provider_health"] = self._health_live()
        elif adapter == "visual-livetalking":
            item["provider_health"] = self._health_livetalking(module)
        elif adapter == "memory":
            item["provider_health"] = self._health_memory()
        elif adapter == "tts":
            item["provider_health"] = self._health_tts(module)
        elif kind == "extension":
            item["provider_health"] = provider_health(
                str(module.get("extension", {}).get("provider_id", adapter or "")),
                ProviderContext(self.project_root, self.data_root, module_id, self._module_config(module)),
            )
        else:
            item["provider_health"] = {"status": "UNAVAILABLE", "reason": "HEALTH_ADAPTER_MISSING"}
        provider_status = str(item["provider_health"].get("status", "UNAVAILABLE"))
        item["status"] = provider_status if provider_status in {"HEALTHY", "DEGRADED", "UNAVAILABLE", "DISABLED"} else "UNAVAILABLE"
        return item

    def health(self) -> dict[str, Any]:
        state = self._state()
        modules = [self._health_item(module, state) for module in self.manifest["modules"]]
        summary = {
            key: sum(1 for item in modules if item["status"] == value)
            for key, value in {
                "healthy": "HEALTHY",
                "degraded": "DEGRADED",
                "unavailable": "UNAVAILABLE",
                "disabled": "DISABLED",
                "not_installed": "NOT_INSTALLED",
                "pending": "PENDING",
            }.items()
        }
        status = "HEALTHY" if all(item["status"] == "HEALTHY" for item in modules) else "DEGRADED"
        return {
            "schema_version": "b10b.health.v1",
            "status": status,
            "summary": summary,
            "modules": modules,
            "preservation": {
                "external_assets_copied": False,
                "user_data_deleted": False,
                "legacy_letters_mutated": False,
            },
            "boundary": "Health reads local contracts and external reference state only; it never installs, copies or deletes provider assets.",
        }

    doctor = health

    def manifest_view(self) -> dict[str, Any]:
        return {
            "$schema": self.manifest.get("$schema"),
            "schema_version": self.manifest["schema_version"],
            "bundle": self.manifest.get("bundle", {}),
            "extension_api": self.manifest.get("extension_api", {}),
            "provider_slots": self.manifest.get("provider_slots", {}),
            "provenance": self.manifest.get("provenance", {}),
            "modules": [
                {
                    "id": module["id"],
                    "version": module["version"],
                    "availability": module["availability"],
                    "implementation_batch": module.get("implementation_batch"),
                    "dependencies": module["dependencies"],
                    "optional_dependencies": module["optional_dependencies"],
                    "capabilities": module["capabilities"],
                    "health": module["health"],
                    "customizable": module.get("customizable", []),
                    "external_references": module.get("external_references", []),
                    "extension": module.get("extension"),
                    "ownership": module["ownership"],
                    "reason": module.get("reason"),
                }
                for module in self.manifest["modules"]
            ],
        }
