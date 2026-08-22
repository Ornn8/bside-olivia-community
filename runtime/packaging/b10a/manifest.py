"""Declarative B10A module manifest loading and validation."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .errors import B10AError
from .security import validate_relative_path


MODULE_SCHEMA_VERSION = "b10a.modules.v1"
_MODULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)?$")
_AVAILABILITY = {"available", "pending", "unavailable"}
_HEALTH_KINDS = {"project_files", "pending"}

DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifests" / "b10a.modules.json"


def _require(value: Any, kind: type, field: str) -> Any:
    if not isinstance(value, kind):
        raise B10AError("INVALID_MANIFEST", f"{field} has an invalid type.")
    return value


def _paths(values: Any, field: str) -> list[str]:
    values = _require(values, list, field)
    result: list[str] = []
    for index, value in enumerate(values):
        result.append(validate_relative_path(value, field=f"{field}[{index}]"))
    if len(set(result)) != len(result):
        raise B10AError("INVALID_MANIFEST", f"{field} contains duplicate paths.")
    return result


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise B10AError("MANIFEST_MISSING", "The B10A module manifest was not found.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise B10AError("INVALID_MANIFEST", "The B10A module manifest is unreadable.") from exc
    return validate_manifest(raw)


def validate_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise B10AError("INVALID_MANIFEST", "The B10A module manifest must be an object.")
    if raw.get("schema_version") != MODULE_SCHEMA_VERSION:
        raise B10AError("INVALID_MANIFEST", "The B10A module manifest schema version is unsupported.")
    modules = _require(raw.get("modules"), list, "modules")
    if not modules:
        raise B10AError("INVALID_MANIFEST", "The B10A module manifest must declare modules.")

    canonical = copy.deepcopy(raw)
    seen: set[str] = set()
    module_map: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(modules):
        if not isinstance(item, dict):
            raise B10AError("INVALID_MANIFEST", f"modules[{index}] must be an object.")
        module_id = item.get("id")
        if not isinstance(module_id, str) or not _MODULE_ID.fullmatch(module_id):
            raise B10AError("INVALID_MANIFEST", f"modules[{index}].id is invalid.")
        if module_id in seen:
            raise B10AError("INVALID_MANIFEST", f"Module {module_id} is declared more than once.")
        seen.add(module_id)
        if not isinstance(item.get("version"), str) or not item["version"]:
            raise B10AError("INVALID_MANIFEST", f"Module {module_id} needs a version.")
        if item.get("availability") not in _AVAILABILITY:
            raise B10AError("INVALID_MANIFEST", f"Module {module_id} has invalid availability.")
        for field in ("dependencies", "optional_dependencies", "capabilities"):
            _require(item.get(field), list, f"modules[{index}].{field}")
        health = _require(item.get("health"), dict, f"modules[{index}].health")
        if health.get("kind") not in _HEALTH_KINDS:
            raise B10AError("INVALID_MANIFEST", f"Module {module_id} has invalid health kind.")
        if health.get("kind") == "project_files":
            _paths(health.get("required_files"), f"modules[{index}].health.required_files")
        ownership = _require(item.get("ownership"), dict, f"modules[{index}].ownership")
        owned_paths = _paths(
            ownership.get("owned_paths"), f"modules[{index}].ownership.owned_paths"
        )
        if not owned_paths:
            raise B10AError("INVALID_MANIFEST", f"Module {module_id} must declare owned paths.")
        preserved = _require(
            ownership.get("preserved_boundaries"),
            list,
            f"modules[{index}].ownership.preserved_boundaries",
        )
        if any(not isinstance(value, str) or not value.strip() for value in preserved):
            raise B10AError("INVALID_MANIFEST", f"Module {module_id} has an invalid preserved boundary.")
        processes = _require(
            ownership.get("processes", []), list, f"modules[{index}].ownership.processes"
        )
        for process in processes:
            if not isinstance(process, str) or not process:
                raise B10AError("INVALID_MANIFEST", f"Module {module_id} has an invalid process id.")
        process_specs = _require(item.get("processes", []), list, f"modules[{index}].processes")
        for process in process_specs:
            if not isinstance(process, dict) or not isinstance(process.get("id"), str):
                raise B10AError("INVALID_MANIFEST", f"Module {module_id} has an invalid process spec.")
            if process.get("kind") != "built-in-mock":
                raise B10AError("INVALID_MANIFEST", f"Module {module_id} has an unsupported process kind.")
            if not isinstance(process.get("default_port"), int) or not 0 < process["default_port"] < 65536:
                raise B10AError("INVALID_MANIFEST", f"Module {module_id} has an invalid process port.")
        module_map[module_id] = item

    for module_id, item in module_map.items():
        for field in ("dependencies", "optional_dependencies"):
            for dependency in item[field]:
                if not isinstance(dependency, str) or dependency not in module_map:
                    raise B10AError(
                        "INVALID_MANIFEST",
                        f"Module {module_id} references an unknown dependency.",
                        {"dependency": dependency},
                    )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_id: str) -> None:
        if module_id in visiting:
            raise B10AError("INVALID_MANIFEST", "The B10A module dependency graph has a cycle.")
        if module_id in visited:
            return
        visiting.add(module_id)
        for dependency in module_map[module_id]["dependencies"]:
            visit(dependency)
        visiting.remove(module_id)
        visited.add(module_id)

    for module_id in module_map:
        visit(module_id)

    provider_slots = raw.get("provider_slots", {})
    if not isinstance(provider_slots, dict):
        raise B10AError("INVALID_MANIFEST", "provider_slots must be an object.")
    return canonical


def module_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {module["id"]: module for module in manifest["modules"]}
