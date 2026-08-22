"""Verified local B10B installation profiles.

Profiles are deliberately small assembly records.  They discover only the
already-validated local B05/B06/B11 inputs, then hand their paths to the
existing provider contracts.  They never download, build, copy, or delete an
upstream runtime, checkpoint, model, avatar, reference media, or user data.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import B10BError
from .manifest import load_manifest


VERIFIED_LOCAL = "verified-local"
_ROOT = Path(__file__).resolve().parents[3]
_ASR_EVIDENCE = _ROOT / ".evidence" / "asr"
_VISUAL_EVIDENCE = _ROOT / ".evidence" / "visual"
_FIXED_ROOT = _ROOT
_FIXED_ASR_EVIDENCE = _ASR_EVIDENCE
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASR_MODEL_SHA256 = "a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae"
_ASR_EXECUTABLE_SHA256 = "9773da48b63716f65540d5f843e62900a1a929deb2c490059ab7e1b6a48e1fb8"
_ASR_ACCEPTANCE_SHA256 = "a08408a0fb3e5433e376b275ae340d772a0664e36bfc5d15bc0f02f7d24a3387"
_ASR_RUNTIME_LICENSE_SHA256 = "6eb0d1526ef4d4e4f5516c6d679eb148aefc36d0d2df9d9cc1fa048fa287963c"
_TTS_RUNTIME_LICENSE_SHA256 = "1eb85fc97224598dad1852b5d6483bbcf0aa8608790dcc657a5a2a761ae9c8c6"
_TTS_MODEL_HASHES = {
    "README.md": "d4ec0fe1342b60a424ce1cd8971c670d26be38068a96148bcb6a1b8a334c3088",
    "cosyvoice3.yaml": "f5a6b2c6f05139d0f18861a1fe506f751e787026b77c05f7e8fef9f8a4405965",
    "flow.pt": "a6fab32a7825e5b0bc855ddd948f8db9370b0a786fbc249caa4595e95b608e4b",
    "hift.pt": "b279d7641eb97ae55b3b540cfba4f953c26492a2df758328a89a4d007ab87a65",
    "llm.pt": "69f43bd545131c30e98947fb360ea8b4dc9916d8e83dded7757c7ea4f5a24970",
}
_TTS_REFERENCE_AUDIO_SHA256 = "f4c63c02ce8d1bf714bd8dda67923fc988e29c28422a4b4f0bd1e6fcfff57118"
_TTS_REFERENCE_SCRIPT_SHA256 = "ee9db79344234b2f72273485feb88b338cebc602e1b48536ef5b362509e673f7"
_TTS_REFERENCE_SOURCE_SHA256 = "72e7208000e38637553476d0deeee7d8a7cf42a6b95d25ac33fb47e7764e6ac9"
_VISUAL_CONFIG_SHA256 = "97a5adc0c1b0c1bdfd3e7755d042bcb39c453c67edca70e78a16d6c9088decd8"
_VISUAL_CHECKPOINT_SHA256 = "b22d7ac86295df667644b17254dc71250c2600b89e20403e90e58812450bc173"
_VISUAL_RUNTIME_LICENSE_SHA256 = "1ce4841f8a5a165f555ee53ef0ebb2dd0a342bf1a98b11409db5e8e65134b034"
_VISUAL_FIXED = {
    "runtime_root": str(_ROOT / "LiveTalking"),
    "python_executable": str(_ROOT / "LiveTalking/venv/Scripts/python.exe"),
    "checkpoint_path": str(_ROOT / "LiveTalking/models/wav2lip.pth"),
    "checkpoint_sha256": _VISUAL_CHECKPOINT_SHA256,
    "checkpoint_url": "https://drive.google.com/file/d/1wu6XujFL9rF-0P2l44G6kpeapeY0cME7/view",
    "checkpoint_revision": "LiveTalking a97f01ba; official wav2lip256.pth; Google Drive file 1wu6XujFL9rF-0P2l44G6kpeapeY0cME7",
    "checkpoint_license": "LiveTalking official distribution; checkpoint terms recorded in the B11 download manifest",
    "avatar_payload": str(_ROOT / "LiveTalking/data/avatars/olivia_b11"),
    "avatar_id": "olivia_b11",
    "original_reference": str(_ROOT / "olivia_assets/立绘动画/assets_idle_normal-c800bce5.mp4"),
    "work_root": str(_VISUAL_EVIDENCE / "upstream-work"),
}
_VERIFIED_OVERRIDE_NAMES = (
    "B10B_ASR_CACHE_ROOT",
    "B10B_ASR_EXECUTABLE",
    "B10B_ASR_MODEL_PATH",
    "B10B_ASR_MODEL_ROOT",
    "B10B_ASR_RUNTIME_ROOT",
    "B10B_TTS_MODEL_DIR",
    "B10B_TTS_REFERENCE_AUDIO",
    "B10B_TTS_REFERENCE_SCRIPT",
    "B10B_TTS_REFERENCE_SOURCE",
    "B10B_TTS_RUNTIME_ROOT",
    "B10B_VISUAL_CONFIG",
)


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_verified_overrides() -> None:
    present = sorted(name for name in _VERIFIED_OVERRIDE_NAMES if name in os.environ)
    if present:
        raise B10BError(
            "VERIFIED_PROFILE_OVERRIDE_FORBIDDEN",
            "verified-local only accepts its pinned provider assembly; use ordinary module customization for other assets.",
            {"profile": VERIFIED_LOCAL, "module_status": "NOT_INSTALLED", "environment_overrides": present},
        )


def _canonical_upstream(manifest: dict[str, Any], upstream_id: str, component: str) -> dict[str, Any]:
    upstreams = manifest.get("provenance", {}).get("upstreams", [])
    upstream = next((item for item in upstreams if item.get("id") == upstream_id), None)
    if not isinstance(upstream, dict):
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The verified-local manifest provenance is incomplete.",
            {
                "profile": VERIFIED_LOCAL,
                "component": component,
                "module_status": "NOT_INSTALLED",
                "mismatches": [upstream_id],
            },
        )
    return upstream


def _require_canonical_value(
    upstream: dict[str, Any],
    field: str,
    expected: str,
    component: str,
    *,
    mismatch: str | None = None,
) -> str:
    value = str(upstream.get(field, ""))
    if value != expected:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The verified-local manifest conflicts with authoritative upstream evidence.",
            {
                "profile": VERIFIED_LOCAL,
                "component": component,
                "module_status": "NOT_INSTALLED",
                "mismatches": [mismatch or field],
            },
        )
    return value


def verified_local_provenance(
    manifest: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve and validate the profile's provenance without touching provider assets."""

    declared_manifest = manifest if manifest is not None else load_manifest()
    canonical_manifest = load_manifest()
    specifications = (
        ("b05-nemotron-runtime", "asr-local", "upstream"),
        ("b05-nemotron-model", "asr-local", "model"),
        ("b06-cosyvoice-runtime", "tts-local", "upstream"),
        ("b06-cosyvoice-model", "tts-local", "model"),
        ("b11-livetalking-runtime", "visual-livetalking", "upstream"),
    )
    resolved: dict[str, dict[str, Any]] = {}
    for upstream_id, component, label in specifications:
        declared = _canonical_upstream(declared_manifest, upstream_id, component)
        canonical = _canonical_upstream(canonical_manifest, upstream_id, component)
        for field in ("source", "revision", "license"):
            _require_canonical_value(
                declared,
                field,
                str(canonical[field]),
                component,
                mismatch=f"{label}_{field}",
            )
        resolved[upstream_id] = declared

    from asr.config import (
        MODEL_LICENSE as B05_MODEL_LICENSE,
        MODEL_REPO as B05_MODEL_REPO,
        MODEL_REVISION as B05_MODEL_REVISION,
        RUNTIME_LICENSE as B05_RUNTIME_LICENSE,
        RUNTIME_REPO as B05_RUNTIME_REPO,
        RUNTIME_REVISION as B05_RUNTIME_REVISION,
    )
    from runtime.visual.livetalking import (
        LIVE_TALKING_LICENSE,
        LIVE_TALKING_REVISION,
        LIVE_TALKING_SOURCE,
    )
    from tts.registry import default_registry

    b05_runtime = resolved["b05-nemotron-runtime"]
    b05_model = resolved["b05-nemotron-model"]
    b06_runtime = resolved["b06-cosyvoice-runtime"]
    b06_model = resolved["b06-cosyvoice-model"]
    b11_runtime = resolved["b11-livetalking-runtime"]
    _require_canonical_value(
        b05_runtime,
        "source",
        _normalize_repo_source(B05_RUNTIME_REPO),
        "asr-local",
        mismatch="upstream_source",
    )
    _require_canonical_value(
        b05_runtime, "revision", B05_RUNTIME_REVISION, "asr-local", mismatch="upstream_revision"
    )
    _require_canonical_value(
        b05_runtime, "license", B05_RUNTIME_LICENSE, "asr-local", mismatch="upstream_license"
    )
    _require_canonical_value(
        b05_model,
        "source",
        f"https://huggingface.co/{B05_MODEL_REPO}",
        "asr-local",
        mismatch="model_source",
    )
    _require_canonical_value(
        b05_model, "revision", B05_MODEL_REVISION, "asr-local", mismatch="model_revision"
    )
    _require_canonical_value(
        b05_model, "license", B05_MODEL_LICENSE, "asr-local", mismatch="model_license"
    )
    tts_license = default_registry().registration("cosyvoice3").license_id
    _require_canonical_value(
        b06_runtime, "license", tts_license, "tts-local", mismatch="upstream_license"
    )
    _require_canonical_value(
        b06_model, "license", tts_license, "tts-local", mismatch="model_license"
    )
    _require_canonical_value(
        b11_runtime, "source", LIVE_TALKING_SOURCE, "visual-livetalking", mismatch="upstream_source"
    )
    _require_canonical_value(
        b11_runtime,
        "revision",
        LIVE_TALKING_REVISION,
        "visual-livetalking",
        mismatch="upstream_revision",
    )
    _require_canonical_value(
        b11_runtime,
        "license",
        LIVE_TALKING_LICENSE,
        "visual-livetalking",
        mismatch="upstream_license",
    )
    return resolved


def _git_value(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "A verified-local upstream revision could not be validated.",
            {"profile": VERIFIED_LOCAL, "module_status": "NOT_INSTALLED"},
        ) from exc
    return result.stdout.strip()


def _normalize_repo_source(value: str) -> str:
    normalized = value.strip().rstrip("/")
    return normalized[:-4] if normalized.lower().endswith(".git") else normalized


def _validate_asr_pins(
    executable: Path,
    model_path: Path,
    acceptance: Path,
    runtime_upstream: dict[str, Any],
    model_upstream: dict[str, Any],
) -> None:
    if _ASR_EVIDENCE != _FIXED_ASR_EVIDENCE:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The existing B05 runtime does not match the verified-local pins.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "asr-local",
                "module_status": "NOT_INSTALLED",
                "mismatches": ["evidence_root"],
            },
        )
    repo = _ASR_EVIDENCE / "NeMo-Speech.cpp"
    try:
        acceptance_value = json.loads(acceptance.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The B05 acceptance record could not be validated.",
            {"profile": VERIFIED_LOCAL, "component": "asr-local", "module_status": "NOT_INSTALLED"},
        ) from exc
    checks = {
        "model_path": model_path == _FIXED_ASR_EVIDENCE / "model/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf",
        "model_sha256": _sha256(model_path) == _ASR_MODEL_SHA256,
        "runtime_executable_path": executable == _FIXED_ASR_EVIDENCE / "build-cuda-asr-http-vcpkg/bin/nemo-speech.exe",
        "runtime_executable_sha256": _sha256(executable) == _ASR_EXECUTABLE_SHA256,
        "acceptance_path": acceptance == _FIXED_ASR_EVIDENCE / "native-probe-accepted/evidence/native_acceptance.json",
        "acceptance_sha256": _sha256(acceptance) == _ASR_ACCEPTANCE_SHA256,
        "acceptance_verified": acceptance_value.get("verified") is True,
        "runtime_revision": acceptance_value.get("runtime_revision") == runtime_upstream["revision"],
        "model_revision": acceptance_value.get("model_revision") == model_upstream["revision"],
        "acceptance_model_sha256": acceptance_value.get("model_sha256") == _ASR_MODEL_SHA256,
        "upstream_revision": _git_value(repo, "rev-parse", "HEAD") == runtime_upstream["revision"],
        "upstream_source": _normalize_repo_source(_git_value(repo, "remote", "get-url", "origin"))
        == _normalize_repo_source(str(runtime_upstream["source"])),
        "upstream_license": _sha256(repo / "LICENSE") == _ASR_RUNTIME_LICENSE_SHA256,
    }
    mismatches = sorted(name for name, valid in checks.items() if not valid)
    if mismatches:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The existing B05 runtime does not match the verified-local pins.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "asr-local",
                "module_status": "NOT_INSTALLED",
                "mismatches": mismatches,
            },
        )


def _validate_tts_pins(
    runtime: Path,
    model: Path,
    reference_audio: Path,
    reference_script: Path,
    runtime_upstream: dict[str, Any],
    model_upstream: dict[str, Any],
) -> None:
    if _ROOT != _FIXED_ROOT:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The existing B06 runtime does not match the verified-local pins.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "tts-local",
                "module_status": "NOT_INSTALLED",
                "mismatches": ["client_root"],
            },
        )
    reference_source = _FIXED_ROOT / "test_cosyvoice3.py"
    metadata = model / ".cache/huggingface/download/llm.pt.metadata"
    try:
        model_revision = metadata.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, UnicodeError, IndexError) as exc:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The B06 model revision metadata could not be validated.",
            {"profile": VERIFIED_LOCAL, "component": "tts-local", "module_status": "NOT_INSTALLED"},
        ) from exc
    checks = {
        "runtime_path": runtime == _FIXED_ROOT / "CosyVoice",
        "model_path": model == _FIXED_ROOT / "CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B",
        "reference_audio_path": reference_audio == _FIXED_ROOT / "output_audio/bv113_prompt_4p85s.wav",
        "reference_script_path": reference_script == _FIXED_ASR_EVIDENCE.parent / "run_real_local_tts.py",
        "upstream_revision": _git_value(runtime, "rev-parse", "HEAD") == runtime_upstream["revision"],
        "upstream_source": _normalize_repo_source(_git_value(runtime, "remote", "get-url", "origin"))
        == _normalize_repo_source(str(runtime_upstream["source"])),
        "upstream_license": _sha256(runtime / "LICENSE") == _TTS_RUNTIME_LICENSE_SHA256,
        "model_revision": model_revision == str(model_upstream["revision"]),
        "reference_audio_sha256": _sha256(reference_audio) == _TTS_REFERENCE_AUDIO_SHA256,
        "reference_script_sha256": _sha256(reference_script) == _TTS_REFERENCE_SCRIPT_SHA256,
        "reference_source_sha256": _sha256(reference_source) == _TTS_REFERENCE_SOURCE_SHA256,
    }
    checks.update(
        {f"model_{name}_sha256": _sha256(model / name) == expected for name, expected in _TTS_MODEL_HASHES.items()}
    )
    mismatches = sorted(name for name, valid in checks.items() if not valid)
    if mismatches:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The existing B06 runtime does not match the verified-local pins.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "tts-local",
                "module_status": "NOT_INSTALLED",
                "mismatches": mismatches,
            },
        )


def _reference_text(path: Path) -> str:
    """Read the existing verified reference text without printing it."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "The existing B06 reference-text source is unavailable.",
            {"component": "tts-local", "missing": ["reference_text_source"]},
        ) from exc
    function = next(
        (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "reference_text"),
        None,
    )
    if function is None:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "The existing B06 reference-text source is unavailable.",
            {"component": "tts-local", "missing": ["reference_text_source"]},
        )
    namespace: dict[str, Any] = {
        "re": re,
        "REFERENCE_SOURCE": _env_path("B10B_TTS_REFERENCE_SOURCE", _ROOT / "test_cosyvoice3.py"),
    }
    try:
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        value = str(namespace["reference_text"]()).strip()
    except (OSError, ValueError, KeyError, TypeError, re.error) as exc:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "The existing B06 reference text could not be read.",
            {"component": "tts-local", "missing": ["reference_text"]},
        ) from exc
    if not value:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "The existing B06 reference text is empty.",
            {"component": "tts-local", "missing": ["reference_text"]},
        )
    return value


def _visual_settings(runtime_upstream: dict[str, Any]) -> dict[str, Any]:
    path = _VISUAL_EVIDENCE / "runtime-config.json"
    try:
        payload = path.read_bytes()
        raw = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "The existing B11 verified runtime profile is unavailable.",
            {"component": "visual-livetalking", "missing": ["runtime_config"]},
        ) from exc
    fields = (
        "runtime_root", "python_executable", "checkpoint_path", "checkpoint_sha256", "checkpoint_url",
        "checkpoint_revision", "checkpoint_license", "avatar_payload", "avatar_id", "original_reference",
        "work_root", "upstream_source", "upstream_revision", "upstream_license",
    )
    value = {field: raw.get(field) for field in fields}
    missing = [field for field, item in value.items() if not isinstance(item, str) or not item.strip()]
    expected_values = {
        **_VISUAL_FIXED,
        "upstream_source": str(runtime_upstream["source"]),
        "upstream_revision": str(runtime_upstream["revision"]),
        "upstream_license": str(runtime_upstream["license"]),
    }
    invalid_fields = sorted(field for field, expected in expected_values.items() if value.get(field) != expected)
    checkpoint_path = Path(str(value.get("checkpoint_path", "")))
    checkpoint_hash_matches = checkpoint_path.is_file() and _sha256(checkpoint_path) == _VISUAL_CHECKPOINT_SHA256
    if (
        missing
        or not _SHA256.fullmatch(str(value["checkpoint_sha256"]).lower())
        or hashlib.sha256(payload).hexdigest() != _VISUAL_CONFIG_SHA256
        or invalid_fields
        or not checkpoint_hash_matches
    ):
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The existing B11 runtime does not match the verified-local pins.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "visual-livetalking",
                "module_status": "NOT_INSTALLED",
                "mismatches": sorted(set(missing + invalid_fields + ([] if checkpoint_hash_matches else ["checkpoint_file_sha256"]))),
            },
        )
    runtime_root = Path(str(value["runtime_root"]))
    upstream_checks = {
        "upstream_revision": _git_value(runtime_root, "rev-parse", "HEAD") == runtime_upstream["revision"],
        "upstream_source": _normalize_repo_source(_git_value(runtime_root, "remote", "get-url", "origin"))
        == _normalize_repo_source(str(runtime_upstream["source"])),
        "upstream_license": _sha256(runtime_root / "LICENSE") == _VISUAL_RUNTIME_LICENSE_SHA256,
    }
    upstream_mismatches = sorted(name for name, valid in upstream_checks.items() if not valid)
    if upstream_mismatches:
        raise B10BError(
            "VERIFIED_PROFILE_PIN_MISMATCH",
            "The existing B11 upstream does not match the verified-local pins.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "visual-livetalking",
                "module_status": "NOT_INSTALLED",
                "mismatches": upstream_mismatches,
            },
        )
    return value


def _validated_visual_settings(runtime_upstream: dict[str, Any]) -> dict[str, Any]:
    visual = _visual_settings(runtime_upstream)
    required = (
        Path(str(visual["runtime_root"])),
        Path(str(visual["python_executable"])),
        Path(str(visual["checkpoint_path"])),
        Path(str(visual["avatar_payload"])),
        Path(str(visual["original_reference"])),
        Path(str(visual["work_root"])),
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "A verified local provider dependency is missing.",
            {
                "profile": VERIFIED_LOCAL,
                "component": "visual-livetalking",
                "module_status": "NOT_INSTALLED",
                "missing": ["verified_reference" for _path in missing],
            },
        )
    return visual


def verified_local_visual_settings(
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate only the fixed B11 profile record and its current local pins."""

    _reject_verified_overrides()
    provenance = verified_local_provenance(manifest if manifest is not None else load_manifest())
    return _validated_visual_settings(provenance["b11-livetalking-runtime"])


def verified_local_profile(manifest: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return settings for the maintained local B05/B06/B11 assemblies.

    This profile is byte- and revision-pinned and rejects every ``B10B_*``
    external-reference override.  Other local assets remain supported through
    ordinary module customization, which is not a verified-local profile.
    Validation happens before B10B writes any lifecycle metadata.
    """

    _reject_verified_overrides()
    canonical_manifest = manifest if manifest is not None else load_manifest()
    provenance = verified_local_provenance(canonical_manifest)
    b05_runtime = provenance["b05-nemotron-runtime"]
    b05_model = provenance["b05-nemotron-model"]
    b06_runtime = provenance["b06-cosyvoice-runtime"]
    b06_model = provenance["b06-cosyvoice-model"]
    b11_runtime = provenance["b11-livetalking-runtime"]

    # ``AsrConfig.acceptance_manifest`` is intentionally derived from
    # runtime_root.parent.  This logical runtime root is retained from the
    # verified probe; the executable remains separately pinned below.
    asr_runtime_root = _env_path("B10B_ASR_RUNTIME_ROOT", _ASR_EVIDENCE / "native-probe-accepted/runtime")
    asr_executable = _env_path("B10B_ASR_EXECUTABLE", _ASR_EVIDENCE / "build-cuda-asr-http-vcpkg/bin/nemo-speech.exe")
    asr_model_root = _env_path("B10B_ASR_MODEL_ROOT", _ASR_EVIDENCE / "model")
    asr_model_path = _env_path("B10B_ASR_MODEL_PATH", asr_model_root / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf")
    asr_cache_root = _env_path("B10B_ASR_CACHE_ROOT", _ASR_EVIDENCE / "native-probe-accepted/cache")
    asr_manifest = asr_runtime_root.parent / "evidence/native_acceptance.json"
    tts_runtime = _env_path("B10B_TTS_RUNTIME_ROOT", _ROOT / "CosyVoice")
    tts_model = _env_path("B10B_TTS_MODEL_DIR", tts_runtime / "pretrained_models/Fun-CosyVoice3-0.5B")
    tts_reference_audio = _env_path("B10B_TTS_REFERENCE_AUDIO", _ROOT / "output_audio/bv113_prompt_4p85s.wav")
    tts_reference_script = _env_path("B10B_TTS_REFERENCE_SCRIPT", _ASR_EVIDENCE.parent / "run_real_local_tts.py")

    requirements = {
        "asr-local": [asr_executable, asr_model_root, asr_model_path, asr_manifest],
        "tts-local": [tts_runtime, tts_model, tts_reference_audio, tts_reference_script],
    }
    visual = _validated_visual_settings(b11_runtime)
    missing = {module: [str(path) for path in paths if not path.exists()] for module, paths in requirements.items()}
    missing = {module: paths for module, paths in missing.items() if paths}
    if missing:
        raise B10BError(
            "MODULE_PROVIDER_MISSING",
            "A verified local provider dependency is missing; no profile metadata was written.",
            {"profile": VERIFIED_LOCAL, "module_status": "NOT_INSTALLED", "missing": missing},
        )

    _validate_asr_pins(
        asr_executable, asr_model_path, asr_manifest, b05_runtime, b05_model
    )
    _validate_tts_pins(
        tts_runtime,
        tts_model,
        tts_reference_audio,
        tts_reference_script,
        b06_runtime,
        b06_model,
    )

    return {
        "asr-local": {
            "provider": "nemotron-speech-cpp", "language": "zh", "server_url": "ws://127.0.0.1:18081",
            "runtime_root": str(asr_runtime_root), "runtime_executable": str(asr_executable),
            "model_root": str(asr_model_root), "model_path": str(asr_model_path), "cache_root": str(asr_cache_root),
            "upstream_source": str(b05_runtime["source"]), "upstream_revision": str(b05_runtime["revision"]),
            "upstream_license": str(b05_runtime["license"]), "runtime_sha256": _ASR_EXECUTABLE_SHA256,
            "model_source": str(b05_model["source"]), "model_revision": str(b05_model["revision"]),
            "model_license": str(b05_model["license"]), "model_sha256": _ASR_MODEL_SHA256,
            "acceptance_sha256": _ASR_ACCEPTANCE_SHA256,
        },
        "tts-local": {
            "provider": "cosyvoice3", "runtime_root": str(tts_runtime), "model_dir": str(tts_model),
            "reference_audio": str(tts_reference_audio), "reference_text": _reference_text(tts_reference_script),
            "fallback": "text", "fp16": True,
            "upstream_source": str(b06_runtime["source"]), "upstream_revision": str(b06_runtime["revision"]),
            "upstream_license": str(b06_runtime["license"]), "model_source": str(b06_model["source"]),
            "model_revision": str(b06_model["revision"]), "model_license": str(b06_model["license"]),
            "model_hashes": dict(_TTS_MODEL_HASHES), "reference_audio_sha256": _TTS_REFERENCE_AUDIO_SHA256,
        },
        "visual-livetalking": visual,
    }


def profile_settings(profile: str, *, manifest: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    if profile != VERIFIED_LOCAL:
        raise B10BError("UNKNOWN_PROFILE", "The requested B10B profile is not declared.", {"profile": profile})
    return verified_local_profile(manifest)


def profile_modules(profile: str) -> list[str]:
    profile_settings(profile)  # validate before the caller mutates lifecycle state
    return ["core/http", "asr-local", "visual-driver", "visual-livetalking", "tts-local"]
