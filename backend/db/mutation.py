"""事务/standalone 共用的可恢复业务 mutation seam。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Generic, TypeVar

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now, to_object_id


T = TypeVar("T")
MutationCallback = Callable[[Any, "MutationRecorder"], Awaitable[T]]
_LOCKS: dict[str, asyncio.Lock] = {}
logger = logging.getLogger(__name__)


class MutationConflictError(RuntimeError):
    """同一幂等键被绑定到内容不同的命令。"""


class UnsupportedMutationError(RuntimeError):
    """命令的 operation/version 没有已注册 handler。"""


def _digest_value(value: Any) -> Any:
    """规范化为 MongoDB 往返后仍稳定的 JSON 形状。"""
    if isinstance(value, dict):
        return {
            str(key): _digest_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_digest_value(item) for item in value]
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        # BSON datetime 只保留毫秒；摘要必须使用相同精度。
        normalized = normalized.replace(
            microsecond=(normalized.microsecond // 1000) * 1000
        )
        return {"$datetime_utc": normalized.isoformat(timespec="milliseconds")}
    return value


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

    def digest(self) -> str:
        """返回不含 novel/idempotency 定位字段的稳定命令摘要。"""
        encoded = json.dumps(
            _digest_value({
                "operation": self.operation,
                "version": self.version,
                "payload": self.payload,
                "before_image": self.before_image,
                "child_ids": self.child_ids,
            }),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RecoveryScope:
    novel_id: str | None = None


@dataclass(frozen=True)
class RecoveryPolicy:
    max_attempts: int = 3
    base_backoff_seconds: int = 30
    max_backoff_seconds: int = 3600

    def backoff_after(self, recovery_attempts: int) -> timedelta:
        seconds = self.base_backoff_seconds * (2 ** max(0, recovery_attempts - 1))
        return timedelta(seconds=min(seconds, self.max_backoff_seconds))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


class MutationEngine:
    """通过 operation/version 选择 handler，并隐藏 journal 提交细节。"""

    def __init__(
        self,
        handlers: dict[tuple[str, int], MutationCallback[Any]],
        *,
        recovery_policy: RecoveryPolicy | None = None,
        now: Callable[[], datetime] = get_utc_now,
    ) -> None:
        self._handlers = dict(handlers)
        self._recovery_policy = recovery_policy or RecoveryPolicy()
        self._now = now

    async def execute(self, command: MutationCommand) -> Any:
        callback = self._handlers.get((command.operation, command.version))
        if callback is None:
            raise UnsupportedMutationError(
                f"Unsupported mutation command: {command.operation}@{command.version}"
            )
        return await _commit_mutation(command, callback)

    async def recover(
        self, scope: RecoveryScope | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """恢复 scope 内的 journal；未知 operation/version 只报告、不执行。"""
        recovered: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        novel_id = scope.novel_id if scope is not None else None
        collection = get_database()[collections.MUTATION_JOURNALS]

        for journal in await list_recoverable_mutations(novel_id):
            journal_id = str(journal["_id"])
            command = MutationCommand.from_journal(journal)
            if (command.operation, command.version) not in self._handlers:
                item = {
                    "journal_id": journal_id,
                    "operation": command.operation,
                    "version": command.version,
                }
                await collection.update_one(
                    {"_id": journal["_id"]},
                    {"$set": {
                        "status": "unsupported",
                        "last_recovery_error": item,
                        "updated_at": self._now(),
                    }},
                )
                unsupported.append(item)
                continue

            next_recovery_at = journal.get("next_recovery_at")
            if (
                isinstance(next_recovery_at, datetime)
                and _as_utc(next_recovery_at) > _as_utc(self._now())
            ):
                deferred.append({
                    "journal_id": journal_id,
                    "operation": command.operation,
                    "version": command.version,
                    "recovery_attempts": int(journal.get("recovery_attempts") or 0),
                    "next_recovery_at": next_recovery_at,
                })
                continue
            try:
                result = await self.execute(command)
                await collection.update_one(
                    {"_id": journal["_id"]},
                    {"$unset": {
                        "last_recovery_error": "",
                        "next_recovery_at": "",
                    }},
                )
                recovered.append({
                    "journal_id": journal_id,
                    "operation": command.operation,
                    "version": command.version,
                    "result": result,
                })
            except MutationConflictError as exc:
                item = {
                    "journal_id": journal_id,
                    "operation": command.operation,
                    "version": command.version,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                await collection.update_one(
                    {"_id": journal["_id"]},
                    {"$set": {
                        "status": "conflict",
                        "last_recovery_error": item,
                        "updated_at": self._now(),
                    }},
                )
                conflicts.append(item)
            except Exception as exc:
                logger.exception(
                    "Mutation recovery failed: journal_id=%s operation=%s version=%s",
                    journal_id,
                    command.operation,
                    command.version,
                )
                recovery_attempts = int(journal.get("recovery_attempts") or 0) + 1
                item = {
                    "journal_id": journal_id,
                    "operation": command.operation,
                    "version": command.version,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "recovery_attempts": recovery_attempts,
                }
                if recovery_attempts >= self._recovery_policy.max_attempts:
                    await collection.update_one(
                        {"_id": journal["_id"]},
                        {
                            "$set": {
                                "status": "quarantined",
                                "recovery_attempts": recovery_attempts,
                                "last_recovery_error": item,
                                "quarantined_at": self._now(),
                                "updated_at": self._now(),
                            },
                            "$unset": {"next_recovery_at": ""},
                        },
                    )
                    quarantined.append(item)
                    continue
                next_attempt = self._now() + self._recovery_policy.backoff_after(
                    recovery_attempts
                )
                await collection.update_one(
                    {"_id": journal["_id"]},
                    {"$set": {
                        "status": "failed",
                        "recovery_attempts": recovery_attempts,
                        "last_recovery_error": item,
                        "next_recovery_at": next_attempt,
                        "updated_at": self._now(),
                    }},
                )
                failed.append(item)

        return {
            "recovered": recovered,
            "failed": failed,
            "unsupported": unsupported,
            "deferred": deferred,
            "quarantined": quarantined,
            "conflicts": conflicts,
        }


async def _commit_mutation(command: MutationCommand, callback: MutationCallback[T]) -> T:
    """提交完整命令；standalone 崩溃后以稳定子 ID 和逐项回执安全重放。"""
    lock = _LOCKS.setdefault(command.idempotency_key, asyncio.Lock())
    async with lock:
        collection = get_database()[collections.MUTATION_JOURNALS]
        command_digest = command.digest()

        async def execute(session):
            now = get_utc_now()
            await collection.update_one(
                {
                    "novel_id": to_object_id(command.novel_id),
                    "idempotency_key": command.idempotency_key,
                },
                {"$setOnInsert": {
                    "operation": command.operation,
                    "command_digest": command_digest,
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
            stored_digest = str(journal.get("command_digest") or "")
            if not stored_digest:
                stored_digest = MutationCommand.from_journal(journal).digest()
                await collection.update_one(
                    {"_id": journal["_id"], "command_digest": {"$exists": False}},
                    {"$set": {"command_digest": stored_digest}},
                    session=session,
                )
            if stored_digest != command_digest:
                raise MutationConflictError(
                    "The idempotency key is already bound to a different command"
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
        except MutationConflictError:
            # 冲突属于调用者错误，不能把已存在的正确 journal 改成 failed。
            raise
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
                        "command_digest": command_digest,
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


async def commit_mutation(command: MutationCommand, callback: MutationCallback[T]) -> T:
    """旧调用者兼容入口；执行仍经过 MutationEngine 的 operation/version seam。"""
    engine = MutationEngine({(command.operation, command.version): callback})
    return await engine.execute(command)


async def list_recoverable_mutations(novel_id: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {
        "status": {"$in": ["intent", "running", "failed", "unsupported"]}
    }
    if novel_id:
        query["novel_id"] = to_object_id(novel_id)
    cursor = get_database()[collections.MUTATION_JOURNALS].find(query).sort("updated_at", 1)
    return await cursor.to_list(length=None)


async def resume_persisted_mutation(
    journal: dict[str, Any], callback: MutationCallback[T]
) -> T:
    """使用持久化 command/child IDs/receipts 恢复一个未完成 mutation。"""
    return await commit_mutation(MutationCommand.from_journal(journal), callback)
