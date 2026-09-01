"""Frozen successor readiness and Job seam for required chapter review.

The Module stops at one independently reviewed, non-formal prose candidate.
It never generates state, accepts prose, advances Job progress, or finalizes a
chapter.  A future Module may consume the persisted result through its exact
versioned Interface.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.services.generation.independent_outline_review import (
    IndependentReviewPlan,
)
from backend.services.generation.prose_runs import prose_revision
from backend.services.generation.required_adherence_capacity import (
    RequiredAdherenceCapacity,
)
from backend.services.generation.required_chapter_review import (
    RequiredChapterReviewOutcome,
    RequiredChapterReviewPlan,
)
from backend.services.generation.required_initial_prose_contracts import (
    RequiredInitialProseAuthorization,
    build_required_initial_prose_authorization,
    build_required_initial_prose_origin,
)
from backend.services.generation.required_prose_rewrite_contracts import (
    RequiredProseRewritePlan,
    RequiredRewriteAuthorization,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    StructuredOutputMode,
    WorkflowStepTarget,
)


REQUIRED_CHAPTER_REVIEW_PIPELINE_REVISION = "required-chapter-review-job-r1"
REQUIRED_CHAPTER_REVIEW_PLANNING_KEY = "required_chapter_review_authorization"
REQUIRED_CHAPTER_REVIEW_REVISION_KEY = (
    "required_chapter_review_pipeline_revision"
)
REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON = "required_reviewed_candidate_ready"
REQUIRED_REVIEW_ACKNOWLEDGEMENT = (
    "successor_stops_before_state_and_formal_commit"
)

_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MAX = 2**63 - 1
_SUCCESSOR_PLANNING_KEYS = frozenset({
    REQUIRED_CHAPTER_REVIEW_PLANNING_KEY,
    REQUIRED_CHAPTER_REVIEW_REVISION_KEY,
})
_LEGACY_CANDIDATE_KEYS = frozenset({
    "chapter_candidate_pipeline_revision",
    "chapter_candidate_repair_authorization",
    "chapter_candidate_job_execution_authorization",
})
_FORMAL_AUTHORITY_KEYS = frozenset({
    "chapter_finalization_authorization",
    "prose_continuation_authorization",
})
_REPLACED_ISSUE_CODES = frozenset({
    "automatic_continuations_require_confirmation",
    "automatic_reference_card_creation_requires_confirmation",
    "batch_generation_budget_may_pause",
    "batch_generation_requires_token_budget",
    "batch_generation_token_bound_unproven",
    "prose_token_bound_unproven",
})

_INITIAL_TARGET = ("write_chapter_by_ai", "chapter_content")
_PLANNER_TARGET = (
    "remediate_chapter_prose_by_agent",
    "remediation_planner",
)
_REWRITE_TARGET = (
    "remediate_chapter_prose_by_agent",
    "prose_candidate_rewrite",
)
_REVIEW_TARGET = (
    "remediate_chapter_prose_by_agent",
    "outline_adherence",
)


class RequiredChapterReviewJobConflict(ValueError):
    """A stored successor result or its exact authority diverged."""


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RequiredGenerationPlanSnapshot(_Closed):
    """Reconstructable GenerationPlan including its Provider override."""

    schema_version: Literal["required_generation_plan.v1"] = (
        "required_generation_plan.v1"
    )
    call_kind: Literal["structured", "text"]
    workflow: str = Field(min_length=1, max_length=160)
    step: str = Field(min_length=1, max_length=160)
    target_provider_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    structured_output_mode: Literal[
        "prompt_json",
        "json_object",
        "schema_enforced",
    ]
    reviewer_alias: str | None = Field(default=None, min_length=1, max_length=160)
    timeout_seconds: int = Field(ge=1, le=86_400)
    config_revision: str = Field(min_length=1, max_length=240)
    capability_snapshot: str = Field(min_length=1, max_length=240)
    max_semantic_attempts: int = Field(ge=1, le=2)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    max_context_tokens: int | None = Field(default=None, ge=1, le=2**31 - 1)
    thinking_mode: Literal["enabled", "disabled"] | None = None

    @classmethod
    def freeze(
        cls,
        plan: GenerationPlan,
        *,
        call_kind: Literal["structured", "text"],
        expected_target: tuple[str, str],
    ) -> "RequiredGenerationPlanSnapshot":
        target = plan.target
        if (
            not isinstance(target, WorkflowStepTarget)
            or (target.workflow_name, target.step_name) != expected_target
            or plan.reviewer_alias is not None
            or type(plan.timeout_seconds) is not int
            or type(plan.max_output_tokens) is not int
        ):
            raise ValueError("required_generation_plan_unsupported")
        return cls(
            call_kind=call_kind,
            workflow=target.workflow_name,
            step=target.step_name,
            target_provider_alias=target.provider_alias,
            provider_alias=plan.provider_alias,
            provider_model=plan.provider_model,
            structured_output_mode=plan.mode.value,
            reviewer_alias=plan.reviewer_alias,
            timeout_seconds=plan.timeout_seconds,
            config_revision=plan.config_revision,
            capability_snapshot=plan.capability_snapshot,
            max_semantic_attempts=plan.max_semantic_attempts,
            max_output_tokens=plan.max_output_tokens,
            max_context_tokens=plan.max_context_tokens,
            thinking_mode=plan.thinking_mode,
        )

    def thaw(self) -> GenerationPlan:
        return GenerationPlan(
            target=WorkflowStepTarget(
                self.workflow,
                self.step,
                provider_alias=self.target_provider_alias,
            ),
            provider_alias=self.provider_alias,
            timeout_seconds=self.timeout_seconds,
            mode=StructuredOutputMode(self.structured_output_mode),
            reviewer_alias=self.reviewer_alias,
            config_revision=self.config_revision,
            capability_snapshot=self.capability_snapshot,
            max_semantic_attempts=self.max_semantic_attempts,
            provider_model=self.provider_model,
            max_output_tokens=self.max_output_tokens,
            max_context_tokens=self.max_context_tokens,
            thinking_mode=self.thinking_mode,
        )


class RequiredReviewAuthorization(_Closed):
    schema_version: Literal["required_adherence_job_authorization.v1"] = (
        "required_adherence_job_authorization.v1"
    )
    chapter_ids: list[str] = Field(min_length=1, max_length=1000)
    capacity: RequiredAdherenceCapacity
    provider_alias: str = Field(min_length=1, max_length=64)
    deadline_at: datetime

    @model_validator(mode="after")
    def validate_identity(self) -> "RequiredReviewAuthorization":
        if (
            self.deadline_at.tzinfo is None
            or len(set(self.chapter_ids)) != len(self.chapter_ids)
            or any(re.fullmatch(_OBJECT_ID, item) is None for item in self.chapter_ids)
        ):
            raise ValueError("required_review_authorization_invalid")
        return self


class RequiredInitialChapterAuthorization(_Closed):
    chapter_id: str = Field(pattern=_OBJECT_ID)
    volume_id: str = Field(pattern=_OBJECT_ID)
    order_index: int = Field(ge=0, le=2**31 - 1)
    outline_revision: str = Field(pattern=_SHA256)
    initial_prose: RequiredInitialProseAuthorization


class RequiredProviderBound(_Closed):
    provider_alias: str = Field(min_length=1, max_length=160)
    maximum_paid_attempts_total: int = Field(ge=1, le=_MAX)
    maximum_tokens_total: int = Field(ge=1, le=_MAX)


class RequiredChapterReviewAuthorization(_Closed):
    schema_version: Literal["required_chapter_review_job_authorization.v1"] = (
        "required_chapter_review_job_authorization.v1"
    )
    protocol_revision: Literal["required-chapter-review-job-r1"] = (
        REQUIRED_CHAPTER_REVIEW_PIPELINE_REVISION
    )
    contract_digest: str = Field(pattern=_SHA256)
    novel_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    scope: Literal["volume", "book"]
    volume_id: str | None = Field(default=None, pattern=_OBJECT_ID)
    authorization_revision: int = Field(ge=1, le=_MAX)
    narrative_revision: int = Field(ge=0, le=_MAX)
    created_at: datetime
    deadline_at: datetime
    work_digest: str = Field(pattern=_SHA256)
    chapters: list[RequiredInitialChapterAuthorization] = Field(
        min_length=1,
        max_length=1000,
    )
    initial_generation: RequiredGenerationPlanSnapshot
    rewrite_planner: RequiredGenerationPlanSnapshot
    rewrite_generation: RequiredGenerationPlanSnapshot
    independent_review: RequiredGenerationPlanSnapshot
    review_writer_model: str = Field(min_length=1, max_length=240)
    review_input_token_bound: int = Field(ge=1, le=_MAX)
    review_max_response_bytes: int = Field(ge=1, le=_MAX)
    review: RequiredReviewAuthorization
    rewrite: RequiredRewriteAuthorization
    maximum_provider_attempts_total: int = Field(ge=1, le=_MAX)
    maximum_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_serial_seconds_total: int = Field(ge=1, le=_MAX)
    token_budget: int = Field(ge=1, le=_MAX)
    provider_bounds: list[RequiredProviderBound] = Field(min_length=1, max_length=32)
    max_repairs_per_chapter: Literal[2] = 2
    can_write_formal_prose: Literal[False] = False
    can_generate_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_aggregate_identity(self) -> "RequiredChapterReviewAuthorization":
        chapter_ids = [item.chapter_id for item in self.chapters]
        if (
            self.created_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or self.deadline_at
            < self.created_at + timedelta(seconds=self.maximum_serial_seconds_total)
            or len(set(chapter_ids)) != len(chapter_ids)
            or self.review.chapter_ids != chapter_ids
            or self.review.deadline_at != self.deadline_at
            or self.rewrite.review_capacity != self.review.capacity
            or self.token_budget < self.maximum_tokens_total
            or self.scope == "volume" and self.volume_id is None
            or self.scope == "book" and self.volume_id is not None
        ):
            raise ValueError("required_chapter_review_authorization_invalid")
        attempts = sum(item.maximum_paid_attempts_total for item in self.provider_bounds)
        tokens = sum(item.maximum_tokens_total for item in self.provider_bounds)
        if (
            attempts != self.maximum_provider_attempts_total
            or tokens != self.maximum_tokens_total
            or len({item.provider_alias for item in self.provider_bounds})
            != len(self.provider_bounds)
        ):
            raise ValueError("required_chapter_review_budget_invalid")
        identity = self.model_dump(mode="python", exclude={"contract_digest"})
        if _digest(identity) != self.contract_digest:
            raise ValueError("required_chapter_review_contract_digest_changed")
        return self

    def chapter(self, chapter_id: str) -> RequiredInitialChapterAuthorization:
        matches = [item for item in self.chapters if item.chapter_id == chapter_id]
        if len(matches) != 1:
            raise ValueError("required_chapter_review_chapter_not_authorized")
        return matches[0]

    def plan(self) -> RequiredChapterReviewPlan:
        review = IndependentReviewPlan(
            generation=self.independent_review.thaw(),
            writer_model=self.review_writer_model,
            input_token_bound=self.review_input_token_bound,
            max_response_bytes=self.review_max_response_bytes,
        )
        plan = RequiredChapterReviewPlan(
            initial_generation=self.initial_generation.thaw(),
            rewrite=RequiredProseRewritePlan(
                planner=self.rewrite_planner.thaw(),
                rewrite=self.rewrite_generation.thaw(),
                review=review,
            ),
        )
        if (
            RequiredAdherenceCapacity.from_plan(review) != self.review.capacity
            or RequiredRewriteAuthorization.model_validate(
                plan.rewrite.authorization()
            ) != self.rewrite
        ):
            raise ValueError("required_chapter_review_plan_changed")
        return plan

    def maximum_attempts_for_chapter(self, chapter_id: str) -> int:
        initial = self.chapter(chapter_id).initial_prose
        return (
            initial.max_calls
            + self.review.capacity.max_review_attempts_per_chapter
            + self.rewrite.max_rewrites * self.rewrite.attempts
        )


class RequiredReviewedChapterCandidate(_Closed):
    """Metadata-only handoff.  It deliberately contains no prose text."""

    schema_version: Literal["required_reviewed_chapter_candidate.v1"] = (
        "required_reviewed_chapter_candidate.v1"
    )
    status: Literal["reviewed"] = "reviewed"
    result_digest: str = Field(pattern=_SHA256)
    job_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    novel_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    readiness_digest: str = Field(pattern=_SHA256)
    authorization_revision: int = Field(ge=1, le=_MAX)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    narrative_revision: int = Field(ge=0, le=_MAX)
    source_run_id: str = Field(pattern=_OBJECT_ID)
    source_run_revision: int = Field(ge=2, le=_MAX)
    source_content_digest: str = Field(pattern=_SHA256)
    source_view_digest: str = Field(pattern=_SHA256)
    candidate_digest: str = Field(pattern=_SHA256)
    review_checkpoint_digest: str = Field(pattern=_SHA256)
    review_contract_digest: str = Field(pattern=_SHA256)
    review_ledger_digest: str = Field(pattern=_SHA256)
    repair_count: int = Field(ge=0, le=2)
    decision: Literal["pass"] = "pass"
    next_step: Literal["state_candidate"] = "state_candidate"
    can_write_formal_prose: Literal[False] = False
    can_generate_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_result_digest(self) -> "RequiredReviewedChapterCandidate":
        identity = self.model_dump(mode="python", exclude={"result_digest"})
        if _digest(identity) != self.result_digest:
            raise ValueError("required_reviewed_candidate_digest_changed")
        return self


@dataclass(frozen=True)
class RequiredChapterReviewJobOutcome:
    phase: Literal["reviewed", "incomplete", "blocked"]
    reviewed_candidate: RequiredReviewedChapterCandidate | None
    repair_count: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if (
            self.phase not in {"reviewed", "incomplete", "blocked"}
            or (self.phase == "reviewed")
            != (self.reviewed_candidate is not None)
            or type(self.repair_count) is not int
            or not 0 <= self.repair_count <= 2
            or self.phase == "reviewed" and self.reason_code is not None
            or self.phase != "reviewed" and not _safe_reason(self.reason_code)
        ):
            raise ValueError("required_chapter_review_job_outcome_invalid")


def _safe_reason(value: object) -> str | None:
    candidate = str(value or "")
    return candidate if _SAFE_REASON.fullmatch(candidate) else None


def _plan_snapshot(
    plan: GenerationPlan,
    *,
    call_kind: Literal["structured", "text"],
    target: tuple[str, str],
) -> RequiredGenerationPlanSnapshot:
    return RequiredGenerationPlanSnapshot.freeze(
        plan,
        call_kind=call_kind,
        expected_target=target,
    )


def _provider_bounds(
    chapters: Sequence[RequiredInitialChapterAuthorization],
    plan: RequiredChapterReviewPlan,
) -> list[RequiredProviderBound]:
    totals: dict[str, list[int]] = {}

    def add(alias: str, attempts: int, tokens: int) -> None:
        current = totals.setdefault(alias, [0, 0])
        current[0] += attempts
        current[1] += tokens

    for chapter in chapters:
        initial = chapter.initial_prose
        add(initial.provider_alias, initial.max_calls, initial.token_bound)
    count = len(chapters)
    capacity = plan.review_capacity
    add(
        plan.review.generation.provider_alias,
        count * capacity.max_review_attempts_per_chapter,
        count * capacity.max_review_tokens_per_chapter,
    )
    rewrite = RequiredRewriteAuthorization.model_validate(
        plan.rewrite.authorization()
    )
    rewrite_count = count * rewrite.max_rewrites
    add(
        rewrite.planner.provider_alias,
        rewrite_count * 2 * rewrite.planner.max_attempts,
        rewrite_count * 2 * rewrite.planner.tokens,
    )
    add(
        rewrite.rewrite.provider_alias,
        rewrite_count * rewrite.rewrite.max_attempts,
        rewrite_count * rewrite.rewrite.tokens,
    )
    return [
        RequiredProviderBound(
            provider_alias=alias,
            maximum_paid_attempts_total=values[0],
            maximum_tokens_total=values[1],
        )
        for alias, values in sorted(totals.items())
    ]


def build_required_chapter_review_authorization(
    *,
    base_readiness: Mapping[str, Any],
    chapters: Sequence[Mapping[str, Any]],
    plan: RequiredChapterReviewPlan,
    token_budget: int,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
) -> RequiredChapterReviewAuthorization:
    """Freeze explicit plans and each chapter's outline-dependent authority."""

    resources = base_readiness.get("resources")
    work = base_readiness.get("work")
    raw_work = work.get("chapters") if isinstance(work, Mapping) else None
    if (
        base_readiness.get("version") != 2
        or base_readiness.get("scope") not in {"volume", "book"}
        or not isinstance(resources, Mapping)
        or not isinstance(raw_work, list)
        or isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or token_budget <= 0
        or isinstance(authorization_revision, bool)
        or not isinstance(authorization_revision, int)
        or authorization_revision < 1
        or created_at.tzinfo is None
        or deadline_at.tzinfo is None
    ):
        raise ValueError("required_chapter_review_readiness_invalid")
    snapshots = {
        str(chapter.get("_id") or ""): chapter
        for chapter in chapters
        if isinstance(chapter, Mapping)
    }
    expected_ids = [
        str(item.get("chapter_id") or "")
        for item in raw_work
        if isinstance(item, Mapping)
        and not bool(item.get("has_content"))
    ]
    if (
        not expected_ids
        or len(expected_ids) != len(set(expected_ids))
        or set(expected_ids) != set(snapshots)
    ):
        raise ValueError("required_chapter_review_worklist_changed")
    entries: list[RequiredInitialChapterAuthorization] = []
    for raw in raw_work:
        if not isinstance(raw, Mapping):
            raise ValueError("required_chapter_review_worklist_changed")
        chapter_id = str(raw.get("chapter_id") or "")
        chapter = snapshots.get(chapter_id)
        if chapter is None:
            continue
        outline = chapter.get("outline")
        if (
            raw.get("has_outline") is not True
            or raw.get("has_content") is not False
            or not isinstance(outline, Mapping)
            or not outline
            or str(chapter.get("volume_id") or "")
            != str(raw.get("volume_id") or "")
        ):
            raise ValueError("required_chapter_review_requires_formal_outline")
        entries.append(RequiredInitialChapterAuthorization(
            chapter_id=chapter_id,
            volume_id=str(chapter.get("volume_id") or ""),
            order_index=int(chapter.get("order_index") or 0),
            outline_revision=prose_revision(outline),
            initial_prose=build_required_initial_prose_authorization(
                plan.initial_generation,
                outline,
            ),
        ))
    initial_snapshot = _plan_snapshot(
        plan.initial_generation,
        call_kind="text",
        target=_INITIAL_TARGET,
    )
    planner_snapshot = _plan_snapshot(
        plan.rewrite.planner,
        call_kind="structured",
        target=_PLANNER_TARGET,
    )
    rewrite_snapshot = _plan_snapshot(
        plan.rewrite.rewrite,
        call_kind="structured",
        target=_REWRITE_TARGET,
    )
    review_snapshot = _plan_snapshot(
        plan.review.generation,
        call_kind="structured",
        target=_REVIEW_TARGET,
    )
    capacity = plan.review_capacity
    chapter_ids = [item.chapter_id for item in entries]
    review = RequiredReviewAuthorization(
        chapter_ids=chapter_ids,
        capacity=capacity,
        provider_alias=plan.review.generation.provider_alias,
        deadline_at=deadline_at,
    )
    rewrite = RequiredRewriteAuthorization.model_validate(
        plan.rewrite.authorization()
    )
    bounds = _provider_bounds(entries, plan)
    serial_per_chapter = (
        max(item.initial_prose.serial_seconds for item in entries)
        + capacity.max_review_seconds_per_chapter
        + rewrite.max_rewrites * rewrite.seconds
    )
    identity = {
        "schema_version": "required_chapter_review_job_authorization.v1",
        "protocol_revision": REQUIRED_CHAPTER_REVIEW_PIPELINE_REVISION,
        "novel_id": str(base_readiness.get("novel_id") or ""),
        "owner_id": str(resources.get("owner_id") or ""),
        "scope": str(base_readiness.get("scope") or ""),
        "volume_id": (
            str(base_readiness.get("volume_id"))
            if base_readiness.get("volume_id") is not None
            else None
        ),
        "authorization_revision": authorization_revision,
        "narrative_revision": resources.get("narrative_revision"),
        "created_at": created_at,
        "deadline_at": deadline_at,
        "work_digest": _digest(list(raw_work)),
        "chapters": entries,
        "initial_generation": initial_snapshot,
        "rewrite_planner": planner_snapshot,
        "rewrite_generation": rewrite_snapshot,
        "independent_review": review_snapshot,
        "review_writer_model": plan.review.writer_model,
        "review_input_token_bound": plan.review.input_token_bound,
        "review_max_response_bytes": plan.review.max_response_bytes,
        "review": review,
        "rewrite": rewrite,
        "maximum_provider_attempts_total": sum(
            item.maximum_paid_attempts_total for item in bounds
        ),
        "maximum_tokens_total": sum(
            item.maximum_tokens_total for item in bounds
        ),
        "maximum_serial_seconds_total": len(entries) * serial_per_chapter,
        "token_budget": token_budget,
        "provider_bounds": bounds,
        "max_repairs_per_chapter": 2,
        "can_write_formal_prose": False,
        "can_generate_state": False,
    }
    return RequiredChapterReviewAuthorization(
        **identity,
        contract_digest=_digest(identity),
    )


def prepare_required_chapter_review_readiness(
    base_report: Mapping[str, Any],
    *,
    chapters: Sequence[Mapping[str, Any]],
    plan: RequiredChapterReviewPlan,
    token_budget: int,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
) -> dict[str, Any]:
    """Replace the old full-completion authority with the non-formal successor."""

    authorization = build_required_chapter_review_authorization(
        base_readiness=base_report,
        chapters=chapters,
        plan=plan,
        token_budget=token_budget,
        authorization_revision=authorization_revision,
        created_at=created_at,
        deadline_at=deadline_at,
    )
    raw_planning = base_report.get("planning")
    if not isinstance(raw_planning, Mapping):
        raise ValueError("required_chapter_review_readiness_invalid")
    # The successor owns a complete, explicit execution envelope. Reusing an
    # unrelated legacy planning field would create a second implied authority.
    planning: dict[str, Any] = {}
    planning.update({
        REQUIRED_CHAPTER_REVIEW_REVISION_KEY: (
            REQUIRED_CHAPTER_REVIEW_PIPELINE_REVISION
        ),
        REQUIRED_CHAPTER_REVIEW_PLANNING_KEY: authorization.model_dump(
            mode="python"
        ),
        "attempt_capacity": authorization.maximum_provider_attempts_total,
        "providers": [item.provider_alias for item in authorization.provider_bounds],
        "batch_generation_budget_coverage": {
            "schema_version": "required_chapter_review_budget_coverage.v1",
            "maximum_provider_attempts_total": (
                authorization.maximum_provider_attempts_total
            ),
            "maximum_tokens_total": authorization.maximum_tokens_total,
            "provider_bounds": [
                item.model_dump(mode="python")
                for item in authorization.provider_bounds
            ],
            "token_bound_known": True,
            "token_budget": token_budget,
            "covers_full_job_authority": True,
            "can_write_formal_prose": False,
            "can_generate_state": False,
        },
    })
    issues = [
        deepcopy(item)
        for item in (base_report.get("issues") or [])
        if isinstance(item, Mapping)
        and item.get("code") not in _REPLACED_ISSUE_CODES
    ]
    if not any(
        item.get("code") == REQUIRED_REVIEW_ACKNOWLEDGEMENT for item in issues
    ):
        issues.append({
            "code": REQUIRED_REVIEW_ACKNOWLEDGEMENT,
            "level": "warning_requires_ack",
            "details": {
                "next_step": "state_candidate",
                "can_write_formal_prose": False,
                "can_generate_state": False,
            },
            "action_codes": ["review_successor_boundary"],
        })
    snapshot = {
        "version": 2,
        "novel_id": deepcopy(base_report.get("novel_id")),
        "scope": deepcopy(base_report.get("scope")),
        "volume_id": deepcopy(base_report.get("volume_id")),
        "outline_deviation_policy": deepcopy(
            base_report.get("outline_deviation_policy")
        ),
        "work": deepcopy(base_report.get("work") or {}),
        "resources": deepcopy(base_report.get("resources") or {}),
        "active_proposal": deepcopy(base_report.get("active_proposal")),
        "planning": planning,
        "issues": issues,
    }
    levels = {str(item.get("level") or "") for item in issues}
    status = (
        "blocked"
        if "blocked" in levels
        else "warning_requires_ack"
        if "warning_requires_ack" in levels
        else "warning"
        if "warning" in levels
        else "ready"
    )
    prepared = {**snapshot, "status": status, "digest": _digest(snapshot)}
    validate_required_chapter_review_readiness(prepared)
    return prepared


def parse_required_chapter_review_authorization(
    value: Any,
) -> RequiredChapterReviewAuthorization:
    try:
        parsed = RequiredChapterReviewAuthorization.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError(
            "required_chapter_review_authorization_invalid"
        ) from exc
    if _digest(value) != _digest(parsed.model_dump(mode="python")):
        raise ValueError("required_chapter_review_authorization_not_canonical")
    parsed.plan()
    return parsed


def _successor_presence(planning: Mapping[str, Any]) -> bool:
    return any(key in planning for key in _SUCCESSOR_PLANNING_KEYS)


def required_chapter_review_planning_present(planning: Any) -> bool:
    """Return presence only; callers must still parse and fail closed."""

    return isinstance(planning, Mapping) and _successor_presence(planning)


def readiness_uses_required_chapter_review(readiness: Any) -> bool:
    if not isinstance(readiness, Mapping):
        return False
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        return False
    if not _successor_presence(planning):
        return False
    validate_required_chapter_review_readiness(readiness)
    return True


def validate_required_chapter_review_readiness(
    readiness: Mapping[str, Any],
) -> RequiredChapterReviewAuthorization:
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise ValueError("required_chapter_review_readiness_invalid")
    if (
        planning.get(REQUIRED_CHAPTER_REVIEW_REVISION_KEY)
        != REQUIRED_CHAPTER_REVIEW_PIPELINE_REVISION
        or REQUIRED_CHAPTER_REVIEW_PLANNING_KEY not in planning
        or any(key in planning for key in _LEGACY_CANDIDATE_KEYS)
        or any(key in planning for key in _FORMAL_AUTHORITY_KEYS)
    ):
        raise ValueError("required_chapter_review_mode_conflict")
    authorization = parse_required_chapter_review_authorization(
        planning[REQUIRED_CHAPTER_REVIEW_PLANNING_KEY]
    )
    resources = readiness.get("resources")
    work = readiness.get("work")
    raw_chapters = work.get("chapters") if isinstance(work, Mapping) else None
    coverage = planning.get("batch_generation_budget_coverage")
    if (
        readiness.get("version") != 2
        or not isinstance(resources, Mapping)
        or not isinstance(raw_chapters, list)
        or readiness.get("novel_id") != authorization.novel_id
        or resources.get("owner_id") != authorization.owner_id
        or readiness.get("scope") != authorization.scope
        or readiness.get("volume_id") != authorization.volume_id
        or resources.get("narrative_revision") != authorization.narrative_revision
        or _digest(raw_chapters) != authorization.work_digest
        or planning.get("attempt_capacity")
        != authorization.maximum_provider_attempts_total
        or planning.get("providers")
        != [item.provider_alias for item in authorization.provider_bounds]
        or not isinstance(coverage, Mapping)
        or coverage.get("schema_version")
        != "required_chapter_review_budget_coverage.v1"
        or coverage.get("maximum_provider_attempts_total")
        != authorization.maximum_provider_attempts_total
        or coverage.get("maximum_tokens_total")
        != authorization.maximum_tokens_total
        or coverage.get("token_budget") != authorization.token_budget
        or coverage.get("covers_full_job_authority") is not True
        or coverage.get("can_write_formal_prose") is not False
        or coverage.get("can_generate_state") is not False
    ):
        raise ValueError("required_chapter_review_readiness_changed")
    expected_ids = [
        str(item.get("chapter_id") or "")
        for item in raw_chapters
        if isinstance(item, Mapping) and not bool(item.get("has_content"))
    ]
    if (
        expected_ids != [item.chapter_id for item in authorization.chapters]
        or any(
            not isinstance(item, Mapping)
            or type(item.get("has_outline")) is not bool
            or type(item.get("has_content")) is not bool
            for item in raw_chapters
        )
        or any(
            item.get("has_outline") is not True
            for item in raw_chapters
            if isinstance(item, Mapping)
            and item.get("has_content") is False
        )
    ):
        raise ValueError("required_chapter_review_worklist_changed")
    digest = readiness.get("digest")
    digest_snapshot = {
        key: deepcopy(readiness.get(key))
        for key in (
            "version",
            "novel_id",
            "scope",
            "volume_id",
            "outline_deviation_policy",
            "work",
            "resources",
            "active_proposal",
            "planning",
            "issues",
        )
    }
    if not isinstance(digest, str) or digest != _digest(digest_snapshot):
        raise ValueError("required_chapter_review_readiness_digest_changed")
    return authorization


def readiness_chapter_uses_required_chapter_review(
    readiness: Any,
    *,
    chapter_id: str,
) -> bool:
    if not readiness_uses_required_chapter_review(readiness):
        return False
    authorization = validate_required_chapter_review_readiness(readiness)
    authorization.chapter(chapter_id)
    return True


def required_initial_authorization_from_planning(
    planning: Mapping[str, Any],
    *,
    chapter_id: str,
) -> RequiredInitialProseAuthorization:
    if _successor_presence(planning):
        authorization = parse_required_chapter_review_authorization(
            planning.get(REQUIRED_CHAPTER_REVIEW_PLANNING_KEY)
        )
        return authorization.chapter(chapter_id).initial_prose
    return RequiredInitialProseAuthorization.model_validate(
        planning["required_initial_prose"]
    )


def required_review_authorization_from_planning(
    planning: Mapping[str, Any],
) -> dict[str, Any]:
    if _successor_presence(planning):
        authorization = parse_required_chapter_review_authorization(
            planning.get(REQUIRED_CHAPTER_REVIEW_PLANNING_KEY)
        )
        return authorization.review.model_dump(mode="python")
    value = planning["required_adherence_review"]
    if not isinstance(value, Mapping):
        raise ValueError("required_review_authorization_invalid")
    return dict(value)


def required_rewrite_authorization_from_planning(
    planning: Mapping[str, Any],
) -> dict[str, Any]:
    if _successor_presence(planning):
        authorization = parse_required_chapter_review_authorization(
            planning.get(REQUIRED_CHAPTER_REVIEW_PLANNING_KEY)
        )
        return authorization.rewrite.model_dump(mode="python")
    value = planning["required_prose_rewrite"]
    if not isinstance(value, Mapping):
        raise ValueError("required_rewrite_authorization_invalid")
    return dict(value)


def required_review_repair_count(job: Mapping[str, Any]) -> int:
    """Read the durable logical repair count without changing recovery state."""

    raw = job.get("required_prose_rewrite_journal")
    if raw is None:
        return 0
    from backend.db.required_prose_rewrite_journal import RewriteJournal

    journal = RewriteJournal.model_validate_json(json.dumps(raw))
    count = len(journal.entries)
    if not 0 <= count <= 2:
        raise ValueError("required_chapter_review_repair_count_invalid")
    return count


def _build_reviewed_candidate(
    *,
    binding: Any,
    authorization: RequiredChapterReviewAuthorization,
    outcome: RequiredChapterReviewOutcome,
) -> RequiredReviewedChapterCandidate:
    candidate = outcome.candidate
    review = outcome.review
    if (
        outcome.phase != "reviewed"
        or candidate is None
        or review is None
        or review.phase != "review_settled"
        or (review.evidence or {}).get("decision") != "pass"
        or review.ledger_digest is None
        or outcome.reason_code is not None
    ):
        raise ValueError("required_reviewed_candidate_invalid")
    candidate.require_complete()
    snapshot = candidate.snapshot
    receipt = review.receipt
    from backend.services.generation.required_adherence_handoff import (
        InitialAwaitingAdherenceReceipt,
        required_review_ordinal,
    )

    chapter_authorization = authorization.chapter(binding.chapter_id)
    initial_origin = (
        build_required_initial_prose_origin(
            job_id=binding.job_id,
            readiness_digest=binding.readiness_digest,
            authorization_revision=binding.authorization_revision,
            narrative_revision=binding.narrative_revision,
            chapter_id=binding.chapter_id,
            outline_revision=chapter_authorization.outline_revision,
            authorization=chapter_authorization.initial_prose,
        )
        if isinstance(receipt, InitialAwaitingAdherenceReceipt)
        else None
    )

    if (
        chapter_authorization.outline_revision
        != prose_revision(json.loads(snapshot.outline_json))
        or receipt.source_run_id != snapshot.source_run_id
        or receipt.source_run_revision != snapshot.source_run_revision
        or receipt.source_content_digest != snapshot.source_content_digest
        or receipt.view_digest != snapshot.view_digest
        or receipt.review_contract_digest
        != authorization.review.capacity.review_contract_digest
        or required_review_ordinal(receipt) != outcome.repair_count
        or (
            initial_origin is not None
            and (
                receipt.initial_request_digest
                != initial_origin.request_digest
                or receipt.initial_contract_digest
                != initial_origin.contract_digest
            )
        )
    ):
        raise ValueError("required_reviewed_candidate_lineage_changed")
    checkpoint_digest = _digest(review.model_dump(mode="json"))
    identity = {
        "schema_version": "required_reviewed_chapter_candidate.v1",
        "status": "reviewed",
        "job_id": binding.job_id,
        "owner_id": binding.owner_id,
        "novel_id": binding.novel_id,
        "chapter_id": binding.chapter_id,
        "readiness_digest": binding.readiness_digest,
        "authorization_revision": binding.authorization_revision,
        "authorization_contract_digest": authorization.contract_digest,
        "narrative_revision": binding.narrative_revision,
        "source_run_id": snapshot.source_run_id,
        "source_run_revision": snapshot.source_run_revision,
        "source_content_digest": snapshot.source_content_digest,
        "source_view_digest": snapshot.view_digest,
        "candidate_digest": candidate.check_digest,
        "review_checkpoint_digest": checkpoint_digest,
        "review_contract_digest": receipt.review_contract_digest,
        "review_ledger_digest": review.ledger_digest,
        "repair_count": outcome.repair_count,
        "decision": "pass",
        "next_step": "state_candidate",
        "can_write_formal_prose": False,
        "can_generate_state": False,
    }
    return RequiredReviewedChapterCandidate(
        **identity,
        result_digest=_digest(identity),
    )


def parse_required_reviewed_candidate(value: Any) -> RequiredReviewedChapterCandidate:
    try:
        return RequiredReviewedChapterCandidate.model_validate(value)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterReviewJobConflict(
            "required_reviewed_candidate_invalid"
        ) from exc


def validate_required_reviewed_candidate_job(
    job: Mapping[str, Any],
    candidate: RequiredReviewedChapterCandidate,
) -> None:
    """Re-prove the metadata handoff from the durable ordered journals."""

    try:
        authorization = validate_required_chapter_review_readiness(
            job["readiness"]
        )
        chapter_authorization = authorization.chapter(candidate.chapter_id)
        from backend.db.required_adherence_journal import (
            REVIEW_STEP_PREFIX,
            RequiredReviewJobBinding,
            _ReviewAuthorization,
            _ReviewLedger,
            _checked_bookkeeping,
            _project_attempts,
        )
        from backend.db.required_initial_prose_journal import (
            RequiredInitialProseJournal,
            _validated_initial_attempts,
        )
        from backend.db.required_prose_rewrite_journal import (
            RewriteJournal,
            rewrite_accounting,
        )
        from backend.services.generation.required_adherence_handoff import (
            AwaitingAdherenceReceipt,
            InitialAwaitingAdherenceReceipt,
            RequiredAdherenceHandoff,
            required_review_ordinal,
        )
        from backend.services.generation.required_prose_rewrite_contracts import (
            contract_digest,
        )

        initial = RequiredInitialProseJournal.model_validate_json(
            json.dumps(job["required_initial_prose_journal"])
        )
        reviews = _ReviewLedger.model_validate_json(
            json.dumps(job["required_adherence_journal"])
        )
        raw_rewrites = job.get("required_prose_rewrite_journal")
        rewrites = (
            None
            if raw_rewrites is None
            else RewriteJournal.model_validate_json(json.dumps(raw_rewrites))
        )
        latest_review = reviews.entries[-1]
        checkpoint = latest_review.checkpoint
        receipt = checkpoint.receipt
        rewrite_count = 0 if rewrites is None else len(rewrites.entries)
        expected_binding = RequiredReviewJobBinding(
            job_id=candidate.job_id,
            owner_id=candidate.owner_id,
            novel_id=candidate.novel_id,
            chapter_id=candidate.chapter_id,
            readiness_digest=candidate.readiness_digest,
            authorization_revision=candidate.authorization_revision,
            narrative_revision=candidate.narrative_revision,
        )
        expected_review_authorization = _ReviewAuthorization.model_validate(
            authorization.review.model_dump(mode="python")
        )
        expected_initial_origin = build_required_initial_prose_origin(
            job_id=expected_binding.job_id,
            readiness_digest=expected_binding.readiness_digest,
            authorization_revision=expected_binding.authorization_revision,
            narrative_revision=expected_binding.narrative_revision,
            chapter_id=expected_binding.chapter_id,
            outline_revision=chapter_authorization.outline_revision,
            authorization=chapter_authorization.initial_prose,
        )
        _checked_bookkeeping(job)
        _validated_initial_attempts(
            job,
            initial,
            require_recorded_identity=True,
        )
        review_plan = authorization.plan().review
        for entry in reviews.entries:
            review_attempts = _project_attempts(
                job,
                binding=expected_binding,
                step_id=(
                    f"{REVIEW_STEP_PREFIX}"
                    f"{entry.checkpoint.receipt.view_digest}"
                ),
                provider_alias=authorization.review.provider_alias,
                capacity=authorization.review.capacity,
            )
            RequiredAdherenceHandoff._validate_ledger(
                entry.checkpoint,
                review_plan,
                review_attempts,
            )
        reviews_by_ordinal = {
            required_review_ordinal(entry.checkpoint.receipt): entry
            for entry in reviews.entries
        }
        review_sources_valid = len(reviews_by_ordinal) == len(reviews.entries)
        for ordinal, entry in reviews_by_ordinal.items():
            checked_receipt = entry.checkpoint.receipt
            if ordinal == 0:
                review_sources_valid = review_sources_valid and (
                    isinstance(
                        checked_receipt,
                        InitialAwaitingAdherenceReceipt,
                    )
                    and initial.phase == "produced"
                    and checked_receipt.initial_request_digest
                    == expected_initial_origin.request_digest
                    and checked_receipt.initial_contract_digest
                    == expected_initial_origin.contract_digest
                    and checked_receipt.source_run_id == initial.run_id
                    and checked_receipt.source_run_revision
                    == initial.run_revision
                    and checked_receipt.source_content_digest
                    == initial.content_digest
                )
                continue
            matching_rewrite = (
                rewrites.entries[ordinal - 1]
                if rewrites is not None
                and 1 <= ordinal <= len(rewrites.entries)
                else None
            )
            review_sources_valid = review_sources_valid and (
                isinstance(checked_receipt, AwaitingAdherenceReceipt)
                and matching_rewrite is not None
                and matching_rewrite.phase == "produced"
                and checked_receipt.source_run_id
                == matching_rewrite.origin.request.source_run_id
                and checked_receipt.source_run_revision
                == matching_rewrite.result_revision
                and checked_receipt.source_content_digest
                == matching_rewrite.result_digest
                and checked_receipt.previous_revision
                == matching_rewrite.origin.request.source_revision
                and checked_receipt.previous_content_digest
                == matching_rewrite.origin.request.source_content_digest
            )
        rewrite_entries_valid = rewrites is None
        if rewrites is not None:
            rewrite_entries_valid = (
                rewrites.binding == expected_binding
                and rewrites.authorization == authorization.rewrite
                and rewrites.review_authorization
                == expected_review_authorization
                and bool(rewrites.entries)
                and rewrites.entries[-1].phase == "produced"
            )
            for index, entry in enumerate(rewrites.entries, start=1):
                expected_request_digest = contract_digest({
                    "binding": expected_binding.model_dump(mode="json"),
                    "contract": authorization.rewrite.contract_digest,
                    "request": entry.origin.request.model_dump(mode="json"),
                })
                terminal_values = (
                    entry.result_revision,
                    entry.result_digest,
                    entry.agent_run_id,
                )
                rewrite_entries_valid = rewrite_entries_valid and (
                    entry.origin.job_id == expected_binding.job_id
                    and entry.origin.readiness_digest
                    == expected_binding.readiness_digest
                    and entry.origin.authorization_revision
                    == expected_binding.authorization_revision
                    and entry.origin.contract_digest
                    == authorization.rewrite.contract_digest
                    and entry.origin.review_contract_digest
                    == authorization.review.capacity.review_contract_digest
                    and entry.origin.rewrite_ordinal == index
                    and entry.origin.request_digest == expected_request_digest
                    and entry.outline_revision
                    == chapter_authorization.outline_revision
                    and entry.phase in {"produced", "incomplete"}
                    and all(value is not None for value in terminal_values)
                )
                if index == 1:
                    if entry.prior_review_digest is None:
                        rewrite_entries_valid = rewrite_entries_valid and (
                            initial.phase == "incomplete"
                            and entry.origin.request.source_run_id
                            == initial.run_id
                            and entry.origin.request.source_revision
                            == initial.run_revision
                            and entry.origin.request.source_content_digest
                            == initial.content_digest
                        )
                    else:
                        prior = reviews_by_ordinal.get(0)
                        rewrite_entries_valid = rewrite_entries_valid and (
                            initial.phase == "produced"
                            and prior is not None
                            and prior.checkpoint.phase == "review_settled"
                            and (prior.checkpoint.evidence or {}).get(
                                "decision"
                            )
                            == "repair"
                            and contract_digest(
                                prior.checkpoint.model_dump(mode="json")
                            )
                            == entry.prior_review_digest
                            and entry.origin.request.source_run_id
                            == prior.checkpoint.receipt.source_run_id
                            and entry.origin.request.source_revision
                            == prior.checkpoint.receipt.source_run_revision
                            and entry.origin.request.source_content_digest
                            == prior.checkpoint.receipt.source_content_digest
                        )
                else:
                    previous = rewrites.entries[index - 2]
                    rewrite_entries_valid = rewrite_entries_valid and (
                        previous.phase in {"produced", "incomplete"}
                        and entry.origin.request.source_run_id
                        == previous.origin.request.source_run_id
                        and entry.origin.request.source_revision
                        == previous.result_revision
                        and entry.origin.request.source_content_digest
                        == previous.result_digest
                    )
                    if previous.phase == "incomplete":
                        rewrite_entries_valid = rewrite_entries_valid and (
                            entry.prior_review_digest is None
                        )
                    else:
                        prior = reviews_by_ordinal.get(index - 1)
                        rewrite_entries_valid = rewrite_entries_valid and (
                            prior is not None
                            and prior.checkpoint.phase == "review_settled"
                            and (prior.checkpoint.evidence or {}).get(
                                "decision"
                            )
                            == "repair"
                            and entry.prior_review_digest is not None
                            and contract_digest(
                                prior.checkpoint.model_dump(mode="json")
                            )
                            == entry.prior_review_digest
                        )
                rewrite_accounting(job, entry, authorization.rewrite)
        source_matches = (
            initial.run_id == candidate.source_run_id
            and initial.run_revision == candidate.source_run_revision
            and initial.content_digest == candidate.source_content_digest
            if rewrite_count == 0
            else rewrites is not None
            and rewrites.entries[-1].origin.request.source_run_id
            == candidate.source_run_id
            and rewrites.entries[-1].result_revision
            == candidate.source_run_revision
            and rewrites.entries[-1].result_digest
            == candidate.source_content_digest
        )
        receipt_source_valid = (
            isinstance(receipt, InitialAwaitingAdherenceReceipt)
            and rewrite_count == 0
            and receipt.initial_request_digest
            == expected_initial_origin.request_digest
            and receipt.initial_contract_digest
            == expected_initial_origin.contract_digest
        ) or (
            isinstance(receipt, AwaitingAdherenceReceipt)
            and rewrite_count > 0
            and receipt.rewrite_ordinal == rewrite_count
        )
        progress = job.get("progress")
        if (
            str(job.get("_id")) != candidate.job_id
            or str(job.get("owner_id")) != candidate.owner_id
            or str(job.get("novel_id")) != candidate.novel_id
            or job.get("is_deleted") is not False
            or job.get("status") not in {"running", "paused"}
            or job.get("current_chapter_id") != candidate.chapter_id
            or job.get("authorization_revision")
            != candidate.authorization_revision
            or job.get("expected_narrative_revision")
            != candidate.narrative_revision
            or job["readiness"].get("digest") != candidate.readiness_digest
            or authorization.contract_digest
            != candidate.authorization_contract_digest
            or initial.origin != expected_initial_origin
            or initial.authorization != chapter_authorization.initial_prose
            or rewrite_count == 0 and initial.phase != "produced"
            or initial.phase not in {"produced", "incomplete"}
            or reviews.binding != expected_binding
            or reviews.authorization != expected_review_authorization
            or reviews.writer_contract_digest
            != authorization.rewrite.contract_digest
            or not reviews.entries
            or latest_review.outline_revision
            != chapter_authorization.outline_revision
            or checkpoint.phase != "review_settled"
            or (checkpoint.evidence or {}).get("decision") != "pass"
            or _digest(checkpoint.model_dump(mode="json"))
            != candidate.review_checkpoint_digest
            or checkpoint.ledger_digest != candidate.review_ledger_digest
            or receipt.review_contract_digest
            != candidate.review_contract_digest
            or receipt.source_run_id != candidate.source_run_id
            or receipt.source_run_revision != candidate.source_run_revision
            or receipt.source_content_digest != candidate.source_content_digest
            or receipt.view_digest != candidate.source_view_digest
            or latest_review.candidate_digest != candidate.candidate_digest
            or rewrite_count != candidate.repair_count
            or required_review_ordinal(receipt) != rewrite_count
            or not receipt_source_valid
            or not review_sources_valid
            or not rewrite_entries_valid
            or not source_matches
            or job.get("has_uncertain_attempts") is not False
            or job.get("active_token_reservations") not in (None, [])
            or job.get("tokens_reserved") not in (None, 0)
            or job.get("attempt_reservation") is not None
            or job.get("candidate_pipeline_checkpoints") not in (None, [])
            or job.get("job_mutation_recovery") is not None
            or not isinstance(progress, list)
            or any(
                isinstance(item, Mapping)
                and str(item.get("chapter_id") or "") == candidate.chapter_id
                for item in progress
            )
        ):
            raise ValueError("reviewed candidate proof changed")
    except (IndexError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterReviewJobConflict(
            "required_reviewed_candidate_proof_invalid"
        ) from exc


def required_review_finalization_evidence(
    job: Mapping[str, Any],
    candidate: RequiredReviewedChapterCandidate,
) -> dict[str, Any]:
    """Rebuild the finalization evidence from the exact reviewed Job journals.

    The handoff stays metadata-only: it contains the already-persisted V4
    adherence evidence plus digests of the rewrite/review transitions, never a
    second copy of the prose.  Callers must still bind this projection into a
    new, explicit formal-write readiness before it can be consumed.
    """

    validate_required_reviewed_candidate_job(job, candidate)
    try:
        from backend.db.required_adherence_journal import _ReviewLedger
        from backend.db.required_prose_rewrite_journal import RewriteJournal

        reviews = _ReviewLedger.model_validate_json(
            json.dumps(job["required_adherence_journal"])
        )
        raw_rewrites = job.get("required_prose_rewrite_journal")
        rewrites = (
            None
            if raw_rewrites is None
            else RewriteJournal.model_validate_json(json.dumps(raw_rewrites))
        )
        latest = reviews.entries[-1].checkpoint
        evidence = deepcopy(latest.evidence)
        if not isinstance(evidence, dict) or evidence.get("decision") != "pass":
            raise ValueError("terminal review is not a pass")
        repair_count = 0 if rewrites is None else len(rewrites.entries)
        if repair_count != candidate.repair_count:
            raise ValueError("rewrite count changed")
        repair_trace = None
        if repair_count:
            reviews_by_ordinal = {
                required_review_ordinal(entry.checkpoint.receipt): entry
                for entry in reviews.entries
            }
            convergence: list[dict[str, Any]] = []
            for ordinal, rewrite in enumerate(rewrites.entries, start=1):
                before = reviews_by_ordinal.get(ordinal - 1)
                after = reviews_by_ordinal.get(ordinal)
                convergence.append({
                    "schema_version": (
                        "required_review_rewrite_convergence.v1"
                    ),
                    "rewrite_ordinal": ordinal,
                    "trigger": (
                        "initial_incomplete"
                        if ordinal == 1 and rewrite.prior_review_digest is None
                        else "adherence_repair"
                    ),
                    "prior_review_checkpoint_digest": (
                        _digest(before.checkpoint.model_dump(mode="json"))
                        if before is not None
                        else None
                    ),
                    "rewrite_result_digest": rewrite.result_digest,
                    "next_review_checkpoint_digest": (
                        _digest(after.checkpoint.model_dump(mode="json"))
                        if after is not None
                        else None
                    ),
                    "decision": (
                        (after.checkpoint.evidence or {}).get("decision")
                        if after is not None
                        else None
                    ),
                })
            if any(
                item["next_review_checkpoint_digest"] is None
                for item in convergence
            ) or convergence[-1]["decision"] != "pass":
                raise ValueError("rewrite convergence chain is incomplete")
            repair_trace = {
                "schema_version": "chapter_repair_trace.v2",
                "repair_cycles_used": repair_count,
                "component_usage": [{
                    "schema_version": "required_review_rewrite_usage.v1",
                    "component": "local_prose_repair",
                    "used": repair_count,
                    "authorized_limit": 2,
                }],
                "convergence": convergence,
                "final_issue_signatures": [],
                "converged": True,
            }
        return {
            "outline_adherence": evidence,
            "repair_cycles_used": repair_count,
            "repair_trace": repair_trace,
        }
    except (IndexError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RequiredChapterReviewJobConflict(
            "required_review_finalization_evidence_invalid"
        ) from exc


class RequiredChapterReviewJobRunner:
    """Production Adapter from a frozen Job to RequiredChapterReviewLoop."""

    def __init__(self, job_id: str, *, repository=None, loop_factory=None) -> None:
        if repository is None:
            from backend.db.repositories.generation_job_repository import (
                generation_job_repo,
            )

            repository = generation_job_repo
        self._job_id = str(job_id)
        self._repository = repository
        self._loop_factory = loop_factory

    async def run(
        self,
        novel_id: str,
        chapter: Mapping[str, Any],
    ) -> RequiredChapterReviewJobOutcome:
        from backend.db.required_adherence_journal import RequiredReviewJobBinding
        from backend.services.generation.required_chapter_review import (
            RequiredChapterReviewLoop,
        )

        job = await self._repository.get_job(self._job_id)
        authorization = validate_required_chapter_review_readiness(
            job.get("readiness")
        )
        chapter_id = str(chapter.get("_id") or "")
        chapter_authorization = authorization.chapter(chapter_id)
        outline = chapter.get("outline")
        existing_result = job.get("required_reviewed_candidate")
        if (
            str(job.get("novel_id") or "") != str(novel_id)
            or str(novel_id) != authorization.novel_id
            or (
                existing_result is None
                and job.get("status") != "running"
            )
            or (
                existing_result is not None
                and job.get("status") not in {"running", "paused"}
            )
            or job.get("current_chapter_id") != chapter_id
            or job.get("authorization_revision")
            != authorization.authorization_revision
            or job.get("expected_narrative_revision")
            != authorization.narrative_revision
            or not isinstance(outline, Mapping)
            or bool(str(chapter.get("content") or "").strip())
            or prose_revision(outline) != chapter_authorization.outline_revision
        ):
            raise ValueError("required_chapter_review_job_binding_stale")
        plan = authorization.plan()
        if (
            build_required_initial_prose_authorization(
                plan.initial_generation,
                outline,
            )
            != chapter_authorization.initial_prose
        ):
            raise ValueError("required_chapter_review_outline_changed")
        binding = RequiredReviewJobBinding(
            job_id=self._job_id,
            owner_id=str(job.get("owner_id") or ""),
            novel_id=str(novel_id),
            chapter_id=chapter_id,
            readiness_digest=str(job["readiness"]["digest"]),
            authorization_revision=authorization.authorization_revision,
            narrative_revision=authorization.narrative_revision,
        )
        if existing_result is not None:
            candidate = parse_required_reviewed_candidate(existing_result)
            validate_required_reviewed_candidate_job(job, candidate)
            return RequiredChapterReviewJobOutcome(
                phase="reviewed",
                reviewed_candidate=candidate,
                repair_count=candidate.repair_count,
            )
        successor_prefixes = (
            "required-initial-prose:",
            "required-adherence:",
            "required-prose-rewrite:",
        )
        used = sum(
            1
            for slot in job.get("attempt_slots") or []
            if isinstance(slot, Mapping)
            and slot.get("chapter_id") == chapter_id
            and any(
                str(slot.get("step_id") or "").startswith(prefix)
                for prefix in successor_prefixes
            )
        )
        remaining = authorization.maximum_attempts_for_chapter(chapter_id) - used
        if remaining < 0:
            raise ValueError("required_chapter_review_attempt_capacity_changed")
        await self._repository.reserve_attempts(
            self._job_id,
            chapter_id,
            remaining,
        )
        try:
            factory = self._loop_factory or RequiredChapterReviewLoop
            outcome = await factory(binding=binding, plan=plan).run()
        finally:
            await self._repository.finish_attempt_reservation(
                self._job_id,
                chapter_id,
            )
        if outcome.phase == "reviewed":
            reviewed = _build_reviewed_candidate(
                binding=binding,
                authorization=authorization,
                outcome=outcome,
            )
            return RequiredChapterReviewJobOutcome(
                phase="reviewed",
                reviewed_candidate=reviewed,
                repair_count=outcome.repair_count,
            )
        reason = _safe_reason(outcome.reason_code) or (
            "required_review_incomplete"
            if outcome.phase == "incomplete"
            else "required_review_blocked"
        )
        return RequiredChapterReviewJobOutcome(
            phase=outcome.phase,
            reviewed_candidate=None,
            repair_count=outcome.repair_count,
            reason_code=reason,
        )
