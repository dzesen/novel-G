"""伏笔线索仓储：追踪每条伏笔的埋设、发展与回收。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.collections import PLOT_THREADS
from backend.db.errors import NotFoundError
from backend.db.utils import to_object_id

THREAD_STATUS_VALUES = {"planted", "developing", "resolved", "abandoned"}
THREAD_IMPORTANCE_VALUES = {"main", "sub"}
THREAD_SOURCE_VALUES = {"outline", "manual"}
# 装配器只喂这两种：已收和废弃的伏笔不该再占上下文预算。
ACTIVE_THREAD_STATUSES = {"planted", "developing"}


def _coerce_chapter_order(value: Any) -> Optional[int]:
    """把章序字段转成正整数；空值表示"尚未确定"。"""
    if value in (None, ""):
        return None
    order = int(value)
    if order <= 0:
        raise ValueError("chapter order must be greater than 0")
    return order


class PlotThreadRepository(BaseRepository):
    def __init__(self) -> None:
        """初始化伏笔仓储，指定集合为 'plot_threads'。"""
        super().__init__(PLOT_THREADS)

    async def create_thread(
        self,
        novel_id: str,
        data: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> str:
        """创建一条伏笔线索。

        Args:
            novel_id: 所属小说 ObjectId 字符串。
            data: 伏笔字段。
            session: 可选 MongoDB 会话，用于事务写入。

        Returns:
            新伏笔的 ObjectId 字符串。
        """
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("Plot thread name cannot be empty")

        status = str(data.get("status") or "planted")
        if status not in THREAD_STATUS_VALUES:
            raise ValueError(f"Unsupported plot thread status: {status}")

        importance = str(data.get("importance") or "sub")
        if importance not in THREAD_IMPORTANCE_VALUES:
            raise ValueError(f"Unsupported plot thread importance: {importance}")

        source = str(data.get("source") or "outline")
        if source not in THREAD_SOURCE_VALUES:
            raise ValueError(f"Unsupported plot thread source: {source}")

        prepared = {
            "novel_id": to_object_id(novel_id),
            "name": name,
            "description": str(data.get("description", "")).strip(),
            "status": status,
            "importance": importance,
            "planted_chapter_order": _coerce_chapter_order(data.get("planted_chapter_order")),
            "due_chapter_order": _coerce_chapter_order(data.get("due_chapter_order")),
            "resolved_chapter_order": _coerce_chapter_order(data.get("resolved_chapter_order")),
            "notes": str(data.get("notes", "")).strip(),
            "source": source,
        }
        return await self.insert_one(prepared, session=session)

    async def list_threads(
        self,
        novel_id: str,
        statuses: set[str] | None = None,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        """列出小说下的伏笔，可按状态过滤。

        Args:
            novel_id: 所属小说 ObjectId 字符串。
            statuses: 需要的状态集合；None 表示全部。
            session: 可选 MongoDB 会话，用于事务读取。

        Returns:
            按 due_chapter_order 升序排列的伏笔列表；due 为空的排在最后。
        """
        query: Dict[str, Any] = {"novel_id": to_object_id(novel_id), "is_deleted": False}
        if statuses:
            query["status"] = {"$in": sorted(statuses)}
        cursor = self.collection.find(query, session=session).sort("created_at", 1)
        threads = await cursor.to_list(length=None)
        # MongoDB 的 BSON 比较序里 Null 排在 Number 之前，若直接对
        # due_chapter_order 做 $sort: 1，没有截止章节的伏笔反而会排到最前面，
        # 变成"最紧迫"，与装配器的预期（无 due = 最不紧迫，最先被砍）正好相反。
        # 结果集很小（单本小说的伏笔，至多几十条），所以改在 Python 端排序，
        # 用 (due 是否为空, due 值) 作为键，让空值稳定排在最后。
        threads.sort(
            key=lambda t: (
                t.get("due_chapter_order") is None,
                t.get("due_chapter_order") if t.get("due_chapter_order") is not None else 0,
            )
        )
        return threads

    async def _get_thread(
        self,
        novel_id: str,
        thread_id: str,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        """按 novel_id + thread_id 取伏笔，不存在时抛 NotFoundError。"""
        thread = await self.find_one(
            {"_id": to_object_id(thread_id), "novel_id": to_object_id(novel_id)},
            session=session,
        )
        if not thread:
            raise NotFoundError(f"Plot thread '{thread_id}' was not found")
        return thread

    async def update_thread(
        self,
        novel_id: str,
        thread_id: str,
        data: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        """更新伏笔的可变字段。

        Args:
            novel_id: 所属小说 ObjectId 字符串。
            thread_id: 伏笔 ObjectId 字符串。
            data: 待更新字段。
            session: 可选 MongoDB 会话，用于事务写入。

        Returns:
            实际修改了文档时返回 True。
        """
        current = await self._get_thread(novel_id, thread_id, session=session)
        allowed = {
            "name",
            "description",
            "status",
            "importance",
            "planted_chapter_order",
            "due_chapter_order",
            "resolved_chapter_order",
            "notes",
        }
        prepared = {key: value for key, value in data.items() if key in allowed}
        if "status" in prepared and prepared["status"] not in THREAD_STATUS_VALUES:
            raise ValueError(f"Unsupported plot thread status: {prepared['status']}")
        if "importance" in prepared and prepared["importance"] not in THREAD_IMPORTANCE_VALUES:
            raise ValueError(f"Unsupported plot thread importance: {prepared['importance']}")
        for key in ("planted_chapter_order", "due_chapter_order", "resolved_chapter_order"):
            if key in prepared:
                prepared[key] = _coerce_chapter_order(prepared[key])
        if not prepared:
            return False
        return await self.update_one({"_id": current["_id"]}, prepared, session=session)

    async def soft_delete_thread(
        self,
        novel_id: str,
        thread_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """软删除一条伏笔。"""
        current = await self._get_thread(novel_id, thread_id, session=session)
        return await self.soft_delete_one({"_id": current["_id"]}, session=session)


plot_thread_repo = PlotThreadRepository()
