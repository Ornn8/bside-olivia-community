"""B10A module installation, ownership, configuration, and process lifecycle."""

from __future__ import annotations

import base64
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import config_summary, load_config
from .errors import B10AError
from .manifest import DEFAULT_MANIFEST_PATH, load_manifest, module_map
from .security import ensure_regular_owned_file, safe_owned_path, validate_relative_path


STATE_SCHEMA_VERSION = "b10a.state.v1"
TRANSACTION_SCHEMA_VERSION = "b10a.transaction.v1"
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


def _managed_process_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise B10AError("WRITE_FAILED", "B10A local state could not be written.") from exc
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def _read_json(path: Path, *, error_code: str, message: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise B10AError(error_code, message) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise B10AError(error_code, message) from exc
    if not isinstance(value, dict):
        raise B10AError(error_code, message)
    return value


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise B10AError("INVALID_TRANSACTION", "A rollback transaction contains invalid file data.") from exc


class B10AManager:
    """Operate a declarative B10A skeleton inside an explicit data root."""

    def __init__(
        self,
        *,
        project_root: Path | str | None = None,
        data_root: Path | str | None = None,
        manifest_path: Path | str | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve(strict=False)
        if not self.project_root.exists() or not self.project_root.is_dir():
            raise B10AError("PROJECT_ROOT_INVALID", "The B10A project root must be an existing directory.")
        self.data_root = Path(data_root).expanduser().resolve(strict=False) if data_root else (
            self.project_root / ".b10a"
        ).resolve(strict=False)
        if self.data_root == self.project_root:
            raise B10AError(
                "DATA_ROOT_INVALID",
                "The B10A data root must be a dedicated directory, not the project root itself.",
            )
        self.manifest_path = Path(manifest_path).expanduser().resolve(strict=False) if manifest_path else DEFAULT_MANIFEST_PATH
        self.manifest = load_manifest(self.manifest_path)
        self.modules = module_map(self.manifest)
        self.state_path = self.data_root / "state.json"

    # ---------- local state and configuration ----------

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "project_root": str(self.project_root),
            "modules": {},
            "processes": {},
            "last_transactions": {},
            "process_history": [],
        }

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._initial_state()
        state = _read_json(
            self.state_path,
            error_code="STATE_INVALID",
            message="B10A local state is unreadable or invalid JSON.",
        )
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise B10AError("STATE_INVALID", "B10A local state has an unsupported schema version.")
        if not isinstance(state.get("modules"), dict) or not isinstance(state.get("processes"), dict):
            raise B10AError("STATE_INVALID", "B10A local state has an invalid registry shape.")
        if not isinstance(state.get("last_transactions", {}), dict):
            raise B10AError("STATE_INVALID", "B10A local state has an invalid transaction registry.")
        unknown = sorted(set(state["modules"]) - set(self.modules))
        if unknown:
            raise B10AError("STATE_INVALID", "B10A local state references unknown modules.", {"modules": unknown})
        return state

    def _ensure_data_root(self) -> None:
        if self.data_root.exists() and not self.data_root.is_dir():
            raise B10AError("DATA_ROOT_INVALID", "The B10A data root is not a directory.")
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise B10AError("DATA_ROOT_INVALID", "The B10A data root could not be created.") from exc

    def _write_state(self, state: dict[str, Any]) -> None:
        self._ensure_data_root()
        _atomic_write_json(self.state_path, state)

    def _config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return load_config(self.project_root, self.data_root)

    # ---------- manifest and ownership ----------

    def _module(self, module_id: str) -> dict[str, Any]:
        try:
            return self.modules[module_id]
        except KeyError as exc:
            raise B10AError("UNKNOWN_MODULE", "The requested B10A module is not declared.", {"module": module_id}) from exc

    def _owned_paths(self, module: dict[str, Any]) -> list[tuple[str, Path]]:
        result: list[tuple[str, Path]] = []
        for relative in module["ownership"]["owned_paths"]:
            normalized = validate_relative_path(relative, field=f"{module['id']}.owned_path")
            result.append(
                (normalized, safe_owned_path(self.data_root, normalized, field=f"{module['id']}.owned_path"))
            )
        return result

    def _marker_path(self, module: dict[str, Any]) -> tuple[str, Path]:
        relative, path = self._owned_paths(module)[0]
        return relative, path

    def _marker(self, module: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "b10a.module-marker.v1",
            "module_id": module["id"],
            "version": module["version"],
            "availability": module["availability"],
            "status": "installed",
            "managed_by": "B10A local skeleton",
        }

    def _project_required_path(self, relative: str) -> Path:
        candidate = (self.project_root / Path(*relative.split("/"))).resolve(strict=False)
        try:
            candidate.relative_to(self.project_root.resolve(strict=False))
        except ValueError as exc:
            raise B10AError("PATH_ESCAPE", "A manifest health path escapes the project root.") from exc
        return candidate

    # ---------- file snapshots and reversible transactions ----------

    def _snapshot_files(self, module: dict[str, Any]) -> dict[str, str | None]:
        snapshot: dict[str, str | None] = {}
        for relative, path in self._owned_paths(module):
            if not path.exists():
                snapshot[relative] = None
                continue
            ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise B10AError("OWNERSHIP_READ_FAILED", "An owned file could not be read safely.") from exc
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise B10AError(
                    "OWNERSHIP_TOO_LARGE",
                    "An owned file is too large for a reversible local transaction.",
                    {"path": relative},
                )
            snapshot[relative] = _b64(data)
        return snapshot

    def _write_owned_bytes(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        except OSError as exc:
            raise B10AError("WRITE_FAILED", "A B10A owned file could not be written.") from exc
        finally:
            if temporary:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def _transaction_path(self, relative: str) -> Path:
        return safe_owned_path(self.data_root, relative, field="transaction_path")

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
        self._ensure_data_root()
        relative = f"transactions/{uuid.uuid4().hex}-{module['id'].replace('/', '-')}.json"
        path = self._transaction_path(relative)
        transaction = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "operation": operation,
            "module_id": module["id"],
            "created_at": _now(),
            "before_record": copy.deepcopy(before_record),
            "after_record": copy.deepcopy(after_record),
            "before_files": before_files,
            "after_files": after_files,
            "status": "active",
        }
        _atomic_write_json(path, transaction)
        return relative

    def _load_transaction(self, relative: str) -> tuple[str, Path, dict[str, Any]]:
        normalized = validate_relative_path(relative, field="transaction_path")
        if not normalized.startswith("transactions/"):
            raise B10AError("INVALID_TRANSACTION", "Rollback may only read B10A transaction records.")
        path = self._transaction_path(normalized)
        transaction = _read_json(
            path,
            error_code="TRANSACTION_MISSING",
            message="The requested B10A rollback transaction is missing.",
        )
        if transaction.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
            raise B10AError("INVALID_TRANSACTION", "The B10A rollback transaction schema is unsupported.")
        return normalized, path, transaction

    def _current_snapshot(self, module: dict[str, Any]) -> dict[str, str | None]:
        return self._snapshot_files(module)

    def _assert_snapshot_matches(
        self, module: dict[str, Any], expected: dict[str, str | None], *, phase: str
    ) -> None:
        current = self._current_snapshot(module)
        if current != expected:
            raise B10AError(
                "DIRTY_OWNED_PATH",
                f"Rollback refused because a manager-owned file changed after {phase}.",
                {"module": module["id"]},
            )

    def _restore_transition(
        self,
        module: dict[str, Any],
        before: dict[str, str | None],
        after: dict[str, str | None],
    ) -> None:
        """Recover only when every owned file is still before/after a transition."""
        current = self._current_snapshot(module)
        for relative in before:
            if current.get(relative) not in {before.get(relative), after.get(relative)}:
                raise B10AError(
                    "DIRTY_OWNED_PATH",
                    "Transaction recovery refused because an owned file changed unexpectedly.",
                    {"module": module["id"], "path": relative},
                )
        self._restore_snapshot(module, before)

    def _restore_snapshot(self, module: dict[str, Any], snapshot: dict[str, str | None]) -> None:
        for relative, encoded in snapshot.items():
            path = safe_owned_path(self.data_root, relative, field=f"{module['id']}.owned_path")
            if encoded is None:
                if path.exists():
                    ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
                    try:
                        path.unlink()
                    except OSError as exc:
                        raise B10AError("DELETE_FAILED", "A rollback file could not be removed.") from exc
            else:
                ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
                self._write_owned_bytes(path, _unb64(encoded))

    # ---------- module operations ----------

    def _selected_ids(self, module_ids: list[str] | None, *, all_modules: bool = False) -> list[str]:
        if all_modules:
            selected = list(self.modules)
        else:
            selected = module_ids or []
        if not selected:
            raise B10AError("MODULE_REQUIRED", "Specify at least one --module or use --all.")
        if len(set(selected)) != len(selected):
            raise B10AError("MODULE_DUPLICATE", "A module was selected more than once.")
        for module_id in selected:
            self._module(module_id)
        return selected

    def _check_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._config()

    def install(self, module_ids: list[str] | None = None, *, all_modules: bool = False) -> dict[str, Any]:
        self._check_config()
        selected = self._selected_ids(module_ids, all_modules=all_modules)
        state = self._state()
        preflight: list[dict[str, Any]] = []
        for module_id in selected:
            module = self._module(module_id)
            if module["availability"] != "available":
                raise B10AError(
                    "MODULE_PENDING" if module["availability"] == "pending" else "MODULE_UNAVAILABLE",
                    "This B10A module is declared but not implemented in the skeleton.",
                    {
                        "module": module_id,
                        "availability": module["availability"],
                        "implementation_batch": module.get("implementation_batch"),
                    },
                )
            existing = state["modules"].get(module_id)
            if existing is not None:
                if existing.get("version") != module["version"]:
                    raise B10AError(
                        "UPGRADE_REQUIRED",
                        "The installed module has a different manifest version; use upgrade.",
                        {"module": module_id},
                    )
                marker_relative, marker_path = self._marker_path(module)
                if not marker_path.exists():
                    raise B10AError(
                        "OWNERSHIP_MISSING",
                        "The installed module marker is missing; refusing to recreate state silently.",
                        {"module": module_id, "path": marker_relative},
                    )
                preflight.append({"module": module_id, "status": "NO_OP"})
                continue
            missing = [dependency for dependency in module["dependencies"] if dependency not in state["modules"]]
            if missing:
                raise B10AError(
                    "MISSING_DEPENDENCY",
                    "A required B10A module is not installed.",
                    {"module": module_id, "dependencies": missing},
                )
            for relative, path in self._owned_paths(module):
                if path.exists():
                    ensure_regular_owned_file(path, field=f"{module_id}.owned_path")
                    raise B10AError(
                        "OWNERSHIP_CONFLICT",
                        "A manager-owned path already exists without an installed state record.",
                        {"module": module_id, "path": relative},
                    )
            preflight.append({"module": module_id, "status": "INSTALL"})

        install_items = [item for item in preflight if item["status"] == "INSTALL"]
        if not install_items:
            return {"operation": "install", "status": "NO_OP", "modules": preflight}

        self._ensure_data_root()
        created: list[Path] = []
        try:
            for item in install_items:
                module = self._module(item["module"])
                marker_relative, marker_path = self._marker_path(module)
                _atomic_write_json(marker_path, self._marker(module))
                created.append(marker_path)
                state["modules"][module["id"]] = {
                    "module_id": module["id"],
                    "version": module["version"],
                    "availability": module["availability"],
                    "marker_path": marker_relative,
                    "installed_at": _now(),
                }
            self._write_state(state)
        except Exception:
            for path in reversed(created):
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return {
            "operation": "install",
            "status": "INSTALLED",
            "modules": [*preflight],
            "data_root": str(self.data_root),
        }

    def upgrade(self, module_ids: list[str] | None = None) -> dict[str, Any]:
        self._check_config()
        selected = self._selected_ids(module_ids)
        state = self._state()
        original_state = copy.deepcopy(state)
        results: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        try:
            for module_id in selected:
                module = self._module(module_id)
                record = state["modules"].get(module_id)
                if record is None:
                    raise B10AError("NOT_INSTALLED", "Only an installed module can be upgraded.", {"module": module_id})
                if module["availability"] != "available":
                    raise B10AError("MODULE_UNAVAILABLE", "The requested module is not currently installable.", {"module": module_id})
                marker_relative, marker_path = self._marker_path(module)
                if not marker_path.exists():
                    raise B10AError("OWNERSHIP_MISSING", "The installed module marker is missing.", {"module": module_id})
                if record.get("version") == module["version"]:
                    results.append({"module": module_id, "status": "NO_OP", "version": module["version"]})
                    continue
                before_files = self._snapshot_files(module)
                change: dict[str, Any] = {
                    "module": module,
                    "before_files": before_files,
                    "after_files": None,
                    "transaction": None,
                }
                changes.append(change)
                before_record = copy.deepcopy(record)
                after_record = copy.deepcopy(record)
                after_record["version"] = module["version"]
                after_record["upgraded_at"] = _now()
                marker = self._marker(module)
                marker["status"] = "upgraded"
                _atomic_write_json(marker_path, marker)
                after_files = self._snapshot_files(module)
                change["after_files"] = after_files
                transaction = self._write_transaction(
                    operation="upgrade",
                    module=module,
                    before_record=before_record,
                    after_record=after_record,
                    before_files=before_files,
                    after_files=after_files,
                )
                change["transaction"] = transaction
                state["modules"][module_id] = after_record
                state["last_transactions"][module_id] = transaction
                results.append({"module": module_id, "status": "UPGRADED", "version": module["version"]})
            self._write_state(state)
        except Exception:
            recovery_error: B10AError | None = None
            for change in reversed(changes):
                try:
                    if change["after_files"] is None:
                        self._restore_snapshot(change["module"], change["before_files"])
                    else:
                        self._restore_transition(
                            change["module"], change["before_files"], change["after_files"]
                        )
                except B10AError as recovery:
                    if recovery_error is None:
                        recovery_error = recovery
                transaction = change.get("transaction")
                if isinstance(transaction, str):
                    transaction_path = self._transaction_path(transaction)
                    try:
                        transaction_path.unlink()
                    except FileNotFoundError:
                        pass
            try:
                self._write_state(original_state)
            except B10AError as state_recovery:
                recovery_error = state_recovery
            if recovery_error:
                raise B10AError(
                    "TRANSACTION_RECOVERY_FAILED",
                    "Upgrade failed and automatic transaction recovery was incomplete.",
                    {"module": selected[0]},
                ) from recovery_error
            raise
        return {"operation": "upgrade", "status": "UPGRADED", "modules": results}

    def _process_spec(self, service_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        for module in self.modules.values():
            for process in module.get("processes", []):
                if process["id"] == service_id:
                    return module, process
        raise B10AError("UNKNOWN_SERVICE", "The requested local service is not declared.", {"service": service_id})

    def _identity(self, record: dict[str, Any]) -> tuple[dict[str, Any] | None, Path]:
        relative = record.get("identity_path")
        if not isinstance(relative, str):
            raise B10AError("PROCESS_STATE_INVALID", "The process identity path is invalid.")
        path = safe_owned_path(self.data_root, relative, field="process.identity_path")
        if not path.exists():
            return None, path
        ensure_regular_owned_file(path, field="process.identity_path")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise B10AError("PROCESS_IDENTITY_INVALID", "The local process identity file is invalid.") from exc
        return value if isinstance(value, dict) else None, path

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # Query the Windows process exit code instead of using os.kill(pid,
            # 0), which can deliver a console control event.  Access-denied and
            # exit-code-query failures remain live fail-closed outcomes.
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87
            exit_code = wintypes.DWORD()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _health_url(host: str, port: int) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=0.5) as response:
                body = response.read(4096)
            value = json.loads(body.decode("utf-8"))
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            return {"status": "UNHEALTHY"}
        if isinstance(value, dict) and value.get("status") == "HEALTHY":
            return {"status": "HEALTHY"}
        return {"status": "UNHEALTHY"}

    @staticmethod
    def _request_shutdown(host: str, port: int, nonce: str) -> bool:
        request = urllib.request.Request(
            f"http://{host}:{port}/_b10a/shutdown",
            data=b"",
            method="POST",
            headers={"X-B10A-Nonce": nonce},
        )
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                response.read(256)
                return response.status == 202
        except (OSError, urllib.error.URLError, TimeoutError):
            return False

    def _process_status(self, service_id: str, state: dict[str, Any]) -> dict[str, Any]:
        record = state["processes"].get(service_id)
        if record is None:
            return {"service": service_id, "status": "STOPPED"}
        try:
            pid = int(record["pid"])
            nonce = str(record["nonce"])
            identity, identity_path = self._identity(record)
        except (KeyError, TypeError, ValueError) as exc:
            raise B10AError("PROCESS_STATE_INVALID", "The local process state is invalid.") from exc
        if not identity:
            return {
                "service": service_id,
                # A managed service removes its identity file during a normal
                # exit.  A still-live PID is not enough evidence of ownership
                # (Windows may briefly retain/reuse the PID); with no identity
                # we only report stale state and never send a control signal.
                "status": "ABNORMAL_EXIT",
                "pid": pid,
            }
        if identity.get("pid") != pid or identity.get("nonce") != nonce:
            return {
                "service": service_id,
                "status": "OWNERSHIP_CONFLICT",
                "pid": pid,
            }
        if not self._pid_alive(pid):
            return {"service": service_id, "status": "ABNORMAL_EXIT", "pid": pid}
        health = self._health_url(str(record["host"]), int(record["port"]))
        return {
            "service": service_id,
            "status": "RUNNING" if health["status"] == "HEALTHY" else "UNHEALTHY",
            "health": health["status"],
            "pid": pid,
            "host": record["host"],
            "port": record["port"],
            "identity_path": str(identity_path),
        }

    @staticmethod
    def _port_available(host: str, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
        except OSError:
            return False
        return True

    def start(
        self, service_id: str, *, port: int | None = None, exit_after: float | None = None
    ) -> dict[str, Any]:
        self._check_config()
        module, process_spec = self._process_spec(service_id)
        state = self._state()
        if module["id"] not in state["modules"]:
            raise B10AError("NOT_INSTALLED", "The service's owning module is not installed.", {"module": module["id"]})
        existing = self._process_status(service_id, state)
        if existing["status"] == "RUNNING":
            raise B10AError("ALREADY_RUNNING", "The local service is already running.", {"service": service_id})
        if existing["status"] in {"OWNERSHIP_CONFLICT", "PROCESS_OWNERSHIP_CONFLICT"}:
            raise B10AError("PROCESS_OWNERSHIP_CONFLICT", "Refusing to control a process with a mismatched identity.", {"service": service_id})
        state["processes"].pop(service_id, None)
        selected_port = port if port is not None else int(process_spec["default_port"])
        if not 0 < selected_port < 65536:
            raise B10AError("PORT_INVALID", "The local service port must be between 1 and 65535.")
        if exit_after is not None and exit_after <= 0:
            raise B10AError("EXIT_AFTER_INVALID", "The mock service exit timer must be positive.")
        host = str(process_spec.get("host", "127.0.0.1"))
        if not self._port_available(host, selected_port):
            raise B10AError("PORT_CONFLICT", "The requested local service port is occupied.", {"port": selected_port})

        self._ensure_data_root()
        owned = {relative: path for relative, path in self._owned_paths(module)}
        identity_relative = next(
            (relative for relative in owned if relative == f"services/{service_id}/identity.json"), None
        )
        log_relative = next((relative for relative in owned if relative == f"logs/{service_id}.log"), None)
        if identity_relative is None or log_relative is None:
            raise B10AError("INVALID_MANIFEST", "The managed service lacks explicit identity/log ownership paths.")
        identity_path = owned[identity_relative]
        log_path = owned[log_relative]
        for path in (identity_path, log_path):
            ensure_regular_owned_file(path, field=f"{service_id}.owned_path")
        nonce = uuid.uuid4().hex
        command = [
            sys.executable,
            "-m",
            "runtime.packaging.b10a.mock_service",
            "--host",
            host,
            "--port",
            str(selected_port),
            "--identity-file",
            str(identity_path),
            "--nonce",
            nonce,
        ]
        if exit_after is not None:
            command.extend(["--exit-after", str(exit_after)])
        environment = os.environ.copy()
        package_root = Path(__file__).resolve().parents[3]
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(package_root), existing_pythonpath] if existing_pythonpath else [str(package_root)]
        )
        log_handle = None
        process: subprocess.Popen[bytes] | None = None
        managed_pid: int | None = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("ab")
            process = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                shell=False,
                creationflags=_managed_process_creation_flags(),
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                if identity_path.exists() and self._health_url(host, selected_port)["status"] == "HEALTHY":
                    break
                time.sleep(0.05)
            if process.poll() is not None or not identity_path.exists() or self._health_url(host, selected_port)["status"] != "HEALTHY":
                raise B10AError("START_FAILED", "The local mock service did not become healthy.", {"service": service_id})
            try:
                identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
                managed_pid = int(identity_payload["pid"])
                if identity_payload.get("nonce") != nonce or not self._pid_alive(managed_pid):
                    raise ValueError("identity mismatch")
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise B10AError("START_FAILED", "The local mock service identity could not be verified.", {"service": service_id}) from exc
            state["processes"][service_id] = {
                "service_id": service_id,
                "module_id": module["id"],
                "pid": managed_pid,
                "nonce": nonce,
                "host": host,
                "port": selected_port,
                "identity_path": identity_relative,
                "log_path": log_relative,
                "started_at": _now(),
            }
            self._write_state(state)
        except Exception:
            if managed_pid is not None and self._pid_alive(managed_pid):
                self._request_shutdown(host, selected_port, nonce)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and self._pid_alive(managed_pid):
                    time.sleep(0.05)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            if identity_path.exists():
                try:
                    identity = json.loads(identity_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    identity = None
                if isinstance(identity, dict) and identity.get("pid") == managed_pid and identity.get("nonce") == nonce and not self._pid_alive(int(identity["pid"])):
                    try:
                        identity_path.unlink()
                    except OSError:
                        pass
            raise
        finally:
            if log_handle is not None:
                log_handle.close()
        return {
            "operation": "start",
            "status": "RUNNING",
            "service": service_id,
            "host": host,
            "port": selected_port,
            "pid": managed_pid if managed_pid is not None else (process.pid if process else None),
        }

    def stop(self, service_id: str) -> dict[str, Any]:
        self._check_config()
        module, _process_spec = self._process_spec(service_id)
        state = self._state()
        record = state["processes"].get(service_id)
        if record is None:
            return {"operation": "stop", "status": "NOT_RUNNING", "service": service_id}
        status = self._process_status(service_id, state)
        if status["status"] in {"OWNERSHIP_CONFLICT", "PROCESS_OWNERSHIP_CONFLICT"}:
            raise B10AError("PROCESS_OWNERSHIP_CONFLICT", "Refusing to control a process with a mismatched identity.", {"service": service_id})
        if status["status"] == "ABNORMAL_EXIT":
            state["processes"].pop(service_id, None)
            state.setdefault("process_history", []).append({"service": service_id, "status": "ABNORMAL_EXIT", "at": _now()})
            self._write_state(state)
            return {"operation": "stop", "status": "ABNORMAL_EXIT", "service": service_id}
        pid = int(record["pid"])
        nonce = str(record["nonce"])
        self._request_shutdown(str(record["host"]), int(record["port"]), nonce)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self._pid_alive(pid):
            time.sleep(0.05)
        if self._pid_alive(pid):
            try:
                identity_now, _identity_path_now = self._identity(record)
            except B10AError as exc:
                raise B10AError(
                    "PROCESS_OWNERSHIP_CONFLICT",
                    "The service did not stop and its identity could not be revalidated; no PID kill was attempted.",
                    {"service": service_id},
                ) from exc
            if not identity_now or identity_now.get("pid") != pid or identity_now.get("nonce") != nonce:
                raise B10AError(
                    "PROCESS_OWNERSHIP_CONFLICT",
                    "The service identity changed while stopping; no PID kill was attempted.",
                    {"service": service_id},
                )
            raise B10AError(
                "STOP_FAILED",
                "The local service did not stop through its authenticated control endpoint; no PID kill was attempted.",
                {"service": service_id},
            )
        try:
            identity, identity_path = self._identity(record)
        except B10AError:
            identity, identity_path = None, self.data_root / record["identity_path"]
        if identity and identity.get("pid") == pid and identity.get("nonce") == record.get("nonce"):
            try:
                identity_path.unlink()
            except FileNotFoundError:
                pass
        state["processes"].pop(service_id, None)
        state.setdefault("process_history", []).append({"service": service_id, "status": "STOPPED", "at": _now()})
        self._write_state(state)
        return {"operation": "stop", "status": "STOPPED", "service": service_id, "module": module["id"]}

    def uninstall(
        self,
        module_ids: list[str] | None = None,
        *,
        all_modules: bool = False,
        apply: bool = False,
    ) -> dict[str, Any]:
        self._check_config()
        selected = self._selected_ids(module_ids, all_modules=all_modules)
        state = self._state()
        selected_set = set(selected)
        plans: list[dict[str, Any]] = []
        for module_id in selected:
            module = self._module(module_id)
            record = state["modules"].get(module_id)
            existing_paths = []
            for relative, path in self._owned_paths(module):
                if path.exists():
                    ensure_regular_owned_file(path, field=f"{module_id}.owned_path")
                    existing_paths.append(relative)
            if record is None:
                plans.append({"module": module_id, "status": "NOT_INSTALLED", "owned_paths": existing_paths})
                continue
            dependents = [
                other["id"]
                for other in self.modules.values()
                if module_id in other["dependencies"]
                and other["id"] in state["modules"]
                and other["id"] not in selected_set
            ]
            if dependents:
                raise B10AError(
                    "DEPENDENTS_INSTALLED",
                    "The module is still required by an installed dependent.",
                    {"module": module_id, "dependents": dependents},
                )
            for service_id in module["ownership"].get("processes", []):
                process = self._process_status(service_id, state)
                if process["status"] in {"RUNNING", "UNHEALTHY"}:
                    raise B10AError(
                        "PROCESS_RUNNING",
                        "Stop the module's managed process before uninstalling it, even if its health check is failing.",
                        {"module": module_id, "service": service_id},
                    )
                if process["status"] in {"OWNERSHIP_CONFLICT", "PROCESS_OWNERSHIP_CONFLICT"}:
                    raise B10AError("PROCESS_OWNERSHIP_CONFLICT", "The managed process identity does not match.", {"service": service_id})
            plans.append(
                {
                    "module": module_id,
                    "status": "READY",
                    "owned_paths": existing_paths,
                    "preserved_boundaries": module["ownership"]["preserved_boundaries"],
                }
            )
        if not apply:
            return {
                "operation": "uninstall",
                "status": "DRY_RUN",
                "dry_run": True,
                "modules": plans,
                "preserved_boundary": "Only exact manifest-owned paths under data_root are deletable.",
            }

        changed = [plan for plan in plans if plan["status"] == "READY"]
        original_state = copy.deepcopy(state)
        changes: list[dict[str, Any]] = []
        try:
            for plan in changed:
                module = self._module(plan["module"])
                before_record = copy.deepcopy(state["modules"][module["id"]])
                before_files = self._snapshot_files(module)
                after_files = {relative: None for relative in before_files}
                change: dict[str, Any] = {
                    "module": module,
                    "before_files": before_files,
                    "after_files": after_files,
                    "transaction": None,
                }
                changes.append(change)
                change["transaction"] = self._write_transaction(
                    operation="uninstall",
                    module=module,
                    before_record=before_record,
                    after_record=None,
                    before_files=before_files,
                    after_files=after_files,
                )
            changes_by_module = {change["module"]["id"]: change for change in changes}
            for plan in changed:
                module = self._module(plan["module"])
                for relative, path in self._owned_paths(module):
                    if path.exists():
                        ensure_regular_owned_file(path, field=f"{module['id']}.owned_path")
                        try:
                            path.unlink()
                        except OSError as exc:
                            raise B10AError("DELETE_FAILED", "A manager-owned file could not be removed.", {"path": relative}) from exc
                state["modules"].pop(module["id"], None)
                state["processes"] = {
                    key: value for key, value in state["processes"].items() if value.get("module_id") != module["id"]
                }
                state["last_transactions"][module["id"]] = changes_by_module[module["id"]]["transaction"]
            self._write_state(state)
        except Exception:
            recovery_error: B10AError | None = None
            for change in reversed(changes):
                try:
                    self._restore_transition(
                        change["module"], change["before_files"], change["after_files"]
                    )
                except B10AError as recovery:
                    if recovery_error is None:
                        recovery_error = recovery
                transaction = change.get("transaction")
                if isinstance(transaction, str):
                    transaction_path = self._transaction_path(transaction)
                    try:
                        transaction_path.unlink()
                    except FileNotFoundError:
                        pass
            try:
                self._write_state(original_state)
            except B10AError as state_recovery:
                recovery_error = state_recovery
            if recovery_error:
                raise B10AError(
                    "TRANSACTION_RECOVERY_FAILED",
                    "Uninstall failed and automatic transaction recovery was incomplete.",
                    {"module": changed[0]["module"]["id"] if changed else None},
                ) from recovery_error
            raise
        return {"operation": "uninstall", "status": "UNINSTALLED", "dry_run": False, "modules": plans}

    def rollback(self, module_id: str) -> dict[str, Any]:
        self._check_config()
        module = self._module(module_id)
        state = self._state()
        relative = state.get("last_transactions", {}).get(module_id)
        if not isinstance(relative, str):
            raise B10AError("NO_ROLLBACK", "No reversible B10A transaction is recorded for the module.", {"module": module_id})
        transaction_relative, _transaction_path, transaction = self._load_transaction(relative)
        if transaction.get("module_id") != module_id or transaction.get("status") != "active":
            raise B10AError("INVALID_TRANSACTION", "The rollback transaction does not belong to the requested module.")
        after_files = transaction.get("after_files")
        before_files = transaction.get("before_files")
        if not isinstance(after_files, dict) or not isinstance(before_files, dict):
            raise B10AError("INVALID_TRANSACTION", "The rollback transaction has invalid file snapshots.")
        self._assert_snapshot_matches(module, after_files, phase="the last transaction")
        original_state = copy.deepcopy(state)
        try:
            self._restore_snapshot(module, before_files)
            before_record = transaction.get("before_record")
            if before_record is None:
                state["modules"].pop(module_id, None)
            elif isinstance(before_record, dict):
                state["modules"][module_id] = before_record
            else:
                raise B10AError("INVALID_TRANSACTION", "The rollback transaction has an invalid state snapshot.")
            state["last_transactions"].pop(module_id, None)
            self._write_state(state)
        except Exception:
            recovery_error: B10AError | None = None
            try:
                self._restore_transition(module, after_files, before_files)
            except B10AError as recovery:
                recovery_error = recovery
            try:
                self._write_state(original_state)
            except B10AError as state_recovery:
                recovery_error = state_recovery
            if recovery_error:
                raise B10AError(
                    "TRANSACTION_RECOVERY_FAILED",
                    "Rollback failed and automatic recovery to the prior state was incomplete.",
                    {"module": module_id},
                ) from recovery_error
            raise
        return {
            "operation": "rollback",
            "status": "ROLLED_BACK",
            "module": module_id,
            "transaction": transaction_relative,
        }

    # ---------- health and declaration views ----------

    def manifest_view(self) -> dict[str, Any]:
        return {
            "schema_version": self.manifest["schema_version"],
            "bundle": self.manifest.get("bundle", {}),
            "provider_slots": self.manifest.get("provider_slots", {}),
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
                    "ownership": module["ownership"],
                }
                for module in self.manifest["modules"]
            ],
        }

    def doctor(self) -> dict[str, Any]:
        state = self._state()
        config_error: dict[str, Any] | None = None
        try:
            config, metadata = self._config()
            config_view: dict[str, Any] = {"status": "valid", **config_summary(config, metadata)}
        except B10AError as exc:
            config_view = {"status": "invalid", "code": exc.code, "message": exc.message}
            config_error = {"code": exc.code}

        modules: list[dict[str, Any]] = []
        counts = {"healthy": 0, "pending": 0, "unavailable": 0, "not_installed": 0, "unhealthy": 0}
        for module in self.manifest["modules"]:
            module_id = module["id"]
            record = state["modules"].get(module_id)
            item: dict[str, Any] = {
                "id": module_id,
                "availability": module["availability"],
                "installed": record is not None,
                "version": record.get("version") if record else None,
            }
            if module["availability"] == "pending":
                item.update(status="PENDING", reason=module.get("reason", "future implementation batch"))
                counts["pending"] += 1
            elif module["availability"] == "unavailable":
                item.update(status="UNAVAILABLE", reason=module.get("reason", "not available"))
                counts["unavailable"] += 1
            elif record is None:
                item.update(status="NOT_INSTALLED", reason="install the available module to enable it")
                counts["not_installed"] += 1
            else:
                health = module["health"]
                missing: list[str] = []
                for required in health.get("required_files", []):
                    path = self._project_required_path(required)
                    if not path.is_file():
                        missing.append(required)
                if missing:
                    item.update(status="UNHEALTHY", missing_files=missing)
                    counts["unhealthy"] += 1
                else:
                    item["status"] = "HEALTHY"
                    counts["healthy"] += 1
            modules.append(item)

        processes: list[dict[str, Any]] = []
        process_error = False
        for service_id in state["processes"]:
            try:
                process = self._process_status(service_id, state)
            except B10AError as exc:
                process = {"service": service_id, "status": "UNHEALTHY", "code": exc.code}
            processes.append(process)
            if process["status"] not in {"RUNNING", "STOPPED"}:
                process_error = True

        status = "HEALTHY"
        if config_error or counts["pending"] or counts["unavailable"] or counts["not_installed"] or counts["unhealthy"] or process_error:
            status = "DEGRADED"
        return {
            "schema_version": "b10a.health.v1",
            "status": status,
            "skeleton": True,
            "config": config_view,
            "summary": counts,
            "modules": modules,
            "processes": processes,
            "boundary": "Pending/unavailable declarations are reported explicitly and never counted as healthy.",
        }
