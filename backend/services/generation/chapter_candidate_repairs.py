"""Production adapters for the bounded candidate-repair phase."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.db.repositories.agent_runtime_repository import (
    agent_runtime_repository,
)
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.services.agent_runtime.contracts import (
    AgentReadinessRequest,
    AgentScope,
)
from backend.services.generation.chapter_candidate_authorization import (
    CandidateRepairAuthorization,
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
from backend.services.generation.prose_remediation_runtime import (
    REMEDIATION_SCOPE_KIND,
    build_prose_remediation_runtime,
)
from backend.services.generation.prose_runs import chapter_content_digest
from backend.services.llm.generation_runtime import (
    AttemptScope,
    GenerationPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)


_MAX_REPAIR_ATTEMPT_EVIDENCE = 64


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


@dataclass(frozen=True)
class ChapterCandidateRepairApplicationDeps:
    build_remediation_runtime: Callable[..., Any]
    plan_candidate_workflows: Callable[[], tuple[GenerationPlan, GenerationPlan]]
    validate_execution: Callable[..., CandidateRepairAuthorization]
    find_agent_run: Callable[..., Awaitable[Mapping[str, Any] | None]]
    get_prose_run: Callable[[str, str], Awaitable[Mapping[str, Any]]]
    generate_state_candidate: Callable[..., Awaitable[ChapterGenerationResult]]

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


def _bundle_attempts(bundle: Any) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for call in (
        bundle.planner_call,
        bundle.rewrite_call,
        bundle.adherence_call,
    ):
        attempts.extend(
            _attempt_projection(item)
            for item in tuple(getattr(call.runtime, "attempts", ()) or ())
        )
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
    usage["total_tokens"] = max(
        usage["total_tokens"],
        usage["input_tokens"] + usage["output_tokens"],
    )
    return usage


class ChapterCandidateRepairApplication:
    """Run one frozen repair cycle without exposing Runtime details to the job."""

    def __init__(
        self,
        *,
        execution_id: str,
        readiness: Mapping[str, Any],
        generation_params: Mapping[str, Any] | None,
        attempt_scope_factory: Callable[[str], AttemptScope],
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
        await self._source(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=request.source_run_id,
            revision=request.source_run_revision,
            digest=request.source_content_digest,
        )
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

        attempts = _bundle_attempts(bundle)
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
        source = await self._source(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=request.source_run_id,
            revision=request.source_run_revision,
            digest=request.source_content_digest,
        )
        _authorization, _bundle, _adherence_plan, state_plan = (
            self._authorized_snapshot(
                chapter_id=chapter_id,
                cycle=request.cycle,
            )
        )
        generation = await self._deps.generate_state_candidate(
            novel_id,
            {"_id": chapter_id},
            source,
            attempt_scope=self._attempt_scope_factory(
                f"candidate-state-repair:{request.cycle}"
            ),
            generation_params=self._generation_params,
            generation_plan=state_plan,
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
        return StateCandidateRepairReceipt(generation=generation)
