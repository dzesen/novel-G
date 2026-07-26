"""Authenticated management endpoints for built-in and custom Agents."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.api.default_routers.auth_router import require_authenticated_request
from backend.db.errors import NotFoundError
from backend.db.repositories.agent_definition_repository import (
    AgentDefinitionVersionConflict,
)
from backend.services.auth.identity_service import Actor
from backend.services.llm.agent_catalog import AgentCatalog, agent_catalog


router = APIRouter(
    prefix="/api/agents",
    tags=["agents"],
    dependencies=[Depends(require_authenticated_request)],
)


class AgentGenerationDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentDefinitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=500)
    capability: str = Field(min_length=1, max_length=80)
    instruction: str = Field(min_length=20, max_length=4000)
    provider_alias: str | None = Field(default=None, max_length=120)
    generation_params: AgentGenerationDefaults = Field(
        default_factory=AgentGenerationDefaults
    )
    visibility: Literal["private", "shared"] = "private"
    enabled: bool = True


class AgentDefinitionUpdate(AgentDefinitionRequest):
    expected_version: int = Field(ge=1)


class CloneAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=2, max_length=64)


def get_agent_catalog() -> AgentCatalog:
    return agent_catalog


@router.get("/capabilities")
async def list_agent_capabilities(
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    return {
        "data": [item.public_view() for item in catalog.list_capabilities()]
    }


@router.get("/providers")
async def list_agent_provider_options(
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    """Expose non-secret enabled Provider labels for Agent defaults."""
    return {"data": catalog.list_provider_options()}


@router.get("")
async def list_agents(
    capability: str | None = Query(default=None),
    include_disabled: bool = Query(default=False),
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    try:
        profiles = await catalog.list_profiles(
            actor,
            capability=capability,
            include_disabled=include_disabled,
        )
        return {"data": [profile.public_view() for profile in profiles]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("", status_code=201)
async def create_agent(
    request: AgentDefinitionRequest,
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    try:
        profile = await catalog.create_profile(
            actor,
            **request.model_dump(),
        )
        return {"agent": profile.public_view()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    request: AgentDefinitionUpdate,
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    try:
        values = request.model_dump()
        expected_version = values.pop("expected_version")
        profile = await catalog.update_profile(
            actor,
            agent_id=agent_id,
            expected_version=expected_version,
            **values,
        )
        return {"agent": profile.public_view()}
    except AgentDefinitionVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{agent_id}/clone", status_code=201)
async def clone_agent(
    agent_id: str,
    request: CloneAgentRequest,
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> dict[str, Any]:
    try:
        profile = await catalog.clone_profile(
            actor,
            source_agent_id=agent_id,
            label=request.label,
        )
        return {"agent": profile.public_view()}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
) -> Response:
    try:
        await catalog.delete_profile(actor, agent_id=agent_id)
        return Response(status_code=204)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
