"""Production adapters for the bounded candidate-repair phase."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from backend.db.repositories.agent_runtime_repository import (
    agent_runtime_repository,
)
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.repositories.state_candidate_repair_receipt_repository import (
    StateCandidateRepairResultProjection,
    state_candidate_repair_receipt_repo,
)
from backend.services.agent_runtime.contracts import (
    AgentReadinessRequest,
    AgentScope,
)
from backend.services.generation.chapter_candidate_authorization import (
    CandidateRepairAuthorization,
    authorized_candidate_repair_attempt_slots,
    parse_candidate_repair_authorization,
    validate_candidate_repair_execution_authorization,
)
from backend.services.generation.chapter_candidate_pipeline import (
    ProseCandidateRepairReceipt,
    ProseCandidateRepairRequest,
    StateCandidateRepairReceipt,
    StateCandidateRepairRequest,
)
from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    OUTLINE_ADHERENCE_STEP,
    PROSE_REMEDIATION_WORKFLOW,
    ProseCandidateSource,
    STATE_STEP,
    STATE_WORKFLOW,
    StateRepairGuidance,
)
from backend.services.generation.candidate_repair_contracts import (
    PreDispatchFenceV1,
)
from backend.services.generation.prose_remediation_runtime import (
    REMEDIATION_SCOPE_KIND,
    build_prose_remediation_runtime,
)
from backend.services.generation.prose_runs import chapter_content_digest
from backend.services.novel.state_proposal import state_proposal_module
from backend.services.llm.generation_runtime import (
    AttemptScope,
    GenerationPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)


_MAX_REPAIR_ATTEMPT_EVIDENCE = 64


class FencedAttemptScope(AttemptScope, Protocol):
    """A paid-attempt scope that can atomically fence receipt takeovers."""

    async def bind_pre_dispatch_fence(
        self,
        fence: PreDispatchFenceV1,
    ) -> None: ...


class CandidateRepairRunStopped(RuntimeError):
    """Expose bounded paid evidence when an authorized repair cannot finish."""

    def __init__(
        self,
        message: str,
        *,
        usage: Mapping[str, Any],
        attempts: Sequence[Mapping[str, Any]],
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage)
        self.attempts = [dict(item) for item in attempts]


class _StateRepairProposalUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ChapterCandidateRepairApplicationDeps:
    build_remediation_runtime: Callable[..., Any]
    plan_candidate_workflows: Callable[[], tuple[GenerationPlan, GenerationPlan]]
    validate_execution: Callable[..., CandidateRepairAuthorization]
    find_agent_run: Callable[..., Awaitable[Mapping[str, Any] | None]]
    get_prose_run: Callable[[str, str], Awaitable[Mapping[str, Any]]]
    generate_state_candidate: Callable[..., Awaitable[ChapterGenerationResult]]
    read_ordered_attempts: Callable[..., Awaitable[Sequence[Mapping[str, Any]]]]
    get_state_proposal: Callable[..., Awaitable[Mapping[str, Any]]]
    state_repair_receipts: Any
    recover_state_proposal: Callable[..., Awaitable[Any]]
    discard_pre_dispatch_attempt: Callable[..., Awaitable[bool]] = (
        generation_job_repo.discard_proven_pre_dispatch_attempt
    )

    @classmethod
    def production(cls) -> "ChapterCandidateRepairApplicationDeps":
        from backend.services.generation.headless_generation import (
            generate_state_candidate,
        )

        return cls(
            build_remediation_runtime=build_prose_remediation_runtime,
            plan_candidate_workflows=_plan_candidate_workflows,
            validate_execution=_validate_execution,
            find_agent_run=(
                agent_runtime_repository.find_run_by_start_request_id_owned
            ),
            get_prose_run=prose_run_repo.get_run,
            generate_state_candidate=generate_state_candidate,
            read_ordered_attempts=generation_job_repo.list_attempt_slots,
            get_state_proposal=(
                state_proposal_module.get_owned_repair_source
            ),
            state_repair_receipts=state_candidate_repair_receipt_repo,
            recover_state_proposal=(
                state_proposal_module.recover_owned_repair_result
            ),
            discard_pre_dispatch_attempt=(
                generation_job_repo.discard_proven_pre_dispatch_attempt
            ),
        )


def _plan_candidate_workflows() -> tuple[GenerationPlan, GenerationPlan]:
    runtime = create_generation_runtime(max_provider_retries=0)
    return (
        runtime.plan_structured(WorkflowStepTarget(
            PROSE_REMEDIATION_WORKFLOW,
            OUTLINE_ADHERENCE_STEP,
        )),
        runtime.plan_structured(WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)),
    )


def _validate_execution(**kwargs: Any) -> CandidateRepairAuthorization:
    return parse_candidate_repair_authorization(
        validate_candidate_repair_execution_authorization(**kwargs)
    )


def _attempt_projection(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        attempt_id = item.get("attempt_id")
        provider_alias = item.get("provider_alias")
        phase = item.get("phase")
        usage = item.get("usage")
        state = item.get("state")
        usage_is_valid = isinstance(usage, Mapping) and all(
            field in usage
            and type(usage[field]) is int
            and usage[field] >= 0
            for field in ("input_tokens", "output_tokens", "total_tokens")
        )
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(provider_alias, str)
            or not provider_alias
            or not isinstance(phase, str)
            or not phase
            or (state == "accounted" and not usage_is_valid)
        ):
            raise ValueError("candidate repair attempt evidence is invalid")
        return {
            "attempt_id": attempt_id,
            "provider_alias": provider_alias,
            "phase": phase,
            "state": str(item.get("state") or "accounted"),
            "usage": (
                {
                    field: int(usage[field])
                    for field in (
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    )
                }
                if usage_is_valid
                else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            ),
        }
    raw_usage = getattr(item, "usage", None)
    if hasattr(raw_usage, "model_dump"):
        usage = raw_usage.model_dump(mode="json")
    elif isinstance(raw_usage, Mapping):
        usage = dict(raw_usage)
    else:
        usage = {
            "input_tokens": int(getattr(raw_usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(raw_usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(raw_usage, "total_tokens", 0) or 0),
        }
    return {
        "attempt_id": str(getattr(item, "attempt_id", "") or ""),
        "provider_alias": str(
            getattr(item, "provider_alias", "") or "unreported"
        ),
        "phase": str(getattr(item, "phase", "") or "unknown"),
        "state": str(getattr(item, "state", "") or "accounted"),
        "usage": usage,
    }


def _ordered_attempt_projection(
    raw_attempts: Sequence[Mapping[str, Any]],
    *,
    claimed_are_uncertain: bool = False,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    charged_states = {
        "accounted": "accounted",
        "uncertain": "uncertain",
        "uncertain_retry_acknowledged": "resolved_retry",
        "uncertain_skip_acknowledged": "resolved_skip",
        "uncertain_abort_acknowledged": "resolved_abort",
    }
    allowed_states = {*charged_states, "released_pre_dispatch", "claimed"}
    for raw in raw_attempts:
        state = raw.get("state")
        if not isinstance(state, str) or state not in allowed_states:
            raise ValueError("candidate repair attempt state is invalid")
        if state == "released_pre_dispatch":
            continue
        if state == "claimed" and not claimed_are_uncertain:
            continue
        projection = _attempt_projection(raw)
        projection["state"] = (
            "uncertain" if state == "claimed" else charged_states[state]
        )
        if state != "accounted":
            bound = raw.get("conservative_tokens")
            if type(bound) is not int or bound <= 0:
                raise ValueError("candidate repair uncertain usage is invalid")
            projection["usage"] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": bound,
            }
        attempts.append(projection)
    if len(attempts) > _MAX_REPAIR_ATTEMPT_EVIDENCE:
        raise ValueError("candidate repair attempt evidence exceeds V1")
    if any(not item["attempt_id"] for item in attempts):
        raise ValueError("candidate repair attempt evidence has no identity")
    if len({item["attempt_id"] for item in attempts}) != len(attempts):
        raise ValueError("candidate repair attempt evidence is duplicated")
    return attempts


def _attempt_usage(attempts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for attempt in attempts:
        raw = attempt.get("usage")
        if not isinstance(raw, Mapping):
            raise ValueError("candidate repair attempt usage is invalid")
        for field in usage:
            value = raw.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("candidate repair attempt usage is invalid")
            usage[field] += value
            if usage[field] > 1_000_000_000:
                raise ValueError("candidate repair attempt usage exceeds V1")
    usage["total_tokens"] = max(
        usage["total_tokens"],
        usage["input_tokens"] + usage["output_tokens"],
    )
    return usage


def _bounded_count(value: Any, *, maximum: int) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(maximum, value)


def _truncation_projection(value: Any) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        return 0, 0
    if "truncated_section_count" in value or "dropped_item_count" in value:
        return (
            _bounded_count(value.get("truncated_section_count"), maximum=100),
            _bounded_count(value.get("dropped_item_count"), maximum=10_000),
        )
    sections = value.get("truncated_sections")
    counts = value.get("dropped_item_counts")
    truncated = min(100, len(sections)) if isinstance(sections, list) else 0
    dropped = 0
    if isinstance(counts, Mapping):
        for item in list(counts.values())[:100]:
            dropped = min(
                10_000,
                dropped + _bounded_count(item, maximum=10_000),
            )
    return truncated, dropped


def _dropped_reference_count(value: Any) -> int:
    if isinstance(value, Mapping):
        exact = value.get("dropped_reference_count")
        if type(exact) is int:
            return min(1_000, max(0, exact))
        return min(
            1_000,
            sum(_dropped_reference_count(item) for item in value.values()),
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return min(1_000, len(value))
    return int(bool(value))


def _state_result_projection(
    generation: ChapterGenerationResult,
) -> dict[str, Any]:
    value = generation.value
    if not isinstance(value, Mapping):
        raise ValueError("state repair result is not a proposal mapping")
    proposal_id = value.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise ValueError("state repair result has no proposal identity")
    truncated, dropped_items = _truncation_projection(generation.truncation)
    return StateCandidateRepairResultProjection(
        proposal_id=proposal_id,
        truncated_section_count=truncated,
        dropped_item_count=dropped_items,
        dropped_reference_count=_dropped_reference_count(generation.dropped),
    ).model_dump(mode="json")


class _StateRepairReceiptAttemptScope:
    """Bind the receipt's dispatch fence to the real paid-attempt claim."""

    def __init__(
        self,
        wrapped: Any,
        *,
        receipts: Any,
        fence: PreDispatchFenceV1,
    ) -> None:
        self._wrapped = wrapped
        self._receipts = receipts
        self._fence = fence

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def _mark(self, attempt_id: str) -> str:
        try:
            await self._receipts.mark_dispatched(
                receipt_id=self._fence.receipt_id,
                claim_token=self._fence.claim_token,
                claim_epoch=self._fence.claim_epoch,
                attempt_id=attempt_id,
            )
        except BaseException:
            release = getattr(self._wrapped, "release_pre_dispatch", None)
            if callable(release):
                await release(
                    attempt_id,
                    "state repair receipt dispatch fence failed",
                )
            raise
        return attempt_id

    async def claim(self, provider_alias: str, phase: str) -> str:
        return await self._mark(
            await self._wrapped.claim(provider_alias, phase)
        )

    async def claim_with_budget(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        claim = getattr(self._wrapped, "claim_with_budget", None)
        attempt_id = (
            await claim(provider_alias, phase, conservative_tokens)
            if callable(claim)
            else await self._wrapped.claim(provider_alias, phase)
        )
        return await self._mark(attempt_id)

    async def account(self, attempt_id: str, usage: Any) -> None:
        await self._wrapped.account(attempt_id, usage)

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        await self._wrapped.mark_uncertain(attempt_id, reason)

    async def release_pre_dispatch(self, attempt_id: str, reason: str) -> None:
        release = getattr(self._wrapped, "release_pre_dispatch", None)
        if callable(release):
            await release(attempt_id, reason)
        await self._receipts.release_pre_dispatch(
            receipt_id=self._fence.receipt_id,
            claim_token=self._fence.claim_token,
            claim_epoch=self._fence.claim_epoch,
            attempt_id=attempt_id,
        )


class ChapterCandidateRepairApplication:
    """Run one frozen repair cycle without exposing Runtime details to the job."""

    def __init__(
        self,
        *,
        execution_id: str,
        readiness: Mapping[str, Any],
        generation_params: Mapping[str, Any] | None,
        attempt_scope_factory: Callable[[str], FencedAttemptScope],
        deps: ChapterCandidateRepairApplicationDeps | None = None,
    ) -> None:
        if not str(execution_id):
            raise ValueError("candidate repair execution id is required")
        self._execution_id = str(execution_id)
        self._readiness = dict(readiness)
        self._generation_params = dict(generation_params or {})
        self._attempt_scope_factory = attempt_scope_factory
        self._deps = deps or ChapterCandidateRepairApplicationDeps.production()

    def _authorized_snapshot(
        self,
        *,
        chapter_id: str,
        cycle: int,
    ) -> tuple[Any, Any, GenerationPlan, GenerationPlan]:
        prefix = f"candidate-prose-repair:{cycle}:"
        bundle = self._deps.build_remediation_runtime(
            attempt_scope_factory=lambda step: self._attempt_scope_factory(
                f"{prefix}{step}"
            )
        )
        adherence_plan, state_plan = self._deps.plan_candidate_workflows()
        authorization = self._deps.validate_execution(
            readiness=self._readiness,
            chapter_id=chapter_id,
            remediation_bundle=bundle,
            adherence_plan=adherence_plan,
            state_plan=state_plan,
            generation_params=self._generation_params,
        )
        if authorization.prose_remediation is None:
            raise ValueError("candidate repair has no prose Runtime authority")
        return authorization, bundle, adherence_plan, state_plan

    def execution_snapshot(
        self,
        *,
        chapter_id: str,
    ) -> tuple[int, GenerationPlan, GenerationPlan]:
        """Validate and expose the frozen plans used by the outer candidate tail."""

        planning = self._readiness.get("planning")
        if not isinstance(planning, Mapping):
            raise ValueError("candidate repair readiness planning is invalid")
        raw_authorization = planning.get(
            "chapter_candidate_repair_authorization"
        )
        if not isinstance(raw_authorization, Mapping):
            raise ValueError("candidate repair authority is missing")
        authorization = parse_candidate_repair_authorization(
            raw_authorization
        )
        cycles = authorization.max_repair_cycles_per_chapter
        adherence_plan, state_plan = self._deps.plan_candidate_workflows()
        if cycles:
            _authorization, _bundle, adherence_plan, state_plan = (
                self._authorized_snapshot(
                    chapter_id=chapter_id,
                    cycle=1,
                )
            )
        else:
            authorized_candidate_repair_attempt_slots(
                self._readiness,
                chapter_id=chapter_id,
                generation_params=self._generation_params,
            )
        return cycles, adherence_plan, state_plan

    def _state_receipt_identity(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        request: StateCandidateRepairRequest,
    ) -> dict[str, Any]:
        digest_payload = {
            "schema_version": "state_candidate_repair_execution.v1",
            "execution_id": self._execution_id,
            "owner_id": str(owner_id),
            "novel_id": str(novel_id),
            "chapter_id": str(chapter_id),
            "readiness_digest": str(self._readiness.get("digest") or ""),
            "request": request.model_dump(mode="json"),
        }
        if not digest_payload["readiness_digest"]:
            raise ValueError("candidate repair readiness digest is missing")
        encoded = json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "owner_id": str(owner_id),
            "novel_id": str(novel_id),
            "chapter_id": str(chapter_id),
            "execution_id": self._execution_id,
            "cycle": request.cycle,
            "request_digest": hashlib.sha256(
                encoded.encode("utf-8")
            ).hexdigest(),
        }

    async def _state_attempts(
        self,
        *,
        chapter_id: str,
        cycle: int,
        claimed_are_uncertain: bool = False,
    ) -> list[dict[str, Any]]:
        return _ordered_attempt_projection(
            await self._raw_state_attempts(
                chapter_id=chapter_id,
                cycle=cycle,
            ),
            claimed_are_uncertain=claimed_are_uncertain,
        )

    async def _raw_state_attempts(
        self,
        *,
        chapter_id: str,
        cycle: int,
    ) -> Sequence[Mapping[str, Any]]:
        return await self._deps.read_ordered_attempts(
                job_id=self._execution_id,
                chapter_id=chapter_id,
                step_prefix=f"candidate-state-repair:{cycle}",
            )

    async def _reopen_released_state_receipt(
        self,
        *,
        chapter_id: str,
        cycle: int,
        receipt: Mapping[str, Any],
        new_claim_token: str,
    ) -> Mapping[str, Any]:
        raw_ids = receipt.get("provider_attempt_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return receipt
        attempt_ids = tuple(
            item for item in raw_ids if isinstance(item, str) and item
        )
        if len(attempt_ids) != len(raw_ids) or len(set(attempt_ids)) != len(
            attempt_ids
        ):
            raise ValueError("state repair receipt attempt identities are invalid")
        slots = await self._raw_state_attempts(
            chapter_id=chapter_id,
            cycle=cycle,
        )
        states = {
            str(slot.get("attempt_id") or ""): slot.get("state")
            for slot in slots
        }
        if not all(
            states.get(attempt_id) == "released_pre_dispatch"
            for attempt_id in attempt_ids
        ):
            return receipt
        return await self._deps.state_repair_receipts.reopen_released_pre_dispatch(
            receipt_id=str(receipt.get("_id") or ""),
            claim_token=str(receipt.get("claim_token") or ""),
            claim_epoch=receipt.get("claim_epoch"),
            new_claim_token=new_claim_token,
            attempt_ids=attempt_ids,
        )

    async def _discard_reserved_state_attempts(
        self,
        *,
        chapter_id: str,
        cycle: int,
        fence: PreDispatchFenceV1,
    ) -> None:
        step_id = f"candidate-state-repair:{cycle}"
        if fence.step_id != step_id:
            raise ValueError("state repair cleanup fence does not match its cycle")
        for _attempt in range(4):
            slots = await self._raw_state_attempts(
                chapter_id=chapter_id,
                cycle=cycle,
            )
            orphaned = [
                slot
                for slot in slots
                if slot.get("state") in {
                    "claimed",
                    "released_pre_dispatch",
                }
            ]
            if not orphaned:
                return
            changed = False
            for slot in orphaned:
                attempt_id = slot.get("attempt_id")
                if not isinstance(attempt_id, str) or not attempt_id:
                    raise ValueError(
                        "state repair pre-dispatch attempt identity is invalid"
                    )
                changed = bool(
                    await self._deps.discard_pre_dispatch_attempt(
                        self._execution_id,
                        chapter_id,
                        step_id,
                        attempt_id,
                        current_pre_dispatch_fence=fence,
                    )
                ) or changed
            if not changed:
                continue
        raise ValueError("state repair pre-dispatch attempts could not reconcile")

    async def _recover_state_generation(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        request: StateCandidateRepairRequest,
        receipt: Mapping[str, Any],
        require_result_projection: bool,
    ) -> ChapterGenerationResult:
        raw_projection = receipt.get("result_projection")
        projection = (
            StateCandidateRepairResultProjection.model_validate(raw_projection)
            if isinstance(raw_projection, Mapping)
            else None
        )
        if require_result_projection and projection is None:
            raise ValueError("completed state repair receipt has no result")
        recovered = await self._deps.recover_state_proposal(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            proposal_id=(projection.proposal_id if projection else None),
            request_id=str(receipt.get("_id") or ""),
            source_run_id=request.source_run_id,
            source_run_revision=request.source_run_revision,
            source_content_digest=request.source_content_digest,
        )
        if recovered is None:
            raise _StateRepairProposalUnavailable(
                "state repair proposal result is unavailable"
            )
        if isinstance(recovered, Mapping):
            value = dict(recovered)
            recovered_context = None
            recovered_dropped = None
        else:
            value = dict(getattr(recovered, "value", {}) or {})
            raw_truncated = getattr(
                recovered,
                "truncated_section_count",
                None,
            )
            raw_dropped_items = getattr(
                recovered,
                "dropped_item_count",
                None,
            )
            raw_dropped_references = getattr(
                recovered,
                "dropped_reference_count",
                None,
            )
            if any(
                type(item) is not int or item < 0
                for item in (
                    raw_truncated,
                    raw_dropped_items,
                    raw_dropped_references,
                )
            ):
                raise ValueError(
                    "state repair proposal recovery metadata is invalid"
                )
            recovered_context = (
                min(100, raw_truncated),
                min(10_000, raw_dropped_items),
            )
            recovered_dropped = min(1_000, raw_dropped_references)
        if not value.get("proposal_id") or not value.get("acceptance_token"):
            raise ValueError("state repair proposal result is incomplete")
        if projection is None:
            if recovered_context is None or recovered_dropped is None:
                raise ValueError(
                    "state repair proposal recovery metadata is unavailable"
                )
            projection = StateCandidateRepairResultProjection(
                proposal_id=str(value["proposal_id"]),
                truncated_section_count=recovered_context[0],
                dropped_item_count=recovered_context[1],
                dropped_reference_count=recovered_dropped,
            )
        elif str(value["proposal_id"]) != projection.proposal_id:
            raise ValueError("state repair receipt proposal identity diverged")
        elif recovered_context is not None and (
            recovered_context
            != (
                projection.truncated_section_count,
                projection.dropped_item_count,
            )
            or recovered_dropped != projection.dropped_reference_count
        ):
            raise ValueError("state repair receipt metadata diverged")
        attempts = await self._state_attempts(
            chapter_id=chapter_id,
            cycle=request.cycle,
        )
        return ChapterGenerationResult(
            stage=ChapterGenerationStage.STATE,
            value=value,
            usage=_attempt_usage(attempts),
            attempts=attempts,
            truncation={
                "truncated_section_count": projection.truncated_section_count,
                "dropped_item_count": projection.dropped_item_count,
            },
            dropped=(
                {
                    "dropped_reference_count": (
                        projection.dropped_reference_count
                    )
                }
                if projection.dropped_reference_count
                else {}
            ),
            accepted=False,
        )

    async def _source(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        revision: int,
        digest: str,
    ) -> ProseCandidateSource:
        run = await self._deps.get_prose_run(run_id, owner_id)
        text = str(run.get("assembled_text") or "")
        if (
            str(run.get("_id") or "") != run_id
            or str(run.get("owner_id") or "") != owner_id
            or str(run.get("novel_id") or "") != novel_id
            or str(run.get("chapter_id") or "") != chapter_id
            or type(run.get("revision")) is not int
            or int(run["revision"]) != revision
            or chapter_content_digest(text) != digest
        ):
            raise ValueError("candidate repair source identity changed")
        completion = dict(run.get("completion") or {})
        completion.update({
            "source_run_id": run_id,
            "source_run_revision": revision,
            "source_run_digest": digest,
        })
        return ProseCandidateSource(
            text=text,
            source_run_id=run_id,
            source_run_revision=revision,
            source_content_digest=digest,
            completion=completion,
        )

    async def recover_source(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        revision: int,
        digest: str,
    ) -> ProseCandidateSource:
        """Rebuild one exact persisted ProseRun without dispatching a Provider."""

        return await self._source(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=run_id,
            revision=revision,
            digest=digest,
        )

    @staticmethod
    def _goal(request: ProseCandidateRepairRequest) -> str:
        categories = ",".join(item.value for item in request.issue_categories)
        scenes = ",".join(str(item) for item in request.scene_indexes) or "none"
        reasons = ",".join(request.reason_codes)
        return (
            "修复已冻结的正文候选；"
            f"trigger={request.trigger}; reasons={reasons}; "
            f"issue_categories={categories}; scene_indexes={scenes}; "
            f"revision={request.source_run_revision}; "
            f"content_digest={request.source_content_digest}。"
        )

    async def repair_prose_candidate(
        self,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        request: ProseCandidateRepairRequest,
    ) -> ProseCandidateRepairReceipt:
        authorization, bundle, _adherence_plan, _state_plan = (
            self._authorized_snapshot(
                chapter_id=chapter_id,
                cycle=request.cycle,
            )
        )
        prose = authorization.prose_remediation
        assert prose is not None
        start_request_id = (
            f"{self._execution_id}:{chapter_id}:prose-repair:{request.cycle}"
        )
        existing = await self._deps.find_agent_run(
            owner_id=owner_id,
            start_request_id=start_request_id,
        )
        if existing is None:
            await self._source(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                run_id=request.source_run_id,
                revision=request.source_run_revision,
                digest=request.source_content_digest,
            )
            inspected = await bundle.runtime.inspect_readiness(
                owner_id=owner_id,
                request=AgentReadinessRequest(
                    novel_id=novel_id,
                    goal=self._goal(request),
                    scope=AgentScope(
                        kind=REMEDIATION_SCOPE_KIND,
                        object_id=request.source_run_id,
                    ),
                    allowed_tools=prose.allowed_tools,
                    allowed_effects=prose.allowed_effects,
                    allowed_change_classes=prose.allowed_change_classes,
                    allowed_external_data_categories=(
                        prose.allowed_external_data_categories
                    ),
                    limits=prose.limits,
                ),
            )
            view = await bundle.runtime.start(
                owner_id=owner_id,
                readiness_id=inspected.readiness_id,
                digest=inspected.digest,
                start_request_id=start_request_id,
            )
        else:
            view = await bundle.runtime.resume(
                owner_id=owner_id,
                run_id=str(existing["_id"]),
            )

        attempts = _ordered_attempt_projection(
            await self._deps.read_ordered_attempts(
                job_id=self._execution_id,
                chapter_id=chapter_id,
                step_prefix=f"candidate-prose-repair:{request.cycle}:",
            )
        )
        usage = _attempt_usage(attempts)
        if (
            view.status != "completed"
            or view.termination is None
            or view.termination.reason_code != "goal_satisfied"
        ):
            raise CandidateRepairRunStopped(
                f"candidate prose repair stopped with status {view.status}",
                usage=usage,
                attempts=attempts,
            )
        if (
            int(view.usage.paid_attempts) != len(attempts)
            or int(view.usage.total_tokens) != usage["total_tokens"]
        ):
            raise ValueError("candidate repair Agent usage evidence diverged")

        repaired_run = await self._deps.get_prose_run(
            request.source_run_id,
            owner_id,
        )
        repaired_revision = repaired_run.get("revision")
        repaired_text = str(repaired_run.get("assembled_text") or "")
        repaired_digest = chapter_content_digest(repaired_text)
        remediation = dict(repaired_run.get("remediation") or {})
        verification = dict(remediation.get("verification") or {})
        completion = dict(repaired_run.get("completion") or {})
        verification_revision = verification.get("candidate_revision")
        if (
            type(repaired_revision) is not int
            or repaired_revision <= request.source_run_revision
            or str(repaired_run.get("status") or "") != "complete"
            or str(repaired_run.get("novel_id") or "") != novel_id
            or str(repaired_run.get("chapter_id") or "") != chapter_id
            or remediation.get("schema_version")
            != "prose_run_remediation.v1"
            or verification.get("schema_version")
            != "prose_remediation_verification.v1"
            or str(verification.get("agent_run_id") or "") != view.run_id
            or type(verification_revision) is not int
            or verification_revision != repaired_revision
            or str(verification.get("content_digest") or "")
            != repaired_digest
            or completion.get("can_write_formal_prose") is not True
            or str(completion.get("status") or "") != "complete"
            or str(completion.get("finish_reason") or "") != "stop"
        ):
            raise ValueError("candidate repair completion receipt is invalid")
        completion.update({
            "source_run_id": request.source_run_id,
            "source_run_revision": repaired_revision,
            "source_run_digest": repaired_digest,
        })
        source = ProseCandidateSource(
            text=repaired_text,
            source_run_id=request.source_run_id,
            source_run_revision=repaired_revision,
            source_content_digest=repaired_digest,
            completion=completion,
        )
        generation = ChapterGenerationResult(
            stage=ChapterGenerationStage.PROSE,
            value=repaired_text,
            usage=usage,
            attempts=attempts,
            truncation={},
            completion=completion,
            accepted=False,
        )
        return ProseCandidateRepairReceipt(
            generation=generation,
            source=source,
        )

    async def repair_state_candidate(
        self,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        request: StateCandidateRepairRequest,
    ) -> StateCandidateRepairReceipt:
        try:
            return await self._repair_state_candidate(
                owner_id,
                novel_id,
                chapter_id,
                request,
            )
        except CandidateRepairRunStopped:
            raise
        except Exception as exc:
            claimed_are_uncertain = False
            try:
                identity = self._state_receipt_identity(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    chapter_id=chapter_id,
                    request=request,
                )
                receipt = await self._deps.state_repair_receipts.find_receipt(
                    **identity
                )
                claimed_are_uncertain = bool(
                    receipt is not None
                    and receipt.get("state") == "dispatched"
                )
            except Exception:
                claimed_are_uncertain = True
            attempts = await self._state_attempts(
                chapter_id=chapter_id,
                cycle=request.cycle,
                claimed_are_uncertain=claimed_are_uncertain,
            )
            if attempts:
                raise CandidateRepairRunStopped(
                    "state repair failed after a persisted Provider attempt",
                    usage=_attempt_usage(attempts),
                    attempts=attempts,
                ) from exc
            raise

    async def _repair_state_candidate(
        self,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        request: StateCandidateRepairRequest,
    ) -> StateCandidateRepairReceipt:
        _authorization, _bundle, _adherence_plan, state_plan = (
            self._authorized_snapshot(
                chapter_id=chapter_id,
                cycle=request.cycle,
            )
        )
        identity = self._state_receipt_identity(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            request=request,
        )
        existing = await self._deps.state_repair_receipts.find_receipt(
            **identity
        )
        reconciled_claim_token: str | None = None
        if existing is not None and existing.get("state") == "dispatched":
            proposed_claim_token = uuid4().hex
            existing = await self._reopen_released_state_receipt(
                chapter_id=chapter_id,
                cycle=request.cycle,
                receipt=existing,
                new_claim_token=proposed_claim_token,
            )
            if (
                existing.get("state") == "reserved"
                and existing.get("claim_token") == proposed_claim_token
            ):
                reconciled_claim_token = proposed_claim_token
        if existing is not None and existing.get("state") == "completed":
            generation = await self._recover_state_generation(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                request=request,
                receipt=existing,
                require_result_projection=True,
            )
            return StateCandidateRepairReceipt(
                request_id=str(existing["_id"]),
                generation=generation,
            )
        if existing is not None and existing.get("state") == "dispatched":
            try:
                generation = await self._recover_state_generation(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    chapter_id=chapter_id,
                    request=request,
                    receipt=existing,
                    require_result_projection=False,
                )
            except _StateRepairProposalUnavailable:
                attempts = await self._state_attempts(
                    chapter_id=chapter_id,
                    cycle=request.cycle,
                    claimed_are_uncertain=True,
                )
                raise CandidateRepairRunStopped(
                    "state repair Provider result is uncertain",
                    usage=_attempt_usage(attempts),
                    attempts=attempts,
                ) from None
            await self._deps.state_repair_receipts.complete_receipt(
                receipt_id=str(existing["_id"]),
                claim_token=str(existing.get("claim_token") or ""),
                claim_epoch=existing.get("claim_epoch"),
                result_projection=_state_result_projection(generation),
            )
            return StateCandidateRepairReceipt(
                request_id=str(existing["_id"]),
                generation=generation,
            )

        source = await self._source(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=request.source_run_id,
            revision=request.source_run_revision,
            digest=request.source_content_digest,
        )
        prior = await self._deps.get_state_proposal(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            proposal_id=request.proposal_id,
        )
        audit = prior.get("generation_audit")
        if (
            str(prior.get("_id") or "") != request.proposal_id
            or str(prior.get("owner_id") or "") != owner_id
            or str(prior.get("novel_id") or "") != novel_id
            or str(prior.get("chapter_id") or "") != chapter_id
            or str(prior.get("status") or "") != "proposed"
            or str(prior.get("source_prose_run_id") or "")
            != request.source_run_id
            or type(prior.get("source_prose_run_revision")) is not int
            or prior.get("source_prose_run_revision")
            != request.source_run_revision
            or str(prior.get("source_content_digest") or "")
            != request.source_content_digest
            or not isinstance(audit, Mapping)
            or audit.get("workflow") != STATE_WORKFLOW
            or audit.get("step") != STATE_STEP
            or audit.get("mode") != "system"
        ):
            raise ValueError("state repair proposal identity is invalid")
        claim_token = reconciled_claim_token or uuid4().hex
        receipt_state, receipt = (
            await self._deps.state_repair_receipts.claim_receipt(
                **identity,
                claim_token=claim_token,
            )
        )
        if receipt_state == "completed":
            generation = await self._recover_state_generation(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                request=request,
                receipt=receipt,
                require_result_projection=True,
            )
            return StateCandidateRepairReceipt(
                request_id=str(receipt["_id"]),
                generation=generation,
            )
        if receipt_state != "claimed":
            attempts = await self._state_attempts(
                chapter_id=chapter_id,
                cycle=request.cycle,
                claimed_are_uncertain=(receipt_state == "in_progress_dispatched"),
            )
            raise CandidateRepairRunStopped(
                f"state repair receipt is {receipt_state}",
                usage=_attempt_usage(attempts),
                attempts=attempts,
            )
        claim_epoch = receipt.get("claim_epoch")
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("state repair receipt claim epoch is invalid")
        fence = PreDispatchFenceV1(
            receipt_id=str(receipt["_id"]),
            claim_token=claim_token,
            claim_epoch=claim_epoch,
            cycle=request.cycle,
        )
        base_attempt_scope = self._attempt_scope_factory(
            f"candidate-state-repair:{request.cycle}"
        )
        try:
            bind_fence = base_attempt_scope.bind_pre_dispatch_fence
        except AttributeError as exc:
            raise ValueError(
                "state repair requires a fenced paid-attempt scope"
            ) from exc
        await bind_fence(fence)
        await self._discard_reserved_state_attempts(
            chapter_id=chapter_id,
            cycle=request.cycle,
            fence=fence,
        )
        attempt_scope = _StateRepairReceiptAttemptScope(
            base_attempt_scope,
            receipts=self._deps.state_repair_receipts,
            fence=fence,
        )
        generation = await self._deps.generate_state_candidate(
            novel_id,
            {"_id": chapter_id},
            source,
            attempt_scope=attempt_scope,
            generation_params=self._generation_params,
            generation_plan=state_plan,
            request_id=str(receipt["_id"]),
            repair_guidance=StateRepairGuidance(
                cycle=request.cycle,
                prior_proposal_id=request.proposal_id,
                reason_codes=request.reason_codes,
                consistency_issue_count=request.consistency_issue_count,
                affected_card_ids=request.affected_card_ids,
                dropped_reference_count=request.dropped_reference_count,
            ),
        )
        if (
            generation.stage is not ChapterGenerationStage.STATE
            or generation.accepted
        ):
            raise ValueError("state repair did not return a deferred candidate")
        attempts = await self._state_attempts(
            chapter_id=chapter_id,
            cycle=request.cycle,
        )
        generation = generation.model_copy(
            update={
                "usage": _attempt_usage(attempts),
                "attempts": attempts,
            },
            deep=True,
        )
        await self._deps.state_repair_receipts.complete_receipt(
            receipt_id=str(receipt["_id"]),
            claim_token=claim_token,
            claim_epoch=claim_epoch,
            result_projection=_state_result_projection(generation),
        )
        return StateCandidateRepairReceipt(
            request_id=str(receipt["_id"]),
            generation=generation,
        )
