from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from bson import ObjectId

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.mutation import MutationCommand, MutationConflictError, commit_mutation
from backend.db.utils import get_utc_now, to_object_id
from backend.services.novel.reference_card_curation import (
    merge_reference_card_data,
    normalize_card_name,
    prepare_persisted_reference_card_candidates,
    validate_reference_card_candidate,
)
from backend.services.novel.reference_card_service import (
    get_card_repository,
    validate_card_type,
)
from backend.db.repositories.novel_repository import novel_repo


REVIEWABLE_STATUSES = frozenset({"pending", "deferred"})
EDITABLE_FIELDS = frozenset(
    {
        "name",
        "subtitle",
        "description",
        "details",
        "tags",
        "importance",
        "character_profile",
    }
)
DECISION_ACTIONS = frozenset(
    {"create", "merge", "restore_merge", "defer", "ignore"}
)


class CandidateReviewError(ValueError):
    pass


class StaleCandidateReview(CandidateReviewError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return normalized.isoformat()
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _candidate_parts(raw: Mapping[str, Any]) -> tuple[str, bool, str, dict[str, Any]]:
    value = deepcopy(dict(raw))
    card_type = validate_card_type(str(value.pop("card_type", "")))
    requires_review = bool(
        value.pop("requires_review_before_next_chapter", False)
    )
    evidence_summary = str(value.pop("evidence_summary", "") or "").strip()
    candidate_data = validate_reference_card_candidate(card_type, value)
    return card_type, requires_review, evidence_summary, candidate_data


def _queue_input(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(document["_id"]),
        "reserved_card_id": str(document["reserved_card_id"]),
        "card_type": str(document["card_type"]),
        "candidate_data": deepcopy(document.get("candidate_data") or {}),
        "status": str(document.get("status") or "pending"),
        "requires_review_before_next_chapter": bool(
            document.get("requires_review_before_next_chapter")
        ),
        "evidence": deepcopy(document.get("evidence") or {}),
        "chapter_id": str(document.get("chapter_id") or ""),
    }


class EmergentReferenceCardCandidateModule:
    @property
    def collection(self):
        return get_database()[collections.EMERGENT_REFERENCE_CARD_CANDIDATES]

    async def register_from_outline(
        self,
        *,
        session: Any,
        mutation: Any,
        novel_id: str,
        chapter: Mapping[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[str]:
        source_mutation_id = str(mutation.journal["idempotency_key"])
        candidate_ids = [str(item["candidate_id"]) for item in candidates]
        supersede_key = "reference_card_candidates_superseded"
        if not mutation.was_received(supersede_key):
            query: dict[str, Any] = {
                "novel_id": to_object_id(novel_id),
                "chapter_id": to_object_id(str(chapter["_id"])),
                "status": {"$in": sorted(REVIEWABLE_STATUSES)},
                "source_mutation_id": {"$ne": source_mutation_id},
            }
            if candidate_ids:
                query["_id"] = {
                    "$nin": [to_object_id(item) for item in candidate_ids]
                }
            result = await self.collection.update_many(
                query,
                {
                    "$set": {
                        "status": "superseded",
                        "superseded_by_source_mutation_id": source_mutation_id,
                        "updated_at": get_utc_now(),
                    }
                },
                session=session,
            )
            await mutation.receipt(
                supersede_key,
                {"count": int(result.modified_count)},
            )

        created_ids: list[str] = []
        now = get_utc_now()
        for index, item in enumerate(candidates):
            receipt_key = f"reference_card_candidate_{index}"
            candidate_id = str(item["candidate_id"])
            created_ids.append(candidate_id)
            if mutation.was_received(receipt_key):
                continue
            card_type, requires_review, evidence_summary, candidate_data = (
                _candidate_parts(item["candidate"])
            )
            document = {
                "_id": to_object_id(candidate_id),
                "novel_id": to_object_id(novel_id),
                "volume_id": to_object_id(str(chapter["volume_id"])),
                "chapter_id": to_object_id(str(chapter["_id"])),
                "chapter_order": int(chapter.get("order_index") or 0),
                "chapter_title": str(chapter.get("title") or ""),
                "source_kind": "chapter_outline",
                "source_mutation_id": source_mutation_id,
                "card_type": card_type,
                "normalized_name": normalize_card_name(
                    str(candidate_data.get("name") or "")
                ),
                "candidate_data": candidate_data,
                "requires_review_before_next_chapter": requires_review,
                "evidence": {
                    "summary": evidence_summary,
                    "chapter_id": str(chapter["_id"]),
                    "chapter_order": int(chapter.get("order_index") or 0),
                    "chapter_title": str(chapter.get("title") or ""),
                    "source_kind": "chapter_outline",
                },
                "reserved_card_id": to_object_id(
                    str(item["reserved_card_id"])
                ),
                "status": "pending",
                "decision": None,
                "resolved_card_id": None,
                "created_at": now,
                "updated_at": now,
                "is_deleted": False,
            }
            await self.collection.update_one(
                {"_id": document["_id"]},
                {"$setOnInsert": document},
                upsert=True,
                session=session,
            )
            await mutation.receipt(
                receipt_key,
                {"candidate_id": candidate_id},
            )
        return created_ids

    async def _load_documents(
        self,
        novel_id: str,
        *,
        candidate_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"novel_id": to_object_id(novel_id)}
        if candidate_ids is None:
            query["status"] = {"$in": sorted(REVIEWABLE_STATUSES)}
        else:
            query["_id"] = {
                "$in": [to_object_id(candidate_id) for candidate_id in candidate_ids]
            }
        cursor = self.collection.find(query).sort(
            [("chapter_order", 1), ("created_at", 1), ("_id", 1)]
        )
        return await cursor.to_list(length=100)

    async def inspect(
        self,
        novel_id: str,
        *,
        candidate_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        await novel_repo.get_novel_by_id(novel_id)
        documents = await self._load_documents(
            novel_id,
            candidate_ids=candidate_ids,
        )
        prepared = await prepare_persisted_reference_card_candidates(
            novel_id,
            [_queue_input(document) for document in documents],
        )
        counts = {
            "pending": 0,
            "deferred": 0,
            "ignored": 0,
            "resolved": 0,
            "superseded": 0,
            "blocking": 0,
        }
        cursor = self.collection.find(
            {"novel_id": to_object_id(novel_id)},
            {
                "status": 1,
                "requires_review_before_next_chapter": 1,
            },
        )
        async for item in cursor:
            status = str(item.get("status") or "")
            if status in counts:
                counts[status] += 1
            if (
                status in REVIEWABLE_STATUSES
                and item.get("requires_review_before_next_chapter")
            ):
                counts["blocking"] += 1
        review_digest = _digest(
            {
                "novel_id": novel_id,
                "candidates": prepared,
            }
        )
        return {
            "candidates": prepared,
            "counts": counts,
            "review_digest": review_digest,
        }

    async def blocking_summary(self, novel_id: str) -> dict[str, Any] | None:
        query = {
            "novel_id": to_object_id(novel_id),
            "status": {"$in": sorted(REVIEWABLE_STATUSES)},
            "requires_review_before_next_chapter": True,
        }
        documents = await self.collection.find(query).sort(
            [("chapter_order", 1), ("created_at", 1)]
        ).to_list(length=100)
        if not documents:
            return None
        return {
            "count": len(documents),
            "candidate_ids": [str(item["_id"]) for item in documents],
            "chapter_ids": list(
                dict.fromkeys(str(item["chapter_id"]) for item in documents)
            ),
            "names": [
                str((item.get("candidate_data") or {}).get("name") or "")
                for item in documents
            ],
        }

    async def _normalize_decisions(
        self,
        novel_id: str,
        documents: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        by_id = {str(item["_id"]): item for item in documents}
        received_ids = [str(item.get("candidate_id") or "") for item in decisions]
        if len(received_ids) != len(set(received_ids)):
            raise CandidateReviewError(
                "Each candidate must be decided exactly once"
            )
        if set(received_ids) != set(by_id):
            raise CandidateReviewError(
                "Decisions must cover the selected candidates exactly once"
            )

        normalized: list[dict[str, Any]] = []
        for raw in decisions:
            candidate_id = str(raw.get("candidate_id") or "")
            document = by_id[candidate_id]
            action = str(raw.get("action") or "")
            if action not in DECISION_ACTIONS:
                raise CandidateReviewError(
                    f"Unsupported candidate decision: {action}"
                )
            overrides = raw.get("overrides") or {}
            if not isinstance(overrides, dict):
                raise CandidateReviewError("Candidate overrides must be an object")
            unknown = set(overrides) - EDITABLE_FIELDS
            if unknown:
                raise CandidateReviewError(
                    f"Unsupported candidate override fields: {sorted(unknown)}"
                )
            candidate_data = deepcopy(document.get("candidate_data") or {})
            candidate_data.update(deepcopy(overrides))
            candidate_data = validate_reference_card_candidate(
                str(document["card_type"]),
                candidate_data,
            )
            overwrite_fields = sorted(
                {
                    str(field)
                    for field in raw.get("overwrite_fields") or []
                    if str(field)
                }
            )
            if any(
                field not in EDITABLE_FIELDS
                and not field.startswith("details.")
                and not field.startswith("character_profile.")
                for field in overwrite_fields
            ):
                raise CandidateReviewError("Unsupported merge overwrite field")
            target_card_id = raw.get("target_card_id")
            target_digest: str | None = None
            if action in {"merge", "restore_merge"}:
                if not target_card_id:
                    raise CandidateReviewError(
                        f"{action} requires target_card_id"
                    )
                repository = get_card_repository(str(document["card_type"]))
                target = await repository.get_card(
                    novel_id,
                    str(document["card_type"]),
                    str(target_card_id),
                    include_deleted=True,
                )
                if action == "merge" and target.get("is_deleted"):
                    raise StaleCandidateReview("Merge target moved to trash")
                if action == "restore_merge" and not target.get("is_deleted"):
                    raise StaleCandidateReview(
                        "Restore-and-merge target is no longer in trash"
                    )
                target_digest = _digest(target)
            elif target_card_id:
                raise CandidateReviewError(
                    f"{action} must not include target_card_id"
                )
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "card_type": str(document["card_type"]),
                    "action": action,
                    "target_card_id": (
                        str(target_card_id) if target_card_id else None
                    ),
                    "target_digest": target_digest,
                    "reserved_card_id": str(document["reserved_card_id"]),
                    "candidate": candidate_data,
                    "overwrite_fields": overwrite_fields,
                }
            )
        normalized.sort(key=lambda item: item["candidate_id"])
        return normalized, _digest(normalized)

    @staticmethod
    async def _execute_apply(session: Any, mutation: Any) -> dict[str, Any]:
        command = mutation.journal["command"]["payload"]
        candidate_collection = get_database()[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ]
        counts = {
            "created": 0,
            "merged": 0,
            "restored_merged": 0,
            "deferred": 0,
            "ignored": 0,
        }
        mappings: list[dict[str, Any]] = []
        for decision in command["decisions"]:
            receipt_key = f"candidate_{decision['candidate_id']}"
            if mutation.was_received(receipt_key):
                receipt = deepcopy(mutation.journal["receipts"][receipt_key])
            else:
                document = await candidate_collection.find_one(
                    {
                        "_id": to_object_id(decision["candidate_id"]),
                        "novel_id": to_object_id(command["novel_id"]),
                    },
                    session=session,
                )
                if document is None:
                    raise MutationConflictError(
                        "Reference-card candidate disappeared during application"
                    )
                existing_decision = document.get("decision") or {}
                if (
                    str(existing_decision.get("decision_digest") or "")
                    == str(command["decision_digest"])
                ):
                    receipt = {
                        "candidate_id": decision["candidate_id"],
                        "action": decision["action"],
                        "card_id": (
                            str(document.get("resolved_card_id"))
                            if document.get("resolved_card_id")
                            else None
                        ),
                    }
                    await mutation.receipt(receipt_key, receipt)
                    mappings.append(receipt)
                    counts[{
                        "create": "created",
                        "merge": "merged",
                        "restore_merge": "restored_merged",
                        "defer": "deferred",
                        "ignore": "ignored",
                    }[decision["action"]]] += 1
                    continue
                if str(document.get("status") or "") not in REVIEWABLE_STATUSES:
                    raise MutationConflictError(
                        "Reference-card candidate was decided by another action"
                    )

                action = decision["action"]
                card_id: str | None = None
                if action in {"defer", "ignore"}:
                    next_status = "deferred" if action == "defer" else "ignored"
                else:
                    repository = get_card_repository(decision["card_type"])
                    if action == "create":
                        card_id = decision["reserved_card_id"]
                        try:
                            await repository.get_card(
                                command["novel_id"],
                                decision["card_type"],
                                card_id,
                                include_deleted=True,
                                session=session,
                            )
                        except NotFoundError:
                            await repository.create_card(
                                command["novel_id"],
                                decision["card_type"],
                                decision["candidate"],
                                session=session,
                                card_id=card_id,
                            )
                    else:
                        card_id = decision["target_card_id"]
                        current = await repository.get_card(
                            command["novel_id"],
                            decision["card_type"],
                            card_id,
                            include_deleted=True,
                            session=session,
                        )
                        if _digest(current) != decision["target_digest"]:
                            raise MutationConflictError(
                                "Reference-card merge target changed; reload"
                            )
                        if action == "merge" and current.get("is_deleted"):
                            raise MutationConflictError(
                                "Reference-card merge target moved to trash"
                            )
                        if action == "restore_merge" and current.get("is_deleted"):
                            await repository.restore_card(
                                command["novel_id"],
                                decision["card_type"],
                                card_id,
                                session=session,
                            )
                            current = await repository.get_card(
                                command["novel_id"],
                                decision["card_type"],
                                card_id,
                                session=session,
                            )
                        merged = merge_reference_card_data(
                            current,
                            decision["candidate"],
                            set(decision["overwrite_fields"]),
                        )
                        await repository.update_card(
                            command["novel_id"],
                            decision["card_type"],
                            card_id,
                            merged,
                            session=session,
                        )
                    next_status = "resolved"

                now = get_utc_now()
                updated = await candidate_collection.update_one(
                    {
                        "_id": document["_id"],
                        "status": {"$in": sorted(REVIEWABLE_STATUSES)},
                    },
                    {
                        "$set": {
                            "status": next_status,
                            "resolved_card_id": (
                                to_object_id(card_id) if card_id else None
                            ),
                            "decision": {
                                "action": action,
                                "actor_id": command["actor_id"],
                                "decision_digest": command["decision_digest"],
                                "overwrite_fields": decision["overwrite_fields"],
                                "decided_at": now,
                            },
                            "updated_at": now,
                        }
                    },
                    session=session,
                )
                if updated.modified_count != 1:
                    raise MutationConflictError(
                        "Reference-card candidate changed during application"
                    )
                receipt = {
                    "candidate_id": decision["candidate_id"],
                    "action": action,
                    "card_id": card_id,
                }
                await mutation.receipt(receipt_key, receipt)
            mappings.append(receipt)
            counts[{
                "create": "created",
                "merge": "merged",
                "restore_merge": "restored_merged",
                "defer": "deferred",
                "ignore": "ignored",
            }[receipt["action"]]] += 1
        return {"counts": counts, "mappings": mappings}

    async def apply(
        self,
        *,
        novel_id: str,
        actor_id: str,
        review_digest: str,
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        await novel_repo.get_novel_by_id(novel_id)
        candidate_ids = [
            str(decision.get("candidate_id") or "")
            for decision in decisions
        ]
        documents = await self._load_documents(
            novel_id,
            candidate_ids=candidate_ids,
        )
        review = await self.inspect(
            novel_id,
            candidate_ids=candidate_ids,
        )
        if not review_digest or review_digest != review["review_digest"]:
            raise StaleCandidateReview(
                "Reference-card candidates or match targets changed; reload"
            )
        normalized, decision_digest = await self._normalize_decisions(
            novel_id,
            documents,
            decisions,
        )
        formal_write = any(
            item["action"] in {"create", "merge", "restore_merge"}
            for item in normalized
        )
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"apply-emergent-reference-cards:{decision_digest}"
                ),
                operation="apply_emergent_reference_card_candidates",
                payload={
                    "novel_id": novel_id,
                    "actor_id": actor_id,
                    "decision_digest": decision_digest,
                    "decisions": normalized,
                },
                child_ids={
                    item["candidate_id"]: item["reserved_card_id"]
                    for item in normalized
                    if item["action"] == "create"
                },
            ),
            EmergentReferenceCardCandidateModule._execute_apply,
            advances_narrative_revision=formal_write,
        )
        return {
            **result,
            "blocking": await self.blocking_summary(novel_id),
        }


emergent_reference_card_candidate_module = (
    EmergentReferenceCardCandidateModule()
)
