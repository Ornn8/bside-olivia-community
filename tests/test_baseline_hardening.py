"""Regression tests for the repository hardening baseline."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def git_ignores(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def test_sensitive_paths_under_docs_stay_ignored_without_creating_files() -> None:
    sensitive_paths = (
        "docs/.env",
        "docs/token.json",
        "docs/secret.key",
        "docs/logs/x.log",
        "docs/model.safetensors",
    )

    assert all(git_ignores(path) for path in sensitive_paths)
    assert git_ignores("docs/random-note.md")
    assert git_ignores("docs/evidence-extra.json")
    assert git_ignores("docs/reviews/extra.md")
    assert git_ignores(".evidence/baseline-hardening/run-id/pytest.log")
    assert git_ignores(".evidence/baseline-hardening/run-id/manifest.sha256")
    assert not git_ignores("docs/MASTER_PLAN.md")
    assert not git_ignores("docs/reviews/baseline-hardening-evidence.md")
    assert not git_ignores("linli_character/system_prompt.md")
    assert git_ignores("linli_character/letter_1.md")


def test_local_server_contains_no_official_forwarding_or_token_capture_code() -> None:
    source = (ROOT / "local_server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported_modules.isdisjoint({"httpx", "requests", "urllib", "urllib3"})
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "urllib"
        and any(alias.name == "request" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "aiohttp"
        and any(alias.name in {"ClientSession", "request"} for alias in node.names)
        for node in ast.walk(tree)
    )

    def dotted_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = dotted_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    network_calls = {
        "aiohttp.ClientSession",
        "aiohttp.request",
        "httpx.request",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "requests.request",
        "requests.post",
        "requests.put",
        "requests.patch",
        "urllib.request.urlopen",
    }
    assert {
        dotted_name(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }.isdisjoint(network_calls)

    forbidden_markers = (
        "OFFICIAL_BASE",
        "official_request",
        "capture_official",
        "capture_official_reply",
        "download_reply_video",
        "Olivia-steam/logs",
    )
    assert not any(marker in source for marker in forbidden_markers)

    sensitive_sink_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = dotted_name(node.func)
        if call_name in {"json.dump", "json.dumps", "open"} or call_name.endswith(
            (".write_text", ".write_bytes")
        ):
            snippet = ast.get_source_segment(source, node) or ""
            if any(marker in snippet.lower() for marker in ("token", "authorization", "x-uid")):
                sensitive_sink_calls.append(snippet)
    assert sensitive_sink_calls == []


def test_local_server_binds_only_to_loopback() -> None:
    source = (ROOT / "local_server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_app_hosts = [
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_app"
        for keyword in node.keywords
        if keyword.arg == "host"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    ]
    assert run_app_hosts == ["127.0.0.1"]


def test_b00_scanner_patterns_cover_variants_without_echoing_source() -> None:
    import baseline_hardening_scan as scanner

    synthetic_comment = (
        "# OFFICIAL offical 官方; historical snapshot 快照; "
        "remote API dependency; persona distillation completed"
    )
    labels = {
        label
        for label, pattern in scanner.COMMENT_PATTERNS
        if pattern.search(synthetic_comment)
    }

    assert labels == {
        "official_word",
        "historical_snapshot_word",
        "online_dependency",
        "completed_persona_distillation",
    }
    assert "synthetic_comment" not in "\n".join(
        f"local_server.py:1:{label}" for label in sorted(labels)
    )


def test_b00_scanner_covers_english_dependency_and_triple_quoted_docstrings(
    tmp_path: Path,
) -> None:
    import baseline_hardening_scan as scanner

    fixture = tmp_path / "synthetic_runtime.py"
    fixture.write_text(
        '"""external dependency and official response source; '
        'persona distillation completed."""\n',
        encoding="utf-8",
    )

    findings, _, checked = scanner.scan_runtime_comments(tmp_path, [fixture])

    assert checked == 1
    assert {finding.rsplit(":", 1)[-1] for finding in findings} == {
        "official_word",
        "online_dependency",
        "completed_persona_distillation",
    }
    assert all("external dependency" not in finding for finding in findings)


def test_b00_scanner_covers_space_separated_official_runtime_markers() -> None:
    import baseline_hardening_scan as scanner

    marker_pattern = dict(scanner.RUNTIME_DEPENDENCY_PATTERNS)[
        "official_request_poll_download_token_marker"
    ]
    for marker in (
        "official request",
        "official poll",
        "official download",
        "official token",
        "capture official reply",
        "download reply video",
        "x token",
    ):
        assert marker_pattern.search(marker), marker


def test_b00_scanner_allows_pinned_cors_metadata_but_rejects_forwarding(
    tmp_path: Path,
) -> None:
    import baseline_hardening_scan as scanner

    local_server = tmp_path / "local_server.py"
    local_server.write_text(
        "TRUSTED_FRONTEND_ORIGINS = frozenset({\n"
        "    'https://toy-cnbeta01.olivia.miyoushe.com',\n"
        "})\n"
        "ALLOWED_HEADERS = ', '.join((\n"
        "    'X-Token',\n"
        "))\n",
        encoding="utf-8",
    )
    forwarding = tmp_path / "forwarding.py"
    forwarding.write_text(
        "import requests\n"
        "requests.post('https://toy-cnbeta01.olivia.miyoushe.com/toy/signIn', "
        "headers={'X-Token': token})\n",
        encoding="utf-8",
    )

    findings, _, checked = scanner.scan_runtime_dependencies(
        tmp_path, [local_server, forwarding]
    )

    assert checked == 2
    assert findings == [
        "forwarding.py:2:known_official_host",
        "forwarding.py:2:official_request_poll_download_token_marker",
    ]


def test_persona_release_scan_allows_contracts_code_and_public_declarations(
    tmp_path: Path,
) -> None:
    import json
    import baseline_hardening_scan as scanner

    contract = tmp_path / "contracts" / "private_world.schema.json"
    runtime = tmp_path / "private_world_port.py"
    persona = tmp_path / "linli_character" / "persona_v2.json"
    contract.parent.mkdir()
    persona.parent.mkdir()
    contract.write_text('{"properties":{"view":{"const":"control"}}}', encoding="utf-8")
    runtime.write_text('CONTROL_VIEW = "control_only"\n', encoding="utf-8")
    persona.write_text(
        json.dumps(
            {
                    "declarations": [
                        {
                            "allowed_public_release": True,
                            "rights_status": "REDISTRIBUTABLE",
                            "statement": "Synthetic public rule.",
                        },
                        {
                            "allowed_public_release": False,
                            "rights_status": "UNKNOWN_BLOCK_RELEASE",
                            "statement": "Synthetic quarantined registry record.",
                        }
                    ]
            }
        ),
        encoding="utf-8",
    )

    findings, _, checked = scanner.scan_persona_release(
        tmp_path, [contract, runtime, persona]
    )

    assert findings == []
    assert checked == 1


def test_persona_release_scan_rejects_private_instances_and_communications(
    tmp_path: Path,
) -> None:
    import json
    import baseline_hardening_scan as scanner

    state = tmp_path / "linli_character" / "private_world_state.json"
    chat = tmp_path / "linli_character" / "chat_export.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "view": "control",
                "continuation_awareness": "pending",
                "nickname_permissions": ["synthetic_private_nickname"],
            }
        ),
        encoding="utf-8",
    )
    chat.write_text('{"messages":["synthetic private communication"]}', encoding="utf-8")

    findings, _, _ = scanner.scan_persona_release(tmp_path, [state, chat])

    assert {finding.rsplit(":", 1)[-1] for finding in findings} >= {
        "private_state_path",
        "control_view_instance",
        "continuation_instance",
        "private_nickname_instance",
        "private_communication_path",
    }
    assert "synthetic_private_nickname" not in "\n".join(findings)
    assert "synthetic private communication" not in "\n".join(findings)


def test_persona_release_scan_fails_closed_on_blocked_rights_and_source_copy(
    tmp_path: Path,
) -> None:
    import json
    import baseline_hardening_scan as scanner

    asset = tmp_path / "linli_character" / "persona_extra.json"
    asset.parent.mkdir()
    asset.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "allowed_public_release": False,
                        "rights_status": "UNKNOWN_BLOCK_RELEASE",
                        "statement": "x" * 1300,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    findings, _, _ = scanner.scan_persona_release(tmp_path, [asset])

    assert {finding.rsplit(":", 1)[-1] for finding in findings} == {
        "blocked_release_record",
        "long_source_copy",
    }
    assert "x" * 20 not in "\n".join(findings)


def test_b00_scanner_process_exit_codes_and_sanitized_findings(tmp_path: Path) -> None:
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    (repo / "local_server.py").write_text(
        "# official response synthetic-secret\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "local_server.py"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    finding = subprocess.run(
        [
            sys.executable,
            str(ROOT / "baseline_hardening_scan.py"),
            "--root",
            str(repo),
            "--mode",
            "comments",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert finding.returncode == 1
    assert "status=FAIL" in finding.stdout
    assert "finding=local_server.py:1:official_word" in finding.stdout
    assert "synthetic-secret" not in finding.stdout

    missing = subprocess.run(
        [
            sys.executable,
            str(ROOT / "baseline_hardening_scan.py"),
            "--root",
            str(tmp_path / "not-a-repository"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert "status=ERROR" in missing.stdout
    assert str(tmp_path) not in missing.stdout


def test_legacy_memory_is_nonpersistent_by_default_and_can_be_cleared(tmp_path: Path) -> None:
    from memory import Memory

    path = tmp_path / "memory_store.json"
    path.write_text('{"player_profile": {"synthetic": "value"}}', encoding="utf-8")
    original = path.read_text(encoding="utf-8")

    memory = Memory(path=path)

    assert memory.persist is False
    assert memory.data["player_profile"] == {}
    memory.add_letter("synthetic-id", "synthetic input", "synthetic output")
    assert path.read_text(encoding="utf-8") == original

    memory.clear()
    assert memory.data["letters"] == []
    assert memory.data["facts"] == []


def test_cors_allows_explicit_local_origins_only() -> None:
    import local_server

    allowed = local_server.cors_headers("http://127.0.0.1:8899")
    localhost = local_server.cors_headers("http://localhost:3000")
    denied = local_server.cors_headers("https://example.invalid/endpoint")

    assert allowed["Access-Control-Allow-Origin"] == "http://127.0.0.1:8899"
    assert localhost["Access-Control-Allow-Credentials"] == "true"
    assert "Access-Control-Allow-Origin" not in denied
    assert "Access-Control-Allow-Credentials" not in denied


def test_cors_requires_exact_pinned_frontend_origin() -> None:
    import local_server

    pinned = local_server.cors_headers("https://toy-cnbeta01.olivia.miyoushe.com")
    spoofed = local_server.cors_headers(
        "https://toy-cnbeta01.olivia.miyoushe.com.evil.invalid"
    )

    assert pinned["Access-Control-Allow-Origin"] == (
        "https://toy-cnbeta01.olivia.miyoushe.com"
    )
    assert "Access-Control-Allow-Origin" not in spoofed
    assert "Access-Control-Allow-Credentials" not in spoofed


def test_handler_cors_behavior_allows_local_and_rejects_external_origin() -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            allowed = await client.get(
                "/toy/getUserInfo", headers={"Origin": "http://localhost:3000"}
            )
            denied = await client.get(
                "/toy/getUserInfo", headers={"Origin": "https://example.invalid/endpoint"}
            )
            return (
                allowed.status,
                allowed.headers.get("Access-Control-Allow-Origin"),
                allowed.headers.get("Access-Control-Allow-Credentials"),
                denied.status,
                denied.headers.get("Access-Control-Allow-Origin"),
                denied.headers.get("Access-Control-Allow-Credentials"),
            )

    assert asyncio.run(exercise()) == (
        200,
        "http://localhost:3000",
        "true",
        403,
        None,
        None,
    )


def test_handler_cors_allows_pinned_official_frontend_preflight() -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    requested_headers = (
        "content-type,x-bundle_id,x-client_type,x-device_id,x-device_model,"
        "x-language,x-level,x-lifecycle_id,x-pkg_version,x-platform,x-sys_version,x-token,x-uid"
    )

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.options(
                "/toy/signIn",
                headers={
                    "Origin": "https://toy-cnbeta01.olivia.miyoushe.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": requested_headers,
                },
            )
            return (
                response.status,
                response.headers.get("Access-Control-Allow-Origin"),
                response.headers.get("Access-Control-Allow-Headers"),
            )

    status, origin, allowed_headers = asyncio.run(exercise())
    assert status == 204
    assert origin == "https://toy-cnbeta01.olivia.miyoushe.com"
    assert set(allowed_headers.lower().replace(" ", "").split(",")) >= set(
        requested_headers.split(",")
    )


def test_handler_returns_http_503_for_llm_failure(monkeypatch) -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    def fail(_content, _context=""):
        raise local_server.LLMError("LLM_TIMEOUT")

    monkeypatch.setattr(local_server.letters_adapter, "reply", fail)

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            response = await client.post(
                "/toy/letter/send",
                json={"content": "synthetic HTTP status input", "material": {}},
            )
            return response.status, await response.json()

    status, payload = asyncio.run(exercise())

    assert status == 503
    assert payload["code"] == 503
    assert payload["data"]["status"] == "FAILED"


def test_handler_returns_http_501_404_and_200_for_terminal_route_results() -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            unimplemented = await client.get("/toy/not-implemented")
            letter_not_found = await client.get(
                "/toy/letter/detail?letter_id=missing-synthetic-letter"
            )
            not_found = await client.get(
                "/toy/midi/getGenerateResult?jobId=missing-synthetic-job"
            )
            success = await client.get("/toy/getUserInfo")
            return (
                unimplemented.status,
                (await unimplemented.json())["code"],
                letter_not_found.status,
                (await letter_not_found.json())["code"],
                not_found.status,
                (await not_found.json())["code"],
                success.status,
                (await success.json())["code"],
            )

    assert asyncio.run(exercise()) == (501, 501, 404, 404, 404, 404, 200, 0)


def test_unimplemented_write_routes_return_http_501_not_success() -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    paths = (
        "/toy/submitPreferenceSurvey",
        "/toy/letter/resend",
        "/toy/letter/share",
        "/toy/addPerformance",
        "/toy/editPerformance",
        "/toy/delPerformance",
        "/toy/addToPlaylist",
        "/toy/delFromPlaylist",
        "/toy/deleteUserSong",
        "/toy/editProfile",
        "/toy/createFeedback",
        "/toy/generateShareToken",
        "/toy/genObjectUploadUrl",
        "/toy/midi/importShareCode",
    )

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            responses = []
            for path in paths:
                response = await client.post(path, json={})
                responses.append((response.status, (await response.json())["code"]))
            return responses

    assert asyncio.run(exercise()) == [(501, 501)] * len(paths)


def test_llm_failure_is_explicit_failed_without_placeholder_text(monkeypatch) -> None:
    import asyncio
    import local_server

    local_server.store.letters.clear()

    def fail(_content, _context=""):
        raise local_server.LLMError("LLM_TIMEOUT")

    monkeypatch.setattr(local_server.letters_adapter, "reply", fail)
    result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic test input", "material": {}},
            {},
        )
    )

    assert result["code"] == 503
    assert result["data"]["status"] == "FAILED"
    assert result["data"]["error_code"] == "LLM_TIMEOUT"
    assert local_server.store.letters[0]["letter_status"] == "FAILED"
    assert local_server.store.letters[0]["reply_text"] == ""
    assert "LLM 未就绪" not in str(local_server.store.letters[0])


def test_midi_unimplemented_is_terminal_and_cancel_is_idempotent() -> None:
    import asyncio
    import local_server

    local_server.store.midi_jobs.clear()
    created = asyncio.run(
        local_server.route(
            "POST",
            "/toy/midi/generate",
            {"midiUrl": "synthetic://input", "filename": "synthetic.mid"},
            {},
        )
    )

    assert created["code"] == 501
    assert created["data"]["status"] == "NOT_IMPLEMENTED"
    job_id = created["data"]["job_id"]
    listed = asyncio.run(local_server.route("GET", "/toy/midi/listJobs", {}, {}))
    assert listed["data"]["list"][0]["status"] == "NOT_IMPLEMENTED"

    first_cancel = asyncio.run(
        local_server.route("POST", "/toy/midi/cancelGenerate", {"jobId": job_id}, {})
    )
    second_cancel = asyncio.run(
        local_server.route("POST", "/toy/midi/cancelGenerate", {"jobId": job_id}, {})
    )
    assert first_cancel["data"]["status"] == "NOT_IMPLEMENTED"
    assert second_cancel["data"]["status"] == "NOT_IMPLEMENTED"


def test_midi_cancel_only_changes_cancellable_jobs() -> None:
    import asyncio

    import local_server

    local_server.store.midi_jobs[:] = [
        {"job_id": "processing-synthetic", "state": 1, "status": "PROCESSING"},
        {"job_id": "completed-synthetic", "state": 3, "status": "COMPLETED"},
        {"job_id": "failed-synthetic", "state": 5, "status": "FAILED"},
        {"job_id": "not-implemented-synthetic", "state": 5, "status": "NOT_IMPLEMENTED"},
    ]

    def cancel(job_id: str) -> str:
        result = asyncio.run(
            local_server.route("POST", "/toy/midi/cancelGenerate", {"jobId": job_id}, {})
        )
        return result["data"]["status"]

    assert cancel("processing-synthetic") == "CANCELED"
    assert cancel("processing-synthetic") == "CANCELED"
    assert cancel("completed-synthetic") == "COMPLETED"
    assert cancel("failed-synthetic") == "FAILED"
    assert cancel("not-implemented-synthetic") == "NOT_IMPLEMENTED"


def test_unknown_midi_cancel_and_delete_are_http_404() -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    local_server.store.midi_jobs.clear()

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app, access_log=None)) as client:
            cancel = await client.post(
                "/toy/midi/cancelGenerate", json={"jobId": "missing-synthetic-job"}
            )
            delete = await client.post(
                "/toy/midi/deleteJob", json={"jobId": "missing-synthetic-job"}
            )
            return (
                cancel.status,
                (await cancel.json())["code"],
                delete.status,
                (await delete.json())["code"],
            )

    assert asyncio.run(exercise()) == (404, 404, 404, 404)


def test_offline_music_fallback_has_no_remote_media_urls() -> None:
    import asyncio
    import json
    import local_server

    music_type = asyncio.run(local_server.route("GET", "/toy/getMusicTypeInfo", {}, {}))
    songs = asyncio.run(local_server.route("GET", "/toy/searchSongs", {}, {}))
    payload = json.dumps({"music_type": music_type, "songs": songs}, ensure_ascii=False)
    assert "http://" not in payload
    assert "https://" not in payload


def test_blocking_llm_call_is_offloaded_from_event_loop() -> None:
    source = (ROOT / "local_server.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(" in source
    assert "asyncio.wait_for(" in source


def test_slow_llm_does_not_block_loop_and_times_out_as_failed(monkeypatch) -> None:
    import asyncio
    import threading
    import time

    import local_server

    local_server.store.letters.clear()
    started = threading.Event()
    finished = threading.Event()

    def slow_reply(_content, _context=""):
        started.set()
        time.sleep(0.25)
        finished.set()
        return "late synthetic reply"

    monkeypatch.setattr(local_server.letters_adapter, "reply", slow_reply)
    monkeypatch.setattr(local_server, "LLM_TIMEOUT_SECONDS", 0.03)

    async def exercise():
        heartbeat_ticks = 0

        async def heartbeat():
            nonlocal heartbeat_ticks
            while True:
                heartbeat_ticks += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            result_task = asyncio.create_task(
                local_server.route(
                    "POST",
                    "/toy/letter/send",
                    {"content": "synthetic timeout input", "material": {}},
                    {},
                )
            )
            await asyncio.to_thread(started.wait, 1)
            result = await result_task
            return result, heartbeat_ticks
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    result, heartbeat_ticks = asyncio.run(exercise())

    assert heartbeat_ticks >= 2
    assert result["code"] == 503
    assert result["data"]["status"] == "FAILED"
    assert local_server.store.letters[0]["letter_status"] == "FAILED"
    assert finished.wait(1)


def test_persona_configuration_is_explicitly_draft_only() -> None:
    persona = (ROOT / "linli_character" / "system_prompt.md").read_text(encoding="utf-8")
    assert "DRAFT" in persona
    assert "不得作为最终官方人格验收依据" in persona
    assert "完整或官方定稿" in persona


def test_persona_missing_or_unreadable_file_falls_back_as_draft(monkeypatch, tmp_path: Path) -> None:
    import local_server

    missing = tmp_path / "missing-persona.md"
    monkeypatch.setitem(local_server.LLM_CFG, "persona_file", str(missing))
    missing_prompt = local_server._persona()

    unreadable = tmp_path / "persona-directory"
    unreadable.mkdir()
    monkeypatch.setitem(local_server.LLM_CFG, "persona_file", str(unreadable))
    unreadable_prompt = local_server._persona()

    for prompt in (missing_prompt, unreadable_prompt):
        assert prompt.startswith("PERSONA STATUS: DRAFT.")
        assert "not distilled" in prompt
        assert "final persona" in prompt


def test_runtime_logging_is_structured_and_does_not_format_user_content() -> None:
    server_source = (ROOT / "local_server.py").read_text(encoding="utf-8")
    memory_source = (ROOT / "memory.py").read_text(encoding="utf-8")

    assert "def _safe_log" in server_source
    assert "json.dumps(body" not in server_source
    assert "reply[:" not in server_source
    assert "content[:" not in server_source
    assert "str(e)" not in server_source
    assert "print(f" not in memory_source


def test_request_logging_does_not_emit_letter_content(monkeypatch, capsys) -> None:
    import asyncio
    import local_server

    local_server.store.letters.clear()
    monkeypatch.setattr(local_server.letters_adapter, "reply", lambda *_args: "synthetic reply")
    result = asyncio.run(
        local_server.route(
            "POST",
            "/toy/letter/send",
            {"content": "synthetic private input", "material": {}},
            {},
        )
    )

    captured = capsys.readouterr().out
    assert result["code"] == 0
    assert '"event"' in captured
    assert "synthetic private input" not in captured
    assert "synthetic reply" not in captured


def test_handler_logs_exclude_body_reply_token_and_full_query(monkeypatch, capsys) -> None:
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    local_server.store.letters.clear()
    monkeypatch.setattr(local_server.letters_adapter, "reply", lambda *_args: "synthetic reply secret")
    capsys.readouterr()

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/toy/letter/send?token=synthetic-token&query_secret=synthetic-query-secret"
                "&redirect=https%3A%2F%2Fprivate.example%2F%3Ftoken%3Dhidden",
                json={
                    "content": "synthetic private body",
                    "material": {"reply": "synthetic reply secret", "token": "body-token"},
                },
            )
            return response.status, await response.json()

    status, payload = asyncio.run(exercise())
    captured = capsys.readouterr().out

    assert status == 200
    assert payload["code"] == 0
    assert '"event"' in captured
    for secret in (
        "synthetic private body",
        "synthetic reply secret",
        "synthetic-token",
        "synthetic-query-secret",
        "body-token",
        "private.example",
    ):
        assert secret not in captured


def test_aiohttp_app_access_logging_never_leaks_sensitive_query(monkeypatch, capsys, caplog) -> None:
    import asyncio
    import logging

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import local_server

    local_server.store.letters.clear()
    monkeypatch.setattr(local_server.letters_adapter, "reply", lambda *_args: "synthetic access reply")
    capsys.readouterr()

    async def exercise():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", local_server.handler)
        server = TestServer(app)
        await server.start_server(access_log=None)
        client = TestClient(server)
        try:
            response = await client.post(
                "/toy/letter/send?token=access-token&query_secret=access-query-secret"
                "&redirect=https%3A%2F%2Fprivate.example%2F%3Ftoken%3Dhidden",
                json={"content": "synthetic access body", "material": {}},
            )
            return response.status
        finally:
            await client.close()
            await server.close()

    with caplog.at_level(logging.DEBUG):
        status = asyncio.run(exercise())
    captured = capsys.readouterr().out
    captured += "\n".join(record.getMessage() for record in caplog.records)

    assert status == 200
    for secret in (
        "access-token",
        "access-query-secret",
        "private.example",
        "synthetic access body",
        "synthetic access reply",
    ):
        assert secret not in captured
    assert "access_log=None" in (ROOT / "local_server.py").read_text(encoding="utf-8")


def _write_feapp_fixture(path: Path, javascript: str) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("assets/main-917d29fc.js", javascript)


def test_patch_feapp_uses_backup_hash_and_atomic_replacement(tmp_path: Path) -> None:
    import hashlib
    import zipfile

    import patch_feapp

    feapp = tmp_path / "feapp.dat"
    _write_feapp_fixture(
        feapp,
        (
            'He=e=>new Promise((t,n)=>{try{,"query.response":no(a)}}),t(c)},onFailure:'
            '!z.isNew||N?(await t.replace({name:ye.Home}),'
            'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
        ),
    )
    source_hash = hashlib.sha256(feapp.read_bytes()).hexdigest()

    result = patch_feapp.patch_feapp(feapp, "ws://127.0.0.1:8899/ws", work_root=tmp_path)

    assert result["source_sha256"] == source_hash
    assert Path(result["backup_path"]).exists()
    assert result["patched_sha256"] != source_hash
    with zipfile.ZipFile(feapp) as archive:
        patched = archive.read("assets/main-917d29fc.js").decode("utf-8")
    assert "127.0.0.1:8899" in patched
    assert 'toyApiUrl="http://127.0.0.1:8899"' in patched
    assert 'toyWsUrl="ws://127.0.0.1:8899/ws"' in patched
    assert 'personaStatus="DRAFT"' not in patched


def test_patch_feapp_routes_fresh_lite_login_to_existing_mailbox_collection(
    tmp_path: Path,
) -> None:
    import zipfile

    import patch_feapp

    feapp = tmp_path / "feapp.dat"
    _write_feapp_fixture(
        feapp,
        (
            'He=e=>new Promise((t,n)=>{try{,'
            '"query.response":no(a)}}),t(c)},onFailure:'
            'n.appMode===$e.LITE){!z.isNew||N?('
            'await t.replace({name:ye.Home}),'
            'await h(z.uid.toString(),z.modelGatewayToken||"",!1))'
        ),
    )

    patch_feapp.patch_feapp(feapp, "ws://127.0.0.1:8899/ws", work_root=tmp_path)

    with zipfile.ZipFile(feapp) as archive:
        patched = archive.read("assets/main-917d29fc.js").decode("utf-8")

    assert 'localStorage.setItem("appMode","lite")' in patched
    assert "await t.replace({name:ye.Collection})" in patched
    assert "await t.replace({name:ye.Home})" not in patched


def test_patch_feapp_requires_explicit_local_ws_and_rolls_back_on_failure(tmp_path: Path) -> None:
    import hashlib

    import patch_feapp

    feapp = tmp_path / "feapp.dat"
    _write_feapp_fixture(feapp, "no patch anchor")
    original = feapp.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()

    with __import__("pytest").raises(ValueError):
        patch_feapp.patch_feapp(feapp, None, work_root=tmp_path)
    assert feapp.read_bytes() == original

    with __import__("pytest").raises(ValueError):
        patch_feapp.patch_feapp(feapp, "ws://127.0.0.1:8899/ws", work_root=tmp_path)
    assert hashlib.sha256(feapp.read_bytes()).hexdigest() == original_hash
    assert not list(tmp_path.glob("*.tmp"))


def test_patch_feapp_has_no_api_injection_surface(tmp_path: Path) -> None:
    import pytest

    import patch_feapp

    feapp = tmp_path / "feapp.dat"
    _write_feapp_fixture(
        feapp,
        'He=e=>new Promise((t,n)=>{try{,"query.response":no(a)}}),t(c)},onFailure:',
    )
    original = feapp.read_bytes()

    with pytest.raises(TypeError):
        patch_feapp.patch_feapp(
            feapp,
            "ws://127.0.0.1:8899/ws",
            new_api="https://example.invalid/endpoint",
        )

    assert feapp.read_bytes() == original


def test_extract_player_rejects_zip_slip_and_outside_output_root(tmp_path: Path) -> None:
    import zipfile

    import pytest
    import extract_player

    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../escape.txt", "synthetic")

    with pytest.raises(ValueError):
        extract_player.safe_extract_zip(archive, tmp_path / "output", allowed_root=tmp_path)
    assert not (tmp_path / "escape.txt").exists()

    safe_archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe_archive, "w") as package:
        package.writestr("safe/file.txt", "synthetic")
    with pytest.raises(ValueError):
        extract_player.safe_extract_zip(safe_archive, tmp_path.parent / "outside", allowed_root=tmp_path)
