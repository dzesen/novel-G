"""Bounded book-level coordinator contract for the successor chapter chain.

The three chapter Modules intentionally keep their own immutable readiness and
durable evidence.  This Module supplies the missing parent authority: one user
confirmation freezes the ordered book work, every Provider plan and the full
resource ceiling, while each child Job is derived from the current formal
narrative revision.  The coordinator stores metadata only and never receives
prose, prompts, state values or acceptance tokens.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.services.generation.attempt_ledger_contracts import (
    validate_launchable_attempt_ledgers,
)
from backend.services.generation.chapter_generation_application import (
    STATE_STEP,
    STATE_WORKFLOW,
)
from backend.services.generation.provider_budget import structured_call_budget
from backend.services.generation.prose_runs import prose_revision
from backend.services.generation.required_chapter_finalization_job import (
    REQUIRED_CHAPTER_FINALIZATION_ACKNOWLEDGEMENT,
    REQUIRED_CHAPTER_FINALIZATION_RESULT_STEP,
    parse_required_chapter_finalization_result,
    prepare_required_chapter_finalization_readiness,
    validate_required_chapter_finalization_readiness,
    validate_required_chapter_finalization_result_job,
)
from backend.services.generation.required_chapter_review_job import (
    REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON,
    REQUIRED_REVIEW_ACKNOWLEDGEMENT,
    RequiredChapterReviewAuthorization,
    RequiredGenerationPlanSnapshot,
    RequiredProviderBound,
    build_required_chapter_review_authorization,
    parse_required_reviewed_candidate,
    prepare_required_chapter_review_readiness,
    validate_required_chapter_review_readiness,
    validate_required_reviewed_candidate_job,
)
from backend.services.generation.required_chapter_state_job import (
    MAX_REQUIRED_STATE_CALLS,
    REQUIRED_STATE_CANDIDATE_ACKNOWLEDGEMENT,
    REQUIRED_STATE_CANDIDATE_PAUSE_REASON,
    parse_required_state_candidate,
    prepare_required_chapter_state_readiness,
    validate_required_chapter_state_readiness,
    validate_required_state_candidate_job,
)
from backend.services.llm.generation_runtime import GenerationPlan
from backend.services.novel.book_completion import BookCompletionReport


REQUIRED_BOOK_SUCCESSOR_PIPELINE_REVISION = "required-book-successor-r1"
REQUIRED_BOOK_SUCCESSOR_PLANNING_KEY = "required_book_successor_authorization"
REQUIRED_BOOK_SUCCESSOR_REVISION_KEY = "required_book_successor_pipeline_revision"
REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT = (
    "successor_book_runs_all_stages_and_writes_formal_prose_and_state"
)
REQUIRED_BOOK_SUCCESSOR_ACTIVE_REASON = "required_book_successor_active"
REQUIRED_BOOK_SUCCESSOR_BLOCKED_REASON = "required_book_successor_blocked"
REQUIRED_BOOK_SUCCESSOR_AUDIT_REASON = "required_book_successor_final_audit"
REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_NONE = "none"
REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_BEFORE_FIRST_CHILD = (
    "before_first_child"
)

_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MAX = 2**63 - 1
_STATE_TARGET = (STATE_WORKFLOW, STATE_STEP)
_PLANNING_KEYS = frozenset({
    REQUIRED_BOOK_SUCCESSOR_PLANNING_KEY,
    REQUIRED_BOOK_SUCCESSOR_REVISION_KEY,
})
_CHILD_PLANNING_KEYS = frozenset({
    "required_chapter_review_authorization",
    "required_chapter_review_pipeline_revision",
    "required_chapter_state_authorization",
    "required_chapter_state_pipeline_revision",
    "required_chapter_finalization_authorization",
    "required_chapter_finalization_pipeline_revision",
})
_FORBIDDEN_PLANNING_KEYS = frozenset({
    "chapter_candidate_pipeline_revision",
    "chapter_candidate_repair_authorization",
    "chapter_candidate_job_execution_authorization",
    "chapter_finalization_authorization",
    "prose_continuation_authorization",
})


class RequiredBookSuccessorConflict(ValueError):
    """The root authority, ordered child evidence or final audit diverged."""


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


def required_book_successor_digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _root_worklist(
    review: RequiredChapterReviewAuthorization,
) -> list[dict[str, Any]]:
    return [
        {
            "chapter_id": item.chapter_id,
            "volume_id": item.volume_id,
            "order_index": item.order_index,
            "has_outline": True,
            "has_content": False,
        }
        for item in review.chapters
    ]


class RequiredBookSuccessorAuthorization(_Closed):
    schema_version: Literal["required_book_successor_authorization.v1"] = (
        "required_book_successor_authorization.v1"
    )
    protocol_revision: Literal["required-book-successor-r1"] = (
        REQUIRED_BOOK_SUCCESSOR_PIPELINE_REVISION
    )
    contract_digest: str = Field(pattern=_SHA256)
    novel_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    authorization_revision: int = Field(ge=1, le=_MAX)
    base_narrative_revision: int = Field(ge=0, le=_MAX)
    expected_final_narrative_revision: int = Field(ge=1, le=_MAX)
    created_at: datetime
    deadline_at: datetime
    work_digest: str = Field(pattern=_SHA256)
    review_template: RequiredChapterReviewAuthorization
    state_generation: RequiredGenerationPlanSnapshot
    state_maximum_provider_attempts_per_chapter: int = Field(ge=1, le=_MAX)
    state_maximum_tokens_per_chapter: int = Field(ge=1, le=_MAX)
    state_maximum_serial_seconds_per_chapter: int = Field(ge=1, le=_MAX)
    maximum_provider_attempts_total: int = Field(ge=1, le=_MAX)
    maximum_tokens_total: int = Field(ge=1, le=_MAX)
    maximum_serial_seconds_total: int = Field(ge=1, le=_MAX)
    token_budget: int = Field(ge=1, le=_MAX)
    provider_bounds: tuple[RequiredProviderBound, ...] = Field(
        min_length=1,
        max_length=32,
    )
    formal_write_count: int = Field(ge=1, le=1000)
    final_audit_required: Literal[True] = True
    can_write_formal_prose: Literal[True] = True
    can_accept_formal_state: Literal[True] = True
    recovery_checkpoint: Literal["none", "before_first_child"]

    @model_validator(mode="after")
    def validate_authority(self) -> "RequiredBookSuccessorAuthorization":
        review = self.review_template
        chapter_count = len(review.chapters)
        state_plan = self.state_generation.thaw()
        state_budget = structured_call_budget(state_plan)
        expected_state_attempts = (
            state_budget.max_paid_attempts * MAX_REQUIRED_STATE_CALLS
        )
        expected_state_tokens = (
            state_budget.max_tokens_per_call * MAX_REQUIRED_STATE_CALLS
        )
        expected_state_seconds = state_plan.timeout_seconds * MAX_REQUIRED_STATE_CALLS
        expected_providers: dict[str, list[int]] = {}
        for bound in review.provider_bounds:
            expected_providers[bound.provider_alias] = [
                bound.maximum_paid_attempts_total,
                bound.maximum_tokens_total,
            ]
        for bound in state_budget.provider_bounds:
            values = expected_providers.setdefault(bound.provider_alias, [0, 0])
            values[0] += bound.paid_attempts * MAX_REQUIRED_STATE_CALLS * chapter_count
            values[1] += bound.tokens * MAX_REQUIRED_STATE_CALLS * chapter_count
        expected_bounds = tuple(
            RequiredProviderBound(
                provider_alias=alias,
                maximum_paid_attempts_total=values[0],
                maximum_tokens_total=values[1],
            )
            for alias, values in sorted(expected_providers.items())
        )
        if (
            self.created_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or review.scope != "book"
            or review.volume_id is not None
            or review.novel_id != self.novel_id
            or review.owner_id != self.owner_id
            or review.authorization_revision != self.authorization_revision
            or review.narrative_revision != self.base_narrative_revision
            or self.work_digest
            != required_book_successor_digest(_root_worklist(review))
            or review.created_at != self.created_at
            or review.deadline_at != self.deadline_at
            or self.state_generation.call_kind != "structured"
            or (self.state_generation.workflow, self.state_generation.step)
            != _STATE_TARGET
            or self.formal_write_count != chapter_count
            or self.expected_final_narrative_revision
            != self.base_narrative_revision + chapter_count
            or self.state_maximum_provider_attempts_per_chapter
            != expected_state_attempts
            or self.state_maximum_tokens_per_chapter != expected_state_tokens
            or self.state_maximum_serial_seconds_per_chapter
            != expected_state_seconds
            or self.maximum_provider_attempts_total
            != review.maximum_provider_attempts_total
            + expected_state_attempts * chapter_count
            or self.maximum_tokens_total
            != review.maximum_tokens_total + expected_state_tokens * chapter_count
            or self.maximum_serial_seconds_total
            != review.maximum_serial_seconds_total
            + expected_state_seconds * chapter_count
            or self.token_budget < self.maximum_tokens_total
            or self.deadline_at
            < self.created_at + timedelta(seconds=self.maximum_serial_seconds_total)
            or self.provider_bounds != expected_bounds
        ):
            raise ValueError("required_book_successor_authorization_invalid")
        identity = self.model_dump(mode="python", exclude={"contract_digest"})
        if required_book_successor_digest(identity) != self.contract_digest:
            raise ValueError("required_book_successor_contract_digest_changed")
        return self

    def chapter(self, ordinal: int):
        if type(ordinal) is not int or not 0 <= ordinal < len(self.review_template.chapters):
            raise ValueError("required_book_successor_chapter_not_authorized")
        return self.review_template.chapters[ordinal]


class RequiredBookSuccessorProviderUsageBound(_Closed):
    """Priceable input/output ceiling for one Provider in the root grant."""

    schema_version: Literal["required_book_successor_provider_usage.v1"] = (
        "required_book_successor_provider_usage.v1"
    )
    provider_alias: str = Field(min_length=1, max_length=160)
    maximum_paid_attempts: int = Field(ge=1, le=_MAX)
    maximum_input_tokens: int = Field(ge=1, le=_MAX)
    maximum_output_tokens: int = Field(ge=1, le=_MAX)
    maximum_total_tokens: int = Field(ge=1, le=_MAX)

    @model_validator(mode="after")
    def validate_total(self) -> "RequiredBookSuccessorProviderUsageBound":
        if self.maximum_total_tokens != (
            self.maximum_input_tokens + self.maximum_output_tokens
        ):
            raise ValueError("required_book_successor_provider_usage_invalid")
        return self


def required_book_successor_provider_usage_bounds(
    authority: RequiredBookSuccessorAuthorization,
) -> tuple[RequiredBookSuccessorProviderUsageBound, ...]:
    """Split the already-authorized root total into priceable token classes.

    This is a deterministic projection of the frozen child contracts.  It does
    not alter capacity or grant execution rights, and it must reconcile exactly
    with the root's existing per-Provider attempt/total-token ledger.
    """

    if not isinstance(authority, RequiredBookSuccessorAuthorization):
        raise ValueError("required_book_successor_authority_required")
    totals: dict[str, list[int]] = {}

    def add(
        provider_alias: str,
        *,
        attempts: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if (
            not str(provider_alias or "").strip()
            or type(attempts) is not int
            or type(input_tokens) is not int
            or type(output_tokens) is not int
            or min(attempts, input_tokens, output_tokens) < 0
        ):
            raise RequiredBookSuccessorConflict(
                "required book successor usage projection is invalid"
            )
        current = totals.setdefault(str(provider_alias).strip(), [0, 0, 0])
        current[0] += attempts
        current[1] += input_tokens
        current[2] += output_tokens

    review = authority.review_template
    for chapter in review.chapters:
        initial = chapter.initial_prose
        add(
            initial.provider_alias,
            attempts=initial.max_calls,
            input_tokens=initial.max_calls * initial.input_tokens_per_call,
            output_tokens=initial.max_calls * initial.output_tokens_per_call,
        )

    chapter_count = len(review.chapters)
    capacity = review.review.capacity
    review_attempts = chapter_count * capacity.max_review_attempts_per_chapter
    add(
        review.independent_review.provider_alias,
        attempts=review_attempts,
        input_tokens=review_attempts * capacity.input_tokens_per_attempt,
        output_tokens=review_attempts * capacity.output_tokens_per_attempt,
    )

    rewrite = review.rewrite
    planner_calls = (
        chapter_count * rewrite.max_rewrites * rewrite.max_planner_calls
    )
    planner_attempts = planner_calls * rewrite.planner.max_attempts
    add(
        rewrite.planner.provider_alias,
        attempts=planner_attempts,
        input_tokens=planner_attempts * rewrite.planner.input_tokens,
        output_tokens=planner_attempts * rewrite.planner.output_tokens,
    )
    rewrite_calls = (
        chapter_count * rewrite.max_rewrites * rewrite.max_tool_calls
    )
    rewrite_attempts = rewrite_calls * rewrite.rewrite.max_attempts
    add(
        rewrite.rewrite.provider_alias,
        attempts=rewrite_attempts,
        input_tokens=rewrite_attempts * rewrite.rewrite.input_tokens,
        output_tokens=rewrite_attempts * rewrite.rewrite.output_tokens,
    )

    state_budget = structured_call_budget(authority.state_generation.thaw())
    state_logical_calls = chapter_count * MAX_REQUIRED_STATE_CALLS
    state_attempts = state_logical_calls * state_budget.max_paid_attempts
    # Root successor plans forbid a format reviewer, so the complete state
    # attempt envelope belongs to the one frozen state Provider.
    if len(state_budget.provider_bounds) != 1:
        raise RequiredBookSuccessorConflict(
            "required book successor state usage is not single-provider"
        )
    add(
        state_budget.provider_bounds[0].provider_alias,
        attempts=state_attempts,
        input_tokens=(
            state_attempts * state_budget.max_input_tokens_per_attempt
        ),
        output_tokens=(
            state_attempts * state_budget.max_output_tokens_per_attempt
        ),
    )

    result = tuple(
        RequiredBookSuccessorProviderUsageBound(
            provider_alias=alias,
            maximum_paid_attempts=values[0],
            maximum_input_tokens=values[1],
            maximum_output_tokens=values[2],
            maximum_total_tokens=values[1] + values[2],
        )
        for alias, values in sorted(totals.items())
    )
    expected = {
        bound.provider_alias: (
            bound.maximum_paid_attempts_total,
            bound.maximum_tokens_total,
        )
        for bound in authority.provider_bounds
    }
    projected = {
        bound.provider_alias: (
            bound.maximum_paid_attempts,
            bound.maximum_total_tokens,
        )
        for bound in result
    }
    if projected != expected:
        raise RequiredBookSuccessorConflict(
            "required book successor usage projection changed"
        )
    return result


class RequiredBookSuccessorAction(_Closed):
    schema_version: Literal["required_book_successor_action.v1"] = (
        "required_book_successor_action.v1"
    )
    action_digest: str = Field(pattern=_SHA256)
    coordinator_job_id: str = Field(pattern=_OBJECT_ID)
    coordinator_readiness_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    stage: Literal["review", "state", "finalization", "book_audit"]
    chapter_ordinal: int | None = Field(default=None, ge=0, le=999)
    chapter_id: str | None = Field(default=None, pattern=_OBJECT_ID)
    expected_narrative_revision: int = Field(ge=0, le=_MAX)
    predecessor_job_ids: tuple[str, ...] = Field(max_length=2)
    predecessor_result_digests: tuple[str, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_action(self) -> "RequiredBookSuccessorAction":
        chapter_stage = self.stage != "book_audit"
        if (
            chapter_stage != (self.chapter_ordinal is not None)
            or chapter_stage != (self.chapter_id is not None)
            or self.stage == "review"
            and (self.predecessor_job_ids or self.predecessor_result_digests)
            or self.stage == "state"
            and (
                len(self.predecessor_job_ids) != 1
                or len(self.predecessor_result_digests) != 1
            )
            or self.stage == "finalization"
            and (
                len(self.predecessor_job_ids) != 2
                or len(self.predecessor_result_digests) != 2
            )
            or self.stage == "book_audit"
            and (
                self.predecessor_job_ids
                or not self.predecessor_result_digests
            )
            or len(set(self.predecessor_job_ids)) != len(self.predecessor_job_ids)
        ):
            raise ValueError("required_book_successor_action_invalid")
        identity = self.model_dump(mode="python", exclude={"action_digest"})
        if required_book_successor_digest(identity) != self.action_digest:
            raise ValueError("required_book_successor_action_digest_changed")
        return self


class RequiredBookSuccessorRecoveryCheckpoint(_Closed):
    """Durable proof that the root stopped before its first child.

    Execution epochs are worker identities, not recovery evidence.  Persisting
    this marker lets a replacement worker distinguish "the checkpoint was
    already observed" from "the first worker died before it could pause".
    """

    schema_version: Literal[
        "required_book_successor_recovery_checkpoint.v1"
    ] = "required_book_successor_recovery_checkpoint.v1"
    checkpoint_digest: str = Field(pattern=_SHA256)
    coordinator_job_id: str = Field(pattern=_OBJECT_ID)
    coordinator_readiness_digest: str = Field(pattern=_SHA256)
    journal_digest: str = Field(pattern=_SHA256)
    execution_epoch: int = Field(ge=1, le=_MAX)
    checkpoint: Literal["before_first_child"] = "before_first_child"

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "RequiredBookSuccessorRecoveryCheckpoint":
        identity = self.model_dump(mode="python", exclude={"checkpoint_digest"})
        if required_book_successor_digest(identity) != self.checkpoint_digest:
            raise ValueError(
                "required_book_successor_recovery_checkpoint_changed"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        coordinator_job_id: str,
        coordinator_readiness_digest: str,
        journal_digest: str,
        execution_epoch: int,
    ) -> "RequiredBookSuccessorRecoveryCheckpoint":
        identity = {
            "schema_version": (
                "required_book_successor_recovery_checkpoint.v1"
            ),
            "coordinator_job_id": str(coordinator_job_id),
            "coordinator_readiness_digest": str(
                coordinator_readiness_digest
            ),
            "journal_digest": str(journal_digest),
            "execution_epoch": execution_epoch,
            "checkpoint": "before_first_child",
        }
        return cls(
            **identity,
            checkpoint_digest=required_book_successor_digest(identity),
        )


class RequiredBookSuccessorProviderUsage(_Closed):
    provider_alias: str = Field(min_length=1, max_length=160)
    paid_attempts: int = Field(ge=0, le=_MAX)
    tokens: int = Field(ge=0, le=_MAX)


class RequiredBookSuccessorStageRecord(_Closed):
    schema_version: Literal["required_book_successor_stage.v1"] = (
        "required_book_successor_stage.v1"
    )
    action_digest: str = Field(pattern=_SHA256)
    stage: Literal["review", "state", "finalization"]
    chapter_ordinal: int = Field(ge=0, le=999)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    child_job_id: str = Field(pattern=_OBJECT_ID)
    child_readiness_digest: str = Field(pattern=_SHA256)
    result_digest: str = Field(pattern=_SHA256)
    narrative_revision_before: int = Field(ge=0, le=_MAX)
    narrative_revision_after: int = Field(ge=0, le=_MAX)
    source_content_digest: str = Field(pattern=_SHA256)
    provider_usage: tuple[RequiredBookSuccessorProviderUsage, ...] = Field(
        max_length=32
    )

    @model_validator(mode="after")
    def validate_stage(self) -> "RequiredBookSuccessorStageRecord":
        expected_after = (
            self.narrative_revision_before + 1
            if self.stage == "finalization"
            else self.narrative_revision_before
        )
        if (
            self.narrative_revision_after != expected_after
            or len({item.provider_alias for item in self.provider_usage})
            != len(self.provider_usage)
            or self.stage == "finalization" and self.provider_usage
            or self.stage != "finalization" and not self.provider_usage
        ):
            raise ValueError("required_book_successor_stage_invalid")
        return self


class RequiredBookSuccessorJournal(_Closed):
    schema_version: Literal["required_book_successor_journal.v1"] = (
        "required_book_successor_journal.v1"
    )
    journal_digest: str = Field(pattern=_SHA256)
    coordinator_job_id: str = Field(pattern=_OBJECT_ID)
    coordinator_readiness_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    base_narrative_revision: int = Field(ge=0, le=_MAX)
    expected_narrative_revision: int = Field(ge=0, le=_MAX)
    chapter_count: int = Field(ge=1, le=1000)
    phase: Literal[
        "review",
        "state",
        "finalization",
        "book_audit",
        "completed",
        "blocked",
    ]
    stages: tuple[RequiredBookSuccessorStageRecord, ...] = Field(max_length=3000)
    audit_digest: str | None = Field(default=None, pattern=_SHA256)
    blocking_reason: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_journal(self) -> "RequiredBookSuccessorJournal":
        if len({item.child_job_id for item in self.stages}) != len(self.stages):
            raise ValueError("required_book_successor_child_job_reused")
        for index, record in enumerate(self.stages):
            expected_stage = ("review", "state", "finalization")[index % 3]
            expected_ordinal = index // 3
            expected_before = self.base_narrative_revision + expected_ordinal
            if (
                record.stage != expected_stage
                or record.chapter_ordinal != expected_ordinal
                or record.narrative_revision_before != expected_before
            ):
                raise ValueError("required_book_successor_stage_order_changed")
        completed_chapters, partial = divmod(len(self.stages), 3)
        if completed_chapters > self.chapter_count:
            raise ValueError("required_book_successor_stage_overflow")
        expected_revision = self.base_narrative_revision + completed_chapters
        derived_phase: str
        if completed_chapters == self.chapter_count:
            if partial:
                raise ValueError("required_book_successor_stage_overflow")
            derived_phase = "book_audit"
        else:
            derived_phase = ("review", "state", "finalization")[partial]
        if (
            self.expected_narrative_revision != expected_revision
            or self.phase not in {derived_phase, "completed", "blocked"}
            or self.phase == "completed"
            and (derived_phase != "book_audit" or self.audit_digest is None)
            or self.phase != "completed" and self.audit_digest is not None
            or self.phase == "blocked"
            and (
                self.blocking_reason is None
                or _SAFE_REASON.fullmatch(self.blocking_reason) is None
            )
            or self.phase != "blocked" and self.blocking_reason is not None
        ):
            raise ValueError("required_book_successor_journal_state_invalid")
        identity = self.model_dump(mode="python", exclude={"journal_digest"})
        if required_book_successor_digest(identity) != self.journal_digest:
            raise ValueError("required_book_successor_journal_digest_changed")
        return self


def _provider_bounds(
    review: RequiredChapterReviewAuthorization,
    state_plan: GenerationPlan,
) -> tuple[RequiredProviderBound, ...]:
    totals = {
        item.provider_alias: [
            item.maximum_paid_attempts_total,
            item.maximum_tokens_total,
        ]
        for item in review.provider_bounds
    }
    state = structured_call_budget(state_plan)
    chapter_count = len(review.chapters)
    for item in state.provider_bounds:
        values = totals.setdefault(item.provider_alias, [0, 0])
        values[0] += item.paid_attempts * MAX_REQUIRED_STATE_CALLS * chapter_count
        values[1] += item.tokens * MAX_REQUIRED_STATE_CALLS * chapter_count
    return tuple(
        RequiredProviderBound(
            provider_alias=alias,
            maximum_paid_attempts_total=values[0],
            maximum_tokens_total=values[1],
        )
        for alias, values in sorted(totals.items())
    )


def build_required_book_successor_authorization(
    review_readiness: Mapping[str, Any],
    *,
    state_plan: GenerationPlan,
    token_budget: int,
    recovery_checkpoint: Literal[
        "none",
        "before_first_child",
    ] = REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_NONE,
) -> RequiredBookSuccessorAuthorization:
    review = validate_required_chapter_review_readiness(review_readiness)
    if review.scope != "book" or review.volume_id is not None:
        raise ValueError("required_book_successor_requires_book_scope")
    snapshot = RequiredGenerationPlanSnapshot.freeze(
        state_plan,
        call_kind="structured",
        expected_target=_STATE_TARGET,
    )
    state_budget = structured_call_budget(state_plan)
    chapter_count = len(review.chapters)
    state_attempts = state_budget.max_paid_attempts * MAX_REQUIRED_STATE_CALLS
    state_tokens = state_budget.max_tokens_per_call * MAX_REQUIRED_STATE_CALLS
    state_seconds = state_plan.timeout_seconds * MAX_REQUIRED_STATE_CALLS
    identity = {
        "schema_version": "required_book_successor_authorization.v1",
        "protocol_revision": REQUIRED_BOOK_SUCCESSOR_PIPELINE_REVISION,
        "novel_id": review.novel_id,
        "owner_id": review.owner_id,
        "authorization_revision": review.authorization_revision,
        "base_narrative_revision": review.narrative_revision,
        "expected_final_narrative_revision": (
            review.narrative_revision + chapter_count
        ),
        "created_at": review.created_at,
        "deadline_at": review.deadline_at,
        "work_digest": required_book_successor_digest(
            _root_worklist(review)
        ),
        "review_template": review.model_dump(mode="python"),
        "state_generation": snapshot.model_dump(mode="python"),
        "state_maximum_provider_attempts_per_chapter": state_attempts,
        "state_maximum_tokens_per_chapter": state_tokens,
        "state_maximum_serial_seconds_per_chapter": state_seconds,
        "maximum_provider_attempts_total": (
            review.maximum_provider_attempts_total + state_attempts * chapter_count
        ),
        "maximum_tokens_total": (
            review.maximum_tokens_total + state_tokens * chapter_count
        ),
        "maximum_serial_seconds_total": (
            review.maximum_serial_seconds_total + state_seconds * chapter_count
        ),
        "token_budget": token_budget,
        "provider_bounds": tuple(
            item.model_dump(mode="python")
            for item in _provider_bounds(review, state_plan)
        ),
        "formal_write_count": chapter_count,
        "final_audit_required": True,
        "can_write_formal_prose": True,
        "can_accept_formal_state": True,
        "recovery_checkpoint": recovery_checkpoint,
    }
    return RequiredBookSuccessorAuthorization(
        **identity,
        contract_digest=required_book_successor_digest(identity),
    )


def prepare_required_book_successor_readiness(
    review_readiness: Mapping[str, Any],
    *,
    state_plan: GenerationPlan,
    token_budget: int,
    recovery_checkpoint: Literal[
        "none",
        "before_first_child",
    ] = REQUIRED_BOOK_SUCCESSOR_RECOVERY_CHECKPOINT_NONE,
) -> dict[str, Any]:
    authorization = build_required_book_successor_authorization(
        review_readiness,
        state_plan=state_plan,
        token_budget=token_budget,
        recovery_checkpoint=recovery_checkpoint,
    )
    issues = [
        deepcopy(item)
        for item in (review_readiness.get("issues") or [])
        if isinstance(item, Mapping)
        and item.get("code") != REQUIRED_REVIEW_ACKNOWLEDGEMENT
    ]
    if not any(
        item.get("code") == REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT
        for item in issues
    ):
        issues.append({
            "code": REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT,
            "level": "warning_requires_ack",
            "details": {
                "chapter_count": authorization.formal_write_count,
                "final_audit_required": True,
                "can_write_formal_prose": True,
                "can_accept_formal_state": True,
            },
            "action_codes": ["book_successor_formal_execution"],
        })
    planning = {
        REQUIRED_BOOK_SUCCESSOR_REVISION_KEY: (
            REQUIRED_BOOK_SUCCESSOR_PIPELINE_REVISION
        ),
        REQUIRED_BOOK_SUCCESSOR_PLANNING_KEY: authorization.model_dump(
            mode="python"
        ),
        "attempt_capacity": authorization.maximum_provider_attempts_total,
        "providers": [item.provider_alias for item in authorization.provider_bounds],
        "batch_generation_budget_coverage": {
            "schema_version": "required_book_successor_budget_coverage.v1",
            "maximum_provider_attempts_total": (
                authorization.maximum_provider_attempts_total
            ),
            "maximum_tokens_total": authorization.maximum_tokens_total,
            "provider_bounds": [
                item.model_dump(mode="python")
                for item in authorization.provider_bounds
            ],
            "token_bound_known": True,
            "token_budget": authorization.token_budget,
            "covers_full_job_authority": True,
            "can_write_formal_prose": True,
            "can_accept_formal_state": True,
            "final_audit_required": True,
        },
    }
    snapshot = {
        "version": 2,
        "novel_id": deepcopy(review_readiness.get("novel_id")),
        "scope": "book",
        "volume_id": None,
        "outline_deviation_policy": deepcopy(
            review_readiness.get("outline_deviation_policy")
        ),
        "work": {"chapters": _root_worklist(authorization.review_template)},
        "resources": deepcopy(review_readiness.get("resources") or {}),
        "active_proposal": deepcopy(review_readiness.get("active_proposal")),
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
    readiness = {
        **snapshot,
        "status": status,
        "digest": required_book_successor_digest(snapshot),
    }
    validate_required_book_successor_readiness(readiness)
    return readiness


def parse_required_book_successor_authorization(
    value: Any,
) -> RequiredBookSuccessorAuthorization:
    candidate = deepcopy(value)
    if isinstance(candidate, dict) and isinstance(
        candidate.get("provider_bounds"), list
    ):
        candidate["provider_bounds"] = tuple(candidate["provider_bounds"])
    try:
        parsed = RequiredBookSuccessorAuthorization.model_validate(candidate)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_authorization_invalid"
        ) from exc
    if required_book_successor_digest(value) != required_book_successor_digest(
        parsed.model_dump(mode="python")
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_authorization_not_canonical"
        )
    return parsed


def required_book_successor_planning_present(planning: Any) -> bool:
    return isinstance(planning, Mapping) and any(
        key in planning for key in _PLANNING_KEYS
    )


def readiness_uses_required_book_successor(readiness: Any) -> bool:
    if not isinstance(readiness, Mapping):
        return False
    planning = readiness.get("planning")
    if not required_book_successor_planning_present(planning):
        return False
    validate_required_book_successor_readiness(readiness)
    return True


def validate_required_book_successor_readiness(
    readiness: Mapping[str, Any],
) -> RequiredBookSuccessorAuthorization:
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_readiness_invalid"
        )
    if (
        planning.get(REQUIRED_BOOK_SUCCESSOR_REVISION_KEY)
        != REQUIRED_BOOK_SUCCESSOR_PIPELINE_REVISION
        or REQUIRED_BOOK_SUCCESSOR_PLANNING_KEY not in planning
        or any(key in planning for key in _CHILD_PLANNING_KEYS)
        or any(key in planning for key in _FORBIDDEN_PLANNING_KEYS)
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_mode_conflict"
        )
    authorization = parse_required_book_successor_authorization(
        planning[REQUIRED_BOOK_SUCCESSOR_PLANNING_KEY]
    )
    resources = readiness.get("resources")
    work = readiness.get("work")
    raw_chapters = work.get("chapters") if isinstance(work, Mapping) else None
    coverage = planning.get("batch_generation_budget_coverage")
    issues = readiness.get("issues")
    if (
        readiness.get("version") != 2
        or readiness.get("novel_id") != authorization.novel_id
        or readiness.get("scope") != "book"
        or readiness.get("volume_id") is not None
        or not isinstance(resources, Mapping)
        or resources.get("owner_id") != authorization.owner_id
        or resources.get("narrative_revision")
        != authorization.base_narrative_revision
        or not isinstance(raw_chapters, list)
        or required_book_successor_digest(raw_chapters)
        != authorization.work_digest
        or not isinstance(issues, list)
        or not any(
            isinstance(item, Mapping)
            and item.get("code") == REQUIRED_BOOK_SUCCESSOR_ACKNOWLEDGEMENT
            and item.get("level") == "warning_requires_ack"
            for item in issues
        )
        or planning.get("attempt_capacity")
        != authorization.maximum_provider_attempts_total
        or planning.get("providers")
        != [item.provider_alias for item in authorization.provider_bounds]
        or not isinstance(coverage, Mapping)
        or coverage.get("schema_version")
        != "required_book_successor_budget_coverage.v1"
        or coverage.get("maximum_provider_attempts_total")
        != authorization.maximum_provider_attempts_total
        or coverage.get("maximum_tokens_total")
        != authorization.maximum_tokens_total
        or coverage.get("token_budget") != authorization.token_budget
        or coverage.get("covers_full_job_authority") is not True
        or coverage.get("can_write_formal_prose") is not True
        or coverage.get("can_accept_formal_state") is not True
        or coverage.get("final_audit_required") is not True
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_readiness_changed"
        )
    target_ids = [
        str(item.get("chapter_id") or "")
        for item in raw_chapters
        if isinstance(item, Mapping) and item.get("has_content") is False
    ]
    if target_ids != [item.chapter_id for item in authorization.review_template.chapters]:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_worklist_changed"
        )
    snapshot = {
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
    if readiness.get("digest") != required_book_successor_digest(snapshot):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_readiness_digest_changed"
        )
    return authorization


def parse_required_book_successor_journal(
    value: Any,
) -> RequiredBookSuccessorJournal:
    candidate = deepcopy(value)
    if isinstance(candidate, dict) and isinstance(candidate.get("stages"), list):
        stages = []
        for raw_stage in candidate["stages"]:
            stage = deepcopy(raw_stage)
            if isinstance(stage, dict) and isinstance(
                stage.get("provider_usage"), list
            ):
                stage["provider_usage"] = tuple(stage["provider_usage"])
            stages.append(stage)
        candidate["stages"] = tuple(stages)
    try:
        parsed = RequiredBookSuccessorJournal.model_validate(candidate)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_journal_invalid"
        ) from exc
    if required_book_successor_digest(value) != required_book_successor_digest(
        parsed.model_dump(mode="python")
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_journal_not_canonical"
        )
    return parsed


def parse_required_book_successor_recovery_checkpoint(
    value: Any,
) -> RequiredBookSuccessorRecoveryCheckpoint:
    try:
        parsed = RequiredBookSuccessorRecoveryCheckpoint.model_validate(
            deepcopy(value)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_recovery_checkpoint_invalid"
        ) from exc
    if required_book_successor_digest(value) != required_book_successor_digest(
        parsed.model_dump(mode="python")
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_recovery_checkpoint_not_canonical"
        )
    return parsed


def parse_required_book_successor_action(
    value: Any,
) -> RequiredBookSuccessorAction:
    candidate = deepcopy(value)
    if isinstance(candidate, dict):
        for key in ("predecessor_job_ids", "predecessor_result_digests"):
            if isinstance(candidate.get(key), list):
                candidate[key] = tuple(candidate[key])
    try:
        parsed = RequiredBookSuccessorAction.model_validate(candidate)
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_child_binding_invalid"
        ) from exc
    if required_book_successor_digest(value) != required_book_successor_digest(
        parsed.model_dump(mode="python")
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_child_binding_not_canonical"
        )
    return parsed


def _new_journal(**identity: Any) -> RequiredBookSuccessorJournal:
    return RequiredBookSuccessorJournal(
        **identity,
        journal_digest=required_book_successor_digest(identity),
    )


def _settled_provider_usage(
    job: Mapping[str, Any],
) -> tuple[RequiredBookSuccessorProviderUsage, ...]:
    if (
        job.get("has_uncertain_attempts") is not False
        or job.get("active_token_reservations") not in (None, [])
        or job.get("tokens_reserved") not in (None, 0)
        or job.get("attempt_reservation") is not None
    ):
        raise RequiredBookSuccessorConflict(
            "required_book_successor_child_attempts_unsettled"
        )
    attempt_slots = job.get("attempt_slots", [])
    try:
        validate_launchable_attempt_ledgers(
            attempt_slots=attempt_slots,
            active_token_reservations=job.get(
                "active_token_reservations",
                [],
            ),
            usage_attempt_ids=job.get("usage_attempt_ids", []),
            attempt_capacity=job.get("usage_attempt_capacity", 0),
            attempts_claimed=job.get("usage_attempt_claimed", 0),
            tokens_used=job.get("tokens_used", 0),
            tokens_reserved=job.get("tokens_reserved", 0),
            token_budget=job.get("token_budget"),
            maximum_active_reservations=0,
        )
    except ValueError as exc:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_child_attempt_invalid"
        ) from exc
    totals: dict[str, list[int]] = {}
    token_total = 0
    for raw in attempt_slots:
        if not isinstance(raw, Mapping):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_attempt_invalid"
            )
        state = str(raw.get("state") or "")
        if state == "released_pre_dispatch":
            continue
        usage = raw.get("usage")
        charged = raw.get("charged_tokens")
        alias = str(raw.get("provider_alias") or "")
        if (
            state != "accounted"
            or not alias
            or not isinstance(usage, Mapping)
            or type(usage.get("total_tokens")) is not int
            or usage.get("total_tokens") < 0
            or charged != usage.get("total_tokens")
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_attempt_invalid"
            )
        values = totals.setdefault(alias, [0, 0])
        values[0] += 1
        values[1] += int(charged)
        token_total += int(charged)
    if job.get("tokens_used") != token_total:
        raise RequiredBookSuccessorConflict(
            "required_book_successor_child_tokens_changed"
        )
    return tuple(
        RequiredBookSuccessorProviderUsage(
            provider_alias=alias,
            paid_attempts=values[0],
            tokens=values[1],
        )
        for alias, values in sorted(totals.items())
    )


class RequiredBookSuccessorCoordinator:
    """Pure transition Module over exact child Job results and final audit."""

    def __init__(
        self,
        *,
        coordinator_job_id: str,
        readiness: Mapping[str, Any],
    ) -> None:
        self.coordinator_job_id = str(coordinator_job_id)
        if re.fullmatch(_OBJECT_ID, self.coordinator_job_id) is None:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_job_id_invalid"
            )
        self.readiness = deepcopy(dict(readiness))
        self.authorization = validate_required_book_successor_readiness(readiness)
        self.readiness_digest = str(readiness.get("digest") or "")

    def initial_journal(self) -> RequiredBookSuccessorJournal:
        return _new_journal(
            schema_version="required_book_successor_journal.v1",
            coordinator_job_id=self.coordinator_job_id,
            coordinator_readiness_digest=self.readiness_digest,
            authorization_contract_digest=self.authorization.contract_digest,
            base_narrative_revision=self.authorization.base_narrative_revision,
            expected_narrative_revision=(
                self.authorization.base_narrative_revision
            ),
            chapter_count=len(self.authorization.review_template.chapters),
            phase="review",
            stages=(),
            audit_digest=None,
            blocking_reason=None,
        )

    def _validate_journal(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
    ) -> RequiredBookSuccessorJournal:
        parsed = (
            journal
            if isinstance(journal, RequiredBookSuccessorJournal)
            else parse_required_book_successor_journal(journal)
        )
        if (
            parsed.coordinator_job_id != self.coordinator_job_id
            or parsed.coordinator_readiness_digest != self.readiness_digest
            or parsed.authorization_contract_digest
            != self.authorization.contract_digest
            or parsed.base_narrative_revision
            != self.authorization.base_narrative_revision
            or parsed.chapter_count != len(self.authorization.review_template.chapters)
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_journal_binding_changed"
            )
        provider_totals: dict[str, list[int]] = {}
        for index, record in enumerate(parsed.stages):
            ordinal = index // 3
            stage = ("review", "state", "finalization")[index % 3]
            chapter = self.authorization.chapter(ordinal)
            recent = parsed.stages[ordinal * 3:index]
            action_identity = {
                "schema_version": "required_book_successor_action.v1",
                "coordinator_job_id": self.coordinator_job_id,
                "coordinator_readiness_digest": self.readiness_digest,
                "authorization_contract_digest": (
                    self.authorization.contract_digest
                ),
                "stage": stage,
                "chapter_ordinal": ordinal,
                "chapter_id": chapter.chapter_id,
                "expected_narrative_revision": (
                    self.authorization.base_narrative_revision + ordinal
                ),
                "predecessor_job_ids": tuple(
                    item.child_job_id for item in recent
                ),
                "predecessor_result_digests": tuple(
                    item.result_digest for item in recent
                ),
            }
            source_digests = {
                item.source_content_digest for item in recent
            }
            if (
                record.stage != stage
                or record.chapter_id != chapter.chapter_id
                or record.action_digest
                != required_book_successor_digest(action_identity)
                or source_digests
                and record.source_content_digest not in source_digests
            ):
                raise RequiredBookSuccessorConflict(
                    "required_book_successor_journal_stage_changed"
                )
            for usage in record.provider_usage:
                values = provider_totals.setdefault(
                    usage.provider_alias,
                    [0, 0],
                )
                values[0] += usage.paid_attempts
                values[1] += usage.tokens
        authorized = {
            item.provider_alias: item
            for item in self.authorization.provider_bounds
        }
        if (
            sum(values[0] for values in provider_totals.values())
            > self.authorization.maximum_provider_attempts_total
            or sum(values[1] for values in provider_totals.values())
            > self.authorization.maximum_tokens_total
            or any(
                alias not in authorized
                or values[0] > authorized[alias].maximum_paid_attempts_total
                or values[1] > authorized[alias].maximum_tokens_total
                for alias, values in provider_totals.items()
            )
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_journal_budget_changed"
            )
        return parsed

    def _required_action(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        stage: Literal["review", "state", "finalization", "book_audit"],
    ) -> RequiredBookSuccessorAction:
        action = self.next_action(journal)
        if action is None or action.stage != stage:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_action_not_expected"
            )
        return action

    @staticmethod
    def _authorize_derived_readiness(
        report: dict[str, Any],
        acknowledgement: str,
    ) -> dict[str, Any]:
        # Local import prevents the generic readiness Module from importing
        # this coordinator while it is still being initialized.
        from backend.services.generation.readiness import (
            generation_readiness_module,
        )

        return generation_readiness_module.authorize(
            report,
            supplied_digest=str(report.get("digest") or ""),
            acknowledged_warning_codes=(acknowledgement,),
        )

    def derive_review_readiness(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        chapter: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Derive one current-revision review child from the frozen root."""

        action = self._required_action(journal, "review")
        frozen = self.authorization.chapter(action.chapter_ordinal)
        raw_outline = chapter.get("outline")
        if (
            str(chapter.get("_id") or "") != action.chapter_id
            or str(chapter.get("volume_id") or "") != frozen.volume_id
            or type(chapter.get("order_index")) is not int
            or chapter.get("order_index") != frozen.order_index
            or str(chapter.get("content") or "").strip()
            or not isinstance(raw_outline, Mapping)
            or prose_revision(raw_outline) != frozen.outline_revision
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_review_source_changed"
            )
        work_item = {
            "chapter_id": action.chapter_id,
            "volume_id": frozen.volume_id,
            "order_index": frozen.order_index,
            "has_outline": True,
            "has_content": False,
        }
        base = {
            "version": 2,
            "novel_id": self.authorization.novel_id,
            "scope": "book",
            "volume_id": None,
            "outline_deviation_policy": self.readiness.get(
                "outline_deviation_policy"
            ),
            "work": {"chapters": [work_item]},
            "resources": {
                "owner_id": self.authorization.owner_id,
                "narrative_revision": action.expected_narrative_revision,
            },
            "active_proposal": None,
            "planning": {},
            "issues": [],
        }
        plan = self.authorization.review_template.plan()
        preview = build_required_chapter_review_authorization(
            base_readiness=base,
            chapters=[chapter],
            plan=plan,
            token_budget=self.authorization.token_budget,
            authorization_revision=self.authorization.authorization_revision,
            created_at=self.authorization.created_at,
            deadline_at=self.authorization.deadline_at,
        )
        report = prepare_required_chapter_review_readiness(
            base,
            chapters=[chapter],
            plan=plan,
            token_budget=preview.maximum_tokens_total,
            authorization_revision=self.authorization.authorization_revision,
            created_at=self.authorization.created_at,
            deadline_at=self.authorization.deadline_at,
        )
        return self._authorize_derived_readiness(
            report,
            REQUIRED_REVIEW_ACKNOWLEDGEMENT,
        )

    def derive_state_readiness(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        reviewed_job: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Derive the exact state child from the accepted review result."""

        action = self._required_action(journal, "state")
        if str(reviewed_job.get("_id") or "") != action.predecessor_job_ids[0]:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_state_predecessor_changed"
            )
        report = prepare_required_chapter_state_readiness(
            reviewed_job,
            state_plan=self.authorization.state_generation.thaw(),
            token_budget=self.authorization.state_maximum_tokens_per_chapter,
            authorization_revision=self.authorization.authorization_revision,
            created_at=self.authorization.created_at,
            deadline_at=self.authorization.deadline_at,
        )
        return self._authorize_derived_readiness(
            report,
            REQUIRED_STATE_CANDIDATE_ACKNOWLEDGEMENT,
        )

    def derive_finalization_readiness(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        state_job: Mapping[str, Any],
        reviewed_job: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Derive the zero-Provider formal child from both exact results."""

        action = self._required_action(journal, "finalization")
        if (
            str(reviewed_job.get("_id") or "")
            != action.predecessor_job_ids[0]
            or str(state_job.get("_id") or "")
            != action.predecessor_job_ids[1]
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_finalization_predecessor_changed"
            )
        report = prepare_required_chapter_finalization_readiness(
            state_job,
            reviewed_job,
            authorization_revision=self.authorization.authorization_revision,
            created_at=self.authorization.created_at,
            deadline_at=self.authorization.deadline_at,
        )
        return self._authorize_derived_readiness(
            report,
            REQUIRED_CHAPTER_FINALIZATION_ACKNOWLEDGEMENT,
        )

    def _validate_child_authority(
        self,
        action: RequiredBookSuccessorAction,
        job: Mapping[str, Any],
    ) -> None:
        readiness = job.get("readiness")
        if not isinstance(readiness, Mapping):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_readiness_invalid"
            )
        try:
            if action.stage == "review":
                child = validate_required_chapter_review_readiness(readiness)
                frozen = self.authorization.chapter(action.chapter_ordinal)
                valid = (
                    child.novel_id == self.authorization.novel_id
                    and child.owner_id == self.authorization.owner_id
                    and child.authorization_revision
                    == self.authorization.authorization_revision
                    and child.narrative_revision
                    == action.expected_narrative_revision
                    and child.created_at == self.authorization.created_at
                    and child.deadline_at == self.authorization.deadline_at
                    and child.chapters == [frozen]
                    and child.plan() == self.authorization.review_template.plan()
                    and child.token_budget == child.maximum_tokens_total
                )
            elif action.stage == "state":
                child = validate_required_chapter_state_readiness(readiness)
                valid = (
                    child.novel_id == self.authorization.novel_id
                    and child.owner_id == self.authorization.owner_id
                    and child.chapter_id == action.chapter_id
                    and child.authorization_revision
                    == self.authorization.authorization_revision
                    and child.narrative_revision
                    == action.expected_narrative_revision
                    and child.created_at == self.authorization.created_at
                    and child.deadline_at == self.authorization.deadline_at
                    and child.state_generation == self.authorization.state_generation
                    and child.predecessor_candidate.job_id
                    == action.predecessor_job_ids[0]
                    and child.predecessor_candidate.result_digest
                    == action.predecessor_result_digests[0]
                    and child.maximum_provider_attempts_total
                    == self.authorization.state_maximum_provider_attempts_per_chapter
                    and child.maximum_tokens_total
                    == self.authorization.state_maximum_tokens_per_chapter
                    and child.maximum_serial_seconds_total
                    == self.authorization.state_maximum_serial_seconds_per_chapter
                    and child.token_budget == child.maximum_tokens_total
                )
            else:
                child = validate_required_chapter_finalization_readiness(readiness)
                valid = (
                    child.novel_id == self.authorization.novel_id
                    and child.owner_id == self.authorization.owner_id
                    and child.chapter_id == action.chapter_id
                    and child.authorization_revision
                    == self.authorization.authorization_revision
                    and child.narrative_revision
                    == action.expected_narrative_revision
                    and child.created_at == self.authorization.created_at
                    and child.deadline_at == self.authorization.deadline_at
                    and child.reviewed_candidate.job_id
                    == action.predecessor_job_ids[0]
                    and child.reviewed_candidate.result_digest
                    == action.predecessor_result_digests[0]
                    and child.state_candidate.job_id
                    == action.predecessor_job_ids[1]
                    and child.state_candidate.result_digest
                    == action.predecessor_result_digests[1]
                    and child.maximum_provider_attempts_total == 0
                    and child.maximum_tokens_total == 0
                )
        except (TypeError, ValueError) as exc:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_readiness_invalid"
            ) from exc
        if not valid:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_authority_changed"
            )

    def validate_child_authority(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        action: RequiredBookSuccessorAction | Mapping[str, Any],
        job: Mapping[str, Any],
    ) -> RequiredBookSuccessorAction:
        """Validate a not-yet-executed child against the current root step."""

        current = self._validate_journal(journal)
        expected = self.next_action(current)
        parsed = (
            action
            if isinstance(action, RequiredBookSuccessorAction)
            else parse_required_book_successor_action(action)
        )
        if expected is None or expected != parsed or parsed.stage == "book_audit":
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_binding_changed"
            )
        stored = parse_required_book_successor_action(
            job.get("required_book_successor_action")
        )
        if stored != parsed:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_binding_changed"
            )
        self._validate_child_authority(parsed, job)
        return parsed

    def next_action(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
    ) -> RequiredBookSuccessorAction | None:
        current = self._validate_journal(journal)
        if current.phase in {"completed", "blocked"}:
            return None
        completed, partial = divmod(len(current.stages), 3)
        if current.phase == "book_audit":
            predecessors = tuple(
                item.result_digest
                for item in current.stages
                if item.stage == "finalization"
            )
            identity = {
                "schema_version": "required_book_successor_action.v1",
                "coordinator_job_id": self.coordinator_job_id,
                "coordinator_readiness_digest": self.readiness_digest,
                "authorization_contract_digest": (
                    self.authorization.contract_digest
                ),
                "stage": "book_audit",
                "chapter_ordinal": None,
                "chapter_id": None,
                "expected_narrative_revision": (
                    current.expected_narrative_revision
                ),
                "predecessor_job_ids": (),
                "predecessor_result_digests": predecessors,
            }
        else:
            chapter = self.authorization.chapter(completed)
            stage = ("review", "state", "finalization")[partial]
            recent = current.stages[completed * 3:]
            identity = {
                "schema_version": "required_book_successor_action.v1",
                "coordinator_job_id": self.coordinator_job_id,
                "coordinator_readiness_digest": self.readiness_digest,
                "authorization_contract_digest": (
                    self.authorization.contract_digest
                ),
                "stage": stage,
                "chapter_ordinal": completed,
                "chapter_id": chapter.chapter_id,
                "expected_narrative_revision": (
                    current.expected_narrative_revision
                ),
                "predecessor_job_ids": tuple(
                    item.child_job_id for item in recent
                ),
                "predecessor_result_digests": tuple(
                    item.result_digest for item in recent
                ),
            }
        return RequiredBookSuccessorAction(
            **identity,
            action_digest=required_book_successor_digest(identity),
        )

    def accept_child_job(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        job: Mapping[str, Any],
    ) -> RequiredBookSuccessorJournal:
        current = self._validate_journal(journal)
        action = self.next_action(current)
        if action is None or action.stage == "book_audit":
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_not_expected"
            )
        stored_action = parse_required_book_successor_action(
            job.get("required_book_successor_action")
        )
        if stored_action != action:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_binding_changed"
            )
        self._validate_child_authority(action, job)
        job_id = str(job.get("_id") or "")
        readiness = job.get("readiness")
        readiness_digest = (
            str(readiness.get("digest") or "")
            if isinstance(readiness, Mapping)
            else ""
        )
        if re.fullmatch(_OBJECT_ID, job_id) is None or re.fullmatch(
            _SHA256, readiness_digest
        ) is None:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_identity_invalid"
            )
        if action.stage == "review":
            candidate = parse_required_reviewed_candidate(
                job.get("required_reviewed_candidate")
            )
            validate_required_reviewed_candidate_job(job, candidate)
            if (
                job.get("status") != "paused"
                or job.get("pause_reason")
                != REQUIRED_REVIEWED_CANDIDATE_PAUSE_REASON
            ):
                raise RequiredBookSuccessorConflict(
                    "required_book_successor_review_not_ready"
                )
            result_digest = candidate.result_digest
            before = candidate.narrative_revision
            after = before
            source_digest = candidate.source_content_digest
        elif action.stage == "state":
            candidate = parse_required_state_candidate(
                job.get("required_state_candidate")
            )
            validate_required_state_candidate_job(job, candidate)
            if (
                job.get("status") != "paused"
                or job.get("pause_reason")
                != REQUIRED_STATE_CANDIDATE_PAUSE_REASON
                or candidate.predecessor_job_id
                != action.predecessor_job_ids[0]
                or candidate.predecessor_result_digest
                != action.predecessor_result_digests[0]
            ):
                raise RequiredBookSuccessorConflict(
                    "required_book_successor_state_not_ready"
                )
            result_digest = candidate.result_digest
            before = candidate.narrative_revision
            after = before
            source_digest = candidate.source_content_digest
        else:
            candidate = parse_required_chapter_finalization_result(
                job.get("required_chapter_finalization_result")
            )
            validate_required_chapter_finalization_result_job(job, candidate)
            if (
                job.get("status") != "completed"
                or candidate.next_step != REQUIRED_CHAPTER_FINALIZATION_RESULT_STEP
                or candidate.reviewed_result_digest
                != action.predecessor_result_digests[0]
                or candidate.state_result_digest
                != action.predecessor_result_digests[1]
            ):
                raise RequiredBookSuccessorConflict(
                    "required_book_successor_finalization_not_ready"
                )
            result_digest = candidate.result_digest
            before = candidate.narrative_revision_before
            after = candidate.narrative_revision_after
            source_digest = candidate.source_content_digest
        if (
            candidate.job_id != job_id
            or candidate.chapter_id != action.chapter_id
            or before != action.expected_narrative_revision
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_child_result_changed"
            )
        usage = _settled_provider_usage(job)
        record = RequiredBookSuccessorStageRecord(
            action_digest=action.action_digest,
            stage=action.stage,
            chapter_ordinal=action.chapter_ordinal,
            chapter_id=action.chapter_id,
            child_job_id=job_id,
            child_readiness_digest=readiness_digest,
            result_digest=result_digest,
            narrative_revision_before=before,
            narrative_revision_after=after,
            source_content_digest=source_digest,
            provider_usage=usage,
        )
        stages = (*current.stages, record)
        provider_totals: dict[str, list[int]] = {}
        for stage in stages:
            for item in stage.provider_usage:
                values = provider_totals.setdefault(item.provider_alias, [0, 0])
                values[0] += item.paid_attempts
                values[1] += item.tokens
        authorized = {
            item.provider_alias: item for item in self.authorization.provider_bounds
        }
        if (
            sum(values[0] for values in provider_totals.values())
            > self.authorization.maximum_provider_attempts_total
            or sum(values[1] for values in provider_totals.values())
            > self.authorization.maximum_tokens_total
            or any(
                alias not in authorized
                or values[0] > authorized[alias].maximum_paid_attempts_total
                or values[1] > authorized[alias].maximum_tokens_total
                for alias, values in provider_totals.items()
            )
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_budget_exceeded"
            )
        completed, partial = divmod(len(stages), 3)
        phase = (
            "book_audit"
            if completed == current.chapter_count and partial == 0
            else ("review", "state", "finalization")[partial]
        )
        return _new_journal(
            schema_version=current.schema_version,
            coordinator_job_id=current.coordinator_job_id,
            coordinator_readiness_digest=current.coordinator_readiness_digest,
            authorization_contract_digest=current.authorization_contract_digest,
            base_narrative_revision=current.base_narrative_revision,
            expected_narrative_revision=(
                current.base_narrative_revision + completed
            ),
            chapter_count=current.chapter_count,
            phase=phase,
            stages=stages,
            audit_digest=None,
            blocking_reason=None,
        )

    def accept_book_audit(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        report: BookCompletionReport | Mapping[str, Any],
    ) -> RequiredBookSuccessorJournal:
        current = self._validate_journal(journal)
        action = self.next_action(current)
        if action is None or action.stage != "book_audit":
            raise RequiredBookSuccessorConflict(
                "required_book_successor_audit_not_expected"
            )
        try:
            parsed = (
                report
                if isinstance(report, BookCompletionReport)
                else BookCompletionReport.model_validate(report)
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_audit_invalid"
            ) from exc
        target_ids = [
            item.chapter_id for item in self.authorization.review_template.chapters
        ]
        audited_ids = [item.chapter_id for item in parsed.chapters]
        audited = {item.chapter_id: item for item in parsed.chapters}
        if (
            not parsed.complete
            or parsed.status != "complete"
            or parsed.novel_id != self.authorization.novel_id
            or parsed.narrative_revision != action.expected_narrative_revision
            or parsed.blueprint.frozen_job_id != self.coordinator_job_id
            or parsed.blueprint.matches_frozen_worklist is not True
            or audited_ids != target_ids
            or parsed.summary.chapter_count != len(target_ids)
            or parsed.summary.complete_chapter_count != len(target_ids)
            or parsed.summary.current_state_count != len(target_ids)
            or any(
                audited[chapter_id].prose_status != "certificate_verified_v2"
                or audited[chapter_id].state_status != "current"
                for chapter_id in target_ids
            )
        ):
            raise RequiredBookSuccessorConflict(
                "required_book_successor_audit_incomplete"
            )
        return _new_journal(
            schema_version=current.schema_version,
            coordinator_job_id=current.coordinator_job_id,
            coordinator_readiness_digest=current.coordinator_readiness_digest,
            authorization_contract_digest=current.authorization_contract_digest,
            base_narrative_revision=current.base_narrative_revision,
            expected_narrative_revision=current.expected_narrative_revision,
            chapter_count=current.chapter_count,
            phase="completed",
            stages=current.stages,
            audit_digest=parsed.audit_digest,
            blocking_reason=None,
        )

    def block(
        self,
        journal: RequiredBookSuccessorJournal | Mapping[str, Any],
        reason: str,
    ) -> RequiredBookSuccessorJournal:
        current = self._validate_journal(journal)
        safe = str(reason or "")
        if _SAFE_REASON.fullmatch(safe) is None:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_block_reason_invalid"
            )
        if current.phase in {"completed", "blocked"}:
            raise RequiredBookSuccessorConflict(
                "required_book_successor_already_terminal"
            )
        return _new_journal(
            schema_version=current.schema_version,
            coordinator_job_id=current.coordinator_job_id,
            coordinator_readiness_digest=current.coordinator_readiness_digest,
            authorization_contract_digest=current.authorization_contract_digest,
            base_narrative_revision=current.base_narrative_revision,
            expected_narrative_revision=current.expected_narrative_revision,
            chapter_count=current.chapter_count,
            phase="blocked",
            stages=current.stages,
            audit_digest=None,
            blocking_reason=safe,
        )
