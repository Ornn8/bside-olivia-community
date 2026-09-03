from datetime import datetime, timezone
from pathlib import Path

from llm_gateway import GatewayConfig
from local_server import LetterAdapter
from memory_port import NullMemoryPort
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 22, tzinfo=timezone.utc)


class SnapshotPort:
    def __init__(self, snapshot: PrivateWorldSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> PrivateWorldSnapshot:
        return self._snapshot


def test_private_continuity_reaches_persona_without_control_only_state() -> None:
    adapter = LetterAdapter(
        GatewayConfig(
            provider="mock",
            model="synthetic",
            persona_v2_enabled=True,
            persona_v2_file=str(
                ROOT / "linli_character" / "persona_release_v2.json"
            ),
        ),
        memory_port=NullMemoryPort(),
        private_world_port=SnapshotPort(
            PrivateWorldSnapshot(
                trust=81,
                relationship_stage="close",
                nickname_permissions=("小河豚",),
                home_access=HomeAccess.VISIT_ACCESS,
                continuation_facts=(
                    LocalContinuationFact(
                        "class.known",
                        "林离已经知道下周课程时间会调整。",
                        ContinuationAwareness.CHARACTER_KNOWN,
                    ),
                    LocalContinuationFact(
                        "trip.pending",
                        "角色尚未知道的旅行安排。",
                        ContinuationAwareness.PENDING,
                    ),
                    LocalContinuationFact(
                        "plan.control",
                        "控制层保存的未来计划。",
                        ContinuationAwareness.CONTROL_ONLY,
                    ),
                ),
            )
        ),
        now=lambda: NOW,
    )

    system = adapter._messages("今天普通地有点累。")[0]["content"]

    assert "小河豚" in system
    assert '"trust":"high"' in system
    assert '"home_history_allowed":true' in system
    assert "visit_access" not in system
    assert "林离已经知道下周课程时间会调整。" in system
    assert '"trust":81' not in system
    assert "trip.pending" not in system
    assert "plan.control" not in system
    assert "角色尚未知道的旅行安排" not in system
    assert "控制层保存的未来计划" not in system
    assert "control_only" not in system
    assert '"awareness":"pending"' not in system
