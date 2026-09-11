"""Provider wire differences; unknown models use standard OpenAI parameters."""
from dataclasses import dataclass
import re
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelCapabilities:
    thinking: str = "none"
    reasoning_effort: str | None = None
    json_mode: bool = True
    stream_usage: bool = False
    tool_choice: bool = True

    def reasoning_parameters(self, enabled: bool) -> dict[str, object]:
        if self.thinking == "deepseek":
            result = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        elif self.thinking == "qwen":
            result = {"enable_thinking": enabled}
        else:
            return {}
        if enabled and self.reasoning_effort:
            result["reasoning_effort"] = self.reasoning_effort
        if enabled and self.thinking == "qwen" and self.reasoning_effort:
            result["max_completion_tokens"] = 10000
        return result


def model_capabilities(base_url: str, model: str, options: Mapping | None = None) -> ModelCapabilities:
    name = model.casefold()
    host = urlsplit(base_url).hostname
    if name.startswith("deepseek-v4-") or name == "deepseek-flash":
        defaults = ModelCapabilities("deepseek", "max", stream_usage=host == "api.deepseek.com", tool_choice=False)
    elif re.fullmatch(r"qwen3\.8-(?:flash|max)(?:-.*)?", name):
        defaults = ModelCapabilities("qwen", "high", stream_usage=True)
    elif re.fullmatch(r"qwen3\.[567]-(?:flash|plus|max)(?:-.*)?", name) or name in {"qwen-flash", "qwen-plus"}:
        defaults = ModelCapabilities("qwen", stream_usage=True)
    else:
        defaults = ModelCapabilities()
    overrides = (options or {}).get("capabilities", {})
    if not isinstance(overrides, Mapping) or set(overrides) - set(defaults.__dataclass_fields__):
        raise ValueError("invalid provider capabilities")
    values = {**defaults.__dict__, **overrides}
    if values['thinking'] not in {'none', 'deepseek', 'qwen'} or values['reasoning_effort'] not in {None, 'low', 'medium', 'high', 'max'}:
        raise ValueError("invalid provider reasoning capabilities")
    if any(type(values[k]) is not bool for k in ('json_mode', 'stream_usage', 'tool_choice')):
        raise ValueError("invalid provider boolean capabilities")
    return ModelCapabilities(**values)
