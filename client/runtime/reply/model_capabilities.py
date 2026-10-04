"""Provider wire differences; unknown models use standard OpenAI parameters."""
from dataclasses import dataclass
import re
from typing import Mapping


@dataclass(frozen=True)
class ModelCapabilities:
    thinking: str = "none"
    reasoning_effort: str | None = None
    json_mode: bool = True
    stream_usage: bool = False
    tool_choice: bool = True
    tools: bool = True
    json_schema: bool = False

    @property
    def scoped_reasoning(self) -> bool:
        return self.thinking == "qwen" or (self.thinking != "none" and self.reasoning_effort is not None)

    def reasoning_parameters(self, enabled: bool) -> dict[str, object]:
        if self.thinking == "qwen":
            result = {"enable_thinking": enabled}
        else:
            return {}
        if enabled and self.reasoning_effort:
            result["reasoning_effort"] = self.reasoning_effort
        return result


def model_capabilities(base_url: str, model: str, options: Mapping | None = None) -> ModelCapabilities:
    if options is not None and not isinstance(options, Mapping):
        raise ValueError("invalid provider options")
    name = model.casefold()
    if re.fullmatch(r"qwen3\.8-(?:flash|max)(?:-.*)?", name):
        defaults = ModelCapabilities("qwen", "high", stream_usage=True)
    elif re.fullmatch(r"qwen3\.[567]-(?:flash|plus|max)(?:-.*)?", name) or name in {"qwen-flash", "qwen-plus"}:
        defaults = ModelCapabilities("qwen", stream_usage=True)
    elif name in {'claude-opus-5-5', 'claude-sonnet-5-5'}:
        # These models support tools, but reject forced tool_choice. The reply
        # adapter still validates that the returned tool matches its contract.
        defaults = ModelCapabilities(tool_choice=False, stream_usage=True)
    elif name in {'claude-opus-4-6', 'gemini-3.8-flash'}:
        defaults = ModelCapabilities(stream_usage=True)
    else:
        defaults = ModelCapabilities()
    overrides = (options or {}).get("capabilities", {})
    if not isinstance(overrides, Mapping) or set(overrides) - set(defaults.__dataclass_fields__):
        raise ValueError("invalid provider capabilities")
    values = {**defaults.__dict__, **overrides}
    if not isinstance(values['thinking'], str) or values['thinking'] not in {'none', 'qwen'}:
        raise ValueError("invalid provider thinking capabilities")
    if values['reasoning_effort'] is not None and (
        not isinstance(values['reasoning_effort'], str) or values['reasoning_effort'] not in {'low', 'medium', 'high', 'max'}
    ):
        raise ValueError("invalid provider reasoning capabilities")
    if any(type(values[k]) is not bool for k in ('json_mode', 'stream_usage', 'tool_choice', 'tools', 'json_schema')):
        raise ValueError("invalid provider boolean capabilities")
    return ModelCapabilities(**values)
