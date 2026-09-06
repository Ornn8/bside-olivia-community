from types import SimpleNamespace

import pytest

from runtime.memory import mem0_memory


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
