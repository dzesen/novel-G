"""HTTP adapters for preview-only Agent capabilities."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.api.default_routers.agent_router import get_agent_catalog
from backend.api.default_routers.auth_router import require_authenticated_request
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)
from backend.services.interop.card_import_proposal_service import (
    CardImportProposalError,
    StaleCardImportProposal,
)
from backend.services.llm.agent_capability_contracts import (
    AgentScopeRequest,
    CardImportDirectionReference,
    ContinuityReviewRequest,
    CreativeDirectorRequest,
    CreativeInspirationRequest,
    IllustrationPromptRequest,
    StyleConsistencyRequest,
    VolumeRetrospectiveRequest,
)
from backend.services.llm.agent_capability_registry import (
    build_agent_capability_registry,
)
from backend.services.llm.agent_catalog import AgentCatalog
from backend.services.llm.agent_context import StaleAgentContext
from backend.services.llm.agent_run import AgentRunStore, agent_run_store
from backend.services.llm.capability_registry import CapabilityCall


router = APIRouter(
    prefix="/api/llm",
    tags=["llm-agents"],
    dependencies=[Depends(require_authenticated_request)],
)
logger = logging.getLogger(__name__)


def get_agent_run_store() -> AgentRunStore:
    return agent_run_store


def _agent_capability_registry(*, access=None, catalog=None, runs=None):
    return build_agent_capability_registry(
        access=access,
        catalog=catalog,
        runs=runs,
    )


@router.post("/creative-director")
async def generate_creative_direction(
    request: CreativeDirectorRequest,
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    try:
        execution = await _agent_capability_registry(catalog=catalog).execute(
            "novel_direction",
            request,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except (
        CardImportProposalError,
        StaleCardImportProposal,
        NotFoundError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "[creative_director] failed agent_id=%s",
            request.agent_id,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent-inspiration")
async def generate_agent_inspiration(
    request: CreativeInspirationRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        execution = await _agent_capability_registry(
            access=access,
            catalog=catalog,
            runs=runs,
        ).execute(
            "creative_inspiration",
            request,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except StaleAgentContext as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent-continuity-review")
async def generate_agent_continuity_review(
    request: ContinuityReviewRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        execution = await _agent_capability_registry(
            access=access,
            catalog=catalog,
            runs=runs,
        ).execute(
            "continuity_review",
            request,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except StaleAgentContext as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent-style-consistency")
async def generate_agent_style_consistency(
    request: StyleConsistencyRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        execution = await _agent_capability_registry(
            access=access,
            catalog=catalog,
            runs=runs,
        ).execute(
            "style_consistency",
            request,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except StaleAgentContext as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent-illustration-prompt")
async def generate_agent_illustration_prompt(
    request: IllustrationPromptRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        execution = await _agent_capability_registry(
            access=access,
            catalog=catalog,
            runs=runs,
        ).execute(
            "illustration_prompt",
            request,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except StaleAgentContext as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent-volume-retrospective")
async def generate_agent_volume_retrospective(
    request: VolumeRetrospectiveRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
    runs: AgentRunStore = Depends(get_agent_run_store),
) -> dict[str, Any]:
    try:
        execution = await _agent_capability_registry(
            access=access,
            catalog=catalog,
            runs=runs,
        ).execute(
            "volume_retrospective",
            request,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except StaleAgentContext as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
