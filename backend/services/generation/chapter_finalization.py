"""受批量 readiness 授权的正文 + 状态单一正式提交边界。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from backend.db.mutation import (
    MutationCommand,
    MutationConflictError,
    MutationRecorder,
    commit_mutation,
)
from backend.db.narrative_revision import NarrativeRevisionConflict
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.generation_job_repository import (
    ChapterCompletionDecisionFence,
    generation_job_repo,
)
from backend.llm.schemas.scene_contract_pydantic import (
    ValidatedChapterOutlineAdherenceEvidenceV4Schema,
)
from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES,
    AdherenceCandidateCheckpointV5,
    JobMutationRecoveryBindingV1,
    parse_candidate_pipeline_checkpoint,
)
from backend.services.generation.chapter_completion_certificate import (
    CHAPTER_COMPLETION_POLICY_REVISION,
    CHAPTER_COMPLETION_FAILURE_EVIDENCE_SCHEMA,
    ChapterBinding,
    ChapterCompletionCandidateSnapshot,
    ChapterCompletionCertificate,
    ChapterCompletionCurrentSnapshot,
    ChapterCompletionDecision,
    ChapterCompletionEvidenceBundle,
    ChapterCompletionFailureFact,
    ChapterCompletionFailureEvidenceBundle,
    ChapterCompletionPolicy,
    ChapterCompletionPolicyError,
    ChapterSourceBinding,
    FailureClass,
    FailureAssessmentStage,
    FailureFactReason,
    build_chapter_authorization_binding,
    canonical_completion_digest,
    classify_chapter_completion_failure,
    failure_fact_from_outline_adherence_decision,
    verify_persisted_chapter_completion_certificate,
)
from backend.services.generation.chapter_repair_policy import (
    RepairFailureEvidenceV1,
)
from backend.services.generation.outline_adherence import (
    OutlineAdherenceValidationError,
    revalidate_current_outline_adherence_evidence,
    validate_complete_outline_adherence,
)
from backend.services.generation.prose_runs import (
    ProseRunModule,
    prose_revision,
    prose_run_module,
)
from backend.services.generation.prose_completion_contract import (
    completion_allows_formal_write,
)
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.derived_stats import derived_stats
from backend.services.novel.state_proposal import (
    FactAccountingPolicy,
    StateProposalCompletionFailure,
    StateProposalCompletionFailureReason,
    StaleStatePreview,
    state_proposal_module,
)
from backend.services.novel.state_fact_accounting import (
    StateFactAccountingError,
    validate_state_fact_accounting,
)


FINALIZATION_AUTHORIZATION_SCHEMA = "chapter_finalization_authorization.v2"
FINALIZE_CHAPTER_GENERATION_COMMAND_VERSION = 2
FINALIZATION_CHANGE_CLASSES = ("chapter_prose", "chapter_state")
DEFAULT_FINALIZATION_REPAIR_CYCLES = 2
MAX_FINALIZATION_REPAIR_CYCLES = MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES


def _chapter_outline_contract_digest(outline: Mapping[str, Any]) -> str:
    """Hash persisted outlines after normalizing BSON identifiers to strings."""

    return prose_revision(dict(outline))


class ChapterFinalizationDenied(ValueError):
    """候选、闸门或批量作业授权不允许正式提交。"""


class _CompletionGateDenied(ChapterFinalizationDenied):
    def __init__(
        self,
        message: str,
        failure_fact: ChapterCompletionFailureFact,
    ) -> None:
        super().__init__(message)
        if not isinstance(failure_fact, ChapterCompletionFailureFact):
            raise TypeError("chapter completion failure fact is required")
        self.failure_fact = failure_fact


def _failure_fact(
    reason: FailureFactReason,
    observed_at: FailureAssessmentStage,
    *,
    repair_failure: RepairFailureEvidenceV1 | None = None,
) -> ChapterCompletionFailureFact:
    return ChapterCompletionFailureFact(
        reason=reason,
        observed_at=observed_at,
        repair_failure=repair_failure,
    )


def _retarget_failure_fact(
    fact: ChapterCompletionFailureFact,
    observed_at: FailureAssessmentStage,
) -> ChapterCompletionFailureFact:
    return _failure_fact(
        fact.reason,
        observed_at,
        repair_failure=fact.repair_failure,
    )


_STATE_PROPOSAL_FAILURE_REASONS: dict[
    StateProposalCompletionFailureReason,
    FailureFactReason,
] = {
    "canonical_fact_unaccounted": "canonical_fact_unaccounted",
    "evidence_invalid": "evidence_invalid",
    "internal_reference_invalid": "internal_reference_invalid",
    "semantic_unknown": "semantic_unknown",
    "source_binding_stale": "source_binding_stale",
}


def _state_proposal_failure_fact(
    failure: StateProposalCompletionFailure,
) -> ChapterCompletionFailureFact:
    return _failure_fact(
        _STATE_PROPOSAL_FAILURE_REASONS[
            failure.completion_failure_reason
        ],
        "state_proposal",
    )


def _required_finalization_authority(
    job: Mapping[str, Any],
) -> Any | None:
    """Return the exact formal successor authority, or fail on a partial mode."""

    readiness = job.get("readiness")
    planning = (
        readiness.get("planning")
        if isinstance(readiness, Mapping)
        else None
    )
    if not isinstance(planning, Mapping):
        return None
    from backend.services.generation.required_chapter_finalization_job import (
        required_chapter_finalization_planning_present,
        validate_required_chapter_finalization_readiness,
    )

    if not required_chapter_finalization_planning_present(planning):
        return None
    try:
        return validate_required_chapter_finalization_readiness(readiness)
    except ValueError as exc:
        raise ChapterFinalizationDenied(
            "章节正式 successor readiness 无效"
        ) from exc


def _trusted_outline_adherence_evidence(
    *,
    job: Mapping[str, Any],
    chapter_id: str,
) -> ValidatedChapterOutlineAdherenceEvidenceV4Schema:
    """Load the exact server-persisted V4 evidence for this finalization."""

    raw_trusted: Any = None
    successor = _required_finalization_authority(job)
    if successor is not None:
        if successor.chapter_id != str(chapter_id):
            raise _CompletionGateDenied(
                "章节正式 successor 没有绑定当前章节",
                _failure_fact("evidence_invalid", "outline_adherence"),
            )
        raw_trusted = successor.outline_adherence
    interactive = job.get("interactive_completion_evidence")
    if (
        raw_trusted is None
        and isinstance(interactive, Mapping)
        and "outline_adherence" in interactive
    ):
        raw_trusted = interactive.get("outline_adherence")
    if raw_trusted is None:
        raw_checkpoints = job.get("candidate_pipeline_checkpoints")
        if isinstance(raw_checkpoints, list):
            latest: AdherenceCandidateCheckpointV5 | None = None
            try:
                for raw_checkpoint in raw_checkpoints:
                    checkpoint = parse_candidate_pipeline_checkpoint(
                        raw_checkpoint
                    )
                    if (
                        isinstance(checkpoint, AdherenceCandidateCheckpointV5)
                        and checkpoint.chapter_id == chapter_id
                    ):
                        latest = checkpoint
            except ValueError as exc:
                raise _CompletionGateDenied(
                    "作业中的章纲符合度检查点无效",
                    _failure_fact("evidence_invalid", "outline_adherence"),
                ) from exc
            if latest is not None:
                raw_trusted = latest.validated_evidence.model_dump(
                    mode="python"
                )
    try:
        return ValidatedChapterOutlineAdherenceEvidenceV4Schema.model_validate(
            raw_trusted
        )
    except ValueError as exc:
        raise _CompletionGateDenied(
            "作业缺少当前章纲符合度的持久证据",
            _failure_fact("evidence_invalid", "outline_adherence"),
        ) from exc


class ChapterFinalizationAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal[
        "job_readiness",
        "interactive_completion_readiness",
    ] = "job_readiness"
    authorization_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    job_id: str = Field(min_length=1)
    readiness_digest: str = Field(min_length=1)
    authorization_revision: int = Field(ge=1)
    execution_claim_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_kind(self) -> "ChapterFinalizationAuthorization":
        if (
            self.kind == "interactive_completion_readiness"
            and self.authorization_id is None
        ):
            raise ValueError(
                "interactive completion authorization identity is required"
            )
        if (
            self.kind == "job_readiness"
            and self.execution_claim_token is not None
        ):
            raise ValueError("batch finalization cannot carry an execution claim")
        return self


class ChapterFinalizationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    outline_adherence: Mapping[str, Any]
    repair_cycles_used: int = Field(default=0, ge=0)
    repair_trace: Mapping[str, Any] | None = None


def chapter_finalization_idempotency_key(
    *,
    prose_run_id: str,
    prose_run_revision: int,
    state_proposal_id: str,
) -> str:
    if not str(prose_run_id or "") or not str(state_proposal_id or ""):
        raise ValueError("chapter finalization identity is incomplete")
    if (
        isinstance(prose_run_revision, bool)
        or not isinstance(prose_run_revision, int)
        or prose_run_revision < 0
    ):
        raise ValueError("chapter finalization prose revision is invalid")
    return (
        f"finalize-chapter-generation:{prose_run_id}:"
        f"{prose_run_revision}:{state_proposal_id}"
    )


def _strict_int(
    value: Any,
    *,
    field: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ChapterFinalizationDenied(f"{field} 不是有效的冻结整数")
    return value


def build_chapter_finalization_authorization(
    *,
    authorization_revision: int,
    max_repair_cycles: int = DEFAULT_FINALIZATION_REPAIR_CYCLES,
) -> dict[str, Any]:
    """Build the closed authority later consumed by the journal finalizer."""
    if (
        isinstance(authorization_revision, bool)
        or not isinstance(authorization_revision, int)
        or authorization_revision < 1
    ):
        raise ValueError("authorization revision must be a positive integer")
    if (
        isinstance(max_repair_cycles, bool)
        or not isinstance(max_repair_cycles, int)
        or max_repair_cycles < 0
        or max_repair_cycles > MAX_FINALIZATION_REPAIR_CYCLES
    ):
        raise ValueError(
            "repair cycles must be an integer between 0 and "
            f"{MAX_FINALIZATION_REPAIR_CYCLES}"
        )
    return {
        "schema_version": FINALIZATION_AUTHORIZATION_SCHEMA,
        "change_classes": list(FINALIZATION_CHANGE_CLASSES),
        "authorization_revision": authorization_revision,
        "max_repair_cycles": max_repair_cycles,
    }


def parse_chapter_finalization_authorization(
    value: Any,
) -> dict[str, Any]:
    """Revalidate one persisted finalization authority without coercion."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "change_classes",
        "authorization_revision",
        "max_repair_cycles",
    }:
        raise ChapterFinalizationDenied("批量作业没有有效的正式提交授权")
    if value.get("schema_version") != FINALIZATION_AUTHORIZATION_SCHEMA:
        raise ChapterFinalizationDenied("批量作业没有正式提交授权")
    change_classes = value.get("change_classes")
    if (
        not isinstance(change_classes, list)
        or change_classes != list(FINALIZATION_CHANGE_CLASSES)
    ):
        raise ChapterFinalizationDenied("正式提交授权范围不完整")
    authorization_revision = _strict_int(
        value.get("authorization_revision"),
        field="正式提交授权版本",
        minimum=1,
    )
    max_repair_cycles = _strict_int(
        value.get("max_repair_cycles"),
        field="正文修复次数上限",
        maximum=MAX_FINALIZATION_REPAIR_CYCLES,
    )
    return build_chapter_finalization_authorization(
        authorization_revision=authorization_revision,
        max_repair_cycles=max_repair_cycles,
    )


@dataclass(frozen=True)
class ChapterFinalizationDeps:
    job_repo: Any = generation_job_repo
    chapter_repo: Any = chapter_repo
    prose_runs: Any = prose_run_module
    state_proposals: Any = state_proposal_module
    state_service: Any = ChapterStateService


@dataclass(frozen=True)
class _CompletionDecisionContext:
    owner_id: str
    novel_id: str
    chapter_id: str
    prose_run_id: str
    prose_run_revision: int
    candidate_digest: str
    expected_narrative_revision: int
    candidate_text: str
    completion: Mapping[str, Any]
    run: Mapping[str, Any]
    chapter: Mapping[str, Any]
    authorization: ChapterFinalizationAuthorization


def _serialize_subcommand(command: MutationCommand) -> dict[str, Any]:
    return {
        "operation": command.operation,
        "version": command.version,
        "payload": deepcopy(command.payload),
        "before_image": deepcopy(command.before_image),
        "child_ids": deepcopy(command.child_ids),
    }


def _aware_snapshot_time(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ChapterFinalizationDenied("正文候选缺少有效的冻结审计时间")
    return value.astimezone(UTC)


def _is_completion_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _bounded_failure_code(value: Any, *, maximum: int = 128) -> str | None:
    return (
        value
        if isinstance(value, str) and 0 < len(value) <= maximum
        else None
    )


def _validated_failure_adherence(
    adherence: Mapping[str, Any] | None,
    *,
    context: _CompletionDecisionContext,
) -> dict[str, Any] | None:
    if not isinstance(adherence, Mapping):
        return None
    outline = context.chapter.get("outline")
    if not isinstance(outline, Mapping):
        return None
    try:
        return revalidate_current_outline_adherence_evidence(
            adherence,
            outline=outline,
            prose=context.candidate_text,
            source_prose_run_id=context.prose_run_id,
            source_prose_run_revision=context.prose_run_revision,
            source_content_digest=context.candidate_digest,
        )
    except OutlineAdherenceValidationError:
        return None


def _reconcile_outline_failure_fact(
    failure_fact: ChapterCompletionFailureFact,
    adherence: Mapping[str, Any] | None,
) -> ChapterCompletionFailureFact:
    if (
        failure_fact.observed_at != "outline_adherence"
        or failure_fact.reason
        not in {
            "evidence_invalid",
            "semantic_unknown",
            "scene_contract_unsatisfied",
        }
    ):
        return failure_fact
    reason: FailureFactReason = "evidence_invalid"
    if isinstance(adherence, Mapping):
        decision = adherence.get("decision")
        if decision == "manual_review":
            reason = "semantic_unknown"
        elif decision == "repair":
            reason = "scene_contract_unsatisfied"
    return _failure_fact(reason, "outline_adherence")


def _failure_quality_binding(
    adherence: Mapping[str, Any] | None,
    *,
    assessment_stage: FailureAssessmentStage,
) -> tuple[Literal["evaluated", "not_run", "not_applicable"], str]:
    if isinstance(adherence, Mapping):
        sidecar = adherence.get("quality_debt_sidecar")
        digest = (
            sidecar.get("sidecar_digest")
            if isinstance(sidecar, Mapping)
            else None
        )
        if _is_completion_digest(digest):
            return "evaluated", str(digest)
    return "not_run", canonical_completion_digest({
        "schema_version": "chapter_quality_debt_not_run.v1",
        "assessment_stage": assessment_stage,
    })


def _failure_adherence_projection(
    adherence: Mapping[str, Any] | None,
    *,
    quality_debt_status: str,
    quality_debt_sidecar_digest: str,
) -> dict[str, Any] | None:
    if not isinstance(adherence, Mapping):
        return None
    source = adherence
    local_issues: Any = _local_issue_projection(source)
    return {
        "evidence_schema_version": _bounded_failure_code(
            source.get("evidence_schema_version")
        ),
        "issue_policy_version": _bounded_failure_code(
            source.get("issue_policy_version")
        ),
        "decision": (
            source.get("decision")
            if source.get("decision") in {"pass", "repair", "manual_review"}
            else None
        ),
        "source_prose_run_id": _bounded_failure_code(
            source.get("source_prose_run_id")
        ),
        "source_prose_run_revision": (
            source.get("source_prose_run_revision")
            if type(source.get("source_prose_run_revision")) is int
            else None
        ),
        "source_content_digest": (
            source.get("source_content_digest")
            if _is_completion_digest(source.get("source_content_digest"))
            else None
        ),
        "outline_contract_digest": (
            source.get("outline_contract_digest")
            if _is_completion_digest(source.get("outline_contract_digest"))
            else None
        ),
        "beat_evidence_count": (
            len(source["beat_evidence"])
            if isinstance(source.get("beat_evidence"), list)
            else None
        ),
        "finding_count": (
            len(source["findings"])
            if isinstance(source.get("findings"), list)
            else None
        ),
        "unknown_count": (
            len(source["unknowns"])
            if isinstance(source.get("unknowns"), list)
            else None
        ),
        "local_issues": local_issues,
        "quality_debt_status": quality_debt_status,
        "quality_debt_sidecar_digest": quality_debt_sidecar_digest,
    }


def _failure_state_projection(
    *,
    state_proposal_id: str | None,
    state_command: MutationCommand | None,
    proposal_claim: Mapping[str, Any] | None,
    state_fact_accounting: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if (
        state_proposal_id is None
        and state_command is None
        and proposal_claim is None
        and state_fact_accounting is None
    ):
        return None
    state_completion: Mapping[str, Any] = {}
    if state_command is not None:
        acceptance = state_command.payload.get("acceptance_metadata")
        if isinstance(acceptance, Mapping):
            raw_completion = acceptance.get("state_completion")
            if isinstance(raw_completion, Mapping):
                state_completion = raw_completion
    command_accounting = state_completion.get("fact_accounting")
    accounting = (
        command_accounting
        if isinstance(command_accounting, Mapping)
        else (
            state_fact_accounting
            if isinstance(state_fact_accounting, Mapping)
            else {}
        )
    )
    claim = proposal_claim if isinstance(proposal_claim, Mapping) else {}
    return {
        "state_proposal_id": state_proposal_id,
        "state_proposal_digest": claim.get("candidate_digest"),
        "expected_narrative_revision": claim.get(
            "expected_narrative_revision"
        ),
        "fact_accounting_digest": accounting.get("accounting_digest"),
        "unaccounted_canonical_facts": accounting.get(
            "unaccounted_canonical_facts"
        ),
        "invalid_internal_references": accounting.get(
            "invalid_internal_references"
        ),
        "dangling_references": accounting.get("dangling_references"),
        "extraction_failure_count": accounting.get("extraction_failure_count"),
    }


def _provider_attempt_ledger_projection(
    job: Mapping[str, Any],
    *,
    chapter_id: str,
) -> list[dict[str, Any]]:
    """Whitelist bounded attempt metadata; never hash prose or raw responses."""

    successor = _required_finalization_authority(job)
    if successor is not None:
        if successor.chapter_id != str(chapter_id):
            raise ChapterFinalizationDenied(
                "章节正式 successor 付费账本没有绑定当前章节"
            )
        return [
            item.model_dump(mode="json")
            for item in successor.attempt_ledger
        ]

    projected: list[dict[str, Any]] = []
    for raw in list(job.get("attempt_slots") or []):
        if not isinstance(raw, Mapping):
            raise ChapterFinalizationDenied("付费调用账本格式无效")
        if str(raw.get("chapter_id") or "") != str(chapter_id):
            continue
        attempt_id = str(raw.get("attempt_id") or "")
        if not attempt_id or len(attempt_id) > 128:
            raise ChapterFinalizationDenied("付费调用账本身份无效")
        usage = raw.get("usage")
        usage_projection = None
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ChapterFinalizationDenied("付费调用用量证据无效")
            usage_projection = {
                key: usage.get(key)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            }
            if any(
                type(value) is not int or value < 0
                for value in usage_projection.values()
            ):
                raise ChapterFinalizationDenied("付费调用用量证据无效")
        projected.append({
            "attempt_id": attempt_id,
            "chapter_id": str(raw.get("chapter_id") or ""),
            "step_id": str(raw.get("step_id") or ""),
            "phase": str(raw.get("phase") or ""),
            "provider_alias": str(raw.get("provider_alias") or ""),
            "state": str(raw.get("state") or ""),
            "conservative_tokens": raw.get("conservative_tokens"),
            "charged_tokens": raw.get("charged_tokens"),
            "usage": usage_projection,
        })
    return projected


def _local_issue_projection(adherence: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_issues = adherence.get("local_issues") or []
    if not isinstance(raw_issues, list) or len(raw_issues) > 200:
        raise ChapterFinalizationDenied("本地问题集合格式无效")
    projected: list[dict[str, Any]] = []
    for raw in raw_issues:
        if not isinstance(raw, Mapping):
            raise ChapterFinalizationDenied("本地问题集合格式无效")
        signature = str(raw.get("issue_signature") or "")
        if not _is_completion_digest(signature):
            raise ChapterFinalizationDenied("本地问题集合身份无效")
        severity = _bounded_failure_code(raw.get("severity"), maximum=32)
        category = _bounded_failure_code(raw.get("category"), maximum=64)
        source_kind = _bounded_failure_code(
            raw.get("source_kind"),
            maximum=64,
        )
        scene_id = raw.get("scene_id")
        if scene_id is not None:
            scene_id = _bounded_failure_code(scene_id, maximum=128)
            if scene_id is None:
                raise ChapterFinalizationDenied("本地问题场景身份无效")
        raw_references = raw.get("contract_reference_ids") or []
        references = (
            list(raw_references)
            if isinstance(raw_references, (list, tuple))
            else None
        )
        if (
            references is None
            or len(references) > 100
            or any(
                _bounded_failure_code(value, maximum=128) is None
                for value in references
            )
        ):
            raise ChapterFinalizationDenied("本地问题合同引用无效")
        projected.append({
            "issue_signature": signature,
            "severity": severity,
            "category": category,
            "source_kind": source_kind,
            "scene_id": scene_id,
            "contract_reference_ids": references,
        })
    return sorted(projected, key=lambda item: item["issue_signature"])


def _failure_issue_signatures(
    *,
    failure_fact: ChapterCompletionFailureFact,
    failure_class: FailureClass,
    adherence: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    signatures: list[str] = []
    if isinstance(adherence, Mapping):
        signatures.extend(
            item["issue_signature"]
            for item in _local_issue_projection(adherence)
            if item["severity"] in {"blocker", "major", "unknown"}
        )
    repair = failure_fact.repair_failure
    if repair is not None:
        signatures.append(canonical_completion_digest({
            "schema_version": "chapter_completion_repair_failure_issue.v1",
            "failure_class": failure_class,
            "repair_failure": repair.model_dump(mode="json"),
        }))
    if not signatures:
        signatures.append(canonical_completion_digest({
            "schema_version": "chapter_completion_failure_issue.v2",
            "assessment_stage": failure_fact.observed_at,
            "failure_class": failure_class,
        }))
    return tuple(sorted(set(signatures)))


def _failure_provider_attempt_ledger_digest(
    job: Mapping[str, Any],
    *,
    chapter_id: str,
) -> str:
    try:
        projection: Any = _provider_attempt_ledger_projection(
            job,
            chapter_id=chapter_id,
        )
    except ChapterFinalizationDenied:
        raw = job.get("attempt_slots")
        projection = {
            "schema_version": "chapter_attempt_ledger_invalid.v1",
            "status": "invalid",
            "slot_count": len(raw) if isinstance(raw, list) else None,
        }
    return canonical_completion_digest(projection)


def _repair_trace_binding(
    evidence: ChapterFinalizationEvidence,
) -> tuple[str | None, str]:
    if evidence.repair_cycles_used == 0:
        if evidence.repair_trace is not None:
            raise ChapterFinalizationDenied("未发生修复时不得绑定修复轨迹")
        return None, "not_required"
    trace = evidence.repair_trace
    if not isinstance(trace, Mapping) or set(trace) != {
        "schema_version",
        "repair_cycles_used",
        "component_usage",
        "convergence",
        "final_issue_signatures",
        "converged",
    }:
        raise ChapterFinalizationDenied("正文修复缺少闭集收敛轨迹")
    if (
        trace.get("schema_version") != "chapter_repair_trace.v2"
        or trace.get("repair_cycles_used") != evidence.repair_cycles_used
        or trace.get("converged") is not True
        or trace.get("final_issue_signatures") != []
        or not isinstance(trace.get("component_usage"), list)
        or not isinstance(trace.get("convergence"), list)
    ):
        raise ChapterFinalizationDenied("正文修复尚未证明问题集合收敛")
    return canonical_completion_digest(dict(trace)), "pass"


def _completion_receipt(
    certificate: Mapping[str, Any],
    *,
    narrative_revision_after: int,
) -> dict[str, Any]:
    source = certificate.get("source_binding")
    if not isinstance(source, Mapping):
        raise ValueError("章节完成证书来源绑定无效")
    before = source.get("expected_narrative_revision_before_commit")
    if type(before) is not int or narrative_revision_after != before + 1:
        raise ValueError("章节完成证书 narrative revision 回执无效")
    return {
        "schema_version": "chapter_completion_receipt.v2",
        "certificate_id": str(certificate.get("certificate_id") or ""),
        "certificate_digest": str(certificate.get("certificate_digest") or ""),
        "narrative_revision_before": before,
        "narrative_revision_after": narrative_revision_after,
    }


class _SubMutationRecorder:
    """让既有深模块在一个 journal 内使用隔离的回执命名空间。"""

    def __init__(
        self,
        root: MutationRecorder,
        *,
        prefix: str,
        command: Mapping[str, Any],
    ) -> None:
        if not prefix or "." in prefix:
            raise ValueError("mutation receipt prefix must be non-empty and dot-free")
        self._root = root
        self._prefix = f"{prefix}__"
        self._command = deepcopy(dict(command))

    @property
    def journal(self) -> dict[str, Any]:
        root_receipts = self._root.journal.get("receipts") or {}
        receipts = {
            key[len(self._prefix):]: deepcopy(value)
            for key, value in root_receipts.items()
            if key.startswith(self._prefix)
        }
        if "narrative_revision" in root_receipts:
            receipts["narrative_revision"] = deepcopy(
                root_receipts["narrative_revision"]
            )
        return {
            **self._root.journal,
            "command": self._command,
            "receipts": receipts,
        }

    def child_id(self, key: str) -> str:
        return str((self._command.get("child_ids") or {})[key])

    def was_received(self, key: str) -> bool:
        return self._root.was_received(f"{self._prefix}{key}")

    async def advance_phase(self, phase: str) -> None:
        await self._root.advance_phase(phase)

    async def receipt(self, key: str, value: Any) -> None:
        await self._root.receipt(f"{self._prefix}{key}", value)


class ChapterFinalizationService:
    """验证所有候选闸门，并以一个可恢复命令提交正式章节。"""

    def __init__(self, deps: ChapterFinalizationDeps | None = None) -> None:
        self._deps = deps or ChapterFinalizationDeps()

    @staticmethod
    def _assert_completion_decision_scope(
        *,
        job: Mapping[str, Any],
        authorization: ChapterFinalizationAuthorization,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        content_digest: str,
        prose_run: Mapping[str, Any],
    ) -> None:
        if str(job.get("novel_id") or "") != str(novel_id):
            raise ChapterFinalizationDenied(
                "章节完成决定不能写入其他小说的作业"
            )
        scope = str(job.get("scope") or "")
        job_kind = str(job.get("job_kind") or "")
        if authorization.kind == "job_readiness":
            successor = _required_finalization_authority(job)
            if successor is not None:
                if (
                    scope != "book"
                    or job_kind == "interactive_chapter_completion"
                    or str(job.get("owner_id") or "") != str(owner_id)
                    or str(job.get("current_chapter_id") or "")
                    != str(chapter_id)
                    or successor.chapter_id != str(chapter_id)
                    or successor.reviewed_candidate.source_run_id
                    != str(prose_run_id)
                    or successor.reviewed_candidate.source_run_revision
                    != int(prose_run_revision)
                    or successor.reviewed_candidate.source_content_digest
                    != str(content_digest)
                    or str(prose_run.get("generation_job_id") or "")
                    != successor.reviewed_candidate.job_id
                ):
                    raise ChapterFinalizationDenied(
                        "章节正式 successor 不能跨来源作用域写入"
                    )
                return
            if (
                scope not in {"book", "volume"}
                or job_kind == "interactive_chapter_completion"
                or str(prose_run.get("generation_job_id") or "")
                != authorization.job_id
            ):
                raise ChapterFinalizationDenied(
                    "章节完成决定不能跨作业类型写入"
                )
            return
        readiness = job.get("readiness")
        source = (
            readiness.get("source_binding")
            if isinstance(readiness, Mapping)
            else None
        )
        if (
            scope != "interactive_completion"
            or job_kind != "interactive_chapter_completion"
            or str(job.get("owner_id") or "") != str(owner_id)
            or str(job.get("current_chapter_id") or "") != str(chapter_id)
            or not isinstance(readiness, Mapping)
            or readiness.get("schema_version") not in {
                "interactive_chapter_completion_readiness.v1",
                "interactive_chapter_completion_readiness.v2",
            }
            or readiness.get("authorization_id")
            != authorization.authorization_id
            or not isinstance(source, Mapping)
            or str(source.get("owner_id") or "") != str(owner_id)
            or str(source.get("novel_id") or "") != str(novel_id)
            or str(source.get("chapter_id") or "") != str(chapter_id)
            or str(source.get("prose_run_id") or "") != str(prose_run_id)
            or source.get("prose_run_revision") != prose_run_revision
            or str(source.get("content_digest") or "")
            != str(content_digest)
        ):
            raise ChapterFinalizationDenied(
                "交互式章节完成决定不能跨来源作用域写入"
            )

    @staticmethod
    def _candidate_snapshot(
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        content_digest: str,
        expected_narrative_revision: int,
        outline_contract_digest: str,
        run: Mapping[str, Any],
        chapter: Mapping[str, Any],
        authorization: ChapterFinalizationAuthorization,
    ) -> ChapterCompletionCandidateSnapshot:
        return ChapterCompletionCandidateSnapshot(
            chapter_binding=ChapterBinding(
                owner_id=owner_id,
                novel_id=novel_id,
                volume_id=str(chapter.get("volume_id") or ""),
                chapter_id=chapter_id,
            ),
            source_binding=ChapterSourceBinding(
                prose_run_id=prose_run_id,
                prose_run_revision=prose_run_revision,
                content_digest=content_digest,
                outline_revision=str(run.get("outline_revision") or ""),
                outline_contract_digest=outline_contract_digest,
                expected_narrative_revision_before_commit=(
                    expected_narrative_revision
                ),
            ),
            authorization_binding=build_chapter_authorization_binding(
                kind=authorization.kind,
                authorization_id=(
                    authorization.authorization_id
                    or (
                        f"{authorization.job_id}:"
                        f"{authorization.readiness_digest}:"
                        f"{authorization.authorization_revision}"
                    )
                ),
                authorization_revision=authorization.authorization_revision,
                job_id=(
                    authorization.job_id
                    if authorization.kind == "job_readiness"
                    else None
                ),
                readiness_digest=authorization.readiness_digest,
            ),
            evaluated_at=_aware_snapshot_time(run.get("updated_at")),
        )

    async def _persist_completion_decision(
        self,
        *,
        authorization: ChapterFinalizationAuthorization,
        job: Mapping[str, Any],
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        decision: ChapterCompletionDecision,
    ) -> None:
        await self._deps.job_repo.append_chapter_completion_decision(
            authorization.job_id,
            chapter_id=chapter_id,
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            decision=decision.model_dump(mode="json"),
            fence=ChapterCompletionDecisionFence.capture(job),
        )

    @staticmethod
    def _decision_authorization(
        supplied: ChapterFinalizationAuthorization,
        job: Mapping[str, Any],
    ) -> ChapterFinalizationAuthorization:
        readiness = job.get("readiness")
        current = readiness if isinstance(readiness, Mapping) else {}
        current_digest = current.get("digest")
        readiness_digest = (
            current_digest
            if _is_completion_digest(current_digest)
            else supplied.readiness_digest
        )
        planning = current.get("planning")
        frozen = (
            planning.get("chapter_finalization_authorization")
            if isinstance(planning, Mapping)
            else None
        )
        frozen_revision = (
            frozen.get("authorization_revision")
            if isinstance(frozen, Mapping)
            else None
        )
        authorization_revision = (
            frozen_revision
            if type(frozen_revision) is int and frozen_revision >= 1
            else supplied.authorization_revision
        )
        authorization_id = supplied.authorization_id
        if supplied.kind == "interactive_completion_readiness":
            current_id = current.get("authorization_id")
            if isinstance(current_id, str) and current_id:
                authorization_id = current_id
        return ChapterFinalizationAuthorization(
            kind=supplied.kind,
            authorization_id=authorization_id,
            job_id=supplied.job_id,
            readiness_digest=readiness_digest,
            authorization_revision=authorization_revision,
            execution_claim_token=supplied.execution_claim_token,
        )

    def _decision_context(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        run: Mapping[str, Any],
        chapter: Mapping[str, Any],
        job: Mapping[str, Any],
        authorization: ChapterFinalizationAuthorization,
    ) -> _CompletionDecisionContext:
        return _CompletionDecisionContext(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            candidate_digest=candidate_digest,
            expected_narrative_revision=int(
                candidate["captured_narrative_revision"]
            ),
            candidate_text=str(candidate["text"]),
            completion=dict(candidate.get("completion") or {}),
            run=run,
            chapter=chapter,
            authorization=self._decision_authorization(authorization, job),
        )

    async def _persist_failure_decision(
        self,
        *,
        context: _CompletionDecisionContext,
        job: Mapping[str, Any],
        failure_fact: ChapterCompletionFailureFact,
        adherence: Mapping[str, Any] | None = None,
        state_proposal_id: str | None = None,
        state_command: MutationCommand | None = None,
        proposal_claim: Mapping[str, Any] | None = None,
        state_fact_accounting: Mapping[str, Any] | None = None,
    ) -> ChapterCompletionDecision:
        self._assert_completion_decision_scope(
            job=job,
            authorization=context.authorization,
            owner_id=context.owner_id,
            novel_id=context.novel_id,
            chapter_id=context.chapter_id,
            prose_run_id=context.prose_run_id,
            prose_run_revision=context.prose_run_revision,
            content_digest=context.candidate_digest,
            prose_run=context.run,
        )
        validated_adherence = _validated_failure_adherence(
            adherence,
            context=context,
        )
        terminal_fact = _reconcile_outline_failure_fact(
            failure_fact,
            validated_adherence,
        )
        failure_class, assessment_stage = (
            classify_chapter_completion_failure(terminal_fact)
        )
        raw_contract_digest = (
            validated_adherence.get("outline_contract_digest")
            if isinstance(validated_adherence, Mapping)
            else None
        )
        outline_contract_digest = (
            str(raw_contract_digest)
            if _is_completion_digest(raw_contract_digest)
            else _chapter_outline_contract_digest(
                dict(context.chapter.get("outline") or {})
            )
        )
        candidate_snapshot = self._candidate_snapshot(
            owner_id=context.owner_id,
            novel_id=context.novel_id,
            chapter_id=context.chapter_id,
            prose_run_id=context.prose_run_id,
            prose_run_revision=context.prose_run_revision,
            content_digest=context.candidate_digest,
            expected_narrative_revision=(
                context.expected_narrative_revision
            ),
            outline_contract_digest=outline_contract_digest,
            run=context.run,
            chapter=context.chapter,
            authorization=context.authorization,
        )
        quality_debt_status, quality_debt_sidecar_digest = (
            _failure_quality_binding(
                validated_adherence,
                assessment_stage=assessment_stage,
            )
        )
        blocking_issue_signatures = _failure_issue_signatures(
            failure_fact=terminal_fact,
            failure_class=failure_class,
            adherence=validated_adherence,
        )
        available_evidence = {
            "schema_version": "chapter_completion_available_evidence.v1",
            "assessment_stage": assessment_stage,
            "completion": {
                key: context.completion.get(key)
                for key in (
                    "status",
                    "can_write_formal_prose",
                    "finish_reason",
                    "scene_count",
                    "completed_scene_count",
                )
            },
            "outline_adherence": _failure_adherence_projection(
                validated_adherence,
                quality_debt_status=quality_debt_status,
                quality_debt_sidecar_digest=quality_debt_sidecar_digest,
            ),
            "state": _failure_state_projection(
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
                state_fact_accounting=state_fact_accounting,
            ),
            "repair_failure": (
                terminal_fact.repair_failure.model_dump(mode="json")
                if terminal_fact.repair_failure is not None
                else None
            ),
        }
        evidence_bundle = ChapterCompletionFailureEvidenceBundle(
            schema_version=CHAPTER_COMPLETION_FAILURE_EVIDENCE_SCHEMA,
            assessment_stage=assessment_stage,
            provider_attempt_ledger_digest=(
                _failure_provider_attempt_ledger_digest(
                    job,
                    chapter_id=context.chapter_id,
                )
            ),
            available_evidence_digest=canonical_completion_digest(
                available_evidence
            ),
            blocking_issue_signatures=blocking_issue_signatures,
            failure_classes=(failure_class,),
            quality_debt_status=quality_debt_status,
            quality_debt_sidecar_digest=quality_debt_sidecar_digest,
        )
        decision = ChapterCompletionPolicy().assess(
            candidate_snapshot,
            evidence_bundle,
            CHAPTER_COMPLETION_POLICY_REVISION,
        )
        await self._persist_completion_decision(
            authorization=context.authorization,
            job=job,
            chapter_id=context.chapter_id,
            prose_run_id=context.prose_run_id,
            prose_run_revision=context.prose_run_revision,
            decision=decision,
        )
        return decision

    async def record_failure(
        self,
        *,
        owner_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        authorization: ChapterFinalizationAuthorization,
        adherence: Mapping[str, Any] | None,
        failure_fact: ChapterCompletionFailureFact,
        state_proposal_id: str | None = None,
        state_fact_accounting: Mapping[str, Any] | None = None,
    ) -> ChapterCompletionDecision:
        """Persist one terminal evaluation that stops before finalization."""

        try:
            candidate = await self._deps.prose_runs.inspect_ai_completion_candidate(
                owner_id=owner_id,
                run_id=prose_run_id,
                chapter_id=chapter_id,
                expected_revision=prose_run_revision,
            )
        except ValueError as exc:
            raise ChapterFinalizationDenied(str(exc)) from exc
        run = dict(candidate["run"])
        candidate_digest = str(candidate["text_digest"])
        novel_id = str(run.get("novel_id") or "")
        chapter = await self._deps.chapter_repo.get_chapter_by_id(chapter_id)
        job = await self._deps.job_repo.get_job(authorization.job_id)
        self._assert_completion_decision_scope(
            job=job,
            authorization=authorization,
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            content_digest=candidate_digest,
            prose_run=run,
        )
        context = self._decision_context(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            candidate_digest=candidate_digest,
            candidate=candidate,
            run=run,
            chapter=chapter,
            job=job,
            authorization=authorization,
        )
        if not isinstance(failure_fact, ChapterCompletionFailureFact):
            raise ChapterFinalizationDenied(
                "章节完成失败事实不是闭集合同"
            )
        terminal_fact = failure_fact
        try:
            await self._verify_authorization(
                authorization,
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                prose_run_id=prose_run_id,
                prose_run_revision=prose_run_revision,
                content_digest=candidate_digest,
                job=job,
            )
        except _CompletionGateDenied as exc:
            terminal_fact = exc.failure_fact
        if str(chapter.get("novel_id") or "") != novel_id:
            terminal_fact = _failure_fact(
                "source_binding_stale",
                "chapter_binding",
            )
        return await self._persist_failure_decision(
            context=context,
            job=job,
            failure_fact=terminal_fact,
            adherence=adherence,
            state_proposal_id=state_proposal_id,
            state_fact_accounting=state_fact_accounting,
        )

    async def commit(
        self,
        *,
        owner_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        state_proposal_id: str,
        state_acceptance_token: str,
        authorization: ChapterFinalizationAuthorization,
        evidence: ChapterFinalizationEvidence,
    ) -> dict[str, Any]:
        job = await self._deps.job_repo.get_job(authorization.job_id)
        successor = _required_finalization_authority(job)
        try:
            if successor is None:
                candidate = (
                    await self._deps.prose_runs.inspect_ai_completion_candidate(
                        owner_id=owner_id,
                        run_id=prose_run_id,
                        chapter_id=chapter_id,
                        expected_revision=prose_run_revision,
                    )
                )
                completion_promotion = None
            else:
                candidate = await self._deps.prose_runs.inspect_required_reviewed_completion_candidate(
                    owner_id=owner_id,
                    run_id=prose_run_id,
                    chapter_id=chapter_id,
                    expected_revision=prose_run_revision,
                )
                from backend.services.generation.required_chapter_finalization_job import (
                    build_required_completion_promotion,
                )

                effective_completion, completion_promotion = (
                    build_required_completion_promotion(
                        finalization_job_id=authorization.job_id,
                        readiness_digest=authorization.readiness_digest,
                        authorization=successor,
                        stored_completion=candidate["completion"],
                    )
                )
                candidate = {
                    **candidate,
                    "completion": effective_completion,
                }
                expected_evidence = ChapterFinalizationEvidence(
                    outline_adherence=successor.outline_adherence,
                    repair_cycles_used=successor.repair_cycles_used,
                    repair_trace=successor.repair_trace,
                )
                if evidence != expected_evidence:
                    raise ChapterFinalizationDenied(
                        "章节正式 successor 证据与冻结授权不一致"
                    )
        except ValueError as exc:
            raise ChapterFinalizationDenied(str(exc)) from exc
        run = dict(candidate["run"])
        candidate_text = str(candidate["text"])
        candidate_digest = str(candidate["text_digest"])
        novel_id = str(run.get("novel_id") or "")
        chapter = await self._deps.chapter_repo.get_chapter_by_id(chapter_id)
        self._assert_completion_decision_scope(
            job=job,
            authorization=authorization,
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            content_digest=candidate_digest,
            prose_run=run,
        )
        decision_context = self._decision_context(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            candidate_digest=candidate_digest,
            candidate=candidate,
            run=run,
            chapter=chapter,
            job=job,
            authorization=authorization,
        )
        try:
            stored_authorization, job = await self._verify_authorization(
                authorization,
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                prose_run_id=prose_run_id,
                prose_run_revision=prose_run_revision,
                content_digest=candidate_digest,
                job=job,
            )
        except _CompletionGateDenied as exc:
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=exc.failure_fact,
                adherence=evidence.outline_adherence,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        if str(chapter.get("novel_id") or "") != novel_id:
            failure = _CompletionGateDenied(
                "正式提交章节不属于正文候选所在小说",
                _failure_fact(
                    "source_binding_stale",
                    "chapter_binding",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
            )
            raise ChapterFinalizationDenied(str(failure)) from failure
        try:
            if successor is None:
                state_payload, state_metadata, proposal_claim = (
                    await self._deps.state_proposals.prepare_policy_decision(
                        chapter_id=chapter_id,
                        proposal_id=state_proposal_id,
                        acceptance_token=state_acceptance_token,
                        policy=FactAccountingPolicy(),
                    )
                )
            else:
                finalization_binding = JobMutationRecoveryBindingV1(
                    novel_id=novel_id,
                    job_id=authorization.job_id,
                    chapter_id=chapter_id,
                    readiness_digest=authorization.readiness_digest,
                    authorization_revision=(
                        authorization.authorization_revision
                    ),
                    expected_narrative_revision=int(
                        candidate["captured_narrative_revision"]
                    ),
                    operation="finalize_chapter_generation",
                    idempotency_key=chapter_finalization_idempotency_key(
                        prose_run_id=prose_run_id,
                        prose_run_revision=prose_run_revision,
                        state_proposal_id=state_proposal_id,
                    ),
                )
                prepare_required_state = getattr(
                    self._deps.state_proposals,
                    "prepare_required_state_policy_decision",
                )
                state_payload, state_metadata, proposal_claim = (
                    await prepare_required_state(
                        chapter_id=chapter_id,
                        proposal_id=state_proposal_id,
                        acceptance_token=state_acceptance_token,
                        policy=FactAccountingPolicy(),
                        required_state_generation_binding=(
                            successor.state_generation_binding
                        ),
                        finalization_binding=finalization_binding,
                    )
                )
        except StateProposalCompletionFailure as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _state_proposal_failure_fact(exc),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        except StaleStatePreview as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "source_binding_stale",
                    "state_proposal",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        except ValueError as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "evidence_invalid",
                    "state_proposal",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        proposal_claim = {
            **proposal_claim,
            "finalized_prose": {
                "run_id": prose_run_id,
                "run_revision": int(prose_run_revision),
                "content_digest": candidate_digest,
            },
        }
        try:
            state_command = (
                await self._deps.state_service.prepare_chapter_state_mutation(
                    chapter_id,
                    state_payload,
                    acceptance_metadata=state_metadata,
                    proposal_claim=proposal_claim,
                )
            )
        except ValueError as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "evidence_invalid",
                    "state_mutation",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        if state_command.novel_id != novel_id:
            failure = _CompletionGateDenied(
                "正文候选与状态候选不属于同一小说",
                _failure_fact(
                    "source_binding_stale",
                    "state_mutation",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(failure)) from failure

        prose_payload = {
            "run_id": prose_run_id,
            "expected_revision": int(prose_run_revision),
            "text_digest": candidate_digest,
            "completion": dict(candidate["completion"]),
            "captured_narrative_revision": int(
                candidate["captured_narrative_revision"]
            ),
        }
        expected_revision = int(prose_payload["captured_narrative_revision"])
        try:
            adherence_metadata, fact_accounting = self._validate_gates(
                prose_payload=prose_payload,
                state_command=state_command,
                evidence=evidence,
                authorization=stored_authorization,
                chapter=chapter,
                job=job,
                prose_text=candidate_text,
            )
            if (
                int(proposal_claim["expected_narrative_revision"])
                != expected_revision
            ):
                raise _CompletionGateDenied(
                    "正文候选与状态候选基于不同的小说版本",
                    _failure_fact(
                        "source_binding_stale",
                        "completion_gates",
                    ),
                )
        except ChapterFinalizationDenied as exc:
            failure = (
                exc
                if isinstance(exc, _CompletionGateDenied)
                else _CompletionGateDenied(
                    str(exc),
                    _failure_fact(
                        "evidence_invalid",
                        "completion_gates",
                    ),
                )
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc

        try:
            repair_trace_digest, repair_convergence = _repair_trace_binding(
                evidence
            )
            local_issue_set = _local_issue_projection(
                evidence.outline_adherence
            )
            candidate_snapshot = self._candidate_snapshot(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                prose_run_id=prose_run_id,
                prose_run_revision=prose_run_revision,
                content_digest=candidate_digest,
                expected_narrative_revision=expected_revision,
                outline_contract_digest=str(
                    adherence_metadata.get("outline_contract_digest") or ""
                ),
                run=run,
                chapter=chapter,
                authorization=authorization,
            )
            evidence_bundle = ChapterCompletionEvidenceBundle(
                provider_attempt_ledger_digest=canonical_completion_digest(
                    _provider_attempt_ledger_projection(
                        job,
                        chapter_id=chapter_id,
                    )
                ),
                prose_integrity_digest=canonical_completion_digest({
                    "source": prose_payload,
                    "scene_progress": list(run.get("scene_progress") or []),
                }),
                scene_contract_digest=str(
                    adherence_metadata.get("outline_contract_digest") or ""
                ),
                beat_evidence_digest=canonical_completion_digest({
                    "schema_version": evidence.outline_adherence.get(
                        "evidence_schema_version"
                    ),
                    "source_prose_run_id": prose_run_id,
                    "source_prose_run_revision": prose_run_revision,
                    "source_content_digest": candidate_digest,
                    "beat_evidence": list(
                        evidence.outline_adherence.get("beat_evidence") or []
                    ),
                    "findings": list(
                        evidence.outline_adherence.get("findings") or []
                    ),
                    "unknowns": list(
                        evidence.outline_adherence.get("unknowns") or []
                    ),
                }),
                local_issue_set_digest=canonical_completion_digest(
                    local_issue_set
                ),
                state_proposal_id=state_proposal_id,
                state_proposal_digest=str(
                    proposal_claim.get("candidate_digest") or ""
                ),
                state_fact_accounting_digest=str(
                    fact_accounting.get("accounting_digest") or ""
                ),
                repair_trace_digest=repair_trace_digest,
                quality_debt_status=str(
                    adherence_metadata.get("quality_debt_status") or ""
                ),
                quality_debt_sidecar_digest=str(
                    adherence_metadata.get("quality_debt_sidecar_digest") or ""
                ),
                prose_integrity_passed=True,
                scene_contract_passed=True,
                state_fact_accounting_passed=True,
                repair_convergence=repair_convergence,
                blocking_issue_signatures=tuple(
                    item["issue_signature"]
                    for item in local_issue_set
                    if item["severity"] in {"blocker", "major", "unknown"}
                ),
                failure_classes=(),
                quality_debt_count=int(
                    adherence_metadata.get("quality_debt_count") or 0
                ),
            )
            completion_policy = ChapterCompletionPolicy()
            completion_decision = completion_policy.assess(
                candidate_snapshot,
                evidence_bundle,
                CHAPTER_COMPLETION_POLICY_REVISION,
            )
            await self._persist_completion_decision(
                authorization=authorization,
                job=job,
                chapter_id=chapter_id,
                prose_run_id=prose_run_id,
                prose_run_revision=prose_run_revision,
                decision=completion_decision,
            )
            certificate = completion_policy.issue(completion_decision)
            completion_policy.verify(
                certificate,
                ChapterCompletionCurrentSnapshot(
                    candidate_snapshot=candidate_snapshot,
                    evidence_bundle=evidence_bundle,
                ),
            )
        except (ChapterCompletionPolicyError, ValueError) as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "evidence_invalid",
                    "policy_assessment",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc

        try:
            prose_command = await self._deps.prose_runs.prepare_accept_mutation(
                owner_id=owner_id,
                run_id=prose_run_id,
                chapter_id=chapter_id,
                expected_revision=prose_run_revision,
                accept_partial=False,
                partial_acknowledgement=False,
                chapter_completion_certificate=certificate.model_dump(
                    mode="json"
                ),
                required_completion_promotion=completion_promotion,
            )
        except ValueError as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "publication_conflict",
                    "publication_prepare",
                ),
            )
            await self._persist_failure_decision(
                context=decision_context,
                job=job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        try:
            latest_authorization, latest_job = await self._verify_authorization(
                authorization,
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                prose_run_id=prose_run_id,
                prose_run_revision=prose_run_revision,
                content_digest=candidate_digest,
            )
        except _CompletionGateDenied as exc:
            latest_job = await self._deps.job_repo.get_job(
                authorization.job_id
            )
            latest_decision_context = replace(
                decision_context,
                authorization=self._decision_authorization(
                    authorization,
                    latest_job,
                ),
            )
            await self._persist_failure_decision(
                context=latest_decision_context,
                job=latest_job,
                failure_fact=_retarget_failure_fact(
                    exc.failure_fact,
                    "publication_revalidation",
                ),
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc
        latest_decision_context = replace(
            decision_context,
            authorization=self._decision_authorization(
                authorization,
                latest_job,
            ),
        )
        latest_attempt_digest = canonical_completion_digest(
            _provider_attempt_ledger_projection(
                latest_job,
                chapter_id=chapter_id,
            )
        )
        if (
            latest_authorization != stored_authorization
            or latest_attempt_digest
            != certificate.evidence_binding.provider_attempt_ledger_digest
        ):
            failure = _CompletionGateDenied(
                "正式提交授权或付费调用账本在证书签发后发生变化",
                _failure_fact(
                    "authorization_binding_stale",
                    "publication_revalidation",
                ),
            )
            await self._persist_failure_decision(
                context=latest_decision_context,
                job=latest_job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(failure)) from failure

        idempotency_key = chapter_finalization_idempotency_key(
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            state_proposal_id=state_proposal_id,
        )
        deterministic_child_ids = {
            key: hashlib.sha256(
                f"{idempotency_key}:{key}".encode("utf-8")
            ).hexdigest()[:24]
            for key in state_command.child_ids
        }
        prose_command = replace(
            prose_command,
            payload={**prose_command.payload, "defer_derived_stats": True},
        )
        state_command = replace(
            state_command,
            child_ids=deterministic_child_ids,
        )
        command = MutationCommand(
            novel_id=novel_id,
            idempotency_key=idempotency_key,
            operation="finalize_chapter_generation",
            version=FINALIZE_CHAPTER_GENERATION_COMMAND_VERSION,
            expected_narrative_revision=expected_revision,
            payload={
                "chapter_id": chapter_id,
                "authorization": {
                    **authorization.model_dump(
                        exclude_defaults=True,
                        exclude_none=True,
                    ),
                    "snapshot": deepcopy(stored_authorization),
                },
                "chapter_completion_certificate": certificate.model_dump(
                    mode="json"
                ),
                "prose_command": _serialize_subcommand(prose_command),
                "state_command": _serialize_subcommand(state_command),
            },
        )
        try:
            return await commit_mutation(
                command,
                self._execute_finalize,
                advances_narrative_revision=True,
            )
        except (MutationConflictError, NarrativeRevisionConflict) as exc:
            failure = _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "publication_conflict",
                    "publication_commit",
                ),
            )
            latest_job = await self._deps.job_repo.get_job(
                authorization.job_id
            )
            conflict_decision_context = replace(
                decision_context,
                authorization=self._decision_authorization(
                    authorization,
                    latest_job,
                ),
            )
            await self._persist_failure_decision(
                context=conflict_decision_context,
                job=latest_job,
                failure_fact=failure.failure_fact,
                adherence=evidence.outline_adherence,
                state_proposal_id=state_proposal_id,
                state_command=state_command,
                proposal_claim=proposal_claim,
            )
            raise ChapterFinalizationDenied(str(exc)) from exc

    async def recover_completed(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        state_proposal_id: str,
        authorization: ChapterFinalizationAuthorization,
    ) -> dict[str, Any] | None:
        """Recover one frozen finalization journal without rebuilding evidence."""

        from backend.services.novel.mutation_recovery import (
            find_job_bound_finalization_recovery_binding,
            recover_bound_mutation_revision,
        )

        job = await self._deps.job_repo.get_job(authorization.job_id)
        successor = _required_finalization_authority(job)
        interactive_recovery = (
            authorization.kind != "interactive_completion_readiness"
            or authorization.authorization_id != authorization.job_id
        ) is False
        successor_recovery = (
            authorization.kind == "job_readiness"
            and successor is not None
            and successor.owner_id == str(owner_id)
            and successor.novel_id == str(novel_id)
            and successor.chapter_id == str(chapter_id)
            and successor.reviewed_candidate.source_run_id
            == str(prose_run_id)
            and successor.reviewed_candidate.source_run_revision
            == int(prose_run_revision)
            and successor.state_candidate.state_proposal_id
            == str(state_proposal_id)
        )
        if not interactive_recovery and not successor_recovery:
            raise ChapterFinalizationDenied(
                "章节正式提交恢复授权无效"
            )
        binding = await find_job_bound_finalization_recovery_binding(
            novel_id=novel_id,
            job_id=authorization.job_id,
            chapter_id=chapter_id,
            readiness_digest=authorization.readiness_digest,
            authorization_revision=authorization.authorization_revision,
            expected_narrative_revision=(
                await self._expected_recovery_revision(
                    chapter_id=chapter_id,
                    prose_run_id=prose_run_id,
                    prose_run_revision=prose_run_revision,
                    authorization=authorization,
                )
            ),
        )
        if binding is None:
            return None
        recovered_revision = await recover_bound_mutation_revision(binding)
        chapter = await self._deps.chapter_repo.get_chapter_by_id(chapter_id)
        try:
            verification = verify_persisted_chapter_completion_certificate(
                chapter
            )
            certificate = ChapterCompletionCertificate.model_validate(
                (chapter.get("prose_acceptance") or {}).get(
                    "chapter_completion_certificate"
                )
            )
        except (ChapterCompletionPolicyError, ValueError) as exc:
            raise ChapterFinalizationDenied(
                "恢复后的章节完成证书或回执无效"
            ) from exc
        source = certificate.source_binding
        expected_authorization = build_chapter_authorization_binding(
            kind=authorization.kind,
            authorization_id=(
                authorization.authorization_id
                or (
                    f"{authorization.job_id}:"
                    f"{authorization.readiness_digest}:"
                    f"{authorization.authorization_revision}"
                )
            ),
            authorization_revision=authorization.authorization_revision,
            job_id=(
                authorization.job_id
                if authorization.kind == "job_readiness"
                else None
            ),
            readiness_digest=authorization.readiness_digest,
        )
        if (
            str(chapter.get("novel_id") or "") != str(novel_id)
            or certificate.chapter_binding.owner_id != str(owner_id)
            or source.prose_run_id != str(prose_run_id)
            or source.prose_run_revision != int(prose_run_revision)
            or certificate.evidence_binding.state_proposal_id
            != str(state_proposal_id)
            or certificate.authorization_binding != expected_authorization
            or recovered_revision
            != source.expected_narrative_revision_before_commit + 1
        ):
            raise ChapterFinalizationDenied(
                "恢复后的章节完成证书没有绑定当前授权"
            )
        acceptance = dict(chapter.get("prose_acceptance") or {})
        return {
            "chapter_id": chapter_id,
            "certificate": certificate.model_dump(mode="json"),
            "completion_receipt": deepcopy(acceptance["completion_receipt"]),
            "verification": verification.model_dump(mode="json"),
            "prose": {
                "acceptance_state": "ai_complete",
                "content_digest": str(acceptance.get("content_digest") or ""),
            },
            "recovered": True,
        }

    async def _expected_recovery_revision(
        self,
        *,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        authorization: ChapterFinalizationAuthorization,
    ) -> int:
        """Read only the frozen journal identity needed for exact discovery."""

        job = await self._deps.job_repo.get_job(authorization.job_id)
        readiness = job.get("readiness")
        source = (
            readiness.get("source_binding")
            if isinstance(readiness, Mapping)
            else None
        )
        expected = (
            source.get("expected_narrative_revision")
            if isinstance(source, Mapping)
            else None
        )
        if (
            type(expected) is not int
            or expected < 0
            or str(job.get("current_chapter_id") or "") != str(chapter_id)
            or (
                str(source.get("prose_run_id") or "")
                != str(prose_run_id)
                or source.get("prose_run_revision")
                != int(prose_run_revision)
            )
        ):
            raise ChapterFinalizationDenied(
                "章节正式提交恢复授权已经漂移"
            )
        return expected

    async def _verify_authorization(
        self,
        supplied: ChapterFinalizationAuthorization,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        content_digest: str,
        job: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_job = (
            dict(job)
            if isinstance(job, Mapping)
            else await self._deps.job_repo.get_job(supplied.job_id)
        )
        if str(current_job.get("novel_id") or "") != str(novel_id):
            raise _CompletionGateDenied(
                "批量作业不属于正文候选所在小说",
                _failure_fact(
                    "authorization_binding_stale",
                    "authorization",
                ),
            )
        attempt_slots = current_job.get("attempt_slots")
        has_live_attempt = isinstance(attempt_slots, list) and any(
            isinstance(slot, Mapping)
            and slot.get("state") in {"claimed", "uncertain"}
            for slot in attempt_slots
        )
        if current_job.get("has_uncertain_attempts") or has_live_attempt:
            raise _CompletionGateDenied(
                "尚有未处置的付费调用，不能正式提交",
                _failure_fact(
                    "paid_attempt_unresolved",
                    "authorization",
                ),
            )
        if supplied.kind == "interactive_completion_readiness":
            readiness = dict(current_job.get("readiness") or {})
            source = readiness.get("source_binding")
            if (
                str(current_job.get("job_kind") or "")
                != "interactive_chapter_completion"
                or str(current_job.get("status") or "")
                != "completion_running"
                or str(current_job.get("owner_id") or "") != str(owner_id)
                or readiness.get("schema_version") not in {
                    "interactive_chapter_completion_readiness.v1",
                    "interactive_chapter_completion_readiness.v2",
                }
                or readiness.get("authorization_id")
                != supplied.authorization_id
                or readiness.get("authorization_revision")
                != supplied.authorization_revision
                or not isinstance(source, Mapping)
                or str(source.get("owner_id") or "") != str(owner_id)
                or str(source.get("novel_id") or "") != str(novel_id)
                or str(source.get("chapter_id") or "") != str(chapter_id)
                or str(source.get("prose_run_id") or "")
                != str(prose_run_id)
                or source.get("prose_run_revision") != prose_run_revision
                or str(source.get("content_digest") or "")
                != str(content_digest)
                or str(
                    (
                        current_job.get("interactive_execution_claim")
                        if isinstance(
                            current_job.get("interactive_execution_claim"),
                            Mapping,
                        )
                        else {}
                    ).get("token")
                    or ""
                )
                != supplied.execution_claim_token
            ):
                raise _CompletionGateDenied(
                    "交互式完成授权没有绑定当前正文候选",
                    _failure_fact(
                        "authorization_binding_stale",
                        "authorization",
                    ),
                )
        else:
            if str(current_job.get("status") or "") != "running":
                raise _CompletionGateDenied(
                    "批量作业当前不允许正式提交",
                    _failure_fact(
                        "authorization_binding_stale",
                        "authorization",
                    ),
                )
            readiness = dict(current_job.get("readiness") or {})
        if str(current_job.get("current_chapter_id") or "") != str(chapter_id):
            raise _CompletionGateDenied(
                "批量作业未授权当前章节",
                _failure_fact(
                    "authorization_binding_stale",
                    "authorization",
                ),
            )
        if str(readiness.get("digest") or "") != supplied.readiness_digest:
            raise _CompletionGateDenied(
                "批量 readiness 摘要已经变化",
                _failure_fact(
                    "authorization_binding_stale",
                    "authorization",
                ),
            )
        planning = readiness.get("planning")
        if not isinstance(planning, Mapping):
            raise _CompletionGateDenied(
                "批量 readiness 规划无效",
                _failure_fact(
                    "authorization_binding_stale",
                    "authorization",
                ),
            )
        try:
            frozen = parse_chapter_finalization_authorization(
                planning.get("chapter_finalization_authorization")
            )
        except ChapterFinalizationDenied as exc:
            raise _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "authorization_binding_stale",
                    "authorization",
                ),
            ) from exc
        frozen_revision = frozen["authorization_revision"]
        if frozen_revision != supplied.authorization_revision:
            raise _CompletionGateDenied(
                "正式提交授权版本已经变化",
                _failure_fact(
                    "authorization_binding_stale",
                    "authorization",
                ),
            )
        return frozen, current_job

    @staticmethod
    def _validate_gates(
        *,
        prose_payload: Mapping[str, Any],
        state_command: MutationCommand,
        evidence: ChapterFinalizationEvidence,
        authorization: Mapping[str, Any],
        chapter: Mapping[str, Any],
        job: Mapping[str, Any],
        prose_text: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        completion = dict(prose_payload.get("completion") or {})
        if not completion_allows_formal_write(
            status=completion.get("status"),
            can_write_formal_prose=completion.get("can_write_formal_prose"),
            finish_reason=completion.get("finish_reason"),
        ):
            raise _CompletionGateDenied(
                "正文候选未通过完整性闸门",
                _failure_fact(
                    "prose_incomplete",
                    "completion_gates",
                ),
            )
        adherence = dict(evidence.outline_adherence or {})
        outline = chapter.get("outline")
        if not isinstance(outline, Mapping):
            raise _CompletionGateDenied(
                "正式提交章节缺少有效章纲",
                _failure_fact(
                    "evidence_invalid",
                    "completion_gates",
                ),
            )
        scenes = outline.get("scenes")
        if (
            not isinstance(scenes, list)
            or not scenes
            or completion.get("scene_count") != len(scenes)
            or completion.get("completed_scene_count") != len(scenes)
        ):
            raise _CompletionGateDenied(
                "正文候选场景完整性证据无效",
                _failure_fact(
                    "scene_contract_unsatisfied",
                    "completion_gates",
                ),
            )
        if (
            str(adherence.get("source_prose_run_id") or "")
            != str(prose_payload["run_id"])
            or _strict_int(
                adherence.get("source_prose_run_revision"),
                field="细纲符合度正文版本",
            )
            != int(prose_payload["expected_revision"])
            or str(adherence.get("source_content_digest") or "")
            != str(prose_payload["text_digest"])
        ):
            raise _CompletionGateDenied(
                "细纲符合度结果没有绑定当前正文候选",
                _failure_fact(
                    "source_binding_stale",
                    "completion_gates",
                ),
            )
        try:
            submitted_adherence = (
                ValidatedChapterOutlineAdherenceEvidenceV4Schema.model_validate(
                    adherence
                )
            )
        except ValueError as exc:
            raise _CompletionGateDenied(
                "章纲符合度当前本地问题策略证据无效",
                _failure_fact(
                    "evidence_invalid",
                    "outline_adherence",
                ),
            ) from exc
        try:
            rebuilt_adherence = revalidate_current_outline_adherence_evidence(
                adherence,
                outline=outline,
                prose=prose_text,
                source_prose_run_id=str(prose_payload["run_id"]),
                source_prose_run_revision=int(
                    prose_payload["expected_revision"]
                ),
                source_content_digest=str(prose_payload["text_digest"]),
            )
        except OutlineAdherenceValidationError as exc:
            raise _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "evidence_invalid",
                    "outline_adherence",
                ),
            ) from exc
        trusted_adherence = _trusted_outline_adherence_evidence(
            job=job,
            chapter_id=str(chapter.get("_id") or ""),
        )
        if submitted_adherence != trusted_adherence:
            raise _CompletionGateDenied(
                "章纲符合度证据与作业持久证据不一致",
                _failure_fact("evidence_invalid", "outline_adherence"),
            )
        try:
            adherence_metadata = validate_complete_outline_adherence(
                rebuilt_adherence,
                outline=outline,
                prose=prose_text,
                require_current_evidence=True,
            )
        except OutlineAdherenceValidationError as exc:
            raise _CompletionGateDenied(
                str(exc),
                failure_fact_from_outline_adherence_decision(
                    rebuilt_adherence.get("decision")
                ),
            ) from exc
        max_repairs = _strict_int(
            authorization.get("max_repair_cycles"),
            field="正文修复次数上限",
            maximum=MAX_FINALIZATION_REPAIR_CYCLES,
        )
        if evidence.repair_cycles_used > max_repairs:
            raise _CompletionGateDenied(
                "正文修复次数超过已授权上限",
                _failure_fact(
                    "authorization_binding_stale",
                    "completion_gates",
                ),
            )

        state_payload = state_command.payload
        metadata = dict(state_payload.get("acceptance_metadata") or {})
        if list(metadata.get("consistency_issues") or []):
            raise _CompletionGateDenied(
                "状态候选仍有一致性冲突",
                _failure_fact(
                    "canonical_fact_unaccounted",
                    "completion_gates",
                ),
            )
        completion_evidence = dict(metadata.get("state_completion") or {})
        resolution = dict(completion_evidence.get("reference_resolution") or {})
        resolution_fields = (
            "proposed_character_update_count",
            "accepted_character_update_count",
            "dropped_character_update_count",
            "proposed_thread_update_count",
            "accepted_thread_update_count",
            "dropped_thread_update_count",
        )
        if any(field not in resolution for field in resolution_fields):
            raise _CompletionGateDenied(
                "状态候选缺少完整的引用校验证据",
                _failure_fact(
                    "evidence_invalid",
                    "completion_gates",
                ),
            )
        counts = {
            field: _strict_int(
                resolution.get(field),
                field=f"状态引用证据 {field}",
            )
            for field in resolution_fields
        }
        if any(
            counts[field] > 0
            for field in (
                "dropped_character_update_count",
                "dropped_thread_update_count",
            )
        ):
            raise _CompletionGateDenied(
                "状态候选仍含无效内部引用",
                _failure_fact(
                    "internal_reference_invalid",
                    "completion_gates",
                ),
            )
        raw_fact_accounting = completion_evidence.get("fact_accounting")
        if not isinstance(raw_fact_accounting, Mapping):
            raise _CompletionGateDenied(
                "状态候选缺少正式事实核算证据",
                _failure_fact(
                    "evidence_invalid",
                    "completion_gates",
                ),
            )
        try:
            fact_accounting = validate_state_fact_accounting(
                raw_fact_accounting
            )
        except StateFactAccountingError as exc:
            raise _CompletionGateDenied(
                str(exc),
                _failure_fact(
                    "evidence_invalid",
                    "completion_gates",
                ),
            ) from exc
        if not fact_accounting["gate_passed"]:
            raise _CompletionGateDenied(
                "状态候选仍有未核算正式事实或非法内部引用",
                _failure_fact(
                    "canonical_fact_unaccounted",
                    "completion_gates",
                ),
            )
        fact_source = fact_accounting["source_binding"]
        if (
            str(fact_source.get("chapter_id") or "")
            != str(chapter.get("_id") or "")
            or str(fact_source.get("source_prose_run_id") or "")
            != str(prose_payload["run_id"])
            or _strict_int(
                fact_source.get("source_prose_run_revision"),
                field="正式事实核算正文版本",
            )
            != int(prose_payload["expected_revision"])
            or str(fact_source.get("source_content_digest") or "")
            != str(prose_payload["text_digest"])
        ):
            raise _CompletionGateDenied(
                "正式事实核算没有绑定当前正文候选",
                _failure_fact(
                    "source_binding_stale",
                    "completion_gates",
                ),
            )
        source_acceptance_state = completion_evidence.get(
            "source_prose_acceptance_state"
        )
        successor = _required_finalization_authority(job)
        accepted_source_states = (
            {"required_reviewed_candidate"}
            if successor is not None
            else {"ai_complete"}
        )
        if source_acceptance_state not in accepted_source_states:
            raise _CompletionGateDenied(
                "状态候选未绑定完整正文",
                _failure_fact(
                    "prose_incomplete",
                    "completion_gates",
                ),
            )
        if (
            str(completion_evidence.get("source_prose_run_id") or "")
            != str(prose_payload["run_id"])
            or _strict_int(
                completion_evidence.get("source_prose_run_revision"),
                field="状态候选正文版本",
            )
            != int(prose_payload["expected_revision"])
            or str(completion_evidence.get("source_content_digest") or "")
            != str(prose_payload["text_digest"])
        ):
            raise _CompletionGateDenied(
                "状态候选没有绑定当前正文候选",
                _failure_fact(
                    "source_binding_stale",
                    "completion_gates",
                ),
            )
        return adherence_metadata, fact_accounting

    @staticmethod
    async def _execute_finalize(session: Any, mutation: MutationRecorder) -> dict[str, Any]:
        root_command = mutation.journal["command"]
        command_version = int(root_command.get("version") or 0)
        if command_version not in {1, FINALIZE_CHAPTER_GENERATION_COMMAND_VERSION}:
            raise ValueError("章节正式提交命令版本未知")
        payload = root_command["payload"]
        prose = _SubMutationRecorder(
            mutation,
            prefix="prose",
            command=payload["prose_command"],
        )
        state = _SubMutationRecorder(
            mutation,
            prefix="state",
            command=payload["state_command"],
        )
        prose_result = await ProseRunModule._execute_accept(session, prose)
        state_result = await ChapterStateService._execute_accept_chapter_state(
            session, state
        )
        certificate_payload: dict[str, Any] | None = None
        completion_receipt: dict[str, Any] | None = None
        if command_version == FINALIZE_CHAPTER_GENERATION_COMMAND_VERSION:
            raw_certificate = payload.get("chapter_completion_certificate")
            try:
                certificate = ChapterCompletionCertificate.model_validate(
                    raw_certificate
                )
            except ValueError as exc:
                raise ValueError("章节正式提交证书格式或摘要无效") from exc
            certificate_payload = certificate.model_dump(mode="json")
            narrative_receipt = (
                mutation.journal.get("receipts") or {}
            ).get("narrative_revision") or {}
            narrative_revision_after = narrative_receipt.get("revision")
            if type(narrative_revision_after) is not int:
                raise ValueError("章节正式提交缺少 narrative revision 回执")
            completion_receipt = _completion_receipt(
                certificate_payload,
                narrative_revision_after=narrative_revision_after,
            )
            if not mutation.was_received("chapter_completion"):
                chapter = await chapter_repo.get_chapter_by_id(
                    str(payload["chapter_id"]),
                    session=session,
                )
                acceptance = dict(chapter.get("prose_acceptance") or {})
                if (
                    acceptance.get("state") != "ai_complete"
                    or acceptance.get("chapter_completion_certificate")
                    != certificate_payload
                ):
                    raise ValueError("正式正文与章节完成证书不一致")
                if acceptance.get("completion_receipt") != completion_receipt:
                    acceptance["completion_receipt"] = completion_receipt
                    await chapter_repo.update_chapter(
                        str(payload["chapter_id"]),
                        {"prose_acceptance": acceptance},
                        session=session,
                    )
                await mutation.receipt(
                    "chapter_completion",
                    completion_receipt,
                )
            else:
                stored_receipt = mutation.journal["receipts"].get(
                    "chapter_completion"
                )
                if stored_receipt != completion_receipt:
                    raise ValueError("章节完成回执已经漂移")
        await mutation.advance_phase("derived_data")
        if mutation.was_received("final_derived_stats"):
            stats = mutation.journal["receipts"]["final_derived_stats"]
        else:
            stats = await derived_stats.refresh(
                str(mutation.journal["novel_id"]), session=session
            )
            await mutation.receipt("final_derived_stats", stats)
        result = {
            "chapter_id": str(payload["chapter_id"]),
            "prose": prose_result,
            "state": state_result,
            "derived_stats": stats,
        }
        if certificate_payload is not None:
            result["certificate"] = certificate_payload
            result["completion_receipt"] = completion_receipt
        return result


chapter_finalization_service = ChapterFinalizationService()
