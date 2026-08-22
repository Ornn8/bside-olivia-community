"""Pure projection from hidden PrivateWorld state to bounded model hints."""

from __future__ import annotations

from dataclasses import dataclass

from private_world_port import (
    ContinuationAwareness,
    HomeAccess as PrivateHomeAccess,
    PrivateWorldSnapshot,
)
from reply_context import (
    BehaviorLevel,
    HomeAccess,
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


@dataclass(frozen=True)
class ProjectedPrivateWorld:
    behavior: PrivateBehaviorView
    authorized_nicknames: tuple[str, ...]
    may_acknowledge_home_history: bool
    continuation_known: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "behavior": self.behavior.to_dict(),
            "authorized_nicknames": list(self.authorized_nicknames),
            "continuation_known": self.continuation_known,
        }


def project_private_world(snapshot: PrivateWorldSnapshot) -> ProjectedPrivateWorld:
    if not isinstance(snapshot, PrivateWorldSnapshot):
        raise TypeError("projection requires a PrivateWorldSnapshot")
    nicknames = tuple(snapshot.nickname_permissions)
    behavior = PrivateBehaviorView(
        familiarity=_level(snapshot.familiarity),
        trust=_level(snapshot.trust),
        comfort=_level(snapshot.comfort),
        closeness=_level(snapshot.closeness),
        tension=_level(snapshot.tension),
        relationship_stage=_stage(snapshot.relationship_stage),
        nickname_permission=(
            NicknamePermission.ALLOWED
            if nicknames
            else NicknamePermission.NOT_ALLOWED
        ),
        home_access=HomeAccess.NO_ACCESS,
    )
    return ProjectedPrivateWorld(
        behavior=behavior,
        authorized_nicknames=nicknames,
        may_acknowledge_home_history=(
            snapshot.home_access is not PrivateHomeAccess.NO_ACCESS
        ),
        continuation_known=(
            snapshot.continuation_awareness
            is ContinuationAwareness.CHARACTER_KNOWN
        ),
    )
