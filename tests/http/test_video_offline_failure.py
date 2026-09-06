import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import original_client_video_capability_api as api
from runtime.diagnostics.support_bundle import _project_health


@pytest.mark.parametrize("failure,code,stage", [
    ("session", "VIDEO_CAPABILITY_LOGIN_REQUIRED", "authorization"),
    ("picker", "VIDEO_OFFLINE_PICKER_UNAVAILABLE", "selection"),
    ("archive", "VIDEO_OFFLINE_ARCHIVE_INVALID", "validation"),
    ("decode", "VIDEO_OFFLINE_PICKER_UNAVAILABLE", "selection"),
])
def test_early_failure_is_safe_and_does_not_replace_ready(tmp_path, failure, code, stage):
    installer = SimpleNamespace(status=lambda: {
        "capability": "video", "status": "READY", "runtime_import": {"state": "ready"},
    })

    def authorize(token):
        if failure == "session":
            raise ValueError("private token/path must not escape")

    def picker():
        if failure == "decode":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic")
        if failure == "picker":
            raise api.VideoCapabilityAPIError("VIDEO_OFFLINE_PICKER_UNAVAILABLE", status=503)
        return tmp_path / "private.zip"

    async def run():
        app = web.Application()
        api.mount_original_client_video_capability_api(
            app, installer, trusted_origins=(), authorize_session=authorize,
            select_offline_archive=picker,
        )
        headers = {"Origin": "http://localhost:3000", "X-Olivia-Capability-Action": "confirmed"}
        async with TestClient(TestServer(app)) as client:
            response = await client.post(api.ACTION_PATH, json={"action": "import_offline"}, headers=headers)
            assert (await response.json())["error_code"] == code
            status = await client.get(api.STATUS_PATH, headers=headers)
            payload = await status.json()
            assert payload["status"] == "READY"
            assert payload["runtime_import"] == {"state": "ready"}
            assert payload["offline_action_failure"] == {"error_code": code, "stage": stage}
            assert installer._offline_action_failure == payload["offline_action_failure"]
    asyncio.run(run())


def test_diagnostic_failure_projection_is_bounded():
    failure = {"state": "unavailable", "error_code": "VIDEO_OFFLINE_ARCHIVE_INVALID", "stage": "validation"}
    result = _project_health({"status": "available", "checks": {"video_offline_action": {**failure, "path": "private"}}})
    assert result["checks"]["video_offline_action"] == failure
    for field in ("stage", "error_code"):
        with pytest.raises(Exception):
            _project_health({"status": "available", "checks": {"video_offline_action": {**failure, field: "private"}}})


def test_failure_status_extension_matches_contract():
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/video_capability_status.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema["properties"]["offline_action_failure"])
    for code in api.OFFLINE_ACTION_ERROR_CODES:
        for stage in api.OFFLINE_ACTION_STAGES:
            validator.validate({"error_code": code, "stage": stage})
    assert not validator.is_valid({"error_code": "PRIVATE", "stage": "selection"})
