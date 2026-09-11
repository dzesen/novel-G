from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Dict, List, Literal, Optional
from backend.novel_scale import ChapterCount, WordsPerChapter

from backend.db.repositories.novel_repository import novel_repo
from backend.services.novel.novel_service import NovelService
from backend.db.errors import NotFoundError, InvalidIdError
from backend.db.utils import to_object_id
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)
from backend.api.default_routers.auth_router import require_actor, require_csrf_actor
from backend.api.default_routers.reference_card_router import (
    ReferenceCardCurationDecision,
)
from backend.db.mutation import MutationConflictError
from backend.services.interop.card_import_proposal_service import (
    DIRECTION_CONTEXT_MAX_PROPOSALS,
    MAX_CARD_IMPORT_CANDIDATES,
    CardImportProposalError,
    StaleCardImportProposal,
)
from backend.services.llm.agent_orchestrator import CreativeDirectionSelection
from backend.services.generation.author_brief import AuthorInput
from backend.services.novel.card_driven_creation_service import (
    CardDrivenCreationConflict,
    card_driven_creation_service,
)
from backend.services.novel.style_controls import (
    StyleControlsSchema,
    normalize_style_controls,
)
from backend.services.novel.world_baseline import (
    WORLD_BASELINE_DECISION_KEYS,
    WorldBaselineError,
    WorldBaselineService,
)

router = APIRouter(prefix="/api/novels", tags=["novels"])


class CardImportCreationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64)
    digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decisions: List[ReferenceCardCurationDecision] = Field(
        min_length=1,
        max_length=MAX_CARD_IMPORT_CANDIDATES,
    )


class CreateNovelRequest(BaseModel):
    author_input: AuthorInput | None = None
    title: str
    subtitle: Optional[str] = None
    genre: Optional[str] = "unclassified"
    tags: Optional[List[str]] = []
    introduction: Optional[str] = None
    summary: Optional[str] = None
    core_seed: Optional[str] = None
    worldview: Optional[str] = None
    writing_style: Optional[str] = None
    narrative_pov: Optional[str] = None
    era_background: Optional[str] = None
    cover_image: Optional[str] = None
    plot: Optional[str] = None
    tone: Optional[str] = None
    target_audience: Optional[str] = None
    core_idea: Optional[str] = None
    number_of_chapters: ChapterCount | None = None
    words_per_chapter: WordsPerChapter | None = None
    style_controls: StyleControlsSchema | None = None
    creation_mode: Literal["manual", "ai"] = "manual"
    creative_direction: CreativeDirectionSelection | None = None
    card_creation_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    card_imports: List[CardImportCreationSelection] = Field(
        default_factory=list,
        max_length=DIRECTION_CONTEXT_MAX_PROPOSALS,
    )

    @model_validator(mode="after")
    def validate_creation_provenance(self):
        if self.creative_direction is not None and self.creation_mode != "ai":
            raise ValueError(
                "creative_direction requires creation_mode='ai'"
            )
        if self.card_imports:
            if self.creation_mode != "ai" or self.creative_direction is None:
                raise ValueError(
                    "card_imports require an author-confirmed AI creative direction"
                )
            if not self.card_creation_id:
                raise ValueError(
                    "card_creation_id is required with card_imports"
                )
            if not self.creative_direction.card_context_digest:
                raise ValueError(
                    "card_imports require a direction bound to the reviewed card context"
                )
        elif self.card_creation_id is not None:
            raise ValueError(
                "card_creation_id cannot be used without card_imports"
            )
        return self


class UpdateNovelRequest(BaseModel):
    author_input: AuthorInput | None = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    genre: Optional[str] = None
    tags: Optional[List[str]] = None
    introduction: Optional[str] = None
    summary: Optional[str] = None
    core_seed: Optional[str] = None
    worldview: Optional[str] = None
    writing_style: Optional[str] = None
    narrative_pov: Optional[str] = None
    era_background: Optional[str] = None
    cover_image: Optional[str] = None
    plot: Optional[str] = None
    tone: Optional[str] = None
    target_audience: Optional[str] = None
    core_idea: Optional[str] = None
    number_of_chapters: ChapterCount | None = None
    words_per_chapter: WordsPerChapter | None = None
    style_controls: StyleControlsSchema | None = None


class StatusUpdate(BaseModel):
    status: str


class ConfirmWorldBaselineRequest(BaseModel):
    expected_review_digest: Optional[str] = None
    decisions: Dict[
        Literal[
            "character",
            "location",
            "item",
            "rule",
            "lore",
            "factions",
            "relationships",
        ],
        Literal["reviewed", "not_applicable"],
    ]

    @model_validator(mode="after")
    def validate_complete_decisions(self):
        if set(self.decisions) != set(WORLD_BASELINE_DECISION_KEYS):
            raise ValueError("every world-baseline domain requires a decision")
        return self


@router.post("/create")
async def create_novel(
    req: CreateNovelRequest,
    actor: Actor = Depends(require_csrf_actor),
):
    """创建一个新的小说项目。"""
    data = req.model_dump(exclude_unset=True)
    creation_mode = data.pop("creation_mode", req.creation_mode)
    creative_direction = data.pop("creative_direction", None)
    card_creation_id = data.pop("card_creation_id", None)
    card_imports = data.pop("card_imports", [])
    if "style_controls" in data:
        data["style_controls"] = normalize_style_controls(data["style_controls"])
    owner_id = to_object_id(actor.id)
    data.update(
        {
            "owner_id": owner_id,
            "created_by": owner_id,
            "creation_source": creation_mode,
        }
    )
    if creative_direction is not None:
        data["creation_provenance"] = {
            "creative_director": creative_direction,
        }
    try:
        if card_imports:
            return await card_driven_creation_service.create_or_resume(
                data,
                owner_id=actor.id,
                creation_id=str(card_creation_id),
                card_imports=card_imports,
            )
        novel_id = await novel_repo.create_novel(data)
        return {"id": novel_id, "message": "Novel created"}
    except (
        CardDrivenCreationConflict,
        StaleCardImportProposal,
        MutationConflictError,
    ) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except CardImportProposalError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@router.get("/list")
async def get_all_novels(actor: Actor = Depends(require_actor)):
    """获取所有小说的列表，仅包含基础信息。"""
    novels = await novel_repo.get_all_novels(actor.id)
    for novel in novels:
        if "_id" in novel:
            novel["_id"] = str(novel["_id"])
        if novel.get("cover_asset_id") is not None:
            novel["cover_asset_id"] = str(novel["cover_asset_id"])
        novel["stats"] = {
            "chapter_count": novel.get("current_chapter_count", 0),
            "total_word_count": novel.get("current_word_count", 0)
        }
    return {"data": novels}

@router.get("/deleted/list")
async def get_deleted_novels(actor: Actor = Depends(require_actor)):
    """获取所有已软删除的小说列表（回收站）。"""
    novels = await novel_repo.get_deleted_novels(actor.id)
    for novel in novels:
        if "_id" in novel:
            novel["_id"] = str(novel["_id"])
        if novel.get("cover_asset_id") is not None:
            novel["cover_asset_id"] = str(novel["cover_asset_id"])
        novel["stats"] = {
            "chapter_count": novel.get("current_chapter_count", 0),
            "total_word_count": novel.get("current_word_count", 0)
        }
    return {"data": novels}


@router.get("/{novel_id}/world-baseline")
async def inspect_world_baseline(
    novel_id: str,
    actor: Actor = Depends(require_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    try:
        await access.require_owned_novel(actor, novel_id)
        return await WorldBaselineService.inspect(novel_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, WorldBaselineError) as exc:
        detail = (
            {"code": exc.code, "message": str(exc)}
            if isinstance(exc, WorldBaselineError)
            else str(exc)
        )
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/{novel_id}/world-baseline/confirm")
async def confirm_world_baseline(
    novel_id: str,
    req: ConfirmWorldBaselineRequest,
    actor: Actor = Depends(require_csrf_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    try:
        await access.require_owned_novel(actor, novel_id)
        return await WorldBaselineService.confirm(
            novel_id,
            decisions=req.decisions,
            confirmed_by=actor.id,
            expected_review_digest=req.expected_review_digest,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorldBaselineError as exc:
        status_code = (
            400
            if exc.code == "world_baseline_decisions_incomplete"
            else 409
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

@router.get("/{novel_id}")
async def get_novel(
    novel_id: str,
    actor: Actor = Depends(require_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """根据ID获取指定小说的详细信息。"""
    try:
        novel = await access.require_owned_novel(actor, novel_id)
        novel["_id"] = str(novel["_id"])
        novel["owner_id"] = str(novel["owner_id"])
        novel["created_by"] = str(novel["created_by"])
        if novel.get("cover_asset_id") is not None:
            novel["cover_asset_id"] = str(novel["cover_asset_id"])
        novel.pop("narrative_revision", None)
        novel.pop("narrative_revision_operations", None)
        novel["stats"] = {
            "chapter_count": novel.get("current_chapter_count", 0),
            "total_word_count": novel.get("current_word_count", 0)
        }
        return jsonable_encoder(novel, custom_encoder={ObjectId: str})
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/{novel_id}")
async def update_novel(
    novel_id: str,
    req: UpdateNovelRequest,
    actor: Actor = Depends(require_csrf_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """更新指定小说的基础信息（如标题、简介等）。"""
    try:
        await access.require_owned_novel(actor, novel_id)
        success = await NovelService.update_novel_info(
            novel_id, req.model_dump(exclude_unset=True)
        )
        return {"success": success}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.patch("/{novel_id}/status")
async def update_status(
    novel_id: str,
    req: StatusUpdate,
    actor: Actor = Depends(require_csrf_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """更新指定小说的状态（例如从草稿变为连载中）。"""
    try:
        await access.require_owned_novel(actor, novel_id)
        success = await novel_repo.update_novel_status(novel_id, req.status)
        return {"success": success}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/{novel_id}")
async def soft_delete(
    novel_id: str,
    actor: Actor = Depends(require_csrf_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """软删除指定的小说及将其放入回收站。"""
    try:
        await access.require_owned_novel(actor, novel_id)
        success = await NovelService.soft_delete_novel(novel_id)
        return {"success": success}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{novel_id}/restore")
async def restore_novel(
    novel_id: str,
    actor: Actor = Depends(require_csrf_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """从回收站中恢复（取消软删除）指定的小说。"""
    try:
        await access.require_owned_novel(actor, novel_id, include_deleted=True)
        success = await NovelService.restore_novel(novel_id)
        return {"success": success}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/{novel_id}/hard")
async def hard_delete(
    novel_id: str,
    actor: Actor = Depends(require_csrf_actor),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """彻底（物理）删除指定的小说及其所有关联数据，不可恢复。"""
    try:
        await access.require_owned_novel(actor, novel_id, include_deleted=True)
        stats = await NovelService.hard_delete_novel(novel_id)
        return {"message": "Hard deleted successfully", "stats": stats}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
