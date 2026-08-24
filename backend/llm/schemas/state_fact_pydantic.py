"""Provider and locally validated contracts for chapter-state fact evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)

from backend.llm.schemas.scene_contract_pydantic import (
    ProseEvidenceSpanSchema,
    ValidatedProseEvidenceSpanSchema,
)
from backend.state_fact_contract_versions import (
    STATE_FACT_ACCOUNTING_VERSION,
    STATE_FACT_EVIDENCE_VERSION,
)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


StableFactIdentity = Annotated[
    str,
    Field(min_length=1, max_length=96, pattern=r"^[a-z0-9][a-z0-9._:-]*$"),
]
StateFactKind = Literal[
    "canonical_fact",
    "character_cognition",
    "rumor",
    "deception",
    "temporary_fact",
]
StateFactSupport = Literal["supported", "unsupported", "unknown"]
StateFactTargetType = Literal["character", "thread", "chapter"]
StateFactActionType = Literal[
    "chapter_summary",
    "character_state",
    "permanent_fact",
    "thread_status",
]
StateFactDropReason = Literal[
    "duplicate_existing_fact",
    "duplicate_proposal",
    "legal_no_op",
    "character_cognition",
    "rumor",
    "deception",
    "temporary_fact",
    "unsupported_by_prose",
]
StateFactManualDropReason = Literal[
    "duplicate_existing_fact",
    "duplicate_proposal",
    "legal_no_op",
    "unsupported_by_prose",
]
StateFactReasonCode = Literal[
    "accepted_chapter_summary",
    "accepted_character_state",
    "accepted_permanent_fact",
    "accepted_thread_update",
    "duplicate_existing_fact",
    "duplicate_proposal",
    "legal_no_op",
    "character_cognition",
    "rumor",
    "deception",
    "temporary_fact",
    "unsupported_by_prose",
    "unaccounted",
]


class StateFactActionRefSchema(BaseModel):
    """Provider reference to one exact proposed state action."""

    model_config = ConfigDict(extra="forbid")

    action_type: StateFactActionType
    target_id: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1, max_length=1000)
    permanent_fact_kind: Literal[
        "death", "injury", "identity", "relation", "ability"
    ] | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> "StateFactActionRefSchema":
        if (self.action_type == "permanent_fact") != (
            self.permanent_fact_kind is not None
        ):
            raise ValueError(
                "permanent_fact_kind is required only for permanent facts"
            )
        return self


class StateFactEvidenceItemSchema(BaseModel):
    """One Provider-observed fact, independent from whether it will be stored."""

    model_config = ConfigDict(extra="forbid")

    fact_id: StableFactIdentity
    kind: StateFactKind
    support: StateFactSupport
    statement: str = Field(..., min_length=1, max_length=500)
    target_type: StateFactTargetType
    target_id: str | None = Field(default=None, min_length=1, max_length=128)
    action_ref: StateFactActionRefSchema | None = None
    spans: list[ProseEvidenceSpanSchema] = Field(
        default_factory=list,
        max_length=2,
    )
    explanation: str = Field(..., min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_fact_shape(self) -> "StateFactEvidenceItemSchema":
        if self.target_type == "chapter":
            if self.target_id is not None:
                raise ValueError("chapter fact target_id is assigned locally")
        elif self.target_id is None:
            raise ValueError("character and thread facts require target_id")
        if self.support != "unknown" and not self.spans:
            raise ValueError("supported or unsupported facts require prose spans")
        if self.action_ref is not None:
            if (
                self.target_type != "chapter"
                and self.action_ref.target_id != self.target_id
            ):
                raise ValueError("fact target and action target diverged")
            expected_target = {
                "chapter_summary": "chapter",
                "character_state": "character",
                "permanent_fact": "character",
                "thread_status": "thread",
            }[self.action_ref.action_type]
            if expected_target != self.target_type:
                raise ValueError("fact target type and action type diverged")
        return self


class StateNoChangeEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spans: list[ProseEvidenceSpanSchema] = Field(..., min_length=1, max_length=2)
    explanation: str = Field(..., min_length=1, max_length=500)


class ChapterStateFactEvidenceSchema(BaseModel):
    """Provider-only evidence; it never decides whether formal writes are safe."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chapter_state_fact_evidence.v1"]
    extraction_status: Literal["complete", "complete_no_change", "unknown"]
    facts: list[StateFactEvidenceItemSchema] = Field(
        default_factory=list,
        max_length=100,
    )
    no_change: StateNoChangeEvidenceSchema | None = None
    unknown_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_extraction_state(self) -> "ChapterStateFactEvidenceSchema":
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("state fact ids must be unique")
        if self.extraction_status == "complete":
            if not self.facts or self.no_change is not None or self.unknown_reason:
                raise ValueError("complete extraction requires facts only")
        elif self.extraction_status == "complete_no_change":
            if self.facts or self.no_change is None or self.unknown_reason:
                raise ValueError("complete_no_change requires no-change evidence only")
        elif self.no_change is not None or not self.unknown_reason:
            raise ValueError("unknown extraction requires an unknown_reason")
        return self


class StateFactSourceBindingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    chapter_id: str = Field(..., min_length=1, max_length=128)
    source_prose_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_prose_run_revision: StrictInt | None = Field(default=None, ge=0)
    source_content_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_run_pair(self) -> "StateFactSourceBindingSchema":
        if (self.source_prose_run_id is None) != (
            self.source_prose_run_revision is None
        ):
            raise ValueError("state fact prose run identity is incomplete")
        return self


class ValidatedStateFactEvidenceItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fact_id: StableFactIdentity
    fact_signature: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    kind: StateFactKind
    support: StateFactSupport
    statement_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    target_type: StateFactTargetType
    target_id: str = Field(..., min_length=1, max_length=128)
    action_ids: tuple[str, ...] = Field(default=(), max_length=100)
    spans: tuple[ValidatedProseEvidenceSpanSchema, ...] = Field(
        default=(),
        max_length=2,
    )
    explanation: str = Field(..., min_length=1, max_length=500)


class ValidatedStateNoChangeEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action_ids: tuple[str, ...] = Field(default=(), max_length=10)
    spans: tuple[ValidatedProseEvidenceSpanSchema, ...] = Field(
        ...,
        min_length=1,
        max_length=2,
    )
    explanation: str = Field(..., min_length=1, max_length=500)


class ValidatedChapterStateFactEvidenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_schema_version: Literal["chapter_state_fact_evidence.v1"]
    extraction_status: Literal["complete", "complete_no_change", "unknown"]
    facts: tuple[ValidatedStateFactEvidenceItemSchema, ...] = Field(
        default=(),
        max_length=100,
    )
    no_change: ValidatedStateNoChangeEvidenceSchema | None = None
    unknown_reason: str | None = Field(default=None, min_length=1, max_length=500)
    source_binding: StateFactSourceBindingSchema
    invalid_internal_references: StrictInt = Field(ge=0, le=1000)
    dangling_references: StrictInt = Field(ge=0, le=1000)
    evidence_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> "ValidatedChapterStateFactEvidenceSchema":
        expected_digest = _canonical_digest(
            self.model_dump(mode="json", exclude={"evidence_digest"})
        )
        if self.evidence_digest != expected_digest:
            raise ValueError("state fact evidence digest diverged")
        return self


class StateFactAccountSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fact_id: StableFactIdentity
    fact_signature: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    kind: StateFactKind
    support: StateFactSupport
    target_type: StateFactTargetType
    target_id: str = Field(..., min_length=1, max_length=128)
    action_ids: tuple[str, ...] = Field(default=(), max_length=100)
    selected_action_ids: tuple[str, ...] = Field(default=(), max_length=100)
    reason_code: StateFactReasonCode
    spans: tuple[ValidatedProseEvidenceSpanSchema, ...] = Field(
        default=(),
        max_length=2,
    )


class StateFactActionAccountSchema(BaseModel):
    """One immutable decision record for one exact proposed state action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action_id: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    fact_id: StableFactIdentity
    fact_signature: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    action_type: StateFactActionType
    target_id: str = Field(..., min_length=1, max_length=128)
    decision: Literal["accepted", "dropped"]
    reason_code: StateFactReasonCode
    spans: tuple[ValidatedProseEvidenceSpanSchema, ...] = Field(
        default=(),
        max_length=2,
    )

    @model_validator(mode="after")
    def validate_decision_reason(self) -> "StateFactActionAccountSchema":
        accepted = self.reason_code in {
            "accepted_chapter_summary",
            "accepted_character_state",
            "accepted_permanent_fact",
            "accepted_thread_update",
        }
        if accepted != (self.decision == "accepted"):
            raise ValueError("state action decision and reason diverged")
        return self


class StateNoChangeAccountSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reason_code: Literal["legal_no_op"]
    action_ids: tuple[str, ...] = Field(default=(), max_length=10)
    spans: tuple[ValidatedProseEvidenceSpanSchema, ...] = Field(
        ...,
        min_length=1,
        max_length=2,
    )


class StateFactAccountingSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["chapter_state_fact_accounting.v1"]
    evidence_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    source_binding: StateFactSourceBindingSchema
    extraction_status: Literal["complete", "complete_no_change", "unknown"]
    accounts: tuple[StateFactAccountSchema, ...] = Field(default=(), max_length=100)
    action_accounts: tuple[StateFactActionAccountSchema, ...] = Field(
        default=(),
        max_length=1000,
    )
    no_change_account: StateNoChangeAccountSchema | None = None
    canonical_fact_count: StrictInt = Field(ge=0, le=1000)
    accounted_canonical_fact_count: StrictInt = Field(ge=0, le=1000)
    unaccounted_canonical_facts: StrictInt = Field(ge=0, le=1000)
    invalid_internal_references: StrictInt = Field(ge=0, le=1000)
    dangling_references: StrictInt = Field(ge=0, le=1000)
    extraction_failure_count: StrictInt = Field(ge=0, le=1)
    accepted_action_count: StrictInt = Field(ge=0, le=1000)
    dropped_action_count: StrictInt = Field(ge=0, le=1000)
    gate_passed: bool
    accounting_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_projection(self) -> "StateFactAccountingSchema":
        if self.accounted_canonical_fact_count > self.canonical_fact_count:
            raise ValueError("accounted canonical fact count exceeds total")
        expected_unaccounted = (
            self.canonical_fact_count
            - self.accounted_canonical_fact_count
            + self.extraction_failure_count
        )
        if self.unaccounted_canonical_facts != expected_unaccounted:
            raise ValueError("unaccounted canonical fact count diverged")
        action_ids = [item.action_id for item in self.action_accounts]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("state action accounting contains duplicate actions")
        accepted_actions = sum(
            item.decision == "accepted" for item in self.action_accounts
        )
        dropped_actions = sum(
            item.decision == "dropped" and item.reason_code != "unaccounted"
            for item in self.action_accounts
        )
        if (
            accepted_actions != self.accepted_action_count
            or dropped_actions != self.dropped_action_count
        ):
            raise ValueError("state action accounting counts diverged")
        expected_gate = (
            self.unaccounted_canonical_facts == 0
            and self.invalid_internal_references == 0
            and self.dangling_references == 0
        )
        if self.gate_passed != expected_gate:
            raise ValueError("state fact gate projection diverged")
        digest_payload = self.model_dump(
            mode="json",
            exclude={"accounting_digest"},
        )
        expected_digest = _canonical_digest(digest_payload)
        if self.accounting_digest != expected_digest:
            raise ValueError("state fact accounting digest diverged")
        return self
