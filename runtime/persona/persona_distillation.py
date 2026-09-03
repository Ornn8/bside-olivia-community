"""Deterministic, local-only persona distillation contracts and safeguards."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Protocol
from unicodedata import normalize

from .style_exemplar_builder import (
    StyleExemplarBuildError,
    build_style_exemplar,
)


DISTILLATION_TOPICS = (
    "family_background",
    "living_arrangements",
    "current_practice_repertoire",
    "original_compositions",
    "singing_self_report_and_performance",
    "music_taste_and_collection",
    "reading_interests",
    "daily_habits_and_aesthetics",
    "name_origin",
    "instruments_and_equipment",
    "explicit_boundaries",
)
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_FACETS = frozenset(
    {
        "POLICY",
        "IDENTITY",
        "BACKGROUND",
        "CORE_TRAIT",
        "AUTONOMY",
        "KNOWLEDGE_BOUNDARY",
        "EXPRESSION_STYLE",
        "RELATIONSHIP_STYLE",
        "MEMORY_CONTINUITY",
        "UNCERTAINTY",
        "MODE_STYLE",
        "SAFETY",
    }
)
_TIERS = frozenset(
    {"CONSTITUTION", "PUBLIC_CANON", "COMMUNITY_SOFT_CANON", "INFERRED", "UNCERTAINTY", "MODE_STYLE"}
)
_CONFIDENCE = frozenset({"LOW", "MEDIUM", "HIGH"})
_MODES = frozenset({"text_letter", "spoken_video", "musical_video", "future_im"})


class DistillationBuildError(ValueError):
    pass


class DistillationSourceKind(StrEnum):
    PUBLIC_DOCUMENT = "public_document"
    LOCAL_PRIVATE_CORRESPONDENCE = "local_private_correspondence"


class DistillationEvidenceRole(StrEnum):
    USER_LETTER = "user_letter"
    CANONICAL_CHARACTER_REPLY = "canonical_character_reply"
    PUBLIC_CHARACTER_SOURCE = "public_character_source"


@dataclass(frozen=True)
class DistillationDocument:
    source_id: str
    partition_id: str
    text: str
    source_kind: DistillationSourceKind
    rights_status: str
    evidence_role: DistillationEvidenceRole

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not _ID_RE.fullmatch(value)
            for value in (self.source_id, self.partition_id)
        ):
            raise DistillationBuildError("DISTILLATION_SOURCE_ID_INVALID")
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > 200_000
        ):
            raise DistillationBuildError("DISTILLATION_SOURCE_TEXT_INVALID")
        if not isinstance(self.source_kind, DistillationSourceKind):
            raise DistillationBuildError("DISTILLATION_SOURCE_KIND_INVALID")
        if not isinstance(self.evidence_role, DistillationEvidenceRole):
            raise DistillationBuildError("DISTILLATION_EVIDENCE_ROLE_INVALID")
        if self.source_kind is DistillationSourceKind.PUBLIC_DOCUMENT:
            valid_role = (
                self.evidence_role is DistillationEvidenceRole.PUBLIC_CHARACTER_SOURCE
            )
        else:
            valid_role = self.evidence_role in {
                DistillationEvidenceRole.USER_LETTER,
                DistillationEvidenceRole.CANONICAL_CHARACTER_REPLY,
            }
        if not valid_role:
            raise DistillationBuildError("DISTILLATION_EVIDENCE_ROLE_INVALID")
        expected = (
            "LOCAL_PRIVATE_ONLY"
            if self.source_kind
            is DistillationSourceKind.LOCAL_PRIVATE_CORRESPONDENCE
            else {"REDISTRIBUTABLE", "SUMMARY_ONLY"}
        )
        if isinstance(expected, str):
            valid_rights = self.rights_status == expected
        else:
            valid_rights = self.rights_status in expected
        if not valid_rights:
            raise DistillationBuildError("DISTILLATION_SOURCE_RIGHTS_INVALID")


@dataclass(frozen=True)
class CandidateDeclaration:
    declaration_id: str
    source_id: str
    topic: str
    statement: str
    confidence: str
    facet: str
    tier: str
    mode: str | None = None
    allowed_public_release: bool = False

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not _ID_RE.fullmatch(value)
            for value in (self.declaration_id, self.source_id)
        ):
            raise DistillationBuildError("DISTILLATION_DECLARATION_ID_INVALID")
        if self.topic not in DISTILLATION_TOPICS:
            raise DistillationBuildError("DISTILLATION_TOPIC_INVALID")
        if (
            not isinstance(self.statement, str)
            or not self.statement.strip()
            or len(self.statement) > 600
        ):
            raise DistillationBuildError("DISTILLATION_STATEMENT_INVALID")
        if self.confidence not in _CONFIDENCE or self.facet not in _FACETS:
            raise DistillationBuildError("DISTILLATION_CLASSIFICATION_INVALID")
        if self.tier not in _TIERS:
            raise DistillationBuildError("DISTILLATION_TIER_INVALID")
        if (self.tier == "MODE_STYLE") != (self.mode is not None):
            raise DistillationBuildError("DISTILLATION_MODE_STYLE_INVALID")
        if self.mode is not None and self.mode not in _MODES:
            raise DistillationBuildError("DISTILLATION_MODE_INVALID")
        if type(self.allowed_public_release) is not bool:
            raise DistillationBuildError("DISTILLATION_RELEASE_FLAG_INVALID")


@dataclass(frozen=True)
class CandidateStyleExemplar:
    exemplar_id: str
    source_id: str
    mode: str
    situation: str
    user_text: str
    assistant_text: str
    user_text_is_synthetic: bool

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not _ID_RE.fullmatch(value)
            for value in (self.exemplar_id, self.source_id, self.situation)
        ):
            raise DistillationBuildError("DISTILLATION_EXEMPLAR_ID_INVALID")
        if type(self.user_text_is_synthetic) is not bool:
            raise DistillationBuildError(
                "DISTILLATION_EXEMPLAR_USER_TEXT_PROVENANCE_INVALID"
            )


@dataclass(frozen=True)
class TopicExtraction:
    declarations: tuple[CandidateDeclaration, ...] = ()
    style_exemplars: tuple[CandidateStyleExemplar, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.declarations, (str, bytes)) or any(
            not isinstance(item, CandidateDeclaration) for item in self.declarations
        ):
            raise DistillationBuildError("DISTILLATION_DECLARATIONS_INVALID")
        if isinstance(self.style_exemplars, (str, bytes)) or any(
            not isinstance(item, CandidateStyleExemplar)
            for item in self.style_exemplars
        ):
            raise DistillationBuildError("DISTILLATION_EXEMPLARS_INVALID")


class PersonaTopicExtractor(Protocol):
    def extract(
        self,
        topic: str,
        documents: tuple[DistillationDocument, ...],
    ) -> TopicExtraction: ...


@dataclass(frozen=True)
class DistillationResult:
    declarations: tuple[dict[str, object], ...]
    style_exemplars: tuple[dict[str, object], ...]
    training_source_count: int
    holdout_source_count: int
    unresolved_topics: tuple[str, ...]

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "declarations": self.declarations,
                "holdout_source_count": self.holdout_source_count,
                "style_exemplars": self.style_exemplars,
                "training_source_count": self.training_source_count,
                "unresolved_topics": self.unresolved_topics,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def result_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorpusInventory:
    user_partition_count: int
    document_count: int
    training_document_count: int
    holdout_document_count: int
    video_count: int
    corpus_sha256: str
    character_evidence_document_count: int = 0

    def public_report_json(self) -> str:
        return json.dumps(
            {
                "corpus_sha256": self.corpus_sha256,
                "document_count": self.document_count,
                "holdout_document_count": self.holdout_document_count,
                "character_evidence_document_count": (
                    self.character_evidence_document_count
                ),
                "status": (
                    "LOCAL_PRIVATE_READY"
                    if self.character_evidence_document_count
                    else "LOCAL_PRIVATE_INPUT_ONLY"
                ),
                "training_document_count": self.training_document_count,
                "user_partition_count": self.user_partition_count,
                "video_count": self.video_count,
                "videos_excluded": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def assign_user_folds(
    user_partition_ids: tuple[str, ...],
    *,
    fold_count: int = 5,
) -> dict[str, int]:
    if type(fold_count) is not int or fold_count < 2:
        raise DistillationBuildError("DISTILLATION_FOLD_COUNT_INVALID")
    normalized = tuple(normalize("NFC", item) for item in user_partition_ids)
    if len(normalized) != len(set(normalized)):
        raise DistillationBuildError("DISTILLATION_PARTITIONS_NOT_UNIQUE")
    ordered = sorted(
        normalized,
        key=lambda item: hashlib.sha256(item.encode("utf-8")).hexdigest(),
    )
    return {partition: index % fold_count for index, partition in enumerate(ordered)}


def _load_role_manifest(
    manifest_path: Path | None,
    source_paths: tuple[str, ...],
) -> dict[str, tuple[str, DistillationEvidenceRole]] | None:
    if manifest_path is None:
        return None
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DistillationBuildError(
            "DISTILLATION_ROLE_MANIFEST_INVALID"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "entries",
    } or payload["schema_version"] != "p02.persona-corpus-manifest.v1":
        raise DistillationBuildError("DISTILLATION_ROLE_MANIFEST_INVALID")
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise DistillationBuildError("DISTILLATION_ROLE_MANIFEST_INVALID")
    roles: dict[str, tuple[str, DistillationEvidenceRole]] = {}
    correspondence_roles: dict[
        tuple[str, str], set[DistillationEvidenceRole]
    ] = {}
    correspondence_partitions: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "relative_path",
            "correspondence_id",
            "evidence_role",
        }:
            raise DistillationBuildError("DISTILLATION_ROLE_MANIFEST_INVALID")
        relative = entry["relative_path"]
        correspondence_id = entry["correspondence_id"]
        try:
            role = DistillationEvidenceRole(entry["evidence_role"])
        except (TypeError, ValueError) as exc:
            raise DistillationBuildError(
                "DISTILLATION_ROLE_MANIFEST_INVALID"
            ) from exc
        relative_path = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            relative_path is None
            or relative_path.is_absolute()
            or relative_path.suffix.casefold() != ".txt"
            or ".." in relative_path.parts
            or "\\" in relative
            or not isinstance(correspondence_id, str)
            or not _ID_RE.fullmatch(correspondence_id)
            or role is DistillationEvidenceRole.PUBLIC_CHARACTER_SOURCE
            or relative in roles
        ):
            raise DistillationBuildError("DISTILLATION_ROLE_MANIFEST_INVALID")
        partition = relative_path.parts[0]
        roles[relative] = (correspondence_id, role)
        correspondence_roles.setdefault((partition, correspondence_id), set()).add(role)
        correspondence_partitions.setdefault(correspondence_id, set()).add(partition)
    if set(roles) != set(source_paths):
        raise DistillationBuildError(
            "DISTILLATION_ROLE_MANIFEST_COVERAGE_INVALID"
        )
    if (
        any(len(grouped) != 1 for grouped in correspondence_partitions.values())
        or any(
            DistillationEvidenceRole.CANONICAL_CHARACTER_REPLY in grouped
            and DistillationEvidenceRole.USER_LETTER not in grouped
            for grouped in correspondence_roles.values()
        )
    ):
        raise DistillationBuildError("DISTILLATION_ROLE_MANIFEST_PAIR_INVALID")
    return roles


def load_local_corpus(
    root: Path,
    *,
    fold_count: int = 5,
    holdout_fold: int = 0,
    role_manifest_path: Path | None = None,
) -> tuple[tuple[DistillationDocument, ...], tuple[str, ...], CorpusInventory]:
    path = Path(root)
    if not path.is_dir() or not 0 <= holdout_fold < fold_count:
        raise DistillationBuildError("DISTILLATION_CORPUS_INVALID")
    partitions = tuple(sorted(item.name for item in path.iterdir() if item.is_dir()))
    folds = assign_user_folds(partitions, fold_count=fold_count)
    source_paths = tuple(
        sorted(
            item.relative_to(path).as_posix()
            for item in path.rglob("*.txt")
            if item.is_file()
        )
    )
    role_manifest = _load_role_manifest(role_manifest_path, source_paths)
    documents: list[DistillationDocument] = []
    digest_rows: list[str] = []
    holdout_ids: list[str] = []
    for partition in partitions:
        partition_path = path / partition
        partition_digest = hashlib.sha256(
            normalize("NFC", partition).encode("utf-8")
        ).hexdigest()[:32]
        for source_path in sorted(partition_path.rglob("*.txt")):
            relative = source_path.relative_to(path).as_posix()
            source_digest = hashlib.sha256(
                normalize("NFC", relative).encode("utf-8")
            ).hexdigest()[:32]
            source_id = f"private-letter:{source_digest}"
            evidence_role = (
                role_manifest[relative][1]
                if role_manifest is not None
                else DistillationEvidenceRole.USER_LETTER
            )
            if folds[partition] == holdout_fold:
                holdout_ids.append(source_id)
                digest_rows.append(
                    f"holdout:{source_digest}:{source_path.stat().st_size}"
                )
                continue
            raw = source_path.read_bytes()
            try:
                source_text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DistillationBuildError(
                    "DISTILLATION_CORPUS_ENCODING_INVALID"
                ) from exc
            documents.append(
                DistillationDocument(
                    source_id=source_id,
                    partition_id=f"private-user:{partition_digest}",
                    text=source_text,
                    source_kind=(
                        DistillationSourceKind.LOCAL_PRIVATE_CORRESPONDENCE
                    ),
                    rights_status="LOCAL_PRIVATE_ONLY",
                    evidence_role=evidence_role,
                )
            )
            digest_rows.append(
                f"{source_digest}:{hashlib.sha256(raw).hexdigest()}"
            )
    videos = sum(1 for item in path.rglob("*.mp4") if item.is_file())
    corpus_digest = hashlib.sha256(
        "\n".join(sorted(digest_rows)).encode("ascii")
    ).hexdigest()
    inventory = CorpusInventory(
        user_partition_count=len(partitions),
        document_count=len(documents) + len(holdout_ids),
        training_document_count=len(documents),
        holdout_document_count=len(holdout_ids),
        video_count=videos,
        corpus_sha256=corpus_digest,
        character_evidence_document_count=(
            sum(
                role is DistillationEvidenceRole.CANONICAL_CHARACTER_REPLY
                for _, role in role_manifest.values()
            )
            if role_manifest is not None
            else 0
        ),
    )
    return tuple(documents), tuple(sorted(holdout_ids)), inventory


def inventory_local_corpus(
    root: Path,
    *,
    fold_count: int = 5,
    holdout_fold: int = 0,
    role_manifest_path: Path | None = None,
) -> CorpusInventory:
    return load_local_corpus(
        root,
        fold_count=fold_count,
        holdout_fold=holdout_fold,
        role_manifest_path=role_manifest_path,
    )[2]


def distill_persona(
    documents: tuple[DistillationDocument, ...],
    holdout_source_ids: tuple[str, ...],
    extractor: PersonaTopicExtractor,
) -> DistillationResult:
    supplied = tuple(sorted(documents, key=lambda item: item.source_id))
    if any(not isinstance(item, DistillationDocument) for item in supplied):
        raise DistillationBuildError("DISTILLATION_SOURCE_INVALID")
    by_id = {item.source_id: item for item in supplied}
    if len(by_id) != len(supplied):
        raise DistillationBuildError("DISTILLATION_SOURCE_ID_DUPLICATE")
    holdout = frozenset(holdout_source_ids)
    if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in holdout):
        raise DistillationBuildError("DISTILLATION_HOLDOUT_INVALID")
    training = tuple(item for item in supplied if item.source_id not in holdout)
    if not training:
        raise DistillationBuildError("DISTILLATION_TRAINING_EMPTY")

    declarations: list[dict[str, object]] = []
    exemplars: list[dict[str, object]] = []
    unresolved: list[str] = []
    for topic in DISTILLATION_TOPICS:
        extracted = extractor.extract(topic, training)
        if not isinstance(extracted, TopicExtraction):
            raise DistillationBuildError("DISTILLATION_EXTRACTOR_RESULT_INVALID")
        if not extracted.declarations and not extracted.style_exemplars:
            unresolved.append(topic)
        for candidate in extracted.declarations:
            if candidate.source_id in holdout:
                raise DistillationBuildError("HOLDOUT_SOURCE_REFERENCED")
            source = by_id.get(candidate.source_id)
            if source is None or candidate.topic != topic:
                raise DistillationBuildError("DISTILLATION_EVIDENCE_INVALID")
            if source.evidence_role is DistillationEvidenceRole.USER_LETTER:
                raise DistillationBuildError(
                    "USER_LETTER_NOT_CHARACTER_EVIDENCE"
                )
            if candidate.allowed_public_release and source.source_kind is not (
                DistillationSourceKind.PUBLIC_DOCUMENT
            ):
                raise DistillationBuildError("PUBLIC_RELEASE_SOURCE_INVALID")
            row: dict[str, object] = {
                "declaration_id": candidate.declaration_id,
                "source_id": candidate.source_id,
                "tier": candidate.tier,
                "facet": candidate.facet,
                "confidence": candidate.confidence,
                "rights_status": source.rights_status,
                "allowed_public_release": candidate.allowed_public_release,
                "statement": candidate.statement,
            }
            if candidate.mode is not None:
                row["mode"] = candidate.mode
            declarations.append(row)
        for candidate in extracted.style_exemplars:
            if candidate.source_id in holdout:
                raise DistillationBuildError("HOLDOUT_SOURCE_REFERENCED")
            source = by_id.get(candidate.source_id)
            if source is None:
                raise DistillationBuildError("DISTILLATION_EVIDENCE_INVALID")
            if source.evidence_role is DistillationEvidenceRole.USER_LETTER:
                raise DistillationBuildError(
                    "USER_LETTER_NOT_CHARACTER_EVIDENCE"
                )
            try:
                exemplars.append(
                    build_style_exemplar(
                        exemplar_id=candidate.exemplar_id,
                        source_id=candidate.source_id,
                        mode=candidate.mode,
                        situation=candidate.situation,
                        user_text=candidate.user_text,
                        assistant_text=candidate.assistant_text,
                        source_texts=(item.text for item in training),
                        user_text_is_synthetic=(
                            candidate.user_text_is_synthetic
                        ),
                        derivation="PRIVATE_CORPUS_ABSTRACTION",
                        rights_status=source.rights_status,
                        allowed_public_release=False,
                    )
                )
            except StyleExemplarBuildError as exc:
                raise DistillationBuildError(str(exc)) from exc

    declarations.sort(key=lambda item: str(item["declaration_id"]))
    exemplars.sort(key=lambda item: str(item["exemplar_id"]))
    if len({item["declaration_id"] for item in declarations}) != len(declarations):
        raise DistillationBuildError("DISTILLATION_DECLARATION_ID_DUPLICATE")
    if len({item["exemplar_id"] for item in exemplars}) != len(exemplars):
        raise DistillationBuildError("DISTILLATION_EXEMPLAR_ID_DUPLICATE")
    return DistillationResult(
        declarations=tuple(declarations),
        style_exemplars=tuple(exemplars),
        training_source_count=len(training),
        holdout_source_count=len(holdout),
        unresolved_topics=tuple(unresolved),
    )


__all__ = [
    "CandidateDeclaration",
    "CandidateStyleExemplar",
    "CorpusInventory",
    "DISTILLATION_TOPICS",
    "DistillationBuildError",
    "DistillationDocument",
    "DistillationEvidenceRole",
    "DistillationResult",
    "DistillationSourceKind",
    "PersonaTopicExtractor",
    "TopicExtraction",
    "assign_user_folds",
    "distill_persona",
    "inventory_local_corpus",
    "load_local_corpus",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory a local-only persona corpus without printing private data."
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--fold-count", type=int, default=5)
    parser.add_argument("--holdout-fold", type=int, default=0)
    parser.add_argument("--role-manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        inventory = inventory_local_corpus(
            args.corpus_root,
            fold_count=args.fold_count,
            holdout_fold=args.holdout_fold,
            role_manifest_path=args.role_manifest,
        )
    except (OSError, DistillationBuildError):
        print('{"status":"LOCAL_PRIVATE_UNAVAILABLE"}')
        return 2
    print(inventory.public_report_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
