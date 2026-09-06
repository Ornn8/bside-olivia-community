from types import SimpleNamespace

import pytest

from runtime.memory import mem0_memory


def _raw_response_backend(monkeypatch, base_url, finish_reason="stop", content='{"memory": []}'):
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
    return mem0_memory._default_factory({}), calls, create


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
