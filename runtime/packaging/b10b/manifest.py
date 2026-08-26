"""Declarative B10B module manifest loading and fail-closed validation."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .errors import B10BError
from .security import validate_relative_path


MODULE_SCHEMA_VERSION = "b10b.modules.v1"
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifests" / "b10b.modules.json"
_MODULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)?$")
_AVAILABILITY = {"available", "pending", "unavailable"}
_HEALTH_KINDS = {"project_files", "python_contract", "provider", "extension", "pending"}
_TOP_KEYS = {"$schema", "schema_version", "bundle", "extension_api", "provider_slots", "modules"}
_REQUIRED_TOP_KEYS = set(_TOP_KEYS) | {"provenance"}
_BUNDLE_KEYS = {"id", "version", "batch", "status", "policy"}
_EXTENSION_KEYS = {"id", "api_version", "registration", "health_method"}
_SLOT_KEYS = {"implementation", "status_source", "status"}
_MODULE_KEYS = {
    "id",
    "version",
    "availability",
    "implementation_batch",
    "reason",
    "dependencies",
    "optional_dependencies",
    "capabilities",
    "health",
    "customizable",
    "external_references",
    "extension",
    "ownership",
}
_HEALTH_KEYS = {"kind", "adapter", "required_files"}
_OWNERSHIP_KEYS = {"owned_paths", "preserved_boundaries", "processes"}
_REFERENCE_KEYS = {"name", "kind", "copy_policy", "preserve", "drive_policy", "path_policy"}
_EXTENSION_MODULE_KEYS = {"api_version", "provider_id"}
_PROVENANCE_KEYS = {"policy", "upstreams"}
_UPSTREAM_KEYS = {
    "id",
    "kind",
    "source",
    "version",
    "revision",
    "license",
    "status",
    "adapter_boundary",
    "uninstall_path",
}
_UPSTREAM_KINDS = {"runtime", "model", "visual-runtime"}
_UPSTREAM_STATUS = {"composed", "not_composed"}
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_LOCAL_ABSOLUTE_NON_ROOT = "local-absolute-non-root"


def _object(value: Any, field: str, allowed: set[str], *, required: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise B10BError("INVALID_MANIFEST", f"{field} must be an object.")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise B10BError("INVALID_MANIFEST", f"{field} contains unsupported fields.", {"fields": unknown})
    missing = sorted((required or set()) - set(value))
    if missing:
        raise B10BError("INVALID_MANIFEST", f"{field} is missing required fields.", {"fields": missing})
    return value


def _string(value: Any, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise B10BError("INVALID_MANIFEST", f"{field} must be a non-empty string.")
    return value


def _strings(value: Any, field: str, *, unique: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise B10BError("INVALID_MANIFEST", f"{field} must be a list of non-empty strings.")
    result = [str(item) for item in value]
    if unique and len(set(result)) != len(result):
        raise B10BError("INVALID_MANIFEST", f"{field} contains duplicates.")
    return result


def _paths(value: Any, field: str) -> list[str]:
    result = _strings(value, field, unique=True)
    return [validate_relative_path(item, field=f"{field}[{index}]") for index, item in enumerate(result)]


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise B10BError("MANIFEST_MISSING", "The B10B module manifest was not found.") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise B10BError("INVALID_MANIFEST", "The B10B module manifest is unreadable.") from exc
    return validate_manifest(raw)


def validate_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise B10BError("INVALID_MANIFEST", "The B10B module manifest must be an object.")
    unknown_top = sorted(set(raw) - _REQUIRED_TOP_KEYS)
    missing_top = sorted(_REQUIRED_TOP_KEYS - set(raw))
    if unknown_top or missing_top:
        raise B10BError(
            "INVALID_MANIFEST",
            "The B10B module manifest must use its exact top-level schema.",
            {"unsupported": unknown_top, "missing": missing_top},
        )
    if raw.get("schema_version") != MODULE_SCHEMA_VERSION:
        raise B10BError("INVALID_MANIFEST", "The B10B module manifest schema version is unsupported.")
    if "$schema" in raw:
        _string(raw["$schema"], "$schema")

    bundle = _object(raw.get("bundle"), "bundle", _BUNDLE_KEYS, required=_BUNDLE_KEYS)
    for field in _BUNDLE_KEYS:
        _string(bundle.get(field), f"bundle.{field}")

    extension = _object(raw.get("extension_api"), "extension_api", _EXTENSION_KEYS, required=_EXTENSION_KEYS)
    for field in _EXTENSION_KEYS:
        _string(extension.get(field), f"extension_api.{field}")
    if extension["api_version"] != "b10b.provider.v1":
        raise B10BError("INVALID_MANIFEST", "The B10B provider extension API is missing or unsupported.")

    provider_slots = _object(raw.get("provider_slots"), "provider_slots", {"asr", "visual", "tts"}, required={"asr", "visual", "tts"})
    for slot_id, slot in provider_slots.items():
        slot_value = _object(slot, f"provider_slots.{slot_id}", _SLOT_KEYS, required={"implementation", "status_source"})
        if not slot_value:
            raise B10BError("INVALID_MANIFEST", f"provider_slots.{slot_id} must declare a slot contract.")
        for field, value in slot_value.items():
            _string(value, f"provider_slots.{slot_id}.{field}")

    modules = raw.get("modules")
    if not isinstance(modules, list) or not modules:
        raise B10BError("INVALID_MANIFEST", "The B10B module manifest must declare modules.")

    result = copy.deepcopy(raw)
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(modules):
        field = f"modules[{index}]"
        module = _object(item, field, _MODULE_KEYS, required={
            "id",
            "version",
            "availability",
            "implementation_batch",
            "dependencies",
            "optional_dependencies",
            "capabilities",
            "health",
            "customizable",
            "external_references",
            "ownership",
        })
        module_id = _string(module.get("id"), f"{field}.id")
        if not _MODULE_ID.fullmatch(module_id):
            raise B10BError("INVALID_MANIFEST", f"{field}.id is invalid.")
        if module_id in by_id:
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} is declared more than once.")
        _string(module.get("version"), f"{field}.version")
        availability = module.get("availability")
        if availability not in _AVAILABILITY:
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} has invalid availability.")
        _string(module.get("implementation_batch"), f"{field}.implementation_batch")
        required_dependencies = _strings(module.get("dependencies"), f"{field}.dependencies", unique=True)
        optional_dependencies = _strings(module.get("optional_dependencies"), f"{field}.optional_dependencies", unique=True)
        if module_id in {*required_dependencies, *optional_dependencies}:
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} cannot depend on itself.")
        if set(required_dependencies) & set(optional_dependencies):
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} repeats a dependency in both dependency lists.")
        _strings(module.get("capabilities"), f"{field}.capabilities", unique=True)
        _strings(module.get("customizable"), f"{field}.customizable", unique=True)
        if "reason" in module:
            _string(module["reason"], f"{field}.reason")
        if availability != "available" and not isinstance(module.get("reason"), str):
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} must explain non-available status.")

        health = _object(module.get("health"), f"{field}.health", _HEALTH_KEYS, required={"kind", "adapter", "required_files"})
        kind = health.get("kind")
        if kind not in _HEALTH_KINDS:
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} has invalid health kind.")
        _string(health.get("adapter"), f"{field}.health.adapter")
        health_files = _paths(health.get("required_files"), f"{field}.health.required_files")
        if kind == "project_files" and not health_files:
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} project-file health needs required files.")
        if availability == "pending" and kind != "pending":
            raise B10BError("INVALID_MANIFEST", f"Pending module {module_id} must use pending health.")
        if availability == "available" and kind == "pending":
            raise B10BError("INVALID_MANIFEST", f"Available module {module_id} cannot use pending health.")

        ownership = _object(module.get("ownership"), f"{field}.ownership", _OWNERSHIP_KEYS, required=_OWNERSHIP_KEYS)
        owned_paths = _paths(ownership.get("owned_paths"), f"{field}.ownership.owned_paths")
        if not owned_paths:
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} must declare owned paths.")
        preserved = _strings(ownership.get("preserved_boundaries"), f"{field}.ownership.preserved_boundaries")
        _strings(ownership.get("processes"), f"{field}.ownership.processes", unique=True)

        references = module.get("external_references")
        if not isinstance(references, list):
            raise B10BError("INVALID_MANIFEST", f"Module {module_id} external_references must be a list.")
        reference_names: set[str] = set()
        for ref_index, ref in enumerate(references):
            ref_field = f"{field}.external_references[{ref_index}]"
            reference = _object(ref, ref_field, _REFERENCE_KEYS, required={"name", "kind", "copy_policy"})
            name = _string(reference.get("name"), f"{ref_field}.name")
            if name in reference_names:
                raise B10BError("INVALID_MANIFEST", f"Module {module_id} repeats external reference {name}.")
            reference_names.add(name)
            _string(reference.get("kind"), f"{ref_field}.kind")
            if reference.get("copy_policy") != "reference-only":
                raise B10BError("INVALID_MANIFEST", f"Module {module_id} has an unsafe external reference policy.")
            if "preserve" in reference and not isinstance(reference["preserve"], bool):
                raise B10BError("INVALID_MANIFEST", f"{ref_field}.preserve must be boolean.")
            for policy in ("drive_policy", "path_policy"):
                if policy in reference:
                    _string(reference[policy], f"{ref_field}.{policy}")
            if reference.get("drive_policy") not in {None, _LOCAL_ABSOLUTE_NON_ROOT}:
                raise B10BError("INVALID_MANIFEST", f"{ref_field}.drive_policy is unsupported.")
            if reference.get("path_policy") not in {None, "logical-or-external"}:
                raise B10BError("INVALID_MANIFEST", f"{ref_field}.path_policy is unsupported.")

        if "extension" in module:
            module_extension = _object(module["extension"], f"{field}.extension", _EXTENSION_MODULE_KEYS, required=_EXTENSION_MODULE_KEYS)
            if module_extension["api_version"] != "b10b.provider.v1":
                raise B10BError("INVALID_MANIFEST", f"Module {module_id} declares an unsupported extension API.")
            _string(module_extension["provider_id"], f"{field}.extension.provider_id")
        by_id[module_id] = module

    provenance = _object(raw.get("provenance"), "provenance", _PROVENANCE_KEYS, required=_PROVENANCE_KEYS)
    if provenance.get("policy") != "assembly-only":
        raise B10BError("INVALID_MANIFEST", "B10B provenance must declare the assembly-only policy.")
    upstreams = provenance.get("upstreams")
    if not isinstance(upstreams, list) or not upstreams:
        raise B10BError("INVALID_MANIFEST", "B10B provenance must declare at least one upstream.")
    upstream_ids: set[str] = set()
    for index, upstream in enumerate(upstreams):
        field = f"provenance.upstreams[{index}]"
        item = _object(upstream, field, _UPSTREAM_KEYS, required=_UPSTREAM_KEYS)
        upstream_id = _string(item.get("id"), f"{field}.id")
        if upstream_id in upstream_ids:
            raise B10BError("INVALID_MANIFEST", "B10B provenance upstream ids must be unique.")
        upstream_ids.add(upstream_id)
        if item.get("kind") not in _UPSTREAM_KINDS:
            raise B10BError("INVALID_MANIFEST", f"{field}.kind is unsupported.")
        source = _string(item.get("source"), f"{field}.source")
        if not (source.startswith("https://github.com/") or source.startswith("https://huggingface.co/")):
            raise B10BError("INVALID_MANIFEST", f"{field}.source must be a GitHub or Hugging Face source.")
        _string(item.get("version"), f"{field}.version")
        revision = _string(item.get("revision"), f"{field}.revision")
        if not _REVISION.fullmatch(revision.lower()):
            raise B10BError("INVALID_MANIFEST", f"{field}.revision must be a full commit or model revision.")
        _string(item.get("license"), f"{field}.license")
        if item.get("status") not in _UPSTREAM_STATUS:
            raise B10BError("INVALID_MANIFEST", f"{field}.status is unsupported.")
        _string(item.get("adapter_boundary"), f"{field}.adapter_boundary")
        _string(item.get("uninstall_path"), f"{field}.uninstall_path")

    for module_id, module in by_id.items():
        for dependency in [*module["dependencies"], *module["optional_dependencies"]]:
            if dependency not in by_id:
                raise B10BError("INVALID_MANIFEST", f"Module {module_id} references an unknown dependency.", {"dependency": dependency})

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_id: str) -> None:
        if module_id in visiting:
            raise B10BError("INVALID_MANIFEST", "The B10B module dependency graph has a cycle.")
        if module_id in visited:
            return
        visiting.add(module_id)
        module = by_id[module_id]
        for dependency in [*module["dependencies"], *module["optional_dependencies"]]:
            visit(dependency)
        visiting.remove(module_id)
        visited.add(module_id)

    for module_id in by_id:
        visit(module_id)
    return result


def module_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {module["id"]: module for module in manifest["modules"]}
