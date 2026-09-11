"""Close owned async streams even inside a cancelled HTTP task group."""

from contextlib import asynccontextmanager
import logging

import anyio


@asynccontextmanager
async def closing_stream(stream):
    """Propagate consumer closure through wrappers with bounded cleanup."""
    try:
        yield stream
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            with anyio.move_on_after(5, shield=True) as cleanup:
                await close()
            if cleanup.cancel_called:
                logging.getLogger(__name__).warning("Owned stream cleanup timed out")
