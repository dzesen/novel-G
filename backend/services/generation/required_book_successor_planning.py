"""Side-effect-free GenerationPlan assembly for the book successor.

The caller must choose every Provider alias.  This Module only resolves those
explicit routes through one readonly ``GenerationRuntime`` and proves that the
result can be frozen by the review/state successor contracts.  It never opens
an adapter, reads a repository, creates readiness, or grants execution rights.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.services.generation.chapter_generation_application import (
    OUTLINE_ADHERENCE_STEP,
    PROSE_REMEDIATION_WORKFLOW,
    STATE_STEP,
    STATE_WORKFLOW,
)
from backend.services.generation.independent_outline_review import (
    IndependentReviewPlan,
)
from backend.services.generation.prose_remediation_runtime import (
    PROSE_CANDIDATE_REWRITE_STEP,
    REMEDIATION_PLANNER_STEP,
)
from backend.services.generation.provider_budget import structured_call_budget
from backend.services.generation.required_chapter_review import (
    RequiredChapterReviewPlan,
)
from backend.services.generation.required_initial_prose_contracts import (
    INITIAL_PROSE_INPUT_BOUND,
)
from backend.services.generation.required_prose_rewrite_contracts import (
    RequiredProseRewritePlan,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    GenerationRuntime,
    StructuredOutputMode,
    WorkflowStepTarget,
)


INITIAL_PROSE_WORKFLOW = "write_chapter_by_ai"
INITIAL_PROSE_STEP = "chapter_content"


def _provider_alias(value: object, *, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"required_book_successor_{role}_provider_missing")
    return value.strip()


def _positive_int(value: object, *, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(reason)
    return value


def _require_supported_structured_plan(
    plan: GenerationPlan,
    *,
    role: str,
) -> None:
    if (
        plan.mode
        not in {
            StructuredOutputMode.PROMPT_JSON,
            StructuredOutputMode.JSON_OBJECT,
        }
        or plan.reviewer_alias is not None
        or plan.max_semantic_attempts != 2
    ):
        raise ValueError(
            f"required_book_successor_{role}_structured_plan_unsupported"
        )
    _positive_int(
        plan.timeout_seconds,
        reason=f"required_book_successor_{role}_timeout_unbounded",
    )
    _positive_int(
        plan.max_output_tokens,
        reason=f"required_book_successor_{role}_output_unbounded",
    )
    _positive_int(
        plan.max_context_tokens,
        reason=f"required_book_successor_{role}_context_unbounded",
    )


@dataclass(frozen=True)
class RequiredBookSuccessorPlanBundle:
    """Exact plans consumed by ``inspect_required_book_successor_readiness``."""

    chapter_review: RequiredChapterReviewPlan
    state_generation: GenerationPlan

    def __post_init__(self) -> None:
        # Force every nested authorization calculation now.  A caller must not
        # discover an unsupported route only after readiness was presented.
        self.chapter_review.rewrite.authorization()
        structured_call_budget(self.state_generation)

        state_target = self.state_generation.target
        if (
            not isinstance(state_target, WorkflowStepTarget)
            or state_target.workflow_name != STATE_WORKFLOW
            or state_target.step_name != STATE_STEP
            or state_target.provider_alias is None
        ):
            raise ValueError(
                "required_book_successor_state_target_not_explicit"
            )

    @property
    def writer_provider_alias(self) -> str:
        return self.chapter_review.initial_generation.provider_alias

    @property
    def judge_provider_alias(self) -> str:
        return self.chapter_review.review.generation.provider_alias

    @property
    def state_provider_alias(self) -> str:
        return self.state_generation.provider_alias


def build_required_book_successor_plan_bundle(
    runtime: GenerationRuntime,
    *,
    writer_provider_alias: str,
    judge_provider_alias: str,
    state_provider_alias: str | None = None,
    review_input_token_bound: int | None = None,
    review_max_response_bytes: int,
) -> RequiredBookSuccessorPlanBundle:
    """Resolve explicit Provider routes without constructing an adapter.

    When ``review_input_token_bound`` is omitted, the independent review uses
    the Judge context window minus its maximum output.  A smaller explicit
    value may tighten the future authorization, but can never exceed that
    context-safe ceiling.
    """

    if not isinstance(runtime, GenerationRuntime):
        raise ValueError("required_book_successor_runtime_invalid")
    writer_alias = _provider_alias(writer_provider_alias, role="writer")
    judge_alias = _provider_alias(judge_provider_alias, role="judge")
    state_alias = _provider_alias(
        state_provider_alias if state_provider_alias is not None else writer_alias,
        role="state",
    )
    response_bytes = _positive_int(
        review_max_response_bytes,
        reason="required_book_successor_review_response_bytes_unbounded",
    )

    initial = runtime.plan_text(
        WorkflowStepTarget(
            INITIAL_PROSE_WORKFLOW,
            INITIAL_PROSE_STEP,
            provider_alias=writer_alias,
        )
    )
    if (
        initial.reviewer_alias is not None
        or initial.max_semantic_attempts != 1
        or not str(initial.provider_model or "").strip()
    ):
        raise ValueError(
            "required_book_successor_initial_writer_plan_unsupported"
        )
    _positive_int(
        initial.timeout_seconds,
        reason="required_book_successor_initial_writer_timeout_unbounded",
    )
    initial_output = _positive_int(
        initial.max_output_tokens,
        reason="required_book_successor_initial_writer_output_unbounded",
    )
    initial_context = _positive_int(
        initial.max_context_tokens,
        reason="required_book_successor_initial_writer_context_unbounded",
    )
    if INITIAL_PROSE_INPUT_BOUND + initial_output > initial_context:
        raise ValueError(
            "required_book_successor_initial_writer_context_exhausted"
        )

    planner = runtime.plan_structured(
        WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            REMEDIATION_PLANNER_STEP,
            provider_alias=writer_alias,
        )
    )
    rewrite = runtime.plan_structured(
        WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            PROSE_CANDIDATE_REWRITE_STEP,
            provider_alias=writer_alias,
        )
    )
    judge = runtime.plan_structured(
        WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            OUTLINE_ADHERENCE_STEP,
            provider_alias=judge_alias,
        )
    )
    state = runtime.plan_structured(
        WorkflowStepTarget(
            STATE_WORKFLOW,
            STATE_STEP,
            provider_alias=state_alias,
        )
    )
    for role, plan in (
        ("rewrite_planner", planner),
        ("rewrite_writer", rewrite),
        ("judge", judge),
        ("state", state),
    ):
        _require_supported_structured_plan(plan, role=role)

    if initial.provider_model.strip() != rewrite.provider_model.strip():
        raise ValueError("required_book_successor_writer_model_changed")

    judge_context = _positive_int(
        judge.max_context_tokens,
        reason="required_book_successor_judge_context_unbounded",
    )
    judge_output = _positive_int(
        judge.max_output_tokens,
        reason="required_book_successor_judge_output_unbounded",
    )
    maximum_review_input = judge_context - judge_output
    if maximum_review_input < 1:
        raise ValueError("required_book_successor_judge_context_exhausted")
    if review_input_token_bound is None:
        review_input = maximum_review_input
    else:
        review_input = _positive_int(
            review_input_token_bound,
            reason="required_book_successor_review_input_bound_invalid",
        )
        if review_input > maximum_review_input:
            raise ValueError(
                "required_book_successor_review_input_exceeds_context"
            )

    independent_review = IndependentReviewPlan(
        generation=judge,
        writer_model=initial.provider_model,
        input_token_bound=review_input,
        max_response_bytes=response_bytes,
    )
    rewrite_plan = RequiredProseRewritePlan(
        planner=planner,
        rewrite=rewrite,
        review=independent_review,
    )
    review_plan = RequiredChapterReviewPlan(
        initial_generation=initial,
        rewrite=rewrite_plan,
    )
    return RequiredBookSuccessorPlanBundle(
        chapter_review=review_plan,
        state_generation=state,
    )
