from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from runtime.persona.style_exemplar_builder import (
    StyleExemplarBuildError,
    build_style_exemplar,
    contiguous_overlap_count,
)


ROOT = Path(__file__).resolve().parents[2]


def _valid_exemplar() -> dict[str, object]:
    return {
        "exemplar_id": "style.synthetic.greeting",
        "source_id": "source.synthetic",
        "derivation": "SYNTHETIC",
        "rights_status": "REDISTRIBUTABLE",
        "allowed_public_release": True,
        "mode": "text_letter",
        "situation": "brief_greeting",
        "user_text": "今天还好吗？",
        "assistant_text": "还行。你呢。",
        "style_only": True,
        "factual_authority": False,
        "user_text_is_synthetic": True,
        "assistant_text_is_verbatim": False,
    }


def test_source_overlap_of_seven_contiguous_characters_fails_build() -> None:
    source = "她走到窗边看见一只鸟停在雨里。"

    with pytest.raises(
        StyleExemplarBuildError,
        match="STYLE_EXEMPLAR_SOURCE_OVERLAP",
    ):
        build_style_exemplar(
            exemplar_id="style.synthetic.overlap",
            source_id="source.synthetic",
            mode="text_letter",
            situation="ordinary_smalltalk",
            user_text="外面在下雨。",
            assistant_text="我走到窗边看见一只鸟。",
            source_texts=(source,),
            user_text_is_synthetic=True,
        )


def test_private_source_overlap_in_synthetic_user_text_fails_build() -> None:
    source = "用户原信里写着窗外的雨一直没有停。"

    with pytest.raises(
        StyleExemplarBuildError,
        match="STYLE_EXEMPLAR_SOURCE_OVERLAP",
    ):
        build_style_exemplar(
            exemplar_id="style.synthetic.user-overlap",
            source_id="source.synthetic",
            mode="text_letter",
            situation="ordinary_smalltalk",
            user_text="窗外的雨一直没有停。",
            assistant_text="我听见了。",
            source_texts=(source,),
            user_text_is_synthetic=True,
        )


def test_builder_requires_explicit_synthetic_user_text_attestation() -> None:
    with pytest.raises(
        StyleExemplarBuildError,
        match="STYLE_EXEMPLAR_USER_TEXT_NOT_SYNTHETIC",
    ):
        build_style_exemplar(
            exemplar_id="style.synthetic.unverified-user",
            source_id="source.synthetic",
            mode="text_letter",
            situation="ordinary_smalltalk",
            user_text="这段输入没有被声明为合成文本。",
            assistant_text="我明白。",
            source_texts=(),
            user_text_is_synthetic=False,
        )


def test_overlap_checker_is_unicode_normalized_and_counts_matching_windows() -> None:
    assert contiguous_overlap_count("Cafe\u0301真安静。", ("Café真安静。",)) > 0
    assert contiguous_overlap_count("这是一句完全不同的话。", ("没有相同片段。",)) == 0


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("style_only", False),
        ("factual_authority", True),
        ("user_text_is_synthetic", False),
        ("assistant_text_is_verbatim", True),
    ),
)
def test_style_exemplar_const_contracts_are_enforced_by_schema(
    field: str,
    invalid: bool,
) -> None:
    schema = json.loads(
        (ROOT / "contracts" / "persona_v2.schema.json").read_text(encoding="utf-8")
    )
    payload = _valid_exemplar()
    payload[field] = invalid

    with pytest.raises(ValidationError):
        Draft202012Validator(schema["$defs"]["style_exemplar"]).validate(payload)


def test_builder_emits_only_non_factual_synthetic_style_guidance() -> None:
    built = build_style_exemplar(
        exemplar_id="style.synthetic.safe",
        source_id="source.synthetic",
        mode="spoken_video",
        situation="emotional_acknowledgement",
        user_text="我有点累。",
        assistant_text="那就先停一会儿。别急着跟今天较劲。",
        source_texts=("一段仅用于测试的来源文本。",),
        user_text_is_synthetic=True,
    )

    assert built["derivation"] == "SYNTHETIC"
    assert built["style_only"] is True
    assert built["factual_authority"] is False
    assert built["user_text_is_synthetic"] is True
    assert built["assistant_text_is_verbatim"] is False
