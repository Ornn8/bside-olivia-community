"""Thin external-process adapter for Breeze TTS 2.

The product owns only configuration, health reporting, cancellation, and PCM
transport.  Model loading and inference remain in the pinned upstream runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .contracts import AudioChunk, TTSConfig, TTSRequest, TTSUnavailable


BREEZE_LICENSE_ID = "BreezeBlue-Research-and-Non-Commercial-1.0"

_VARIANT_WEIGHTS = {
    "int8_hybrid": "Breeze-TTS-2-int8-hybrid.safetensors",
    "bf16": "Breeze-TTS-2-bf16.safetensors",
    "int8_convrot": "Breeze-TTS-2-int8-convrot.safetensors",
    "int8_text_encoder": "Breeze-TTS-2-int8-text-encoder.safetensors",
}
_MODEL_REPOSITORY_DIR = "drbaph_Breeze-TTS-2-comfyui"
_MODEL_LICENSE_HEADING = "BREEZEBLUE RESEARCH AND NON-COMMERCIAL LICENSE AGREEMENT"


def _pace_direction(speed: float) -> str:
    if speed <= 1.02:
        return "保持自然语速"
    if speed <= 1.08:
        return "整体语速略快但保持自然对话感"
    return "整体语速轻快但吐字清楚"


def _energy_direction(energy: float) -> str:
    if energy < 0.45:
        return "能量轻柔克制"
    if energy <= 0.65:
        return "保持中等能量"
    return "能量饱满但不喊叫"


def _sentence_marks_direction(prefix: str, values: tuple[int, ...]) -> str | None:
    if not values:
        return None
    numbers = "、".join(str(value) for value in values)
    return f"{prefix}第{numbers}句"


class BreezeTTS2Provider:
    """Breeze TTS 2 voice-clone provider backed by an isolated runtime."""

    name = "breeze_tts2"
    license_id = BREEZE_LICENSE_ID

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self._closed = False

    @property
    def runtime_root(self) -> Path:
        return Path(self.config.runtime_root)

    @property
    def model_root(self) -> Path:
        return Path(self.config.model_dir)

    @property
    def model_variant(self) -> str:
        return str(
            self.config.provider_options.get("model_variant", "int8_hybrid")
            or "int8_hybrid"
        ).strip().lower()

    @property
    def model_repository(self) -> Path:
        return self.model_root / _MODEL_REPOSITORY_DIR

    @property
    def external_python(self) -> Path:
        return Path(str(self.config.provider_options.get("external_python", "") or ""))

    @property
    def model_license(self) -> Path:
        return Path(
            str(self.config.provider_options.get("model_license_path", "") or "")
        )

    def _model_license_verified(self) -> bool:
        try:
            heading = self.model_license.read_text(encoding="utf-8")[:512]
        except (OSError, UnicodeDecodeError):
            return False
        return _MODEL_LICENSE_HEADING in heading and "Version 1.0" in heading

    def _missing_files(self) -> list[str]:
        missing: list[str] = []
        for relative in ("__init__.py", "loader.py", "nodes.py", "int8.py", "LICENSE"):
            if not (self.runtime_root / relative).is_file():
                missing.append(f"runtime:{relative}")
        weights = _VARIANT_WEIGHTS.get(self.model_variant)
        if weights is None:
            missing.append("model_variant")
        else:
            for relative in (
                "config.json",
                "tokenizer.json",
                weights,
                "audio_tokenizer/config.json",
                "audio_tokenizer/model.safetensors",
            ):
                if not (self.model_repository / relative).is_file():
                    missing.append(f"model:{relative}")
        if not self.external_python.is_file():
            missing.append("external_python")
        if not Path(self.config.reference_audio).is_file():
            missing.append("reference_audio")
        if not self.config.reference_text:
            missing.append("reference_text")
        if not self.model_license.is_file():
            missing.append("model_license")
        return missing

    def health(self) -> dict[str, Any]:
        if self._closed:
            return {
                "status": "unavailable",
                "provider": self.name,
                "reason_code": "TTS_CLOSED",
                "license_id": self.license_id,
            }
        if self.config.license_id != self.license_id:
            return {
                "status": "unavailable",
                "provider": self.name,
                "reason_code": "TTS_LICENSE_UNVERIFIED",
                "license_id": self.config.license_id,
            }
        if self.model_license.is_file() and not self._model_license_verified():
            return {
                "status": "unavailable",
                "provider": self.name,
                "reason_code": "TTS_LICENSE_UNVERIFIED",
                "license_id": self.config.license_id,
            }
        missing = self._missing_files()
        if missing:
            return {
                "status": "unavailable",
                "provider": self.name,
                "reason_code": "TTS_ASSET_MISSING",
                "missing": missing,
                "license_id": self.license_id,
            }
        return {
            "status": "available",
            "provider": self.name,
            "license_id": self.license_id,
            "model": f"Breeze-TTS-2-{self.model_variant.replace('_', '-')}",
            "model_variant": self.model_variant,
            "streaming": False,
            "delivery_mode": "single_pass_voice_performance_plan",
            "reference_audio_local_only": True,
            "offline_only": True,
            "execution": "external-process",
        }

    def performance_request(self, plan: object) -> dict[str, object]:
        """Map one persisted LLM plan into Breeze's non-spoken direction channel."""

        text = str(getattr(plan, "spoken_text", "") or "")
        emotion = str(
            getattr(plan, "short_instruction", "")
            or getattr(plan, "overall_emotion", "")
            or ""
        ).strip().rstrip("。")
        speed = float(getattr(plan, "global_speed", 1.0))
        energy = float(getattr(plan, "energy", 0.55))
        breaths = tuple(getattr(plan, "breath_before_sentences", ()) or ())
        emphasis = tuple(getattr(plan, "emphasize_sentences", ()) or ())
        instruction_parts = [emotion, _pace_direction(speed), _energy_direction(energy)]
        breath_direction = _sentence_marks_direction("在", breaths)
        if breath_direction:
            instruction_parts.append(breath_direction + "前自然换气")
        emphasis_direction = _sentence_marks_direction("轻轻强调", emphasis)
        if emphasis_direction:
            instruction_parts.append(emphasis_direction)
        instruction = "，".join(part for part in instruction_parts if part)
        options = self.config.provider_options
        units = tuple(getattr(plan, "speech_units")())
        gain_db = float(getattr(units[0], "gain_db", 0.0)) if units else 0.0
        return {
            "runtime_root": str(self.runtime_root),
            "model_dir": str(self.model_root),
            "reference_audio": self.config.reference_audio,
            "reference_text": self.config.reference_text,
            "text": text,
            "instruction": instruction,
            "voice_plan": {
                "emotion": str(getattr(plan, "overall_emotion", "") or ""),
                "speed": speed,
                "energy": energy,
                "breath_before_sentences": list(breaths),
                "emphasize_sentences": list(emphasis),
            },
            "model_variant": self.model_variant,
            "dtype": str(options.get("dtype", "bf16") or "bf16"),
            "device": str(options.get("device", "cuda") or "cuda"),
            "attention": str(options.get("attention", "eager") or "eager"),
            "decode_mode": str(options.get("decode_mode", "eager") or "eager"),
            "cfg_scale": float(options.get("cfg_scale", 4.0)),
            "seed": int(options.get("seed", 200717)),
            # The ordinary-reply contract rejects audio over 52 seconds.
            # Breeze emits 12.5 frames/s, so never spend GPU time beyond the
            # longest candidate the product can accept.
            "max_new_tokens": max(
                64, min(650, int(options.get("max_new_tokens", 650)))
            ),
            "temperature": float(options.get("temperature", 0.9)),
            "top_k": int(options.get("top_k", 50)),
            "top_p": float(options.get("top_p", 1.0)),
            "repetition_penalty": float(options.get("repetition_penalty", 1.1)),
            "depth_temperature": float(options.get("depth_temperature", 0.9)),
            "depth_top_k": int(options.get("depth_top_k", 50)),
            "depth_top_p": float(options.get("depth_top_p", 1.0)),
            "gain_db": max(-1.5, min(1.5, gain_db)),
            "quality_gate_required": True,
            "quality_forbidden_text": instruction,
            "quality_gate_model": str(options.get("quality_gate_model", "base") or "base"),
            "quality_gate_cache_root": str(options.get("quality_gate_cache_root", "") or ""),
            "quality_max_cer": 0.18,
            "duration_target_seconds": [40.0, 50.0],
            "max_attempts": 3,
            "performance_control_mode": "single_pass_llm_breeze_direction",
        }

    def stream_sentence(
        self, text: str, request: TTSRequest, sentence_index: int
    ) -> Iterator[AudioChunk]:
        del text, request, sentence_index
        health = self.health()
        if health.get("status") != "available":
            raise TTSUnavailable(str(health.get("reason_code", "TTS_UNAVAILABLE")))
        raise TTSUnavailable("TTS_PERFORMANCE_PLAN_REQUIRED")
        yield  # pragma: no cover - keeps this method an iterator

    def close(self) -> None:
        self._closed = True


__all__ = ["BREEZE_LICENSE_ID", "BreezeTTS2Provider"]
