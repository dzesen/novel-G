"""Task discovery and history use bounded read models, never worker documents."""
from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from backend.db.repositories.generation_job_read_repository import generation_job_read_repo
from backend.services.generation.job_public_views import project_job_summary


logger = logging.getLogger(__name__)


class GenerationJobReadService:
    @staticmethod
    async def summary(job_id: str) -> dict[str, Any]:
        started = perf_counter()
        source = await generation_job_read_repo.get_summary_source(job_id)
        read_ms = (perf_counter() - started) * 1000
        result = project_job_summary(source)
        logger.debug("generation_job_read surface=summary database_ms=%.3f projection_ms=%.3f", read_ms, (perf_counter() - started) * 1000 - read_ms)
        return result

    @staticmethod
    async def current(novel_id: str) -> dict[str, Any] | None:
        source = await generation_job_read_repo.latest_root(novel_id)
        # A terminal latest root closes this surface. Never revive an older run.
        if source is None or source.get("status") in {"completed", "aborted"}:
            return None
        return project_job_summary(source)

    @staticmethod
    async def history(novel_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        page = await generation_job_read_repo.history(novel_id, limit=limit, cursor=cursor)
        return {**page, "items": [project_job_summary(item) for item in page["items"]]}

    @staticmethod
    async def children(job_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        root = await generation_job_read_repo.get_summary_source(job_id)
        parent_id = root.get("required_book_successor_parent_job_id")
        root_id = str(parent_id) if parent_id is not None else str(root["_id"])
        page = await generation_job_read_repo.children(root_id, novel_id=str(root["novel_id"]), limit=limit, cursor=cursor)
        return {**page, "items": [project_job_summary(item) for item in page["items"]]}
