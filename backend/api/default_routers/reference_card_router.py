"""CRUD API for character, location, item, world-rule and lore cards."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.mutation import MutationConflictError
from backend.services.novel.reference_card_curation import (
    ReferenceCardProposalError,
    StaleReferenceCardProposal,
    reference_card_curation_service,
)
from backend.services.novel.reference_card_service import ReferenceCardService
from backend.services.novel.character_profile import CharacterProfileSchema
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.services.auth.identity_service import Actor


router = APIRouter(
    prefix="/api/reference-cards",
    tags=["reference-cards"],
    dependencies=[Depends(require_owned_path_resource)],
)


class ReferenceCardCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    description: str = ""
    details: Dict[str, str] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    sort_order: Optional[int] = None
    # 用 Optional[str] 而非 Literal["main","sub"]：非法值要落到仓储层的
    # ValueError，经 _translate_error 转成 400 + 可读消息，而不是 FastAPI 的 422。
    importance: Optional[str] = None
    character_profile: Optional[CharacterProfileSchema] = None


class ReferenceCardUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = None
    details: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None
    sort_order: Optional[int] = None
    importance: Optional[str] = None
    character_profile: Optional[CharacterProfileSchema] = None


class ReferenceCardCurationDecision(BaseModel):
    candidate_id: str = Field(min_length=1)
    action: Literal["create", "merge", "restore_merge", "skip"]
    target_card_id: Optional[str] = None
    overrides: Dict[str, Any] = Field(default_factory=dict)
    overwrite_fields: List[str] = Field(default_factory=list)


class ReferenceCardCurationApplyRequest(BaseModel):
    acceptance_token: str = Field(min_length=1)
    decisions: List[ReferenceCardCurationDecision] = Field(min_length=1, max_length=30)


class ReferenceCardCurationPrepareRequest(BaseModel):
    force_regenerate: bool = False


def _serialize_card(card: dict) -> dict:
    result = dict(card)
    for key in ("_id", "novel_id"):
        if key in result:
            result[key] = str(result[key])
    return result


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/novel/{novel_id}/curation/prepare")
async def prepare_reference_card_curation(
    novel_id: str,
    req: ReferenceCardCurationPrepareRequest,
    actor: Actor = Depends(require_owned_path_resource),
):
    """Generate or resume a persisted review proposal without writing formal cards."""
    try:
        return await reference_card_curation_service.prepare(
            novel_id,
            actor_id=actor.id,
            force_regenerate=req.force_regenerate,
        )
    except (InvalidIdError, NotFoundError) as exc:
        raise _translate_error(exc) from exc
    except ReferenceCardProposalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/novel/{novel_id}/curation/proposal")
async def inspect_reference_card_curation(novel_id: str):
    try:
        proposal = await reference_card_curation_service.inspect(novel_id)
        if proposal is None:
            raise HTTPException(
                status_code=404,
                detail="No active reference-card proposal",
            )
        return proposal
    except HTTPException:
        raise
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/novel/{novel_id}/curation/{proposal_id}/apply")
async def apply_reference_card_curation(
    novel_id: str,
    proposal_id: str,
    req: ReferenceCardCurationApplyRequest,
    actor: Actor = Depends(require_owned_path_resource),
):
    try:
        return await reference_card_curation_service.apply(
            novel_id=novel_id,
            proposal_id=proposal_id,
            acceptance_token=req.acceptance_token,
            decisions=[
                item.model_dump(exclude_none=True) for item in req.decisions
            ],
            actor_id=actor.id,
        )
    except (StaleReferenceCardProposal, MutationConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ReferenceCardProposalError, InvalidIdError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/novel/{novel_id}/{card_type}")
async def create_card(novel_id: str, card_type: str, req: ReferenceCardCreateRequest):
    try:
        card_id = await ReferenceCardService.create(
            novel_id,
            card_type,
            req.model_dump(exclude_none=True),
        )
        card = await ReferenceCardService.get(novel_id, card_type, card_id)
        return _serialize_card(card)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/novel/{novel_id}/{card_type}")
async def list_cards(novel_id: str, card_type: str):
    try:
        cards = await ReferenceCardService.list(novel_id, card_type)
        return {"data": [_serialize_card(card) for card in cards]}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/novel/{novel_id}/{card_type}/trash")
async def list_deleted_cards(novel_id: str, card_type: str):
    try:
        cards = await ReferenceCardService.list(novel_id, card_type, deleted_only=True)
        return {"data": [_serialize_card(card) for card in cards]}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/novel/{novel_id}/{card_type}/{card_id}")
async def get_card(novel_id: str, card_type: str, card_id: str):
    try:
        return _serialize_card(await ReferenceCardService.get(novel_id, card_type, card_id))
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/{card_type}/{card_id}")
async def update_card(novel_id: str, card_type: str, card_id: str, req: ReferenceCardUpdateRequest):
    try:
        await ReferenceCardService.update(
            novel_id,
            card_type,
            card_id,
            req.model_dump(exclude_unset=True),
        )
        card = await ReferenceCardService.get(novel_id, card_type, card_id)
        return _serialize_card(card)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/{card_type}/{card_id}")
async def soft_delete_card(novel_id: str, card_type: str, card_id: str):
    try:
        return {"success": await ReferenceCardService.soft_delete(novel_id, card_type, card_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/novel/{novel_id}/{card_type}/{card_id}/restore")
async def restore_card(novel_id: str, card_type: str, card_id: str):
    try:
        await ReferenceCardService.restore(novel_id, card_type, card_id)
        card = await ReferenceCardService.get(novel_id, card_type, card_id)
        return _serialize_card(card)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/{card_type}/{card_id}/hard")
async def hard_delete_card(novel_id: str, card_type: str, card_id: str):
    try:
        return {"success": await ReferenceCardService.hard_delete(novel_id, card_type, card_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc
