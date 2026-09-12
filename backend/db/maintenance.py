"""Coordinate application database operations with local backup/restore."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from backend.config.config import get_config_value
from backend.runtime_paths import data_path

# Task identity matters: create_task copies ContextVars but must not inherit a held lock.
_ACCESS: ContextVar[tuple | None] = ContextVar("database_access", default=None)
_LOCK_DIRECTORY = data_path("reports", "database-locks")


def database_key(database_name: str | None = None) -> str:
    uri = str(get_config_value("mongodb_url", "mongodb://localhost:27017"))
    name = database_name or str(get_config_value("mongo_database_name", "novel_generator"))
    return hashlib.sha256((uri + "\0" + name).encode("utf-8")).hexdigest()


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def database_access(*, key: str | None = None, write: bool = False, snapshot: bool = False, timeout: float = 30):
    key = key or database_key()
    owner = asyncio.current_task()
    current = _ACCESS.get()
    if current is not None and current[0] == key and current[1] is owner:
        if write and current[2]:
            raise ValueError("数据库快照读取期间不能写入，请稍后重试")
        token = _ACCESS.set((key, owner, current[2] or snapshot))
        try:
            yield
        finally:
            _ACCESS.reset(token)
        return

    _LOCK_DIRECTORY.mkdir(parents=True, exist_ok=True)
    # Empty lock files contain no credentials or manuscript data. Never unlink a live lock file.
    handle = (_LOCK_DIRECTORY / (key + ".lock")).open("a+b", buffering=0)
    acquired = False
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not (acquired := _try_lock(handle)):
            if loop.time() >= deadline:
                raise TimeoutError("数据库备份、恢复或写入尚未完成，请稍后重试")
            await asyncio.sleep(0.01)
        token = _ACCESS.set((key, owner, snapshot))
        try:
            yield
        finally:
            _ACCESS.reset(token)
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()


async def finish_database_io(awaitable):
    """Do not release the lock while a cancelled caller's Mongo request is still in flight."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve any error so abandoned calls do not produce unhandled-task warnings.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


def database_operation(*, write: bool = False, snapshot: bool = False, finish_on_cancel: bool = False):
    def decorate(callback):
        @wraps(callback)
        async def wrapped(*args, **kwargs):
            async def run():
                async with database_access(write=write, snapshot=snapshot):
                    return await callback(*args, **kwargs)
            current = _ACCESS.get()
            if finish_on_cancel and not (current is not None and current[1] is asyncio.current_task()):
                # The child owns its lock and completes restore/rollback even if the HTTP request leaves.
                return await finish_database_io(run())
            return await run()
        return wrapped
    return decorate
