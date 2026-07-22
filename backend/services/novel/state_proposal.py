"""Persisted state-generation leases, proposals, decisions, and acceptance claims.

The module owns the complete trust boundary between a paid model result and a
recoverable narrative mutation.  Callers receive opaque handles; tokens are
verified here and are never copied into mutation journals.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterable, AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument

from backend.config.config import CONFIG_PATH
from backend.config.lifecycle import FileSecretVersionStore
from backend.db import collections
from backend.db.mongo import get_database
from backend.db.mutation import MutationConflictError
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.llm.workflow_runner import parse_sse_event, sse_event
from backend.services.novel.state_validation import validate_state_ids


PROPOSAL_TTL_SECONDS = 15 * 60


class StaleStatePreview(ValueError):
    """A proposal is missing, expired, reused, or bound to stale narrative data."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (ObjectId, datetime)):
        return str(value)
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _proposal_key() -> bytes:
    store = FileSecretVersionStore(
        Path(CONFIG_PATH).with_name(".config-secret-versions.json")
    )
    return store.derive_key("chapter-state-preview")


def _content_digest(chapter: dict[str, Any]) -> str:
    return _digest(
        {
            "chapter_id": str(chapter.get("_id")),
            "content": str(chapter.get("content") or ""),
            "updated_at": chapter.get("updated_at"),
        }
    )


@dataclass(frozen=True)
class StateGenerationSnapshot:
    novel_id: str
    chapter_id: str
    content_digest: str
    narrative_revision: int
    captured_at: datetime


@dataclass(frozen=True)
class StateProposalLease:
    proposal_id: ObjectId
    snapshot: StateGenerationSnapshot


@dataclass(frozen=True)
class SelectAllPolicy:
    """Deterministic headless policy that accepts every selectable candidate."""

    name: str = "select_all"
    version: str = "1"

    def decide(self, proposal: dict[str, Any]) -> tuple[list[str], list[str]]:
        fact_ids = [
            str(fact["selection_id"])
            for character in proposal.get("character_updates") or []
            for fact in character.get("new_permanent_facts") or []
        ]
        thread_ids = [
            str(thread["selection_id"])
            for thread in proposal.get("thread_updates") or []
        ]
        return fact_ids, thread_ids


def add_selection_ids(candidate: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(candidate)
    for character in result.get("character_updates") or []:
        character["selection_id"] = uuid4().hex
        for fact in character.get("new_permanent_facts") or []:
            fact["selection_id"] = uuid4().hex
    for thread in result.get("thread_updates") or []:
        thread["selection_id"] = uuid4().hex
    return result


class StateProposalModule:
    @property
    def collection(self):
        return get_database()[collections.STATE_PREVIEWS]

    async def capture(
        self,
        novel_id: str,
        chapter_id: str,
        *,
        chapter: dict[str, Any] | None = None,
    ) -> StateGenerationSnapshot:
        captured_chapter = chapter or await chapter_repo.get_chapter_by_id(chapter_id)
        if str(captured_chapter.get("novel_id")) != str(novel_id):
            raise ValueError("Chapter does not belong to novel")
        return StateGenerationSnapshot(
            novel_id=str(novel_id),
            chapter_id=str(chapter_id),
            content_digest=_content_digest(captured_chapter),
            narrative_revision=await narrative_revision_store.current(novel_id),
            captured_at=get_utc_now(),
        )

    async def ensure_current(self, snapshot: StateGenerationSnapshot) -> None:
        chapter = await chapter_repo.get_chapter_by_id(snapshot.chapter_id)
        if _content_digest(chapter) != snapshot.content_digest:
            raise StaleStatePreview("Chapter content changed during state generation")
        if (
            await narrative_revision_store.current(snapshot.novel_id)
            != snapshot.narrative_revision
        ):
            raise StaleStatePreview("Narrative state changed during generation")

    @staticmethod
    def _validate_snapshot_identity(
        snapshot: StateGenerationSnapshot, novel_id: str, chapter_id: str
    ) -> None:
        if (
            snapshot.novel_id != str(novel_id)
            or snapshot.chapter_id != str(chapter_id)
        ):
            raise ValueError("Generation snapshot belongs to another chapter")

    async def begin(
        self,
        novel_id: str,
        chapter_id: str,
        *,
        snapshot: StateGenerationSnapshot | None = None,
        chapter: dict[str, Any] | None = None,
        audit: dict[str, Any] | None = None,
    ) -> StateProposalLease:
        """Persist a generation lease before any Provider request is started."""
        active_snapshot = snapshot or await self.capture(
            novel_id, chapter_id, chapter=chapter
        )
        self._validate_snapshot_identity(active_snapshot, novel_id, chapter_id)
        await self.ensure_current(active_snapshot)
        proposal_id = ObjectId()
        now = get_utc_now()
        await self.collection.insert_one(
            {
                "_id": proposal_id,
                "novel_id": to_object_id(novel_id),
                "chapter_id": to_object_id(chapter_id),
                "status": "generating",
                "content_digest": active_snapshot.content_digest,
                "state_revision": active_snapshot.narrative_revision,
                "narrative_revision": active_snapshot.narrative_revision,
                "generation_captured_at": active_snapshot.captured_at,
                "generation_started_at": now,
                "generation_audit": deepcopy(audit or {}),
                "expires_at": now + timedelta(seconds=PROPOSAL_TTL_SECONDS),
                "created_at": now,
                "updated_at": now,
                "is_deleted": False,
            }
        )
        return StateProposalLease(proposal_id=proposal_id, snapshot=active_snapshot)

    async def publish(
        self,
        lease: StateProposalLease,
        candidate: dict[str, Any],
        *,
        audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify a live lease and atomically publish its model candidate."""
        try:
            await self.ensure_current(lease.snapshot)
        except StaleStatePreview as exc:
            await self.collection.update_one(
                {"_id": lease.proposal_id, "status": "generating"},
                {
                    "$set": {
                        "status": "stale",
                        "stale_reason": str(exc),
                        "updated_at": get_utc_now(),
                    }
                },
            )
            raise

        current = await self.collection.find_one({"_id": lease.proposal_id})
        if not current or current.get("status") != "generating":
            raise StaleStatePreview("Generation lease is missing or no longer active")
        prepared = add_selection_ids(candidate)
        expires_at = current.get("expires_at")
        if not isinstance(expires_at, datetime):
            raise StaleStatePreview("Generation lease has no valid expiry")
        candidate_digest = _digest(prepared)
        token_payload = (
            f"{lease.proposal_id}:{lease.snapshot.content_digest}:"
            f"{lease.snapshot.narrative_revision}:{candidate_digest}:"
            f"{int(expires_at.replace(tzinfo=timezone.utc).timestamp())}"
        )
        token = hmac.new(
            _proposal_key(), token_payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        generation_audit = {
            **deepcopy(current.get("generation_audit") or {}),
            **deepcopy(audit or {}),
        }
        published = await self.collection.find_one_and_update(
            {"_id": lease.proposal_id, "status": "generating"},
            {
                "$set": {
                    "status": "proposed",
                    "candidate": prepared,
                    "candidate_digest": candidate_digest,
                    "token_digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                    "generation_audit": generation_audit,
                    "proposed_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if published is None:
            raise StaleStatePreview("Generation lease was published concurrently")
        return {
            **prepared,
            "proposal_id": str(lease.proposal_id),
            "acceptance_token": token,
            "proposal_expires_at": expires_at.isoformat(),
        }

    async def mark_failed(
        self,
        lease: StateProposalLease,
        exc: BaseException,
        *,
        audit: dict[str, Any] | None = None,
    ) -> None:
        current = await self.collection.find_one({"_id": lease.proposal_id})
        generation_audit = {
            **deepcopy((current or {}).get("generation_audit") or {}),
            **deepcopy(audit or {}),
        }
        await self.collection.update_one(
            {"_id": lease.proposal_id, "status": "generating"},
            {
                "$set": {
                    "status": "failed",
                    "failure": {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "generation_audit": generation_audit,
                    "failed_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                }
            },
        )

    async def record_generation_audit(
        self,
        lease: StateProposalLease,
        audit: dict[str, Any],
    ) -> None:
        """Merge late usage/attempt data without changing proposal validity."""
        current = await self.collection.find_one({"_id": lease.proposal_id})
        if current is None:
            return
        generation_audit = {
            **deepcopy(current.get("generation_audit") or {}),
            **deepcopy(audit),
        }
        await self.collection.update_one(
            {
                "_id": lease.proposal_id,
                "status": {"$in": ["generating", "proposed"]},
            },
            {
                "$set": {
                    "generation_audit": generation_audit,
                    "updated_at": get_utc_now(),
                }
            },
        )

    async def stream_preview(
        self,
        lease: StateProposalLease,
        frames: AsyncIterable[str],
        *,
        roster: dict[str, Any],
        state_step: str = "chapter_state",
    ) -> AsyncIterator[str]:
        """Own SSE candidate validation, publication, auditing, and failure state."""
        proposal_payload: dict[str, Any] | None = None
        generation_audit: dict[str, Any] = {}
        reported_invalid_ids = False
        try:
            async for frame in frames:
                parsed = parse_sse_event(frame)
                if parsed is None:
                    yield frame
                    continue
                event, event_data = parsed
                usage = event_data.get("usage") or event_data.get("usage_so_far")
                if isinstance(usage, dict):
                    generation_audit["usage"] = usage
                candidate = None
                if (
                    event == "step"
                    and event_data.get("step") == state_step
                    and event_data.get("status") == "done"
                    and isinstance(event_data.get("data"), dict)
                ):
                    candidate = event_data["data"]
                elif (
                    event == "done"
                    and event_data.get("success")
                    and isinstance(event_data.get("result"), dict)
                    and isinstance(event_data["result"].get(state_step), dict)
                ):
                    candidate = event_data["result"][state_step]
                if candidate is None:
                    yield frame
                    continue

                cleaned, dropped = validate_state_ids(candidate, roster)
                if dropped and not reported_invalid_ids:
                    yield sse_event("id_validation", {"dropped": dropped})
                    reported_invalid_ids = True
                if proposal_payload is None:
                    proposal_payload = await self.publish(
                        lease,
                        cleaned,
                        audit=generation_audit,
                    )
                replacement = dict(event_data)
                if event == "step":
                    replacement["data"] = proposal_payload
                else:
                    result = dict(replacement["result"])
                    result[state_step] = proposal_payload
                    replacement["result"] = result
                yield sse_event(event, replacement)

            if generation_audit:
                await self.record_generation_audit(lease, generation_audit)
            if proposal_payload is None:
                await self.mark_failed(
                    lease,
                    RuntimeError("State workflow ended without a proposal candidate"),
                    audit=generation_audit,
                )
        except BaseException as exc:
            await self.mark_failed(lease, exc, audit=generation_audit)
            raise

    async def create(
        self,
        novel_id: str,
        chapter_id: str,
        candidate: dict[str, Any],
        *,
        snapshot: StateGenerationSnapshot | None = None,
        audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility convenience for already-generated local candidates."""
        lease = await self.begin(
            novel_id, chapter_id, snapshot=snapshot, audit=audit
        )
        return await self.publish(lease, candidate, audit=audit)

    async def _load_verified_proposal(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
    ) -> dict[str, Any]:
        proposal = await self.collection.find_one({"_id": to_object_id(proposal_id)})
        if not proposal:
            raise StaleStatePreview("State proposal is missing")
        if proposal.get("status") not in {"proposed", "claimed", "applied"}:
            raise StaleStatePreview("State proposal is not available for acceptance")
        now = datetime.now(timezone.utc)
        expires_at = proposal.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or expires_at <= now:
            await self.collection.update_one(
                {"_id": proposal["_id"], "status": "proposed"},
                {"$set": {"status": "expired", "updated_at": get_utc_now()}},
            )
            raise StaleStatePreview("State proposal has expired")
        if str(proposal.get("chapter_id")) != str(chapter_id):
            raise StaleStatePreview("State proposal belongs to another chapter")
        expected_token = hashlib.sha256(acceptance_token.encode("ascii")).hexdigest()
        if not hmac.compare_digest(
            expected_token, str(proposal.get("token_digest") or "")
        ):
            raise StaleStatePreview("State proposal token is invalid")
        return proposal

    async def prepare_decision(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
        selected_fact_ids: list[str],
        selected_thread_ids: list[str],
        edits: dict[str, Any] | None = None,
        policy_name: str = "human_review",
        policy_version: str = "1",
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Validate a decision without consuming the proposal or creating a gap."""
        proposal = await self._load_verified_proposal(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
        )
        novel_id = str(proposal["novel_id"])
        if proposal.get("status") == "proposed":
            chapter = await chapter_repo.get_chapter_by_id(chapter_id)
            if _content_digest(chapter) != proposal.get("content_digest"):
                await self.collection.update_one(
                    {"_id": proposal["_id"], "status": "proposed"},
                    {
                        "$set": {
                            "status": "stale",
                            "stale_reason": "Chapter content changed after state generation",
                            "updated_at": get_utc_now(),
                        }
                    },
                )
                raise StaleStatePreview("Chapter content changed after state generation")
            stored_revision = int(
                proposal.get("narrative_revision", proposal.get("state_revision") or 0)
            )
            if await narrative_revision_store.current(novel_id) != stored_revision:
                await self.collection.update_one(
                    {"_id": proposal["_id"], "status": "proposed"},
                    {
                        "$set": {
                            "status": "stale",
                            "stale_reason": "Narrative state changed after state generation",
                            "updated_at": get_utc_now(),
                        }
                    },
                )
                raise StaleStatePreview("Narrative state changed after state generation")

        candidate = deepcopy(proposal["candidate"])
        facts_by_id = {
            fact["selection_id"]: (character, fact)
            for character in candidate.get("character_updates") or []
            for fact in character.get("new_permanent_facts") or []
        }
        threads_by_id = {
            thread["selection_id"]: thread
            for thread in candidate.get("thread_updates") or []
        }
        if not set(selected_fact_ids).issubset(facts_by_id):
            raise StaleStatePreview("Unknown permanent-fact selection id")
        if not set(selected_thread_ids).issubset(threads_by_id):
            raise StaleStatePreview("Unknown plot-thread selection id")

        allowed_edits = deepcopy(edits or {})
        unknown_edits = set(allowed_edits) - {"summary", "current_states"}
        if unknown_edits:
            raise StaleStatePreview(
                f"Unsupported proposal edits: {sorted(unknown_edits)}"
            )
        current_state_edits = allowed_edits.get("current_states") or {}
        payload: dict[str, Any] = {
            "summary": str(
                allowed_edits.get("summary", candidate.get("summary") or "")
            ),
            "character_updates": [],
            "accepted_thread_updates": [],
        }
        for character in candidate.get("character_updates") or []:
            card_id = str(character["card_id"])
            selected = [
                {key: value for key, value in fact.items() if key != "selection_id"}
                for fact in character.get("new_permanent_facts") or []
                if fact["selection_id"] in selected_fact_ids
            ]
            payload["character_updates"].append(
                {
                    "card_id": card_id,
                    "current_state": str(
                        current_state_edits.get(
                            card_id, character.get("current_state") or ""
                        )
                    ),
                    "accepted_permanent_facts": selected,
                }
            )
        payload["accepted_thread_updates"] = [
            {"thread_id": str(thread["thread_id"]), "status": thread["status"]}
            for thread in candidate.get("thread_updates") or []
            if thread["selection_id"] in selected_thread_ids
        ]
        policy = {"name": policy_name, "version": policy_version}
        metadata = {
            "novel_id": novel_id,
            "manual_edits": allowed_edits,
            "proposal_id": proposal_id,
            "candidate_digest": proposal.get("candidate_digest"),
            "decision_policy": policy,
            "evidence": [
                {
                    "selection_id": thread.get("selection_id"),
                    "thread_id": str(thread.get("thread_id")),
                    "evidence": str(thread.get("evidence") or ""),
                    "selected": thread.get("selection_id")
                    in selected_thread_ids,
                }
                for thread in candidate.get("thread_updates") or []
            ],
            "consistency_issues": deepcopy(
                candidate.get("consistency_issues") or []
            ),
            "human_feedback": {
                "selected_fact_count": len(selected_fact_ids),
                "rejected_fact_count": max(
                    0, len(facts_by_id) - len(selected_fact_ids)
                ),
                "selected_thread_count": len(selected_thread_ids),
                "rejected_thread_count": max(
                    0, len(threads_by_id) - len(selected_thread_ids)
                ),
                "edited_fields": sorted(allowed_edits),
            },
            "confidence": (
                "human_reviewed" if policy_name == "human_review" else "auto_accepted"
            ),
        }
        decision_digest = _digest(
            {
                "payload": payload,
                "candidate_digest": proposal.get("candidate_digest"),
                "policy": policy,
            }
        )
        claim = {
            "proposal_id": proposal_id,
            "decision_digest": decision_digest,
            "candidate_digest": proposal.get("candidate_digest"),
            "expected_narrative_revision": int(
                proposal.get("narrative_revision", proposal.get("state_revision") or 0)
            ),
            "policy": policy,
        }
        existing_claim = proposal.get("claim") or {}
        if proposal.get("status") in {"claimed", "applied"} and (
            existing_claim.get("decision_digest") != decision_digest
        ):
            raise MutationConflictError(
                "State proposal is already claimed by a different decision"
            )
        return payload, metadata, claim

    async def accept(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
        selected_fact_ids: list[str],
        selected_thread_ids: list[str],
        edits: dict[str, Any] | None = None,
        policy_name: str = "human_review",
        policy_version: str = "1",
    ) -> dict[str, Any]:
        payload, metadata, claim = await self.prepare_decision(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            selected_fact_ids=selected_fact_ids,
            selected_thread_ids=selected_thread_ids,
            edits=edits,
            policy_name=policy_name,
            policy_version=policy_version,
        )
        # Lazy import keeps the proposal lifecycle independent of timeline writes.
        from backend.services.novel.chapter_state_service import ChapterStateService
        from backend.db.mutation import resume_persisted_mutation

        proposal = await self.collection.find_one(
            {"_id": to_object_id(proposal_id)}
        )
        if proposal and proposal.get("status") in {"claimed", "applied"}:
            journal = await get_database()[collections.MUTATION_JOURNALS].find_one(
                {
                    "novel_id": proposal["novel_id"],
                    "idempotency_key": f"accept-state-proposal:{proposal_id}",
                }
            )
            if journal is None:
                raise MutationConflictError(
                    "Claimed state proposal has no recoverable mutation intent"
                )
            if journal.get("status") == "completed":
                return deepcopy(
                    journal.get("result") or proposal.get("accept_result") or {}
                )
            return await resume_persisted_mutation(
                journal, ChapterStateService._execute_accept_chapter_state
            )

        return await ChapterStateService._accept_proposal_state(
            chapter_id,
            payload,
            acceptance_metadata=metadata,
            proposal_claim=claim,
        )

    async def run_auto(
        self,
        *,
        chapter_id: str,
        proposal: dict[str, Any],
        policy: SelectAllPolicy,
    ) -> dict[str, Any]:
        """Apply a versioned automatic decision through the normal accept path."""
        proposal_id = str(proposal.get("proposal_id") or "")
        acceptance_token = str(proposal.get("acceptance_token") or "")
        if not proposal_id or not acceptance_token:
            raise ValueError("Automatic state acceptance requires a proposal handle")
        selected_fact_ids, selected_thread_ids = policy.decide(proposal)
        return await self.accept(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            selected_fact_ids=selected_fact_ids,
            selected_thread_ids=selected_thread_ids,
            policy_name=policy.name,
            policy_version=policy.version,
        )

    async def claim_for_mutation(
        self,
        claim: dict[str, Any],
        *,
        mutation_revision: int,
        session: Any = None,
    ) -> None:
        proposal_id = to_object_id(str(claim["proposal_id"]))
        expected_revision = int(claim["expected_narrative_revision"])
        decision_digest = str(claim["decision_digest"])
        claim_id = f"accept-state-proposal:{proposal_id}"
        current = await self.collection.find_one({"_id": proposal_id}, session=session)
        if current is None:
            raise MutationConflictError("State proposal disappeared before acceptance")
        existing = current.get("claim") or {}
        if current.get("status") in {"claimed", "applied"}:
            if (
                existing.get("claim_id") == claim_id
                and existing.get("decision_digest") == decision_digest
            ):
                return
            raise MutationConflictError(
                "State proposal is already claimed by a different mutation"
            )
        if current.get("status") != "proposed":
            raise MutationConflictError("State proposal cannot be claimed")
        expires_at = current.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(timezone.utc):
            await self.collection.update_one(
                {"_id": proposal_id, "status": "proposed"},
                {"$set": {"status": "expired", "updated_at": get_utc_now()}},
                session=session,
            )
            raise MutationConflictError("State proposal expired before claim")
        chapter = await chapter_repo.get_chapter_by_id(
            str(current["chapter_id"]), session=session
        )
        if _content_digest(chapter) != current.get("content_digest"):
            await self.collection.update_one(
                {"_id": proposal_id, "status": "proposed"},
                {
                    "$set": {
                        "status": "stale",
                        "stale_reason": "Chapter content changed before claim",
                        "updated_at": get_utc_now(),
                    }
                },
                session=session,
            )
            raise MutationConflictError(
                "Chapter content changed before proposal acceptance"
            )
        if mutation_revision != expected_revision + 1:
            await self.collection.update_one(
                {"_id": proposal_id, "status": "proposed"},
                {
                    "$set": {
                        "status": "stale",
                        "stale_reason": "Narrative revision changed before claim",
                        "updated_at": get_utc_now(),
                    }
                },
                session=session,
            )
            raise MutationConflictError(
                "Narrative state changed before proposal acceptance"
            )
        claimed = await self.collection.find_one_and_update(
            {
                "_id": proposal_id,
                "status": "proposed",
                "narrative_revision": expected_revision,
            },
            {
                "$set": {
                    "status": "claimed",
                    "claim": {
                        "claim_id": claim_id,
                        "decision_digest": decision_digest,
                        "candidate_digest": claim.get("candidate_digest"),
                        "policy": deepcopy(claim.get("policy") or {}),
                        "claimed_at": get_utc_now(),
                    },
                    "used_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if claimed is None:
            latest = await self.collection.find_one({"_id": proposal_id}, session=session)
            latest_claim = (latest or {}).get("claim") or {}
            if (
                (latest or {}).get("status") in {"claimed", "applied"}
                and latest_claim.get("claim_id") == claim_id
                and latest_claim.get("decision_digest") == decision_digest
            ):
                return
            raise MutationConflictError("State proposal was claimed concurrently")

    async def mark_applied(
        self,
        claim: dict[str, Any],
        result: dict[str, Any],
        *,
        session: Any = None,
    ) -> None:
        proposal_id = to_object_id(str(claim["proposal_id"]))
        decision_digest = str(claim["decision_digest"])
        applied = await self.collection.find_one_and_update(
            {
                "_id": proposal_id,
                "status": "claimed",
                "claim.decision_digest": decision_digest,
            },
            {
                "$set": {
                    "status": "applied",
                    "accept_result": deepcopy(result),
                    "applied_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if applied is not None:
            return
        current = await self.collection.find_one({"_id": proposal_id}, session=session)
        current_claim = (current or {}).get("claim") or {}
        if (
            (current or {}).get("status") == "applied"
            and current_claim.get("decision_digest") == decision_digest
        ):
            return
        raise MutationConflictError("State proposal could not be marked applied")


state_proposal_module = StateProposalModule()
