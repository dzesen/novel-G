"""HTTP adapters for scene Agent discovery and rewrite previews."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.api.default_routers.agent_router import get_agent_catalog
from backend.api.default_routers.auth_router import require_authenticated_request
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)
from backend.services.llm.agent_capability_contracts import (
    RewriteChapterSceneRequest,
)
from backend.services.llm.agent_capability_registry import (
    build_agent_capability_registry,
)
from backend.services.llm.agent_catalog import AgentCatalog
from backend.services.llm.agent_context import StaleAgentContext
from backend.services.llm.capability_registry import CapabilityCall
from backend.services.llm.context_builder import ContextBudgetError


router = APIRouter(
    prefix="/api/llm",
    tags=["llm-agents"],
    dependencies=[Depends(require_authenticated_request)],
)


@router.get("/scene-agents")
async def list_scene_agents(
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
):
    profiles = await catalog.list_profiles(actor, capability="scene_rewrite")
    return {"data": [profile.public_view() for profile in profiles]}


def _agent_capability_registry(*, access, catalog):
    return build_agent_capability_registry(access=access, catalog=catalog)


@router.post("/rewrite-chapter-scene")
async def rewrite_chapter_scene(
    req: RewriteChapterSceneRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
):
    try:
        execution = await _agent_capability_registry(
            access=access,
            catalog=catalog,
        ).execute(
            "scene_rewrite",
            req,
            call=CapabilityCall(source="http", actor=actor),
        )
        return execution.value.model_dump()
    except HTTPException:
        raise
    except StaleAgentContext as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError, ContextBudgetError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
