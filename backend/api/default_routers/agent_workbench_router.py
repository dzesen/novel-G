"""Authenticated Agent run history and stale-safe revision proposal endpoints."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.api.default_routers.auth_router import require_authenticated_request
from backend.api.llm_routers.agent_tool_router import get_agent_run_store
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)
from backend.services.llm.agent_run import AgentRunStore
from backend.services.novel.agent_revision import (
    AgentRevisionProposalService,
    RevisionPatch,
    RevisionProposalConflict,
    RevisionTarget,
    StaleRevisionProposal,
    agent_revision_proposal_service,
)


router = APIRouter(
    prefix="/api/agent-tools",
    tags=["agent-tools"],
    dependencies=[Depends(require_authenticated_request)],
)


def get_agent_revision_service() -> AgentRevisionProposalService:
    return agent_revision_proposal_service


class CreateRevisionProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    novel_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_kind: Literal["creative_idea", "continuity_issue"]
    source_index: int = Field(ge=0)
    target: RevisionTarget
    patch: RevisionPatch


class ProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=1000)


@router.get("/runs")
async def list_agent_runs(
    novel_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        await access.require_owned_novel(actor, novel_id)
        return {
            "data": await runs.list_owned(
                actor_id=actor.id,
                novel_id=novel_id,
                limit=limit,
            )
        }
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
async def get_agent_run(
    run_id: str,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        run = await runs.get_owned(actor_id=actor.id, run_id=run_id)
        await access.require_owned_novel(actor, str(run["novel_id"]))
        return {"run": runs.public_view(run)}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/proposals")
async def list_revision_proposals(
    novel_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    proposals: AgentRevisionProposalService = Depends(
        get_agent_revision_service
    ),
) -> dict[str, Any]:
    try:
        await access.require_owned_novel(actor, novel_id)
        return {
            "data": await proposals.list_owned(
                actor_id=actor.id,
                novel_id=novel_id,
                status=status,
                limit=limit,
            )
        }
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals", status_code=201)
async def create_revision_proposal(
    request: CreateRevisionProposalRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    proposals: AgentRevisionProposalService = Depends(
        get_agent_revision_service
    ),
) -> dict[str, Any]:
    try:
        await access.require_owned_novel(actor, request.novel_id)
        proposal = await proposals.create(
            actor_id=actor.id,
            novel_id=request.novel_id,
            run_id=request.run_id,
            source_kind=request.source_kind,
            source_index=request.source_index,
            target=request.target,
            patch=request.patch,
        )
        return {"proposal": proposal}
    except StaleRevisionProposal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RevisionProposalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/apply")
async def apply_revision_proposal(
    proposal_id: str,
    request: ProposalDecisionRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    proposals: AgentRevisionProposalService = Depends(
        get_agent_revision_service
    ),
) -> dict[str, Any]:
    try:
        proposal = await proposals.get_owned(
            actor_id=actor.id,
            proposal_id=proposal_id,
        )
        await access.require_owned_novel(actor, str(proposal["novel_id"]))
        applied = await proposals.apply(
            actor_id=actor.id,
            proposal_id=proposal_id,
            expected_version=request.expected_version,
        )
        return {"proposal": applied}
    except (StaleRevisionProposal, RevisionProposalConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{proposal_id}/reject")
async def reject_revision_proposal(
    proposal_id: str,
    request: ProposalDecisionRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    proposals: AgentRevisionProposalService = Depends(
        get_agent_revision_service
    ),
) -> dict[str, Any]:
    try:
        proposal = await proposals.get_owned(
            actor_id=actor.id,
            proposal_id=proposal_id,
        )
        await access.require_owned_novel(actor, str(proposal["novel_id"]))
        rejected = await proposals.reject(
            actor_id=actor.id,
            proposal_id=proposal_id,
            expected_version=request.expected_version,
            reason=request.reason,
        )
        return {"proposal": rejected}
    except RevisionProposalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
