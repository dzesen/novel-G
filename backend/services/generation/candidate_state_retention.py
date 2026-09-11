"""Retention proof for non-formal state results owned by a candidate Job.

Retention never grants acceptance authority. The finalizer must independently
verify the Job authorization and the exact persisted state checkpoint.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import to_object_id
from backend.services.generation.candidate_repair_contracts import (
    ProseCandidateCheckpointV1, StateCandidateCheckpointV1, StateCandidateCheckpointV3,
    parse_candidate_pipeline_checkpoint,
)
from backend.services.novel.state_completion import chapter_content_digest


class CandidateStateRetentionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["candidate_state_retention.v1"] = "candidate_state_retention.v1"
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    owner_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    novel_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_revision: int = Field(ge=1)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_narrative_revision: int = Field(ge=0)
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=240)


async def validate_candidate_state_retention(
    binding: CandidateStateRetentionBinding,
    *,
    proposal: Mapping[str, Any] | None = None,
    dispatch: bool = False,
    require_state_checkpoint: bool = True,
) -> None:
    """Prove ownership, frozen source and (for recovery) the state checkpoint."""
    db = get_database()
    job = await db[collections.GENERATION_JOBS].find_one({
        "_id": to_object_id(binding.job_id), "is_deleted": False,
    })
    novel = await db[collections.NOVELS].find_one({
        "_id": to_object_id(binding.novel_id), "owner_id": to_object_id(binding.owner_id),
        "is_deleted": False,
    })
    run = await db[collections.PROSE_RUNS].find_one({
        "_id": to_object_id(binding.source_run_id), "is_deleted": False,
    })
    if (
        job is None or novel is None or run is None
        or str(job.get("novel_id") or "") != binding.novel_id
        or job.get("scope") not in {"book", "volume"}
        or job.get("status") not in ({"running"} if dispatch else {"running", "failed", "paused", "interrupted"})
        or job.get("current_chapter_id") != binding.chapter_id
        or job.get("expected_narrative_revision") != binding.expected_narrative_revision
        or novel.get("narrative_revision", 0) != binding.expected_narrative_revision
        or (job.get("readiness") or {}).get("digest") != binding.readiness_digest
        or str((job.get("readiness") or {}).get("resources", {}).get("owner_id") or "") != binding.owner_id
        or str(run.get("owner_id") or "") != binding.owner_id
        or str(run.get("novel_id") or "") != binding.novel_id
        or str(run.get("chapter_id") or "") != binding.chapter_id
        or run.get("revision") != binding.source_run_revision
        or run.get("status") != "complete"
        or chapter_content_digest(str(run.get("assembled_text") or "")) != binding.source_content_digest
    ):
        raise ValueError("Candidate state retention source or Job authorization changed")
    checkpoints = [
        parse_candidate_pipeline_checkpoint(c)
        for c in job.get("candidate_pipeline_checkpoints", ())
        if isinstance(c, Mapping) and c.get("chapter_id") == binding.chapter_id
    ]
    prose = [c for c in checkpoints if isinstance(c, ProseCandidateCheckpointV1)]
    if not prose or (
        prose[-1].source.source_run_id != binding.source_run_id
        or prose[-1].source.source_run_revision != binding.source_run_revision
        or prose[-1].source.source_content_digest != binding.source_content_digest
    ):
        raise ValueError("Candidate state retention has no matching prose checkpoint")
    if prose[-1].origin == "initial" and str(run.get("generation_job_id") or "") != binding.job_id:
        raise ValueError("Candidate state retention initial run belongs to another Job")
    if proposal is not None:
        if (
            str(proposal.get("novel_id") or "") != binding.novel_id
            or str(proposal.get("chapter_id") or "") != binding.chapter_id
            or str(proposal.get("source_prose_run_id") or "") != binding.source_run_id
            or proposal.get("source_prose_run_revision") != binding.source_run_revision
            or proposal.get("source_content_digest") != binding.source_content_digest
            or proposal.get("narrative_revision") != binding.expected_narrative_revision
            or (proposal.get("generation_audit") or {}).get("request_id") != binding.request_id
        ):
            raise ValueError("Candidate state retention proposal identity changed")
        if not require_state_checkpoint:
            return
        states = [c for c in checkpoints if isinstance(c, (StateCandidateCheckpointV1, StateCandidateCheckpointV3))]
        if not states or (
            states[-1].proposal_id != str(proposal.get("_id") or "")
            or states[-1].request_id != binding.request_id
            or states[-1].source != prose[-1].source
        ):
            raise ValueError("Candidate state retention has no matching state checkpoint")


async def capture_candidate_state_retention(
    job_id: str, snapshot: Any, request_id: str,
) -> CandidateStateRetentionBinding:
    job = await get_database()[collections.GENERATION_JOBS].find_one({
        "_id": to_object_id(job_id), "is_deleted": False,
    })
    if job is None:
        raise ValueError("Candidate state retention Job is missing")
    binding = CandidateStateRetentionBinding(
        job_id=job_id, owner_id=str((job.get("readiness") or {}).get("resources", {}).get("owner_id") or ""),
        novel_id=str(snapshot.novel_id), chapter_id=str(snapshot.chapter_id),
        source_run_id=str(snapshot.source_prose_run_id or ""),
        source_run_revision=snapshot.source_prose_run_revision,
        source_content_digest=str(snapshot.source_content_digest or ""),
        expected_narrative_revision=snapshot.narrative_revision,
        readiness_digest=str((job.get("readiness") or {}).get("digest") or ""),
        request_id=request_id,
    )
    await validate_candidate_state_retention(binding, dispatch=True)
    return binding
