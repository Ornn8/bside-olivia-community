"""Pure projection from hidden PrivateWorld state to bounded model hints."""

from __future__ import annotations

from dataclasses import dataclass

from private_world_port import (
    ContinuationAwareness,
    LocalContinuationFact,
    PrivateWorldSnapshot,
)
from reply_context import (
    BehaviorLevel,
    HomeAccess,
    KnownContinuationFact,
    NicknamePermission,
    PrivateBehaviorView,
    RelationshipStage,
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
    authorized_nicknames: tuple[str, ...]
    may_acknowledge_home_history: bool
    continuation_known: bool
    known_continuation_facts: tuple[KnownContinuationFact, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "behavior": self.behavior.to_dict(),
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
    nicknames = tuple(snapshot.nickname_permissions)
    known_facts = tuple(
        _known_fact(fact)
        for fact in snapshot.continuation_facts
        if fact.awareness
        is ContinuationAwareness.CHARACTER_KNOWN
    )
    continuation_known = (
        snapshot.continuation_awareness
        is ContinuationAwareness.CHARACTER_KNOWN
        or bool(known_facts)
    )
    behavior = PrivateBehaviorView(
        familiarity=_level(snapshot.familiarity),
        trust=_level(snapshot.trust),
        comfort=_level(snapshot.comfort),
        closeness=_level(snapshot.closeness),
        tension=_level(snapshot.tension),
        relationship_stage=_stage(
            snapshot.relationship_stage
        ),
        nickname_permission=(
            NicknamePermission.ALLOWED
            if nicknames
            else NicknamePermission.NOT_ALLOWED
        ),
        home_access=HomeAccess(snapshot.home_access.value),
        known_continuations=known_facts,
    )
    return ProjectedPrivateWorld(
        behavior=behavior,
        authorized_nicknames=nicknames,
        may_acknowledge_home_history=(
            snapshot.home_access.value != "no_access"
        ),
        continuation_known=continuation_known,
        known_continuation_facts=known_facts,
    )
