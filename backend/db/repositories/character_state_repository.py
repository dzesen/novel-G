"""人物状态仓储：追踪角色的当下状态与不可逆的既成事实。

current_state 可覆盖，permanent_facts 只增不改：第 40 章"左臂尽废"不能被
第 50 章"已痊愈"抹掉，否则第 87 章 AI 就会写他双手持剑。

本类自己没有声明任何删除或改写 permanent_facts 的方法——这只是"这个仓储的
操作词汇里不存在这个动作"，不是运行时沙箱或权限拦截。它和其他仓储一样继承
自 BaseRepository，update_one / update_many / bulk_write / hard_delete_one
等通用方法依然可以直接绕过这条规则改写甚至清空 permanent_facts；这是继承
通用 CRUD 的正常代价，不是本类特有的漏洞，也不该被误读成"AI 无法误调"。
真正校验、落地 AI 输出的关卡在阶段 2 服务层（extract_chapter_state_by_ai）：
AI 只产出 JSON，由那一层解释后才会调用到这里的方法。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.collections import CHARACTER_STATES
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id

FACT_KIND_VALUES = {"death", "injury", "identity", "relation", "ability"}


class CharacterStateRepository(BaseRepository):
    def __init__(self) -> None:
        """初始化人物状态仓储，指定集合为 'character_states'。"""
        super().__init__(CHARACTER_STATES)

    async def upsert_state(
        self,
        novel_id: str,
        card_id: str,
        current_state: str,
        as_of_chapter_order: int,
        session: AsyncClientSession | None = None,
    ) -> str:
        """写入或覆盖某角色的当下状态。

        current_state 是覆盖式的，这是设计的一部分：位置、情绪、伤势本就该被覆盖。
        不可逆的事实走 append_permanent_fact，永不被此方法触及。

        Args:
            novel_id: 所属小说 ObjectId 字符串。
            card_id: 引用的 character 卡片 ObjectId 字符串。
            current_state: 可覆盖的当下状态文本。
            as_of_chapter_order: current_state 的截止章序。
            session: 可选 MongoDB 会话，用于事务写入。

        Returns:
            该角色状态文档的 ObjectId 字符串。
        """
        if as_of_chapter_order <= 0:
            raise ValueError("as_of_chapter_order must be greater than 0")

        novel_obj_id = to_object_id(novel_id)
        card_obj_id = to_object_id(card_id)
        now = get_utc_now()
        result = await self.collection.update_one(
            {"novel_id": novel_obj_id, "card_id": card_obj_id},
            {
                "$set": {
                    "current_state": str(current_state).strip(),
                    "as_of_chapter_order": int(as_of_chapter_order),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "novel_id": novel_obj_id,
                    "card_id": card_obj_id,
                    "permanent_facts": [],
                    "created_at": now,
                    "is_deleted": False,
                    "deleted_at": None,
                },
            },
            upsert=True,
            session=session,
        )
        if result.upserted_id is not None:
            return str(result.upserted_id)
        existing = await self.collection.find_one(
            {"novel_id": novel_obj_id, "card_id": card_obj_id},
            projection={"_id": 1},
            session=session,
        )
        return str(existing["_id"])

    async def _get_state(
        self,
        novel_id: str,
        card_id: str,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        """按 novel_id + card_id 取状态文档，不存在时抛 NotFoundError。"""
        state = await self.find_one(
            {"novel_id": to_object_id(novel_id), "card_id": to_object_id(card_id)},
            session=session,
        )
        if not state:
            raise NotFoundError(
                f"Character state for card '{card_id}' was not found"
            )
        return state

    async def append_permanent_fact(
        self,
        novel_id: str,
        card_id: str,
        fact: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        """向某角色追加一条永久事实。只增不改。

        Args:
            novel_id: 所属小说 ObjectId 字符串。
            card_id: 引用的 character 卡片 ObjectId 字符串。
            fact: 含 chapter_order / fact / kind 的事实条目。
            session: 可选 MongoDB 会话，用于事务写入。

        Returns:
            实际追加成功时返回 True。

        Raises:
            NotFoundError: 该角色尚无状态文档（必须先 upsert_state）。
                没有 upsert=True 是刻意的：永久事实绝不能被静默丢弃，
                宁可显式报错也不要凭空插入一条只有事实、没有当下状态的文档。
        """
        kind = str(fact.get("kind", ""))
        if kind not in FACT_KIND_VALUES:
            raise ValueError(f"Unsupported permanent fact kind: {kind}")

        text = str(fact.get("fact", "")).strip()
        if not text:
            raise ValueError("Permanent fact text cannot be empty")

        chapter_order = int(fact.get("chapter_order", 0))
        if chapter_order <= 0:
            raise ValueError("chapter_order must be greater than 0")

        current = await self._get_state(novel_id, card_id, session=session)
        result = await self.collection.update_one(
            {"_id": current["_id"]},
            {
                "$push": {
                    "permanent_facts": {
                        "chapter_order": chapter_order,
                        "fact": text,
                        "kind": kind,
                        "created_at": get_utc_now(),
                    }
                },
                "$set": {"updated_at": get_utc_now()},
            },
            session=session,
        )
        return result.modified_count > 0

    async def get_state(
        self,
        novel_id: str,
        card_id: str,
        session: AsyncClientSession | None = None,
    ) -> Optional[Dict[str, Any]]:
        """取某角色的状态文档；不存在时返回 None。"""
        return await self.find_one(
            {"novel_id": to_object_id(novel_id), "card_id": to_object_id(card_id)},
            session=session,
        )

    async def list_states(
        self,
        novel_id: str,
        card_ids: list[str] | None = None,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        """列出小说下的角色状态，可按卡片 ID 过滤。

        Args:
            novel_id: 所属小说 ObjectId 字符串。
            card_ids: 需要的 character 卡片 ID 列表；None 表示全部。
            session: 可选 MongoDB 会话，用于事务读取。

        Returns:
            角色状态文档列表。
        """
        query: Dict[str, Any] = {"novel_id": to_object_id(novel_id), "is_deleted": False}
        if card_ids is not None:
            query["card_id"] = {"$in": [to_object_id(cid) for cid in card_ids]}
        cursor = self.collection.find(query, session=session)
        return await cursor.to_list(length=None)


character_state_repo = CharacterStateRepository()
