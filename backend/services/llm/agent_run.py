"""Persisted Agent tool runs and attempt-level usage audit records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pymongo import ReturnDocument

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id
from backend.services.llm.agent_context import AgentContextBundle


def _attempt_view(attempt: Any) -> dict[str, Any]:
    usage = getattr(attempt, "usage", None)
    return {
        "attempt_id": str(getattr(attempt, "attempt_id", "")),
        "provider_alias": str(getattr(attempt, "provider_alias", "")),
        "phase": str(getattr(attempt, "phase", "")),
        "state": str(getattr(attempt, "state", "accounted")),
        "usage": (
            usage.model_dump()
            if hasattr(usage, "model_dump")
            else deepcopy(usage or {})
        ),
    }


def _public_view(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(document["_id"]),
        "novel_id": str(document["novel_id"]),
        "actor_id": str(document["actor_id"]),
        "capability": str(document.get("capability") or ""),
        "status": str(document.get("status") or ""),
        "agent_id": str(document.get("agent_id") or ""),
        "agent_version": int(document.get("agent_version") or 1),
        "provider_alias": document.get("provider_alias"),
        "structured_output": document.get("structured_output"),
        "request": deepcopy(document.get("request") or {}),
        "context_snapshot": deepcopy(document.get("context_snapshot") or {}),
        "context_report": deepcopy(document.get("context_report") or {}),
        "result": deepcopy(document.get("result")),
        "usage": deepcopy(document.get("usage") or {}),
        "attempts": deepcopy(document.get("attempts") or []),
        "error": deepcopy(document.get("error")),
        "created_at": document.get("created_at"),
        "completed_at": document.get("completed_at"),
        "updated_at": document.get("updated_at"),
    }


class AgentRunStore:
    @property
    def collection(self):
        return get_database()[collections.AGENT_RUNS]

    async def begin(
        self,
        *,
        actor_id: str,
        novel_id: str,
        capability: str,
        agent_id: str,
        agent_version: int,
        request: dict[str, Any],
        context: AgentContextBundle,
    ) -> str:
        now = get_utc_now()
        result = await self.collection.insert_one(
            {
                "novel_id": to_object_id(novel_id),
                "actor_id": to_object_id(actor_id),
                "capability": capability,
                "status": "running",
                "agent_id": agent_id,
                "agent_version": int(agent_version),
                "request": deepcopy(request),
                "context_snapshot": context.snapshot(),
                "context_report": {
                    "coverage": context.coverage,
                    "truncated_sections": list(context.truncated_sections),
                },
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
                "attempts": [],
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "is_deleted": False,
                "deleted_at": None,
            }
        )
        return str(result.inserted_id)

    async def complete(
        self,
        run_id: str,
        *,
        generated: Any,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        now = get_utc_now()
        attempts = [_attempt_view(item) for item in generated.attempts]
        update = {
            "status": "completed",
            "provider_alias": generated.plan.provider_alias,
            "structured_output": str(
                getattr(generated.plan.mode, "value", generated.plan.mode)
            ),
            "result": deepcopy(result),
            "usage": generated.usage.model_dump(),
            "attempts": attempts,
            "completed_at": now,
            "updated_at": now,
        }
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(run_id),
                "status": "running",
                "is_deleted": False,
            },
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise NotFoundError(f"Agent run '{run_id}' is no longer active")
        return _public_view(document)

    async def fail(
        self,
        run_id: str,
        *,
        error: BaseException,
        runtime: Any | None = None,
        stale: bool = False,
    ) -> None:
        attempts = [
            _attempt_view(item)
            for item in tuple(getattr(runtime, "attempts", ()))
        ]
        usage = {
            "input_tokens": sum(
                int((item.get("usage") or {}).get("input_tokens") or 0)
                for item in attempts
            ),
            "output_tokens": sum(
                int((item.get("usage") or {}).get("output_tokens") or 0)
                for item in attempts
            ),
            "total_tokens": sum(
                int((item.get("usage") or {}).get("total_tokens") or 0)
                for item in attempts
            ),
        }
        now = get_utc_now()
        await self.collection.update_one(
            {
                "_id": to_object_id(run_id),
                "status": "running",
                "is_deleted": False,
            },
            {
                "$set": {
                    "status": "stale" if stale else "failed",
                    "attempts": attempts,
                    "usage": usage,
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error)[:1000],
                    },
                    "completed_at": now,
                    "updated_at": now,
                }
            },
        )

    async def get_owned(
        self,
        *,
        actor_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        document = await self.collection.find_one(
            {
                "_id": to_object_id(run_id),
                "actor_id": to_object_id(actor_id),
                "is_deleted": False,
            }
        )
        if document is None:
            raise NotFoundError(f"Agent run '{run_id}' was not found")
        return document

    async def list_owned(
        self,
        *,
        actor_id: str,
        novel_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        cursor = self.collection.find(
            {
                "actor_id": to_object_id(actor_id),
                "novel_id": to_object_id(novel_id),
                "is_deleted": False,
            }
        ).sort("created_at", -1).limit(max(1, min(int(limit), 100)))
        return [_public_view(item) for item in await cursor.to_list(length=None)]

    @staticmethod
    def public_view(document: dict[str, Any]) -> dict[str, Any]:
        return _public_view(document)


agent_run_store = AgentRunStore()
