"""Authenticated Control Center adapter for PrivateWorld commands."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Mapping, Sequence

from private_world_commands import (
    ConfirmRelationshipStage,
    DeleteContinuationFact,
    GrantNickname,
    PrivateWorldActor,
    PrivateWorldCommandSource,
    RecordBoundaryRespected,
    RecordConflict,
    RecordRepair,
    RevokeNickname,
    SetContinuationAwareness,
    SetHomeAccess,
    UpsertContinuationFact,
)
from private_world_ledger import LedgerEvent
from private_world_port import (
    ContinuationAwareness,
    HomeAccess,
    PrivateWorldSnapshot,
)
from private_world_service import (
    PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA,
    CommandExecutionResult,
    PrivateWorldCommandLedger,
    PrivateWorldCommandService,
    PrivateWorldCommandServiceError,
)
from reply_context import RelationshipStage


PRIVATE_WORLD_CONTROL_SCHEMA = "p03.private-world-control.v1"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RELATIONSHIP_EVENTS = {
    "boundary_respected": RecordBoundaryRespected,
    "conflict": RecordConflict,
    "repair": RecordRepair,
}


class PrivateWorldAPIError(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 400) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _level(value: int) -> str:
    if value == 0:
        return "unknown"
    if value < 35:
        return "low"
    if value < 70:
        return "medium"
    return "high"


def _strict_body(
    value: object,
    *,
    required: Sequence[str],
    optional: Sequence[str] = (),
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PrivateWorldAPIError("CONTROL_BODY_INVALID")
    allowed = frozenset((*required, *optional))
    keys = frozenset(value)
    if not frozenset(required).issubset(keys) or not keys.issubset(allowed):
        raise PrivateWorldAPIError("CONTROL_BODY_FIELDS_INVALID")
    if any(not isinstance(key, str) for key in value):
        raise PrivateWorldAPIError("CONTROL_BODY_FIELDS_INVALID")
    return value


def _text(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PrivateWorldAPIError(code)
    return value.strip()


def _request_identity(value: object) -> tuple[str, str]:
    request_id = _text(value, code="CONTROL_REQUEST_ID_INVALID")
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise PrivateWorldAPIError("CONTROL_REQUEST_ID_INVALID")
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"control.{digest}", f"control.{digest}"


def _occurred_at(value: object) -> datetime:
    raw = _text(value, code="CONTROL_OCCURRED_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateWorldAPIError("CONTROL_OCCURRED_AT_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PrivateWorldAPIError("CONTROL_OCCURRED_AT_INVALID")
    return parsed


def _string_tuple(
    value: object,
    *,
    code: str,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PrivateWorldAPIError(code)
    result = tuple(value)
    if (
        any(not isinstance(item, str) or not item for item in result)
        or len(set(result)) != len(result)
    ):
        raise PrivateWorldAPIError(code)
    return result


def _common_fields(
    body: Mapping[str, object],
) -> dict[str, object]:
    command_id, idempotency_key = _request_identity(body["request_id"])
    evidence = _string_tuple(
        body.get("evidence_refs", []),
        code="CONTROL_EVIDENCE_REFS_INVALID",
        maximum=8,
    )
    return {
        "command_id": command_id,
        "idempotency_key": idempotency_key,
        "actor": PrivateWorldActor.LOCAL_USER,
        "source": PrivateWorldCommandSource.CONTROL_CENTER,
        "occurred_at": _occurred_at(body["occurred_at"]),
        "reason": _text(body["reason"], code="CONTROL_REASON_INVALID"),
        "evidence_refs": evidence,
    }


def _execution_payload(result: CommandExecutionResult) -> dict[str, object]:
    return {
        "schema_version": PRIVATE_WORLD_CONTROL_SCHEMA,
        "result": result.to_dict(),
    }


def _snapshot_payload(snapshot: PrivateWorldSnapshot) -> dict[str, object]:
    if not isinstance(snapshot, PrivateWorldSnapshot):
        raise PrivateWorldAPIError(
            "PRIVATE_WORLD_CONTROL_STORAGE_UNAVAILABLE",
            http_status=503,
        )
    return {
        "schema_version": PRIVATE_WORLD_CONTROL_SCHEMA,
        "version": snapshot.version,
        "relationship_stage": snapshot.relationship_stage,
        "levels": {
            "familiarity": _level(snapshot.familiarity),
            "trust": _level(snapshot.trust),
            "comfort": _level(snapshot.comfort),
            "closeness": _level(snapshot.closeness),
            "tension": _level(snapshot.tension),
        },
        "nickname_permissions": list(snapshot.nickname_permissions),
        "home_access": snapshot.home_access.value,
        "continuation_awareness": snapshot.continuation_awareness.value,
        "continuation_facts": [
            fact.to_dict() for fact in snapshot.continuation_facts
        ],
    }


def _event_payload(event: LedgerEvent) -> dict[str, object]:
    if not isinstance(event, LedgerEvent):
        raise PrivateWorldAPIError(
            "PRIVATE_WORLD_CONTROL_AUDIT_INVALID",
            http_status=503,
        )
    payload = event.payload
    result: dict[str, object] = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "applied": payload.get("applied") is True,
        "reason_code": str(payload.get("reason_code", "UNKNOWN"))[:96],
        "change_fields": [],
    }
    raw_fields = payload.get("change_fields", ())
    if isinstance(raw_fields, list) and all(
        isinstance(field, str) for field in raw_fields
    ):
        result["change_fields"] = list(raw_fields)
    if payload.get("schema_version") == PRIVATE_WORLD_COMMAND_AUDIT_SCHEMA:
        evidence_refs = payload.get("evidence_refs")
        result.update(
            {
                "command_id": str(payload.get("command_id", ""))[:96],
                "command_kind": str(payload.get("command_kind", ""))[:96],
                "actor": str(payload.get("actor", ""))[:64],
                "source": str(payload.get("source", ""))[:64],
                "reason": str(payload.get("reason", ""))[:280],
                "evidence_refs": (
                    list(evidence_refs)
                    if isinstance(evidence_refs, list)
                    and all(isinstance(item, str) for item in evidence_refs)
                    else []
                ),
                "snapshot_version": int(
                    payload.get("snapshot_version", 0)
                ),
            }
        )
    return result


def _service_error(exc: PrivateWorldCommandServiceError) -> PrivateWorldAPIError:
    if exc.code == "PRIVATE_WORLD_COMMAND_IDENTITY_CONFLICT":
        status = 409
    elif exc.code == "PRIVATE_WORLD_COMMAND_STORAGE_UNAVAILABLE":
        status = 503
    elif exc.code in {
        "PRIVATE_WORLD_COMMAND_APPROVAL_REQUIRED",
        "PRIVATE_WORLD_COMMAND_SOURCE_FORBIDDEN",
    }:
        status = 403
    else:
        status = 400
    return PrivateWorldAPIError(exc.code, http_status=status)


class PrivateWorldControlAPI:
    def __init__(
        self,
        ledger: PrivateWorldCommandLedger,
        service: PrivateWorldCommandService,
    ) -> None:
        if not isinstance(ledger, PrivateWorldCommandLedger):
            raise TypeError("a PrivateWorld command ledger is required")
        if not isinstance(service, PrivateWorldCommandService):
            raise TypeError("a PrivateWorld command service is required")
        self._ledger = ledger
        self._service = service

    def snapshot(self) -> dict[str, object]:
        try:
            return _snapshot_payload(self._ledger.snapshot())
        except PrivateWorldAPIError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise PrivateWorldAPIError(
                "PRIVATE_WORLD_CONTROL_STORAGE_UNAVAILABLE",
                http_status=503,
            ) from exc

    def events(self) -> dict[str, object]:
        try:
            rows = self._ledger.events()
            return {
                "schema_version": PRIVATE_WORLD_CONTROL_SCHEMA,
                "events": [_event_payload(event) for event in rows],
            }
        except PrivateWorldAPIError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            raise PrivateWorldAPIError(
                "PRIVATE_WORLD_CONTROL_STORAGE_UNAVAILABLE",
                http_status=503,
            ) from exc

    def relationship_event(self, raw_body: object) -> dict[str, object]:
        body = _strict_body(
            raw_body,
            required=(
                "request_id",
                "occurred_at",
                "reason",
                "event_type",
            ),
            optional=("evidence_refs",),
        )
        event_type = _text(
            body["event_type"],
            code="CONTROL_RELATIONSHIP_EVENT_INVALID",
        )
        command_type = _RELATIONSHIP_EVENTS.get(event_type)
        if command_type is None:
            raise PrivateWorldAPIError("CONTROL_RELATIONSHIP_EVENT_INVALID")
        command = command_type(**_common_fields(body))
        return self._execute(command)

    def relationship_stage(self, raw_body: object) -> dict[str, object]:
        body = _strict_body(
            raw_body,
            required=(
                "request_id",
                "occurred_at",
                "reason",
                "target_stage",
                "basis_event_ids",
            ),
            optional=("evidence_refs",),
        )
        try:
            stage = RelationshipStage(body["target_stage"])
        except (TypeError, ValueError) as exc:
            raise PrivateWorldAPIError(
                "CONTROL_RELATIONSHIP_STAGE_INVALID"
            ) from exc
        basis = _string_tuple(
            body["basis_event_ids"],
            code="CONTROL_STAGE_BASIS_INVALID",
            maximum=8,
        )
        command = ConfirmRelationshipStage(
            **_common_fields(body),
            target_stage=stage,
            basis_event_ids=basis,
        )
        return self._execute(command)

    def nickname(self, raw_body: object) -> dict[str, object]:
        body = _strict_body(
            raw_body,
            required=(
                "request_id",
                "occurred_at",
                "reason",
                "action",
                "nickname",
            ),
            optional=("evidence_refs",),
        )
        action = _text(
            body["action"],
            code="CONTROL_NICKNAME_ACTION_INVALID",
        )
        command_type = {
            "grant": GrantNickname,
            "revoke": RevokeNickname,
        }.get(action)
        if command_type is None:
            raise PrivateWorldAPIError("CONTROL_NICKNAME_ACTION_INVALID")
        command = command_type(
            **_common_fields(body),
            nickname=_text(
                body["nickname"],
                code="CONTROL_NICKNAME_INVALID",
            ),
        )
        return self._execute(command)

    def home_access(self, raw_body: object) -> dict[str, object]:
        body = _strict_body(
            raw_body,
            required=(
                "request_id",
                "occurred_at",
                "reason",
                "home_access",
            ),
            optional=("evidence_refs",),
        )
        try:
            access = HomeAccess(body["home_access"])
        except (TypeError, ValueError) as exc:
            raise PrivateWorldAPIError("CONTROL_HOME_ACCESS_INVALID") from exc
        command = SetHomeAccess(
            **_common_fields(body),
            home_access=access,
        )
        return self._execute(command)

    def continuation(self, raw_body: object) -> dict[str, object]:
        if not isinstance(raw_body, Mapping):
            raise PrivateWorldAPIError("CONTROL_BODY_INVALID")
        action = _text(
            raw_body.get("action"),
            code="CONTROL_CONTINUATION_ACTION_INVALID",
        )
        common_required = (
            "request_id",
            "occurred_at",
            "reason",
            "action",
            "fact_id",
        )
        if action == "upsert":
            body = _strict_body(
                raw_body,
                required=(*common_required, "statement", "awareness"),
                optional=("evidence_refs",),
            )
            try:
                awareness = ContinuationAwareness(body["awareness"])
            except (TypeError, ValueError) as exc:
                raise PrivateWorldAPIError(
                    "CONTROL_CONTINUATION_AWARENESS_INVALID"
                ) from exc
            command = UpsertContinuationFact(
                **_common_fields(body),
                fact_id=_text(
                    body["fact_id"],
                    code="CONTROL_CONTINUATION_ID_INVALID",
                ),
                statement=_text(
                    body["statement"],
                    code="CONTROL_CONTINUATION_STATEMENT_INVALID",
                ),
                awareness=awareness,
            )
        elif action == "set_awareness":
            body = _strict_body(
                raw_body,
                required=(*common_required, "awareness"),
                optional=("evidence_refs",),
            )
            try:
                awareness = ContinuationAwareness(body["awareness"])
            except (TypeError, ValueError) as exc:
                raise PrivateWorldAPIError(
                    "CONTROL_CONTINUATION_AWARENESS_INVALID"
                ) from exc
            command = SetContinuationAwareness(
                **_common_fields(body),
                fact_id=_text(
                    body["fact_id"],
                    code="CONTROL_CONTINUATION_ID_INVALID",
                ),
                awareness=awareness,
            )
        elif action == "delete":
            body = _strict_body(
                raw_body,
                required=common_required,
                optional=("evidence_refs",),
            )
            command = DeleteContinuationFact(
                **_common_fields(body),
                fact_id=_text(
                    body["fact_id"],
                    code="CONTROL_CONTINUATION_ID_INVALID",
                ),
            )
        else:
            raise PrivateWorldAPIError("CONTROL_CONTINUATION_ACTION_INVALID")
        return self._execute(command)

    def _execute(self, command) -> dict[str, object]:
        try:
            return _execution_payload(self._service.execute(command))
        except PrivateWorldCommandServiceError as exc:
            raise _service_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise PrivateWorldAPIError("PRIVATE_WORLD_COMMAND_INVALID") from exc


__all__ = [
    "PRIVATE_WORLD_CONTROL_SCHEMA",
    "PrivateWorldAPIError",
    "PrivateWorldControlAPI",
]
