"""伏笔线索只读查询 API。

**只读是刻意的**（设计 §4.2）：伏笔的唯一创建入口是 accept 章细纲
（2a 设计 §3.2 —— 细纲提议 new_threads、accept 时创建并回填 id）。
多开一个写入口会让那条唯一路径失去唯一性。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.plot_thread_repository import plot_thread_repo


router = APIRouter(prefix="/api/plot-threads", tags=["plot-threads"])


def _serialize_thread(thread: dict) -> dict:
    """把伏笔文档里的 ObjectId 字段转为字符串。

    与 reference_card_router._serialize_card / chapter_router._serialize 同一手法。
    漏掉任何一个 ObjectId 字段都会在 JSON 序列化时 500。
    """
    result = dict(thread)
    for key in ("_id", "novel_id"):
        if key in result:
            result[key] = str(result[key])
    return result


@router.get("/novel/{novel_id}")
async def list_threads(novel_id: str):
    """列出小说下全部未删除的伏笔，按 due_chapter_order 升序、空 due 排最后。

    不提供 statuses 过滤参数（设计 §4.2）：前端要的是全量 id→名 映射，
    按状态挑选在客户端做即可——单本小说的伏笔至多几十条。
    """
    try:
        threads = await plot_thread_repo.list_threads(novel_id)
        return {"data": [_serialize_thread(thread) for thread in threads]}
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
