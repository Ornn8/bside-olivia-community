from types import SimpleNamespace

import pytest

from runtime.memory import mem0_memory


def test_memory_extraction_overrides_high_effort_with_low_only_on_memory_client(monkeypatch):
    backend, calls, original = _raw_response_backend(
        monkeypatch, "https://api.deepseek.com", model="deepseek-v4-flash")
    backend.llm.generate_response(messages=[], reasoning_effort="max")
    assert calls[-1]["reasoning_effort"] == "low"
    original(messages=[], reasoning_effort="max")
    assert calls[-1]["reasoning_effort"] == "max"


def _raw_response_backend(monkeypatch, base_url, finish_reason="stop", content='{"memory": []}', model=""):
    calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=content, reasoning_content="private reasoning"))])
    def create(**kwargs):
        calls.append(kwargs)
        return response
    completions = SimpleNamespace(create=create)
    provider = SimpleNamespace(client=SimpleNamespace(base_url=base_url,
        chat=SimpleNamespace(completions=completions)))
    provider.generate_response = lambda **kwargs: completions.create(**kwargs).choices[0].message.content
    memory = SimpleNamespace(llm=provider)
    monkeypatch.setattr(mem0_memory, "_load_product_mem0_module", lambda: SimpleNamespace(
        Memory=SimpleNamespace(from_config=lambda config: memory)))
    return mem0_memory._default_factory({"llm": {"config": {"model": model}}}), calls, create


@pytest.mark.parametrize("base_url,disabled", [
    ("https://api.deepseek.com/v1", True), ("https://api.deepseek.com", True),
    ("https://api.deepseek.com.evil.invalid/v1", False), ("https://other.invalid/v1", False),
])
def test_memory_client_isolates_thinking_and_never_projects_reasoning(monkeypatch, base_url, disabled):
    backend, calls, original_create = _raw_response_backend(monkeypatch, base_url)
    extra = {"other": "preserved", "thinking": {"type": "enabled"}}
    result = backend.llm.generate_response(messages=[], response_format={"type": "json_object"}, extra_body=extra)
    assert result == '{"memory": []}'
    assert calls[0]["extra_body"]["thinking"]["type"] == ("disabled" if disabled else "enabled")
    assert calls[0]["extra_body"]["other"] == "preserved"
    assert extra["thinking"]["type"] == "enabled"
    # The original callable is unchanged and can still serve an independent client.
    original_create(extra_body=extra)
    assert calls[-1]["extra_body"]["thinking"]["type"] == "enabled"


@pytest.mark.parametrize("content", ["", '{"memory": []}'])
def test_memory_client_rejects_length_even_when_content_is_valid_json(monkeypatch, content):
    backend, _, _ = _raw_response_backend(monkeypatch, "https://other.invalid/v1", "length", content)
    with pytest.raises(mem0_memory.Mem0AdapterError, match="^MEM0_EXTRACTION_RESPONSE_TRUNCATED$"):
        backend.llm.generate_response(messages=[], response_format={"type": "json_object"})


@pytest.mark.parametrize("base_url,model,disabled", [
    ("https://opencode.ai/zen/go/v1", "deepseek-v4-flash", False),
    ("https://opencode.ai/zen/go/v1/", "deepseek-v4-pro", True),
    ("https://opencode.ai/zen/go/v1", "other-model", False),
    ("https://opencode.ai/zen/v1", "deepseek-v4-flash", False),
    ("https://opencode.ai.evil.invalid/zen/go/v1", "deepseek-v4-flash", False),
])
def test_go_deepseek_memory_thinking_is_isolated_by_route_and_model(monkeypatch, base_url, model, disabled):
    backend, calls, original_create = _raw_response_backend(monkeypatch, base_url, model=model)
    extra = {"thinking": {"type": "enabled"}}
    backend.llm.generate_response(messages=[], response_format={"type":"json_object"},
        extra_body=extra, reasoning_effort="low")
    assert calls[0]["extra_body"]["thinking"]["type"] == ("disabled" if disabled else "enabled")
    assert calls[0]["reasoning_effort"] == (
        "low"
    )
    original_create(extra_body=extra)
    assert calls[-1]["extra_body"]["thinking"]["type"] == "enabled"


@pytest.mark.parametrize("base_url", [
    "https://opencode.ai/zen/go/v1", "https://opencode.ai/zen/go/v1/",
    "https://api.deepseek.com", "https://api.deepseek.com/",
    "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/",
])
def test_flash_memory_explicitly_uses_low_reasoning_without_mutating_other_params(monkeypatch, base_url):
    backend, calls, _ = _raw_response_backend(monkeypatch, base_url, model="deepseek-v4-flash")
    extra = {"thinking": {"type": "disabled"}, "other": "preserved"}
    result = backend.llm.generate_response(messages=[], response_format={"type": "json_object"},
        extra_body=extra, reasoning_effort="low", max_tokens=2000, timeout=30)
    assert calls[0]["extra_body"] == {"thinking": {"type": "enabled"}, "other": "preserved"}
    assert calls[0]["reasoning_effort"] == "low"
    assert calls[0]["max_tokens"] == 2000 and calls[0]["timeout"] == 30
    assert extra["thinking"]["type"] == "disabled"
    assert result == '{"memory": []}' and "private reasoning" not in result


@pytest.mark.parametrize("base_url,model,expected_thinking", [
    ("https://api.deepseek.com/v1", "deepseek-chat", "disabled"),
    ("https://api.deepseek.com/v1", "deepseek-reasoner", "disabled"),
    ("https://api.deepseek.com/v1", "deepseek-v4-pro", "disabled"),
    ("https://api.deepseek.com/v1", "other-model", "disabled"),
    ("http://api.deepseek.com/v1", "deepseek-v4-flash", "disabled"),
    ("https://api.deepseek.com/other", "deepseek-v4-flash", "disabled"),
    ("https://api.deepseek.com.evil.invalid/v1", "deepseek-v4-flash", "enabled"),
])
def test_official_flash_reasoning_preserves_other_routes_and_models(
    monkeypatch, base_url, model, expected_thinking,
):
    backend, calls, _ = _raw_response_backend(monkeypatch, base_url, model=model)
    backend.llm.generate_response(messages=[], extra_body={"thinking": {"type": "enabled"}},
        reasoning_effort="low")
    assert calls[0]["extra_body"]["thinking"]["type"] == expected_thinking
    assert calls[0]["reasoning_effort"] == "low"


@pytest.mark.parametrize("content", ["", '{"memory": []}'])
def test_go_flash_low_reasoning_still_rejects_truncated_extraction(monkeypatch, content):
    backend, calls, _ = _raw_response_backend(monkeypatch, "https://opencode.ai/zen/go/v1",
        finish_reason="length", content=content, model="deepseek-v4-flash")
    with pytest.raises(mem0_memory.Mem0AdapterError, match="^MEM0_EXTRACTION_RESPONSE_TRUNCATED$"):
        backend.llm.generate_response(messages=[], response_format={"type": "json_object"})
    assert calls[0]["extra_body"]["thinking"]["type"] == "enabled"
    assert calls[0]["reasoning_effort"] == "low"


@pytest.mark.parametrize("base_url,model,omit_mode", [
    ("https://opencode.ai/zen/go/v1", "deepseek-v4-flash", True),
    ("https://opencode.ai/zen/go/v1/", "deepseek-v4-flash", True),
    ("https://api.deepseek.com", "deepseek-v4-flash", False),
    ("https://api.deepseek.com/v1", "deepseek-v4-flash", False),
    ("https://opencode.ai/zen/v1", "deepseek-v4-flash", False),
    ("https://opencode.ai.evil.invalid/zen/go/v1", "deepseek-v4-flash", False),
    ("https://other.invalid/v1", "other-model", False),
])
def test_memory_json_wire_mode_is_compatible_without_removing_extraction_validation(
    monkeypatch, base_url, model, omit_mode,
):
    backend, calls, _ = _raw_response_backend(monkeypatch, base_url,
        content='{"unexpected": []}', model=model)
    requested = {"type": "json_object"}
    with pytest.raises(mem0_memory.Mem0AdapterError, match="^MEM0_EXTRACTION_RESPONSE_INVALID_MEMORY_MISSING$"):
        backend.llm.generate_response(messages=[{"role":"system", "content":"Return memory JSON."}],
            response_format=requested)
    assert ("response_format" not in calls[0]) is omit_mode
    assert requested == {"type":"json_object"}
    assert "Return memory JSON." in calls[0]["messages"][0]["content"]
    if omit_mode:
        assert calls[0]["reasoning_effort"] == "low"
        assert calls[0]["extra_body"]["thinking"] == {"type":"enabled"}


def test_memory_wire_compatibility_does_not_rewrite_other_response_formats(monkeypatch):
    backend, calls, _ = _raw_response_backend(monkeypatch, "https://opencode.ai/zen/go/v1",
        model="deepseek-v4-flash")
    requested = {"type":"json_schema", "json_schema":{"name":"synthetic", "schema":{"type":"object"}}}
    backend.llm.generate_response(messages=[], response_format=requested)
    assert calls[0]["response_format"] == requested


def test_extraction_request_keeps_grounding_at_system_priority_without_mutating_input(monkeypatch):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return '{"memory": []}'
    memory = SimpleNamespace(llm=SimpleNamespace(generate_response=generate))
    monkeypatch.setattr(mem0_memory, "_load_product_mem0_module", lambda: SimpleNamespace(
        Memory=SimpleNamespace(from_config=lambda config: memory),
    ))
    backend = mem0_memory._default_factory({})
    messages = [{"role": "system", "content": "Extract rich facts. Return memory JSON."},
                {"role": "user", "content": "你还记得你等我的那段日子吗？"}]
    backend.llm.generate_response(messages=messages, response_format={"type": "json_object"})
    assert len(calls) == 1
    sent = calls[0]["messages"]
    assert sent[0]["content"].startswith(messages[0]["content"])
    assert "疑问中的预设不能作为事实" in sent[0]["content"]
    assert "不受最低字数" in sent[0]["content"]
    assert sent[1] == messages[1]
    assert messages[0]["content"] == "Extract rich facts. Return memory JSON."


@pytest.mark.parametrize("response", ['{"memory": [broken}', '{"facts": []}', '', '{"memory": "wrong"}'])
def test_product_factory_rejects_invalid_extraction_before_mem0_can_swallow_it(monkeypatch, response):
    provider = SimpleNamespace(generate_response=lambda **kwargs: response)
    memory = SimpleNamespace(llm=provider)
    monkeypatch.setattr(mem0_memory, "_load_product_mem0_module", lambda: SimpleNamespace(
        Memory=SimpleNamespace(from_config=lambda config: memory),
    ))
    backend = mem0_memory._default_factory({})
    with pytest.raises(mem0_memory.Mem0AdapterError, match="MEM0_EXTRACTION_RESPONSE_INVALID"):
        backend.llm.generate_response(messages=[], response_format={"type": "json_object"})


@pytest.mark.parametrize("response", ['{"memory": []}', '```json\n{"memory": [{"text": "synthetic fact"}]}\n```'])
def test_product_factory_preserves_valid_empty_or_populated_extraction(monkeypatch, response):
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return response
    memory = SimpleNamespace(llm=SimpleNamespace(generate_response=generate))
    monkeypatch.setattr(mem0_memory, "_load_product_mem0_module", lambda: SimpleNamespace(
        Memory=SimpleNamespace(from_config=lambda config: memory),
    ))
    backend = mem0_memory._default_factory({})
    assert backend.llm.generate_response(messages=[], response_format={"type": "json_object"}) == response
    assert len(calls) == 1


@pytest.mark.parametrize("response,suffix", [
    (None, "NOT_TEXT"), ({"memory": []}, "NOT_TEXT"), (" \n ", "EMPTY"),
    ('{"memory": [private_broken}', "JSON"), ('[]', "ROOT"),
    ('{"private_missing_key": []}', "MEMORY_MISSING"),
    ('{"memory": "private_wrong_type"}', "MEMORY_LIST"),
    ('{"memory": ["private_wrong_item"]}', "ITEM"),
    ('{"memory": [{"private_missing_text": 1}]}', "TEXT_TYPE"),
    ('{"memory": [{"text": 12}]}', "TEXT_TYPE"),
    ('{"memory": [{"text": " \n "}]}', "TEXT_EMPTY"),
])
def test_extraction_classification_is_specific_and_content_free(response, suffix):
    code = f"MEM0_EXTRACTION_RESPONSE_INVALID_{suffix}"
    llm = mem0_memory._ValidatedExtractionLLM(SimpleNamespace(generate_response=lambda **_: response))
    with pytest.raises(mem0_memory.Mem0AdapterError) as caught:
        llm.generate_response(messages=[], response_format={"type": "json_object"})
    assert caught.value.code == code
    assert str(caught.value) == code
    assert "private" not in repr(caught.value)
    assert caught.value.__context__ is None
    # The upstream SDK wraps extraction failures; preserve the safe subtype.
    wrapped = RuntimeError("synthetic wrapper")
    wrapped.__cause__ = caught.value
    assert mem0_memory._extraction_failure_code(wrapped) == code
