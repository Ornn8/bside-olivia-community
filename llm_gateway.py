"""Small, replaceable text-generation gateway for the local compatibility layer.

The module deliberately owns provider I/O, configuration, input validation and
safe error categories.  It does not know about letter storage or the legacy
read-only material store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

import aiohttp


PROVIDER_USER_AGENT = "Olivia-Community/0.1"


ALLOWED_ROLES = frozenset({"system", "user", "assistant"})
SUPPORTED_API_STYLES = frozenset({"chat_completions", "responses"})


class GatewayError(RuntimeError):
    """A sanitized provider failure safe for application-level handling."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status: int | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.status = status
        super().__init__(code)


class ProviderUnavailable(GatewayError):
    def __init__(self) -> None:
        super().__init__("PROVIDER_UNAVAILABLE", retryable=True)


class ProviderTimeout(GatewayError):
    def __init__(self) -> None:
        super().__init__("PROVIDER_TIMEOUT", retryable=True)


class ProviderRetryableError(GatewayError):
    def __init__(self, status: int | None = None) -> None:
        super().__init__("PROVIDER_RETRYABLE", retryable=True, status=status)


class ProviderRejected(GatewayError):
    def __init__(self, status: int) -> None:
        super().__init__("PROVIDER_REJECTED", retryable=False, status=status)


class ProviderProtocolError(GatewayError):
    def __init__(self) -> None:
        super().__init__("PROVIDER_PROTOCOL", retryable=False)


class InvalidGatewayInput(GatewayError):
    def __init__(self, code: str = "INVALID_INPUT") -> None:
        super().__init__(code, retryable=False)


@dataclass(frozen=True)
class GatewayConfig:
    """Runtime-safe provider settings.

    A key is addressed by environment-variable name only.  The value itself
    is never part of this object or its public status representation.
    """

    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    api_style: str = "chat_completions"
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.0
    stream: bool = False
    max_input_chars: int = 10000
    max_output_chars: int = 10000
    fallback_provider: str = "none"
    persona_file: str = ""
    persona_config: str = "linli_character/persona_config.json"
    persona_evidence_file: str = "linli_character/provenance.json"
    persona_v2_file: str = "linli_character/persona_release_v2.json"
    persona_v2_enabled: bool = True
    feature_enabled: bool = True
    requires_api_key: bool = False
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "GatewayConfig":
        data = dict(raw or {})
        legacy_backend = str(data.get("backend", "")).strip().lower()
        provider = str(data.get("provider", legacy_backend or "none")).strip().lower()
        provider = {
            "openai": "openai_compatible",
            "openai-compatible": "openai_compatible",
            "openai_compatible_api": "openai_compatible",
            "offline": "mock",
            "deterministic": "mock",
            "disabled": "none",
            "unconfigured": "none",
        }.get(provider, provider)

        base_url = data.get("base_url")
        model = data.get("model")
        api_key_env = data.get("api_key_env")
        api_style = data.get("api_style", "chat_completions")
        if legacy_backend == "openai":
            base_url = base_url or data.get("openai_base", "")
            model = model or data.get("openai_model", "")
            api_key_env = api_key_env or "OLIVIA_LLM_API_KEY"
        elif legacy_backend == "ollama":
            base_url = base_url or data.get("ollama_url", "")
            model = model or data.get("ollama_model", "")
            provider = "openai_compatible"
            api_style = "chat_completions"

        options = data.get("provider_options", {})
        if not isinstance(options, Mapping):
            options = {}

        return cls(
            provider=provider,
            base_url=str(base_url or "").strip(),
            model=str(model or "").strip(),
            api_key_env=str(api_key_env or "").strip(),
            api_style=_normalize_api_style(api_style),
            timeout_seconds=_bounded_float(data.get("timeout_seconds", 30.0), 30.0, 0.05, 600.0),
            max_retries=_bounded_int(data.get("max_retries", 2), 2, 0, 8),
            retry_backoff_seconds=_bounded_float(
                data.get("retry_backoff_seconds", 0.0), 0.0, 0.0, 30.0
            ),
            stream=_as_bool(data.get("stream", False)),
            max_input_chars=_bounded_int(data.get("max_input_chars", 10000), 10000, 1, 100000),
            max_output_chars=_bounded_int(data.get("max_output_chars", 10000), 10000, 1, 100000),
            fallback_provider=str(data.get("fallback_provider", "none") or "none").strip().lower(),
            persona_file=str(data.get("persona_file", "") or ""),
            persona_config=str(data.get("persona_config", "linli_character/persona_config.json") or ""),
            persona_evidence_file=str(
                data.get("persona_evidence_file", "linli_character/provenance.json") or ""
            ),
            persona_v2_file=str(
                data.get("persona_v2_file", "linli_character/persona_release_v2.json") or ""
            ),
            persona_v2_enabled=_as_bool(data.get("persona_v2_enabled", True), True),
            feature_enabled=_as_bool(data.get("feature_enabled", True), True),
            requires_api_key=_as_bool(data.get("requires_api_key", options.get("requires_api_key", False))),
            provider_options=dict(options),
        )

    def public_dict(self, *, api_key_configured: bool | None = None) -> dict[str, Any]:
        """Return health/config metadata without secrets or request data."""

        result: dict[str, Any] = {
            "provider": self.provider,
            "base_url_configured": bool(self.base_url),
            "model_configured": bool(self.model),
            "api_key_env_configured": bool(self.api_key_env),
            "api_style": self.api_style,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "stream": self.stream,
            "max_input_chars": self.max_input_chars,
            "fallback_provider": self.fallback_provider,
            "feature_enabled": self.feature_enabled,
            "persona_configured": bool(self.persona_config),
            "persona_evidence_configured": bool(self.persona_evidence_file),
            "persona_v2_enabled": self.persona_v2_enabled,
        }
        if api_key_configured is not None:
            result["api_key_configured"] = api_key_configured
        if self.model:
            result["model"] = self.model
        return result


@dataclass(frozen=True)
class GatewayResponse:
    text: str
    request_id: str
    provider: str
    model: str
    streamed: bool = False


@dataclass(frozen=True)
class GatewayToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class GatewayDelta:
    text: str
    request_id: str
    index: int = 0
    finish_reason: str | None = None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _normalize_api_style(value: Any) -> str:
    style = str(value or "chat_completions").strip().lower().replace("-", "_")
    if style in {"chat", "chatcompletion", "chat_completions"}:
        return "chat_completions"
    if style in {"response", "responses"}:
        return "responses"
    return "chat_completions"


def _env_overlay(environ: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    direct = {
        "OLIVIA_LLM_PROVIDER": "provider",
        "OLIVIA_LLM_BASE_URL": "base_url",
        "OLIVIA_LLM_MODEL": "model",
        "OLIVIA_LLM_API_KEY_ENV": "api_key_env",
        "OLIVIA_LLM_API_STYLE": "api_style",
        "OLIVIA_LLM_TIMEOUT_SECONDS": "timeout_seconds",
        "OLIVIA_LLM_MAX_RETRIES": "max_retries",
        "OLIVIA_LLM_RETRY_BACKOFF_SECONDS": "retry_backoff_seconds",
        "OLIVIA_LLM_STREAM": "stream",
        "OLIVIA_LLM_MAX_INPUT_CHARS": "max_input_chars",
        "OLIVIA_LLM_MAX_OUTPUT_CHARS": "max_output_chars",
        "OLIVIA_LLM_FALLBACK_PROVIDER": "fallback_provider",
        "OLIVIA_PERSONA_FILE": "persona_file",
        "OLIVIA_PERSONA_CONFIG": "persona_config",
        "OLIVIA_PERSONA_EVIDENCE_FILE": "persona_evidence_file",
        "OLIVIA_PERSONA_V2_FILE": "persona_v2_file",
        "OLIVIA_PERSONA_V2_ENABLED": "persona_v2_enabled",
        "OLIVIA_LLM_FEATURE_ENABLED": "feature_enabled",
        "OLIVIA_LLM_REQUIRES_API_KEY": "requires_api_key",
    }
    for env_name, field_name in direct.items():
        if environ.get(env_name):
            values[field_name] = environ[env_name]

    # Keep the prior environment names readable, but never read a key value
    # from a JSON config or store it in the normalized settings.
    legacy = {
        "OLIVIA_LLM_BACKEND": "backend",
        "OLIVIA_OLLAMA_URL": "ollama_url",
        "OLIVIA_OLLAMA_MODEL": "ollama_model",
        "OLIVIA_OPENAI_BASE": "openai_base",
        "OLIVIA_OPENAI_MODEL": "openai_model",
    }
    for env_name, field_name in legacy.items():
        if environ.get(env_name) and field_name not in values:
            values[field_name] = environ[env_name]
    if environ.get("OLIVIA_OPENAI_KEY") and "api_key_env" not in values:
        values["api_key_env"] = "OLIVIA_LLM_API_KEY"
    return values


def load_gateway_config(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> GatewayConfig:
    """Load ignored local settings and runtime environment overrides."""

    root = Path(__file__).resolve().parent
    config_path = Path(path) if path is not None else root / "llm_config.json"
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                data.update(loaded)
        except (OSError, UnicodeError, json.JSONDecodeError):
            # A malformed local file must disable network I/O rather than
            # turning startup into an opaque exception.
            data = {"provider": "none"}
    data.update(_env_overlay(environ or os.environ))
    return GatewayConfig.from_mapping(data)


def validate_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_input_chars: int,
) -> tuple[dict[str, str], ...]:
    """Validate the narrow chat message shape before any provider call."""

    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        raise InvalidGatewayInput("INVALID_MESSAGES")
    normalized: list[dict[str, str]] = []
    total = 0
    for message in messages:
        if not isinstance(message, Mapping):
            raise InvalidGatewayInput("INVALID_MESSAGE")
        role = message.get("role")
        content = message.get("content")
        if role not in ALLOWED_ROLES:
            raise InvalidGatewayInput("INVALID_ROLE")
        if not isinstance(content, str) or not content.strip():
            raise InvalidGatewayInput("INVALID_MESSAGE_CONTENT")
        total += len(content)
        if total > max_input_chars:
            raise InvalidGatewayInput("INPUT_TOO_LONG")
        normalized.append({"role": role, "content": content})
    return tuple(normalized)


class Gateway:
    """Protocol-like base class implemented by all text providers."""

    stream_enabled = False
    network_call_count = 0

    def mark_network_call(self) -> None:
        """Record an actual provider-bound network attempt for public evidence."""

        self.network_call_count = int(getattr(self, "network_call_count", 0)) + 1

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str | None = None,
    ) -> GatewayResponse:
        raise NotImplementedError

    async def complete_with_tools(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, object]],
        tool_choice: str,
        request_id: str | None = None,
    ) -> Sequence[GatewayToolCall]:
        raise ProviderUnavailable()

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[GatewayDelta]:
        response = await self.complete(messages, request_id=request_id)
        yield GatewayDelta(response.text, response.request_id, finish_reason="stop")


class UnconfiguredAdapter(Gateway):
    async def complete(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> GatewayResponse:
        raise ProviderUnavailable()


class FallbackAdapter(Gateway):
    """Use an offline/custom fallback only for retryable primary failures."""

    def __init__(self, primary: Gateway, fallback: Gateway) -> None:
        self.primary = primary
        self.fallback = fallback
        self.stream_enabled = bool(getattr(primary, "stream_enabled", False))

    @property
    def network_call_count(self) -> int:
        return int(getattr(self.primary, "network_call_count", 0)) + int(
            getattr(self.fallback, "network_call_count", 0)
        )

    async def complete(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> GatewayResponse:
        try:
            return await self.primary.complete(messages, request_id=request_id)
        except GatewayError as exc:
            if not exc.retryable:
                raise
            return await self.fallback.complete(messages, request_id=request_id)

    async def complete_with_tools(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, object]],
        tool_choice: str,
        request_id: str | None = None,
    ) -> Sequence[GatewayToolCall]:
        try:
            return await self.primary.complete_with_tools(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                request_id=request_id,
            )
        except GatewayError as exc:
            if not exc.retryable:
                raise
            return await self.fallback.complete_with_tools(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                request_id=request_id,
            )

    async def stream(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> AsyncIterator[GatewayDelta]:
        emitted = False
        try:
            async for delta in self.primary.stream(messages, request_id=request_id):
                if delta.text:
                    emitted = True
                yield delta
            return
        except GatewayError as exc:
            if emitted or not exc.retryable:
                raise
        async for delta in self.fallback.stream(messages, request_id=request_id):
            yield delta


class OfflineDeterministicAdapter(Gateway):
    """A no-network deterministic response useful for local smoke tests."""

    stream_enabled = True

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    @staticmethod
    def _user_text(messages: Sequence[Mapping[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    def _text(self, messages: Sequence[Mapping[str, Any]]) -> str:
        user_text = self._user_text(messages)
        digest = hashlib.sha256(user_text.encode("utf-8")).hexdigest()[:8]
        configured = self.config.provider_options.get("response_text")
        if isinstance(configured, str) and configured.strip():
            text = configured.strip()
        else:
            text = f"（离线回信 {digest}）我收到你的来信了。谢谢你愿意把这些话告诉我，我会认真读完。"
        return text[: self.config.max_output_chars]

    async def complete(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> GatewayResponse:
        normalized = validate_messages(messages, max_input_chars=self.config.max_input_chars)
        delay = self.config.provider_options.get("delay_seconds", 0)
        try:
            delay_value = max(0.0, float(delay))
        except (TypeError, ValueError):
            delay_value = 0.0
        if delay_value:
            await asyncio.sleep(delay_value)
        request = request_id or uuid.uuid4().hex
        text = self._text(normalized)
        if not text:
            raise ProviderProtocolError()
        return GatewayResponse(text, request, self.config.provider, self.config.model or "offline")

    async def complete_with_tools(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, object]],
        tool_choice: str,
        request_id: str | None = None,
    ) -> Sequence[GatewayToolCall]:
        validate_messages(messages, max_input_chars=self.config.max_input_chars)
        if tool_choice != "required":
            raise InvalidGatewayInput("REQUIRED_TOOL_CHOICE")
        configured = self.config.provider_options.get("tool_call")
        if not isinstance(configured, Mapping):
            raise ProviderProtocolError()
        name = configured.get("name")
        arguments = configured.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            raise ProviderProtocolError()
        return (GatewayToolCall(name=name, arguments=dict(arguments)),)

    async def stream(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> AsyncIterator[GatewayDelta]:
        response = await self.complete(messages, request_id=request_id)
        width = _bounded_int(self.config.provider_options.get("chunk_size", 12), 12, 1, 256)
        for index in range(0, len(response.text), width):
            yield GatewayDelta(
                response.text[index : index + width],
                response.request_id,
                index=index // width,
            )
        yield GatewayDelta("", response.request_id, index=(len(response.text) + width - 1) // width, finish_reason="stop")


class OpenAICompatibleAdapter(Gateway):
    """Chat Completions and Responses shaped HTTP adapter."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        key_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        self.config = config
        self.stream_enabled = bool(config.stream)
        self._key_resolver = key_resolver

    def _key(self) -> str | None:
        if self._key_resolver is not None:
            value = self._key_resolver()
            return value if value else None
        if not self.config.api_key_env:
            return None
        value = os.environ.get(self.config.api_key_env)
        return value if value else None

    def _ensure_configured(self) -> str | None:
        key = self._key()
        if not self.config.feature_enabled or not self.config.base_url or not self.config.model:
            raise ProviderUnavailable()
        if self.config.requires_api_key and not key:
            raise ProviderUnavailable()
        return key

    def _url(self) -> str:
        suffix = "responses" if self.config.api_style == "responses" else "chat/completions"
        return self.config.base_url.rstrip("/") + "/" + suffix

    def _headers(self, key: str | None, request_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": PROVIDER_USER_AGENT,
        }
        if key:
            headers["Authorization"] = "Bearer " + key
        if request_id.startswith("letter-reply:"):
            headers["Idempotency-Key"] = request_id
        headers["X-Request-ID"] = request_id
        return headers

    def _uses_max_reasoning(self, request_id: str) -> bool:
        return (
            self.config.provider == "openai_compatible"
            and self.config.api_style == "chat_completions"
            and self.config.model.casefold() == "deepseek-v4-flash"
            and request_id.startswith(("letter-reply:", "quality-"))
        )

    def _request_timeout_seconds(self, *, max_reasoning: bool) -> float:
        if max_reasoning:
            return max(self.config.timeout_seconds, 600.0)
        return self.config.timeout_seconds

    def _body(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        stream: bool,
        max_reasoning: bool = False,
    ) -> dict[str, Any]:
        normalized = validate_messages(messages, max_input_chars=self.config.max_input_chars)
        if self.config.api_style == "responses":
            request_input = [
                {"role": message["role"], "content": message["content"]}
                for message in normalized
            ]
            return {"model": self.config.model, "input": request_input, "stream": stream}
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(normalized),
            "stream": stream,
        }
        if (
            max_reasoning
            and self.config.provider == "openai_compatible"
            and self.config.model.casefold() == "deepseek-v4-flash"
        ):
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = "max"
        return body

    async def _retry_wait(self, attempt: int) -> None:
        delay = self.config.retry_backoff_seconds * (attempt + 1)
        if delay:
            await asyncio.sleep(delay)

    async def _post_json(
        self,
        body: dict[str, Any],
        request_id: str,
        *,
        max_reasoning: bool = False,
    ) -> dict[str, Any]:
        key = self._ensure_configured()
        timeout = aiohttp.ClientTimeout(
            total=self._request_timeout_seconds(max_reasoning=max_reasoning)
        )
        for attempt in range(self.config.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    self.mark_network_call()
                    async with session.post(
                        self._url(),
                        json=body,
                        headers=self._headers(key, request_id),
                    ) as response:
                        status = response.status
                        if status == 429 or status >= 500:
                            if attempt < self.config.max_retries:
                                await self._retry_wait(attempt)
                                continue
                            raise ProviderRetryableError(status)
                        if status >= 400:
                            raise ProviderRejected(status)
                        try:
                            raw = await response.text()
                            data = json.loads(raw)
                        except (UnicodeError, json.JSONDecodeError, TypeError):
                            raise ProviderProtocolError() from None
                        if not isinstance(data, Mapping) or not data:
                            raise ProviderProtocolError()
                        return dict(data)
            except asyncio.TimeoutError:
                if attempt < self.config.max_retries:
                    await self._retry_wait(attempt)
                    continue
                raise ProviderTimeout() from None
            except aiohttp.ClientError:
                if attempt < self.config.max_retries:
                    await self._retry_wait(attempt)
                    continue
                raise ProviderUnavailable() from None
        raise ProviderUnavailable()

    async def complete(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> GatewayResponse:
        request = request_id or uuid.uuid4().hex
        max_reasoning = self._uses_max_reasoning(request)
        body = self._body(
            messages,
            stream=False,
            max_reasoning=max_reasoning,
        )
        data = await self._post_json(body, request, max_reasoning=max_reasoning)
        text = _extract_response_text(data)
        if not text:
            raise ProviderProtocolError()
        if len(text) > self.config.max_output_chars:
            raise InvalidGatewayInput("OUTPUT_TOO_LONG")
        return GatewayResponse(text, request, self.config.provider, self.config.model)

    async def complete_with_tools(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, object]],
        tool_choice: str,
        request_id: str | None = None,
    ) -> Sequence[GatewayToolCall]:
        if tool_choice != "required":
            raise InvalidGatewayInput("REQUIRED_TOOL_CHOICE")
        request = request_id or uuid.uuid4().hex
        body = self._body(messages, stream=False)
        if self.config.api_style == "responses":
            converted: list[dict[str, object]] = []
            for tool in tools:
                function = tool.get("function") if isinstance(tool, Mapping) else None
                if not isinstance(function, Mapping):
                    raise InvalidGatewayInput("INVALID_TOOL")
                converted.append(
                    {
                        "type": "function",
                        "name": function.get("name"),
                        "description": function.get("description", ""),
                        "parameters": function.get("parameters", {}),
                    }
                )
            body["tools"] = converted
        else:
            body["tools"] = list(tools)
        if not (
            self.config.provider == "openai_compatible"
            and self.config.api_style == "chat_completions"
            and self.config.model.casefold() == "deepseek-v4-flash"
        ):
            body["tool_choice"] = tool_choice
        data = await self._post_json(body, request)
        calls = _extract_tool_calls(data)
        if not calls:
            raise ProviderProtocolError()
        return calls

    async def stream(self, messages: Sequence[Mapping[str, Any]], *, request_id: str | None = None) -> AsyncIterator[GatewayDelta]:
        request = request_id or uuid.uuid4().hex
        max_reasoning = self._uses_max_reasoning(request)
        body = self._body(
            messages,
            stream=True,
            max_reasoning=max_reasoning,
        )
        key = self._ensure_configured()
        timeout = aiohttp.ClientTimeout(
            total=self._request_timeout_seconds(max_reasoning=max_reasoning)
        )
        emitted = False
        for attempt in range(self.config.max_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    self.mark_network_call()
                    async with session.post(
                        self._url(),
                        json=body,
                        headers=self._headers(key, request),
                    ) as response:
                        status = response.status
                        if status == 429 or status >= 500:
                            if attempt < self.config.max_retries:
                                await self._retry_wait(attempt)
                                continue
                            raise ProviderRetryableError(status)
                        if status >= 400:
                            raise ProviderRejected(status)
                        saw_delta = False
                        index = 0
                        async for raw_line in response.content:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line or not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                data = json.loads(payload)
                            except (UnicodeError, json.JSONDecodeError):
                                raise ProviderProtocolError() from None
                            text = _extract_stream_text(data)
                            finish_reason = _extract_finish_reason(data)
                            if text:
                                saw_delta = True
                                emitted = True
                                yield GatewayDelta(text, request, index=index)
                                index += 1
                            if finish_reason:
                                yield GatewayDelta("", request, index=index, finish_reason=finish_reason)
                                break
                        if not saw_delta:
                            raise ProviderProtocolError()
                        return
            except asyncio.TimeoutError:
                if emitted:
                    raise ProviderTimeout() from None
                if attempt < self.config.max_retries:
                    await self._retry_wait(attempt)
                    continue
                raise ProviderTimeout() from None
            except aiohttp.ClientError:
                if emitted:
                    raise ProviderUnavailable() from None
                if attempt < self.config.max_retries:
                    await self._retry_wait(attempt)
                    continue
                raise ProviderUnavailable() from None
        raise ProviderUnavailable()


def _extract_response_text(data: Mapping[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, Mapping):
            message = choice.get("message", choice)
            if isinstance(message, Mapping):
                return _content_to_text(message.get("content", ""))
    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
            elif isinstance(content, str):
                parts.append(content)
        return "".join(parts).strip()
    return ""


def _extract_tool_calls(data: Mapping[str, Any]) -> tuple[GatewayToolCall, ...]:
    raw_calls: list[Mapping[str, Any]] = []
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, Mapping):
            message = choice.get("message", choice)
            if isinstance(message, Mapping) and isinstance(message.get("tool_calls"), list):
                tool_calls = message["tool_calls"]
                if any(not isinstance(item, Mapping) for item in tool_calls):
                    raise ProviderProtocolError()
                if any(not isinstance(item.get("function"), Mapping) for item in tool_calls):
                    raise ProviderProtocolError()
                raw_calls.extend(tool_calls)
    output = data.get("output")
    if isinstance(output, list):
        raw_calls.extend(
            item
            for item in output
            if isinstance(item, Mapping) and item.get("type") == "function_call"
        )

    calls: list[GatewayToolCall] = []
    for raw in raw_calls:
        function = raw.get("function")
        source = function if isinstance(function, Mapping) else raw
        name = source.get("name")
        arguments = source.get("arguments")
        if not isinstance(name, str) or not name:
            raise ProviderProtocolError()
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                raise ProviderProtocolError() from None
        if not isinstance(arguments, Mapping):
            raise ProviderProtocolError()
        calls.append(GatewayToolCall(name=name, arguments=dict(arguments)))
    return tuple(calls)


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def _extract_stream_text(data: Mapping[str, Any]) -> str:
    delta = data.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, Mapping):
        return _content_to_text(delta.get("content", delta.get("text", "")))
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        choice_delta = choices[0].get("delta", {})
        if isinstance(choice_delta, Mapping):
            return _content_to_text(choice_delta.get("content", ""))
    if data.get("type") in {"response.output_text.delta", "response.content_part.added"}:
        return _content_to_text(data.get("delta", data.get("text", "")))
    return ""


def _extract_finish_reason(data: Mapping[str, Any]) -> str | None:
    if isinstance(data.get("finish_reason"), str):
        return data["finish_reason"]
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str):
            return reason
    event_type = data.get("type")
    if event_type in {"response.completed", "response.done"}:
        return "stop"
    return None


ProviderFactory = Callable[[GatewayConfig], Gateway]
_PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    normalized = str(name).strip().lower()
    if not normalized or not callable(factory):
        raise ValueError("provider registration requires a name and callable")
    _PROVIDERS[normalized] = factory


def create_gateway(
    config: GatewayConfig,
    *,
    key_resolver: Callable[[], str | None] | None = None,
) -> Gateway:
    def build_base(
        current: GatewayConfig,
        resolver: Callable[[], str | None] | None = None,
    ) -> Gateway:
        if not current.feature_enabled or current.provider in {"", "none"}:
            return UnconfiguredAdapter()
        if current.provider == "mock":
            return OfflineDeterministicAdapter(current)
        if current.provider == "openai_compatible":
            return OpenAICompatibleAdapter(current, key_resolver=resolver)
        factory = _PROVIDERS.get(current.provider)
        if factory is None:
            raise ProviderUnavailable()
        return factory(current)

    primary = build_base(config, key_resolver)
    fallback_name = config.fallback_provider.strip().lower()
    if fallback_name in {"", "none", config.provider}:
        return primary
    fallback = build_base(
        replace(
            config,
            provider=fallback_name,
            base_url="",
            api_key_env="",
            requires_api_key=False,
            fallback_provider="none",
        )
    )
    return FallbackAdapter(primary, fallback)


def api_key_configured(config: GatewayConfig, *, environ: Mapping[str, str] | None = None) -> bool:
    if not config.api_key_env:
        return False
    return bool((environ or os.environ).get(config.api_key_env))


# Friendly names for integrations that describe the same adapters as gateways.
OpenAICompatibleGateway = OpenAICompatibleAdapter
MockAdapter = OfflineDeterministicAdapter
LLMConfig = GatewayConfig


__all__ = [
    "ALLOWED_ROLES",
    "Gateway",
    "GatewayConfig",
    "GatewayDelta",
    "GatewayError",
    "GatewayToolCall",
    "FallbackAdapter",
    "GatewayResponse",
    "InvalidGatewayInput",
    "LLMConfig",
    "MockAdapter",
    "OfflineDeterministicAdapter",
    "OpenAICompatibleAdapter",
    "OpenAICompatibleGateway",
    "ProviderFactory",
    "ProviderProtocolError",
    "ProviderRejected",
    "ProviderRetryableError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "UnconfiguredAdapter",
    "api_key_configured",
    "create_gateway",
    "load_gateway_config",
    "register_provider",
    "validate_messages",
]
