"""Conservative compensation for one untouched auto-created reference card.

The compensation is intentionally narrower than ordinary card deletion.  It
only reverses the exact write proven by an auto-creation mutation receipt, and
only while no registered narrative or generation dependency contains the
formal card id.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.mutation import (
    MutationCommand,
    MutationEngine,
    MutationHandlerSpec,
)
from backend.db.narrative_revision import narrative_revision_store
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.reference_card_auto_creation import (
    AUTO_CREATE_MUTATION_NAME,
    AUTO_CREATE_MUTATION_VERSION,
    reference_card_content_digest,
)
from backend.services.novel.reference_card_service import get_card_repository


AUTO_CARD_REVERT_MUTATION_NAME = "revert_auto_created_reference_card"
AUTO_CARD_REVERT_MUTATION_VERSION = 1
AUTO_CARD_REVERT_MUTATION_OPERATION = (
    f"{AUTO_CARD_REVERT_MUTATION_NAME}@{AUTO_CARD_REVERT_MUTATION_VERSION}"
)
AUTO_CARD_REVERT_INSPECTION_SCHEMA = "auto_reference_card_revert_inspection.v1"


# Every collection that can carry novel-scoped formal references belongs to one
# named scanner.  The names, rather than content excerpts, are returned as
# bounded evidence when compensation is blocked.
REFERENCE_DEPENDENCY_SCANNERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "formal_narrative",
        (
            collections.NOVELS,
            collections.VOLUMES,
            collections.CHAPTERS,
            collections.FACTIONS,
            collections.FACTION_RELATIONS,
            collections.PLOT_THREADS,
            collections.OUTLINES,
        ),
    ),
    (
        "state_and_facts",
        (
            collections.CHARACTER_STATES,
            collections.CHAPTER_STATE_DELTAS,
            collections.CHARACTER_STATE_SNAPSHOTS,
            collections.PLOT_THREAD_EVENTS,
            collections.MANUAL_CORRECTIONS,
            collections.STATE_PREVIEWS,
        ),
    ),
    (
        "generation_basis",
        (
            collections.GENERATION_JOBS,
            collections.PROSE_RUNS,
            collections.PROSE_REMEDIATION_RECEIPTS,
            collections.STATE_CANDIDATE_REPAIR_RECEIPTS,
            collections.REFERENCE_CARD_REPAIR_RECEIPTS,
            collections.GENERATION_TASKS,
            collections.MEMORY_FRAGMENTS,
        ),
    ),
    (
        "agent_basis",
        (
            collections.AGENT_RUNS,
            collections.AGENT_REVISION_PROPOSALS,
            collections.AGENT_RUNTIME_READINESS,
            collections.AGENT_RUNTIME_RUNS,
            collections.AGENT_RUNTIME_STEPS,
            collections.AGENT_RUNTIME_EVENTS,
        ),
    ),
    (
        "visual_assets",
        (
            collections.IMAGE_ASSETS,
            collections.IMAGE_JOBS,
            collections.IMAGE_BATCHES,
            collections.CHARACTER_VISUAL_PROFILES,
            collections.ILLUSTRATION_BRIEFS,
            collections.ILLUSTRATION_RUNS,
        ),
    ),
    (
        "reference_records",
        (
            collections.CHARACTERS,
            collections.WORLDBOOK,
            collections.REFERENCE_CARD_PROPOSALS,
            collections.CARD_IMPORT_PROPOSALS,
        ),
    ),
    ("mutation_receipts", (collections.MUTATION_JOURNALS,)),
)


class AutoReferenceCardRevertDenied(ValueError):
    """The inspected card no longer satisfies the compensation policy."""

    def __init__(self, result: Mapping[str, Any]) -> None:
        super().__init__("Auto-created reference-card compensation was denied")
        self.result = deepcopy(dict(result))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
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
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contains_card_id(value: Any, card_id: str) -> bool:
    if isinstance(value, ObjectId):
        return str(value) == card_id
    if isinstance(value, str):
        return value.lower() == card_id
    if isinstance(value, Mapping):
        return any(_contains_card_id(item, card_id) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_card_id(item, card_id) for item in value)
    return False


def _blocker(code: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, **deepcopy(evidence)}


def _is_target_card_audit_journal(
    document: Mapping[str, Any],
    card_id: str,
) -> bool:
    """Return true for audit of the target card itself, never its consumers."""

    if str(document.get("operation") or "") not in {
        "create_reference_card",
        "update_reference_card_context",
        "update_reference_card_metadata",
        "soft_delete_reference_card",
        "restore_reference_card",
        "hard_delete_reference_card",
    }:
        return False
    command = document.get("command")
    payload = command.get("payload") if isinstance(command, Mapping) else None
    return (
        isinstance(payload, Mapping)
        and str(payload.get("card_id") or "") == card_id
    )


class AutoReferenceCardRevertService:
    def __init__(
        self,
        *,
        after_card_delete: Callable[[str, str], Awaitable[None]] | None = None,
        after_candidate_write: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._after_card_delete = after_card_delete
        self._after_candidate_write = after_candidate_write

    @staticmethod
    def _idempotency_key(candidate_id: str) -> str:
        return f"revert-auto-created-reference-card:{candidate_id}"

    @staticmethod
    async def _load_origin(
        *,
        owner_id: str,
        novel_id: str,
        candidate_id: str,
        allow_reverted_by: str | None,
        session: Any,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        database = get_database()
        candidate = await database[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ].find_one(
            {
                "_id": to_object_id(candidate_id),
                "novel_id": to_object_id(novel_id),
                "is_deleted": False,
            },
            session=session,
        )
        if candidate is None:
            raise NotFoundError(
                f"Reference-card candidate '{candidate_id}' was not found"
            )

        current_decision = candidate.get("decision")
        original_decision = candidate.get("original_decision")
        auto_creation = candidate.get("auto_creation")
        is_own_partial_revert = bool(
            allow_reverted_by
            and str(candidate.get("status") or "") == "deferred"
            and isinstance(current_decision, Mapping)
            and str(current_decision.get("action") or "") == "reverted"
            and str(current_decision.get("actor_id") or "") == owner_id
            and str(current_decision.get("mutation_idempotency_key") or "")
            == allow_reverted_by
            and isinstance(original_decision, Mapping)
            and isinstance(auto_creation, Mapping)
            and auto_creation.get("reverted") is True
        )
        decision = (
            original_decision if is_own_partial_revert else current_decision
        )
        card_id = str(
            candidate.get("reverted_from_card_id")
            if is_own_partial_revert
            else candidate.get("resolved_card_id")
            or ""
        )
        card_type = str(candidate.get("card_type") or "")
        mutation_key = (
            str(decision.get("mutation_idempotency_key") or "")
            if isinstance(decision, Mapping)
            else ""
        )
        blockers: list[dict[str, Any]] = []
        if (
            (
                str(candidate.get("status") or "") != "resolved"
                and not is_own_partial_revert
            )
            or not isinstance(decision, Mapping)
            or str(decision.get("action") or "") != "auto_create_unique"
            or str(decision.get("actor_id") or "") != owner_id
            or not isinstance(auto_creation, Mapping)
            or auto_creation.get("counted") is not True
            or str(auto_creation.get("mutation_idempotency_key") or "")
            != mutation_key
            or not ObjectId.is_valid(card_id)
            or card_type not in {"character", "location", "item", "rule", "lore"}
            or not mutation_key
        ):
            return None, [_blocker("invalid_auto_creation_origin")]

        origin_journal = await database[collections.MUTATION_JOURNALS].find_one(
            {
                "novel_id": to_object_id(novel_id),
                "idempotency_key": mutation_key,
                "operation": AUTO_CREATE_MUTATION_NAME,
                "status": "completed",
                "is_deleted": False,
            },
            session=session,
        )
        if origin_journal is None:
            return None, [_blocker("invalid_auto_creation_origin")]
        try:
            source_command = MutationCommand.from_journal(origin_journal)
        except (KeyError, TypeError, ValueError):
            return None, [_blocker("invalid_auto_creation_origin")]
        if (
            source_command.version != AUTO_CREATE_MUTATION_VERSION
            or source_command.digest()
            != str(origin_journal.get("command_digest") or "")
        ):
            return None, [_blocker("invalid_auto_creation_origin")]

        payload = source_command.payload
        frozen_candidates = payload.get("candidates")
        matches = (
            [
                item
                for item in frozen_candidates
                if isinstance(item, Mapping)
                and str(item.get("candidate_id") or "") == candidate_id
            ]
            if isinstance(frozen_candidates, list)
            else []
        )
        receipts = origin_journal.get("receipts")
        candidate_receipt = (
            receipts.get(f"candidate_{candidate_id}")
            if isinstance(receipts, Mapping)
            else None
        )
        card_write_receipt = (
            receipts.get(f"card_write_{candidate_id}")
            if isinstance(receipts, Mapping)
            else None
        )
        formal_digest = str(decision.get("formal_card_content_digest") or "")
        if (
            str(payload.get("owner_id") or "") != owner_id
            or str(payload.get("novel_id") or "") != novel_id
            or str(payload.get("mutation_idempotency_key") or "") != mutation_key
            or len(matches) != 1
            or str(matches[0].get("reserved_card_id") or "") != card_id
            or str(matches[0].get("card_type") or "") != card_type
            or not isinstance(candidate_receipt, Mapping)
            or str(candidate_receipt.get("card_id") or "") != card_id
            or str(candidate_receipt.get("formal_card_content_digest") or "")
            != formal_digest
            or not isinstance(card_write_receipt, Mapping)
            or str(card_write_receipt.get("card_id") or "") != card_id
            or str(card_write_receipt.get("formal_card_content_digest") or "")
            != formal_digest
            or len(formal_digest) != 64
        ):
            blockers.append(_blocker("invalid_auto_creation_origin"))
            return None, blockers

        return {
            "candidate": candidate,
            "decision": deepcopy(dict(decision)),
            "card_id": card_id,
            "card_type": card_type,
            "formal_card_content_digest": formal_digest,
            "source_auto_mutation_id": mutation_key,
            "source_auto_journal_id": str(origin_journal["_id"]),
            "source_outline_mutation_id": str(
                payload.get("source_mutation_id") or ""
            ),
            "source_job_id": str(payload.get("job_id") or ""),
            "authorization_digest": str(
                decision.get("authorization_digest") or ""
            ),
        }, []

    @staticmethod
    async def _dependency_counts(
        *,
        novel_id: str,
        card_id: str,
        source_auto_journal_id: str,
        source_outline_mutation_id: str,
        source_job_id: str,
        current_revert_journal_id: str | None,
        session: Any,
    ) -> dict[str, int]:
        database = get_database()
        novel_object_id = to_object_id(novel_id)
        excluded_journal_ids = {source_auto_journal_id}
        if current_revert_journal_id:
            excluded_journal_ids.add(current_revert_journal_id)
        counts: dict[str, int] = {}

        for scanner_name, collection_names in REFERENCE_DEPENDENCY_SCANNERS:
            matched_documents = 0
            for collection_name in collection_names:
                query = (
                    {"_id": novel_object_id}
                    if collection_name == collections.NOVELS
                    else {"novel_id": novel_object_id}
                )
                documents = await database[collection_name].find(
                    query,
                    session=session,
                ).to_list(length=None)
                for raw_document in documents:
                    document_id = str(raw_document.get("_id") or "")
                    if (
                        collection_name
                        in {collections.CHARACTERS, collections.WORLDBOOK}
                        and document_id == card_id
                    ):
                        continue
                    if collection_name == collections.MUTATION_JOURNALS:
                        if (
                            document_id in excluded_journal_ids
                            or str(raw_document.get("idempotency_key") or "")
                            == source_outline_mutation_id
                            or _is_target_card_audit_journal(
                                raw_document,
                                card_id,
                            )
                        ):
                            continue
                    document = raw_document
                    if (
                        collection_name == collections.GENERATION_JOBS
                        and document_id == source_job_id
                    ):
                        # The originating Job's append-only event is audit proof,
                        # not evidence that the formal card became model input.
                        document = {
                            key: value
                            for key, value in raw_document.items()
                            if key != "reference_card_auto_creation_events"
                        }
                    if _contains_card_id(document, card_id):
                        matched_documents += 1
            if matched_documents:
                counts[scanner_name] = matched_documents
        return counts

    async def _inspect_internal(
        self,
        *,
        owner_id: str,
        novel_id: str,
        candidate_id: str,
        expected_revision_override: int | None = None,
        current_revert_journal_id: str | None = None,
        allow_own_partial_delete: bool = False,
        own_revert_idempotency_key: str | None = None,
        session: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if not all(
            ObjectId.is_valid(value)
            for value in (owner_id, novel_id, candidate_id)
        ):
            raise ValueError("owner_id, novel_id, and candidate_id must be ObjectIds")
        novel = await get_database()[collections.NOVELS].find_one(
            {
                "_id": to_object_id(novel_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
            },
            session=session,
        )
        if novel is None:
            raise NotFoundError(f"Novel '{novel_id}' was not found")

        origin, blockers = await self._load_origin(
            owner_id=owner_id,
            novel_id=novel_id,
            candidate_id=candidate_id,
            allow_reverted_by=(
                own_revert_idempotency_key
                if allow_own_partial_delete
                else None
            ),
            session=session,
        )
        card_id = str(origin.get("card_id") or "") if origin else ""
        card_type = str(origin.get("card_type") or "") if origin else ""
        formal_digest = (
            str(origin.get("formal_card_content_digest") or "")
            if origin
            else ""
        )
        if origin is not None:
            repository = get_card_repository(card_type)
            try:
                card = await repository.get_card(
                    novel_id,
                    card_type,
                    card_id,
                    include_deleted=True,
                    session=session,
                )
            except NotFoundError:
                card = None
            if card is None:
                blockers.append(_blocker("card_missing"))
            else:
                own_partial_delete = (
                    allow_own_partial_delete and card.get("is_deleted") is True
                )
                if card.get("is_deleted") is True and not own_partial_delete:
                    blockers.append(_blocker("card_unavailable"))
                if reference_card_content_digest(card) != formal_digest:
                    blockers.append(_blocker("card_changed"))

            scanner_counts = await self._dependency_counts(
                novel_id=novel_id,
                card_id=card_id,
                source_auto_journal_id=str(origin["source_auto_journal_id"]),
                source_outline_mutation_id=str(
                    origin["source_outline_mutation_id"]
                ),
                source_job_id=str(origin["source_job_id"]),
                current_revert_journal_id=current_revert_journal_id,
                session=session,
            )
            if scanner_counts:
                blockers.append(
                    _blocker(
                        "reference_in_use",
                        scanner_counts=scanner_counts,
                        reference_count=sum(scanner_counts.values()),
                    )
                )

        revision = (
            expected_revision_override
            if expected_revision_override is not None
            else await narrative_revision_store.current(novel_id, session=session)
        )
        public = {
            "schema_version": AUTO_CARD_REVERT_INSPECTION_SCHEMA,
            "status": "ready" if not blockers else "blocked",
            "candidate_id": candidate_id,
            "card_id": card_id,
            "card_type": card_type,
            "expected_narrative_revision": revision,
            "formal_card_content_digest": formal_digest,
            "source_auto_mutation_id": (
                str(origin.get("source_auto_mutation_id") or "")
                if origin
                else ""
            ),
            "blockers": blockers,
        }
        public["digest"] = _digest(public)
        return public, origin

    async def inspect(
        self,
        *,
        owner_id: str,
        novel_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        inspection, _ = await self._inspect_internal(
            owner_id=str(owner_id),
            novel_id=str(novel_id),
            candidate_id=str(candidate_id),
        )
        return inspection

    @staticmethod
    def _denied_result(
        inspection: Mapping[str, Any],
        *,
        extra_blocker: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        blockers = [deepcopy(item) for item in inspection.get("blockers", [])]
        if extra_blocker is not None:
            blockers.append(deepcopy(dict(extra_blocker)))
        return {
            "status": "denied",
            "candidate_id": str(inspection.get("candidate_id") or ""),
            "card_id": str(inspection.get("card_id") or ""),
            "blockers": blockers,
            "next_narrative_revision": int(
                inspection.get("expected_narrative_revision") or 0
            ),
        }

    async def _execute_revert(self, session: Any, mutation: Any) -> dict[str, Any]:
        command = mutation.journal["command"]["payload"]
        expected_revision = int(command["expected_narrative_revision"])
        inspection, origin = await self._inspect_internal(
            owner_id=str(command["owner_id"]),
            novel_id=str(command["novel_id"]),
            candidate_id=str(command["candidate_id"]),
            expected_revision_override=expected_revision,
            current_revert_journal_id=str(mutation.journal["_id"]),
            allow_own_partial_delete=mutation.was_received("card_soft_delete"),
            own_revert_idempotency_key=str(
                command["mutation_idempotency_key"]
            ),
            session=session,
        )
        if (
            inspection["digest"] != command["inspection_digest"]
            or inspection["status"] != "ready"
            or origin is None
            or inspection["card_id"] != command["card_id"]
            or inspection["formal_card_content_digest"]
            != command["formal_card_content_digest"]
        ):
            return self._denied_result(
                inspection,
                extra_blocker=(
                    _blocker("inspection_changed")
                    if inspection["digest"] != command["inspection_digest"]
                    else None
                ),
            )

        candidate_id = str(command["candidate_id"])
        card_id = str(command["card_id"])
        card_type = str(command["card_type"])
        database = get_database()
        repository = get_card_repository(card_type)
        if not mutation.was_received("card_soft_delete"):
            now = get_utc_now()
            deleted = await repository.collection.update_one(
                {
                    "_id": to_object_id(card_id),
                    "novel_id": to_object_id(str(command["novel_id"])),
                    "card_type": card_type,
                    "is_deleted": False,
                },
                {
                    "$set": {
                        "is_deleted": True,
                        "deleted_at": now,
                        "updated_at": now,
                    }
                },
                session=session,
            )
            if deleted.modified_count != 1:
                return self._denied_result(
                    inspection,
                    extra_blocker=_blocker("card_changed"),
                )
            await mutation.receipt(
                "card_soft_delete",
                {
                    "card_id": card_id,
                    "card_type": card_type,
                    "formal_card_content_digest": command[
                        "formal_card_content_digest"
                    ],
                },
            )

        if not mutation.was_received("candidate_compensation"):
            if self._after_card_delete is not None:
                await self._after_card_delete(candidate_id, card_id)
            candidate_collection = database[
                collections.EMERGENT_REFERENCE_CARD_CANDIDATES
            ]
            candidate = await candidate_collection.find_one(
                {
                    "_id": to_object_id(candidate_id),
                    "novel_id": to_object_id(str(command["novel_id"])),
                    "is_deleted": False,
                },
                session=session,
            )
            if candidate is None:
                return self._denied_result(
                    inspection,
                    extra_blocker=_blocker("candidate_changed"),
                )
            current_decision = deepcopy(candidate.get("decision") or {})
            exact_origin = (
                str(candidate.get("status") or "") == "resolved"
                and str(candidate.get("resolved_card_id") or "") == card_id
                and current_decision == origin["decision"]
            )
            current_revert = candidate.get("decision") or {}
            exact_revert = (
                str(candidate.get("status") or "") == "deferred"
                and str(candidate.get("reverted_from_card_id") or "") == card_id
                and isinstance(current_revert, Mapping)
                and str(current_revert.get("action") or "") == "reverted"
                and str(current_revert.get("mutation_idempotency_key") or "")
                == command["mutation_idempotency_key"]
            )
            if exact_origin:
                now = get_utc_now()
                auto_creation = deepcopy(dict(candidate.get("auto_creation") or {}))
                auto_creation.update(
                    {
                        "counted": True,
                        "reverted": True,
                        "compensation_mutation_idempotency_key": command[
                            "mutation_idempotency_key"
                        ],
                    }
                )
                updated = await candidate_collection.update_one(
                    {
                        "_id": candidate["_id"],
                        "status": "resolved",
                        "resolved_card_id": to_object_id(card_id),
                        "decision.mutation_idempotency_key": origin[
                            "source_auto_mutation_id"
                        ],
                    },
                    {
                        "$set": {
                            "status": "deferred",
                            "reverted_from_card_id": to_object_id(card_id),
                            "original_decision": origin["decision"],
                            "decision": {
                                "action": "reverted",
                                "actor_id": command["owner_id"],
                                "card_id": card_id,
                                "source_auto_mutation_id": origin[
                                    "source_auto_mutation_id"
                                ],
                                "mutation_idempotency_key": command[
                                    "mutation_idempotency_key"
                                ],
                                "inspection_digest": command[
                                    "inspection_digest"
                                ],
                                "decided_at": now,
                            },
                            "auto_creation": auto_creation,
                            "updated_at": now,
                        },
                        "$unset": {"resolved_card_id": ""},
                    },
                    session=session,
                )
                if updated.modified_count != 1:
                    return self._denied_result(
                        inspection,
                        extra_blocker=_blocker("candidate_changed"),
                    )
            elif not exact_revert:
                return self._denied_result(
                    inspection,
                    extra_blocker=_blocker("candidate_changed"),
                )
            if self._after_candidate_write is not None:
                await self._after_candidate_write(candidate_id, card_id)
            await mutation.receipt(
                "candidate_compensation",
                {
                    "candidate_id": candidate_id,
                    "card_id": card_id,
                    "original_action": "auto_create_unique",
                    "compensation_action": "reverted",
                },
            )

        revision = int(
            (mutation.journal.get("receipts") or {})
            .get("narrative_revision", {})
            .get("revision")
            or expected_revision
        )
        compensation_receipt = {
            "operation": AUTO_CARD_REVERT_MUTATION_OPERATION,
            "candidate_id": candidate_id,
            "card_id": card_id,
            "source_auto_mutation_id": origin["source_auto_mutation_id"],
            "inspection_digest": command["inspection_digest"],
            "formal_card_content_digest": command["formal_card_content_digest"],
            "next_narrative_revision": revision,
        }
        await mutation.receipt("compensation", compensation_receipt)
        return {
            "status": "reverted",
            **compensation_receipt,
            "blockers": [],
        }

    @staticmethod
    def _engine(callback: Callable[..., Awaitable[dict[str, Any]]]) -> MutationEngine:
        return MutationEngine(
            {
                (
                    AUTO_CARD_REVERT_MUTATION_NAME,
                    AUTO_CARD_REVERT_MUTATION_VERSION,
                ): MutationHandlerSpec(
                    callback,
                    advances_narrative_revision=True,
                    persistent_narrative_fence=True,
                )
            }
        )

    async def revert(
        self,
        *,
        owner_id: str,
        novel_id: str,
        candidate_id: str,
        expected_narrative_revision: int,
        inspection_digest: str,
    ) -> dict[str, Any]:
        owner_id = str(owner_id)
        novel_id = str(novel_id)
        candidate_id = str(candidate_id)
        idempotency_key = self._idempotency_key(candidate_id)
        journal = await get_database()[collections.MUTATION_JOURNALS].find_one(
            {
                "novel_id": to_object_id(novel_id),
                "idempotency_key": idempotency_key,
            }
        )
        if journal is not None:
            command = MutationCommand.from_journal(journal)
            payload = command.payload
            if (
                command.operation != AUTO_CARD_REVERT_MUTATION_NAME
                or command.version != AUTO_CARD_REVERT_MUTATION_VERSION
                or str(payload.get("owner_id") or "") != owner_id
                or str(payload.get("novel_id") or "") != novel_id
                or str(payload.get("candidate_id") or "") != candidate_id
                or payload.get("expected_narrative_revision")
                != expected_narrative_revision
                or str(payload.get("inspection_digest") or "")
                != inspection_digest
            ):
                raise AutoReferenceCardRevertDenied(
                    {
                        "status": "denied",
                        "candidate_id": candidate_id,
                        "card_id": "",
                        "blockers": [_blocker("request_changed")],
                        "next_narrative_revision": expected_narrative_revision,
                    }
                )
        else:
            inspection, origin = await self._inspect_internal(
                owner_id=owner_id,
                novel_id=novel_id,
                candidate_id=candidate_id,
            )
            if (
                inspection["digest"] != inspection_digest
                or inspection["expected_narrative_revision"]
                != expected_narrative_revision
            ):
                raise AutoReferenceCardRevertDenied(
                    self._denied_result(
                        inspection,
                        extra_blocker=_blocker("inspection_changed"),
                    )
                )
            if inspection["status"] != "ready" or origin is None:
                raise AutoReferenceCardRevertDenied(
                    self._denied_result(inspection)
                )
            payload = {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "candidate_id": candidate_id,
                "card_id": inspection["card_id"],
                "card_type": inspection["card_type"],
                "expected_narrative_revision": expected_narrative_revision,
                "inspection_digest": inspection_digest,
                "formal_card_content_digest": inspection[
                    "formal_card_content_digest"
                ],
                "source_auto_mutation_id": origin["source_auto_mutation_id"],
                "mutation_idempotency_key": idempotency_key,
            }
            command = MutationCommand(
                novel_id=novel_id,
                idempotency_key=idempotency_key,
                operation=AUTO_CARD_REVERT_MUTATION_NAME,
                version=AUTO_CARD_REVERT_MUTATION_VERSION,
                expected_narrative_revision=expected_narrative_revision,
                payload=payload,
            )

        result = await self._engine(self._execute_revert).execute(command)
        if result.get("status") != "reverted":
            raise AutoReferenceCardRevertDenied(result)
        return result


auto_reference_card_revert_service = AutoReferenceCardRevertService()
