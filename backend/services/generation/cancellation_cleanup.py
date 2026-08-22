"""Cancellation cleanup that never leaves an unobserved child task behind."""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar


_ResultT = TypeVar("_ResultT")


async def drain_cancellation_cleanup(cleanup: Awaitable[_ResultT]) -> _ResultT:
    """Finish one cancellation checkpoint despite repeated owner cancellation.

    ``asyncio.shield`` alone lets the owner stop awaiting while the shielded
    child continues. This helper keeps ownership of that child until it has
    either completed or raised, so its failure is always observed before a
    caller releases the execution lease.
    """
    task = asyncio.ensure_future(cleanup)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            continue
    return task.result()
