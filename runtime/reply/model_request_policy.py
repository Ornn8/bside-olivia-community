"""Task budgets layered on top of provider wire capabilities."""
from typing import Mapping
from urllib.parse import urlsplit

from runtime.reply.model_capabilities import model_capabilities


def reasoning_request_parameters(
    base_url: str, model: str, options: Mapping, *, purpose: str | None, enabled: bool,
) -> dict[str, object]:
    capabilities = model_capabilities(base_url, model, options)
    parameters = capabilities.reasoning_parameters(enabled)
    if not enabled or capabilities.thinking == "none":
        return parameters
    if capabilities.thinking == "qwen" and capabilities.reasoning_effort:
        parameters["max_completion_tokens"] = 10000
    endpoint = urlsplit(base_url)
    if (
        capabilities.thinking == "deepseek"
        and model.casefold() in {"deepseek-v4-flash", "deepseek-flash"}
        and endpoint.scheme == "https"
        and endpoint.hostname == "api.deepseek.com"
        and endpoint.path.rstrip("/") in {"", "/v1"}
        and purpose in {"text_letter_max_reasoning", "background_reasoning"}
    ):
        # Explicit capability overrides take precedence over task defaults.
        if "reasoning_effort" not in options.get("capabilities", {}):
            parameters["reasoning_effort"] = "high"
        parameters["max_tokens"] = 10000
    return parameters
