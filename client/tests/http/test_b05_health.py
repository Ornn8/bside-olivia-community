from __future__ import annotations

import asyncio


def test_contract_exposes_text_fallback_without_promoting_native_asr() -> None:
    import http_contract

    document = http_contract.contract_document()
    assert document["capabilities"]["text.input.fallback"]["status"] == "available"
    assert document["capabilities"]["text.input.fallback"]["provider"] == "text-fallback"
    assert document["capabilities"]["native.asr"]["status"] == "unavailable"


def test_asr_health_profile_is_truthful_when_runtime_is_not_installed() -> None:
    import local_server

    result = asyncio.run(local_server.route("GET", "/health", {}, {"profile": "asr"}))
    assert result["code"] == 0
    assert result["data"]["profile"] == "asr"
    assert result["data"]["status"] == "UNAVAILABLE"
    assert result["data"]["capabilities"]["native.asr"]["status"] == "unavailable"
    assert result["data"]["capabilities"]["text.input.fallback"]["status"] == "available"
