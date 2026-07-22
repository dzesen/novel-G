"""伏笔跨集合审计（读 chapters + plot_threads）。

审计要同时读 chapters 与 plot_threads，不塞进只认单集合的伏笔仓储。
孤儿布尔判定（是否被任一章节引用）对 order_index 是否全书唯一免疫；
referenced_by_chapter_orders 里的章号仅作展示（已知 order_index 非全书唯一，
跨卷同号时展示略有歧义，见设计 §9，不影响是否孤儿的判断）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from bson import ObjectId

from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.services.novel.chapter_timeline import ChapterTimeline
from backend.services.novel.state_timeline import (
    record_manual_correction,
    record_plot_thread_event,
)


class PlotThreadService:
    """把伏笔主文档与时间线事件放入同一个可恢复写单元。"""

    @staticmethod
    async def _execute_create(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        thread_id = mutation.child_id("thread")
        if not mutation.was_received("thread"):
            await plot_thread_repo.create_thread(
                novel_id,
                {**command["data"], "_id": thread_id},
                session=session,
            )
            effective = command.get("effective_chapter_id")
            if effective:
                await record_plot_thread_event(
                    novel_id,
                    str(effective),
                    thread_id,
                    "planted",
                    {"source": str(command["data"].get("source") or "manual")},
                    idempotency_key=f"manual:{thread_id}:planted",
                    session=session,
                )
            await mutation.receipt("thread", {"thread_id": thread_id})
        return {"thread_id": thread_id}

    @staticmethod
    async def create_thread(
        novel_id: str,
        data: Dict[str, Any],
        *,
        effective_chapter_id: str | None,
    ) -> str:
        thread_id = str(ObjectId())
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"create-plot-thread:{thread_id}",
                operation="create_plot_thread",
                payload={
                    "data": dict(data),
                    "effective_chapter_id": effective_chapter_id,
                },
                child_ids={"thread": thread_id},
            ),
            PlotThreadService._execute_create,
        )
        return str(result["thread_id"])

    @staticmethod
    async def _execute_update(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        thread_id = str(command["thread_id"])
        if not mutation.was_received("update"):
            await plot_thread_repo.update_thread(
                novel_id, thread_id, command["data"], session=session
            )
            effective = command.get("effective_chapter_id")
            if effective:
                await record_manual_correction(
                    novel_id,
                    str(effective),
                    "plot_thread",
                    thread_id,
                    command["data"],
                    session=session,
                )
            await mutation.receipt("update", {"thread_id": thread_id})
        return {"thread_id": thread_id, "updated": True}

    @staticmethod
    async def update_thread(
        novel_id: str,
        thread_id: str,
        data: Dict[str, Any],
        *,
        effective_chapter_id: str | None,
    ) -> bool:
        current = await plot_thread_repo.get_thread(novel_id, thread_id)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "data": data,
                    "effective_chapter_id": effective_chapter_id,
                    "before": current,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"update-plot-thread:{thread_id}:{digest}",
                operation="update_plot_thread",
                payload={
                    "thread_id": thread_id,
                    "data": dict(data),
                    "effective_chapter_id": effective_chapter_id,
                },
                before_image=current,
            ),
            PlotThreadService._execute_update,
        )
        return bool(result["updated"])

    @staticmethod
    async def _execute_soft_delete(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        thread_id = str(command["thread_id"])
        if not mutation.was_received("delete"):
            # 第一次执行可能已删成功但在事件落库前崩溃；软删返回 False
            # 不能阻止恢复继续补齐时间线事件。
            stored = await plot_thread_repo.find_one(
                {"_id": ObjectId(thread_id), "novel_id": ObjectId(novel_id)},
                include_deleted=True,
                session=session,
            )
            if not stored:
                raise ValueError(f"Plot thread '{thread_id}' no longer exists")
            if not stored.get("is_deleted"):
                await plot_thread_repo.soft_delete_thread(
                    novel_id, thread_id, session=session
                )
            effective = command.get("effective_chapter_id")
            if effective:
                await record_plot_thread_event(
                    novel_id,
                    str(effective),
                    thread_id,
                    "soft_deleted",
                    {},
                    idempotency_key=f"manual:{thread_id}:soft_deleted:{effective}",
                    session=session,
                )
            await mutation.receipt("delete", {"thread_id": thread_id})
        return {"thread_id": thread_id, "deleted": True}

    @staticmethod
    async def soft_delete_thread(
        novel_id: str,
        thread_id: str,
        *,
        effective_chapter_id: str | None,
    ) -> bool:
        current = await plot_thread_repo.get_thread(novel_id, thread_id)
        digest = hashlib.sha256(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "effective_chapter_id": effective_chapter_id,
                    "before": current,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"soft-delete-plot-thread:{thread_id}:{digest}",
                operation="soft_delete_plot_thread",
                payload={
                    "thread_id": thread_id,
                    "effective_chapter_id": effective_chapter_id,
                },
                before_image=current,
            ),
            PlotThreadService._execute_soft_delete,
        )
        return bool(result["deleted"])


async def audit_thread_references(novel_id: str) -> Dict[str, List[int]]:
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    refs: Dict[str, List[int]] = {}
    for chapter in chapters:
        outline = chapter.get("outline") or {}
        order = chapter.get("order_index")
        thread_ids = list(outline.get("threads_planted") or []) + list(
            outline.get("threads_resolved") or []
        )
        for tid in thread_ids:
            key = str(tid)
            bucket = refs.setdefault(key, [])
            if order is not None and order not in bucket:
                bucket.append(order)
    for key in refs:
        refs[key].sort()
    return refs


async def audit_thread_reference_chapters(novel_id: str) -> Dict[str, List[dict]]:
    """返回无歧义的章节 ID、全书序号和卷章标签。"""
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, chapters)
    refs: Dict[str, List[dict]] = {}
    for chapter in chapters:
        outline = chapter.get("outline") or {}
        position = timeline.position(str(chapter["_id"]))
        for thread_id in list(outline.get("threads_planted") or []) + list(
            outline.get("threads_resolved") or []
        ):
            item = {
                "chapter_id": position.chapter_id,
                "book_ordinal": position.book_ordinal,
                "label": position.label,
            }
            bucket = refs.setdefault(str(thread_id), [])
            if item not in bucket:
                bucket.append(item)
    for bucket in refs.values():
        bucket.sort(key=lambda item: item["book_ordinal"])
    return refs
