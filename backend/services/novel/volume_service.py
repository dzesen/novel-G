import logging
from typing import Any, Dict, List

from pydantic import ValidationError

from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import DuplicateKeyError
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import to_object_id, get_utc_now
from backend.llm.schemas.novel_pydantic import VolumeOutlineResultSchema
from backend.services.novel.outline_validation import validate_chapter_ranges

logger = logging.getLogger(__name__)


class VolumeService:
    """
    卷（Volume）服务层，编排跨集合的级联操作。
    纯集合内操作由 VolumeRepository 负责，跨集合联动由此处编排。
    """

    # 创建 

    @staticmethod
    async def create_volume(data: Dict[str, Any]) -> str:
        """
        创建新卷：
        1. 校验 novel_id 指向的小说存在
        2. 调用 repository 插入卷文档
        3. 向上联动：novels.current_volume_count + 1
        """
        novel_id = data.get("novel_id")
        if not novel_id:
            raise ValueError("novel_id is required")

        auto_order_index = data.get("order_index") is None

        async def _create(session):
            """在同一个写入单元内创建卷并更新小说统计。"""
            await novel_repo.get_novel_by_id(novel_id, session=session)
            volume_id = await volume_repo.create_volume(data, session=session)
            await novel_repo.increment_novel_stats(
                novel_id,
                {"current_volume_count": 1},
                session=session,
            )
            return volume_id

        try:
            return await run_mongo_write_unit(_create, "create_volume")
        except DuplicateKeyError:
            if not auto_order_index:
                raise
            # 自动序号在并发创建时可能撞唯一索引，重新读取最大序号后再尝试一次。
            return await run_mongo_write_unit(_create, "create_volume_retry")

    @staticmethod
    async def has_volumes(novel_id: str, *, session=None) -> bool:
        """小说下是否已存在卷（含软删，仿 has_core_factions_initialized）。"""
        obj_id = to_object_id(novel_id)
        count = await volume_repo.count_documents(
            {"novel_id": obj_id}, include_deleted=True, session=session
        )
        return count > 0

    @staticmethod
    async def accept_volume_outline(novel_id: str, volumes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """接受分卷大纲预览：建卷 + 按 chapter_range 建章存根（设计 §7）。

        非原子（单机 mongod 无事务，run_mongo_write_unit 降级为顺序写）。四层防御见设计 §7.2：
        ①前置全校验（本方法开头，第一次写之前）②run_mongo_write_unit(auto) ③insert_many ④失败精确上报。
        不做幂等续跑（§7.4）；409 前置守卫由端点负责。
        """
        novel = await novel_repo.get_novel_by_id(novel_id)
        number_of_chapters = int(novel.get("number_of_chapters") or 0)

        # 层 1：前置全校验，必须在第一次写之前。先用严格 schema 校验整份 payload
        # （title/summary/arc/chapter_range 形状，extra="forbid"），
        # 再用 validate_chapter_ranges 做 schema 做不到的跨卷覆盖 1..N 检查。
        # 顺序不能反：schema 校验通过后，_build 里的 vol["title"] 等字段访问才安全。
        try:
            VolumeOutlineResultSchema.model_validate({"volumes": volumes})
        except ValidationError as exc:
            raise ValueError(f"分卷数据非法，未做任何写入：{exc}") from exc

        problems = validate_chapter_ranges(volumes, number_of_chapters)
        if problems:
            raise ValueError("分卷区间非法，未做任何写入：" + "；".join(problems))

        novel_obj_id = to_object_id(novel_id)

        async def _build(session):
            created_volume_ids: List[str] = []
            total_chapters = 0
            chapters_repo = BaseRepository(collections.CHAPTERS)
            try:
                for order_index, vol in enumerate(volumes, start=1):
                    rng = vol["chapter_range"]
                    start, end = int(rng["start"]), int(rng["end"])
                    volume_id = await volume_repo.create_volume(
                        {
                            "novel_id": novel_id,
                            "title": vol["title"],
                            "summary": vol.get("summary", ""),
                            "arc": vol.get("arc", ""),
                            "chapter_range": {"start": start, "end": end},
                            "order_index": order_index,
                        },
                        session=session,
                    )
                    created_volume_ids.append(volume_id)
                    # 层 3：批量建存根，一次 insert_many 而非逐章 insert_one。
                    stub_docs = [
                        {
                            "novel_id": novel_obj_id,
                            "volume_id": to_object_id(volume_id),
                            "title": f"第{n}章",
                            "order_index": n,  # 全书章号（见计划建模决策）
                            "status": "draft",
                            "summary": "",
                            "content": "",
                            "word_count": 0,
                        }
                        for n in range(start, end + 1)
                    ]
                    await chapters_repo.insert_many(stub_docs, session=session)
                    await volume_repo.update_volume_stats(
                        volume_id, chapter_count_delta=len(stub_docs), session=session
                    )
                    total_chapters += len(stub_docs)

                await novel_repo.increment_novel_stats(
                    novel_id,
                    {"current_volume_count": len(created_volume_ids), "current_chapter_count": total_chapters},
                    session=session,
                )
                return {
                    "volume_count": len(created_volume_ids),
                    "chapter_count": total_chapters,
                    "volume_ids": created_volume_ids,
                }
            except Exception:
                # 层 4：中途失败精确上报已建内容，不谎报回滚（设计 §7.2/§7.6）。
                logger.error(
                    "accept_volume_outline 中途失败：novel_id=%s 已建 %s 卷 / %s 章（非原子，未回滚）",
                    novel_id, len(created_volume_ids), total_chapters,
                )
                raise

        # 层 2：run_mongo_write_unit(auto)——单机降级顺序写、副本集白捡原子性。
        return await run_mongo_write_unit(_build, "accept_volume_outline")

    # 查询（透传）

    @staticmethod
    async def get_volumes_by_novel(novel_id: str) -> List[Dict[str, Any]]:
        """获取指定小说下所有卷列表。"""
        await novel_repo.get_novel_by_id(novel_id)
        return await volume_repo.get_volumes_by_novel(novel_id)

    @staticmethod
    async def get_volume_by_id(volume_id: str) -> Dict[str, Any]:
        """获取单个卷详情。"""
        return await volume_repo.get_volume_by_id(volume_id)

    # 更新（透传） 

    @staticmethod
    async def update_volume_info(volume_id: str, update_data: Dict[str, Any]) -> bool:
        """更新卷基础信息。"""
        return await volume_repo.update_volume_info(volume_id, update_data)

    @staticmethod
    async def update_volume_stats(
        volume_id: str,
        word_count_delta: int = 0,
        chapter_count_delta: int = 0,
    ) -> bool:
        """更新卷统计（由下级章节增删时回调使用）。"""
        return await volume_repo.update_volume_stats(
            volume_id,
            word_count_delta,
            chapter_count_delta,
        )

    # 软删除（级联） 

    @staticmethod
    async def soft_delete_volume(volume_id: str) -> bool:
        """
        软删除卷 + 级联：
        1. 获取卷信息（用于联动统计扣减）
        2. 软删除该卷自身
        3. 级联软删除该卷下所有章节
        4. 向上联动：novels.current_volume_count - 1，novels.current_word_count 扣减该卷字数
        """
        async def _delete(session):
            """在同一个写入单元内软删除卷、子章节并扣减小说统计。"""
            volume = await volume_repo.get_volume_by_id(volume_id, session=session)
            novel_id = str(volume["novel_id"])
            volume_word_count = volume.get("word_count", 0)

            success = await volume_repo.soft_delete_volume(volume_id, session=session)
            if not success:
                return False

            obj_id = to_object_id(volume_id)
            chapters_repo = BaseRepository(collections.CHAPTERS)
            active_chapter_count = await chapters_repo.count_documents(
                {"volume_id": obj_id},
                session=session,
            )
            chapters_deleted = await chapters_repo.update_many(
                {"volume_id": obj_id},
                {
                    "is_deleted": True,
                    "deleted_at": get_utc_now(),
                    "deleted_with_volume_id": obj_id,
                },
                include_deleted=False,
                session=session,
            )
            logger.info("级联软删除卷 %s 下 %s 个 chapters", volume_id, chapters_deleted)

            stats_delta = {"current_volume_count": -1}
            if active_chapter_count > 0:
                stats_delta["current_chapter_count"] = -active_chapter_count
            if volume_word_count > 0:
                stats_delta["current_word_count"] = -volume_word_count
            await novel_repo.increment_novel_stats(novel_id, stats_delta, session=session)

            return True

        return await run_mongo_write_unit(_delete, "soft_delete_volume")

    # 恢复（级联） 

    @staticmethod
    async def restore_volume(volume_id: str) -> bool:
        """
        恢复已软删除的卷 + 级联：
        1. 恢复卷自身
        2. 级联恢复该卷下所有章节
        3. 向上联动：novels.current_volume_count + 1，回补字数
        """
        async def _restore(session):
            """在同一个写入单元内恢复卷、子章节并回补小说统计。"""
            obj_id = to_object_id(volume_id)
            volume = await volume_repo.find_one({"_id": obj_id}, include_deleted=True, session=session)
            if not volume or not volume.get("is_deleted", False):
                raise ValueError(f"Volume {volume_id} is not in deleted state")

            novel_id = str(volume["novel_id"])
            volume_word_count = volume.get("word_count", 0)

            success = await volume_repo.restore_volume(volume_id, session=session)
            if not success:
                return False

            chapters_repo = BaseRepository(collections.CHAPTERS)
            chapters_restored = await chapters_repo.update_many(
                {"volume_id": obj_id, "deleted_with_volume_id": obj_id},
                {
                    "is_deleted": False,
                    "deleted_at": None,
                    "deleted_with_volume_id": None,
                },
                include_deleted=True,
                session=session,
            )
            logger.info("级联恢复卷 %s 下 %s 个 chapters", volume_id, chapters_restored)

            stats_delta = {"current_volume_count": 1}
            if chapters_restored > 0:
                stats_delta["current_chapter_count"] = chapters_restored
            if volume_word_count > 0:
                stats_delta["current_word_count"] = volume_word_count
            await novel_repo.increment_novel_stats(novel_id, stats_delta, session=session)

            return True

        return await run_mongo_write_unit(_restore, "restore_volume")

    # 硬删除（级联） 

    @staticmethod
    async def hard_delete_volume(volume_id: str) -> Dict[str, Any]:
        """
        物理删除卷 + 级联：
        1. 校验该卷已处于软删除状态
        2. 级联物理删除所有关联章节
        3. 物理删除卷自身
        4. 返回删除统计
        """
        async def _delete(session):
            """在同一个写入单元内物理删除卷及其子章节。"""
            obj_id = to_object_id(volume_id)

            volume = await volume_repo.find_one({"_id": obj_id}, include_deleted=True, session=session)
            if not volume:
                raise ValueError(f"Volume {volume_id} not found")
            if not volume.get("is_deleted", False):
                raise ValueError("Only soft-deleted volumes can be permanently deleted")

            stats = {}

            chapters_repo = BaseRepository(collections.CHAPTERS)
            stats["chapters_deleted"] = await chapters_repo.hard_delete_many(
                {"volume_id": obj_id},
                session=session,
            )

            volume_deleted = await volume_repo.hard_delete_volume(volume_id, session=session)
            stats["volume_deleted"] = 1 if volume_deleted else 0

            logger.info(f"硬删除卷 {volume_id} 完成: {stats}")
            return stats

        return await run_mongo_write_unit(_delete, "hard_delete_volume")
