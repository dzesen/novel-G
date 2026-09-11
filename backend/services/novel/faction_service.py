from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from backend.db.errors import DuplicateKeyError
from backend.db.repositories.faction_relation_repository import faction_relation_repo
from backend.db.repositories.faction_repository import faction_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import to_object_id

logger = logging.getLogger(__name__)


class FactionService:
    """
    阵营（Faction）服务层，编排跨集合的业务逻辑。
    纯集合内操作由 FactionRepository 负责，跨集合联动由此处编排。
    """

    @staticmethod
    def _next_business_ids(first_id: str, prefix: str, count: int) -> list[str]:
        """根据首个业务 ID 生成连续 ID 列表。

        Args:
            first_id: 已解析出的首个业务 ID，例如 fac_000001。
            prefix: 业务 ID 前缀，例如 fac 或 fr。
            count: 需要生成的 ID 数量。

        Returns:
            连续业务 ID 列表。
        """
        try:
            start = int(first_id.split("_", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"Invalid {prefix} sequence id: {first_id}") from exc
        return [f"{prefix}_{start + offset:06d}" for offset in range(count)]

    @staticmethod
    def _normalize_generated_core_faction(data: Dict[str, Any], *, faction_id: str, sort_order: int) -> Dict[str, Any]:
        """将 AI 生成的核心阵营转换为 factions 集合可写入文档。

        Args:
            data: 单个核心阵营生成结果。
            faction_id: 后端分配的业务阵营 ID。
            sort_order: 阵营排序权重。

        Returns:
            可写入 factions 集合的阵营文档片段。
        """
        payload = dict(data)
        payload["faction_id"] = faction_id
        payload["level_type"] = "core"
        payload["parent_faction_id"] = None
        payload["active_status"] = "active"
        payload["sort_order"] = sort_order
        payload.setdefault("alias", [])
        payload.setdefault("first_appearance_volume_id", None)
        payload.setdefault("first_appearance_chapter_id", None)
        payload.setdefault("extra", {})
        return payload

    @staticmethod
    def _normalize_generated_relation(
        data: Dict[str, Any],
        *,
        relation_id: str,
        name_to_faction_id: Dict[str, str],
    ) -> Dict[str, Any]:
        """将 AI 生成的阵营名称关系映射为 faction_id 关系。

        Args:
            data: 单条 AI 生成关系，使用阵营名称引用端点。
            relation_id: 后端分配的业务关系 ID。
            name_to_faction_id: 阵营名称到业务阵营 ID 的映射。

        Returns:
            可写入 faction_relations 集合的关系文档片段。
        """
        source_name = str(data.get("source_faction_name", "")).strip()
        target_name = str(data.get("target_faction_name", "")).strip()
        if source_name not in name_to_faction_id:
            raise ValueError(f"关系发起方阵营不存在: {source_name}")
        if target_name not in name_to_faction_id:
            raise ValueError(f"关系目标方阵营不存在: {target_name}")

        # AI 阶段用名称便于阅读，落库阶段必须统一转换为稳定业务 ID。
        payload = {
            "relation_id": relation_id,
            "source_faction_id": name_to_faction_id[source_name],
            "target_faction_id": name_to_faction_id[target_name],
            "source_faction_name": source_name,
            "target_faction_name": target_name,
            "relation_type": data.get("relation_type"),
            "current_state": data.get("current_state", ""),
            "core_conflict": data.get("core_conflict", ""),
            "hidden_tension": data.get("hidden_tension", ""),
            "possible_change": data.get("possible_change", ""),
            "intensity": data.get("intensity", 3),
            "is_active": data.get("is_active", True),
        }
        return payload

    @staticmethod
    async def _ensure_unique_active_name(
        novel_id: str,
        level_type: str,
        name: str,
        *,
        exclude_faction_id: str | None = None,
        session=None,
    ) -> None:
        """校验同一小说同一层级下未删除阵营名称不重复。

        Args:
            novel_id: 小说 ObjectId 字符串。
            level_type: 阵营层级类型。
            name: 待校验阵营名称。
            exclude_faction_id: 更新时需要排除的当前阵营 ID。
            session: 可选 MongoDB 会话，用于事务读取。

        Returns:
            无。

        Raises:
            DuplicateKeyError: 已存在同名未删除阵营时抛出。
        """
        query: Dict[str, Any] = {
            "novel_id": to_object_id(novel_id),
            "level_type": level_type,
            "name": name,
        }
        if exclude_faction_id:
            query["faction_id"] = {"$ne": exclude_faction_id}
        existing = await faction_repo.find_one(query, session=session)
        if existing:
            raise DuplicateKeyError(f"同一小说同一势力层级下已存在同名阵营: {name}")

    @staticmethod
    async def has_core_factions_initialized(novel_id: str, *, session=None) -> bool:
        """判断小说是否已经存在核心阵营初始化记录。

        Args:
            novel_id: 小说 ObjectId 字符串。
            session: 可选 MongoDB 会话，用于事务读取。

        Returns:
            存在未删除或已软删除核心阵营时返回 True。
        """
        count = await faction_repo.count_factions_by_level_type(
            novel_id,
            "core",
            include_deleted=True,
            session=session,
        )
        return count > 0

    @staticmethod
    async def create_faction(novel_id: str, data: Dict[str, Any]) -> Tuple[str, str]:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("create", novel_id, data)

    @staticmethod
    async def bulk_create_core_factions_with_relations(
        novel_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, List[Dict[str, Any]]]:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("bulk_create", novel_id, data)

    @staticmethod
    async def get_factions_by_novel(novel_id: str) -> List[Dict[str, Any]]:
        """获取指定小说下所有阵营列表。

        Args:
            novel_id: 小说 ObjectId 字符串。

        Returns:
            阵营文档列表。
        """
        await novel_repo.get_novel_by_id(novel_id)
        return await faction_repo.get_factions_by_novel(novel_id)

    @staticmethod
    async def get_faction(novel_id: str, faction_id: str) -> Dict[str, Any]:
        """获取指定小说下的单个阵营详情。

        Args:
            novel_id: 小说 ObjectId 字符串。
            faction_id: 业务层阵营 ID。

        Returns:
            阵营文档。
        """
        await novel_repo.get_novel_by_id(novel_id)
        return await faction_repo.get_faction(novel_id, faction_id)

    @staticmethod
    async def get_factions_by_level_type(novel_id: str, level_type: str) -> List[Dict[str, Any]]:
        """获取指定小说下特定层级类型的阵营列表。

        Args:
            novel_id: 小说 ObjectId 字符串。
            level_type: 阵营层级类型。

        Returns:
            阵营文档列表。
        """
        await novel_repo.get_novel_by_id(novel_id)
        return await faction_repo.get_factions_by_level_type(novel_id, level_type)

    @staticmethod
    async def get_deleted_factions_by_level_type(
        novel_id: str,
        level_type: str | None = None,
    ) -> List[Dict[str, Any]]:
        """获取指定小说下已软删除阵营列表，可按层级过滤。

        Args:
            novel_id: 小说 ObjectId 字符串。
            level_type: 可选阵营层级类型。

        Returns:
            已软删除阵营文档列表。
        """
        await novel_repo.get_novel_by_id(novel_id)
        return await faction_repo.get_deleted_factions_by_level_type(novel_id, level_type)

    @staticmethod
    async def get_child_factions(novel_id: str, parent_faction_id: str) -> List[Dict[str, Any]]:
        """获取指定父级阵营的所有直接子阵营。

        Args:
            novel_id: 小说 ObjectId 字符串。
            parent_faction_id: 父级业务阵营 ID。

        Returns:
            子阵营文档列表。
        """
        await novel_repo.get_novel_by_id(novel_id)
        return await faction_repo.get_child_factions(novel_id, parent_faction_id)

    @staticmethod
    async def update_faction_info(novel_id: str, faction_id: str, update_data: Dict[str, Any]) -> bool:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("update", novel_id, update_data, faction_id=faction_id)

    @staticmethod
    async def batch_update_sort_order(novel_id: str, sort_map: Dict[str, int]) -> int:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("sort", novel_id, sort_map)

    @staticmethod
    async def soft_delete_faction(novel_id: str, faction_id: str) -> bool:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("soft_delete", novel_id, {}, faction_id=faction_id)

    @staticmethod
    async def restore_faction(novel_id: str, faction_id: str) -> bool:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("restore", novel_id, {}, faction_id=faction_id)

    @staticmethod
    async def hard_delete_faction(novel_id: str, faction_id: str) -> Dict[str, Any]:
        from backend.services.novel.faction_mutations import mutate_factions
        return await mutate_factions("hard_delete", novel_id, {}, faction_id=faction_id)
