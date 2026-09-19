from __future__ import annotations

from pathlib import Path
import json

import pytest
from jsonschema import Draft202012Validator

from runtime.persona.persona_distillation import (
    CandidateDeclaration,
    CandidateStyleExemplar,
    DistillationBuildError,
    DistillationDocument,
    DistillationEvidenceRole,
    DistillationSourceKind,
    TopicExtraction,
    assign_user_folds,
    distill_persona,
    inventory_local_corpus,
    load_local_corpus,
)


class SyntheticExtractor:
    def extract(self, topic, documents):
        if topic != "explicit_boundaries":
            return TopicExtraction()
        return TopicExtraction(
            declarations=(
                CandidateDeclaration(
                    declaration_id="private.boundary.synthetic",
                    source_id=documents[0].source_id,
                    topic=topic,
                    statement="The character explicitly declines a synthetic request.",
                    confidence="HIGH",
                    facet="AUTONOMY",
                    tier="COMMUNITY_SOFT_CANON",
                ),
            ),
            style_exemplars=(
                CandidateStyleExemplar(
                    exemplar_id="style.private.synthetic",
                    source_id=documents[0].source_id,
                    mode="text_letter",
                    situation="boundary_refusal",
                    user_text="Please accept this synthetic request.",
                    assistant_text="No. I can decide what I agree to.",
                    user_text_is_synthetic=True,
                ),
            ),
        )


def _private_document(source_id: str, text: str = "Completely different fixture."):
    return DistillationDocument(
        source_id=source_id,
        partition_id="partition.synthetic",
        text=text,
        source_kind=DistillationSourceKind.LOCAL_PRIVATE_CORRESPONDENCE,
        rights_status="LOCAL_PRIVATE_ONLY",
        evidence_role=DistillationEvidenceRole.CANONICAL_CHARACTER_REPLY,
    )


def test_same_inputs_produce_byte_identical_distillation_results() -> None:
    documents = (_private_document("letter.synthetic.1"),)

    first = distill_persona(documents, (), SyntheticExtractor())
    second = distill_persona(tuple(reversed(documents)), (), SyntheticExtractor())

    assert first.canonical_json() == second.canonical_json()
    assert first.result_sha256 == second.result_sha256
    assert first.declarations[0]["rights_status"] == "LOCAL_PRIVATE_ONLY"
    assert first.declarations[0]["allowed_public_release"] is False
    assert first.style_exemplars[0]["derivation"] == "PRIVATE_CORPUS_ABSTRACTION"
    assert first.style_exemplars[0]["allowed_public_release"] is False


def test_holdout_source_cannot_appear_in_any_candidate() -> None:
    class HoldoutReferencingExtractor:
        def extract(self, topic, documents):
            if topic != "family_background":
                return TopicExtraction()
            return TopicExtraction(
                declarations=(
                    CandidateDeclaration(
                        declaration_id="private.holdout.leak",
                        source_id="letter.synthetic.holdout",
                        topic=topic,
                        statement="A synthetic leak attempt.",
                        confidence="HIGH",
                        facet="BACKGROUND",
                        tier="COMMUNITY_SOFT_CANON",
                    ),
                )
            )

    documents = (
        _private_document("letter.synthetic.training"),
        _private_document("letter.synthetic.holdout"),
    )

    with pytest.raises(DistillationBuildError, match="HOLDOUT_SOURCE_REFERENCED"):
        distill_persona(
            documents,
            ("letter.synthetic.holdout",),
            HoldoutReferencingExtractor(),
        )


def test_private_style_exemplar_still_enforces_seven_character_overlap() -> None:
    overlapping = _private_document(
        "letter.synthetic.1",
        "The character says: I can decide what I agree to.",
    )

    with pytest.raises(
        DistillationBuildError,
        match="STYLE_EXEMPLAR_SOURCE_OVERLAP",
    ):
        distill_persona((overlapping,), (), SyntheticExtractor())


def test_public_release_requires_a_public_non_private_source() -> None:
    class PublicClaimExtractor:
        def extract(self, topic, documents):
            if topic != "family_background":
                return TopicExtraction()
            return TopicExtraction(
                declarations=(
                    CandidateDeclaration(
                        declaration_id="public.synthetic",
                        source_id=documents[0].source_id,
                        topic=topic,
                        statement="A synthetic public fact.",
                        confidence="HIGH",
                        facet="BACKGROUND",
                        tier="PUBLIC_CANON",
                        allowed_public_release=True,
                    ),
                )
            )

    with pytest.raises(DistillationBuildError, match="PUBLIC_RELEASE_SOURCE_INVALID"):
        distill_persona(
            (_private_document("letter.synthetic.1"),),
            (),
            PublicClaimExtractor(),
        )


def test_user_letter_cannot_authorize_character_fact_or_style_output() -> None:
    class UserClaimExtractor:
        def extract(self, topic, documents):
            if topic != "family_background":
                return TopicExtraction()
            return TopicExtraction(
                declarations=(
                    CandidateDeclaration(
                        declaration_id="private.user.claim",
                        source_id="letter.synthetic.user",
                        topic=topic,
                        statement="The user claims a character fact.",
                        confidence="HIGH",
                        facet="BACKGROUND",
                        tier="COMMUNITY_SOFT_CANON",
                    ),
                )
            )

    user_letter = DistillationDocument(
        source_id="letter.synthetic.user",
        partition_id="partition.synthetic",
        text="A synthetic user-authored claim.",
        source_kind=DistillationSourceKind.LOCAL_PRIVATE_CORRESPONDENCE,
        rights_status="LOCAL_PRIVATE_ONLY",
        evidence_role=DistillationEvidenceRole.USER_LETTER,
    )

    with pytest.raises(
        DistillationBuildError,
        match="USER_LETTER_NOT_CHARACTER_EVIDENCE",
    ):
        distill_persona((user_letter,), (), UserClaimExtractor())


def test_user_fold_assignment_and_inventory_are_stable_and_sanitized(
    tmp_path: Path,
) -> None:
    for user, count in (("private-alice", 2), ("private-bob", 1)):
        folder = tmp_path / user
        folder.mkdir()
        for index in range(count):
            (folder / f"{index + 1:03d}.txt").write_text(
                f"Synthetic correspondence {index}.",
                encoding="utf-8",
            )
    (tmp_path / "private-alice" / "video.mp4").write_bytes(b"synthetic")

    folds = assign_user_folds(("private-bob", "private-alice"), fold_count=2)
    inventory = inventory_local_corpus(tmp_path, fold_count=2, holdout_fold=0)

    assert folds == assign_user_folds(("private-alice", "private-bob"), fold_count=2)
    assert inventory.document_count == 3
    assert inventory.video_count == 1
    assert inventory.user_partition_count == 2
    serialized = inventory.public_report_json()
    assert "private-alice" not in serialized
    assert "private-bob" not in serialized
    assert "Synthetic correspondence" not in serialized


def test_local_distillation_input_contract_is_valid_and_private_by_default() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "contracts" / "persona_distillation_input.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = {
        "schema_version": "p02.persona-distillation-input.v1",
        "documents": [
            {
                "source_id": "letter.synthetic.1",
                "partition_id": "partition.synthetic",
                "text": "Synthetic private correspondence.",
                "source_kind": "local_private_correspondence",
                "rights_status": "LOCAL_PRIVATE_ONLY",
                "evidence_role": "canonical_character_reply",
            }
        ],
        "holdout_source_ids": [],
    }

    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []


def test_explicit_file_manifest_assigns_correspondence_roles_without_text_guessing(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    partition = corpus / "user-a"
    partition.mkdir(parents=True)
    (partition / "001.txt").write_text(
        "This text deliberately contains no role hint.",
        encoding="utf-8",
    )
    (partition / "002.txt").write_text(
        "This text also deliberately contains no role hint.",
        encoding="utf-8",
    )
    manifest = tmp_path / "roles.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "p02.persona-corpus-manifest.v1",
                "entries": [
                    {
                        "relative_path": "user-a/001.txt",
                        "correspondence_id": "correspondence.synthetic.1",
                        "evidence_role": "user_letter",
                    },
                    {
                        "relative_path": "user-a/002.txt",
                        "correspondence_id": "correspondence.synthetic.1",
                        "evidence_role": "canonical_character_reply",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    documents, holdout_ids, inventory = load_local_corpus(
        corpus,
        fold_count=2,
        holdout_fold=1,
        role_manifest_path=manifest,
    )

    assert holdout_ids == ()
    assert tuple(item.evidence_role for item in documents) == (
        DistillationEvidenceRole.USER_LETTER,
        DistillationEvidenceRole.CANONICAL_CHARACTER_REPLY,
    )
    assert inventory.character_evidence_document_count == 1


def test_manifest_must_cover_every_text_file_and_pair_character_replies(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    partition = corpus / "user-a"
    partition.mkdir(parents=True)
    (partition / "001.txt").write_text("Synthetic text.", encoding="utf-8")
    manifest = tmp_path / "roles.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "p02.persona-corpus-manifest.v1",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        DistillationBuildError,
        match="DISTILLATION_ROLE_MANIFEST_COVERAGE_INVALID",
    ):
        load_local_corpus(
            corpus,
            fold_count=2,
            holdout_fold=1,
            role_manifest_path=manifest,
        )


def test_manifest_rejects_correspondence_pairs_across_user_partitions(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    holdout_partition = corpus / "holdout-user"
    training_partition = corpus / "training-user"
    holdout_partition.mkdir(parents=True)
    training_partition.mkdir(parents=True)
    (holdout_partition / "original.txt").write_text(
        "Synthetic user letter.",
        encoding="utf-8",
    )
    (training_partition / "reply.txt").write_text(
        "Synthetic canonical character reply.",
        encoding="utf-8",
    )
    assert assign_user_folds(
        ("holdout-user", "training-user"),
        fold_count=5,
    ) == {"holdout-user": 0, "training-user": 1}
    manifest = tmp_path / "roles.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "p02.persona-corpus-manifest.v1",
                "entries": [
                    {
                        "relative_path": "holdout-user/original.txt",
                        "correspondence_id": "correspondence.synthetic.cross-user",
                        "evidence_role": "user_letter",
                    },
                    {
                        "relative_path": "training-user/reply.txt",
                        "correspondence_id": "correspondence.synthetic.cross-user",
                        "evidence_role": "canonical_character_reply",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        DistillationBuildError,
        match="DISTILLATION_ROLE_MANIFEST_PAIR_INVALID",
    ):
        load_local_corpus(
            corpus,
            fold_count=5,
            holdout_fold=0,
            role_manifest_path=manifest,
        )


def test_per_file_role_manifest_has_a_closed_schema_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "contracts" / "persona_corpus_manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = {
        "schema_version": "p02.persona-corpus-manifest.v1",
        "entries": [
            {
                "relative_path": "user-a/001.txt",
                "correspondence_id": "correspondence.synthetic.1",
                "evidence_role": "user_letter",
            },
            {
                "relative_path": "user-a/002.txt",
                "correspondence_id": "correspondence.synthetic.1",
                "evidence_role": "canonical_character_reply",
            },
        ],
    }

    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
