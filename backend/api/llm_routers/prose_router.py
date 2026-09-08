"""AI 正文生成路由：流式生成并持久化可恢复的 ``ProseRun`` 草稿。

生成阶段不会直接覆盖正式章节正文；每次 Provider 调用在派发前保存 uncertain
检查点，片段完成后再更新终态。只有独立 accept 端点在 owner、revision 与
完整性校验通过后，才以可恢复 mutation 写入 ``chapter.content``。

与 outline_router 一致，依赖在 event_stream 内取用，使测试可 monkeypatch
本模块的全局名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from backend.services.generation.chapter_review_policy import ReviewEnforcement

from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
)
from backend.api.prose_continuation_contracts import (
    ProseContinuationPolicyRequest,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import to_object_id
from backend.llm.prompts.prompt_selector import PROSE_PROMPT_NAME, load_prompt_config
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_context,
    fetch_context_inputs,
)
from backend.services.llm.agent_orchestrator import apply_agent_profile
from backend.services.llm.prose_runner import stream_prose
from backend.services.llm.workflow_runner import sse_event
from backend.services.llm.workflow_service import get_llm_service_for_step
from backend.services.llm.generation_runtime import WorkflowStepTarget, create_generation_runtime
from backend.services.generation.prose_completion import prose_completion_module
from backend.services.generation.prose_generation import execute_prose_plan
from backend.services.generation.prose_continuation import (
    ProseContinuationPolicy,
    authorization_ruleset_requires_refresh,
)
from backend.services.generation.prose_readiness import (
    ProseReadinessBlocked,
    StaleProseReadiness,
    build_prose_readiness,
    validate_prose_readiness,
)
from backend.services.generation.prose_run_attempt_scope import ProseRunAttemptScope
from backend.services.generation.judge_review_records import judge_review_records
from backend.services.generation.prose_runs import (
    prose_run_module,
    serialize_prose_run,
)
from backend.services.generation.interactive_chapter_completion import (
    InteractiveCompletionBlocked,
    InteractiveCompletionRequestBinding,
    build_interactive_completion_request_binding,
    interactive_chapter_completion_service,
)
from backend.services.generation.chapter_generation_application import (
    AcceptanceAuthority,
    ChapterGenerationApplicationDeps,
    ChapterGenerationApplicationService,
    ProseGenerationCommand,
)
from backend.services.generation.chapter_capability_registry import (
    build_chapter_capability_registry,
)
from backend.services.llm.capability_registry import CapabilityCall
from backend.services.novel.style_controls import render_style_controls
from backend.db.repositories.prose_run_repository import prose_run_repo

from backend.api.default_routers.auth_router import (
    require_owned_body_resource,
    require_owned_path_resource,
)

router = APIRouter(
    prefix="/api/llm",
    tags=["llm"],
    dependencies=[Depends(require_owned_body_resource)],
)
logger = logging.getLogger(__name__)

PROSE_WORKFLOW = "write_chapter_by_ai"
PROSE_STEP = "chapter_content"


def _load_prompts() -> dict:
    return load_prompt_config()


def _chapter_generation_service() -> ChapterGenerationApplicationService:
    """把可替换基础设施接到统一正文应用服务。"""

    def create_runtime(*, attempt_scope=None, **kwargs):
        return create_generation_runtime(
            attempt_scope=attempt_scope,
            **kwargs,
        )

    production = ChapterGenerationApplicationDeps.production()
    return ChapterGenerationApplicationService(
        replace(
            production,
            novel_repo=novel_repo,
            chapter_repo=chapter_repo,
            fetch_context_inputs=fetch_context_inputs,
            assemble_context=assemble_context,
            create_runtime=create_runtime,
            load_prompts=_load_prompts,
            get_legacy_service=get_llm_service_for_step,
            stream_prose=stream_prose,
            prose_completion=prose_completion_module,
            execute_prose_plan=execute_prose_plan,
            prose_runs=prose_run_module,
            prose_run_repo=prose_run_repo,
            create_prose_attempt_scope=ProseRunAttemptScope,
        )
    )


def _chapter_capability_registry():
    return build_chapter_capability_registry(
        service_factory=_chapter_generation_service,
    )


class ProseRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)
    resume_run_id: str | None = Field(default=None, min_length=1)
    expected_run_revision: int | None = Field(default=None, ge=1)
    confirm_uncertain_retry: bool = False
    prose_continuation_policy: ProseContinuationPolicyRequest | None = None
    readiness_digest: str | None = Field(default=None, min_length=1, max_length=128)
    confirm_automatic_continuations: bool = False
    token_budget: int | None = Field(default=None, ge=1)


class AcceptProseRunRequest(ProseRequest):
    expected_run_revision: int = Field(ge=1)
    accept_partial: bool = False
    partial_acknowledgement: bool = False


class DiscardProseRunRequest(ProseRequest):
    expected_run_revision: int = Field(ge=1)


class InteractiveCompletionReadinessRequest(GenerationParamsMixin):
    novel_id: str = Field(..., min_length=1)
    chapter_id: str = Field(..., min_length=1)
    expected_run_revision: int = Field(ge=1)
    review_requested: bool = False
    review_enforcement: ReviewEnforcement = "advisory"


class InteractiveCompletionAuthorityRequest(
    InteractiveCompletionReadinessRequest
):
    authorization_id: str = Field(
        min_length=24,
        max_length=24,
        pattern=r"^[0-9a-f]{24}$",
    )
    authorization_revision: int = Field(ge=1)
    completion_readiness_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class CompleteInteractiveProseRunRequest(
    InteractiveCompletionAuthorityRequest
):
    completion_readiness_confirmed: bool = False
    review_requested: bool | None = None


class ResolveInteractiveCompletionRequest(
    InteractiveCompletionAuthorityRequest
):
    action: Literal["retry", "abort"]


def _interactive_completion_request_binding(
    *,
    owner_id: str,
    run_id: str,
    request: InteractiveCompletionAuthorityRequest,
) -> InteractiveCompletionRequestBinding:
    return build_interactive_completion_request_binding(
        owner_id=owner_id,
        novel_id=request.novel_id,
        chapter_id=request.chapter_id,
        run_id=run_id,
        run_revision=request.expected_run_revision,
        authorization_id=request.authorization_id,
        authorization_revision=request.authorization_revision,
        readiness_digest=request.completion_readiness_digest,
    )


def _interactive_completion_conflict(
    exc: InteractiveCompletionBlocked,
) -> HTTPException:
    detail = {"code": exc.code, "message": str(exc)}
    if exc.diagnostics is not None:
        detail["diagnostics"] = exc.diagnostics
    return HTTPException(
        status_code=409,
        detail=detail,
    )


@dataclass(frozen=True)
class _ProseReadinessPreflight:
    """The immutable planning result reused by the first prose request."""

    plan: Any
    execution_plan: Any
    policy: ProseContinuationPolicy
    readiness: Any


def _provider_capability(plan: Any) -> dict[str, Any]:
    return {
        "max_output_tokens": getattr(plan, "max_output_tokens", None),
        "model": getattr(plan, "provider_model", ""),
    }


async def _authorization_revision_for(
    *,
    req: ProseRequest,
    request: Request,
    policy: ProseContinuationPolicy,
    token_budget: int | None,
) -> int:
    """Advance only the mutable continuation authorization revision."""
    actor = getattr(request.state, "actor", None)
    if actor is None or not req.resume_run_id:
        return 1
    existing = await prose_run_repo.get_run(
        req.resume_run_id,
        str(actor.id),
    )
    stored = dict(existing.get("prose_continuation_authorization") or {})
    stored_policy = dict(stored.get("policy") or {})
    stored_budget = stored.get("token_budget")
    stored_revision = int(
        stored.get("authorization_revision")
        or existing.get("authorization_revision")
        or 0
    )
    if (
        stored_policy == policy.to_dict()
        and stored_budget == token_budget
        and not authorization_ruleset_requires_refresh(stored, policy=policy)
    ):
        return max(1, stored_revision)
    return max(1, stored_revision + 1)


async def _build_prose_readiness(
    *,
    req: ProseRequest,
    request: Request,
    chapter: dict[str, Any],
    context: Any,
    prompt: str,
    words_per_chapter: int,
    gen_kwargs: dict[str, Any],
) -> _ProseReadinessPreflight:
    """Plan one prose request without creating a run or calling a Provider."""
    runtime = create_generation_runtime(**build_runtime_kwargs(req))
    plan = runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP))
    execution_plan = prose_completion_module.plan(
        outline=chapter.get("outline") or {},
        target_word_count=int(words_per_chapter),
        provider_capability=_provider_capability(plan),
        request_overrides=gen_kwargs,
    )
    policy = (
        req.prose_continuation_policy.to_domain()
        if req.prose_continuation_policy is not None
        else ProseContinuationPolicy()
    )
    authorization_revision = await _authorization_revision_for(
        req=req,
        request=request,
        policy=policy,
        token_budget=req.token_budget,
    )
    readiness = build_prose_readiness(
        execution_plan=execution_plan,
        generation_plan=plan,
        policy=policy,
        token_budget=req.token_budget,
        authorization_revision=authorization_revision,
        novel_id=req.novel_id,
        chapter_id=req.chapter_id,
        outline=chapter.get("outline") or {},
        context_text=context.to_prompt_text(),
        base_prompt=prompt,
        generation_kwargs=gen_kwargs,
    )
    return _ProseReadinessPreflight(
        plan=plan,
        execution_plan=execution_plan,
        policy=policy,
        readiness=readiness,
    )


@dataclass(frozen=True)
class _PreparedProseInputs:
    novel: dict[str, Any]
    chapter: dict[str, Any]
    context: Any
    words_per_chapter: int
    prompt: str
    gen_kwargs: dict[str, Any]


async def _prepare_prose_inputs(req: ProseRequest) -> _PreparedProseInputs:
    """Load and render the stable content identity for one chapter request."""
    novel = await novel_repo.get_novel_by_id(req.novel_id)
    chapter = await chapter_repo.get_chapter_by_id(req.chapter_id)
    if chapter.get("novel_id") != to_object_id(req.novel_id):
        raise ValueError("该章节不属于指定小说")
    if not chapter.get("outline"):
        raise ValueError("本章还没有已接受的细纲，请先生成并接受章节细纲")
    context = assemble_context(
        await fetch_context_inputs(req.novel_id, req.chapter_id, purpose="prose")
    )
    words_per_chapter = int(
        (chapter.get("outline") or {}).get("target_word_count")
        or novel.get("words_per_chapter")
        or 3000
    )
    prompts = _load_prompts().get(PROSE_PROMPT_NAME, {})
    prompt = apply_agent_profile(
        "chapter_writer",
        prompts[f"{PROSE_STEP}_prompt_base"].format(
            context=context.to_prompt_text(),
            chapter_order=int(chapter.get("order_index") or 0),
            chapter_title=str(chapter.get("title") or ""),
            style_controls=render_style_controls(novel.get("style_controls")),
            words_per_chapter=words_per_chapter,
        )
        + "\n"
        + prompts[f"{PROSE_STEP}_prompt_without_schema_suffix"],
    )
    return _PreparedProseInputs(
        novel=novel,
        chapter=chapter,
        context=context,
        words_per_chapter=words_per_chapter,
        prompt=prompt,
        gen_kwargs=build_gen_kwargs(req),
    )


@router.get(
    "/prose-runs/novel/{novel_id}/telemetry",
    dependencies=[Depends(require_owned_path_resource)],
)
async def list_prose_run_telemetry(
    novel_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    chapter_id: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
):
    """List metadata-only prose-run telemetry; never return prose or prompts."""
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await prose_run_module.list_telemetry(
            owner_id=str(actor.id),
            novel_id=novel_id,
            limit=limit,
            skip=offset,
            chapter_id=chapter_id,
            generation_job_id=job_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/prose-runs/{run_id}/telemetry")
async def inspect_prose_run_telemetry(run_id: str, request: Request):
    """Inspect one owned prose run even when it is outside the list window."""
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await prose_run_module.inspect_telemetry(
            owner_id=str(actor.id),
            run_id=run_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/prose-runs/novel/{novel_id}/leftovers",
    dependencies=[Depends(require_owned_path_resource)],
)
async def list_leftover_prose_runs(novel_id: str, request: Request):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await prose_run_module.list_leftovers(
            owner_id=str(actor.id),
            novel_id=novel_id,
        )
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/prose-runs/chapter/{chapter_id}",
    dependencies=[Depends(require_owned_path_resource)],
)
async def inspect_active_prose_run(chapter_id: str, request: Request):
    try:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        inputs = await fetch_context_inputs(str(chapter["novel_id"]), chapter_id, purpose="prose")
        context = assemble_context(inputs)
        actor = getattr(request.state, "actor", None)
        if actor is None:
            raise HTTPException(status_code=401, detail="需要登录")
        run = await prose_run_module.inspect_active(
            owner_id=str(actor.id),
            chapter_id=chapter_id,
            outline=chapter.get("outline") or {},
            context_text=context.to_prompt_text(),
        )
        return serialize_prose_run(run)
    except HTTPException:
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ContextBudgetError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/prose-runs/chapter/{chapter_id}/judge-reviews", dependencies=[Depends(require_owned_path_resource)])
async def list_judge_review_records(chapter_id: str, request: Request, before: str | None = Query(default=None, max_length=24)):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await judge_review_records.list_chapter(owner_id=str(actor.id), chapter_id=chapter_id, before=before)
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="审查记录分页标识无效") from exc


@router.get("/prose-runs/chapter/{chapter_id}/judge-reviews/{record_id}", dependencies=[Depends(require_owned_path_resource)])
async def inspect_judge_review_record(chapter_id: str, record_id: str, request: Request):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await judge_review_records.detail(owner_id=str(actor.id), chapter_id=chapter_id, record_id=record_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="审查记录不存在") from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="审查记录标识无效") from exc


@router.post("/prose-runs/{run_id}/accept")
async def accept_prose_run(
    run_id: str,
    req: AcceptProseRunRequest,
    request: Request,
):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await prose_run_module.accept(
            owner_id=str(actor.id),
            run_id=run_id,
            chapter_id=req.chapter_id,
            expected_revision=req.expected_run_revision,
            accept_partial=req.accept_partial,
            partial_acknowledgement=req.partial_acknowledgement,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/completion-readiness")
async def inspect_interactive_completion_readiness(
    run_id: str,
    req: InteractiveCompletionReadinessRequest,
    request: Request,
):
    """Show the additional semantic/state cost before any paid call."""

    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        inspection = (
            await interactive_chapter_completion_service.inspect_with_notices(
                owner_id=str(actor.id),
                novel_id=req.novel_id,
                chapter_id=req.chapter_id,
                run_id=run_id,
                run_revision=req.expected_run_revision,
                review_requested=req.review_requested,
                review_enforcement=req.review_enforcement,
            )
        )
        return inspection.model_dump(mode="json")
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InteractiveCompletionBlocked as exc:
        raise _interactive_completion_conflict(exc) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/complete")
async def complete_interactive_prose_run(
    run_id: str,
    req: CompleteInteractiveProseRunRequest,
    request: Request,
):
    """Consume one explicitly confirmed completion readiness and V2-finalize."""

    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await interactive_chapter_completion_service.complete(
            request=_interactive_completion_request_binding(
                owner_id=str(actor.id),
                run_id=run_id,
                request=req,
            ),
            confirmed=req.completion_readiness_confirmed,
            review_requested=req.review_requested,
            review_enforcement=req.review_enforcement,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InteractiveCompletionBlocked as exc:
        raise _interactive_completion_conflict(exc) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/complete/status")
async def inspect_interactive_completion_progress(
    run_id: str,
    req: InteractiveCompletionAuthorityRequest,
    request: Request,
):
    """Read content-free progress for one bound completion authorization."""

    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await interactive_chapter_completion_service.inspect_progress(
            request=_interactive_completion_request_binding(
                owner_id=str(actor.id),
                run_id=run_id,
                request=req,
            ),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InteractiveCompletionBlocked as exc:
        raise _interactive_completion_conflict(exc) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/complete/uncertain-resolution")
async def resolve_interactive_completion_uncertainty(
    run_id: str,
    req: ResolveInteractiveCompletionRequest,
    request: Request,
):
    """Explicitly retry or abort one frozen uncertain completion attempt."""

    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        return await interactive_chapter_completion_service.resolve_uncertain(
            request=_interactive_completion_request_binding(
                owner_id=str(actor.id),
                run_id=run_id,
                request=req,
            ),
            action=req.action,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InteractiveCompletionBlocked as exc:
        raise _interactive_completion_conflict(exc) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/prose-runs/{run_id}/discard")
async def discard_prose_run(
    run_id: str,
    req: DiscardProseRunRequest,
    request: Request,
):
    actor = getattr(request.state, "actor", None)
    if actor is None:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        await prose_run_module.discard(
            owner_id=str(actor.id),
            novel_id=req.novel_id,
            chapter_id=req.chapter_id,
            run_id=run_id,
            expected_revision=req.expected_run_revision,
        )
        return {"discarded": True}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/write-chapter-by-ai/readiness")
async def inspect_prose_readiness(req: ProseRequest, request: Request):
    """Return a frozen, no-charge prose authorization preview."""
    try:
        inputs = await _prepare_prose_inputs(req)
        preflight = await _build_prose_readiness(
            req=req,
            request=request,
            chapter=inputs.chapter,
            context=inputs.context,
            prompt=inputs.prompt,
            words_per_chapter=inputs.words_per_chapter,
            gen_kwargs=inputs.gen_kwargs,
        )
        response = preflight.readiness.to_dict()
        response["provider"] = {
            "alias": str(getattr(preflight.plan, "provider_alias", "") or ""),
            "model": str(getattr(preflight.plan, "provider_model", "") or ""),
            "max_output_tokens": getattr(
                preflight.plan,
                "max_output_tokens",
                None,
            ),
        }
        return response
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ContextBudgetError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/write-chapter-by-ai")
async def write_chapter_by_ai(req: ProseRequest, request: Request):
    """流式生成正文并保存可恢复草稿，不直接覆盖正式章节正文。"""
    actor = getattr(request.state, "actor", None)
    policy = (
        req.prose_continuation_policy.to_domain()
        if req.prose_continuation_policy is not None
        else ProseContinuationPolicy()
    )
    request_id = uuid4().hex[:8]
    try:
        capability_stream = await _chapter_capability_registry().stream(
            "chapter_prose",
            ProseGenerationCommand(
                novel_id=req.novel_id,
                chapter_id=req.chapter_id,
                authority=AcceptanceAuthority.PREVIEW,
                owner_id=str(actor.id) if actor is not None else None,
                generation_params={
                    **build_gen_kwargs(req),
                    "allow_failure_retry": req.allow_failure_retry,
                },
                resume_run_id=req.resume_run_id,
                expected_run_revision=req.expected_run_revision,
                confirm_uncertain_retry=req.confirm_uncertain_retry,
                continuation_policy=policy,
                readiness_digest=req.readiness_digest,
                confirm_automatic_continuations=(
                    req.confirm_automatic_continuations
                ),
                token_budget=req.token_budget,
                request_id=request_id,
                is_disconnected=request.is_disconnected,
            ),
            call=CapabilityCall(
                source="http",
                request_id=request_id,
                actor=actor,
            ),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StaleProseReadiness as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "readiness_stale", "message": str(exc)},
        ) from exc
    except ProseReadinessBlocked as exc:
        codes = [code for code in str(exc).split(",") if code]
        raise HTTPException(
            status_code=400,
            detail={"code": codes[0], "codes": codes, "message": str(exc)},
        ) from exc
    except (InvalidIdError, ContextBudgetError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def application_event_stream() -> AsyncGenerator[str, None]:
        async for event in capability_stream.events:
            yield sse_event(event.name, event.data)

    return StreamingResponse(
        application_event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
