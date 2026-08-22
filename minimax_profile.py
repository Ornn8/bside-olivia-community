"""Validated inference profiles for MiniMax Music 3 experiments and production."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION = "p03.minimax-inference.v1"
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "seed",
        "text_cfg_scale",
        "top_k",
        "sampler_cfg_scale",
        "steps",
        "sampler_name",
        "scheduler",
        "denoise",
    }
)


class MiniMaxProfileError(ValueError):
    """Stable validation failure for a local inference profile."""


@dataclass(frozen=True)
class MiniMaxInferenceProfile:
    name: str
    seed: int
    text_cfg_scale: float
    top_k: int
    sampler_cfg_scale: float
    steps: int
    sampler_name: str = "euler"
    scheduler: str = "simple"
    denoise: float = 1.0
    schema_version: str = MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION:
            raise MiniMaxProfileError("MINIMAX_PROFILE_SCHEMA_UNSUPPORTED")
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise MiniMaxProfileError("MINIMAX_PROFILE_NAME_INVALID")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**63 - 1:
            raise MiniMaxProfileError("MINIMAX_PROFILE_SEED_INVALID")
        for name in ("text_cfg_scale", "sampler_cfg_scale"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not 0.1 <= float(value) <= 5.0:
                raise MiniMaxProfileError(f"MINIMAX_PROFILE_{name.upper()}_INVALID")
            object.__setattr__(self, name, float(value))
        if type(self.top_k) is not int or not 1 <= self.top_k <= 200:
            raise MiniMaxProfileError("MINIMAX_PROFILE_TOP_K_INVALID")
        if type(self.steps) is not int or not 1 <= self.steps <= 100:
            raise MiniMaxProfileError("MINIMAX_PROFILE_STEPS_INVALID")
        if self.sampler_name != "euler":
            raise MiniMaxProfileError("MINIMAX_PROFILE_SAMPLER_INVALID")
        if self.scheduler != "simple":
            raise MiniMaxProfileError("MINIMAX_PROFILE_SCHEDULER_INVALID")
        if type(self.denoise) not in {int, float} or not 0.1 <= float(self.denoise) <= 1.0:
            raise MiniMaxProfileError("MINIMAX_PROFILE_DENOISE_INVALID")
        object.__setattr__(self, "denoise", float(self.denoise))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "seed": self.seed,
            "text_cfg_scale": self.text_cfg_scale,
            "top_k": self.top_k,
            "sampler_cfg_scale": self.sampler_cfg_scale,
            "steps": self.steps,
            "sampler_name": self.sampler_name,
            "scheduler": self.scheduler,
            "denoise": self.denoise,
        }


CURRENT_MINIMAX_PROFILE = MiniMaxInferenceProfile(
    name="current-1.5",
    seed=200717,
    text_cfg_scale=1.5,
    top_k=50,
    sampler_cfg_scale=1.5,
    steps=30,
)

OFFICIAL_COMFY_MINIMAX_PROFILE = MiniMaxInferenceProfile(
    name="official-comfy-1.7",
    seed=200717,
    text_cfg_scale=1.7,
    top_k=50,
    sampler_cfg_scale=1.7,
    steps=30,
)


def minimax_profile_from_mapping(
    value: object,
    *,
    default: MiniMaxInferenceProfile = CURRENT_MINIMAX_PROFILE,
) -> MiniMaxInferenceProfile:
    """Load one exact profile object or return the explicit current baseline."""

    if value is None or value == {}:
        return default
    if not isinstance(value, Mapping) or set(value) != _PROFILE_FIELDS:
        raise MiniMaxProfileError("MINIMAX_PROFILE_FIELDS_INVALID")
    try:
        return MiniMaxInferenceProfile(
            schema_version=value["schema_version"],
            name=value["name"],
            seed=value["seed"],
            text_cfg_scale=value["text_cfg_scale"],
            top_k=value["top_k"],
            sampler_cfg_scale=value["sampler_cfg_scale"],
            steps=value["steps"],
            sampler_name=value["sampler_name"],
            scheduler=value["scheduler"],
            denoise=value["denoise"],
        )
    except KeyError as exc:
        raise MiniMaxProfileError("MINIMAX_PROFILE_FIELDS_INVALID") from exc


__all__ = [
    "CURRENT_MINIMAX_PROFILE",
    "MINIMAX_INFERENCE_PROFILE_SCHEMA_VERSION",
    "MiniMaxInferenceProfile",
    "MiniMaxProfileError",
    "OFFICIAL_COMFY_MINIMAX_PROFILE",
    "minimax_profile_from_mapping",
]
