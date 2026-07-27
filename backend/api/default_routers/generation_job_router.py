"""批量生成作业的 HTTP 端点（轮询式，无 SSE）。设计 §9.2。"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.services.generation.job_service import ConflictError, GenerationJobService
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
)

router = APIRouter(
    prefix="/api/generation-jobs",
    tags=["generation-jobs"],
    dependencies=[Depends(require_owned_path_resource)],
)

_ID_FIELDS = ("_id", "novel_id", "volume_id", "current_chapter_id")


def _serialize_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if job is None:
        return None
    out = dict(job)
    for f in _ID_FIELDS:
        if out.get(f) is not None:
            out[f] = str(out[f])
    for entry in out.get("progress", []):
        if entry.get("chapter_id") is not None:
            entry["chapter_id"] = str(entry["chapter_id"])
    return out


class StartJobRequest(GenerationParamsMixin):
    checkpoint_interval: int = Field(default=5, ge=1, le=1000)
    token_budget: Optional[int] = Field(default=None, ge=1)
    readiness_digest: Optional[str] = Field(default=None, min_length=1, max_length=128)
    acknowledged_warning_codes: list[str] = Field(default_factory=list, max_length=50)
    outline_deviation_policy: Literal[
        "pause_for_rewrite",
        "accept_and_continue",
    ] = "pause_for_rewrite"


class ResumeJobRequest(BaseModel):
    confirm_uncertain_retry: bool = False
    skip_uncertain: bool = False


def _handle(exc: Exception) -> HTTPException:
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
            },
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
            },
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
        return _serialize_job(await GenerationJobService.resume_job(
            job_id,
            confirm_uncertain_retry=body.confirm_uncertain_retry,
            skip_uncertain=body.skip_uncertain,
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
    """启动时把上次进程遗留的 running 作业置 interrupted（设计 §4.3）。返回置换数量。"""
    running = await generation_job_repo.list_running_jobs()
    for job in running:
        job_id = str(job["_id"])
        uncertain = await generation_job_repo.mark_claimed_attempts_uncertain(
            job_id, "backend process interrupted before usage was recorded"
        )
        await generation_job_repo.update_job_fields(job_id, {
            "status": "interrupted",
            "pause_reason": "uncertain_attempt" if uncertain else "process_restart",
            "current_chapter_id": None,
            "active_slot": None,
        })
    return len(running)
