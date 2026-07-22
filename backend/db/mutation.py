"""事务/standalone 共用的可恢复业务 mutation seam。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, TypeVar

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now, to_object_id


T = TypeVar("T")
MutationCallback = Callable[[Any, "MutationRecorder"], Awaitable[T]]
_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class MutationCommand:
    novel_id: str
    idempotency_key: str
    operation: str
    payload: dict[str, Any]
    before_image: dict[str, Any] | None = None
    child_ids: dict[str, str] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def from_journal(cls, journal: dict[str, Any]) -> "MutationCommand":
        """从持久化 intent 重建命令，供进程重启后的确定性恢复使用。"""
        stored = journal.get("command") or {}
        return cls(
            novel_id=str(journal["novel_id"]),
            idempotency_key=str(journal["idempotency_key"]),
            operation=str(journal["operation"]),
            payload=deepcopy(stored.get("payload") or {}),
            before_image=deepcopy(stored.get("before_image")),
            child_ids={
                str(key): str(value)
                for key, value in (stored.get("child_ids") or {}).items()
            },
            version=int(stored.get("version") or 1),
        )


class MutationRecorder:
    def __init__(self, journal: dict[str, Any], session: Any) -> None:
        self.journal = journal
        self.session = session

    def child_id(self, key: str) -> str:
        return str((self.journal.get("command") or {}).get("child_ids", {})[key])

    def was_received(self, key: str) -> bool:
        return key in (self.journal.get("receipts") or {})

    async def receipt(self, key: str, value: Any) -> None:
        if self.was_received(key):
            return
        await get_database()[collections.MUTATION_JOURNALS].update_one(
            {"_id": self.journal["_id"]},
            {"$set": {f"receipts.{key}": deepcopy(value), "updated_at": get_utc_now()}},
            session=self.session,
        )
        self.journal.setdefault("receipts", {})[key] = deepcopy(value)


async def commit_mutation(command: MutationCommand, callback: MutationCallback[T]) -> T:
    """提交完整命令；standalone 崩溃后以稳定子 ID 和逐项回执安全重放。"""
    lock = _LOCKS.setdefault(command.idempotency_key, asyncio.Lock())
    async with lock:
        collection = get_database()[collections.MUTATION_JOURNALS]

        async def execute(session):
            now = get_utc_now()
            await collection.update_one(
                {
                    "novel_id": to_object_id(command.novel_id),
                    "idempotency_key": command.idempotency_key,
                },
                {"$setOnInsert": {
                    "operation": command.operation,
                    "command": {
                        "version": command.version,
                        "payload": deepcopy(command.payload),
                        "before_image": deepcopy(command.before_image),
                        "child_ids": deepcopy(command.child_ids),
                    },
                    "receipts": {},
                    "status": "intent",
                    "created_at": now,
                    "updated_at": now,
                    "is_deleted": False,
                }},
                upsert=True,
                session=session,
            )
            journal = await collection.find_one(
                {
                    "novel_id": to_object_id(command.novel_id),
                    "idempotency_key": command.idempotency_key,
                },
                session=session,
            )
            if journal.get("status") == "completed":
                return deepcopy(journal.get("result"))
            await collection.update_one(
                {"_id": journal["_id"]},
                {"$set": {"status": "running", "updated_at": get_utc_now()}},
                session=session,
            )
            result = await callback(session, MutationRecorder(journal, session))
            await collection.update_one(
                {"_id": journal["_id"]},
                {"$set": {
                    "status": "completed",
                    "result": deepcopy(result),
                    "updated_at": get_utc_now(),
                }},
                session=session,
            )
            return result

        try:
            return await run_mongo_write_unit(execute, command.operation)
        except BaseException as exc:
            # 事务模式下 intent 可能随事务回滚而不存在；upsert 一条安全失败记录。
            failure_write = collection.update_one(
                    {
                        "novel_id": to_object_id(command.novel_id),
                        "idempotency_key": command.idempotency_key,
                    },
                    {"$set": {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "updated_at": get_utc_now(),
                    }, "$setOnInsert": {
                        "operation": command.operation,
                        "command": {
                            "version": command.version,
                            "payload": deepcopy(command.payload),
                            "before_image": deepcopy(command.before_image),
                            "child_ids": deepcopy(command.child_ids),
                        },
                        "receipts": {},
                        "created_at": get_utc_now(),
                        "is_deleted": False,
                    }},
                    upsert=True,
                )
            if isinstance(exc, asyncio.CancelledError):
                # 清理写必须完成，但原取消仍按原样向上传播。
                await asyncio.shield(failure_write)
            else:
                await failure_write
            raise


async def list_recoverable_mutations(novel_id: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"status": {"$in": ["intent", "running", "failed"]}}
    if novel_id:
        query["novel_id"] = to_object_id(novel_id)
    cursor = get_database()[collections.MUTATION_JOURNALS].find(query).sort("updated_at", 1)
    return await cursor.to_list(length=None)


async def resume_persisted_mutation(
    journal: dict[str, Any], callback: MutationCallback[T]
) -> T:
    """使用持久化 command/child IDs/receipts 恢复一个未完成 mutation。"""
    return await commit_mutation(MutationCommand.from_journal(journal), callback)
