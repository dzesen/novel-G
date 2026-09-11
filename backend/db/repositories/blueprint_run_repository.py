"""Owner/draft-scoped blueprint checkpoints, fenced leases and paid attempts."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from typing import Any, Callable
from uuid import uuid4

from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.restored_authorization import RESTORED_AUTHORITY_FIELD
from backend.db.errors import NotFoundError
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.models import TokenUsage


class BlueprintRunConflict(ValueError):
    provider_request_not_dispatched = True

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


class BlueprintRunRepository(BaseRepository):
    LEASE_SECONDS = 45

    def __init__(self, *, clock: Callable = get_utc_now) -> None:
        super().__init__(collections.BLUEPRINT_RUNS)
        self.clock = clock

    @staticmethod
    def _id(value: str):
        if not isinstance(value, str) or not value:
            raise BlueprintRunConflict("blueprint_identity_required")
        return to_object_id(value)

    async def get_run(self, run_id: str, owner_id: str) -> dict[str, Any]:
        doc = await self.find_one({"_id": self._id(run_id), "owner_id": self._id(owner_id)})
        if doc is None:
            raise NotFoundError("蓝图运行不存在")
        return doc

    async def list_runs(self, owner_id: str, draft_id: str | None = None, *, limit: int = 20):
        query: dict[str, Any] = {"owner_id": self._id(owner_id), "is_deleted": False}
        if draft_id is not None:
            query["draft_id"] = draft_id
        # History does not fetch prompts, author text, candidates or attempt bodies.
        fields = (
            "draft_id", "status", "revision", "authorization_digest", "current_step", "completed_steps",
            "cancel_requested", "calls_reserved", "tokens_used", "tokens_reserved", "has_uncertain",
            "failure_code", "created_at", "updated_at", "authorization.author_brief.revision",
            "authorization.prompt_revision", "authorization.strategy", "authorization.source_summary",
        )
        return await self.collection.find(query, {field: 1 for field in fields}).sort(
            [("created_at", -1), ("_id", -1)]).limit(max(1, min(50, limit))).to_list(length=None)

    async def create_readiness(self, *, run_id: str, owner_id: str, draft_id: str,
                               authorization: dict, report: dict, candidates: dict | None = None):
        if not draft_id:
            raise BlueprintRunConflict("blueprint_identity_required")
        if authorization.get("owner_id") != owner_id or authorization.get("draft_id") != draft_id or authorization.get("run_id") != run_id:
            raise BlueprintRunConflict("blueprint_authorization_identity_mismatch")
        now = self.clock()
        doc = {
            "_id": self._id(run_id), "owner_id": self._id(owner_id), "draft_id": draft_id,
            "schema_version": "blueprint_run.v1", "status": "ready", "revision": 0,
            "authorization": deepcopy(authorization), "authorization_digest": content_digest(authorization),
            "readiness": deepcopy(report), "authorized_at": None,
            "lease": None, "lease_epoch": 0, "cancel_requested": False,
            "current_step": None, "candidates": deepcopy(candidates or {}), "completed_steps": list(candidates or {}),
            "attempts": {}, "calls_reserved": 0, "tokens_reserved": 0, "tokens_used": 0,
            "step_calls": {}, "has_uncertain": False, "failure_code": None,
            "created_at": now, "updated_at": now,
        }
        await self.insert_one(doc)
        return await self.get_run(run_id, owner_id)

    async def _cas(self, run_id: str, owner_id: str, change: Callable[[dict], dict]):
        for _ in range(24):
            current = await self.get_run(run_id, owner_id)
            if current.get(RESTORED_AUTHORITY_FIELD) is not None:
                raise BlueprintRunConflict("blueprint_new_readiness_required")
            patch = change(current)
            if not patch:
                return current
            if set(patch) & {"authorization", "authorization_digest", "owner_id", "draft_id", "_id", "revision"}:
                raise AssertionError("Blueprint authority is immutable")
            patch["updated_at"] = self.clock()
            result = await self.collection.update_one(
                {"_id": current["_id"], "owner_id": current["owner_id"], "revision": current["revision"],
                 RESTORED_AUTHORITY_FIELD: None},
                {"$set": patch, "$inc": {"revision": 1}},
            )
            if result.modified_count == 1:
                updated = deepcopy(current)
                for path, value in patch.items():
                    parent = updated
                    parts = path.split(".")
                    for part in parts[:-1]:
                        parent = parent.setdefault(part, {})
                    parent[parts[-1]] = value
                updated["revision"] += 1
                return updated
        raise BlueprintRunConflict("blueprint_run_changed")

    def _owned_lease(self, doc: dict, token: str, *, settling: bool = False):
        if content_digest(doc["authorization"]) != doc["authorization_digest"]:
            raise BlueprintRunConflict("blueprint_readiness_stale")
        lease = doc.get("lease") or {}
        if (doc["status"] != "running" or lease.get("token") != token
                or lease.get("expires_at") is None or lease["expires_at"] <= self.clock()
                or (doc.get("cancel_requested") and not settling)):
            raise BlueprintRunConflict("blueprint_lease_lost")

    @staticmethod
    def _unfinished_paid_step(doc: dict) -> bool:
        step = doc.get("current_step")
        return bool(step and any(
            value["step"] == step and value["state"] != "released"
            for value in doc["attempts"].values()
        ))

    @staticmethod
    def _uncertain_patch(doc: dict) -> dict:
        attempts = deepcopy(doc["attempts"])
        used = doc["tokens_used"]
        for attempt in attempts.values():
            if attempt["state"] == "reserved":
                attempt["state"] = "uncertain"
                attempt["usage"] = TokenUsage(total_tokens=attempt["reserved_tokens"]).model_dump()
                used += attempt["reserved_tokens"]
        return {
            "status": "uncertain", "has_uncertain": True, "lease": None,
            "tokens_reserved": 0, "tokens_used": used, "attempts": attempts,
            "failure_code": "blueprint_result_uncertain",
        }

    async def acquire(self, run_id: str, owner_id: str, *, digest: str,
                      acknowledge_automatic_budget: bool = False,
                      acknowledge_uncertain_source: bool = False, resume: bool = False):
        token = uuid4().hex

        def change(doc):
            if digest != doc["authorization_digest"] or content_digest(doc["authorization"]) != digest:
                raise BlueprintRunConflict("blueprint_readiness_stale")
            if doc["status"] == "completed":
                return {}
            if doc["status"] == "running":
                if doc["lease"]["expires_at"] > self.clock():
                    raise BlueprintRunConflict("blueprint_run_already_running")
                if doc["has_uncertain"] or self._unfinished_paid_step(doc) or doc["tokens_reserved"]:
                    return self._uncertain_patch(doc)
            elif doc["status"] not in {"ready", "paused"}:
                raise BlueprintRunConflict("blueprint_new_readiness_required")
            if doc["authorized_at"] is None:
                if resume:
                    raise BlueprintRunConflict("blueprint_authorization_required")
                if doc["readiness"]["status"] == "blocked":
                    raise BlueprintRunConflict("blueprint_token_bound_unproven")
                if doc["readiness"]["uses_system_token_budget"] and not acknowledge_automatic_budget:
                    raise BlueprintRunConflict("automatic_token_budget_confirmation_required")
                if doc["readiness"].get("uncertain_source") and not acknowledge_uncertain_source:
                    raise BlueprintRunConflict("blueprint_uncertain_source_confirmation_required")
            elif not resume:
                raise BlueprintRunConflict("blueprint_resume_required")
            return {
                "status": "running", "authorized_at": doc["authorized_at"] or self.clock(),
                "lease_epoch": doc["lease_epoch"] + 1,
                "lease": {"token": token, "epoch": doc["lease_epoch"] + 1, "expires_at": self.clock() + timedelta(seconds=self.LEASE_SECONDS)},
                "cancel_requested": False, "current_step": None, "failure_code": None,
            }

        doc = await self._cas(run_id, owner_id, change)
        if doc["status"] == "uncertain":
            raise BlueprintRunConflict("blueprint_result_uncertain")
        return doc

    async def heartbeat(self, run_id: str, owner_id: str, token: str):
        def change(doc):
            self._owned_lease(doc, token)
            return {"lease": {**doc["lease"], "expires_at": self.clock() + timedelta(seconds=self.LEASE_SECONDS)}}
        return await self._cas(run_id, owner_id, change)

    async def begin_step(self, run_id: str, owner_id: str, token: str, step: str):
        def change(doc):
            self._owned_lease(doc, token)
            order = doc["authorization"]["step_order"]
            next_step = next((item for item in order if item not in doc["candidates"]), None)
            if step != next_step or doc["current_step"] is not None or doc["has_uncertain"]:
                raise BlueprintRunConflict("blueprint_step_checkpoint_conflict")
            return {"current_step": step, "step_started_at": self.clock()}
        return await self._cas(run_id, owner_id, change)

    async def claim(self, run_id: str, owner_id: str, token: str, *, provider_alias: str,
                    phase: str, conservative_tokens: int | None):
        if type(conservative_tokens) is not int or conservative_tokens <= 0:
            raise BlueprintRunConflict("blueprint_token_bound_unproven")
        attempt_id = uuid4().hex

        def change(doc):
            self._owned_lease(doc, token)
            step, limits = doc["current_step"], doc["authorization"]["limits"]
            if not step or doc["has_uncertain"] or provider_alias not in limits["providers_by_step"].get(step, []):
                raise BlueprintRunConflict("blueprint_provider_not_authorized")
            if (doc["calls_reserved"] >= limits["maximum_provider_attempts"]
                    or doc["step_calls"].get(step, 0) >= limits["max_attempts_by_step"][step]):
                raise BlueprintRunConflict("blueprint_attempt_capacity_exhausted")
            if doc["tokens_used"] + doc["tokens_reserved"] + conservative_tokens > limits["token_budget"]:
                raise BlueprintRunConflict("blueprint_token_budget_exhausted")
            return {
                f"attempts.{attempt_id}": {
                    "step": step, "provider_alias": provider_alias, "phase": phase,
                    "state": "reserved", "reserved_tokens": conservative_tokens,
                    "usage": None, "lease_epoch": doc["lease_epoch"], "claimed_at": self.clock(),
                },
                "calls_reserved": doc["calls_reserved"] + 1,
                "tokens_reserved": doc["tokens_reserved"] + conservative_tokens,
                f"step_calls.{step}": doc["step_calls"].get(step, 0) + 1,
            }
        await self._cas(run_id, owner_id, change)
        return attempt_id

    async def settle(self, run_id: str, owner_id: str, token: str, attempt_id: str,
                     *, usage: TokenUsage | None = None, uncertain: bool = False, released: bool = False):
        def change(doc):
            self._owned_lease(doc, token, settling=True)
            attempt = doc["attempts"].get(attempt_id)
            if not attempt or attempt["lease_epoch"] != doc["lease_epoch"]:
                raise BlueprintRunConflict("blueprint_attempt_not_owned")
            if attempt["state"] != "reserved":
                return {}
            bound = attempt["reserved_tokens"]
            actual = max(0, usage.total_tokens, usage.input_tokens + usage.output_tokens) if usage else 0
            cost = 0 if released else bound if uncertain or actual <= 0 else actual
            receipt = (usage.model_copy() if usage else TokenUsage())
            receipt.total_tokens = cost
            patch = {
                f"attempts.{attempt_id}": {
                    **attempt, "state": "released" if released else "uncertain" if uncertain else "accounted",
                    "usage": receipt.model_dump(), "settled_at": self.clock(),
                },
                "tokens_reserved": doc["tokens_reserved"] - bound,
                "tokens_used": doc["tokens_used"] + cost,
                "has_uncertain": doc["has_uncertain"] or uncertain,
            }
            if released:
                patch.update({"calls_reserved": doc["calls_reserved"] - 1, f"step_calls.{attempt['step']}": doc["step_calls"][attempt["step"]] - 1})
            return patch
        return await self._cas(run_id, owner_id, change)

    async def checkpoint(self, run_id: str, owner_id: str, token: str, step: str, *, value: dict, usage: dict):
        def change(doc):
            self._owned_lease(doc, token)
            digest = content_digest(value)
            existing = doc["candidates"].get(step)
            if existing:
                if existing["digest"] != digest:
                    raise BlueprintRunConflict("blueprint_candidate_immutable")
                return {}
            claims = [item for item in doc["attempts"].values() if item["step"] == step and item["state"] != "released"]
            if doc["current_step"] != step or not claims or doc["has_uncertain"] or any(item["state"] != "accounted" for item in claims):
                raise BlueprintRunConflict("blueprint_attempts_unsettled")
            return {
                f"candidates.{step}": {"value": deepcopy(value), "digest": digest, "usage": deepcopy(usage),
                    "source_run_id": run_id, "authorization_digest": doc["authorization_digest"],
                    "author_brief_revision": doc["authorization"]["author_brief"]["revision"], "completed_at": self.clock()},
                "current_step": None, "completed_steps": [*doc["candidates"], step],
            }
        return await self._cas(run_id, owner_id, change)

    async def finish(self, run_id: str, owner_id: str, token: str, *, success: bool, failure_code: str | None = None):
        def change(doc):
            self._owned_lease(doc, token, settling=True)
            if doc["has_uncertain"] or doc["tokens_reserved"]:
                return self._uncertain_patch(doc)
            if success:
                if doc["current_step"] or set(doc["candidates"]) != set(doc["authorization"]["step_order"]):
                    raise BlueprintRunConflict("blueprint_checkpoints_incomplete")
                return {"status": "completed", "lease": None, "completed_at": self.clock()}
            # Known failure is recorded separately from an unknown Provider result.
            return {"status": "failed", "lease": None, "failure_code": failure_code or "blueprint_generation_failed"}
        return await self._cas(run_id, owner_id, change)

    async def suspend(self, run_id: str, owner_id: str, token: str):
        def change(doc):
            if doc["status"] != "running" or (doc.get("lease") or {}).get("token") != token:
                return {}
            if doc["has_uncertain"] or self._unfinished_paid_step(doc) or doc["tokens_reserved"]:
                return self._uncertain_patch(doc)
            return {"status": "paused", "lease": None, "current_step": None}
        return await self._cas(run_id, owner_id, change)

    async def request_pause(self, run_id: str, owner_id: str):
        def change(doc):
            if doc["status"] != "running":
                return {}
            if (doc.get("lease") or {}).get("expires_at", self.clock()) <= self.clock():
                if doc["has_uncertain"] or self._unfinished_paid_step(doc) or doc["tokens_reserved"]:
                    return self._uncertain_patch(doc)
                return {"status": "paused", "lease": None, "current_step": None}
            return {"cancel_requested": True}
        return await self._cas(run_id, owner_id, change)


blueprint_run_repo = BlueprintRunRepository()
