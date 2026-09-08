"""批量生成作业的 HTTP 端点（轮询式，无 SSE）。设计 §9.2。"""
from __future__ import annotations

from backend.services.generation.chapter_review_policy import ChapterReviewSelection

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.utils import get_utc_now
from backend.services.generation.job_public_views import (
    PublicGenerationJob,
    PublicJobPage,
    PublicJobSummary,
    project_job as _serialize_job,
)
from backend.services.generation.job_read_service import GenerationJobReadService
from backend.services.generation.book_structure_initialization import (
    BookStructureBudgetBoundary,
    BookStructureInitializationFailed,
    BookStructureInitializationStale,
)
from backend.services.generation.job_service import (
    ConflictError,
    GenerationJobService,
    ResumeReadinessRequired,
)
from backend.services.generation.readiness import StaleReadiness
from backend.services.generation.reference_card_auto_creation import (
    ReferenceCardAutoCreationPolicy,
)
from backend.services.novel.book_completion import (
    BookCompletionReport,
    book_completion_audit,
)
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
)
from backend.api.prose_continuation_contracts import (
    ProseContinuationPolicyRequest,
)

router = APIRouter(
    prefix="/api/generation-jobs",
    tags=["generation-jobs"],
    dependencies=[Depends(require_owned_path_resource)],
)



class ProtectedBatchGenerationParamsMixin(GenerationParamsMixin):
    """Bounded knobs only; protected chapter workflows reject prompt overrides."""

    system_prompt: None = Field(default=None)
    allow_failure_retry: bool = Field(
        default=False,
        description="是否显式允许沿用 Provider 配置进行传输层失败自动重试",
    )


class StartJobRequest(ProtectedBatchGenerationParamsMixin):
    checkpoint_interval: Optional[int] = Field(default=5, ge=1, le=1000)
    token_budget: Optional[int] = Field(default=None, ge=1)
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledged_warning_codes: list[str] = Field(default_factory=list, max_length=50)
    outline_deviation_policy: Literal[
        "pause_for_rewrite",
        "accept_and_continue",
    ] = "pause_for_rewrite"
    prose_continuation_policy: ProseContinuationPolicyRequest = Field(
        default_factory=ProseContinuationPolicyRequest
    )
    reference_card_auto_creation_policy: ReferenceCardAutoCreationPolicy = Field(
        default_factory=ReferenceCardAutoCreationPolicy
    )

    chapter_review_selection: ChapterReviewSelection = Field(default_factory=ChapterReviewSelection)


class BatchReadinessRequest(ProtectedBatchGenerationParamsMixin):
    token_budget: Optional[int] = Field(default=None, ge=1)
    outline_deviation_policy: Literal[
        "pause_for_rewrite",
        "accept_and_continue",
    ] = "pause_for_rewrite"
    prose_continuation_policy: ProseContinuationPolicyRequest = Field(
        default_factory=ProseContinuationPolicyRequest
    )
    reference_card_auto_creation_policy: ReferenceCardAutoCreationPolicy = Field(
        default_factory=ReferenceCardAutoCreationPolicy
    )

    chapter_review_selection: ChapterReviewSelection = Field(default_factory=ChapterReviewSelection)


class ResumeReadinessRequest(BaseModel):
    """A proposed authorization for one existing, paused generation job.

    This deliberately excludes general generation-parameter overrides.  Resume
    readiness may only vary the two continuation-authority controls covered by
    the persisted job snapshot; accepting a preview must never silently change
    any other generation behavior.
    """

    token_budget: Optional[int] = Field(default=None, ge=1)
    prose_continuation_policy: ProseContinuationPolicyRequest | None = None


class ResumeJobRequest(BaseModel):
    confirm_uncertain_retry: bool = False
    skip_uncertain: bool = False
    prose_continuation_policy: ProseContinuationPolicyRequest | None = None
    token_budget: Optional[int] = Field(default=None, ge=1)
    readiness_digest: Optional[str] = Field(default=None, min_length=1, max_length=128)
    acknowledged_warning_codes: list[str] | None = Field(default=None, max_length=50)


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, BookStructureInitializationStale):
        return HTTPException(
            status_code=409,
            detail={
                "code": "book_structure_initialization_stale",
                "message": str(exc),
            },
        )
    if isinstance(exc, BookStructureBudgetBoundary):
        return HTTPException(
            status_code=409,
            detail={
                "code": "book_structure_budget_boundary",
                "message": str(exc),
            },
        )
    if isinstance(exc, BookStructureInitializationFailed):
        return HTTPException(
            status_code=502,
            detail={
                "code": "book_structure_initialization_failed",
                "message": str(exc),
            },
        )
    if isinstance(exc, ResumeReadinessRequired):
        return HTTPException(
            status_code=409,
            detail={"code": "resume_readiness_required", "message": str(exc)},
        )
    if isinstance(exc, StaleReadiness):
        return HTTPException(
            status_code=409,
            detail={"code": "readiness_stale", "message": str(exc)},
        )
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("/volume/{volume_id}")
async def start_volume_job(volume_id: str, req: StartJobRequest):
    try:
        job = await GenerationJobService.start_volume_job(
            volume_id=volume_id,
            checkpoint_interval=req.checkpoint_interval,
            token_budget=req.token_budget,
            readiness_digest=req.readiness_digest,
            acknowledged_warning_codes=req.acknowledged_warning_codes,
            outline_deviation_policy=req.outline_deviation_policy,
            generation_params={
                **build_gen_kwargs(req),
                "allow_failure_retry": req.allow_failure_retry,
                "prose_continuation_policy": req.prose_continuation_policy.to_domain().to_dict(),
            },
            prose_continuation_policy=req.prose_continuation_policy.to_domain(),
            chapter_review_selection=req.chapter_review_selection,
            reference_card_auto_creation_policy=(
                req.reference_card_auto_creation_policy
            ),
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return _serialize_job(job)


@router.post("/book/{novel_id}")
async def start_book_job(novel_id: str, req: StartJobRequest):
    try:
        job = await GenerationJobService.start_book_job(
            novel_id=novel_id,
            checkpoint_interval=req.checkpoint_interval,
            token_budget=req.token_budget,
            readiness_digest=req.readiness_digest,
            acknowledged_warning_codes=req.acknowledged_warning_codes,
            outline_deviation_policy=req.outline_deviation_policy,
            generation_params={
                **build_gen_kwargs(req),
                "allow_failure_retry": req.allow_failure_retry,
                "prose_continuation_policy": req.prose_continuation_policy.to_domain().to_dict(),
            },
            prose_continuation_policy=req.prose_continuation_policy.to_domain(),
            chapter_review_selection=req.chapter_review_selection,
            reference_card_auto_creation_policy=(
                req.reference_card_auto_creation_policy
            ),
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return _serialize_job(job)


@router.post("/book/{novel_id}/initialize-structure")
async def initialize_book_structure(novel_id: str, req: StartJobRequest):
    """Generate the initial volume/chapter stubs under signed readiness."""

    try:
        return await GenerationJobService.initialize_book_structure(
            novel_id=novel_id,
            token_budget=req.token_budget,
            readiness_digest=req.readiness_digest,
            acknowledged_warning_codes=req.acknowledged_warning_codes,
            outline_deviation_policy=req.outline_deviation_policy,
            generation_params={
                **build_gen_kwargs(req),
                "allow_failure_retry": req.allow_failure_retry,
                "prose_continuation_policy": (
                    req.prose_continuation_policy.to_domain().to_dict()
                ),
            },
            prose_continuation_policy=(
                req.prose_continuation_policy.to_domain()
            ),
            chapter_review_selection=req.chapter_review_selection,
            reference_card_auto_creation_policy=(
                req.reference_card_auto_creation_policy
            ),
        )
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/volume/{volume_id}/readiness")
async def inspect_volume_readiness(volume_id: str):
    try:
        return await GenerationJobService.inspect_volume_readiness(volume_id, chapter_review_selection=ChapterReviewSelection())
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/book/{novel_id}/readiness")
async def inspect_book_readiness(novel_id: str):
    try:
        return await GenerationJobService.inspect_book_readiness(novel_id, chapter_review_selection=ChapterReviewSelection())
    except Exception as exc:
        raise _handle(exc) from exc


@router.get(
    "/book/{novel_id}/completion-audit",
    response_model=BookCompletionReport,
)
async def inspect_book_completion(
    novel_id: str,
    job_id: str | None = Query(default=None),
) -> BookCompletionReport:
    try:
        return await book_completion_audit.inspect(novel_id, job_id=job_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/volume/{volume_id}/readiness")
async def inspect_volume_readiness_with_policy(
    volume_id: str,
    req: BatchReadinessRequest,
):
    try:
        return await GenerationJobService.inspect_volume_readiness(
            volume_id,
            outline_deviation_policy=req.outline_deviation_policy,
            prose_continuation_policy=(
                req.prose_continuation_policy.to_domain()
            ),
            token_budget=req.token_budget,
            chapter_review_selection=req.chapter_review_selection,
            reference_card_auto_creation_policy=(
                req.reference_card_auto_creation_policy
            ),
            generation_params={
                **build_gen_kwargs(req),
                "allow_failure_retry": req.allow_failure_retry,
                "prose_continuation_policy": (
                    req.prose_continuation_policy.to_domain().to_dict()
                ),
            },
        )
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/book/{novel_id}/readiness")
async def inspect_book_readiness_with_policy(
    novel_id: str,
    req: BatchReadinessRequest,
):
    try:
        return await GenerationJobService.inspect_book_readiness(
            novel_id,
            outline_deviation_policy=req.outline_deviation_policy,
            prose_continuation_policy=(
                req.prose_continuation_policy.to_domain()
            ),
            token_budget=req.token_budget,
            chapter_review_selection=req.chapter_review_selection,
            reference_card_auto_creation_policy=(
                req.reference_card_auto_creation_policy
            ),
            generation_params={
                **build_gen_kwargs(req),
                "allow_failure_retry": req.allow_failure_retry,
                "prose_continuation_policy": (
                    req.prose_continuation_policy.to_domain().to_dict()
                ),
            },
        )
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/novel/{novel_id}/diagnostics")
async def summarize_diagnostics(
    novel_id: str,
    limit: int = Query(default=30, ge=1, le=200),
):
    try:
        return await GenerationJobService.summarize_diagnostics(novel_id, limit=limit)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/{job_id}/readiness")
async def inspect_resume_readiness(job_id: str, req: ResumeReadinessRequest):
    """Return the next revision's read-only authorization preview for a job."""
    try:
        return await GenerationJobService.inspect_resume_readiness(
            job_id,
            prose_continuation_policy=(
                req.prose_continuation_policy.to_domain().to_dict()
                if req.prose_continuation_policy is not None
                else None
            ),
            token_budget=req.token_budget,
            token_budget_provided="token_budget" in req.model_fields_set,
        )
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/novel/{novel_id}/current", response_model=PublicJobSummary | None)
async def get_current_root_job(novel_id: str):
    try:
        return await GenerationJobReadService.current(novel_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/novel/{novel_id}/history", response_model=PublicJobPage)
async def get_job_history(
    novel_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=300),
):
    try:
        return await GenerationJobReadService.history(novel_id, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/{job_id}/summary", response_model=PublicJobSummary)
async def get_job_summary(job_id: str):
    try:
        return await GenerationJobReadService.summary(job_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/{job_id}/children", response_model=PublicJobPage)
async def get_child_stages(
    job_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=300),
):
    try:
        return await GenerationJobReadService.children(job_id, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/{job_id}", response_model=PublicGenerationJob, response_model_exclude_unset=True)
async def get_job(job_id: str):
    try:
        return _serialize_job(await GenerationJobService.get_job(job_id))
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/novel/{novel_id}", response_model=list[PublicGenerationJob], response_model_exclude_unset=True)
async def list_jobs(novel_id: str, limit: int = Query(default=20, ge=1, le=100)):
    try:
        return [_serialize_job(j) for j in await GenerationJobService.list_jobs(novel_id, limit=limit)]
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/{job_id}/pause")
async def pause_job(job_id: str):
    try:
        return _serialize_job(await GenerationJobService.pause_job(job_id))
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/{job_id}/resume")
async def resume_job(job_id: str, req: ResumeJobRequest | None = None):
    try:
        body = req or ResumeJobRequest()
        if body.confirm_uncertain_retry and body.skip_uncertain:
            raise ValueError("confirm_uncertain_retry and skip_uncertain are mutually exclusive")
        reauthorization_payload_supplied = (
            body.prose_continuation_policy is not None
            or "token_budget" in body.model_fields_set
            or body.readiness_digest is not None
            or body.acknowledged_warning_codes is not None
        )
        if (
            (body.confirm_uncertain_retry or body.skip_uncertain)
            and reauthorization_payload_supplied
        ):
            raise ValueError(
                "uncertain-attempt recovery and re-authorized resume are mutually exclusive"
            )
        return _serialize_job(await GenerationJobService.resume_job(
            job_id,
            confirm_uncertain_retry=body.confirm_uncertain_retry,
            skip_uncertain=body.skip_uncertain,
            prose_continuation_policy=(
                body.prose_continuation_policy.to_domain()
                if body.prose_continuation_policy is not None
                else None
            ),
            token_budget=body.token_budget,
            token_budget_provided="token_budget" in body.model_fields_set,
            readiness_digest=body.readiness_digest,
            acknowledged_warning_codes=body.acknowledged_warning_codes,
        ))
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/{job_id}/abort")
async def abort_job(job_id: str):
    try:
        return _serialize_job(await GenerationJobService.abort_job(job_id))
    except Exception as exc:
        raise _handle(exc) from exc


async def mark_running_jobs_interrupted() -> int:
    """Fence only missing/expired workers; live workers may belong to another process."""
    running = await generation_job_repo.list_running_jobs()
    interrupted_count = 0
    for job in running:
        job_id = str(job["_id"])
        interrupted = await generation_job_repo.interrupt_stale_execution(
            job_id,
            now=get_utc_now(),
        )
        if not interrupted:
            continue
        interrupted_count += 1
    return interrupted_count
