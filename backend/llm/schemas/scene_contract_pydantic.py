"""Versioned scene-transition contracts used by chapter outlines."""

from __future__ import annotations

import hashlib
import json
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
MAX_V3_LOCAL_ADHERENCE_ISSUES = 80


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
ContractReferenceIdentity = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$",
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


class OutlineQualityDimensionSchema(BaseModel):
    """One uncalibrated literary observation; never a hard gate by itself."""

    model_config = ConfigDict(extra="forbid")

    observation_id: StableSceneIdentity
    dimension: Literal[
        "interest",
        "pacing",
        "character_drive",
        "style",
        "novelty",
        "narrative_function_repetition",
    ]
    status: Literal["concern", "strength"]
    scene_id: StableSceneIdentity | None = None
    spans: list[ProseEvidenceSpanSchema] = Field(
        ...,
        min_length=1,
        max_length=2,
    )
    explanation: RequiredText160


class OutlineSemanticUnknownSchema(BaseModel):
    """A semantic contract question the Provider could not resolve."""

    model_config = ConfigDict(extra="forbid")

    unknown_id: StableSceneIdentity
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
    scene_id: StableSceneIdentity | None = None
    beat_ids: list[StableSceneIdentity] = Field(default_factory=list, max_length=10)
    spans: list[ProseEvidenceSpanSchema] = Field(default_factory=list, max_length=2)
    explanation: RequiredText160


class OutlineContractFindingV3Schema(OutlineContractFindingSchema):
    """A finding with machine-verifiable references for objective blockers."""

    condition_ids: list[StableSceneIdentity] = Field(
        default_factory=list,
        max_length=10,
    )
    event_key: CanonicalEventKey | None = None

    @model_validator(mode="after")
    def validate_objective_references(self) -> "OutlineContractFindingV3Schema":
        if self.category == "forbidden_condition":
            if self.scene_id is None or not self.condition_ids or self.event_key:
                raise ValueError(
                    "forbidden_condition requires scene_id and condition_ids"
                )
        elif self.category == "event_repetition":
            if self.scene_id is None or self.event_key is None or self.condition_ids:
                raise ValueError(
                    "event_repetition requires scene_id and event_key"
                )
        elif self.condition_ids or self.event_key is not None:
            raise ValueError(
                "objective contract references are only valid for objective findings"
            )
        if len(self.condition_ids) != len(set(self.condition_ids)):
            raise ValueError("condition_ids must be unique")
        return self


class ChapterOutlineAdherenceEvidenceV3Schema(BaseModel):
    """Provider evidence only; verdict and severity are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chapter_outline_adherence_evidence.v3"]
    outline_contract_version: Literal["scene_transition_contract.v2"]
    summary: RequiredText500
    beat_evidence: list[BeatEvidenceSchema] = Field(..., min_length=1, max_length=400)
    findings: list[OutlineContractFindingV3Schema] = Field(
        default_factory=list,
        max_length=20,
    )
    quality_dimensions: list[OutlineQualityDimensionSchema] = Field(
        default_factory=list,
        max_length=10,
    )
    unknowns: list[OutlineSemanticUnknownSchema] = Field(
        default_factory=list,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_provider_identities(
        self,
    ) -> "ChapterOutlineAdherenceEvidenceV3Schema":
        identity_groups = (
            [finding.finding_id for finding in self.findings],
            [item.observation_id for item in self.quality_dimensions],
            [item.unknown_id for item in self.unknowns],
        )
        if any(len(values) != len(set(values)) for values in identity_groups):
            raise ValueError("Provider evidence identities must be unique by kind")
        return self


class SceneEventFingerprintSchema(BaseModel):
    """Provider observation used only to recall possible event repetition."""

    model_config = ConfigDict(extra="forbid")

    actor_role: RequiredText160
    action: RequiredText160
    object_role: RequiredText160
    outcome: RequiredText160


class SceneNarrativeFunctionSchema(BaseModel):
    """Seven bounded dimensions used to compare narrative function."""

    model_config = ConfigDict(extra="forbid")

    goal: RequiredText160
    conflict: RequiredText160
    turn: RequiredText160
    outcome: RequiredText160
    new_information: RequiredText160
    character_change: RequiredText160
    stakes_delta: RequiredText160


class SceneQualityProfileSchema(BaseModel):
    """One scene profile; it carries observations, never a local decision."""

    model_config = ConfigDict(extra="forbid")

    profile_id: StableSceneIdentity
    scene_id: StableSceneIdentity
    representative_spans: list[ProseEvidenceSpanSchema] = Field(
        ...,
        min_length=1,
        max_length=2,
    )
    event_fingerprint: SceneEventFingerprintSchema
    narrative_function: SceneNarrativeFunctionSchema


class ChapterOutlineAdherenceEvidenceV4Schema(BaseModel):
    """V3 semantic evidence plus per-scene non-blocking quality profiles."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chapter_outline_adherence_evidence.v4"]
    outline_contract_version: Literal["scene_transition_contract.v2"]
    summary: RequiredText500
    beat_evidence: list[BeatEvidenceSchema] = Field(..., min_length=1, max_length=400)
    findings: list[OutlineContractFindingV3Schema] = Field(
        default_factory=list,
        max_length=20,
    )
    quality_dimensions: list[OutlineQualityDimensionSchema] = Field(
        default_factory=list,
        max_length=10,
    )
    unknowns: list[OutlineSemanticUnknownSchema] = Field(
        default_factory=list,
        max_length=10,
    )
    scene_quality_profiles: list[SceneQualityProfileSchema] = Field(
        ...,
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_provider_identities(
        self,
    ) -> "ChapterOutlineAdherenceEvidenceV4Schema":
        identity_groups = (
            [finding.finding_id for finding in self.findings],
            [item.observation_id for item in self.quality_dimensions],
            [item.unknown_id for item in self.unknowns],
            [item.profile_id for item in self.scene_quality_profiles],
            [item.scene_id for item in self.scene_quality_profiles],
        )
        if any(len(values) != len(set(values)) for values in identity_groups):
            raise ValueError("Provider evidence identities must be unique by kind")
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


class ValidatedOutlineQualityDimensionSchema(OutlineQualityDimensionSchema):
    spans: list[ValidatedProseEvidenceSpanSchema] = Field(
        ...,
        min_length=1,
        max_length=2,
    )


class ValidatedSceneQualityProfileSchema(SceneQualityProfileSchema):
    representative_spans: list[ValidatedProseEvidenceSpanSchema] = Field(
        ...,
        min_length=1,
        max_length=2,
    )


QualitySignalMatchedDimension = Literal[
    "actor_role",
    "action",
    "object_role",
    "outcome",
    "goal",
    "conflict",
    "turn",
    "new_information",
    "character_change",
    "stakes_delta",
]


class NarrativeQualitySignalLayerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "literal_similarity",
        "event_fingerprint",
        "narrative_function",
    ]
    score_basis_points: StrictInt = Field(..., ge=0, le=10_000)
    matched_dimensions: list[QualitySignalMatchedDimension] = Field(
        default_factory=list,
        max_length=7,
    )

    @model_validator(mode="after")
    def validate_layer_evidence(self) -> "NarrativeQualitySignalLayerSchema":
        if len(self.matched_dimensions) != len(set(self.matched_dimensions)):
            raise ValueError("quality signal dimensions must be unique")
        if self.kind == "literal_similarity":
            if self.matched_dimensions or self.score_basis_points < 8_000:
                raise ValueError("literal quality signal is invalid")
        elif self.kind == "event_fingerprint":
            if self.matched_dimensions != [
                "actor_role",
                "action",
                "object_role",
                "outcome",
            ] or self.score_basis_points != 10_000:
                raise ValueError("event fingerprint quality signal is invalid")
        elif (
            len(self.matched_dimensions) < 5
            or self.score_basis_points < 8_500
        ):
            raise ValueError("narrative function quality signal is invalid")
        return self


class NarrativeRepetitionCandidateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_ids: list[StableSceneIdentity] = Field(..., min_length=2, max_length=2)
    layers: list[NarrativeQualitySignalLayerSchema] = Field(
        ...,
        min_length=1,
        max_length=3,
    )
    severity: Literal["quality_debt"]
    hard_gate: Literal[False]
    contract_reference_ids: list[ContractReferenceIdentity] = Field(
        default_factory=list,
        max_length=0,
    )

    @model_validator(mode="after")
    def validate_candidate(self) -> "NarrativeRepetitionCandidateSchema":
        if len(set(self.scene_ids)) != 2:
            raise ValueError("quality candidate scenes must be distinct")
        kinds = [layer.kind for layer in self.layers]
        expected_order = {
            "literal_similarity": 0,
            "event_fingerprint": 1,
            "narrative_function": 2,
        }
        if len(kinds) != len(set(kinds)) or kinds != sorted(
            kinds,
            key=expected_order.__getitem__,
        ):
            raise ValueError("quality candidate layers are invalid")
        return self


class NarrativeQualitySourceBindingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_prose_run_id: str = Field(..., min_length=1, max_length=128)
    source_prose_run_revision: StrictInt = Field(..., ge=0)
    source_content_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    outline_contract_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class NarrativeQualityDetectionScopeSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["current_chapter_scene_pairs"]
    scene_ids: list[StableSceneIdentity] = Field(..., min_length=1, max_length=20)
    scene_count: StrictInt = Field(..., ge=1, le=20)
    comparison_pair_count: StrictInt = Field(..., ge=0, le=190)
    layers: list[
        Literal[
            "literal_similarity",
            "event_fingerprint",
            "narrative_function",
        ]
    ] = Field(..., min_length=3, max_length=3)
    maximum_candidates: Literal[20]
    additional_provider_calls: Literal[0]
    additional_token_bound: Literal[0]
    second_judge_enabled: Literal[False]

    @model_validator(mode="after")
    def validate_scope(self) -> "NarrativeQualityDetectionScopeSchema":
        if (
            len(self.scene_ids) != len(set(self.scene_ids))
            or self.scene_count != len(self.scene_ids)
            or self.comparison_pair_count
            != self.scene_count * (self.scene_count - 1) // 2
            or self.layers
            != [
                "literal_similarity",
                "event_fingerprint",
                "narrative_function",
            ]
        ):
            raise ValueError("quality signal detection scope is invalid")
        return self


class NarrativeQualitySidecarSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chapter_narrative_quality_sidecar.v1"]
    policy_version: Literal["narrative_repetition_signal_policy.v1"]
    status: Literal["evaluated"]
    source_binding: NarrativeQualitySourceBindingSchema
    detection_scope: NarrativeQualityDetectionScopeSchema
    scene_profiles_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    candidate_count: StrictInt = Field(..., ge=0, le=20)
    truncated_candidate_count: StrictInt = Field(..., ge=0, le=190)
    candidates: list[NarrativeRepetitionCandidateSchema] = Field(
        default_factory=list,
        max_length=20,
    )
    sidecar_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_sidecar(self) -> "NarrativeQualitySidecarSchema":
        signatures = [item.candidate_signature for item in self.candidates]
        scene_order = {
            scene_id: index
            for index, scene_id in enumerate(self.detection_scope.scene_ids)
        }
        scene_pairs: list[tuple[str, str]] = []
        for candidate in self.candidates:
            pair = (candidate.scene_ids[0], candidate.scene_ids[1])
            if (
                any(scene_id not in scene_order for scene_id in pair)
                or scene_order[pair[0]] >= scene_order[pair[1]]
            ):
                raise ValueError("quality candidate scene scope changed")
            expected_signature = hashlib.sha256(
                json.dumps(
                    {
                        "policy_version": self.policy_version,
                        "scene_ids": list(pair),
                        "layers": [layer.kind for layer in candidate.layers],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if candidate.candidate_signature != expected_signature:
                raise ValueError("quality candidate signature changed")
            scene_pairs.append(pair)
        if (
            self.candidate_count != len(self.candidates)
            or len(signatures) != len(set(signatures))
            or len(scene_pairs) != len(set(scene_pairs))
            or self.candidate_count + self.truncated_candidate_count
            > self.detection_scope.comparison_pair_count
        ):
            raise ValueError("quality sidecar candidate projection is invalid")
        expected_order = sorted(
            self.candidates,
            key=lambda candidate: (
                -max(
                    layer.score_basis_points
                    for layer in candidate.layers
                ),
                tuple(candidate.scene_ids),
            ),
        )
        if self.candidates != expected_order:
            raise ValueError("quality sidecar candidate order changed")
        payload = self.model_dump(mode="json", exclude={"sidecar_digest"})
        expected_digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if self.sidecar_digest != expected_digest:
            raise ValueError("quality sidecar digest changed")
        return self


class ValidatedOutlineContractFindingV3Schema(OutlineContractFindingV3Schema):
    spans: list[ValidatedProseEvidenceSpanSchema] = Field(
        default_factory=list,
        max_length=2,
    )


class ValidatedOutlineSemanticUnknownSchema(OutlineSemanticUnknownSchema):
    spans: list[ValidatedProseEvidenceSpanSchema] = Field(
        default_factory=list,
        max_length=2,
    )


class LocalOutlineAdherenceIssueSchema(BaseModel):
    """Compact deterministic issue projection derived from Provider evidence."""

    model_config = ConfigDict(extra="forbid")

    issue_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    severity: Literal["blocker", "major", "quality_debt", "unknown", "info"]
    category: Literal[
        "scene_coverage",
        "scene_order",
        "core_conflict",
        "ending_hook",
        "unplanned_major_event",
        "volume_arc",
        "forbidden_condition",
        "event_repetition",
        "interest",
        "pacing",
        "character_drive",
        "style",
        "novelty",
        "narrative_function_repetition",
    ]
    source_kind: Literal[
        "beat_evidence",
        "finding",
        "quality_dimension",
        "quality_signal",
        "unknown",
    ]
    scene_id: StableSceneIdentity | None = None
    source_evidence_count: StrictInt = Field(..., ge=1, le=20)
    contract_reference_ids: list[ContractReferenceIdentity] = Field(
        default_factory=list,
        max_length=10,
    )


class ValidatedChapterOutlineAdherenceEvidenceV3Schema(BaseModel):
    """Locally verified V3 evidence plus the fixed V1 issue policy result."""

    model_config = ConfigDict(extra="forbid")

    evidence_schema_version: Literal["chapter_outline_adherence_evidence.v3"]
    issue_policy_version: Literal["chapter_outline_issue_policy.v1"]
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
    findings: list[ValidatedOutlineContractFindingV3Schema] = Field(
        default_factory=list,
        max_length=20,
    )
    quality_dimensions: list[ValidatedOutlineQualityDimensionSchema] = Field(
        default_factory=list,
        max_length=10,
    )
    unknowns: list[ValidatedOutlineSemanticUnknownSchema] = Field(
        default_factory=list,
        max_length=10,
    )
    local_issues: list[LocalOutlineAdherenceIssueSchema] = Field(
        default_factory=list,
        max_length=MAX_V3_LOCAL_ADHERENCE_ISSUES,
    )
    local_issue_counts: dict[
        Literal["blocker", "major", "quality_debt", "unknown", "info"],
        StrictInt,
    ]
    decision: Literal["pass", "repair", "manual_review"]
    scene_coverage: list[ValidatedOutlineSceneCoverageSchema] = Field(
        ...,
        min_length=1,
        max_length=100,
    )
    source_prose_run_id: str = Field(..., min_length=1, max_length=128)
    source_prose_run_revision: StrictInt = Field(..., ge=0)
    source_content_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_local_issue_policy(
        self,
    ) -> "ValidatedChapterOutlineAdherenceEvidenceV3Schema":
        beat_counts: dict[str, int] = {}
        for evidence in self.beat_evidence:
            beat_counts[evidence.status] = beat_counts.get(evidence.status, 0) + 1
        if self.beat_status_counts != beat_counts:
            raise ValueError("beat_status_counts does not match beat_evidence")

        issue_counts: dict[str, int] = {}
        signatures: list[str] = []
        quality_categories = {
            "interest",
            "pacing",
            "character_drive",
            "style",
            "novelty",
            "narrative_function_repetition",
        }
        for issue in self.local_issues:
            issue_counts[issue.severity] = issue_counts.get(issue.severity, 0) + 1
            signatures.append(issue.issue_signature)
            if issue.category in quality_categories and issue.severity not in {
                "quality_debt",
                "info",
            }:
                raise ValueError("uncalibrated quality cannot become a hard issue")
            if issue.source_kind in {
                "quality_dimension",
                "quality_signal",
            } and issue.category not in quality_categories:
                raise ValueError("quality issue category is invalid")
            if (
                issue.source_kind == "quality_signal"
                and (
                    issue.category != "narrative_function_repetition"
                    or issue.severity != "quality_debt"
                )
            ):
                raise ValueError("quality signal cannot change the hard gate")
            if issue.category in {"forbidden_condition", "event_repetition"}:
                if issue.source_kind == "finding" and not issue.contract_reference_ids:
                    raise ValueError("objective issue requires contract references")
            elif issue.contract_reference_ids:
                raise ValueError("non-objective issue cannot claim contract references")
        if self.local_issue_counts != issue_counts:
            raise ValueError("local_issue_counts does not match local_issues")
        if len(signatures) != len(set(signatures)):
            raise ValueError("local issue signatures must be unique")

        severities = set(issue_counts)
        expected_decision = (
            "manual_review"
            if "unknown" in severities
            else "repair"
            if severities & {"blocker", "major"}
            else "pass"
        )
        if self.decision != expected_decision:
            raise ValueError("decision diverges from local issue policy")
        if self.decision == "pass" and any(
            item.status != "covered" for item in self.scene_coverage
        ):
            raise ValueError("passing V3 review must cover every required scene")
        return self


class ValidatedChapterOutlineAdherenceEvidenceV4Schema(
    ValidatedChapterOutlineAdherenceEvidenceV3Schema
):
    """Current local projection with a source-bound quality-debt sidecar."""

    evidence_schema_version: Literal["chapter_outline_adherence_evidence.v4"]
    issue_policy_version: Literal["chapter_outline_issue_policy.v2"]
    scene_quality_profiles: list[ValidatedSceneQualityProfileSchema] = Field(
        ...,
        min_length=1,
        max_length=20,
    )
    quality_debt_sidecar: NarrativeQualitySidecarSchema

    @model_validator(mode="after")
    def validate_quality_sidecar_binding(
        self,
    ) -> "ValidatedChapterOutlineAdherenceEvidenceV4Schema":
        sidecar = self.quality_debt_sidecar
        profile_scene_ids = [item.scene_id for item in self.scene_quality_profiles]
        if profile_scene_ids != sidecar.detection_scope.scene_ids:
            raise ValueError("quality sidecar scene scope changed")
        binding = sidecar.source_binding
        if (
            binding.source_prose_run_id != self.source_prose_run_id
            or binding.source_prose_run_revision
            != self.source_prose_run_revision
            or binding.source_content_digest != self.source_content_digest
            or binding.outline_contract_digest != self.outline_contract_digest
        ):
            raise ValueError("quality sidecar source binding changed")
        profile_payload = [
            item.model_dump(mode="json") for item in self.scene_quality_profiles
        ]
        expected_profiles_digest = hashlib.sha256(
            json.dumps(
                profile_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if sidecar.scene_profiles_digest != expected_profiles_digest:
            raise ValueError("quality sidecar profile digest changed")
        candidate_signatures = [
            item.candidate_signature for item in sidecar.candidates
        ]
        quality_issues = [
            item for item in self.local_issues if item.source_kind == "quality_signal"
        ]
        if (
            [item.issue_signature for item in quality_issues]
            != candidate_signatures
            or any(
                item.source_evidence_count != len(candidate.layers)
                for item, candidate in zip(
                    quality_issues,
                    sidecar.candidates,
                    strict=True,
                )
            )
        ):
            raise ValueError("quality sidecar issues changed")
        return self
