import hashlib
import json
import logging
from typing import Any, Dict, List

from bson import ObjectId
from pydantic import ValidationError

from backend.db.repositories.volume_repository import volume_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import DuplicateKeyError
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.utils import to_object_id, get_utc_now
from backend.llm.schemas.novel_pydantic import VolumeOutlineResultSchema
from backend.services.novel.outline_validation import validate_chapter_ranges
from backend.services.novel.chapter_timeline import ChapterTimeline
from backend.services.novel.state_timeline import (
    mark_from_chapter_stale,
    mark_downstream_stale,
    record_chapter_tombstone,
)

logger = logging.getLogger(__name__)


class VolumeService:
    """
    卷（Volume）服务层，编排跨集合的级联操作。
    纯集合内操作由 VolumeRepository 负责，跨集合联动由此处编排。
    """

    # 创建 

    @staticmethod
    async def _execute_create_volume(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        volume_id = mutation.child_id("volume")
        stored = await volume_repo.find_one(
            {"_id": to_object_id(volume_id)},
            include_deleted=True,
            session=session,
        )
        if stored is None:
            prepared = dict(command["volume"])
            prepared["_id"] = to_object_id(volume_id)
            await volume_repo.create_volume(prepared, session=session)
        await mutation.receipt("volume", {"volume_id": volume_id})
        current = await novel_repo.get_novel_by_id(novel_id, session=session)
        target = command["novel_stats_after"]
        deltas = {
            key: int(value) - int(current.get(key, 0))
            for key, value in target.items()
            if int(value) != int(current.get(key, 0))
        }
        if deltas:
            await novel_repo.increment_novel_stats(
                novel_id, deltas, session=session
            )
        await mutation.receipt("novel_stats", target)
        return volume_id

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
        if not str(data.get("title") or "").strip():
            raise ValueError("Volume title cannot be empty")
        novel = await novel_repo.get_novel_by_id(novel_id)
        if data.get("order_index") is not None:
            duplicate = await volume_repo.find_one({
                "novel_id": to_object_id(novel_id),
                "order_index": int(data["order_index"]),
            })
            if duplicate:
                raise DuplicateKeyError(
                    f"同一小说下 order_index={data['order_index']} 已存在，请使用其他序号"
                )
        volume_id = str(ObjectId())
        command = MutationCommand(
            novel_id=str(novel_id),
            idempotency_key=f"create-volume:{volume_id}",
            operation="create_volume",
            payload={
                "volume": dict(data),
                "novel_stats_after": {
                    "current_volume_count": int(
                        novel.get("current_volume_count", 0)
                    ) + 1,
                    "current_chapter_count": int(
                        novel.get("current_chapter_count", 0)
                    ),
                    "current_word_count": int(
                        novel.get("current_word_count", 0)
                    ),
                },
            },
            before_image={"novel": novel},
            child_ids={"volume": volume_id},
        )
        try:
            return await commit_mutation(command, VolumeService._execute_create_volume)
        except DuplicateKeyError:
            if not auto_order_index:
                raise
            return await commit_mutation(command, VolumeService._execute_create_volume)

    @staticmethod
    async def has_volumes(novel_id: str, *, session=None) -> bool:
        """小说下是否已存在卷（含软删，仿 has_core_factions_initialized）。"""
        obj_id = to_object_id(novel_id)
        count = await volume_repo.count_documents(
            {"novel_id": obj_id}, include_deleted=True, session=session
        )
        return count > 0

    @staticmethod
    async def _execute_accept_volume_outline(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        chapters_repo = BaseRepository(collections.CHAPTERS)
        completed_volumes = 0
        completed_chapters = 0
        try:
            for index, item in enumerate(command["volumes"]):
                volume_id = str(item["volume_id"])
                volume_obj_id = to_object_id(volume_id)
                stored = await volume_repo.find_one(
                    {"_id": volume_obj_id}, include_deleted=True, session=session
                )
                if stored is None:
                    volume_data = dict(item["volume"])
                    volume_data["_id"] = volume_obj_id
                    await volume_repo.create_volume(volume_data, session=session)
                await mutation.receipt(
                    f"volume_{index}", {"volume_id": volume_id}
                )

                chapter_docs = []
                chapter_ids = []
                for stored_doc in item["chapters"]:
                    chapter = dict(stored_doc)
                    chapter["_id"] = to_object_id(str(chapter["_id"]))
                    chapter["novel_id"] = to_object_id(str(chapter["novel_id"]))
                    chapter["volume_id"] = volume_obj_id
                    chapter_docs.append(chapter)
                    chapter_ids.append(chapter["_id"])
                existing = await chapters_repo.find_many(
                    {"_id": {"$in": chapter_ids}},
                    include_deleted=True,
                    session=session,
                )
                existing_ids = {doc["_id"] for doc in existing}
                missing = [doc for doc in chapter_docs if doc["_id"] not in existing_ids]
                if missing:
                    await chapters_repo.insert_many(missing, session=session)
                await mutation.receipt(
                    f"chapters_{index}",
                    {"chapter_ids": [str(chapter_id) for chapter_id in chapter_ids]},
                )

                await volume_repo.update_volume_info(
                    volume_id,
                    {"chapter_range": item["volume"]["chapter_range"]},
                    session=session,
                )
                await BaseRepository(collections.VOLUMES).update_one(
                    {"_id": volume_obj_id},
                    {"chapter_count": len(chapter_docs)},
                    include_deleted=True,
                    session=session,
                )
                await mutation.receipt(
                    f"volume_stats_{index}", {"chapter_count": len(chapter_docs)}
                )
                completed_volumes += 1
                completed_chapters += len(chapter_docs)

            await novel_repo.update_novel_stats(
                novel_id, command["novel_stats_after"], session=session
            )
            await mutation.receipt("novel_stats", command["novel_stats_after"])
            return dict(command["result"])
        except Exception:
            logger.error(
                "accept_volume_outline 中途失败：novel_id=%s 已建 %s 卷 / %s 章（可恢复）",
                novel_id,
                completed_volumes,
                completed_chapters,
            )
            raise

    @staticmethod
    async def accept_volume_outline(
        novel_id: str, volumes: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """接受分卷大纲，并以持久化稳定 ID 支持 standalone 中断续作。"""
        novel = await novel_repo.get_novel_by_id(novel_id)
        number_of_chapters = int(novel.get("number_of_chapters") or 0)
        try:
            validated = VolumeOutlineResultSchema.model_validate({"volumes": volumes})
        except ValidationError as exc:
            raise ValueError(f"分卷数据非法，未做任何写入：{exc}") from exc
        normalized_volumes = validated.model_dump()["volumes"]
        problems = validate_chapter_ranges(normalized_volumes, number_of_chapters)
        if problems:
            raise ValueError("分卷区间非法，未做任何写入：" + "；".join(problems))

        prepared_volumes = []
        volume_ids = []
        child_ids: Dict[str, str] = {}
        total_chapters = 0
        for order_index, volume in enumerate(normalized_volumes, start=1):
            volume_id = str(ObjectId())
            volume_ids.append(volume_id)
            child_ids[f"volume:{order_index}"] = volume_id
            start = int(volume["chapter_range"]["start"])
            end = int(volume["chapter_range"]["end"])
            chapter_docs = []
            for chapter_order in range(start, end + 1):
                chapter_id = str(ObjectId())
                child_ids[f"chapter:{chapter_order}"] = chapter_id
                chapter_docs.append({
                    "_id": chapter_id,
                    "novel_id": novel_id,
                    "volume_id": volume_id,
                    "title": f"第{chapter_order}章",
                    "order_index": chapter_order,
                    "status": "draft",
                    "summary": "",
                    "content": "",
                    "word_count": 0,
                })
            total_chapters += len(chapter_docs)
            prepared_volumes.append({
                "volume_id": volume_id,
                "volume": {
                    "novel_id": novel_id,
                    "title": volume["title"],
                    "summary": volume.get("summary", ""),
                    "arc": volume.get("arc", ""),
                    "chapter_range": {"start": start, "end": end},
                    "order_index": order_index,
                },
                "chapters": chapter_docs,
            })

        canonical = json.dumps(
            normalized_volumes, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        target_stats = {
            "current_volume_count": int(novel.get("current_volume_count", 0))
            + len(prepared_volumes),
            "current_chapter_count": int(novel.get("current_chapter_count", 0))
            + total_chapters,
            "current_word_count": int(novel.get("current_word_count", 0)),
        }
        result = {
            "volume_count": len(prepared_volumes),
            "chapter_count": total_chapters,
            "volume_ids": volume_ids,
        }
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"accept-volume-outline:{novel_id}:{digest}",
                operation="accept_volume_outline",
                payload={
                    "volumes": prepared_volumes,
                    "novel_stats_after": target_stats,
                    "result": result,
                },
                before_image={"novel": novel},
                child_ids=child_ids,
            ),
            VolumeService._execute_accept_volume_outline,
        )

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
        """更新卷基础信息；重排时把所有位置变化的投影标为 stale。"""
        allowed = {"title", "summary", "status", "order_index", "arc", "chapter_range"}
        prepared = {key: value for key, value in update_data.items() if key in allowed}
        if not prepared:
            return False
        if "title" in prepared and not str(prepared["title"]).strip():
            raise ValueError("Volume title cannot be empty")
        volume = await volume_repo.get_volume_by_id(volume_id)
        novel_id = str(volume["novel_id"])
        stale_from_chapter_id = None
        if "order_index" in prepared:
            new_order = int(prepared["order_index"])
            duplicate = await volume_repo.find_one({
                "novel_id": volume["novel_id"],
                "order_index": new_order,
                "_id": {"$ne": to_object_id(volume_id)},
            })
            if duplicate:
                raise DuplicateKeyError(
                    f"order_index={new_order} 与同一小说下已有卷冲突"
                )
            volumes = await volume_repo.get_volumes_by_novel(novel_id)
            chapters = await BaseRepository(collections.CHAPTERS).find_many(
                {"novel_id": to_object_id(novel_id)}, sort=[("order_index", 1)]
            )
            before = ChapterTimeline(volumes, chapters)
            simulated = [
                {**item, "order_index": new_order}
                if str(item["_id"]) == str(volume_id)
                else item
                for item in volumes
            ]
            after = ChapterTimeline(simulated, chapters)
            changed = [
                position.chapter_id
                for position in after.positions
                if before.position(position.chapter_id).book_ordinal
                != position.book_ordinal
            ]
            if changed:
                stale_from_chapter_id = min(
                    changed, key=lambda item: after.position(item).book_ordinal
                )
        digest = hashlib.sha256(
            json.dumps(prepared, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"update-volume:{volume_id}:{volume.get('updated_at')}:{digest}",
                operation="update_volume",
                payload={
                    "volume_id": volume_id,
                    "update": prepared,
                    "stale_from_chapter_id": stale_from_chapter_id,
                },
                before_image={"volume": volume},
            ),
            VolumeService._execute_update_volume,
        )
        return bool(result["updated"])

    @staticmethod
    async def _execute_update_volume(session, mutation):
        command = mutation.journal["command"]["payload"]
        volume_id = str(command["volume_id"])
        novel_id = str(mutation.journal["novel_id"])
        await volume_repo.update_volume_info(
            volume_id, command["update"], session=session
        )
        await mutation.receipt("volume", {"volume_id": volume_id})
        stale_from = command.get("stale_from_chapter_id")
        if stale_from:
            await mark_from_chapter_stale(
                novel_id, str(stale_from), session=session
            )
            await mutation.receipt("stale", {"chapter_id": str(stale_from)})
        return {"volume_id": volume_id, "updated": True}

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
    async def _execute_soft_delete_volume(session, mutation):
        command = mutation.journal["command"]["payload"]
        volume_id = str(command["volume_id"])
        novel_id = str(mutation.journal["novel_id"])
        chapter_ids = [str(item) for item in command["chapter_ids"]]
        if chapter_ids and not mutation.was_received("stale"):
            await mark_downstream_stale(novel_id, chapter_ids[0], session=session)
            await mutation.receipt("stale", {"chapter_id": chapter_ids[0]})

        obj_id = to_object_id(volume_id)
        stored = await volume_repo.find_one(
            {"_id": obj_id}, include_deleted=True, session=session
        )
        if not stored:
            raise ValueError(f"Volume {volume_id} not found")
        if not stored.get("is_deleted"):
            await volume_repo.soft_delete_volume(volume_id, session=session)
        await mutation.receipt("volume", {"volume_id": volume_id})

        chapters_repo = BaseRepository(collections.CHAPTERS)
        await chapters_repo.update_many(
            {"volume_id": obj_id},
            {
                "is_deleted": True,
                "deleted_at": command["deleted_at"],
                "deleted_with_volume_id": obj_id,
            },
            include_deleted=False,
            session=session,
        )
        await mutation.receipt("chapters", {"chapter_ids": chapter_ids})

        current_novel = await novel_repo.get_novel_by_id(novel_id, session=session)
        target = command["novel_stats_after"]
        deltas = {
            key: int(value) - int(current_novel.get(key, 0))
            for key, value in target.items()
            if int(value) != int(current_novel.get(key, 0))
        }
        if deltas:
            await novel_repo.increment_novel_stats(novel_id, deltas, session=session)
        await mutation.receipt("novel_stats", target)
        return {"volume_id": volume_id, "deleted": True}

    @staticmethod
    async def soft_delete_volume(volume_id: str) -> bool:
        """
        软删除卷 + 级联：
        1. 获取卷信息（用于联动统计扣减）
        2. 软删除该卷自身
        3. 级联软删除该卷下所有章节
        4. 向上联动：novels.current_volume_count - 1，novels.current_word_count 扣减该卷字数
        """
        volume = await volume_repo.get_volume_by_id(volume_id)
        novel_id = str(volume["novel_id"])
        chapters = await BaseRepository(collections.CHAPTERS).find_many(
            {"volume_id": to_object_id(volume_id)}, sort=[("order_index", 1)]
        )
        novel = await novel_repo.get_novel_by_id(novel_id)
        volume_word_count = int(volume.get("word_count", 0))
        target = {
            "current_volume_count": max(
                0, int(novel.get("current_volume_count", 0)) - 1
            ),
            "current_chapter_count": max(
                0, int(novel.get("current_chapter_count", 0)) - len(chapters)
            ),
            "current_word_count": max(
                0, int(novel.get("current_word_count", 0)) - volume_word_count
            ),
        }
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"soft-delete-volume:{volume_id}:{volume.get('updated_at')}",
                operation="soft_delete_volume",
                payload={
                    "volume_id": volume_id,
                    "chapter_ids": [str(chapter["_id"]) for chapter in chapters],
                    "deleted_at": get_utc_now(),
                    "novel_stats_after": target,
                },
                before_image={"volume": volume, "chapters": chapters, "novel": novel},
            ),
            VolumeService._execute_soft_delete_volume,
        )
        return bool(result["deleted"])

    # 恢复（级联） 

    @staticmethod
    async def _execute_restore_volume(session, mutation):
        command = mutation.journal["command"]["payload"]
        volume_id = str(command["volume_id"])
        novel_id = str(mutation.journal["novel_id"])
        obj_id = to_object_id(volume_id)
        stored = await volume_repo.find_one(
            {"_id": obj_id}, include_deleted=True, session=session
        )
        if not stored:
            raise ValueError(f"Volume {volume_id} not found")
        if stored.get("is_deleted"):
            await volume_repo.restore_volume(volume_id, session=session)
        await mutation.receipt("volume", {"volume_id": volume_id})

        chapters_repo = BaseRepository(collections.CHAPTERS)
        await chapters_repo.update_many(
            {"volume_id": obj_id, "deleted_with_volume_id": obj_id},
            {"is_deleted": False, "deleted_at": None, "deleted_with_volume_id": None},
            include_deleted=True,
            session=session,
        )
        await mutation.receipt("chapters", {"chapter_ids": command["chapter_ids"]})

        current_novel = await novel_repo.get_novel_by_id(novel_id, session=session)
        target = command["novel_stats_after"]
        deltas = {
            key: int(value) - int(current_novel.get(key, 0))
            for key, value in target.items()
            if int(value) != int(current_novel.get(key, 0))
        }
        if deltas:
            await novel_repo.increment_novel_stats(novel_id, deltas, session=session)
        await mutation.receipt("novel_stats", target)
        if command["chapter_ids"]:
            await mark_downstream_stale(
                novel_id, str(command["chapter_ids"][0]), session=session
            )
            await mutation.receipt(
                "stale", {"chapter_id": str(command["chapter_ids"][0])}
            )
        return {"volume_id": volume_id, "restored": True}

    @staticmethod
    async def restore_volume(volume_id: str) -> bool:
        """
        恢复已软删除的卷 + 级联：
        1. 恢复卷自身
        2. 级联恢复该卷下所有章节
        3. 向上联动：novels.current_volume_count + 1，回补字数
        """
        obj_id = to_object_id(volume_id)
        volume = await volume_repo.find_one({"_id": obj_id}, include_deleted=True)
        if not volume or not volume.get("is_deleted", False):
            raise ValueError(f"Volume {volume_id} is not in deleted state")
        novel_id = str(volume["novel_id"])
        chapters = await BaseRepository(collections.CHAPTERS).find_many(
            {"volume_id": obj_id, "deleted_with_volume_id": obj_id},
            include_deleted=True,
            sort=[("order_index", 1)],
        )
        novel = await novel_repo.get_novel_by_id(novel_id)
        target = {
            "current_volume_count": int(novel.get("current_volume_count", 0)) + 1,
            "current_chapter_count": int(novel.get("current_chapter_count", 0))
            + len(chapters),
            "current_word_count": int(novel.get("current_word_count", 0))
            + int(volume.get("word_count", 0)),
        }
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"restore-volume:{volume_id}:{volume.get('updated_at')}",
                operation="restore_volume",
                payload={
                    "volume_id": volume_id,
                    "chapter_ids": [str(chapter["_id"]) for chapter in chapters],
                    "novel_stats_after": target,
                },
                before_image={"volume": volume, "chapters": chapters, "novel": novel},
            ),
            VolumeService._execute_restore_volume,
        )
        return bool(result["restored"])

    # 硬删除（级联） 

    @staticmethod
    async def _execute_hard_delete_volume(session, mutation):
        command = mutation.journal["command"]["payload"]
        volume_id = str(command["volume_id"])
        obj_id = to_object_id(volume_id)
        for index, chapter in enumerate(command["chapters"]):
            receipt_key = f"tombstone_{index}"
            if mutation.was_received(receipt_key):
                continue
            await record_chapter_tombstone(chapter, session=session)
            await mutation.receipt(receipt_key, {"chapter_id": str(chapter["_id"])})

        chapters_repo = BaseRepository(collections.CHAPTERS)
        await chapters_repo.hard_delete_many({"volume_id": obj_id}, session=session)
        await mutation.receipt(
            "chapters", {"count": int(command["chapter_count"])}
        )
        stored = await volume_repo.find_one(
            {"_id": obj_id}, include_deleted=True, session=session
        )
        if stored is not None:
            await volume_repo.hard_delete_volume(volume_id, session=session)
        await mutation.receipt("volume", {"volume_id": volume_id})
        return {
            "chapters_deleted": int(command["chapter_count"]),
            "volume_deleted": 1,
        }

    @staticmethod
    async def hard_delete_volume(volume_id: str) -> Dict[str, Any]:
        """
        物理删除卷 + 级联：
        1. 校验该卷已处于软删除状态
        2. 级联物理删除所有关联章节
        3. 物理删除卷自身
        4. 返回删除统计
        """
        obj_id = to_object_id(volume_id)
        volume = await volume_repo.find_one({"_id": obj_id}, include_deleted=True)
        if not volume:
            raise ValueError(f"Volume {volume_id} not found")
        if not volume.get("is_deleted", False):
            raise ValueError("Only soft-deleted volumes can be permanently deleted")
        chapters = await BaseRepository(collections.CHAPTERS).find_many(
            {"volume_id": obj_id}, include_deleted=True
        )
        return await commit_mutation(
            MutationCommand(
                novel_id=str(volume["novel_id"]),
                idempotency_key=f"hard-delete-volume:{volume_id}:{volume.get('updated_at')}",
                operation="hard_delete_volume",
                payload={
                    "volume_id": volume_id,
                    "chapters": chapters,
                    "chapter_count": len(chapters),
                },
                before_image={"volume": volume, "chapters": chapters},
            ),
            VolumeService._execute_hard_delete_volume,
        )
