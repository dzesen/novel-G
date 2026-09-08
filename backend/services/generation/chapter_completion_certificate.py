"""Canonical local completion decision and certificate for AI chapter prose.

Provider output is evidence only.  This module owns the closed local decision,
issues a certificate only for a passing decision, and replays the same
deterministic assessment immediately before a formal mutation intent is frozen.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from backend.services.generation.chapter_repair_policy import (
    RepairFailureEvidenceV1,
)
from backend.services.generation.chapter_review_policy import ChapterReviewCoverage


CHAPTER_COMPLETION_DECISION_SCHEMA = "chapter_completion_decision.v2"
CHAPTER_COMPLETION_FAILURE_FACT_SCHEMA = (
    "chapter_completion_failure_fact.v1"
)
CHAPTER_COMPLETION_FAILURE_EVIDENCE_SCHEMA = (
    "chapter_completion_failure_evidence.v1"
)
CHAPTER_COMPLETION_CERTIFICATE_SCHEMA = "chapter_completion_certificate.v2"
CHAPTER_COMPLETION_VERIFICATION_SCHEMA = (
    "chapter_completion_certificate_verification.v2"
)
LEGACY_CHAPTER_COMPLETION_POLICY_REVISION = "chapter_completion_policy.v2"
CHAPTER_COMPLETION_POLICY_REVISION = "chapter_completion_policy.v3"
SELECTIVE_CHAPTER_COMPLETION_POLICY_REVISION = "chapter_completion_policy.v4"
SUPPORTED_CHAPTER_COMPLETION_POLICY_REVISIONS = frozenset({
    LEGACY_CHAPTER_COMPLETION_POLICY_REVISION,
    CHAPTER_COMPLETION_POLICY_REVISION,
    SELECTIVE_CHAPTER_COMPLETION_POLICY_REVISION,
})
ChapterCompletionPolicyRevision = Literal[
    "chapter_completion_policy.v2",
    "chapter_completion_policy.v3",
    "chapter_completion_policy.v4",
]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_OBJECT_ID_PATTERN = r"^[0-9a-f]{24}$"

Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]
ObjectIdText = Annotated[str, Field(pattern=_OBJECT_ID_PATTERN)]

CompletionDecisionValue = Literal["pass", "repair", "manual_review"]
QualityDebtStatus = Literal["evaluated", "not_run", "not_applicable"]
FailureClass = Literal[
    "incomplete_prose",
    "stale_source",
    "invalid_or_unknown_evidence",
    "semantic_unknown",
    "scene_contract_violation",
    "unaccounted_canonical_fact",
    "invalid_internal_reference",
    "repair_not_converged",
    "repair_budget_exhausted",
    "authorization_stale",
    "uncertain_paid_attempt",
    "publication_conflict",
    "legacy_completion_unproven",
]
FailureAssessmentStage = Literal[
    "authorization",
    "chapter_binding",
    "outline_adherence",
    "state_proposal",
    "state_mutation",
    "completion_gates",
    "policy_assessment",
    "publication_prepare",
    "publication_revalidation",
    "publication_commit",
]
FailureFactReason = Literal[
    "prose_incomplete",
    "source_binding_stale",
    "evidence_invalid",
    "semantic_unknown",
    "scene_contract_unsatisfied",
    "canonical_fact_unaccounted",
    "internal_reference_invalid",
    "repair_not_converged",
    "repair_authority_exhausted",
    "authorization_binding_stale",
    "paid_attempt_unresolved",
    "publication_conflict",
    "legacy_completion_unproven",
]

_FAILURE_CLASS_ORDER: tuple[FailureClass, ...] = (
    "incomplete_prose",
    "stale_source",
    "invalid_or_unknown_evidence",
    "semantic_unknown",
    "scene_contract_violation",
    "unaccounted_canonical_fact",
    "invalid_internal_reference",
    "repair_not_converged",
    "repair_budget_exhausted",
    "authorization_stale",
    "uncertain_paid_attempt",
    "publication_conflict",
    "legacy_completion_unproven",
)
_MANUAL_REVIEW_FAILURES = frozenset({
    "stale_source",
    "invalid_or_unknown_evidence",
    "semantic_unknown",
    "repair_budget_exhausted",
    "authorization_stale",
    "uncertain_paid_attempt",
    "publication_conflict",
    "legacy_completion_unproven",
})


class ChapterCompletionPolicyError(ValueError):
    """The closed V2 completion policy could not prove a valid certificate."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_completion_digest(value: Any) -> str:
    """Return the canonical SHA-256 used by all completion bindings."""

    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_audit_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return value


def _ensure_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class _ClosedCompletionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


_FAILURE_FACT_CLASS: dict[FailureFactReason, FailureClass] = {
    "prose_incomplete": "incomplete_prose",
    "source_binding_stale": "stale_source",
    "evidence_invalid": "invalid_or_unknown_evidence",
    "semantic_unknown": "semantic_unknown",
    "scene_contract_unsatisfied": "scene_contract_violation",
    "canonical_fact_unaccounted": "unaccounted_canonical_fact",
    "internal_reference_invalid": "invalid_internal_reference",
    "repair_not_converged": "repair_not_converged",
    "repair_authority_exhausted": "repair_budget_exhausted",
    "authorization_binding_stale": "authorization_stale",
    "paid_attempt_unresolved": "uncertain_paid_attempt",
    "publication_conflict": "publication_conflict",
    "legacy_completion_unproven": "legacy_completion_unproven",
}
_REPAIR_FAILURE_REASONS = frozenset({
    "repair_not_converged",
    "repair_authority_exhausted",
})
_OPTIONAL_REPAIR_CONTEXT_REASONS = frozenset({
    "evidence_invalid",
    "authorization_binding_stale",
})


class ChapterCompletionFailureFact(_ClosedCompletionModel):
    """One closed local fact; policy, not the caller, classifies its result."""

    schema_version: Literal["chapter_completion_failure_fact.v1"] = (
        "chapter_completion_failure_fact.v1"
    )
    reason: FailureFactReason
    observed_at: FailureAssessmentStage
    repair_failure: RepairFailureEvidenceV1 | None = None

    @model_validator(mode="after")
    def validate_repair_binding(self) -> "ChapterCompletionFailureFact":
        requires_repair = self.reason in _REPAIR_FAILURE_REASONS
        has_repair = self.repair_failure is not None
        if requires_repair and not has_repair:
            raise ValueError("repair failure fact binding is incomplete")
        if (
            has_repair
            and not requires_repair
            and self.reason not in _OPTIONAL_REPAIR_CONTEXT_REASONS
        ):
            raise ValueError("repair failure fact binding is incomplete")
        return self


def classify_chapter_completion_failure(
    fact: ChapterCompletionFailureFact | Mapping[str, Any],
) -> tuple[FailureClass, FailureAssessmentStage]:
    parsed = (
        fact
        if isinstance(fact, ChapterCompletionFailureFact)
        else ChapterCompletionFailureFact.model_validate(fact)
    )
    return _FAILURE_FACT_CLASS[parsed.reason], parsed.observed_at


def failure_fact_from_outline_adherence_decision(
    decision: Any,
) -> ChapterCompletionFailureFact:
    reason: FailureFactReason = (
        "semantic_unknown"
        if decision == "manual_review"
        else "scene_contract_unsatisfied"
        if decision == "repair"
        else "evidence_invalid"
    )
    return ChapterCompletionFailureFact(
        reason=reason,
        observed_at="outline_adherence",
    )


class ChapterBinding(_ClosedCompletionModel):
    owner_id: ObjectIdText
    novel_id: ObjectIdText
    volume_id: ObjectIdText
    chapter_id: ObjectIdText


class ChapterSourceBinding(_ClosedCompletionModel):
    prose_run_id: ObjectIdText
    prose_run_revision: int = Field(ge=1)
    content_digest: Digest
    outline_revision: Digest
    outline_contract_digest: Digest
    expected_narrative_revision_before_commit: int = Field(ge=0)


class ChapterAuthorizationBinding(_ClosedCompletionModel):
    kind: Literal["job_readiness", "interactive_completion_readiness"]
    authorization_id: str = Field(min_length=1, max_length=128)
    authorization_revision: int = Field(ge=1)
    authorization_digest: Digest
    job_id: ObjectIdText | None
    readiness_digest: Digest | None

    @model_validator(mode="after")
    def validate_digest(self) -> "ChapterAuthorizationBinding":
        payload = self.model_dump(mode="json", exclude={"authorization_digest"})
        if canonical_completion_digest(payload) != self.authorization_digest:
            raise ValueError("authorization digest mismatch")
        if self.kind == "job_readiness":
            if self.job_id is None or self.readiness_digest is None:
                raise ValueError("job readiness authorization is incomplete")
        elif self.job_id is not None or self.readiness_digest is None:
            raise ValueError(
                "interactive readiness authorization is incomplete"
            )
        return self


def build_chapter_authorization_binding(
    *,
    kind: Literal["job_readiness", "interactive_completion_readiness"],
    authorization_id: str,
    authorization_revision: int,
    job_id: str | None,
    readiness_digest: str | None,
) -> ChapterAuthorizationBinding:
    payload = {
        "kind": kind,
        "authorization_id": authorization_id,
        "authorization_revision": authorization_revision,
        "job_id": job_id,
        "readiness_digest": readiness_digest,
    }
    return ChapterAuthorizationBinding(
        **payload,
        authorization_digest=canonical_completion_digest(payload),
    )


class ChapterCompletionCandidateSnapshot(_ClosedCompletionModel):
    chapter_binding: ChapterBinding
    source_binding: ChapterSourceBinding
    authorization_binding: ChapterAuthorizationBinding
    evaluated_at: datetime

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def parse_evaluated_at(cls, value: Any) -> Any:
        return _parse_audit_datetime(value)

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, field="evaluated_at")


class ChapterCompletionEvidenceBundle(_ClosedCompletionModel):
    review_coverage: ChapterReviewCoverage | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    provider_attempt_ledger_digest: Digest
    prose_integrity_digest: Digest
    scene_contract_digest: Digest
    beat_evidence_digest: Digest
    local_issue_set_digest: Digest
    state_proposal_id: ObjectIdText
    state_proposal_digest: Digest
    state_fact_accounting_digest: Digest
    repair_trace_digest: Digest | None
    quality_debt_status: QualityDebtStatus
    quality_debt_sidecar_digest: Digest
    prose_integrity_passed: bool
    scene_contract_passed: bool | None
    state_fact_accounting_passed: bool
    repair_convergence: Literal["pass", "not_required", "failed"]
    blocking_issue_signatures: tuple[str, ...] = Field(max_length=200)
    failure_classes: tuple[FailureClass, ...] = Field(max_length=13)
    quality_debt_count: int = Field(ge=0, le=10_000)

    @field_validator(
        "blocking_issue_signatures",
        "failure_classes",
        mode="before",
    )
    @classmethod
    def tupleize_arrays(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_closed_evidence(self) -> "ChapterCompletionEvidenceBundle":
        if any(not item or len(item) > 256 for item in self.blocking_issue_signatures):
            raise ValueError("blocking issue signature is invalid")
        if len(set(self.blocking_issue_signatures)) != len(
            self.blocking_issue_signatures
        ):
            raise ValueError("blocking issue signatures must be unique")
        if len(set(self.failure_classes)) != len(self.failure_classes):
            raise ValueError("failure classes must be unique")
        if self.repair_convergence == "not_required":
            if self.repair_trace_digest is not None:
                raise ValueError("unused repair trace must be null")
        elif self.repair_trace_digest is None:
            raise ValueError("repair convergence requires a repair trace")
        if self.quality_debt_status != "evaluated" and self.quality_debt_count:
            raise ValueError("unevaluated quality debt count must be zero")
        return self


class ChapterCompletionFailureEvidenceBundle(_ClosedCompletionModel):
    """Closed evidence available when evaluation stops before certification.

    A state proposal is deliberately absent here.  Preflight and publication
    failures must still produce a canonical decision without inventing an
    internal ObjectId or pretending that later evidence was collected.
    """

    schema_version: Literal["chapter_completion_failure_evidence.v1"]
    assessment_stage: FailureAssessmentStage
    provider_attempt_ledger_digest: Digest
    available_evidence_digest: Digest
    blocking_issue_signatures: tuple[str, ...] = Field(
        min_length=1,
        max_length=200,
    )
    failure_classes: tuple[FailureClass, ...] = Field(
        min_length=1,
        max_length=13,
    )
    quality_debt_status: QualityDebtStatus
    quality_debt_sidecar_digest: Digest

    @field_validator(
        "blocking_issue_signatures",
        "failure_classes",
        mode="before",
    )
    @classmethod
    def tupleize_arrays(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_closed_failure(self) -> "ChapterCompletionFailureEvidenceBundle":
        if any(
            not item or len(item) > 256
            for item in self.blocking_issue_signatures
        ):
            raise ValueError("blocking issue signature is invalid")
        if len(set(self.blocking_issue_signatures)) != len(
            self.blocking_issue_signatures
        ):
            raise ValueError("blocking issue signatures must be unique")
        if len(set(self.failure_classes)) != len(self.failure_classes):
            raise ValueError("failure classes must be unique")
        return self


class ChapterCompletionDecision(_ClosedCompletionModel):
    schema_version: Literal["chapter_completion_decision.v2"]
    decision_id: Digest
    decision_digest: Digest
    decision: CompletionDecisionValue
    policy_revision: ChapterCompletionPolicyRevision
    source_binding_digest: Digest
    evidence_bundle_digest: Digest
    blocking_issue_signatures: tuple[str, ...] = Field(max_length=200)
    failure_classes: tuple[FailureClass, ...] = Field(max_length=13)
    quality_debt_status: QualityDebtStatus
    quality_debt_digest: Digest
    evaluated_at: datetime

    @field_validator(
        "blocking_issue_signatures",
        "failure_classes",
        mode="before",
    )
    @classmethod
    def tupleize_arrays(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("evaluated_at", mode="before")
    @classmethod
    def parse_evaluated_at(cls, value: Any) -> Any:
        return _parse_audit_datetime(value)

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, field="evaluated_at")

    @model_validator(mode="after")
    def validate_identity(self) -> "ChapterCompletionDecision":
        payload = self.model_dump(
            mode="json",
            exclude={"decision_id", "decision_digest", "evaluated_at"},
        )
        expected_digest = canonical_completion_digest(payload)
        if self.decision_digest != expected_digest:
            raise ValueError("completion decision digest mismatch")
        expected_id = canonical_completion_digest({
            "schema_version": self.schema_version,
            "decision_digest": expected_digest,
        })
        if self.decision_id != expected_id:
            raise ValueError("completion decision identity mismatch")
        return self


class ChapterCompletionEvidenceBinding(_ClosedCompletionModel):
    review_coverage: ChapterReviewCoverage | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    provider_attempt_ledger_digest: Digest
    prose_integrity_digest: Digest
    scene_contract_digest: Digest
    beat_evidence_digest: Digest
    local_issue_set_digest: Digest
    state_proposal_id: ObjectIdText
    state_proposal_digest: Digest
    state_fact_accounting_digest: Digest
    repair_trace_digest: Digest | None
    quality_debt_status: QualityDebtStatus
    quality_debt_sidecar_digest: Digest


class ChapterCompletionGateResults(_ClosedCompletionModel):
    prose_integrity: Literal["pass"]
    scene_contract: Literal["pass", "not_reviewed"]
    state_fact_accounting: Literal["pass"]
    repair_convergence: Literal["pass", "not_required"]
    policy_result: Literal["pass"]
    quality_debt_count: int = Field(ge=0, le=10_000)


class ChapterCompletionCertificate(_ClosedCompletionModel):
    schema_version: Literal["chapter_completion_certificate.v2"]
    certificate_id: Digest
    certificate_digest: Digest
    issued_at: datetime
    issuer: Literal["local_completion_policy"]
    policy_revision: ChapterCompletionPolicyRevision
    decision_id: Digest
    decision_digest: Digest
    source_binding_digest: Digest
    evidence_bundle_digest: Digest
    chapter_binding: ChapterBinding
    source_binding: ChapterSourceBinding
    authorization_binding: ChapterAuthorizationBinding
    evidence_binding: ChapterCompletionEvidenceBinding
    gate_results: ChapterCompletionGateResults
    policy_result: Literal["pass"]
    quality_debt_count: int = Field(ge=0, le=10_000)

    @field_validator("issued_at", mode="before")
    @classmethod
    def parse_issued_at(cls, value: Any) -> Any:
        return _parse_audit_datetime(value)

    @field_validator("issued_at")
    @classmethod
    def validate_issued_at(cls, value: datetime) -> datetime:
        return _ensure_aware(value, field="issued_at")

    @model_validator(mode="after")
    def validate_certificate(self) -> "ChapterCompletionCertificate":
        coverage = self.evidence_binding.review_coverage
        selective = self.policy_revision == SELECTIVE_CHAPTER_COMPLETION_POLICY_REVISION
        if selective != (coverage is not None):
            raise ValueError("completion certificate review policy is inconsistent")
        expected_gate = (
            "not_reviewed" if coverage is not None and coverage.status == "not_reviewed"
            else "pass"
        )
        if self.gate_results.scene_contract != expected_gate:
            raise ValueError("completion certificate misrepresents review coverage")
        if coverage is not None and coverage.prose_repaired and self.evidence_binding.repair_trace_digest is None:
            raise ValueError("completion certificate repaired review lacks its repair trace")
        payload = self.model_dump(mode="json", exclude={"certificate_digest"})
        if canonical_completion_digest(payload) != self.certificate_digest:
            raise ValueError("completion certificate digest mismatch")
        identity = {
            "source_binding_digest": self.source_binding_digest,
            "authorization_digest": (
                self.authorization_binding.authorization_digest
            ),
            "decision_digest": self.decision_digest,
            "evidence_bundle_digest": self.evidence_bundle_digest,
            "policy_revision": self.policy_revision,
        }
        if canonical_completion_digest(identity) != self.certificate_id:
            raise ValueError("completion certificate identity mismatch")
        expected_source = canonical_completion_digest({
            "chapter_binding": self.chapter_binding,
            "source_binding": self.source_binding,
        })
        if expected_source != self.source_binding_digest:
            raise ValueError("completion certificate source binding mismatch")
        expected_decision_id = canonical_completion_digest({
            "schema_version": CHAPTER_COMPLETION_DECISION_SCHEMA,
            "decision_digest": self.decision_digest,
        })
        if self.decision_id != expected_decision_id:
            raise ValueError("completion certificate decision identity mismatch")
        if self.gate_results.quality_debt_count != self.quality_debt_count:
            raise ValueError("completion certificate quality debt mismatch")
        if (
            self.evidence_binding.quality_debt_status != "evaluated"
            and self.quality_debt_count != 0
        ):
            raise ValueError(
                "completion certificate unevaluated quality debt is nonzero"
            )
        if self.gate_results.repair_convergence == "not_required":
            if self.evidence_binding.repair_trace_digest is not None:
                raise ValueError(
                    "completion certificate has an unused repair trace"
                )
        elif self.evidence_binding.repair_trace_digest is None:
            raise ValueError(
                "completion certificate repair pass lacks its trace"
            )
        return self


class ChapterCompletionCurrentSnapshot(_ClosedCompletionModel):
    candidate_snapshot: ChapterCompletionCandidateSnapshot
    evidence_bundle: ChapterCompletionEvidenceBundle


class CertificateVerification(_ClosedCompletionModel):
    schema_version: Literal[
        "chapter_completion_certificate_verification.v2"
    ]
    valid: Literal[True]
    certificate_id: Digest
    certificate_digest: Digest
    source_binding_digest: Digest
    evidence_bundle_digest: Digest


def verify_persisted_chapter_completion_certificate(
    chapter: Mapping[str, Any],
) -> CertificateVerification:
    """Verify the deterministic storage side of one already committed chapter."""

    acceptance = chapter.get("prose_acceptance")
    if not isinstance(acceptance, Mapping):
        raise ChapterCompletionPolicyError(
            "chapter completion acceptance is missing"
        )
    raw_certificate = acceptance.get("chapter_completion_certificate")
    try:
        certificate = ChapterCompletionCertificate.model_validate(
            raw_certificate
        )
    except ValidationError as exc:
        raise ChapterCompletionPolicyError(
            "chapter completion certificate schema is invalid"
        ) from exc
    content_digest = hashlib.sha256(
        str(chapter.get("content") or "").encode("utf-8")
    ).hexdigest()
    binding = certificate.chapter_binding
    source = certificate.source_binding
    if (
        str(chapter.get("_id") or "") != binding.chapter_id
        or str(chapter.get("novel_id") or "") != binding.novel_id
        or str(chapter.get("volume_id") or "") != binding.volume_id
        or acceptance.get("state") != "ai_complete"
        or acceptance.get("content_origin") != "ai"
        or str(acceptance.get("source_run_id") or "")
        != source.prose_run_id
        or str(acceptance.get("content_digest") or "") != content_digest
        or source.content_digest != content_digest
    ):
        raise ChapterCompletionPolicyError(
            "chapter completion certificate storage binding is stale"
        )
    receipt = acceptance.get("completion_receipt")
    expected_receipt = {
        "schema_version": "chapter_completion_receipt.v2",
        "certificate_id": certificate.certificate_id,
        "certificate_digest": certificate.certificate_digest,
        "narrative_revision_before": (
            source.expected_narrative_revision_before_commit
        ),
        "narrative_revision_after": (
            source.expected_narrative_revision_before_commit + 1
        ),
    }
    if receipt != expected_receipt:
        raise ChapterCompletionPolicyError(
            "chapter completion receipt is missing or stale"
        )
    return CertificateVerification(
        schema_version=CHAPTER_COMPLETION_VERIFICATION_SCHEMA,
        valid=True,
        certificate_id=certificate.certificate_id,
        certificate_digest=certificate.certificate_digest,
        source_binding_digest=certificate.source_binding_digest,
        evidence_bundle_digest=certificate.evidence_bundle_digest,
    )


class ChapterCompletionPolicy:
    """Small public interface over the complete local completion policy."""

    def __init__(self) -> None:
        self._issuance_context: dict[
            str,
            tuple[
                ChapterCompletionCandidateSnapshot,
                ChapterCompletionEvidenceBundle,
                ChapterCompletionDecision,
            ],
        ] = {}

    @staticmethod
    def _parse_candidate(
        value: ChapterCompletionCandidateSnapshot | Mapping[str, Any],
    ) -> ChapterCompletionCandidateSnapshot:
        try:
            return (
                value
                if isinstance(value, ChapterCompletionCandidateSnapshot)
                else ChapterCompletionCandidateSnapshot.model_validate(value)
            )
        except ValidationError as exc:
            raise ChapterCompletionPolicyError(
                "chapter completion candidate schema is invalid"
            ) from exc

    @staticmethod
    def _parse_evidence(
        value: (
            ChapterCompletionEvidenceBundle
            | ChapterCompletionFailureEvidenceBundle
            | Mapping[str, Any]
        ),
    ) -> ChapterCompletionEvidenceBundle | ChapterCompletionFailureEvidenceBundle:
        try:
            if isinstance(
                value,
                (ChapterCompletionEvidenceBundle, ChapterCompletionFailureEvidenceBundle),
            ):
                return value
            if value.get("schema_version") == (
                CHAPTER_COMPLETION_FAILURE_EVIDENCE_SCHEMA
            ):
                return ChapterCompletionFailureEvidenceBundle.model_validate(value)
            return ChapterCompletionEvidenceBundle.model_validate(value)
        except ValidationError as exc:
            raise ChapterCompletionPolicyError(
                "chapter completion evidence schema is invalid"
            ) from exc

    def assess(
        self,
        candidate_snapshot: ChapterCompletionCandidateSnapshot | Mapping[str, Any],
        evidence_bundle: (
            ChapterCompletionEvidenceBundle
            | ChapterCompletionFailureEvidenceBundle
            | Mapping[str, Any]
        ),
        policy_revision: str,
    ) -> ChapterCompletionDecision:
        if policy_revision not in SUPPORTED_CHAPTER_COMPLETION_POLICY_REVISIONS:
            raise ChapterCompletionPolicyError(
                "unknown chapter completion policy revision"
            )
        candidate = self._parse_candidate(candidate_snapshot)
        evidence = self._parse_evidence(evidence_bundle)
        failures = set(evidence.failure_classes)
        if isinstance(evidence, ChapterCompletionEvidenceBundle):
            coverage = evidence.review_coverage
            selective = policy_revision == SELECTIVE_CHAPTER_COMPLETION_POLICY_REVISION
            if selective != (coverage is not None):
                raise ChapterCompletionPolicyError("completion review policy evidence is missing or unexpected")
            not_reviewed = coverage is not None and coverage.status == "not_reviewed"
            if not_reviewed and evidence.scene_contract_passed is not None:
                raise ChapterCompletionPolicyError("unreviewed prose cannot carry a semantic gate result")
            if coverage is not None and coverage.prose_repaired and evidence.repair_trace_digest is None:
                raise ChapterCompletionPolicyError("repaired review requires its repair trace")
            if not evidence.prose_integrity_passed:
                failures.add("incomplete_prose")
            if not not_reviewed and evidence.scene_contract_passed is not True:
                failures.add("scene_contract_violation")
            if not evidence.state_fact_accounting_passed:
                failures.add("unaccounted_canonical_fact")
            if evidence.repair_convergence == "failed":
                failures.add("repair_not_converged")
        ordered_failures = tuple(
            failure for failure in _FAILURE_CLASS_ORDER if failure in failures
        )
        signatures = tuple(sorted(set(evidence.blocking_issue_signatures)))
        if not ordered_failures and not signatures:
            decision_value: CompletionDecisionValue = "pass"
        elif any(failure in _MANUAL_REVIEW_FAILURES for failure in failures):
            decision_value = "manual_review"
        else:
            decision_value = "repair"
        source_binding_digest = canonical_completion_digest({
            "chapter_binding": candidate.chapter_binding,
            "source_binding": candidate.source_binding,
        })
        evidence_bundle_digest = canonical_completion_digest(evidence)
        decision_payload = {
            "schema_version": CHAPTER_COMPLETION_DECISION_SCHEMA,
            "decision": decision_value,
            "policy_revision": policy_revision,
            "source_binding_digest": source_binding_digest,
            "evidence_bundle_digest": evidence_bundle_digest,
            "blocking_issue_signatures": signatures,
            "failure_classes": ordered_failures,
            "quality_debt_status": evidence.quality_debt_status,
            "quality_debt_digest": evidence.quality_debt_sidecar_digest,
        }
        decision_digest = canonical_completion_digest(decision_payload)
        decision = ChapterCompletionDecision(
            **decision_payload,
            decision_id=canonical_completion_digest({
                "schema_version": CHAPTER_COMPLETION_DECISION_SCHEMA,
                "decision_digest": decision_digest,
            }),
            decision_digest=decision_digest,
            evaluated_at=candidate.evaluated_at,
        )
        if isinstance(evidence, ChapterCompletionEvidenceBundle):
            self._issuance_context[decision.decision_id] = (
                candidate,
                evidence,
                decision,
            )
        return decision

    def issue(
        self,
        decision: ChapterCompletionDecision | Mapping[str, Any],
    ) -> ChapterCompletionCertificate:
        try:
            parsed = (
                decision
                if isinstance(decision, ChapterCompletionDecision)
                else ChapterCompletionDecision.model_validate(decision)
            )
        except ValidationError as exc:
            raise ChapterCompletionPolicyError(
                "chapter completion decision schema is invalid"
            ) from exc
        if parsed.decision != "pass":
            raise ChapterCompletionPolicyError(
                "chapter completion certificate requires a pass decision"
            )
        context = self._issuance_context.get(parsed.decision_id)
        if context is None or context[2] != parsed:
            raise ChapterCompletionPolicyError(
                "chapter completion decision has no current assessment context"
            )
        candidate, evidence, _stored = context
        evidence_binding = ChapterCompletionEvidenceBinding(
            review_coverage=evidence.review_coverage,
            provider_attempt_ledger_digest=(
                evidence.provider_attempt_ledger_digest
            ),
            prose_integrity_digest=evidence.prose_integrity_digest,
            scene_contract_digest=evidence.scene_contract_digest,
            beat_evidence_digest=evidence.beat_evidence_digest,
            local_issue_set_digest=evidence.local_issue_set_digest,
            state_proposal_id=evidence.state_proposal_id,
            state_proposal_digest=evidence.state_proposal_digest,
            state_fact_accounting_digest=(
                evidence.state_fact_accounting_digest
            ),
            repair_trace_digest=evidence.repair_trace_digest,
            quality_debt_status=evidence.quality_debt_status,
            quality_debt_sidecar_digest=(
                evidence.quality_debt_sidecar_digest
            ),
        )
        gate_results = ChapterCompletionGateResults(
            prose_integrity="pass",
            scene_contract=(
                "not_reviewed" if evidence.review_coverage is not None
                and evidence.review_coverage.status == "not_reviewed" else "pass"
            ),
            state_fact_accounting="pass",
            repair_convergence=evidence.repair_convergence,
            policy_result="pass",
            quality_debt_count=evidence.quality_debt_count,
        )
        identity = {
            "source_binding_digest": parsed.source_binding_digest,
            "authorization_digest": (
                candidate.authorization_binding.authorization_digest
            ),
            "decision_digest": parsed.decision_digest,
            "evidence_bundle_digest": parsed.evidence_bundle_digest,
            "policy_revision": parsed.policy_revision,
        }
        payload = {
            "schema_version": CHAPTER_COMPLETION_CERTIFICATE_SCHEMA,
            "certificate_id": canonical_completion_digest(identity),
            "issued_at": parsed.evaluated_at,
            "issuer": "local_completion_policy",
            "policy_revision": parsed.policy_revision,
            "decision_id": parsed.decision_id,
            "decision_digest": parsed.decision_digest,
            "source_binding_digest": parsed.source_binding_digest,
            "evidence_bundle_digest": parsed.evidence_bundle_digest,
            "chapter_binding": candidate.chapter_binding,
            "source_binding": candidate.source_binding,
            "authorization_binding": candidate.authorization_binding,
            "evidence_binding": evidence_binding,
            "gate_results": gate_results,
            "policy_result": "pass",
            "quality_debt_count": evidence.quality_debt_count,
        }
        return ChapterCompletionCertificate(
            **payload,
            certificate_digest=canonical_completion_digest(payload),
        )

    def verify(
        self,
        certificate: ChapterCompletionCertificate | Mapping[str, Any],
        current_snapshot: ChapterCompletionCurrentSnapshot | Mapping[str, Any],
    ) -> CertificateVerification:
        try:
            parsed_certificate = (
                certificate
                if isinstance(certificate, ChapterCompletionCertificate)
                else ChapterCompletionCertificate.model_validate(certificate)
            )
        except ValidationError as exc:
            errors = repr(exc).lower()
            detail = (
                "chapter completion certificate digest is invalid"
                if "digest mismatch" in errors
                else "chapter completion certificate schema is invalid"
            )
            raise ChapterCompletionPolicyError(detail) from exc
        try:
            current = (
                current_snapshot
                if isinstance(current_snapshot, ChapterCompletionCurrentSnapshot)
                else ChapterCompletionCurrentSnapshot.model_validate(
                    current_snapshot
                )
            )
        except ValidationError as exc:
            raise ChapterCompletionPolicyError(
                "chapter completion current snapshot schema is invalid"
            ) from exc
        fresh_decision = self.assess(
            current.candidate_snapshot,
            current.evidence_bundle,
            parsed_certificate.policy_revision,
        )
        if fresh_decision.decision != "pass":
            raise ChapterCompletionPolicyError(
                "chapter completion current snapshot no longer passes"
            )
        fresh_certificate = self.issue(fresh_decision)
        if fresh_certificate != parsed_certificate:
            raise ChapterCompletionPolicyError(
                "chapter completion certificate no longer matches current snapshot"
            )
        return CertificateVerification(
            schema_version=CHAPTER_COMPLETION_VERIFICATION_SCHEMA,
            valid=True,
            certificate_id=parsed_certificate.certificate_id,
            certificate_digest=parsed_certificate.certificate_digest,
            source_binding_digest=parsed_certificate.source_binding_digest,
            evidence_bundle_digest=parsed_certificate.evidence_bundle_digest,
        )
