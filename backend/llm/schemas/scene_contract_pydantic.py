"""Versioned scene-transition contracts used by chapter outlines."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)


MAX_V2_ADHERENCE_ISSUES = 20


StableSceneIdentity = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    ),
]
CanonicalEventKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._:-]{0,99}$",
    ),
]
RequiredText500 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
RequiredText240 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
RequiredText200 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
RequiredText160 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
RequiredText120 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]


class SceneConditionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: StableSceneIdentity
    description: RequiredText500


class SceneBeatSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: StableSceneIdentity
    description: RequiredText500
    expected_transition: RequiredText500
    required: StrictBool = True


class NarrativeDeltaSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delta_id: StableSceneIdentity
    dimension: Literal[
        "knowledge",
        "relationship",
        "goal",
        "risk",
        "choice",
        "emotion",
    ]
    before: RequiredText500
    after: RequiredText500

    @model_validator(mode="after")
    def validate_state_change(self) -> "NarrativeDeltaSchema":
        before = self.before.strip()
        after = self.after.strip()
        if not before or not after or before == after:
            raise ValueError("narrative delta before and after must differ")
        return self


class SceneWordBudgetSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: StrictInt = Field(..., ge=1, le=50_000)
    target: StrictInt = Field(..., ge=1, le=50_000)
    max: StrictInt = Field(..., ge=1, le=50_000)

    @model_validator(mode="after")
    def validate_order(self) -> "SceneWordBudgetSchema":
        if not self.min <= self.target <= self.max:
            raise ValueError("word_budget must satisfy min <= target <= max")
        return self


class SceneTransitionContractSchema(BaseModel):
    """One auditable scene state transition in a V2 chapter outline."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["scene_transition_contract.v2"]
    scene_id: StableSceneIdentity
    summary: RequiredText500
    purpose: RequiredText200
    preconditions: list[SceneConditionSchema] = Field(
        ..., min_length=1, max_length=20
    )
    beats: list[SceneBeatSchema] = Field(..., min_length=1, max_length=20)
    postconditions: list[SceneConditionSchema] = Field(
        ..., min_length=1, max_length=20
    )
    forbidden_conditions: list[SceneConditionSchema] = Field(
        default_factory=list, max_length=20
    )
    narrative_delta: list[NarrativeDeltaSchema] = Field(
        ..., min_length=1, max_length=20
    )
    event_key: CanonicalEventKey
    repetition_policy: Literal["forbid", "allow_if_escalated", "allow"]
    word_budget: SceneWordBudgetSchema

    @model_validator(mode="after")
    def validate_atomic_identity(self) -> "SceneTransitionContractSchema":
        condition_ids = [
            condition.condition_id
            for condition in (
                *self.preconditions,
                *self.postconditions,
                *self.forbidden_conditions,
            )
        ]
        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError("condition_id values must be unique within a scene")
        beat_ids = [beat.beat_id for beat in self.beats]
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("beat_id values must be unique within a scene")
        if not any(beat.required for beat in self.beats):
            raise ValueError("a V2 scene requires at least one required beat")
        delta_ids = [delta.delta_id for delta in self.narrative_delta]
        if len(delta_ids) != len(set(delta_ids)):
            raise ValueError("delta_id values must be unique within a scene")
        return self


class ProseEvidenceSpanSchema(BaseModel):
    """Provider-selected exact quote with Unicode code-point offsets."""

    model_config = ConfigDict(extra="forbid")

    start: StrictInt = Field(..., ge=0)
    end: StrictInt = Field(..., ge=1)
    quote: str = Field(..., min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ProseEvidenceSpanSchema":
        if self.end <= self.start:
            raise ValueError("evidence span end must be greater than start")
        return self


class ValidatedProseEvidenceSpanSchema(BaseModel):
    """Local canonical evidence; the Provider never computes this hash."""

    model_config = ConfigDict(extra="forbid")

    start: StrictInt = Field(..., ge=0)
    end: StrictInt = Field(..., ge=1)
    quote_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bounds(self) -> "ValidatedProseEvidenceSpanSchema":
        if self.end <= self.start:
            raise ValueError("evidence span end must be greater than start")
        return self


class BeatEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: StableSceneIdentity
    beat_id: StableSceneIdentity
    status: Literal[
        "satisfied",
        "mentioned",
        "contradicted",
        "missing",
        "unknown",
    ]
    spans: list[ProseEvidenceSpanSchema] = Field(default_factory=list, max_length=2)
    explanation: RequiredText120

    @model_validator(mode="after")
    def validate_status_evidence(self) -> "BeatEvidenceSchema":
        if self.status in {"satisfied", "mentioned", "contradicted"} and not self.spans:
            raise ValueError(f"{self.status} beat evidence requires at least one span")
        if self.status == "missing" and self.spans:
            raise ValueError("missing beat evidence must not claim prose spans")
        return self


class OutlineContractFindingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: StableSceneIdentity
    category: Literal[
        "scene_order",
        "core_conflict",
        "ending_hook",
        "unplanned_major_event",
        "volume_arc",
        "forbidden_condition",
        "event_repetition",
    ]
    status: Literal["observed", "unknown"]
    scene_id: StableSceneIdentity | None = None
    beat_ids: list[StableSceneIdentity] = Field(default_factory=list, max_length=10)
    spans: list[ProseEvidenceSpanSchema] = Field(default_factory=list, max_length=2)
    explanation: RequiredText160

    @model_validator(mode="after")
    def validate_finding_evidence(self) -> "OutlineContractFindingSchema":
        if self.status == "observed" and not self.spans:
            raise ValueError("observed outline finding requires at least one span")
        return self


class ChapterOutlineAdherenceEvidenceSchema(BaseModel):
    """Provider evidence only; local policy derives the completion decision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chapter_outline_adherence_evidence.v2"]
    outline_contract_version: Literal["scene_transition_contract.v2"]
    summary: RequiredText500
    beat_evidence: list[BeatEvidenceSchema] = Field(..., min_length=1, max_length=400)
    findings: list[OutlineContractFindingSchema] = Field(
        default_factory=list,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_finding_identity(self) -> "ChapterOutlineAdherenceEvidenceSchema":
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding_id values must be unique")
        return self


class ValidatedOutlineSceneCoverageSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_index: StrictInt = Field(..., ge=1, le=100)
    status: Literal["covered", "partial", "missing"]
    evidence: str = Field(default="", max_length=240)


class ValidatedOutlineAdherenceIssueSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["warning", "error"]
    category: Literal[
        "scene_coverage",
        "scene_order",
        "core_conflict",
        "ending_hook",
        "unplanned_major_event",
        "volume_arc",
        "forbidden_condition",
        "event_repetition",
    ]
    outline_requirement: RequiredText240
    prose_evidence: RequiredText240
    explanation: RequiredText240


class ValidatedOutlineContractFindingSchema(OutlineContractFindingSchema):
    spans: list[ValidatedProseEvidenceSpanSchema] = Field(
        default_factory=list,
        max_length=2,
    )
    local_severity: Literal["warning", "error"]


class ValidatedBeatEvidenceSchema(BeatEvidenceSchema):
    spans: list[ValidatedProseEvidenceSpanSchema] = Field(
        default_factory=list,
        max_length=2,
    )


class ValidatedChapterOutlineAdherenceEvidenceSchema(BaseModel):
    """Locally verified V2 review persisted by the completion pipeline."""

    model_config = ConfigDict(extra="forbid")

    evidence_schema_version: Literal["chapter_outline_adherence_evidence.v2"]
    outline_contract_version: Literal["scene_transition_contract.v2"]
    outline_contract_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    summary: RequiredText500
    beat_evidence: list[ValidatedBeatEvidenceSchema] = Field(
        ...,
        min_length=1,
        max_length=400,
    )
    beat_status_counts: dict[
        Literal[
            "satisfied",
            "mentioned",
            "contradicted",
            "missing",
            "unknown",
        ],
        StrictInt,
    ]
    findings: list[ValidatedOutlineContractFindingSchema] = Field(
        default_factory=list,
        max_length=20,
    )
    verdict: Literal["pass", "fail"]
    scene_coverage: list[ValidatedOutlineSceneCoverageSchema] = Field(
        ...,
        min_length=1,
        max_length=100,
    )
    issues: list[ValidatedOutlineAdherenceIssueSchema] = Field(
        default_factory=list,
        max_length=MAX_V2_ADHERENCE_ISSUES,
    )
    source_prose_run_id: str = Field(..., min_length=1, max_length=128)
    source_prose_run_revision: StrictInt = Field(..., ge=0)
    source_content_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_local_projection(
        self,
    ) -> "ValidatedChapterOutlineAdherenceEvidenceSchema":
        counts: dict[str, int] = {}
        for evidence in self.beat_evidence:
            counts[evidence.status] = counts.get(evidence.status, 0) + 1
        if self.beat_status_counts != counts:
            raise ValueError("beat_status_counts does not match beat_evidence")
        if self.verdict == "pass" and (self.issues or self.findings):
            raise ValueError("passing V2 review cannot contain deviations")
        if self.verdict == "pass" and any(
            item.status != "covered" for item in self.scene_coverage
        ):
            raise ValueError("passing V2 review must cover every scene")
        if self.verdict == "fail" and not (self.issues or self.findings):
            raise ValueError("failed V2 review requires a local deviation")
        return self
