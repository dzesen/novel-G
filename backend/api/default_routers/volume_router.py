from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, List, Optional

from backend.services.novel.volume_service import VolumeService
from backend.db.errors import NotFoundError, InvalidIdError, DuplicateKeyError
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)

router = APIRouter(
    prefix="/api/volumes",
    tags=["volumes"],
    dependencies=[Depends(require_owned_path_resource)],
)


class CreateVolumeRequest(BaseModel):
    novel_id: str
    title: str
    summary: Optional[str] = None
    order_index: Optional[int] = None


class UpdateVolumeRequest(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    order_index: Optional[int] = None


class AcceptVolumeOutlineRequest(BaseModel):
    volumes: List[dict]


@router.post("/create")
async def create_volume(
    req: CreateVolumeRequest,
    actor: Actor = Depends(require_owned_path_resource),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """创建一个新卷，挂载到指定小说下。"""
    data = req.model_dump(exclude_unset=True)
    try:
        await access.require_owned_novel(actor, req.novel_id)
        volume_id = await VolumeService.create_volume(data)
        return {"id": volume_id, "message": "Volume created"}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DuplicateKeyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ValueError, InvalidIdError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/novel/{novel_id}/accept-outline")
async def accept_volume_outline(novel_id: str, req: AcceptVolumeOutlineRequest):
    """接受分卷大纲预览：建卷 + 按 chapter_range 建章存根。小说已有卷时 409（设计 §7.3）。"""
    try:
        # 409 前置守卫：文案必须指路（设计 §7.3）。
        # 用未删卷计数（非 has_volumes 的含软删计数）：垃圾桶里的软删卷不应挡住重建（设计 §7.3）。
        existing = await VolumeService.get_volumes_by_novel(novel_id)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"该小说已有 {len(existing)} 卷，请先清空卷（可在垃圾桶恢复）后重试",
            )
        result = await VolumeService.accept_volume_outline(novel_id, req.volumes)
        return result
    except HTTPException:
        raise
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DuplicateKeyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ValueError, InvalidIdError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/novel/{novel_id}")
async def get_volumes_by_novel(novel_id: str):
    """获取指定小说下的所有卷列表（按 order_index 升序）。"""
    try:
        volumes = await VolumeService.get_volumes_by_novel(novel_id)
        for v in volumes:
            if "_id" in v:
                v["_id"] = str(v["_id"])
            if "novel_id" in v:
                v["novel_id"] = str(v["novel_id"])
        return {"data": volumes}
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{volume_id}")
async def get_volume(volume_id: str):
    """根据ID获取单个卷的详细信息。"""
    try:
        volume = await VolumeService.get_volume_by_id(volume_id)
        volume["_id"] = str(volume["_id"])
        volume["novel_id"] = str(volume["novel_id"])
        return volume
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidIdError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{volume_id}")
async def update_volume(volume_id: str, req: UpdateVolumeRequest):
    """更新卷的基础信息（标题、概要、状态、序号）。"""
    try:
        success = await VolumeService.update_volume_info(volume_id, req.model_dump(exclude_unset=True))
        return {"success": success}
    except DuplicateKeyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ValueError, InvalidIdError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{volume_id}")
async def soft_delete_volume(volume_id: str):
    """软删除指定卷（级联软删除下属章节，并联动扣减小说统计）。"""
    try:
        success = await VolumeService.soft_delete_volume(volume_id)
        return {"success": success}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, InvalidIdError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{volume_id}/restore")
async def restore_volume(volume_id: str):
    """恢复已软删除的卷（级联恢复下属章节，并联动回补小说统计）。"""
    try:
        success = await VolumeService.restore_volume(volume_id)
        return {"success": success}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except DuplicateKeyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ValueError, InvalidIdError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{volume_id}/hard")
async def hard_delete_volume(volume_id: str):
    """彻底物理删除指定卷及其所有关联章节，不可恢复。"""
    try:
        stats = await VolumeService.hard_delete_volume(volume_id)
        return {"message": "Hard deleted successfully", "stats": stats}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
