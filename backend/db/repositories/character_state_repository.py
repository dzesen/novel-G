"""人物状态仓储：追踪角色的当下状态与不可逆的既成事实。

current_state 可覆盖，permanent_facts 对 AI 的写入路径只增不改：第 40 章
"左臂尽废"不能被第 50 章"已痊愈"抹掉，否则第 87 章 AI 就会写他双手持剑。
append_permanent_fact 仍然是 AI（阶段 2 服务层 extract_chapter_state_by_ai /
accept_chapter_state）唯一的写入入口，且只增不改——这条不变量对 AI 没有变。

人工纠错是刻意打破上面这条边界的第二写入面（设计 §2.2/§2.3/§5.3，关闭已知
局限 B1）：set_current_state / update_permanent_fact / delete_permanent_fact
让人类可以订正 current_state、改写或删除单条 permanent_fact，但止步于"纠正"
——不提供"凭空新增一条 fact"的人工入口，新增永远只能走 AI 的
append_permanent_fact。这三个人工方法都要求目标文档/fact 已存在，绝不
upsert、找不到就显式抛 NotFoundError，不会静默失败或凭空造文档。

它和其他仓储一样继承自 BaseRepository，update_one / update_many / bulk_write /
hard_delete_one 等通用方法依然可以直接绕过上面所有校验改写甚至清空
permanent_facts；这是继承通用 CRUD 的正常代价，不是本类特有的漏洞。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from bson import ObjectId
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
                        "id": ObjectId(),
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

    async def set_current_state(
        self,
        novel_id: str,
        card_id: str,
        current_state: str,
        as_of_chapter_order: int,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """人工订正 current_state。要求文档已存在，否则抛 NotFoundError。

        与 AI 的 upsert_state 刻意区分：人工编辑绝不凭空造一份新状态文档。
        """
        if as_of_chapter_order <= 0:
            raise ValueError("as_of_chapter_order must be greater than 0")
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "novel_id": to_object_id(novel_id),
                "card_id": to_object_id(card_id),
                "is_deleted": False,
            },
            {
                "$set": {
                    "current_state": str(current_state).strip(),
                    "as_of_chapter_order": int(as_of_chapter_order),
                    "updated_at": now,
                }
            },
            session=session,
        )
        if result.matched_count == 0:
            raise NotFoundError(f"Character state for card '{card_id}' was not found")
        return result.modified_count > 0

    async def update_permanent_fact(
        self,
        novel_id: str,
        card_id: str,
        fact_id: str,
        fields: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        """按 fact id 改单条永久事实的 fact/kind/chapter_order。

        状态文档或该 fact id 不存在时抛 NotFoundError。
        """
        set_ops: Dict[str, Any] = {}
        if fields.get("fact") is not None:
            text = str(fields["fact"]).strip()
            if not text:
                raise ValueError("Permanent fact text cannot be empty")
            set_ops["permanent_facts.$.fact"] = text
        if fields.get("kind") is not None:
            kind = str(fields["kind"])
            if kind not in FACT_KIND_VALUES:
                raise ValueError(f"Unsupported permanent fact kind: {kind}")
            set_ops["permanent_facts.$.kind"] = kind
        if fields.get("chapter_order") is not None:
            chapter_order = int(fields["chapter_order"])
            if chapter_order <= 0:
                raise ValueError("chapter_order must be greater than 0")
            set_ops["permanent_facts.$.chapter_order"] = chapter_order
        if not set_ops:
            return False
        set_ops["updated_at"] = get_utc_now()
        result = await self.collection.update_one(
            {
                "novel_id": to_object_id(novel_id),
                "card_id": to_object_id(card_id),
                "permanent_facts.id": to_object_id(fact_id),
                "is_deleted": False,
            },
            {"$set": set_ops},
            session=session,
        )
        if result.matched_count == 0:
            raise NotFoundError(f"Permanent fact '{fact_id}' was not found")
        return result.modified_count > 0

    async def delete_permanent_fact(
        self,
        novel_id: str,
        card_id: str,
        fact_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """按 fact id 删单条永久事实。未删到（状态或 fact 不存在）抛 NotFoundError。"""
        result = await self.collection.update_one(
            {
                "novel_id": to_object_id(novel_id),
                "card_id": to_object_id(card_id),
                "permanent_facts.id": to_object_id(fact_id),
                "is_deleted": False,
            },
            {
                "$pull": {"permanent_facts": {"id": to_object_id(fact_id)}},
                "$set": {"updated_at": get_utc_now()},
            },
            session=session,
        )
        if result.matched_count == 0:
            raise NotFoundError(f"Permanent fact '{fact_id}' was not found")
        return result.modified_count > 0

    async def ensure_fact_ids(
        self,
        novel_id: str,
        session: AsyncClientSession | None = None,
    ) -> int:
        """给历史上缺 id 的 permanent_facts 幂等补 id，返回修复条数。

        管理列表端点在返回前调用（唯一一处"读时写"，见设计 §5.4）。
        逐条 Python 判断而非 dot-notation 查询，稳妥覆盖"同文档部分有部分无 id"。
        """
        cursor = self.collection.find(
            {"novel_id": to_object_id(novel_id), "is_deleted": False}, session=session
        )
        docs = await cursor.to_list(length=None)
        fixed = 0
        for doc in docs:
            facts = doc.get("permanent_facts") or []
            missing = [f for f in facts if "id" not in f]
            if not missing:
                continue
            for fact in missing:
                fact["id"] = ObjectId()
            fixed += len(missing)
            await self.collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {"permanent_facts": facts, "updated_at": get_utc_now()}},
                session=session,
            )
        return fixed


character_state_repo = CharacterStateRepository()
