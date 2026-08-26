"""Fresh-process coverage for the local-server PrivateWorld runtime seam."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys

import pytest

from private_world_delivery import DeliveryEvent, DeliveryStatus
from private_world_reducer import ReducerEventKind
from private_world_runtime import create_private_world_runtime


ROOT = Path(__file__).resolve().parents[2]


def _fresh_local_server_payload(
    data_root: Path,
    *,
    private_world_environment: dict[str, str] | None = None,
    seed_available_snapshot: bool = False,
) -> dict[str, object]:
    script = """
import asyncio
import json
from pathlib import Path

import local_server
from private_world_ledger import LedgerEvent
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)
from reply_context import ReplyMode

database = Path(__import__('os').environ['OLIVIA_LOCAL_DATA_ROOT']) / 'private_world' / 'private_world.sqlite3'
if __import__('os').environ.get('OLIVIA_TEST_SEED_PRIVATE_WORLD') == '1':
    local_server.private_world_runtime.port.apply_once(
        LedgerEvent(
            event_id='gateway-projection-event',
            delivery_id='gateway-projection-delivery',
            event_type='canonical_reply_delivered',
            payload={'applied': False},
            occurred_at='2026-08-22T00:00:00+00:00',
        ),
        PrivateWorldSnapshot(
            version=1,
            familiarity=88,
            trust=91,
            comfort=77,
            closeness=74,
            tension=12,
            relationship_stage='close',
            nickname_permissions=('合成称呼',),
            home_access=HomeAccess.DOMESTIC_ACCESS,
            continuation_facts=(
                LocalContinuationFact(
                    'known.class',
                    '角色已知的合成课程调整。',
                    ContinuationAwareness.CHARACTER_KNOWN,
                ),
                LocalContinuationFact(
                    'pending.trip',
                    '角色未知的合成旅行安排。',
                    ContinuationAwareness.PENDING,
                ),
                LocalContinuationFact(
                    'control.plan',
                    '仅控制层可见的合成计划。',
                    ContinuationAwareness.CONTROL_ONLY,
                ),
            ),
        ),
    )
health = asyncio.run(
    local_server.route('GET', '/health', {}, {'profile': 'core'})
)['data']['providers']['private_world']
letter = {'letter_id': 'synthetic-letter'}
local_server._prepare_private_world_delivery(letter, 'synthetic canonical reply')
delivery_committed = local_server._commit_private_world_letter(letter)
reply_context = local_server.letters_adapter.build_reply_context(ReplyMode.TEXT_LETTER)
gateway_messages = local_server.letters_adapter._messages('synthetic current letter')
print(json.dumps({
    'database_exists': database.is_file(),
    'runtime_port_type': type(local_server.private_world_runtime.port).__name__,
    'runtime_committer_type': type(local_server.private_world_runtime.committer).__name__,
    'adapter_uses_runtime_port': local_server.letters_adapter.private_world_port is local_server.private_world_runtime.port,
    'delivery_uses_runtime_committer': local_server.private_world_committer is local_server.private_world_runtime.committer,
    'delivery_committed': delivery_committed,
    'delivery_status': letter['private_world_status'],
    'delivery_error_code': letter.get('private_world_error_code'),
    'reply_context': reply_context.to_dict(),
    'gateway_messages': gateway_messages,
    'health': health,
}, sort_keys=True))
"""
    environment = os.environ.copy()
    environment["OLIVIA_LOCAL_DATA_ROOT"] = str(data_root)
    environment["OLIVIA_LLM_PROVIDER"] = "none"
    environment["OLIVIA_MEMORY_ENABLED"] = "0"
    environment.pop("OLIVIA_PRIVATE_WORLD_DB", None)
    environment.pop("OLIVIA_PRIVATE_WORLD_ENABLED", None)
    if private_world_environment:
        environment.update(private_world_environment)
    if seed_available_snapshot:
        environment["OLIVIA_TEST_SEED_PRIVATE_WORLD"] = "1"
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, "fresh local_server subprocess failed"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _fresh_generate_reply_payload(
    data_root: Path,
    *,
    simulate_sqlite_failure: bool = False,
    simulate_semantic_failure: bool = False,
) -> dict[str, object]:
    script = """
import asyncio
import json
import os
from pathlib import Path

import local_server
from reply_context import ReplyMode
from reply_orchestrator import ReplyState
from reply_pipeline import PipelineResult
from voice_direction import VoicePerformancePlan

class AcceptedPipeline:
    async def run(self, request, context):
        return PipelineResult(
            'fresh-canonical',
            ReplyState.COMPLETED,
            text='synthetic canonical reply',
            quality_status='accepted',
        )

class TextTriage:
    reply_mode = ReplyMode.TEXT_LETTER.value
    def to_dict(self):
        return {'reply_mode': self.reply_mode}

class TextTriageService:
    async def classify(self, content):
        return TextTriage()

async def fixed_voice_plan(letter, text):
    return VoicePerformancePlan(
        reply_text=text,
        overall_emotion='steady',
        global_speed=1.0,
        energy=0.5,
        breath_before_sentences=(),
        emphasize_sentences=(),
    )

def render_reply(text, output_path, **kwargs):
    Path(output_path).write_bytes(b'synthetic media retry')
    return {}

local_server.emotion_triage = TextTriageService()
local_server.reply_pipeline = AcceptedPipeline()
local_server._schedule_text_reply_delay = lambda *args: None
local_server._persist_store_state = lambda: None
local_server._persist_media_state = lambda: None
local_server.letters_adapter.remember_conversation = lambda *args: None
local_server._voice_plan_for_letter = fixed_voice_plan
local_server.render_reply_video = render_reply

if os.environ.get('OLIVIA_TEST_PRIVATE_WORLD_SQLITE_FAILURE') == '1':
    def unavailable_snapshot():
        import sqlite3
        raise sqlite3.DatabaseError('synthetic sqlite failure')
    local_server.private_world_committer.ledger.snapshot = unavailable_snapshot
elif os.environ.get('OLIVIA_TEST_PRIVATE_WORLD_SEMANTIC_FAILURE') == '1':
    def corrupt_snapshot():
        raise json.JSONDecodeError('synthetic corrupt snapshot', '{', 1)
    local_server.private_world_committer.ledger.snapshot = corrupt_snapshot

letter = {
    'letter_id': 'fresh-canonical',
    'content': 'synthetic current letter',
    'reply_text': '',
    'reply_mode': ReplyMode.TEXT_LETTER.value,
    'letter_status': 'PENDING',
}
local_server.store.letters[:] = [letter]
canonical_result = asyncio.run(
    local_server.generate_reply('fresh-canonical', 'synthetic current letter')
)
event_count_after_canonical = local_server.private_world_runtime.port.health()['event_count']
recovery_count = local_server.recover_pending_private_world()

scene = Path(os.environ['OLIVIA_LOCAL_DATA_ROOT']) / 'scene.mp4'
scene.write_bytes(b'synthetic scene')
for key in ('MORNING', 'DAY', 'DUSK', 'NIGHT'):
    os.environ['OLIVIA_SCENE_' + key] = str(scene)
letter['media_status'] = 'UNAVAILABLE'
asyncio.run(
    local_server._render_media_job(
        'fresh-canonical',
        'synthetic current letter',
        'synthetic canonical reply',
        ReplyMode.SPOKEN_VIDEO.value,
    )
)
event_count_after_media_retry = local_server.private_world_runtime.port.health()['event_count']

print(json.dumps({
    'canonical_result': canonical_result,
    'private_world_status': letter['private_world_status'],
    'private_world_error_code': letter.get('private_world_error_code'),
    'private_world_delivery_id': letter['private_world_delivery_id'],
    'reply_revision': letter['reply_revision'],
    'recovery_count': recovery_count,
    'event_count_after_canonical': event_count_after_canonical,
    'event_count_after_media_retry': event_count_after_media_retry,
    'media_status': letter['media_status'],
}, ensure_ascii=False, sort_keys=True))
"""
    environment = os.environ.copy()
    environment["OLIVIA_LOCAL_DATA_ROOT"] = str(data_root)
    environment["OLIVIA_LLM_PROVIDER"] = "none"
    environment["OLIVIA_MEMORY_ENABLED"] = "0"
    environment.pop("OLIVIA_PRIVATE_WORLD_DB", None)
    environment.pop("OLIVIA_PRIVATE_WORLD_ENABLED", None)
    if simulate_sqlite_failure:
        environment["OLIVIA_TEST_PRIVATE_WORLD_SQLITE_FAILURE"] = "1"
    if simulate_semantic_failure:
        environment["OLIVIA_TEST_PRIVATE_WORLD_SEMANTIC_FAILURE"] = "1"
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, "fresh generate_reply subprocess failed"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_local_server_fresh_process_wires_default_private_world_runtime(
    tmp_path: Path,
) -> None:
    payload = _fresh_local_server_payload(tmp_path / "local-data")

    assert payload["database_exists"] is True
    assert payload["runtime_port_type"] == "SQLitePrivateWorldLedger"
    assert payload["runtime_committer_type"] == "PrivateWorldDeliveryCommitter"
    assert payload["adapter_uses_runtime_port"] is True
    assert payload["delivery_uses_runtime_committer"] is True
    assert payload["delivery_committed"] is True
    assert payload["delivery_status"] == "COMMITTED"
    assert payload["delivery_error_code"] is None
    assert payload["health"] == {
        "status": "available",
        "provider": "sqlite",
        "reason_code": None,
        "enabled": True,
        "schema_version": 2,
        "migration_status": "created_v2",
        "event_count": 0,
        "snapshot_count": 0,
        "probe": "in-process",
        "network_called": False,
    }
    serialized = json.dumps(payload["health"], ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "trust" not in serialized
    assert "familiarity" not in serialized


def test_local_server_import_degrades_a_corrupt_sqlite_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt-private-world.sqlite3"
    database.write_bytes(b"this is not a sqlite database")

    payload = _fresh_local_server_payload(
        tmp_path / "local-data",
        private_world_environment={"OLIVIA_PRIVATE_WORLD_DB": str(database)},
    )

    assert payload["runtime_port_type"] == "NullPrivateWorldPort"
    assert payload["runtime_committer_type"] == "NoneType"
    assert payload["health"]["status"] == "unavailable"
    assert payload["health"]["provider"] == "none"
    assert payload["health"]["reason_code"] == "PRIVATE_WORLD_STORAGE_UNAVAILABLE"


def test_invalid_configured_current_user_never_opens_legacy_private_world(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "local-data"
    legacy = create_private_world_runtime(
        {"OLIVIA_LOCAL_DATA_ROOT": str(data_root)}
    )
    assert legacy.committer is not None
    assert legacy.committer.commit(
        DeliveryEvent(
            delivery_id="legacy-local-user:1",
            kind=ReducerEventKind.CANONICAL_REPLY_DELIVERED,
            occurred_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
            semantic_key="legacy.local-user:1",
        )
    ) is DeliveryStatus.COMMITTED

    payload = _fresh_local_server_payload(
        data_root,
        private_world_environment={"OLIVIA_MEMORY_USER_ID": "invalid user"},
    )

    assert payload["runtime_port_type"] == "NullPrivateWorldPort"
    assert payload["runtime_committer_type"] == "NoneType"
    assert payload["delivery_committed"] is False
    assert payload["delivery_status"] == "PENDING"
    assert payload["health"]["status"] == "unavailable"
    assert payload["health"]["reason_code"] == "PRIVATE_WORLD_STORAGE_UNAVAILABLE"
    assert legacy.port.health()["event_count"] == 1


def test_available_sqlite_private_world_reaches_gateway_only_as_character_view(
    tmp_path: Path,
) -> None:
    payload = _fresh_local_server_payload(
        tmp_path / "local-data",
        seed_available_snapshot=True,
    )

    system_message = next(
        str(message["content"])
        for message in payload["gateway_messages"]
        if message["role"] == "system"
    )

    assert '"home_history_allowed":true' in system_message
    assert "合成称呼" in system_message
    assert "角色已知的合成课程调整。" in system_message
    private_behavior = re.search(
        r"<private_behavior>\n(?P<payload>.+?)\n</private_behavior>",
        system_message,
        flags=re.DOTALL,
    )
    assert private_behavior is not None
    serialized_private_behavior = private_behavior.group("payload")
    for hidden in ("88", "91", "77", "74", "12"):
        assert hidden not in serialized_private_behavior
    for hidden in (
        "no_access",
        "visit_access",
        "errand_access",
        "domestic_access",
        "pending.trip",
        "control.plan",
        "角色未知的合成旅行安排。",
        "仅控制层可见的合成计划。",
        "pending",
        "control_only",
    ):
        assert hidden not in system_message


def test_fresh_generate_reply_commits_once_and_media_retry_never_recommits(
    tmp_path: Path,
) -> None:
    payload = _fresh_generate_reply_payload(tmp_path / "local-data")

    assert payload == {
        "canonical_result": True,
        "private_world_status": "COMMITTED",
        "private_world_error_code": None,
        "private_world_delivery_id": "fresh-canonical:1",
        "reply_revision": 1,
        "recovery_count": 0,
        "event_count_after_canonical": 1,
        "event_count_after_media_retry": 1,
        "media_status": "COMPLETED",
    }


def test_fresh_generate_reply_keeps_canonical_reply_pending_when_sqlite_fails(
    tmp_path: Path,
) -> None:
    payload = _fresh_generate_reply_payload(
        tmp_path / "local-data",
        simulate_sqlite_failure=True,
    )

    assert payload == {
        "canonical_result": True,
        "private_world_status": "PENDING",
        "private_world_error_code": "PRIVATE_WORLD_UNAVAILABLE",
        "private_world_delivery_id": "fresh-canonical:1",
        "reply_revision": 1,
        "recovery_count": 0,
        "event_count_after_canonical": 0,
        "event_count_after_media_retry": 0,
        "media_status": "COMPLETED",
    }


def test_fresh_generate_reply_keeps_canonical_reply_pending_when_snapshot_is_corrupt(
    tmp_path: Path,
) -> None:
    payload = _fresh_generate_reply_payload(
        tmp_path / "local-data",
        simulate_semantic_failure=True,
    )

    assert payload == {
        "canonical_result": True,
        "private_world_status": "PENDING",
        "private_world_error_code": "PRIVATE_WORLD_UNAVAILABLE",
        "private_world_delivery_id": "fresh-canonical:1",
        "reply_revision": 1,
        "recovery_count": 0,
        "event_count_after_canonical": 0,
        "event_count_after_media_retry": 0,
        "media_status": "COMPLETED",
    }


@pytest.mark.parametrize(
    ("private_world_environment", "expected_status", "expected_reason"),
    [
        (
            {"OLIVIA_PRIVATE_WORLD_ENABLED": "0"},
            "disabled",
            "PRIVATE_WORLD_DISABLED",
        ),
        (
            {"OLIVIA_PRIVATE_WORLD_DB": "relative.sqlite3"},
            "unavailable",
            "PRIVATE_WORLD_DB_MUST_BE_ABSOLUTE",
        ),
    ],
)
def test_local_server_private_world_failure_uses_null_port_without_blocking_letters(
    tmp_path: Path,
    private_world_environment: dict[str, str],
    expected_status: str,
    expected_reason: str,
) -> None:
    payload = _fresh_local_server_payload(
        tmp_path / "local-data",
        private_world_environment=private_world_environment,
    )

    assert payload["database_exists"] is False
    assert payload["runtime_port_type"] == "NullPrivateWorldPort"
    assert payload["runtime_committer_type"] == "NoneType"
    assert payload["adapter_uses_runtime_port"] is True
    assert payload["delivery_uses_runtime_committer"] is True
    assert payload["delivery_committed"] is False
    assert payload["delivery_status"] == "PENDING"
    assert payload["delivery_error_code"] == "PRIVATE_WORLD_UNAVAILABLE"
    assert payload["health"]["status"] == expected_status
    assert payload["health"]["provider"] == "none"
    assert payload["health"]["reason_code"] == expected_reason
    assert payload["health"]["network_called"] is False
    private_behavior = payload["reply_context"]["private_behavior"]
    assert private_behavior == {
        "familiarity": "unknown",
        "trust": "unknown",
        "comfort": "unknown",
        "closeness": "unknown",
        "tension": "unknown",
        "relationship_stage": "unknown",
        "nickname_permission": "not_allowed",
            "home_history_allowed": False,
        "known_continuations": [],
    }
