"""Read-only, bounded Mongo projections for the generation task surfaces."""
from __future__ import annotations

import base64
from datetime import datetime
import json
from typing import Any

from backend.db import collections
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.mongo import get_database
from backend.db.utils import to_object_id


def _array(field: str) -> dict[str, Any]:
    return {"$cond": [{"$isArray": f"${field}"}, f"${field}", []]}


def _tail(field: str, count: int = 200) -> dict[str, Any]:
    return {"$slice": [_array(field), -count]}


def _diagnostic_values(field: str) -> dict[str, Any]:
    path = f"$$this.details.{field}"
    return {"$slice": [{"$reduce": {
        "input": _array("diagnostics"), "initialValue": [],
        "in": {"$setUnion": ["$$value", {"$cond": [{"$isArray": path}, path, []]}]},
    }}, 100]}


def _latest_diagnostic() -> dict[str, Any]:
    # Select metadata in Mongo as well as at the HTTP boundary. A future stored
    # exception body must not inflate a summary read before it gets sanitized.
    return {"$map": {"input": _tail("diagnostics", 1), "as": "event", "in": {
        **{field: f"$$event.{field}" for field in (
            "schema_version", "event_id", "fingerprint", "category", "code", "evidence",
            "impact", "action_codes", "source", "step", "chapter_id", "occurred_at",
        )},
        "details": {field: f"$$event.details.{field}" for field in (
            "status", "requested_word_count", "actual_word_count", "raw_character_count",
            "scene_count", "completed_scene_count", "finish_reason", "raw_finish_reason",
            "completion_reason", "mode", "reason_codes", "attempt_count", "provider_aliases",
            "provider_models", "candidate_gate", "repair_cycles_used", "repair_cycles_limit",
            "consistency_issue_count", "dropped_reference_count", "affected_card_ids",
            "outline_issue_categories", "prose_run_id", "prose_run_revision", "repair_component",
            "component_used", "component_limit", "next_step", "exception_family",
            "validation_code", "termination_reason_code",
        )},
    }}}


_SCALARS = (
    "_id", "novel_id", "scope", "volume_id", "status", "pause_reason", "job_kind",
    "required_book_successor_parent_job_id", "checkpoint_interval", "token_budget",
    "tokens_used", "tokens_reserved", "current_chapter_id", "last_checkpoint_index",
    "usage_attempt_capacity", "usage_attempt_claimed", "has_uncertain_attempts",
    "created_at", "updated_at", "outline_deviation_policy",
)
SUMMARY_PROJECTION = {
    **dict.fromkeys(_SCALARS, 1),
    "error.step": 1,
    "error.chapter_id": 1,
    "error.reason_codes": 1,
    "error.blocking_issue_codes": 1,
    "error.audit_digest": 1,
    "required_book_successor_action.stage": 1,
    "required_book_successor_journal.phase": 1,
    "readiness.planning.prose_strategy.provider_alias": 1,
    "readiness.planning.prose_strategy.provider_model": 1,
    "diagnostics": _latest_diagnostic(),
    "diagnostics_count": {"$size": _array("diagnostics")},
    "provider_aliases": _diagnostic_values("provider_aliases"),
    "provider_models": _diagnostic_values("provider_models"),
    "diagnostic_code_values": {"$slice": [{"$setUnion": [{"$map": {
        "input": _array("diagnostics"), "as": "event", "in": "$$event.code",
    }}, []]}, 100]},
    "progress_count": {"$size": _array("progress")},
    "progress_chapter_count": {"$size": {"$setDifference": [{"$setUnion": [{"$map": {
        "input": _array("progress"), "as": "entry", "in": "$$entry.chapter_id",
    }}, []]}, [None, ""]]}},
    "auto_creation_event_count": {"$size": _array("reference_card_auto_creation_events")},
    "repair_event_count": {"$size": _array("reference_card_repair_events")},
    "completion_audit.audit_digest": 1,
    # The relationship reader needs identities, not persisted chapter candidates.
    "candidate_pipeline_checkpoints": {"$map": {
        "input": _tail("candidate_pipeline_checkpoints"), "as": "item",
        "in": {"source": {"source_run_id": "$$item.source.source_run_id"}},
    }},
    "candidate_manual_takeover.source.source_run_id": 1,
    "candidate_manual_takeover_events": {"$map": {
        "input": _tail("candidate_manual_takeover_events"), "as": "item",
        "in": {"takeover": {"source": {"source_run_id": "$$item.takeover.source.source_run_id"}}},
    }},
    "progress": {"$map": {
        "input": _tail("progress"), "as": "item",
        "in": {
            "source": {"source_run_id": "$$item.source.source_run_id"},
            "incomplete_prose": {"source_run_id": "$$item.incomplete_prose.source_run_id"},
            "prose_completion": {"source_run_id": "$$item.prose_completion.source_run_id"},
            "candidate_pipeline_completion": {"source": {
                "source_run_id": "$$item.candidate_pipeline_completion.source.source_run_id",
            }},
        },
    }},
}


def encode_job_cursor(job: dict[str, Any]) -> str:
    value = json.dumps([job["created_at"].isoformat(), str(job["_id"])], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _cursor_filter(cursor: str | None) -> dict[str, Any]:
    if cursor is None:
        return {}
    try:
        if not cursor or len(cursor) > 300:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
            raise ValueError
        created_at = datetime.fromisoformat(value[0])
        object_id = to_object_id(value[1])
    except (ValueError, TypeError, InvalidIdError) as exc:
        raise ValueError("Invalid generation history cursor") from exc
    return {"$or": [
        {"created_at": {"$lt": created_at}},
        {"created_at": created_at, "_id": {"$lt": object_id}},
    ]}


class GenerationJobReadRepository:
    """No generic CRUD or worker entry points are exposed by this reader."""

    async def _query(self, query: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        cursor = await get_database()[collections.GENERATION_JOBS].aggregate([
            {"$match": {**query, "is_deleted": False}},
            {"$sort": {"created_at": -1, "_id": -1}},
            {"$limit": limit},
            {"$project": SUMMARY_PROJECTION},
        ])
        return await cursor.to_list(length=limit)

    async def get_summary_source(self, job_id: str) -> dict[str, Any]:
        if not job_id:
            raise ValueError("Generation job ID is required")
        jobs = await self._query({"_id": to_object_id(job_id)}, limit=1)
        if not jobs:
            raise NotFoundError(f"Generation job not found: {job_id}")
        return jobs[0]

    async def latest_root(self, novel_id: str) -> dict[str, Any] | None:
        if not novel_id:
            raise ValueError("Novel ID is required")
        jobs = await self._query({
            "novel_id": to_object_id(novel_id),
            "scope": {"$in": ["volume", "book"]},
            "required_book_successor_parent_job_id": None,
        }, limit=1)
        return jobs[0] if jobs else None

    async def history(self, novel_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        if not novel_id:
            raise ValueError("Novel ID is required")
        return await self._page({
            "novel_id": to_object_id(novel_id),
            "scope": {"$in": ["volume", "book"]},
            "required_book_successor_parent_job_id": None,
        }, limit=limit, cursor=cursor)

    async def children(self, root_id: str, *, novel_id: str, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        if not root_id or not novel_id:
            raise ValueError("Root job and novel IDs are required")
        return await self._page({
            "novel_id": to_object_id(novel_id),
            "required_book_successor_parent_job_id": to_object_id(root_id),
        }, limit=limit, cursor=cursor)

    async def _page(self, query: dict[str, Any], *, limit: int, cursor: str | None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Generation history page size must be between 1 and 100")
        rows = await self._query({**query, **_cursor_filter(cursor)}, limit=limit + 1)
        return {
            "items": rows[:limit],
            "next_cursor": encode_job_cursor(rows[limit - 1]) if len(rows) > limit else None,
        }


generation_job_read_repo = GenerationJobReadRepository()
