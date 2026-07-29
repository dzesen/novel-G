"""Small ASGI request-body guards for raw binary upload endpoints."""

from __future__ import annotations

from starlette.requests import Request


class RequestBodyTooLarge(ValueError):
    """A request body crossed its endpoint-specific byte boundary."""

    def __init__(self, *, current_bytes: int, max_bytes: int) -> None:
        self.current_bytes = current_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"request body exceeds {max_bytes} bytes "
            f"(observed at least {current_bytes} bytes)"
        )


async def read_bounded_body(request: Request, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` and stop consuming ASGI frames on overflow."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError as exc:
            raise ValueError("Content-Length must be a non-negative integer") from exc
        if declared_bytes < 0:
            raise ValueError("Content-Length must be a non-negative integer")
        if declared_bytes > max_bytes:
            raise RequestBodyTooLarge(
                current_bytes=declared_bytes,
                max_bytes=max_bytes,
            )

    chunks: list[bytes] = []
    current_bytes = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        current_bytes += len(chunk)
        if current_bytes > max_bytes:
            raise RequestBodyTooLarge(
                current_bytes=current_bytes,
                max_bytes=max_bytes,
            )
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["RequestBodyTooLarge", "read_bounded_body"]
