"""Explicit, bounded completion authorization for an interactive AI draft.

The prose generation button authorizes prose calls only.  A complete draft
therefore remains a candidate until this module shows a zero-charge readiness,
persists the newly confirmed semantic/state budget, and sends the resulting
evidence through the same V2 chapter finalizer used by batch generation.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from bson import ObjectId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pymongo.errors import DuplicateKeyError

from backend.db.errors import NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.config import get_provider_config
from backend.services.generation.attempt_scope import JobAttemptScope
from backend.services.generation.chapter_candidate_authorization import (
    CandidateJobGenerationPlan,
    candidate_job_generation_plan_snapshot,
    generation_plan_from_candidate_snapshot,
)
from backend.services.generation.chapter_completion_certificate import (
    canonical_completion_digest,
)
from backend.services.generation.chapter_finalization import (
    ChapterFinalizationAuthorization,
    ChapterFinalizationDenied,
    ChapterFinalizationEvidence,
    ChapterFinalizationService,
    build_chapter_finalization_authorization,
    chapter_finalization_service,
    parse_chapter_finalization_authorization,
)
from backend.services.generation.chapter_generation_application import (
    AcceptanceAuthority,
    AcceptanceTiming,
    ChapterGenerationApplicationService,
    OUTLINE_ADHERENCE_STEP,
    PROSE_REMEDIATION_WORKFLOW,
    STATE_STEP,
    STATE_WORKFLOW,
    OutlineAdherenceCommand,
    ProseCandidateSource,
    StateGenerationCommand,
)
from backend.services.generation.outline_adherence import (
    validate_complete_outline_adherence,
)
from backend.services.generation.prose_runs import prose_revision, prose_run_module
from backend.services.generation.provider_budget import (
    ProviderBudgetBound,
    merge_provider_bounds,
    scale_provider_bounds,
    structured_call_budget,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.novel.state_proposal import state_proposal_module
from backend.scene_contract_versions import (
    SCENE_TRANSITION_CONTRACT_VERSION,
    require_known_scene_contract_version,
)


INTERACTIVE_COMPLETION_READINESS_SCHEMA = (
    "interactive_chapter_completion_readiness.v1"
)
INTERACTIVE_COMPLETION_JOB_KIND = "interactive_chapter_completion"
INTERACTIVE_COMPLETION_RUNNING = "completion_running"
INTERACTIVE_COMPLETION_UNCERTAIN = "completion_uncertain"
INTERACTIVE_COMPLETION_RESOLVING = "completion_resolving_uncertain"
INTERACTIVE_COMPLETION_COMPLETED = "completion_completed"
INTERACTIVE_COMPLETION_MANUAL_REVIEW = "completion_manual_review"
INTERACTIVE_COMPLETION_RECOVERY_REPLAY_LIMIT = 1
INTERACTIVE_COMPLETION_EXECUTION_SCHEMA = (
    "interactive_chapter_completion_execution.v1"
)
INTERACTIVE_COMPLETION_EXECUTION_MARGIN_SECONDS = 300

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_OBJECT_ID_PATTERN = r"^[0-9a-f]{24}$"

Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]
ObjectIdText = Annotated[str, Field(pattern=_OBJECT_ID_PATTERN)]
DecimalText = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]


class InteractiveCompletionBlocked(ValueError):
    """The interactive candidate cannot consume this completion authority."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class InteractiveCompletionPricingError(ValueError):
    """A Provider used by this readiness has no valid price snapshot."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InteractiveCompletionSourceBinding(_ClosedModel):
    owner_id: ObjectIdText
    novel_id: ObjectIdText
    volume_id: ObjectIdText
    chapter_id: ObjectIdText
    prose_run_id: ObjectIdText
    prose_run_revision: int = Field(ge=1)
    content_digest: Digest
    outline_revision: Digest
    outline_contract_digest: Digest
    expected_narrative_revision: int = Field(ge=0)


class InteractiveCompletionProviderBound(_ClosedModel):
    provider_alias: str = Field(min_length=1, max_length=160)
    maximum_paid_attempts: int = Field(ge=1, le=1_000)
    conservative_token_bound: int = Field(ge=1, le=1_000_000_000)
    currency: str = Field(pattern=r"^[A-Z][A-Z0-9]{2,11}$")
    maximum_cost: DecimalText
    price_upper_bound_per_million_tokens: DecimalText
    pricing_basis: str = Field(min_length=1, max_length=240)
    pricing_snapshot_digest: Digest


class InteractiveCompletionPricing(_ClosedModel):
    provider_alias: str = Field(min_length=1, max_length=160)
    currency: str = Field(pattern=r"^[A-Z][A-Z0-9]{2,11}$")
    input_cost_per_million_tokens: Decimal = Field(ge=0)
    output_cost_per_million_tokens: Decimal = Field(ge=0)
    pricing_basis: str = Field(min_length=1, max_length=240)


class InteractiveCompletionRequestBinding(_ClosedModel):
    owner_id: ObjectIdText
    novel_id: ObjectIdText
    chapter_id: ObjectIdText
    run_id: ObjectIdText
    run_revision: int = Field(ge=1)
    authorization_id: ObjectIdText
    authorization_revision: int = Field(ge=1)
    readiness_digest: Digest


class InteractiveCompletionExecutionClaim(_ClosedModel):
    schema_version: Literal[
        "interactive_chapter_completion_execution.v1"
    ]
    token: Digest
    authorization_revision: int = Field(ge=1)
    readiness_digest: Digest
    claimed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "InteractiveCompletionExecutionClaim":
        if self.claimed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("interactive execution timestamps must be aware")
        if self.expires_at.astimezone(UTC) <= self.claimed_at.astimezone(UTC):
            raise ValueError("interactive execution expiry must follow claim")
        return self


class InteractiveCompletionPlans(_ClosedModel):
    adherence: CandidateJobGenerationPlan
    state: CandidateJobGenerationPlan


class InteractiveCompletionPlanning(_ClosedModel):
    chapter_finalization_authorization: Mapping[str, Any]

    @model_validator(mode="after")
    def validate_finalization_authority(self) -> "InteractiveCompletionPlanning":
        parsed = parse_chapter_finalization_authorization(
            self.chapter_finalization_authorization
        )
        if parsed["max_repair_cycles"] != 0:
            raise ValueError(
                "interactive completion cannot silently authorize repairs"
            )
        return self


class InteractiveChapterCompletionReadiness(_ClosedModel):
    schema_version: Literal[
        "interactive_chapter_completion_readiness.v1"
    ]
    digest: Digest
    authorization_id: ObjectIdText
    authorization_revision: int = Field(ge=1)
    source_binding: InteractiveCompletionSourceBinding
    planning: InteractiveCompletionPlanning
    generation_plans: InteractiveCompletionPlans
    provider_bounds: tuple[InteractiveCompletionProviderBound, ...] = Field(
        min_length=1,
        max_length=8,
    )
    logical_call_count: Literal[2]
    recovery_replay_limit: Literal[1]
    maximum_paid_attempts: int = Field(ge=2, le=1_000)
    conservative_token_bound: int = Field(ge=1, le=1_000_000_000)
    externalized_prose_utf8_bytes: int = Field(ge=1, le=100_000_000)

    @field_validator("provider_bounds", mode="before")
    @classmethod
    def tupleize_provider_bounds(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_identity_and_bounds(self) -> "InteractiveChapterCompletionReadiness":
        attempts = sum(item.maximum_paid_attempts for item in self.provider_bounds)
        tokens = sum(item.conservative_token_bound for item in self.provider_bounds)
        if attempts != self.maximum_paid_attempts:
            raise ValueError("interactive completion attempt bound changed")
        if tokens != self.conservative_token_bound:
            raise ValueError("interactive completion token bound changed")
        finalization = parse_chapter_finalization_authorization(
            self.planning.chapter_finalization_authorization
        )
        if finalization["authorization_revision"] != self.authorization_revision:
            raise ValueError("interactive finalization authorization changed")
        identity_payload = self.model_dump(
            mode="json",
            exclude={"digest", "authorization_id"},
        )
        expected_id = canonical_completion_digest(identity_payload)[:24]
        if expected_id != self.authorization_id:
            raise ValueError("interactive completion authorization identity changed")
        payload = self.model_dump(mode="json", exclude={"digest"})
        if canonical_completion_digest(payload) != self.digest:
            raise ValueError("interactive completion readiness digest changed")
        return self


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _provider_bound_projection(
    bounds: tuple[ProviderBudgetBound, ...],
    *,
    resolve_pricing: Callable[[str], InteractiveCompletionPricing],
) -> tuple[InteractiveCompletionProviderBound, ...]:
    projected: list[InteractiveCompletionProviderBound] = []
    for item in bounds:
        try:
            pricing = InteractiveCompletionPricing.model_validate(
                resolve_pricing(item.provider_alias)
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise InteractiveCompletionPricingError(
                f"Provider {item.provider_alias} has no valid price snapshot"
            ) from exc
        if pricing.provider_alias != item.provider_alias:
            raise ValueError("Provider price snapshot identity changed")
        price_upper_bound = max(
            pricing.input_cost_per_million_tokens,
            pricing.output_cost_per_million_tokens,
        )
        snapshot = pricing.model_dump(mode="json")
        projected.append(InteractiveCompletionProviderBound(
            provider_alias=item.provider_alias,
            maximum_paid_attempts=item.paid_attempts,
            conservative_token_bound=item.tokens,
            currency=pricing.currency,
            maximum_cost=_decimal_text(
                Decimal(item.tokens) * price_upper_bound / Decimal(1_000_000)
            ),
            price_upper_bound_per_million_tokens=_decimal_text(
                price_upper_bound
            ),
            pricing_basis=pricing.pricing_basis,
            pricing_snapshot_digest=canonical_completion_digest(snapshot),
        ))
    return tuple(projected)


def _build_readiness(
    *,
    source_binding: InteractiveCompletionSourceBinding,
    authorization_revision: int,
    adherence_plan: GenerationPlan,
    state_plan: GenerationPlan,
    externalized_prose_utf8_bytes: int,
    resolve_pricing: Callable[[str], InteractiveCompletionPricing],
) -> InteractiveChapterCompletionReadiness:
    replay_multiplier = INTERACTIVE_COMPLETION_RECOVERY_REPLAY_LIMIT + 1
    adherence_budget = structured_call_budget(adherence_plan)
    state_budget = structured_call_budget(state_plan)
    provider_bounds = merge_provider_bounds(
        scale_provider_bounds(
            adherence_budget.provider_bounds,
            replay_multiplier,
        ),
        scale_provider_bounds(
            state_budget.provider_bounds,
            replay_multiplier,
        ),
    )
    projected_bounds = _provider_bound_projection(
        provider_bounds,
        resolve_pricing=resolve_pricing,
    )
    payload: dict[str, Any] = {
        "schema_version": INTERACTIVE_COMPLETION_READINESS_SCHEMA,
        "authorization_revision": authorization_revision,
        "source_binding": source_binding.model_dump(mode="json"),
        "planning": {
            "chapter_finalization_authorization": (
                build_chapter_finalization_authorization(
                    authorization_revision=authorization_revision,
                    max_repair_cycles=0,
                )
            )
        },
        "generation_plans": {
            "adherence": candidate_job_generation_plan_snapshot(
                adherence_plan,
                call_kind="structured",
            ).model_dump(mode="json"),
            "state": candidate_job_generation_plan_snapshot(
                state_plan,
                call_kind="structured",
            ).model_dump(mode="json"),
        },
        "provider_bounds": [
            item.model_dump(mode="json") for item in projected_bounds
        ],
        "logical_call_count": 2,
        "recovery_replay_limit": INTERACTIVE_COMPLETION_RECOVERY_REPLAY_LIMIT,
        "maximum_paid_attempts": sum(
            item.maximum_paid_attempts for item in projected_bounds
        ),
        "conservative_token_bound": sum(
            item.conservative_token_bound for item in projected_bounds
        ),
        "externalized_prose_utf8_bytes": externalized_prose_utf8_bytes,
    }
    authorization_id = canonical_completion_digest(payload)[:24]
    with_identity = {**payload, "authorization_id": authorization_id}
    return InteractiveChapterCompletionReadiness.model_validate({
        **with_identity,
        "digest": canonical_completion_digest(with_identity),
    })


@dataclass(frozen=True)
class InteractiveChapterCompletionDeps:
    prose_runs: Any
    chapter_repo: Any
    job_repo: Any
    finalizer: Any
    plan_completion: Callable[[], tuple[GenerationPlan, GenerationPlan]]
    review_candidate: Callable[..., Awaitable[Mapping[str, Any]]]
    generate_state_candidate: Callable[..., Awaitable[Mapping[str, Any]]]
    recover_state_candidate: Callable[..., Awaitable[Any]]
    validate_adherence: Callable[..., Any]
    resolve_pricing: Callable[[str], InteractiveCompletionPricing]

    @classmethod
    def production(cls) -> "InteractiveChapterCompletionDeps":
        application = ChapterGenerationApplicationService()

        def plan_completion() -> tuple[GenerationPlan, GenerationPlan]:
            runtime = create_generation_runtime()
            adherence = runtime.plan_structured(WorkflowStepTarget(
                PROSE_REMEDIATION_WORKFLOW,
                OUTLINE_ADHERENCE_STEP,
            ))
            state = runtime.plan_structured(WorkflowStepTarget(
                STATE_WORKFLOW,
                STATE_STEP,
            ))
            return adherence, state

        async def review_candidate(
            *,
            novel_id: str,
            chapter_id: str,
            candidate: ProseCandidateSource,
            attempt_scope: Any,
            generation_plan: GenerationPlan,
        ) -> Mapping[str, Any]:
            result = await application.collect(OutlineAdherenceCommand(
                novel_id=novel_id,
                chapter_id=chapter_id,
                prose_candidate=candidate,
                attempt_scope=attempt_scope,
                generation_plan=generation_plan,
            ))
            if not isinstance(result.value, Mapping):
                raise ValueError("interactive adherence result is invalid")
            return dict(result.value)

        async def generate_state_candidate(
            *,
            novel_id: str,
            chapter_id: str,
            candidate: ProseCandidateSource,
            request_id: str,
            attempt_scope: Any,
            generation_plan: GenerationPlan,
        ) -> Mapping[str, Any]:
            result = await application.collect(StateGenerationCommand(
                novel_id=novel_id,
                chapter_id=chapter_id,
                authority=AcceptanceAuthority.PREVIEW,
                acceptance_timing=AcceptanceTiming.DEFERRED,
                prose_candidate=candidate,
                attempt_scope=attempt_scope,
                generation_plan=generation_plan,
                request_id=request_id,
            ))
            if not isinstance(result.value, Mapping):
                raise ValueError("interactive state result is invalid")
            return dict(result.value)

        def validate_adherence(
            *,
            adherence: Mapping[str, Any],
            outline: Mapping[str, Any],
            prose: str,
        ) -> Any:
            return validate_complete_outline_adherence(
                adherence,
                outline=outline,
                prose=prose,
                require_current_evidence=True,
            )

        def resolve_pricing(alias: str) -> InteractiveCompletionPricing:
            provider = get_provider_config(alias)
            values = (
                provider.billing_currency,
                provider.input_cost_per_million_tokens,
                provider.output_cost_per_million_tokens,
                provider.pricing_basis,
            )
            if any(value is None for value in values):
                raise InteractiveCompletionPricingError(
                    f"Provider {alias} has no complete price snapshot"
                )
            return InteractiveCompletionPricing(
                provider_alias=alias,
                currency=str(provider.billing_currency),
                input_cost_per_million_tokens=(
                    provider.input_cost_per_million_tokens
                ),
                output_cost_per_million_tokens=(
                    provider.output_cost_per_million_tokens
                ),
                pricing_basis=str(provider.pricing_basis),
            )

        return cls(
            prose_runs=prose_run_module,
            chapter_repo=chapter_repo,
            job_repo=generation_job_repo,
            finalizer=chapter_finalization_service,
            plan_completion=plan_completion,
            review_candidate=review_candidate,
            generate_state_candidate=generate_state_candidate,
            recover_state_candidate=(
                state_proposal_module.recover_owned_repair_result
            ),
            validate_adherence=validate_adherence,
            resolve_pricing=resolve_pricing,
        )


def _source_key(source: InteractiveCompletionSourceBinding) -> str:
    return canonical_completion_digest(source.model_dump(mode="json"))


def _candidate_source(candidate: Mapping[str, Any]) -> ProseCandidateSource:
    run = candidate["run"]
    return ProseCandidateSource(
        text=str(candidate["text"]),
        source_run_id=str(run["_id"]),
        source_run_revision=int(run["revision"]),
        source_content_digest=str(candidate["text_digest"]),
        completion=dict(candidate["completion"]),
    )


def _request_binding(
    *,
    owner_id: str,
    novel_id: str,
    chapter_id: str,
    run_id: str,
    run_revision: int,
    authorization_id: str,
    authorization_revision: int,
    readiness_digest: str,
) -> InteractiveCompletionRequestBinding:
    try:
        return InteractiveCompletionRequestBinding(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            run_revision=run_revision,
            authorization_id=authorization_id,
            authorization_revision=authorization_revision,
            readiness_digest=readiness_digest,
        )
    except ValidationError as exc:
        raise InteractiveCompletionBlocked(
            "interactive completion request binding is invalid",
            code="interactive_readiness_stale",
        ) from exc


def _bound_readiness(
    job: Mapping[str, Any],
    request: InteractiveCompletionRequestBinding,
) -> InteractiveChapterCompletionReadiness:
    try:
        readiness = InteractiveChapterCompletionReadiness.model_validate(
            job.get("readiness")
        )
    except ValidationError as exc:
        raise InteractiveCompletionBlocked(
            "interactive completion readiness history is invalid",
            code="interactive_authorization_invalid",
        ) from exc
    source = readiness.source_binding
    if (
        str(job.get("_id") or "") != request.authorization_id
        or str(job.get("owner_id") or "") != request.owner_id
        or str(job.get("novel_id") or "") != request.novel_id
        or str(job.get("current_chapter_id") or "") != request.chapter_id
        or source.owner_id != request.owner_id
        or source.novel_id != request.novel_id
        or source.chapter_id != request.chapter_id
        or source.prose_run_id != request.run_id
        or source.prose_run_revision != request.run_revision
        or readiness.authorization_id != request.authorization_id
        or readiness.authorization_revision != request.authorization_revision
        or readiness.digest != request.readiness_digest
    ):
        raise InteractiveCompletionBlocked(
            "interactive completion persisted binding is stale",
            code="interactive_readiness_stale",
        )
    return readiness


def _execution_window_seconds(
    readiness: InteractiveChapterCompletionReadiness,
) -> int:
    plans = (
        readiness.generation_plans.adherence,
        readiness.generation_plans.state,
    )
    provider_seconds = sum(
        int(plan.timeout_seconds or 60) * int(plan.max_semantic_attempts)
        for plan in plans
    )
    seconds = (
        provider_seconds
        * (INTERACTIVE_COMPLETION_RECOVERY_REPLAY_LIMIT + 1)
        + INTERACTIVE_COMPLETION_EXECUTION_MARGIN_SECONDS
    )
    if seconds <= 0 or seconds > 365 * 24 * 60 * 60:
        raise InteractiveCompletionBlocked(
            "interactive completion execution window is invalid",
            code="interactive_plan_invalid",
        )
    return seconds


def _job_document(
    readiness: InteractiveChapterCompletionReadiness,
) -> dict[str, Any]:
    now = get_utc_now()
    source = readiness.source_binding
    return {
        "_id": to_object_id(readiness.authorization_id),
        "owner_id": to_object_id(source.owner_id),
        "novel_id": to_object_id(source.novel_id),
        "volume_id": to_object_id(source.volume_id),
        "scope": "interactive_completion",
        "job_kind": INTERACTIVE_COMPLETION_JOB_KIND,
        "interactive_source_key": _source_key(source),
        "status": INTERACTIVE_COMPLETION_RUNNING,
        "pause_reason": None,
        "current_chapter_id": source.chapter_id,
        "expected_narrative_revision": source.expected_narrative_revision,
        "token_budget": readiness.conservative_token_bound,
        "tokens_used": 0,
        "tokens_reserved": 0,
        "active_token_reservations": [],
        "usage_attempt_capacity": readiness.maximum_paid_attempts,
        "usage_attempt_claimed": 0,
        "usage_attempt_ids": [],
        "usage_attempt_summaries": [],
        "attempt_slots": [],
        "attempt_reservation": None,
        "candidate_pipeline_checkpoints": [],
        "chapter_completion_decisions": [],
        "progress": [],
        "state_dispatch_resolution": None,
        "job_mutation_recovery": None,
        "execution_epoch": 0,
        "execution_lease": None,
        "interactive_execution_claim": None,
        "interactive_execution_uncertain": False,
        "uncertain_attempt_ids": [],
        "has_uncertain_attempts": False,
        "interactive_completion_evidence": {},
        "readiness": readiness.model_dump(mode="json"),
        "authorization_revision": readiness.authorization_revision,
        "created_at": now,
        "updated_at": now,
        "is_deleted": False,
    }


class InteractiveChapterCompletionService:
    """Inspect and consume one explicit interactive completion authority."""

    def __init__(
        self,
        deps: InteractiveChapterCompletionDeps | None = None,
    ) -> None:
        self._deps = deps or InteractiveChapterCompletionDeps.production()

    async def _snapshot(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        run_revision: int,
    ) -> tuple[
        Mapping[str, Any],
        Mapping[str, Any],
        InteractiveCompletionSourceBinding,
    ]:
        candidate = await self._deps.prose_runs.inspect_ai_completion_candidate(
            owner_id=owner_id,
            run_id=run_id,
            chapter_id=chapter_id,
            expected_revision=run_revision,
        )
        run = candidate.get("run")
        if not isinstance(run, Mapping):
            raise InteractiveCompletionBlocked(
                "interactive completion candidate is invalid",
                code="interactive_candidate_invalid",
            )
        chapter = await self._deps.chapter_repo.get_chapter_by_id(chapter_id)
        outline = chapter.get("outline")
        if not isinstance(outline, Mapping):
            raise InteractiveCompletionBlocked(
                "interactive completion requires a chapter outline",
                code="interactive_outline_missing",
            )
        if require_known_scene_contract_version(outline) != (
            SCENE_TRANSITION_CONTRACT_VERSION
        ):
            raise InteractiveCompletionBlocked(
                "interactive completion requires the current scene contract",
                code="interactive_outline_upgrade_required",
            )
        if (
            str(run.get("owner_id") or "") != owner_id
            or str(run.get("novel_id") or "") != novel_id
            or str(run.get("chapter_id") or "") != chapter_id
            or str(chapter.get("novel_id") or "") != novel_id
            or int(run.get("revision") or 0) != run_revision
        ):
            raise InteractiveCompletionBlocked(
                "interactive completion source binding is stale",
                code="interactive_source_stale",
            )
        source = InteractiveCompletionSourceBinding(
            owner_id=owner_id,
            novel_id=novel_id,
            volume_id=str(chapter.get("volume_id") or ""),
            chapter_id=chapter_id,
            prose_run_id=run_id,
            prose_run_revision=run_revision,
            content_digest=str(candidate.get("text_digest") or ""),
            outline_revision=str(run.get("outline_revision") or ""),
            outline_contract_digest=prose_revision(dict(outline)),
            expected_narrative_revision=int(
                candidate.get("captured_narrative_revision")
            ),
        )
        return candidate, chapter, source

    async def inspect(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        run_revision: int,
    ) -> InteractiveChapterCompletionReadiness:
        candidate, _chapter, source = await self._snapshot(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            run_revision=run_revision,
        )
        adherence_plan, state_plan = self._deps.plan_completion()
        if not isinstance(adherence_plan, GenerationPlan) or not isinstance(
            state_plan, GenerationPlan
        ):
            raise InteractiveCompletionBlocked(
                "interactive completion Provider plan is invalid",
                code="interactive_plan_invalid",
            )
        jobs = await self._deps.job_repo.find_many(
            {
                "owner_id": to_object_id(source.owner_id),
                "novel_id": to_object_id(source.novel_id),
                "job_kind": INTERACTIVE_COMPLETION_JOB_KIND,
                "interactive_source_key": _source_key(source),
            },
            sort=[("authorization_revision", -1), ("created_at", -1)],
            limit=1,
        )
        latest = jobs[0] if jobs else None
        if isinstance(latest, Mapping):
            prior_revision = latest.get("authorization_revision")
            if type(prior_revision) is not int or prior_revision < 1:
                raise InteractiveCompletionBlocked(
                    "interactive completion authorization history is invalid",
                    code="interactive_authorization_invalid",
                )
            authorization_revision = (
                prior_revision
                if latest.get("status") in {
                    INTERACTIVE_COMPLETION_RUNNING,
                    INTERACTIVE_COMPLETION_UNCERTAIN,
                    INTERACTIVE_COMPLETION_RESOLVING,
                }
                else prior_revision + 1
            )
        else:
            authorization_revision = 1
        try:
            readiness = _build_readiness(
                source_binding=source,
                authorization_revision=authorization_revision,
                adherence_plan=adherence_plan,
                state_plan=state_plan,
                externalized_prose_utf8_bytes=len(
                    str(candidate.get("text") or "").encode("utf-8")
                ),
                resolve_pricing=self._deps.resolve_pricing,
            )
        except InteractiveCompletionPricingError as exc:
            raise InteractiveCompletionBlocked(
                "interactive completion requires a complete Provider price snapshot",
                code="interactive_pricing_required",
            ) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise InteractiveCompletionBlocked(
                "interactive completion Provider plan is invalid",
                code="interactive_plan_invalid",
            ) from exc
        if (
            isinstance(latest, Mapping)
            and latest.get("status") in {
                INTERACTIVE_COMPLETION_RUNNING,
                INTERACTIVE_COMPLETION_UNCERTAIN,
                INTERACTIVE_COMPLETION_RESOLVING,
            }
        ):
            try:
                persisted = InteractiveChapterCompletionReadiness.model_validate(
                    latest.get("readiness")
                )
            except ValidationError as exc:
                raise InteractiveCompletionBlocked(
                    "interactive completion readiness history is invalid",
                    code="interactive_authorization_invalid",
                ) from exc
            if persisted != readiness:
                raise InteractiveCompletionBlocked(
                    "interactive completion configuration changed during authorization",
                    code="interactive_authorization_stale",
                )
        return readiness

    async def _ensure_job(
        self,
        readiness: InteractiveChapterCompletionReadiness,
    ) -> dict[str, Any]:
        try:
            job = await self._deps.job_repo.get_job(readiness.authorization_id)
        except (NotFoundError, KeyError):
            try:
                await self._deps.job_repo.create_job(_job_document(readiness))
            except DuplicateKeyError:
                pass
            job = await self._deps.job_repo.get_job(readiness.authorization_id)
        persisted = job.get("readiness")
        if persisted != readiness.model_dump(mode="json"):
            raise InteractiveCompletionBlocked(
                "interactive completion readiness is stale",
                code="interactive_readiness_stale",
            )
        if self._has_live_attempts(job):
            active_claim = self._execution_claim(job)
            if (
                active_claim is not None
                and active_claim.expires_at > get_utc_now()
            ):
                raise InteractiveCompletionBlocked(
                    "interactive completion is already executing",
                    code="interactive_authorization_conflict",
                )
            if job.get("status") != INTERACTIVE_COMPLETION_UNCERTAIN:
                await self._update_job(readiness.authorization_id, {
                    "status": INTERACTIVE_COMPLETION_UNCERTAIN,
                    "pause_reason": "uncertain_provider_attempt",
                    "updated_at": get_utc_now(),
                })
            raise InteractiveCompletionBlocked(
                "interactive completion has an uncertain paid attempt",
                code="interactive_uncertain_attempt",
            )
        if job.get("status") in {
            INTERACTIVE_COMPLETION_UNCERTAIN,
            INTERACTIVE_COMPLETION_RESOLVING,
        }:
            raise InteractiveCompletionBlocked(
                "interactive completion has an unresolved paid attempt",
                code="interactive_uncertain_attempt",
            )
        if job.get("status") != INTERACTIVE_COMPLETION_RUNNING:
            raise InteractiveCompletionBlocked(
                "interactive completion authorization is no longer active",
                code="interactive_authorization_inactive",
            )
        capacity = job.get("usage_attempt_capacity")
        claimed = job.get("usage_attempt_claimed")
        if (
            type(capacity) is not int
            or capacity != readiness.maximum_paid_attempts
            or type(claimed) is not int
            or claimed < 0
            or claimed > capacity
        ):
            raise InteractiveCompletionBlocked(
                "interactive completion attempt ledger is invalid",
                code="interactive_attempt_ledger_invalid",
            )
        await self._deps.job_repo.reserve_attempts(
            readiness.authorization_id,
            readiness.source_binding.chapter_id,
            capacity - claimed,
        )
        return await self._deps.job_repo.get_job(readiness.authorization_id)

    @staticmethod
    def _has_live_attempts(job: Mapping[str, Any]) -> bool:
        if job.get("has_uncertain_attempts") is True:
            return True
        slots = job.get("attempt_slots")
        return isinstance(slots, list) and any(
            isinstance(slot, Mapping)
            and slot.get("state") in {"claimed", "uncertain"}
            for slot in slots
        )

    @staticmethod
    def _execution_claim(
        job: Mapping[str, Any],
    ) -> InteractiveCompletionExecutionClaim | None:
        raw = job.get("interactive_execution_claim")
        if raw is None:
            return None
        try:
            return InteractiveCompletionExecutionClaim.model_validate(raw)
        except ValidationError as exc:
            raise InteractiveCompletionBlocked(
                "interactive completion execution claim is invalid",
                code="interactive_authorization_invalid",
            ) from exc

    @staticmethod
    def _execution_expected(token: str) -> dict[str, Any]:
        return {
            "status": INTERACTIVE_COMPLETION_RUNNING,
            "interactive_execution_claim.token": token,
        }

    async def _claim_execution(
        self,
        readiness: InteractiveChapterCompletionReadiness,
    ) -> InteractiveCompletionExecutionClaim:
        job = await self._deps.job_repo.get_job(readiness.authorization_id)
        existing = self._execution_claim(job)
        now = get_utc_now()
        if existing is not None:
            if (
                existing.authorization_revision
                != readiness.authorization_revision
                or existing.readiness_digest != readiness.digest
            ):
                raise InteractiveCompletionBlocked(
                    "interactive completion execution binding changed",
                    code="interactive_authorization_invalid",
                )
            if existing.expires_at > now:
                raise InteractiveCompletionBlocked(
                    "interactive completion is already executing",
                    code="interactive_authorization_conflict",
                )
            await self._update_job(
                readiness.authorization_id,
                {
                    "status": INTERACTIVE_COMPLETION_UNCERTAIN,
                    "pause_reason": "execution_claim_expired",
                    "interactive_execution_uncertain": True,
                    "updated_at": now,
                },
                expected={
                    "status": INTERACTIVE_COMPLETION_RUNNING,
                    "interactive_execution_claim": existing.model_dump(
                        mode="python"
                    ),
                },
            )
            await self._deps.job_repo.mark_claimed_attempts_uncertain(
                readiness.authorization_id,
                "interactive completion execution claim expired",
            )
            raise InteractiveCompletionBlocked(
                "interactive completion execution became uncertain; "
                "explicitly retry or abort it",
                code="interactive_uncertain_attempt",
            )
        claim = InteractiveCompletionExecutionClaim(
            schema_version=INTERACTIVE_COMPLETION_EXECUTION_SCHEMA,
            token=secrets.token_hex(32),
            authorization_revision=readiness.authorization_revision,
            readiness_digest=readiness.digest,
            claimed_at=now,
            expires_at=now + timedelta(
                seconds=_execution_window_seconds(readiness)
            ),
        )
        await self._update_job(
            readiness.authorization_id,
            {
                "interactive_execution_claim": claim.model_dump(mode="python"),
                "interactive_execution_uncertain": False,
                "updated_at": now,
            },
            expected={
                "status": INTERACTIVE_COMPLETION_RUNNING,
                "interactive_execution_claim": None,
            },
        )
        return claim

    async def _release_execution(
        self,
        *,
        job_id: str,
        token: str,
    ) -> None:
        await self._deps.job_repo.update_one(
            {
                "_id": to_object_id(job_id),
                "status": INTERACTIVE_COMPLETION_RUNNING,
                "interactive_execution_claim.token": token,
            },
            {
                "interactive_execution_claim": None,
                "updated_at": get_utc_now(),
            },
        )

    async def _update_job(
        self,
        job_id: str,
        updates: Mapping[str, Any],
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> None:
        query = {"_id": to_object_id(job_id), **dict(expected or {})}
        updated = await self._deps.job_repo.update_one(
            query,
            dict(updates),
        )
        if updated is not True:
            raise InteractiveCompletionBlocked(
                "interactive completion authorization changed concurrently",
                code="interactive_authorization_conflict",
            )

    async def _pause_if_provider_outcome_is_uncertain(
        self,
        *,
        job_id: str,
        execution_token: str,
        cause: BaseException,
    ) -> None:
        current = await self._deps.job_repo.get_job(job_id)
        claim = self._execution_claim(current)
        if claim is None or claim.token != execution_token:
            raise InteractiveCompletionBlocked(
                "interactive completion execution authority changed",
                code="interactive_authorization_conflict",
            ) from cause
        await self._deps.job_repo.mark_claimed_attempts_uncertain(
            job_id,
            f"interactive completion interrupted: {type(cause).__name__}",
        )
        job = await self._deps.job_repo.get_job(job_id)
        if not self._has_live_attempts(job):
            return
        if job.get("status") != INTERACTIVE_COMPLETION_UNCERTAIN:
            await self._update_job(job_id, {
                "status": INTERACTIVE_COMPLETION_UNCERTAIN,
                "pause_reason": "uncertain_provider_attempt",
                "interactive_execution_claim": None,
                "interactive_execution_uncertain": False,
                "updated_at": get_utc_now(),
            }, expected=self._execution_expected(execution_token))
        raise InteractiveCompletionBlocked(
            "interactive completion has an uncertain paid attempt; "
            "explicitly retry or abort it",
            code="interactive_uncertain_attempt",
        ) from cause

    async def _execute_claimed_completion(
        self,
        *,
        readiness: InteractiveChapterCompletionReadiness,
        execution: InteractiveCompletionExecutionClaim,
        job: Mapping[str, Any],
        candidate: Mapping[str, Any],
        chapter: Mapping[str, Any],
        source: InteractiveCompletionSourceBinding,
    ) -> dict[str, Any]:
        novel_id = source.novel_id
        chapter_id = source.chapter_id
        run_id = source.prose_run_id
        run_revision = source.prose_run_revision
        plans = readiness.generation_plans
        candidate_source = _candidate_source(candidate)
        evidence = dict(job.get("interactive_completion_evidence") or {})
        adherence = evidence.get("outline_adherence")
        if not isinstance(adherence, Mapping):
            attempts = await self._deps.job_repo.list_attempt_slots(
                readiness.authorization_id,
                chapter_id=chapter_id,
                step_prefix="",
            )
            try:
                adherence = dict(await self._deps.review_candidate(
                    novel_id=novel_id,
                    chapter_id=chapter_id,
                    candidate=candidate_source,
                    attempt_scope=JobAttemptScope(
                        readiness.authorization_id,
                        chapter_id,
                        "interactive-adherence",
                        repo=self._deps.job_repo,
                        existing_attempt_slots=attempts,
                        interactive_execution_token=execution.token,
                    ),
                    generation_plan=generation_plan_from_candidate_snapshot(
                        plans.adherence
                    ),
                ))
            except (Exception, asyncio.CancelledError) as exc:
                await self._pause_if_provider_outcome_is_uncertain(
                    job_id=readiness.authorization_id,
                    execution_token=execution.token,
                    cause=exc,
                )
                raise
            evidence["outline_adherence"] = deepcopy(dict(adherence))
            await self._update_job(
                readiness.authorization_id,
                {
                    "interactive_completion_evidence": deepcopy(evidence),
                    "updated_at": get_utc_now(),
                },
                expected=self._execution_expected(execution.token),
            )
        try:
            self._deps.validate_adherence(
                adherence=adherence,
                outline=dict(chapter["outline"]),
                prose=str(candidate["text"]),
            )
        except ValueError as exc:
            await self._update_job(
                readiness.authorization_id,
                {
                    "status": INTERACTIVE_COMPLETION_MANUAL_REVIEW,
                    "pause_reason": "outline_adherence_manual_review",
                    "interactive_execution_claim": None,
                    "updated_at": get_utc_now(),
                },
                expected=self._execution_expected(execution.token),
            )
            raise InteractiveCompletionBlocked(
                str(exc),
                code="interactive_adherence_manual_review",
            ) from exc

        state_checkpoint = evidence.get("state_proposal")
        state: Mapping[str, Any] | None = None
        if isinstance(state_checkpoint, Mapping):
            recovered = await self._deps.recover_state_candidate(
                owner_id=source.owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                proposal_id=str(state_checkpoint.get("proposal_id") or ""),
                request_id=str(state_checkpoint.get("request_id") or ""),
                source_run_id=run_id,
                source_run_revision=run_revision,
                source_content_digest=source.content_digest,
            )
            raw_value = getattr(recovered, "value", recovered)
            if isinstance(raw_value, Mapping):
                state = dict(raw_value)
        if state is None:
            request_id = (
                f"interactive-{readiness.authorization_id[:16]}-"
                f"state-r{readiness.authorization_revision}"
            )
            attempts = await self._deps.job_repo.list_attempt_slots(
                readiness.authorization_id,
                chapter_id=chapter_id,
                step_prefix="",
            )
            try:
                state = dict(await self._deps.generate_state_candidate(
                    novel_id=novel_id,
                    chapter_id=chapter_id,
                    candidate=candidate_source,
                    request_id=request_id,
                    attempt_scope=JobAttemptScope(
                        readiness.authorization_id,
                        chapter_id,
                        "interactive-state",
                        repo=self._deps.job_repo,
                        existing_attempt_slots=attempts,
                        interactive_execution_token=execution.token,
                    ),
                    generation_plan=generation_plan_from_candidate_snapshot(
                        plans.state
                    ),
                ))
            except (Exception, asyncio.CancelledError) as exc:
                await self._pause_if_provider_outcome_is_uncertain(
                    job_id=readiness.authorization_id,
                    execution_token=execution.token,
                    cause=exc,
                )
                raise
            proposal_id = state.get("proposal_id")
            if not isinstance(proposal_id, str) or not ObjectId.is_valid(
                proposal_id
            ):
                raise InteractiveCompletionBlocked(
                    "interactive state proposal identity is invalid",
                    code="interactive_state_invalid",
                )
            evidence["state_proposal"] = {
                "proposal_id": proposal_id,
                "request_id": request_id,
                "source_prose_run_id": run_id,
                "source_prose_run_revision": run_revision,
                "source_content_digest": source.content_digest,
            }
            await self._update_job(
                readiness.authorization_id,
                {
                    "interactive_completion_evidence": deepcopy(evidence),
                    "updated_at": get_utc_now(),
                },
                expected=self._execution_expected(execution.token),
            )

        proposal_id = state.get("proposal_id")
        acceptance_token = state.get("acceptance_token")
        if not isinstance(proposal_id, str) or not isinstance(
            acceptance_token, str
        ):
            raise InteractiveCompletionBlocked(
                "interactive state proposal receipt is invalid",
                code="interactive_state_invalid",
            )
        try:
            result = dict(await self._deps.finalizer.commit(
                owner_id=source.owner_id,
                chapter_id=chapter_id,
                prose_run_id=run_id,
                prose_run_revision=run_revision,
                state_proposal_id=proposal_id,
                state_acceptance_token=acceptance_token,
                authorization=ChapterFinalizationAuthorization(
                    kind="interactive_completion_readiness",
                    authorization_id=readiness.authorization_id,
                    job_id=readiness.authorization_id,
                    readiness_digest=readiness.digest,
                    authorization_revision=readiness.authorization_revision,
                    execution_claim_token=execution.token,
                ),
                evidence=ChapterFinalizationEvidence(
                    outline_adherence=dict(adherence),
                    repair_cycles_used=0,
                ),
            ))
        except ChapterFinalizationDenied as exc:
            await self._update_job(
                readiness.authorization_id,
                {
                    "status": INTERACTIVE_COMPLETION_MANUAL_REVIEW,
                    "pause_reason": "completion_policy_manual_review",
                    "interactive_execution_claim": None,
                    "updated_at": get_utc_now(),
                },
                expected=self._execution_expected(execution.token),
            )
            raise InteractiveCompletionBlocked(
                str(exc),
                code="interactive_completion_manual_review",
            ) from exc
        certificate = result.get("certificate")
        if not isinstance(certificate, Mapping):
            raise InteractiveCompletionBlocked(
                "interactive finalizer omitted the V2 certificate",
                code="interactive_certificate_missing",
            )
        await self._deps.job_repo.finish_attempt_reservation(
            readiness.authorization_id,
            chapter_id,
        )
        await self._update_job(
            readiness.authorization_id,
            {
                "status": INTERACTIVE_COMPLETION_COMPLETED,
                "pause_reason": None,
                "interactive_execution_claim": None,
                "interactive_execution_uncertain": False,
                "completion_certificate": {
                    "certificate_id": str(
                        certificate.get("certificate_id") or ""
                    ),
                    "certificate_digest": str(
                        certificate.get("certificate_digest") or ""
                    ),
                },
                "updated_at": get_utc_now(),
            },
            expected=self._execution_expected(execution.token),
        )
        return result

    async def _recover_committed_finalization(
        self,
        *,
        request: InteractiveCompletionRequestBinding,
    ) -> dict[str, Any] | None:
        try:
            job = await self._deps.job_repo.get_job(request.authorization_id)
        except (NotFoundError, KeyError):
            return None
        readiness = _bound_readiness(job, request)
        status = str(job.get("status") or "")
        if self._has_live_attempts(job) or status not in {
            INTERACTIVE_COMPLETION_RUNNING,
            INTERACTIVE_COMPLETION_COMPLETED,
        }:
            return None
        checkpoint = (
            (job.get("interactive_completion_evidence") or {}).get(
                "state_proposal"
            )
            if isinstance(job.get("interactive_completion_evidence"), Mapping)
            else None
        )
        proposal_id = (
            str(checkpoint.get("proposal_id") or "")
            if isinstance(checkpoint, Mapping)
            else ""
        )
        if not ObjectId.is_valid(proposal_id):
            if status == INTERACTIVE_COMPLETION_COMPLETED:
                raise InteractiveCompletionBlocked(
                    "completed interactive authorization lacks its state receipt",
                    code="interactive_authorization_invalid",
                )
            return None
        recovered = await self._deps.finalizer.recover_completed(
            owner_id=request.owner_id,
            novel_id=request.novel_id,
            chapter_id=request.chapter_id,
            prose_run_id=request.run_id,
            prose_run_revision=request.run_revision,
            state_proposal_id=proposal_id,
            authorization=ChapterFinalizationAuthorization(
                kind="interactive_completion_readiness",
                authorization_id=request.authorization_id,
                job_id=request.authorization_id,
                readiness_digest=request.readiness_digest,
                authorization_revision=request.authorization_revision,
            ),
        )
        if recovered is None:
            if status == INTERACTIVE_COMPLETION_COMPLETED:
                raise InteractiveCompletionBlocked(
                    "completed interactive authorization lacks its finalization journal",
                    code="interactive_authorization_invalid",
                )
            return None
        certificate = recovered.get("certificate")
        if not isinstance(certificate, Mapping):
            raise InteractiveCompletionBlocked(
                "recovered interactive finalization lacks its certificate",
                code="interactive_certificate_missing",
            )
        await self._deps.job_repo.finish_attempt_reservation(
            request.authorization_id,
            request.chapter_id,
        )
        if status != INTERACTIVE_COMPLETION_COMPLETED:
            await self._update_job(
                request.authorization_id,
                {
                    "status": INTERACTIVE_COMPLETION_COMPLETED,
                    "pause_reason": None,
                    "interactive_execution_claim": None,
                    "interactive_execution_uncertain": False,
                    "completion_certificate": {
                        "certificate_id": str(
                            certificate.get("certificate_id") or ""
                        ),
                        "certificate_digest": str(
                            certificate.get("certificate_digest") or ""
                        ),
                    },
                    "updated_at": get_utc_now(),
                },
                expected={"status": status},
            )
        return dict(recovered)

    async def resolve_uncertain(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        run_revision: int,
        authorization_id: str,
        authorization_revision: int,
        readiness_digest: str,
        action: Literal["retry", "abort"],
    ) -> dict[str, Any]:
        """Explicitly dispose one frozen uncertain paid attempt.

        ``retry`` consumes only the replay headroom already shown in the same
        readiness. ``abort`` retires the authority without any formal write.
        The resolving marker makes the acknowledgement recoverable if the
        process stops between its two local persistence steps.
        """

        if action not in {"retry", "abort"}:
            raise InteractiveCompletionBlocked(
                "interactive uncertainty resolution is invalid",
                code="interactive_uncertain_resolution_invalid",
            )
        request = _request_binding(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            run_revision=run_revision,
            authorization_id=authorization_id,
            authorization_revision=authorization_revision,
            readiness_digest=readiness_digest,
        )
        try:
            job = await self._deps.job_repo.get_job(authorization_id)
        except KeyError as exc:
            raise NotFoundError("interactive completion authorization not found") from exc
        _bound_readiness(job, request)
        target_status = (
            INTERACTIVE_COMPLETION_RUNNING
            if action == "retry"
            else INTERACTIVE_COMPLETION_MANUAL_REVIEW
        )
        if (
            job.get("status") == target_status
            and job.get("last_uncertain_resolution_action") == action
            and not self._has_live_attempts(job)
        ):
            return {
                "authorization_id": authorization_id,
                "action": action,
                "status": target_status,
            }
        if self._has_live_attempts(job) and job.get("status") not in {
            INTERACTIVE_COMPLETION_UNCERTAIN,
            INTERACTIVE_COMPLETION_RESOLVING,
        }:
            await self._update_job(authorization_id, {
                "status": INTERACTIVE_COMPLETION_UNCERTAIN,
                "pause_reason": "uncertain_provider_attempt",
                "updated_at": get_utc_now(),
            })
            job = await self._deps.job_repo.get_job(authorization_id)
        if job.get("status") == INTERACTIVE_COMPLETION_UNCERTAIN:
            await self._update_job(
                authorization_id,
                {
                    "status": INTERACTIVE_COMPLETION_RESOLVING,
                    "uncertain_resolution_action": action,
                    "updated_at": get_utc_now(),
                },
                expected={"status": INTERACTIVE_COMPLETION_UNCERTAIN},
            )
        elif (
            job.get("status") != INTERACTIVE_COMPLETION_RESOLVING
            or job.get("uncertain_resolution_action") != action
        ):
            raise InteractiveCompletionBlocked(
                "interactive completion has no matching uncertain attempt",
                code="interactive_uncertain_attempt_missing",
            )
        if self._has_live_attempts(job):
            acknowledged = (
                await self._deps.job_repo.acknowledge_uncertain_attempts(
                    authorization_id,
                    action,
                )
            )
            if acknowledged is not True:
                raise InteractiveCompletionBlocked(
                    "interactive uncertain attempt acknowledgement conflicted",
                    code="interactive_authorization_conflict",
                )
        await self._update_job(
            authorization_id,
            {
                "status": target_status,
                "pause_reason": (
                    None if action == "retry" else "uncertain_attempt_aborted"
                ),
                "uncertain_resolution_action": None,
                "last_uncertain_resolution_action": action,
                "interactive_execution_claim": None,
                "interactive_execution_uncertain": False,
                "updated_at": get_utc_now(),
            },
            expected={
                "status": INTERACTIVE_COMPLETION_RESOLVING,
                "uncertain_resolution_action": action,
            },
        )
        return {
            "authorization_id": authorization_id,
            "action": action,
            "status": target_status,
        }

    async def complete(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        run_revision: int,
        authorization_id: str,
        authorization_revision: int,
        readiness_digest: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise InteractiveCompletionBlocked(
                "interactive completion readiness requires explicit confirmation",
                code="interactive_confirmation_required",
            )
        request = _request_binding(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            run_revision=run_revision,
            authorization_id=authorization_id,
            authorization_revision=authorization_revision,
            readiness_digest=readiness_digest,
        )
        recovered = await self._recover_committed_finalization(
            request=request,
        )
        if recovered is not None:
            return recovered
        readiness = await self.inspect(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            run_revision=run_revision,
        )
        if (
            authorization_id != readiness.authorization_id
            or authorization_revision != readiness.authorization_revision
            or readiness_digest != readiness.digest
        ):
            raise InteractiveCompletionBlocked(
                "interactive completion readiness is stale",
                code="interactive_readiness_stale",
            )
        candidate, chapter, source = await self._snapshot(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            run_revision=run_revision,
        )
        if source != readiness.source_binding:
            raise InteractiveCompletionBlocked(
                "interactive completion source changed after confirmation",
                code="interactive_source_stale",
            )
        job = await self._ensure_job(readiness)
        execution = await self._claim_execution(readiness)
        try:
            return await self._execute_claimed_completion(
                readiness=readiness,
                execution=execution,
                job=job,
                candidate=candidate,
                chapter=chapter,
                source=source,
            )
        finally:
            await self._release_execution(
                job_id=readiness.authorization_id,
                token=execution.token,
            )


interactive_chapter_completion_service = InteractiveChapterCompletionService()
