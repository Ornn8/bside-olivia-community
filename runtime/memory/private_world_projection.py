"""Pure projection from hidden PrivateWorld state to bounded model hints."""

from __future__ import annotations

from dataclasses import dataclass

from private_world_port import (
    LocalContinuationFact,
    PrivateWorldSnapshot,
)
from runtime.reply.reply_context import (
    BehaviorLevel,
    IntimacyTier,
    KnownContinuationFact,
    NicknamePermission,
    PrivateBehaviorView,
    RelationshipStage,
    intimacy_ceiling_for_stage,
)


def _level(value: int) -> BehaviorLevel:
    if value == 0:
        return BehaviorLevel.UNKNOWN
    if value < 35:
        return BehaviorLevel.LOW
    if value < 70:
        return BehaviorLevel.MEDIUM
    return BehaviorLevel.HIGH


def _stage(value: str) -> RelationshipStage:
    try:
        return RelationshipStage(value)
    except ValueError:
        return RelationshipStage.UNKNOWN


def _known_fact(value: LocalContinuationFact) -> KnownContinuationFact:
    return KnownContinuationFact(value.fact_id, value.statement)


@dataclass(frozen=True)
class ProjectedPrivateWorld:
    behavior: PrivateBehaviorView
    intimacy_ceiling: IntimacyTier
    granted_intimacy: IntimacyTier
    authorized_nicknames: tuple[str, ...]
    may_acknowledge_home_history: bool
    continuation_known: bool
    known_continuation_facts: tuple[KnownContinuationFact, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "behavior": self.behavior.to_dict(),
            "intimacy_ceiling": self.intimacy_ceiling.value,
            "granted_intimacy": self.granted_intimacy.value,
            "authorized_nicknames": list(self.authorized_nicknames),
            "continuation_known": self.continuation_known,
            "known_continuations": [
                fact.to_dict() for fact in self.known_continuation_facts
            ],
        }


def project_private_world(
    snapshot: PrivateWorldSnapshot,
) -> ProjectedPrivateWorld:
    if not isinstance(snapshot, PrivateWorldSnapshot):
        raise TypeError(
            "projection requires a PrivateWorldSnapshot"
        )
    character = snapshot.character_view()
    nicknames = tuple(character.nickname_permissions)
    known_facts = tuple(
        _known_fact(fact) for fact in character.continuation_facts
    )
    continuation_known = character.continuation_known
    stage = _stage(character.relationship_stage)
    intimacy_ceiling = intimacy_ceiling_for_stage(stage)
    granted_intimacy = character.granted_intimacy
    behavior = PrivateBehaviorView(
        familiarity=_level(snapshot.familiarity),
        trust=_level(snapshot.trust),
        comfort=_level(snapshot.comfort),
        closeness=_level(snapshot.closeness),
        tension=_level(snapshot.tension),
        relationship_stage=stage,
        intimacy_ceiling=intimacy_ceiling,
        granted_intimacy=granted_intimacy,
        nickname_permission=(
            NicknamePermission.ALLOWED
            if nicknames
            else NicknamePermission.NOT_ALLOWED
        ),
        home_history_allowed=(
            character.home_history_allowed
        ),
        known_continuations=known_facts,
    )
    return ProjectedPrivateWorld(
        behavior=behavior,
        intimacy_ceiling=intimacy_ceiling,
        granted_intimacy=granted_intimacy,
        authorized_nicknames=nicknames,
        may_acknowledge_home_history=(
            character.home_history_allowed
        ),
        continuation_known=continuation_known,
        known_continuation_facts=known_facts,
    )
