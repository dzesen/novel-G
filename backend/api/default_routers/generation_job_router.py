"""批量生成作业的 HTTP 端点（轮询式，无 SSE）。设计 §9.2。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.utils import get_utc_now
from backend.services.generation.failure_diagnostics import infer_job_diagnostics
from backend.services.generation.job_relations import related_prose_run_ids
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

_ID_FIELDS = ("_id", "novel_id", "volume_id", "current_chapter_id")
_OUTLINE_ADHERENCE_VERDICTS = frozenset(("pass", "warn", "fail"))
_AUTO_CREATION_OUTCOMES = frozenset((
    "manual_review_required",
    "auto_created",
    "not_applicable",
    "repair_exhausted",
))


def _safe_error_text(value: Any, *, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _safe_error_text_list(
    value: Any,
    *,
    item_limit: int = 160,
    count_limit: int = 100,
) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:count_limit]:
        text = _safe_error_text(item, limit=item_limit)
        if text is not None:
            result.append(text)
    return result


def _serialize_auto_creation(value: Any) -> Dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    outcome = _safe_error_text(value.get("outcome"), limit=40)
    if outcome not in _AUTO_CREATION_OUTCOMES:
        return None
    created_count = value.get("created_count")
    if isinstance(created_count, bool) or not isinstance(created_count, int):
        created_count = 0
    result: Dict[str, Any] = {
        "outcome": outcome,
        "created_count": max(0, created_count),
        "deny_reasons": _safe_error_text_list(
            value.get("deny_reasons"),
            item_limit=80,
            count_limit=50,
        ),
    }
    pause_reason = _safe_error_text(value.get("pause_reason"), limit=80)
    if pause_reason is not None:
        result["pause_reason"] = pause_reason
    denials: list[Dict[str, str]] = []
    raw_denials = value.get("denials")
    if isinstance(raw_denials, list):
        for raw_denial in raw_denials[:100]:
            if not isinstance(raw_denial, Mapping):
                continue
            reason = _safe_error_text(raw_denial.get("reason"), limit=80)
            if reason is None:
                continue
            denial = {"reason": reason}
            candidate_id = _safe_error_text(
                raw_denial.get("candidate_id"),
                limit=100,
            )
            if candidate_id is not None:
                denial["candidate_id"] = candidate_id
            denials.append(denial)
    result["denials"] = denials
    return result


def _serialize_job_error(value: Any) -> Dict[str, Any] | None:
    """Project recovery metadata without returning raw exception/provider text."""
    if not isinstance(value, Mapping):
        return None
    result: Dict[str, Any] = {}
    for field, limit in (
        ("step", 80),
        ("chapter_id", 100),
        ("audit_digest", 128),
    ):
        text = _safe_error_text(value.get(field), limit=limit)
        if text is not None:
            result[field] = text
    for field, item_limit, count_limit in (
        ("candidate_ids", 100, 100),
        ("candidate_names", 160, 100),
        ("reason_codes", 100, 50),
        ("blocking_issue_codes", 100, 100),
    ):
        items = _safe_error_text_list(
            value.get(field),
            item_limit=item_limit,
            count_limit=count_limit,
        )
        if items:
            result[field] = items
    auto_creation = _serialize_auto_creation(value.get("auto_creation"))
    if auto_creation is not None:
        result["auto_creation"] = auto_creation
    return result or None


def _serialize_outline_adherence(value: Any) -> Optional[Dict[str, Any]]:
    """Return only a usable historical adherence review for API consumers."""
    if not isinstance(value, dict):
        return None
    verdict = value.get("verdict")
    if not isinstance(verdict, str) or verdict not in _OUTLINE_ADHERENCE_VERDICTS:
        return None
    review = dict(value)
    if not isinstance(review.get("issues"), list):
        review["issues"] = []
    return review


def _serialize_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if job is None:
        return None
    out = dict(job)
    out.pop("batch_authorization_contract", None)
    raw_generation_params = out.get("generation_params")
    if isinstance(raw_generation_params, Mapping):
        safe_generation_params = dict(raw_generation_params)
        safe_generation_params.pop("system_prompt", None)
        out["generation_params"] = safe_generation_params
    else:
        out["generation_params"] = {}
    for f in _ID_FIELDS:
        if out.get(f) is not None:
            out[f] = str(out[f])
    progress: list[Dict[str, Any]] = []
    for raw_entry in out.get("progress", []):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        if entry.get("chapter_id") is not None:
            entry["chapter_id"] = str(entry["chapter_id"])
        review = _serialize_outline_adherence(entry.get("outline_adherence"))
        if review is None:
            entry.pop("outline_adherence", None)
        else:
            entry["outline_adherence"] = review
        progress.append(entry)
    out["progress"] = progress
    out["diagnostics"] = infer_job_diagnostics(out)
    out["related_prose_run_ids"] = list(related_prose_run_ids(out))
    out["error"] = _serialize_job_error(out.get("error"))
    return out


class ProtectedBatchGenerationParamsMixin(GenerationParamsMixin):
    """Bounded knobs only; protected chapter workflows reject prompt overrides."""

    system_prompt: None = Field(default=None)
    allow_failure_retry: bool = Field(
        default=False,
        description="是否显式允许沿用 Provider 配置进行传输层失败自动重试",
    )


class StartJobRequest(ProtectedBatchGenerationParamsMixin):
    checkpoint_interval: int = Field(default=5, ge=1, le=1000)
    token_budget: int = Field(ge=1)
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
            reference_card_auto_creation_policy=(
                req.reference_card_auto_creation_policy
            ),
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return _serialize_job(job)


@router.get("/volume/{volume_id}/readiness")
async def inspect_volume_readiness(volume_id: str):
    try:
        return await GenerationJobService.inspect_volume_readiness(volume_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/book/{novel_id}/readiness")
async def inspect_book_readiness(novel_id: str):
    try:
        return await GenerationJobService.inspect_book_readiness(novel_id)
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


@router.get("/{job_id}")
async def get_job(job_id: str):
    try:
        return _serialize_job(await GenerationJobService.get_job(job_id))
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/novel/{novel_id}")
async def list_jobs(novel_id: str):
    try:
        return [_serialize_job(j) for j in await GenerationJobService.list_jobs(novel_id)]
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
