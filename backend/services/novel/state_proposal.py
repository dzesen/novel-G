"""Persisted state-generation leases, proposals, decisions, and acceptance claims.

The module owns the complete trust boundary between a paid model result and a
recoverable narrative mutation.  Callers receive opaque handles; tokens are
verified here and are never copied into mutation journals.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.config.config import CONFIG_PATH
from backend.config.lifecycle import FileSecretVersionStore
from backend.db import collections
from backend.db.mongo import get_database
from backend.db.mutation import MutationConflictError
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
    STATE_DISPATCH_RESOLUTION_ACTIONS,
    StateContextProjection,
)
from backend.services.generation.required_chapter_state_contracts import (
    RequiredStateGenerationBinding,
)
from backend.services.llm.workflow_runner import parse_sse_event, sse_event
from backend.services.novel.state_completion import (
    chapter_content_digest,
    prose_acceptance_state,
)
from backend.services.novel.state_validation import (
    resolve_state_character_references,
    state_reference_resolution,
    validate_state_ids,
)
from backend.services.novel.outline_validation import known_id_sets
from backend.services.novel.state_fact_accounting import (
    StateFactAccountingError,
    account_state_fact_evidence,
    automatic_state_fact_decision,
    validate_state_fact_evidence,
)


PROPOSAL_TTL_SECONDS = 15 * 60
STATE_PROPOSAL_DISPATCH_PROTOCOL_REVISION = 1


class StaleStatePreview(ValueError):
    """A proposal is missing, expired, reused, or bound to stale narrative data."""


StateProposalCompletionFailureReason = Literal[
    "canonical_fact_unaccounted",
    "evidence_invalid",
    "internal_reference_invalid",
    "semantic_unknown",
    "source_binding_stale",
]


_STATE_PROPOSAL_COMPLETION_FAILURE_REASONS: frozenset[
    StateProposalCompletionFailureReason
] = frozenset({
    "canonical_fact_unaccounted",
    "evidence_invalid",
    "internal_reference_invalid",
    "semantic_unknown",
    "source_binding_stale",
})


class StateProposalCompletionFailure(StaleStatePreview):
    """A closed state-policy fact that the chapter finalizer can classify."""

    def __init__(
        self,
        message: str,
        *,
        reason: StateProposalCompletionFailureReason,
    ) -> None:
        if reason not in _STATE_PROPOSAL_COMPLETION_FAILURE_REASONS:
            raise ValueError("state proposal completion failure reason is invalid")
        super().__init__(message)
        self.completion_failure_reason = reason


def _uses_current_dispatch_protocol(proposal: dict[str, Any]) -> bool:
    revision = proposal.get("dispatch_protocol_revision")
    return (
        type(revision) is int
        and revision == STATE_PROPOSAL_DISPATCH_PROTOCOL_REVISION
    )


def _dispatch_protocol_query() -> dict[str, Any]:
    return {
        "$eq": STATE_PROPOSAL_DISPATCH_PROTOCOL_REVISION,
        "$type": "int",
    }


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


def _job_mutation_binding(
    proposal: dict[str, Any],
) -> JobMutationRecoveryBindingV1 | None:
    audit = proposal.get("generation_audit")
    raw_audit = (
        audit.get("job_mutation_binding")
        if isinstance(audit, dict)
        else None
    )
    raw_top_level = proposal.get("job_mutation_binding")
    raw_key = proposal.get("job_mutation_key")
    if raw_audit is None and raw_top_level is None and raw_key is None:
        return None
    if raw_audit is None or raw_top_level is None or raw_key is None:
        raise MutationConflictError(
            "State proposal Job mutation binding is incomplete"
        )
    try:
        binding = JobMutationRecoveryBindingV1.model_validate(raw_audit)
        top_level = JobMutationRecoveryBindingV1.model_validate(raw_top_level)
    except (TypeError, ValueError) as exc:
        raise MutationConflictError(
            "State proposal Job mutation binding is invalid"
        ) from exc
    proposal_revision = proposal.get("narrative_revision")
    if (
        binding != top_level
        or raw_key != binding.idempotency_key
        or binding.operation != "accept_chapter_state"
        or binding.novel_id != str(proposal.get("novel_id") or "")
        or binding.chapter_id != str(proposal.get("chapter_id") or "")
        or type(proposal_revision) is not int
        or binding.expected_narrative_revision
        != proposal_revision
    ):
        raise MutationConflictError(
            "State proposal Job mutation binding diverged"
        )
    return binding


def _job_mutation_binding_query(
    binding: JobMutationRecoveryBindingV1,
) -> dict[str, Any]:
    """Return the canonical three-way Job authority fence for proposal CAS."""

    frozen = JobMutationRecoveryBindingV1.model_validate(
        binding.model_dump(mode="python")
    )
    canonical = frozen.model_dump(mode="json")
    return {
        "job_mutation_key": frozen.idempotency_key,
        "job_mutation_binding": canonical,
        "generation_audit.job_mutation_binding": canonical,
    }


def _required_state_generation_binding(
    proposal: dict[str, Any],
) -> RequiredStateGenerationBinding | None:
    audit = proposal.get("generation_audit")
    raw_audit = (
        audit.get("required_state_generation_binding")
        if isinstance(audit, dict)
        else None
    )
    raw_top_level = proposal.get("required_state_generation_binding")
    raw_key = proposal.get("required_state_generation_key")
    if raw_audit is None and raw_top_level is None and raw_key is None:
        return None
    if raw_audit is None or raw_top_level is None or raw_key is None:
        raise MutationConflictError(
            "Required state generation binding is incomplete"
        )
    try:
        binding = RequiredStateGenerationBinding.model_validate(raw_audit)
        top_level = RequiredStateGenerationBinding.model_validate(raw_top_level)
    except (TypeError, ValueError) as exc:
        raise MutationConflictError(
            "Required state generation binding is invalid"
        ) from exc
    revision = proposal.get("narrative_revision")
    if (
        binding != top_level
        or raw_key != binding.recovery_key
        or binding.novel_id != str(proposal.get("novel_id") or "")
        or binding.chapter_id != str(proposal.get("chapter_id") or "")
        or binding.source_run_id
        != str(proposal.get("source_prose_run_id") or "")
        or binding.source_run_revision
        != proposal.get("source_prose_run_revision")
        or binding.source_content_digest
        != str(proposal.get("source_content_digest") or "")
        or type(revision) is not int
        or binding.expected_narrative_revision != revision
        or binding.can_accept_formal_state is not False
    ):
        raise MutationConflictError(
            "Required state generation binding diverged"
        )
    return binding


def _required_state_generation_binding_query(
    binding: RequiredStateGenerationBinding,
) -> dict[str, Any]:
    frozen = RequiredStateGenerationBinding.model_validate(
        binding.model_dump(mode="python")
    )
    canonical = frozen.model_dump(mode="json")
    return {
        "required_state_generation_key": frozen.recovery_key,
        "required_state_generation_binding": canonical,
        "generation_audit.required_state_generation_binding": canonical,
    }


def _validate_required_state_finalization_handoff(
    proposal: Mapping[str, Any],
    *,
    required_binding: RequiredStateGenerationBinding,
    finalization_binding: JobMutationRecoveryBindingV1,
) -> None:
    """Prove that a new formal Job consumes this exact deferred proposal."""

    stored = _required_state_generation_binding(dict(proposal))
    proposal_id = str(proposal.get("_id") or "")
    expected_idempotency_key = (
        "finalize-chapter-generation:"
        f"{required_binding.source_run_id}:"
        f"{required_binding.source_run_revision}:{proposal_id}"
    )
    if (
        stored != required_binding
        or finalization_binding.operation != "finalize_chapter_generation"
        or finalization_binding.job_id == required_binding.job_id
        or finalization_binding.novel_id != required_binding.novel_id
        or finalization_binding.chapter_id != required_binding.chapter_id
        or finalization_binding.expected_narrative_revision
        != required_binding.expected_narrative_revision
        or finalization_binding.idempotency_key
        != expected_idempotency_key
    ):
        raise StaleStatePreview(
            "Required state proposal finalization handoff diverged"
        )


def _merged_generation_audit(
    proposal: dict[str, Any],
    audit: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], JobMutationRecoveryBindingV1 | None]:
    """Merge metadata without allowing callers to replace persisted authority."""

    existing = proposal.get("generation_audit") or {}
    if not isinstance(existing, Mapping):
        raise StaleStatePreview("Persisted generation audit is invalid")
    incoming = dict(deepcopy(audit or {}))
    binding = _job_mutation_binding(proposal)
    required_binding = _required_state_generation_binding(proposal)
    if "job_mutation_binding" in incoming:
        try:
            supplied = JobMutationRecoveryBindingV1.model_validate(
                incoming["job_mutation_binding"]
            )
        except (TypeError, ValueError) as exc:
            raise StaleStatePreview(
                "Generation audit Job binding is invalid"
            ) from exc
        if binding is None or supplied != binding:
            raise StaleStatePreview(
                "Generation audit Job binding cannot replace persisted authority"
            )
    merged = {**deepcopy(dict(existing)), **incoming}
    if binding is not None:
        merged["job_mutation_binding"] = binding.model_dump(mode="json")
    if "required_state_generation_binding" in incoming:
        try:
            supplied_required = RequiredStateGenerationBinding.model_validate(
                incoming["required_state_generation_binding"]
            )
        except (TypeError, ValueError) as exc:
            raise StaleStatePreview(
                "Generation audit required state binding is invalid"
            ) from exc
        if required_binding is None or supplied_required != required_binding:
            raise StaleStatePreview(
                "Generation audit cannot replace required state authority"
            )
    if required_binding is not None:
        merged["required_state_generation_binding"] = (
            required_binding.model_dump(mode="json")
        )
    return merged, binding


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
    source_content_digest: str | None = None
    source_prose_run_id: str | None = None
    source_prose_run_revision: int | None = None
    source_prose_acceptance_state: str | None = None


@dataclass(frozen=True)
class StateProposalLease:
    proposal_id: ObjectId
    snapshot: StateGenerationSnapshot


@dataclass(frozen=True)
class RecoveredStateProposal:
    value: dict[str, Any]
    truncated_section_count: int
    dropped_item_count: int
    dropped_reference_count: int


@dataclass(frozen=True)
class StatePolicyDecision:
    """One typed selection projection shared by every acceptance policy."""

    selected_character_ids: tuple[str, ...] = ()
    selected_fact_ids: tuple[str, ...] = ()
    selected_thread_ids: tuple[str, ...] = ()
    drop_reasons: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SelectAllPolicy:
    """Deterministic headless policy that accepts every selectable candidate."""

    name: str = "select_all"
    version: str = "1"

    def decide(self, proposal: dict[str, Any]) -> StatePolicyDecision:
        character_ids = tuple(
            str(character["selection_id"])
            for character in proposal.get("character_updates") or []
        )
        fact_ids = tuple(
            str(fact["selection_id"])
            for character in proposal.get("character_updates") or []
            for fact in character.get("new_permanent_facts") or []
        )
        thread_ids = tuple(
            str(thread["selection_id"])
            for thread in proposal.get("thread_updates") or []
        )
        return StatePolicyDecision(
            selected_character_ids=character_ids,
            selected_fact_ids=fact_ids,
            selected_thread_ids=thread_ids,
        )


@dataclass(frozen=True)
class FactAccountingPolicy:
    """Automatic policy that writes only supported canonical state actions."""

    name: str = "fact_accounting"
    version: str = "1"

    def decide(self, proposal: dict[str, Any]) -> StatePolicyDecision:
        evidence = proposal.get("fact_evidence")
        if not isinstance(evidence, Mapping):
            raise StaleStatePreview(
                "State proposal has no current fact-accounting evidence"
            )
        try:
            raw = automatic_state_fact_decision(
                evidence,
                candidate=proposal,
            )
        except StateFactAccountingError as exc:
            raise StaleStatePreview(str(exc)) from exc
        return StatePolicyDecision(
            selected_character_ids=tuple(raw["selected_character_ids"]),
            selected_fact_ids=tuple(raw["selected_fact_ids"]),
            selected_thread_ids=tuple(raw["selected_thread_ids"]),
            drop_reasons=tuple(sorted(dict(raw["drop_reasons"]).items())),
        )


def add_selection_ids(candidate: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(candidate)
    for character in result.get("character_updates") or []:
        character["selection_id"] = uuid4().hex
        for fact in character.get("new_permanent_facts") or []:
            fact["selection_id"] = uuid4().hex
    for thread in result.get("thread_updates") or []:
        thread["selection_id"] = uuid4().hex
    return result


def _proposal_acceptance_token(
    *,
    proposal_id: ObjectId,
    content_digest: str,
    narrative_revision: int,
    candidate_digest: str,
    source_content_digest: str,
    expires_at: datetime,
) -> str:
    token_payload = (
        f"{proposal_id}:{content_digest}:"
        f"{narrative_revision}:{candidate_digest}:"
        f"{source_content_digest}:"
        f"{int(expires_at.replace(tzinfo=timezone.utc).timestamp())}"
    )
    return hmac.new(
        _proposal_key(),
        token_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class StateProposalModule:
    @property
    def collection(self):
        return get_database()[collections.STATE_PREVIEWS]

    async def get_owned_repair_source(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        proposal_id: str,
    ) -> dict[str, Any]:
        """Load one live proposal through its owner/novel/chapter boundary."""
        novel = await novel_repo.collection.find_one(
            {
                "_id": to_object_id(novel_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
            },
            projection={"_id": 1},
        )
        if novel is None:
            raise StaleStatePreview("State proposal owner scope is invalid")
        proposal = await self.collection.find_one({
            "_id": to_object_id(proposal_id),
            "novel_id": to_object_id(novel_id),
            "chapter_id": to_object_id(chapter_id),
            "is_deleted": False,
        })
        if proposal is None:
            raise StaleStatePreview("State proposal is outside the repair scope")
        expires_at = proposal.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(timezone.utc):
            raise StaleStatePreview("State proposal is no longer live")
        return {**proposal, "owner_id": to_object_id(owner_id)}

    async def recover_owned_repair_result(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        proposal_id: str | None,
        request_id: str,
        source_run_id: str,
        source_run_revision: int,
        source_content_digest: str,
    ) -> RecoveredStateProposal | None:
        """Rebuild a live proposal token without replaying Provider work."""
        if not request_id:
            raise StaleStatePreview("State repair request identity is missing")
        resolved_proposal_id = proposal_id
        if resolved_proposal_id is None:
            novel = await novel_repo.collection.find_one(
                {
                    "_id": to_object_id(novel_id),
                    "owner_id": to_object_id(owner_id),
                    "is_deleted": False,
                },
                projection={"_id": 1},
            )
            if novel is None:
                raise StaleStatePreview(
                    "State repair proposal owner scope is invalid"
                )
            located = await self.collection.find_one({
                "novel_id": to_object_id(novel_id),
                "chapter_id": to_object_id(chapter_id),
                "status": "proposed",
                "generation_audit.request_id": str(request_id),
                "is_deleted": False,
            })
            if located is None:
                return None
            resolved_proposal_id = str(located["_id"])
        proposal = await self.get_owned_repair_source(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            proposal_id=str(resolved_proposal_id),
        )
        if str(proposal.get("status") or "") not in {
            "proposed",
            "claimed",
            "applied",
        }:
            raise StaleStatePreview(
                "State repair proposal status is not recoverable"
            )
        audit = proposal.get("generation_audit")
        candidate = proposal.get("candidate")
        expires_at = proposal.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if (
            not isinstance(audit, dict)
            or str(audit.get("request_id") or "") != str(request_id)
            or str(proposal.get("source_prose_run_id") or "")
            != str(source_run_id)
            or type(proposal.get("source_prose_run_revision")) is not int
            or proposal.get("source_prose_run_revision")
            != source_run_revision
            or str(proposal.get("source_content_digest") or "")
            != str(source_content_digest)
            or not isinstance(candidate, dict)
            or not isinstance(expires_at, datetime)
        ):
            raise StaleStatePreview(
                "State repair proposal recovery evidence is invalid"
            )
        candidate_digest = _digest(candidate)
        if candidate_digest != str(proposal.get("candidate_digest") or ""):
            raise StaleStatePreview("State repair proposal digest is invalid")
        token = _proposal_acceptance_token(
            proposal_id=proposal["_id"],
            content_digest=str(proposal.get("content_digest") or ""),
            narrative_revision=int(proposal.get("narrative_revision") or 0),
            candidate_digest=candidate_digest,
            source_content_digest=str(
                proposal.get("source_content_digest") or ""
            ),
            expires_at=expires_at,
        )
        if not hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            str(proposal.get("token_digest") or ""),
        ):
            raise StaleStatePreview("State repair proposal token is invalid")
        reference_resolution = audit.get("reference_resolution")
        dropped = (
            reference_resolution.get("dropped")
            if isinstance(reference_resolution, dict)
            else None
        )
        dropped_count = 0
        if isinstance(dropped, dict):
            dropped_count = min(
                1_000,
                sum(
                    len(items) if isinstance(items, list) else int(bool(items))
                    for items in dropped.values()
                ),
            )
        try:
            context_projection = StateContextProjection.model_validate(
                audit.get("state_context_projection")
            )
        except Exception as exc:
            raise StaleStatePreview(
                "State repair context projection is invalid"
            ) from exc
        return RecoveredStateProposal(
            value={
                **deepcopy(candidate),
                "proposal_id": str(proposal["_id"]),
                "acceptance_token": token,
                "proposal_expires_at": expires_at.isoformat(),
            },
            truncated_section_count=(
                context_projection.truncated_section_count
            ),
            dropped_item_count=context_projection.dropped_item_count,
            dropped_reference_count=dropped_count,
        )

    async def recover_required_state_generation(
        self,
        binding: RequiredStateGenerationBinding,
    ) -> RecoveredStateProposal | None:
        """Recover one exact non-formal successor proposal after any TTL age.

        The binding only reconstructs the deferred proposal handle.  It is not
        accepted here and cannot be used as a formal mutation authorization.
        """

        try:
            frozen = RequiredStateGenerationBinding.model_validate(
                binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise StaleStatePreview(
                "Required state generation recovery binding is invalid"
            ) from exc
        novel = await novel_repo.collection.find_one(
            {
                "_id": to_object_id(frozen.novel_id),
                "owner_id": to_object_id(frozen.owner_id),
                "is_deleted": False,
            },
            projection={"_id": 1},
        )
        if novel is None:
            raise StaleStatePreview(
                "Required state generation owner scope is invalid"
            )
        proposal = await self.collection.find_one({
            "required_state_generation_key": frozen.recovery_key,
            "is_deleted": False,
        })
        if proposal is None:
            return None
        try:
            stored = _required_state_generation_binding(proposal)
        except MutationConflictError as exc:
            raise StaleStatePreview(str(exc)) from exc
        if stored != frozen or not _uses_current_dispatch_protocol(proposal):
            raise StaleStatePreview(
                "Required state generation recovery authority diverged"
            )
        status = str(proposal.get("status") or "")
        if status == "generating":
            released = await self.collection.update_one(
                {
                    "_id": proposal["_id"],
                    "status": "generating",
                    **_required_state_generation_binding_query(frozen),
                    "dispatch_protocol_revision": _dispatch_protocol_query(),
                    "is_deleted": False,
                },
                {
                    "$set": {
                        "status": "released_pre_dispatch",
                        "release_reason": (
                            "required_state_recovery_before_provider_dispatch"
                        ),
                        "released_at": get_utc_now(),
                        "updated_at": get_utc_now(),
                    },
                    "$unset": {
                        "required_state_generation_key": "",
                        "required_state_generation_binding": "",
                        "generation_audit.required_state_generation_binding": "",
                    },
                },
            )
            if released.modified_count == 1:
                return None
            raise StaleStatePreview(
                "Required state generation pre-dispatch recovery raced"
            )
        if status in {"dispatched", "failed"}:
            raise StaleStatePreview(
                "Required state generation result is unknown after dispatch"
            )
        if status != "proposed":
            raise StaleStatePreview(
                "Required state generation result is not recoverable"
            )
        candidate = proposal.get("candidate")
        audit = proposal.get("generation_audit")
        expires_at = proposal.get("acceptance_expires_at") or proposal.get(
            "expires_at"
        )
        if (
            not isinstance(candidate, dict)
            or not isinstance(audit, dict)
            or not isinstance(expires_at, datetime)
        ):
            raise StaleStatePreview(
                "Required state generation result projection is invalid"
            )
        candidate_digest = _digest(candidate)
        if candidate_digest != str(proposal.get("candidate_digest") or ""):
            raise StaleStatePreview(
                "Required state generation candidate digest diverged"
            )
        token = _proposal_acceptance_token(
            proposal_id=proposal["_id"],
            content_digest=str(proposal.get("content_digest") or ""),
            narrative_revision=int(proposal.get("narrative_revision") or 0),
            candidate_digest=candidate_digest,
            source_content_digest=str(
                proposal.get("source_content_digest") or ""
            ),
            expires_at=expires_at,
        )
        if not hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            str(proposal.get("token_digest") or ""),
        ):
            raise StaleStatePreview(
                "Required state generation token digest diverged"
            )
        reference_resolution = audit.get("reference_resolution")
        dropped = (
            reference_resolution.get("dropped")
            if isinstance(reference_resolution, dict)
            else None
        )
        dropped_count = 0
        if isinstance(dropped, dict):
            dropped_count = min(
                1_000,
                sum(
                    len(items) if isinstance(items, list) else int(bool(items))
                    for items in dropped.values()
                ),
            )
        try:
            context_projection = StateContextProjection.model_validate(
                audit.get("state_context_projection")
            )
        except Exception as exc:
            raise StaleStatePreview(
                "Required state generation context projection is invalid"
            ) from exc
        return RecoveredStateProposal(
            value={
                **deepcopy(candidate),
                "proposal_id": str(proposal["_id"]),
                "acceptance_token": token,
                "proposal_expires_at": expires_at.isoformat(),
            },
            truncated_section_count=(
                context_projection.truncated_section_count
            ),
            dropped_item_count=context_projection.dropped_item_count,
            dropped_reference_count=dropped_count,
        )

    async def capture(
        self,
        novel_id: str,
        chapter_id: str,
        *,
        chapter: dict[str, Any] | None = None,
        source_content_digest: str | None = None,
        source_prose_run_id: str | None = None,
        source_prose_run_revision: int | None = None,
        source_prose_acceptance_state: str | None = None,
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
            source_content_digest=(
                str(source_content_digest) if source_content_digest else None
            ),
            source_prose_run_id=(
                str(source_prose_run_id) if source_prose_run_id else None
            ),
            source_prose_run_revision=(
                int(source_prose_run_revision)
                if source_prose_run_revision is not None
                else None
            ),
            source_prose_acceptance_state=(
                str(source_prose_acceptance_state)
                if source_prose_acceptance_state
                else None
            ),
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
        required_state_generation_binding: (
            RequiredStateGenerationBinding | None
        ) = None,
    ) -> StateProposalLease:
        """Persist a generation lease before any Provider request is started."""
        active_snapshot = snapshot or await self.capture(
            novel_id, chapter_id, chapter=chapter
        )
        self._validate_snapshot_identity(active_snapshot, novel_id, chapter_id)
        await self.ensure_current(active_snapshot)
        generation_audit = deepcopy(audit or {})
        raw_job_binding = generation_audit.get("job_mutation_binding")
        job_binding: JobMutationRecoveryBindingV1 | None = None
        required_binding: RequiredStateGenerationBinding | None = None
        if raw_job_binding is not None:
            try:
                job_binding = JobMutationRecoveryBindingV1.model_validate(
                    raw_job_binding
                )
            except (TypeError, ValueError) as exc:
                raise StaleStatePreview(
                    "State proposal Job mutation binding is invalid"
                ) from exc
            if (
                job_binding.operation != "accept_chapter_state"
                or job_binding.novel_id != str(novel_id)
                or job_binding.chapter_id != str(chapter_id)
                or job_binding.expected_narrative_revision
                != active_snapshot.narrative_revision
            ):
                raise StaleStatePreview(
                    "State proposal Job mutation binding diverged"
                )
            existing = await self.collection.find_one({
                "job_mutation_key": job_binding.idempotency_key,
                "is_deleted": False,
            })
            if existing is not None:
                raise StaleStatePreview(
                    "State proposal Job result already exists and must be recovered"
                )
        raw_required_binding = generation_audit.get(
            "required_state_generation_binding"
        )
        if required_state_generation_binding is not None:
            try:
                required_binding = RequiredStateGenerationBinding.model_validate(
                    required_state_generation_binding.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise StaleStatePreview(
                    "Required state generation binding is invalid"
                ) from exc
            if raw_required_binding is None:
                generation_audit["required_state_generation_binding"] = (
                    required_binding.model_dump(mode="json")
                )
            else:
                try:
                    audited = RequiredStateGenerationBinding.model_validate(
                        raw_required_binding
                    )
                except (TypeError, ValueError) as exc:
                    raise StaleStatePreview(
                        "Required state generation audit binding is invalid"
                    ) from exc
                if audited != required_binding:
                    raise StaleStatePreview(
                        "Required state generation audit binding diverged"
                    )
        elif raw_required_binding is not None:
            raise StaleStatePreview(
                "Required state generation authority was not supplied"
            )
        if job_binding is not None and required_binding is not None:
            raise StaleStatePreview(
                "State proposal cannot mix formal and non-formal Job authority"
            )
        if required_binding is not None:
            if (
                required_binding.novel_id != str(novel_id)
                or required_binding.chapter_id != str(chapter_id)
                or required_binding.expected_narrative_revision
                != active_snapshot.narrative_revision
                or required_binding.source_run_id
                != active_snapshot.source_prose_run_id
                or required_binding.source_run_revision
                != active_snapshot.source_prose_run_revision
                or required_binding.source_content_digest
                != active_snapshot.source_content_digest
                or required_binding.can_accept_formal_state is not False
            ):
                raise StaleStatePreview(
                    "Required state generation source binding diverged"
                )
            owned_novel = await novel_repo.collection.find_one(
                {
                    "_id": to_object_id(required_binding.novel_id),
                    "owner_id": to_object_id(required_binding.owner_id),
                    "is_deleted": False,
                },
                projection={"_id": 1},
            )
            if owned_novel is None:
                raise StaleStatePreview(
                    "Required state generation owner scope is invalid"
                )
            existing = await self.collection.find_one({
                "required_state_generation_key": required_binding.recovery_key,
                "is_deleted": False,
            })
            if existing is not None:
                raise StaleStatePreview(
                    "Required state generation result must be recovered"
                )
        proposal_id = ObjectId()
        now = get_utc_now()
        document = {
            "_id": proposal_id,
            "novel_id": to_object_id(novel_id),
            "chapter_id": to_object_id(chapter_id),
            "status": "generating",
            "content_digest": active_snapshot.content_digest,
            "state_revision": active_snapshot.narrative_revision,
            "narrative_revision": active_snapshot.narrative_revision,
            "source_content_digest": active_snapshot.source_content_digest,
            "source_prose_run_id": active_snapshot.source_prose_run_id,
            "source_prose_run_revision": active_snapshot.source_prose_run_revision,
            "source_prose_acceptance_state": (
                active_snapshot.source_prose_acceptance_state
            ),
            "generation_captured_at": active_snapshot.captured_at,
            "generation_started_at": now,
            "dispatch_protocol_revision": STATE_PROPOSAL_DISPATCH_PROTOCOL_REVISION,
            "generation_audit": generation_audit,
            "expires_at": now + timedelta(seconds=PROPOSAL_TTL_SECONDS),
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            **(
                {
                    "job_mutation_key": job_binding.idempotency_key,
                    "job_mutation_binding": job_binding.model_dump(mode="json"),
                }
                if job_binding is not None
                else {}
            ),
            **(
                {
                    "required_state_generation_key": (
                        required_binding.recovery_key
                    ),
                    "required_state_generation_binding": (
                        required_binding.model_dump(mode="json")
                    ),
                }
                if required_binding is not None
                else {}
            ),
        }
        try:
            await self.collection.insert_one(document)
        except DuplicateKeyError as exc:
            raise StaleStatePreview(
                "State proposal Job result already exists and must be recovered"
            ) from exc
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
                {
                    "_id": lease.proposal_id,
                    "status": "dispatched",
                    "dispatch_protocol_revision": _dispatch_protocol_query(),
                },
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
        if (
            not current
            or current.get("status") != "dispatched"
            or not _uses_current_dispatch_protocol(current)
        ):
            raise StaleStatePreview("Generation lease is missing or no longer active")
        prepared = add_selection_ids(candidate)
        expires_at = current.get("acceptance_expires_at") or current.get(
            "expires_at"
        )
        if not isinstance(expires_at, datetime):
            raise StaleStatePreview("Generation lease has no valid expiry")
        candidate_digest = _digest(prepared)
        token = _proposal_acceptance_token(
            proposal_id=lease.proposal_id,
            content_digest=lease.snapshot.content_digest,
            narrative_revision=lease.snapshot.narrative_revision,
            candidate_digest=candidate_digest,
            source_content_digest=lease.snapshot.source_content_digest or "",
            expires_at=expires_at,
        )
        generation_audit, job_binding = _merged_generation_audit(current, audit)
        required_binding = _required_state_generation_binding(current)
        update: dict[str, Any] = {
            "$set": {
                "status": "proposed",
                "candidate": prepared,
                "candidate_digest": candidate_digest,
                "token_digest": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "generation_audit": generation_audit,
                "proposed_at": get_utc_now(),
                "updated_at": get_utc_now(),
                **(
                    {"acceptance_expires_at": expires_at}
                    if job_binding is not None or required_binding is not None
                    else {}
                ),
            }
        }
        if job_binding is not None or required_binding is not None:
            update["$unset"] = {"expires_at": ""}
        publish_query: dict[str, Any] = {
            "_id": lease.proposal_id,
            "status": "dispatched",
            "dispatch_protocol_revision": _dispatch_protocol_query(),
        }
        if job_binding is not None:
            publish_query.update(_job_mutation_binding_query(job_binding))
        if required_binding is not None:
            publish_query.update(
                _required_state_generation_binding_query(required_binding)
            )
        published = await self.collection.find_one_and_update(
            publish_query,
            update,
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
        if current is None or not _uses_current_dispatch_protocol(current):
            return
        generation_audit, job_binding = _merged_generation_audit(current, audit)
        required_binding = _required_state_generation_binding(current)
        now = get_utc_now()
        if current.get("status") == "generating" and (
            job_binding is not None or required_binding is not None
        ):
            authority_query = (
                _job_mutation_binding_query(job_binding)
                if job_binding is not None
                else _required_state_generation_binding_query(required_binding)
            )
            unset_fields = (
                {"job_mutation_key": ""}
                if job_binding is not None
                else {
                    "required_state_generation_key": "",
                    "required_state_generation_binding": "",
                }
            )
            released_audit = generation_audit
            if required_binding is not None:
                released_audit = dict(generation_audit)
                released_audit.pop("required_state_generation_binding", None)
            await self.collection.update_one(
                {
                    "_id": lease.proposal_id,
                    "status": "generating",
                    **authority_query,
                    "dispatch_protocol_revision": _dispatch_protocol_query(),
                },
                {
                    "$set": {
                        "status": "released_pre_dispatch",
                        "failure": {
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                        "generation_audit": released_audit,
                        "release_reason": "state_generation_failed_before_dispatch",
                        "released_at": now,
                        "updated_at": now,
                    },
                    "$unset": unset_fields,
                },
            )
            return
        failure_query: dict[str, Any] = {
            "_id": lease.proposal_id,
            "status": {"$in": ["generating", "dispatched"]},
            "dispatch_protocol_revision": _dispatch_protocol_query(),
        }
        if job_binding is not None:
            failure_query.update(_job_mutation_binding_query(job_binding))
        if required_binding is not None:
            failure_query.update(
                _required_state_generation_binding_query(required_binding)
            )
        await self.collection.update_one(
            failure_query,
            {
                "$set": {
                    "status": "failed",
                    "failure": {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "generation_audit": generation_audit,
                    "failed_at": now,
                    "updated_at": now,
                }
            },
        )

    async def record_pre_dispatch_projection(
        self,
        lease: StateProposalLease,
        projection: StateContextProjection,
    ) -> None:
        """Persist the bounded recovery projection before Provider dispatch."""
        value = StateContextProjection.model_validate(
            projection
        ).model_dump(mode="json")
        current = await self.collection.find_one({"_id": lease.proposal_id})
        if current is None:
            raise StaleStatePreview(
                "State generation pre-dispatch projection lost its lease"
            )
        job_binding = _job_mutation_binding(current)
        required_binding = _required_state_generation_binding(current)
        projection_query: dict[str, Any] = {
            "_id": lease.proposal_id,
            "status": "generating",
            "content_digest": lease.snapshot.content_digest,
            "narrative_revision": lease.snapshot.narrative_revision,
            "dispatch_protocol_revision": _dispatch_protocol_query(),
            "is_deleted": False,
        }
        if job_binding is not None:
            projection_query.update(_job_mutation_binding_query(job_binding))
        if required_binding is not None:
            projection_query.update(
                _required_state_generation_binding_query(required_binding)
            )
        result = await self.collection.update_one(
            projection_query,
            {
                "$set": {
                    "generation_audit.state_context_projection": value,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if result.matched_count != 1:
            raise StaleStatePreview(
                "State generation pre-dispatch projection was not persisted"
            )

    async def mark_dispatched(self, lease: StateProposalLease) -> None:
        """Freeze the Provider dispatch boundary before starting paid work."""

        try:
            await self.ensure_current(lease.snapshot)
        except StaleStatePreview as exc:
            await self.collection.update_one(
                {
                    "_id": lease.proposal_id,
                    "status": "generating",
                    "dispatch_protocol_revision": _dispatch_protocol_query(),
                },
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
        if (
            current is None
            or current.get("status") != "generating"
            or not _uses_current_dispatch_protocol(current)
        ):
            raise StaleStatePreview(
                "State generation lease could not cross the dispatch boundary"
            )
        job_binding = _job_mutation_binding(current)
        required_binding = _required_state_generation_binding(current)
        now = get_utc_now()
        query: dict[str, Any] = {
            "_id": lease.proposal_id,
            "status": "generating",
            "content_digest": lease.snapshot.content_digest,
            "narrative_revision": lease.snapshot.narrative_revision,
            "dispatch_protocol_revision": _dispatch_protocol_query(),
            "is_deleted": False,
        }
        update: dict[str, Any] = {
            "$set": {
                "status": "dispatched",
                "dispatched_at": now,
                "updated_at": now,
            }
        }
        if job_binding is not None or required_binding is not None:
            acceptance_expires_at = current.get("expires_at")
            if not isinstance(acceptance_expires_at, datetime):
                raise StaleStatePreview(
                    "State generation Job lease has no valid acceptance expiry"
                )
            if job_binding is not None:
                query.update(_job_mutation_binding_query(job_binding))
            if required_binding is not None:
                query.update(
                    _required_state_generation_binding_query(required_binding)
                )
            update["$set"]["acceptance_expires_at"] = acceptance_expires_at
            update["$unset"] = {"expires_at": ""}
        dispatched = await self.collection.update_one(
            query,
            update,
        )
        if dispatched.matched_count != 1:
            raise StaleStatePreview(
                "State generation lease could not cross the dispatch boundary"
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
        generation_audit, job_binding = _merged_generation_audit(current, audit)
        required_binding = _required_state_generation_binding(current)
        query: dict[str, Any] = {
            "_id": lease.proposal_id,
            "status": {"$in": ["generating", "dispatched", "proposed"]},
        }
        if job_binding is not None:
            query.update(_job_mutation_binding_query(job_binding))
            query["dispatch_protocol_revision"] = _dispatch_protocol_query()
        if required_binding is not None:
            query.update(
                _required_state_generation_binding_query(required_binding)
            )
            query["dispatch_protocol_revision"] = _dispatch_protocol_query()
        updated = await self.collection.update_one(
            query,
            {
                "$set": {
                    "generation_audit": generation_audit,
                    "updated_at": get_utc_now(),
                }
            },
        )
        if updated.matched_count != 1:
            raise StaleStatePreview(
                "Generation audit lost its persisted Job authority"
            )

    async def stream_preview(
        self,
        lease: StateProposalLease,
        frames: AsyncIterable[str],
        *,
        roster: dict[str, Any],
        prose: str | None = None,
        state_step: str = "chapter_state",
    ) -> AsyncIterator[str]:
        """Own SSE candidate validation, publication, auditing, and failure state."""
        proposal_payload: dict[str, Any] | None = None
        generation_audit: dict[str, Any] = {}
        reported_invalid_ids = False
        reported_remapped_ids = False
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

                resolved, remapped = resolve_state_character_references(
                    candidate,
                    roster,
                )
                cleaned, dropped = validate_state_ids(resolved, roster)
                if prose is not None:
                    known = known_id_sets(roster)
                    cleaned["fact_evidence"] = validate_state_fact_evidence(
                        candidate.get("fact_evidence"),
                        candidate=cleaned,
                        prose=prose,
                        binding={
                            "chapter_id": lease.snapshot.chapter_id,
                            "source_prose_run_id": (
                                lease.snapshot.source_prose_run_id
                            ),
                            "source_prose_run_revision": (
                                lease.snapshot.source_prose_run_revision
                            ),
                            "source_content_digest": (
                                lease.snapshot.source_content_digest
                                or chapter_content_digest(prose)
                            ),
                        },
                        known_character_ids=known["characters"],
                        known_thread_ids=known["threads"],
                    )
                    generation_audit["state_fact_evidence"] = {
                        "schema_version": cleaned["fact_evidence"][
                            "evidence_schema_version"
                        ],
                        "evidence_digest": cleaned["fact_evidence"][
                            "evidence_digest"
                        ],
                        "extraction_status": cleaned["fact_evidence"][
                            "extraction_status"
                        ],
                        "invalid_internal_references": cleaned[
                            "fact_evidence"
                        ]["invalid_internal_references"],
                        "dangling_references": cleaned["fact_evidence"][
                            "dangling_references"
                        ],
                    }
                generation_audit["reference_resolution"] = (
                    state_reference_resolution(
                        candidate,
                        cleaned,
                        dropped,
                        remapped,
                    )
                )
                if remapped and not reported_remapped_ids:
                    yield sse_event("id_remapping", {"remapped": remapped})
                    reported_remapped_ids = True
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
        await self.mark_dispatched(lease)
        return await self.publish(lease, candidate, audit=audit)

    async def _load_verified_proposal(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
        job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
        required_state_generation_binding: (
            RequiredStateGenerationBinding | None
        ) = None,
    ) -> dict[str, Any]:
        proposal = await self.collection.find_one({"_id": to_object_id(proposal_id)})
        if not proposal:
            raise StateProposalCompletionFailure(
                "State proposal is missing",
                reason="evidence_invalid",
            )
        proposal_status = str(proposal.get("status") or "")
        if proposal_status not in {
            "proposed",
            "claimed",
            "applied",
            "expired",
        }:
            raise StaleStatePreview("State proposal is not available for acceptance")
        stored_job_binding = _job_mutation_binding(proposal)
        stored_required_binding = _required_state_generation_binding(proposal)
        if stored_job_binding is not None and not _uses_current_dispatch_protocol(
            proposal
        ):
            raise StateProposalCompletionFailure(
                "State proposal Provider dispatch evidence is unknown",
                reason="evidence_invalid",
            )
        required_handoff = False
        if stored_required_binding is not None:
            if (
                required_state_generation_binding is None
                or job_mutation_binding is None
            ):
                raise StaleStatePreview(
                    "Required state proposal needs a formal successor authority"
                )
            try:
                supplied_required_binding = (
                    RequiredStateGenerationBinding.model_validate(
                        required_state_generation_binding.model_dump(
                            mode="python"
                        )
                    )
                )
                supplied_finalization_binding = (
                    JobMutationRecoveryBindingV1.model_validate(
                        job_mutation_binding.model_dump(mode="python")
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise StaleStatePreview(
                    "Required state proposal finalization authority is invalid"
                ) from exc
            _validate_required_state_finalization_handoff(
                proposal,
                required_binding=supplied_required_binding,
                finalization_binding=supplied_finalization_binding,
            )
            required_handoff = True
        elif required_state_generation_binding is not None:
            raise StaleStatePreview(
                "State proposal has no required successor binding"
            )
        if proposal_status == "expired" and not required_handoff:
            raise StaleStatePreview("State proposal is not available for acceptance")
        allow_expired = required_handoff
        if job_mutation_binding is not None:
            try:
                supplied_job_binding = JobMutationRecoveryBindingV1.model_validate(
                    job_mutation_binding.model_dump(mode="python")
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise StaleStatePreview(
                    "State proposal Job mutation binding is invalid"
                ) from exc
            if (
                stored_job_binding != supplied_job_binding
                and not required_handoff
            ):
                raise StaleStatePreview(
                    "State proposal belongs to another Job authorization"
                )
            allow_expired = True
        now = datetime.now(timezone.utc)
        expires_at = proposal.get("expires_at") or proposal.get(
            "acceptance_expires_at"
        )
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or (
            expires_at <= now and not allow_expired
        ):
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
            raise StateProposalCompletionFailure(
                "State proposal token is invalid",
                reason="evidence_invalid",
            )
        return proposal

    async def prepare_decision(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
        selected_character_ids: list[str] | None = None,
        selected_fact_ids: list[str],
        selected_thread_ids: list[str],
        drop_reasons: Mapping[str, str] | None = None,
        edits: dict[str, Any] | None = None,
        policy_name: str = "human_review",
        policy_version: str = "1",
        job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
        required_state_generation_binding: (
            RequiredStateGenerationBinding | None
        ) = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Validate a decision without consuming the proposal or creating a gap."""
        proposal = await self._load_verified_proposal(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            job_mutation_binding=job_mutation_binding,
            required_state_generation_binding=(
                required_state_generation_binding
            ),
        )
        novel_id = str(proposal["novel_id"])
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        revalidate_deferred_source = (
            proposal.get("status") == "proposed"
            or (
                proposal.get("status") == "expired"
                and required_state_generation_binding is not None
            )
        )
        if revalidate_deferred_source:
            if _content_digest(chapter) != proposal.get("content_digest"):
                await self.collection.update_one(
                    {
                        "_id": proposal["_id"],
                        "status": proposal.get("status"),
                    },
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
                    {
                        "_id": proposal["_id"],
                        "status": proposal.get("status"),
                    },
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
        characters_by_id = {
            character["selection_id"]: character
            for character in candidate.get("character_updates") or []
        }
        facts_by_id = {
            fact["selection_id"]: (character, fact)
            for character in candidate.get("character_updates") or []
            for fact in character.get("new_permanent_facts") or []
        }
        threads_by_id = {
            thread["selection_id"]: thread
            for thread in candidate.get("thread_updates") or []
        }
        effective_character_ids = (
            list(characters_by_id)
            if selected_character_ids is None
            and not isinstance(candidate.get("fact_evidence"), Mapping)
            else list(selected_character_ids or [])
        )
        if not set(effective_character_ids).issubset(characters_by_id):
            raise StaleStatePreview("Unknown character-state selection id")
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
        if not isinstance(current_state_edits, Mapping):
            raise StaleStatePreview("Current-state edits must be an object")

        fact_accounting: dict[str, Any] | None = None
        validated_fact_evidence = candidate.get("fact_evidence")
        fact_summary_accepted = True
        if isinstance(validated_fact_evidence, Mapping):
            reference_resolution = (
                (proposal.get("generation_audit") or {}).get(
                    "reference_resolution"
                )
                or {}
            )
            dropped_references = (
                reference_resolution.get("dropped")
                if isinstance(reference_resolution, Mapping)
                else {}
            )
            has_dropped_references = bool(
                isinstance(dropped_references, Mapping)
                and any(dropped_references.values())
            ) or bool(
                isinstance(reference_resolution, Mapping)
                and (
                    int(
                        reference_resolution.get(
                            "dropped_character_update_count"
                        )
                        or 0
                    )
                    > 0
                    or int(
                        reference_resolution.get(
                            "dropped_thread_update_count"
                        )
                        or 0
                    )
                    > 0
                )
            )
            if has_dropped_references:
                raise StateProposalCompletionFailure(
                    "状态候选包含已清洗的无效内部引用，必须重新生成",
                    reason="internal_reference_invalid",
                )

            candidate_states = {
                str(character.get("card_id") or ""): str(
                    character.get("current_state") or ""
                )
                for character in candidate.get("character_updates") or []
            }
            summary_changed = (
                "summary" in allowed_edits
                and str(allowed_edits["summary"])
                != str(candidate.get("summary") or "")
            )
            states_changed = any(
                card_id not in candidate_states
                or str(value) != candidate_states[card_id]
                for card_id, value in current_state_edits.items()
            )
            if summary_changed or states_changed:
                raise StaleStatePreview(
                    "人工编辑已脱离当前事实证据，请重新生成状态候选"
                )
            # Exact no-op echoes from older clients are harmless, but must not
            # be recorded as edits or alter the digest-sealed candidate.
            allowed_edits = {}
            current_state_edits = {}
            try:
                fact_accounting = account_state_fact_evidence(
                    validated_fact_evidence,
                    candidate=candidate,
                    selected_character_ids=effective_character_ids,
                    selected_fact_ids=selected_fact_ids,
                    selected_thread_ids=selected_thread_ids,
                    drop_reasons=drop_reasons,
                )
            except StateFactAccountingError as exc:
                raise StateProposalCompletionFailure(
                    str(exc),
                    reason="evidence_invalid",
                ) from exc
            source_binding = fact_accounting["source_binding"]
            if (
                str(source_binding.get("chapter_id") or "") != chapter_id
                or str(source_binding.get("source_content_digest") or "")
                != str(proposal.get("source_content_digest") or "")
                or source_binding.get("source_prose_run_id")
                != proposal.get("source_prose_run_id")
                or source_binding.get("source_prose_run_revision")
                != proposal.get("source_prose_run_revision")
            ):
                raise StateProposalCompletionFailure(
                    "State fact accounting is not bound to this proposal",
                    reason="source_binding_stale",
                )
            if not fact_accounting["gate_passed"]:
                if fact_accounting.get("extraction_failure_count"):
                    failure_reason = "semantic_unknown"
                elif (
                    fact_accounting.get("invalid_internal_references")
                    or fact_accounting.get("dangling_references")
                ):
                    failure_reason = "internal_reference_invalid"
                elif fact_accounting.get("unaccounted_canonical_facts"):
                    failure_reason = "canonical_fact_unaccounted"
                else:
                    failure_reason = "evidence_invalid"
                raise StateProposalCompletionFailure(
                    "状态候选仍有未核算正式事实或非法内部引用",
                    reason=failure_reason,
                )
            fact_summary_accepted = any(
                action.get("action_type") == "chapter_summary"
                and action.get("decision") == "accepted"
                for action in fact_accounting.get("action_accounts") or []
            )
            if (
                not fact_summary_accepted
                and str(chapter.get("summary") or "").strip()
                and chapter_content_digest(chapter.get("content") or "")
                != str(proposal.get("source_content_digest") or "")
            ):
                raise StaleStatePreview(
                    "旧摘要没有绑定当前正文候选，必须重新生成受支持的摘要"
                )

        payload: dict[str, Any] = {
            "summary": str(
                (
                    allowed_edits.get(
                        "summary", candidate.get("summary") or ""
                    )
                    if fact_summary_accepted
                    else ""
                )
            ),
            **({"write_summary": False} if not fact_summary_accepted else {}),
            "character_updates": [],
            "accepted_thread_updates": [],
        }
        for character in candidate.get("character_updates") or []:
            card_id = str(character["card_id"])
            character_selected = (
                character["selection_id"] in effective_character_ids
            )
            selected = [
                {key: value for key, value in fact.items() if key != "selection_id"}
                for fact in character.get("new_permanent_facts") or []
                if fact["selection_id"] in selected_fact_ids
            ]
            if not character_selected and not selected:
                continue
            payload["character_updates"].append(
                {
                    "card_id": card_id,
                    "write_current_state": character_selected,
                    "current_state": (
                        str(
                            current_state_edits.get(
                                card_id,
                                character.get("current_state") or "",
                            )
                        )
                        if character_selected
                        else ""
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
        stored_job_binding = _job_mutation_binding(proposal)
        required_binding = _required_state_generation_binding(proposal)
        job_binding = stored_job_binding
        if required_binding is not None:
            if (
                required_state_generation_binding is None
                or job_mutation_binding is None
            ):
                raise StaleStatePreview(
                    "Required state proposal needs a formal successor authority"
                )
            job_binding = JobMutationRecoveryBindingV1.model_validate(
                job_mutation_binding.model_dump(mode="python")
            )
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
                "selected_character_state_count": len(
                    effective_character_ids
                ),
                "rejected_character_state_count": max(
                    0,
                    len(characters_by_id) - len(effective_character_ids),
                ),
            },
            "confidence": (
                "human_reviewed" if policy_name == "human_review" else "auto_accepted"
            ),
            **(
                {"job_mutation_binding": job_binding.model_dump(mode="json")}
                if job_binding is not None
                else {}
            ),
            **(
                {
                    "required_state_generation_binding": (
                        required_binding.model_dump(mode="json")
                    )
                }
                if required_binding is not None
                else {}
            ),
            "state_completion": {
                "source_content_digest": (
                    proposal.get("source_content_digest")
                    or chapter_content_digest(chapter.get("content") or "")
                ),
                "source_prose_acceptance_state": (
                    proposal.get("source_prose_acceptance_state")
                    or prose_acceptance_state(chapter)
                ),
                "source_prose_run_id": proposal.get("source_prose_run_id"),
                "source_prose_run_revision": proposal.get(
                    "source_prose_run_revision"
                ),
                "reference_resolution": deepcopy(
                    (proposal.get("generation_audit") or {}).get(
                        "reference_resolution"
                    )
                    or {}
                ),
                **(
                    {"fact_accounting": deepcopy(fact_accounting)}
                    if fact_accounting is not None
                    else {}
                ),
            },
        }
        decision_digest = _digest(
            {
                "payload": payload,
                "candidate_digest": proposal.get("candidate_digest"),
                "policy": policy,
                "fact_accounting": fact_accounting,
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
            **(
                {
                    "claim_id": job_binding.idempotency_key,
                    "job_mutation_binding": job_binding.model_dump(mode="json"),
                }
                if job_binding is not None
                else {}
            ),
            **(
                {
                    "required_state_generation_binding": (
                        required_binding.model_dump(mode="json")
                    )
                }
                if required_binding is not None
                else {}
            ),
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
        selected_character_ids: list[str] | None = None,
        selected_fact_ids: list[str],
        selected_thread_ids: list[str],
        drop_reasons: Mapping[str, str] | None = None,
        edits: dict[str, Any] | None = None,
        policy_name: str = "human_review",
        policy_version: str = "1",
        job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
    ) -> dict[str, Any]:
        payload, metadata, claim = await self.prepare_decision(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            selected_character_ids=selected_character_ids,
            selected_fact_ids=selected_fact_ids,
            selected_thread_ids=selected_thread_ids,
            drop_reasons=drop_reasons,
            edits=edits,
            policy_name=policy_name,
            policy_version=policy_version,
            job_mutation_binding=job_mutation_binding,
        )
        # Lazy import keeps the proposal lifecycle independent of timeline writes.
        from backend.services.novel.chapter_state_service import ChapterStateService
        from backend.db.mutation import resume_persisted_mutation

        proposal = await self.collection.find_one(
            {"_id": to_object_id(proposal_id)}
        )
        if proposal and proposal.get("status") in {"claimed", "applied"}:
            stored_claim = proposal.get("claim") or {}
            if stored_claim.get("decision_digest") != claim.get("decision_digest"):
                raise MutationConflictError(
                    "State proposal is already claimed by a different decision"
                )
            journal = await get_database()[collections.MUTATION_JOURNALS].find_one(
                {
                    "novel_id": proposal["novel_id"],
                    "idempotency_key": str(
                        stored_claim.get("claim_id")
                        or claim.get("claim_id")
                        or f"accept-state-proposal:{proposal_id}"
                    ),
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
        policy: SelectAllPolicy | FactAccountingPolicy,
        job_mutation_binding: JobMutationRecoveryBindingV1 | None = None,
    ) -> dict[str, Any]:
        """Apply a versioned automatic decision through the normal accept path."""
        proposal_id = str(proposal.get("proposal_id") or "")
        acceptance_token = str(proposal.get("acceptance_token") or "")
        if not proposal_id or not acceptance_token:
            raise ValueError("Automatic state acceptance requires a proposal handle")
        stored = await self._load_verified_proposal(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            job_mutation_binding=job_mutation_binding,
        )
        candidate = deepcopy(stored.get("candidate") or {})
        policy_decision = policy.decide(candidate)
        return await self.accept(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            selected_character_ids=list(
                policy_decision.selected_character_ids
            ),
            selected_fact_ids=list(policy_decision.selected_fact_ids),
            selected_thread_ids=list(policy_decision.selected_thread_ids),
            drop_reasons=dict(policy_decision.drop_reasons),
            policy_name=policy.name,
            policy_version=policy.version,
            job_mutation_binding=job_mutation_binding,
        )

    @staticmethod
    def _validated_job_dispatch_binding(
        binding: JobMutationRecoveryBindingV1,
    ) -> JobMutationRecoveryBindingV1:
        try:
            frozen = JobMutationRecoveryBindingV1.model_validate(
                binding.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise MutationConflictError(
                "State proposal Job mutation binding is invalid"
            ) from exc
        if frozen.operation != "accept_chapter_state":
            raise MutationConflictError(
                "State proposal recovery operation is invalid"
            )
        return frozen

    async def _load_job_bound_proposal(
        self,
        binding: JobMutationRecoveryBindingV1,
    ) -> tuple[JobMutationRecoveryBindingV1, dict[str, Any] | None]:
        frozen = self._validated_job_dispatch_binding(binding)
        proposal = await self.collection.find_one({
            "job_mutation_key": frozen.idempotency_key,
            "is_deleted": False,
        })
        if proposal is None:
            return frozen, None
        try:
            stored = JobMutationRecoveryBindingV1.model_validate(
                proposal.get("job_mutation_binding")
            )
        except (TypeError, ValueError) as exc:
            raise MutationConflictError(
                "Persisted state proposal Job binding is invalid"
            ) from exc
        if stored != frozen or _job_mutation_binding(proposal) != frozen:
            raise MutationConflictError(
                "Persisted state proposal belongs to another Job authorization"
            )
        return frozen, proposal

    async def has_unresolved_job_dispatch(
        self,
        binding: JobMutationRecoveryBindingV1,
    ) -> bool:
        """Return whether one exact Job receipt needs an explicit resolution."""

        _frozen, proposal = await self._load_job_bound_proposal(binding)
        if proposal is None:
            return False
        if not _uses_current_dispatch_protocol(proposal):
            raise MutationConflictError(
                "Persisted state Provider dispatch evidence is unknown"
            )
        return str(proposal.get("status") or "") in {
            "dispatched",
            "failed",
            "uncertain_retry_acknowledged",
            "uncertain_skip_acknowledged",
            "uncertain_abort_acknowledged",
        }

    async def pending_job_bound_dispatch_action(
        self,
        binding: JobMutationRecoveryBindingV1,
    ) -> str | None:
        """Recover an action frozen on the proposal before the Job intent exists."""

        _frozen, proposal = await self._load_job_bound_proposal(binding)
        if proposal is None:
            return None
        if not _uses_current_dispatch_protocol(proposal):
            raise MutationConflictError(
                "Persisted state Provider dispatch evidence is unknown"
            )
        status = str(proposal.get("status") or "")
        if not status.startswith("uncertain_"):
            return None
        resolution = proposal.get("dispatch_resolution")
        if not isinstance(resolution, Mapping):
            raise MutationConflictError(
                "Persisted state Provider dispatch action is invalid"
            )
        action = resolution.get("action")
        if (
            not isinstance(action, str)
            or action not in STATE_DISPATCH_RESOLUTION_ACTIONS
            or status != f"uncertain_{action}_acknowledged"
        ):
            raise MutationConflictError(
                "Persisted state Provider dispatch action diverged"
            )
        return action

    async def acknowledge_job_bound_dispatch(
        self,
        binding: JobMutationRecoveryBindingV1,
        action: str,
    ) -> bool:
        """Persist one explicit retry/skip/abort decision without releasing its key."""

        if action not in STATE_DISPATCH_RESOLUTION_ACTIONS:
            raise ValueError("Unknown state Provider dispatch resolution")
        frozen, proposal = await self._load_job_bound_proposal(binding)
        if proposal is None:
            return False
        if not _uses_current_dispatch_protocol(proposal):
            raise MutationConflictError(
                "Persisted state Provider dispatch evidence is unknown"
            )
        expected_status = f"uncertain_{action}_acknowledged"
        status = str(proposal.get("status") or "")
        allowed_statuses = {"dispatched", "failed", expected_status}
        if action == "abort":
            allowed_statuses.update({"generating", "proposed"})
        if status not in allowed_statuses:
            raise MutationConflictError(
                "Persisted state Provider dispatch cannot accept this resolution"
            )
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": proposal["_id"],
                "status": status,
                **_job_mutation_binding_query(frozen),
                "dispatch_protocol_revision": _dispatch_protocol_query(),
                "is_deleted": False,
            },
            {
                "$set": {
                    "status": expected_status,
                    "dispatch_resolution": {
                        "action": action,
                        "acknowledged_at": now,
                    },
                    "updated_at": now,
                },
                "$unset": {
                    "expires_at": "",
                    "acceptance_expires_at": "",
                },
            },
        )
        if result.modified_count == 1:
            return True
        _frozen, current = await self._load_job_bound_proposal(frozen)
        if current is not None and current.get("status") == expected_status:
            return True
        raise MutationConflictError(
            "Persisted state Provider dispatch resolution raced"
        )

    async def release_job_bound_dispatch(
        self,
        binding: JobMutationRecoveryBindingV1,
        action: str,
    ) -> bool:
        """Release only a receipt carrying the same explicit resolution action."""

        if action not in STATE_DISPATCH_RESOLUTION_ACTIONS:
            raise ValueError("Unknown state Provider dispatch resolution")
        frozen, proposal = await self._load_job_bound_proposal(binding)
        if proposal is None:
            return False
        if not _uses_current_dispatch_protocol(proposal):
            raise MutationConflictError(
                "Persisted state Provider dispatch evidence is unknown"
            )
        expected_status = f"uncertain_{action}_acknowledged"
        if proposal.get("status") != expected_status:
            raise MutationConflictError(
                "Persisted state Provider dispatch resolution is not acknowledged"
            )
        now = get_utc_now()
        result = await self.collection.update_one(
            {
                "_id": proposal["_id"],
                "status": expected_status,
                **_job_mutation_binding_query(frozen),
                "dispatch_protocol_revision": _dispatch_protocol_query(),
                "is_deleted": False,
            },
            {
                "$set": {
                    "dispatch_resolution.released_at": now,
                    "expires_at": now + timedelta(seconds=PROPOSAL_TTL_SECONDS),
                    "updated_at": now,
                },
                "$unset": {"job_mutation_key": ""},
            },
        )
        if result.modified_count == 1:
            return True
        raise MutationConflictError(
            "Persisted state Provider dispatch resolution release raced"
        )

    async def recover_job_bound_result(
        self,
        binding: JobMutationRecoveryBindingV1,
    ) -> dict[str, Any] | None:
        """Release pre-dispatch work or load one exact published Job result."""

        frozen, proposal = await self._load_job_bound_proposal(binding)
        if proposal is None:
            return None
        status = str(proposal.get("status") or "")
        if not _uses_current_dispatch_protocol(proposal):
            raise MutationConflictError(
                "Persisted state Provider dispatch evidence is unknown"
            )
        if status == "generating":
            now = get_utc_now()
            released = await self.collection.update_one(
                {
                    "_id": proposal["_id"],
                    "status": "generating",
                    "job_mutation_key": frozen.idempotency_key,
                    "job_mutation_binding": frozen.model_dump(mode="json"),
                    "dispatch_protocol_revision": _dispatch_protocol_query(),
                    "is_deleted": False,
                },
                {
                    "$set": {
                        "status": "released_pre_dispatch",
                        "release_reason": "job_recovery_before_provider_dispatch",
                        "released_at": now,
                        "updated_at": now,
                    },
                    "$unset": {"job_mutation_key": ""},
                },
            )
            if released.modified_count == 1:
                return None
            proposal = await self.collection.find_one({"_id": proposal["_id"]})
            if proposal is None or proposal.get("is_deleted") is True:
                raise MutationConflictError(
                    "Persisted state proposal disappeared during recovery"
                )
            if _job_mutation_binding(proposal) != frozen:
                raise MutationConflictError(
                    "Persisted state proposal changed authorization during recovery"
                )
            if not _uses_current_dispatch_protocol(proposal):
                raise MutationConflictError(
                    "Persisted state Provider dispatch evidence is unknown"
                )
            status = str(proposal.get("status") or "")
            if status == "released_pre_dispatch" and not proposal.get(
                "job_mutation_key"
            ):
                return None
        if status == "uncertain_retry_acknowledged":
            await self.release_job_bound_dispatch(frozen, "retry")
            return None
        if status in {
            "uncertain_skip_acknowledged",
            "uncertain_abort_acknowledged",
        }:
            raise MutationConflictError(
                "Persisted state Provider dispatch was explicitly terminated"
            )
        if status in {"dispatched", "failed"}:
            raise MutationConflictError(
                "Persisted state Provider result is unknown after dispatch"
            )
        if status != "proposed":
            raise MutationConflictError(
                "Persisted state Provider result is not recoverable"
            )
        candidate = proposal.get("candidate")
        expires_at = proposal.get("acceptance_expires_at") or proposal.get(
            "expires_at"
        )
        if not isinstance(candidate, dict) or not isinstance(expires_at, datetime):
            raise MutationConflictError(
                "Persisted state Provider result projection is invalid"
            )
        candidate_digest = _digest(candidate)
        if candidate_digest != str(proposal.get("candidate_digest") or ""):
            raise MutationConflictError(
                "Persisted state Provider result digest diverged"
            )
        token = _proposal_acceptance_token(
            proposal_id=proposal["_id"],
            content_digest=str(proposal.get("content_digest") or ""),
            narrative_revision=int(proposal.get("narrative_revision") or 0),
            candidate_digest=candidate_digest,
            source_content_digest=str(
                proposal.get("source_content_digest") or ""
            ),
            expires_at=expires_at,
        )
        if not hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            str(proposal.get("token_digest") or ""),
        ):
            raise MutationConflictError(
                "Persisted state Provider result token digest diverged"
            )
        return {
            **deepcopy(candidate),
            "proposal_id": str(proposal["_id"]),
            "acceptance_token": token,
            "proposal_expires_at": expires_at.isoformat(),
        }

    async def prepare_policy_decision(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
        policy: SelectAllPolicy | FactAccountingPolicy,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Prepare a policy decision from the persisted candidate, never caller data."""
        stored = await self._load_verified_proposal(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
        )
        candidate = deepcopy(stored.get("candidate") or {})
        policy_decision = policy.decide(candidate)
        return await self.prepare_decision(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            selected_character_ids=list(
                policy_decision.selected_character_ids
            ),
            selected_fact_ids=list(policy_decision.selected_fact_ids),
            selected_thread_ids=list(policy_decision.selected_thread_ids),
            drop_reasons=dict(policy_decision.drop_reasons),
            policy_name=policy.name,
            policy_version=policy.version,
        )

    async def prepare_required_state_policy_decision(
        self,
        *,
        chapter_id: str,
        proposal_id: str,
        acceptance_token: str,
        policy: SelectAllPolicy | FactAccountingPolicy,
        required_state_generation_binding: RequiredStateGenerationBinding,
        finalization_binding: JobMutationRecoveryBindingV1,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Consume one deferred state result under a new formal authority."""

        stored = await self._load_verified_proposal(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            job_mutation_binding=finalization_binding,
            required_state_generation_binding=(
                required_state_generation_binding
            ),
        )
        candidate = deepcopy(stored.get("candidate") or {})
        policy_decision = policy.decide(candidate)
        return await self.prepare_decision(
            chapter_id=chapter_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
            selected_character_ids=list(
                policy_decision.selected_character_ids
            ),
            selected_fact_ids=list(policy_decision.selected_fact_ids),
            selected_thread_ids=list(policy_decision.selected_thread_ids),
            drop_reasons=dict(policy_decision.drop_reasons),
            policy_name=policy.name,
            policy_version=policy.version,
            job_mutation_binding=finalization_binding,
            required_state_generation_binding=(
                required_state_generation_binding
            ),
        )

    async def claim_for_mutation(
        self,
        claim: dict[str, Any],
        *,
        mutation_revision: int,
        session: Any = None,
        persisted_finalization_intent: bool = False,
    ) -> None:
        proposal_id = to_object_id(str(claim["proposal_id"]))
        expected_revision = int(claim["expected_narrative_revision"])
        decision_digest = str(claim["decision_digest"])
        claim_id = str(
            claim.get("claim_id") or f"accept-state-proposal:{proposal_id}"
        )
        current = await self.collection.find_one({"_id": proposal_id}, session=session)
        if current is None:
            if persisted_finalization_intent:
                return
            raise MutationConflictError("State proposal disappeared before acceptance")
        stored_job_binding = _job_mutation_binding(current)
        stored_required_binding = _required_state_generation_binding(current)
        if stored_job_binding is not None and not _uses_current_dispatch_protocol(
            current
        ):
            raise MutationConflictError(
                "State proposal Provider dispatch evidence is unknown"
            )
        raw_claim_binding = claim.get("job_mutation_binding")
        raw_required_binding = claim.get(
            "required_state_generation_binding"
        )
        supplied_job_binding = None
        supplied_required_binding = None
        if raw_claim_binding is not None:
            try:
                supplied_job_binding = JobMutationRecoveryBindingV1.model_validate(
                    raw_claim_binding
                )
            except (TypeError, ValueError) as exc:
                raise MutationConflictError(
                    "State proposal claim Job binding is invalid"
                ) from exc
        if raw_required_binding is not None:
            try:
                supplied_required_binding = (
                    RequiredStateGenerationBinding.model_validate(
                        raw_required_binding
                    )
                )
            except (TypeError, ValueError) as exc:
                raise MutationConflictError(
                    "State proposal claim required binding is invalid"
                ) from exc
        required_finalization_handoff = False
        if stored_required_binding is not None:
            if (
                supplied_required_binding is None
                or supplied_job_binding is None
            ):
                raise MutationConflictError(
                    "Required state proposal claim lost its successor binding"
                )
            try:
                _validate_required_state_finalization_handoff(
                    current,
                    required_binding=supplied_required_binding,
                    finalization_binding=supplied_job_binding,
                )
            except StaleStatePreview as exc:
                raise MutationConflictError(str(exc)) from exc
            required_finalization_handoff = True
        elif supplied_required_binding is not None:
            raise MutationConflictError(
                "State proposal claim invented a required binding"
            )
        if stored_job_binding is not None and supplied_job_binding is None:
            raise MutationConflictError(
                "State proposal claim lost its Job binding"
            )
        binding_allows_expired = (
            supplied_job_binding is not None
            and (
                supplied_job_binding == stored_job_binding
                or required_finalization_handoff
            )
        )
        if supplied_job_binding is not None and not binding_allows_expired:
            raise MutationConflictError(
                "State proposal claim belongs to another Job authorization"
            )
        allow_expired = binding_allows_expired or persisted_finalization_intent
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
        recoverable_expired = (
            persisted_finalization_intent
            and current.get("status") == "expired"
        )
        if current.get("status") != "proposed" and not recoverable_expired:
            raise MutationConflictError("State proposal cannot be claimed")
        expires_at = current.get("expires_at") or current.get(
            "acceptance_expires_at"
        )
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or (
            expires_at <= datetime.now(timezone.utc) and not allow_expired
        ):
            await self.collection.update_one(
                {"_id": proposal_id, "status": "proposed"},
                {"$set": {"status": "expired", "updated_at": get_utc_now()}},
                session=session,
            )
            raise MutationConflictError("State proposal expired before claim")
        chapter = await chapter_repo.get_chapter_by_id(
            str(current["chapter_id"]), session=session
        )
        baseline_matches = _content_digest(chapter) == current.get("content_digest")
        finalized_prose = dict(claim.get("finalized_prose") or {})
        prose_acceptance = dict(chapter.get("prose_acceptance") or {})
        finalized_candidate_matches = bool(finalized_prose) and (
            str(current.get("source_prose_run_id") or "")
            == str(finalized_prose.get("run_id") or "")
            and int(
                current.get("source_prose_run_revision")
                if current.get("source_prose_run_revision") is not None
                else -1
            )
            == int(
                finalized_prose.get("run_revision")
                if finalized_prose.get("run_revision") is not None
                else -2
            )
            and str(current.get("source_content_digest") or "")
            == str(finalized_prose.get("content_digest") or "")
            and chapter_content_digest(chapter.get("content") or "")
            == str(finalized_prose.get("content_digest") or "")
            and str(prose_acceptance.get("source_run_id") or "")
            == str(finalized_prose.get("run_id") or "")
            and str(prose_acceptance.get("content_digest") or "")
            == str(finalized_prose.get("content_digest") or "")
            and prose_acceptance.get("state") == "ai_complete"
        )
        if not baseline_matches and not finalized_candidate_matches:
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
        claim_query: dict[str, Any] = {
            "_id": proposal_id,
            "status": (
                {"$in": ["proposed", "expired"]}
                if persisted_finalization_intent
                else "proposed"
            ),
            "narrative_revision": expected_revision,
        }
        if stored_job_binding is not None:
            claim_query.update(_job_mutation_binding_query(stored_job_binding))
            claim_query["dispatch_protocol_revision"] = _dispatch_protocol_query()
        if required_finalization_handoff:
            claim_query.update(
                _required_state_generation_binding_query(
                    stored_required_binding
                )
            )
            claim_query["dispatch_protocol_revision"] = (
                _dispatch_protocol_query()
            )
        claimed = await self.collection.find_one_and_update(
            claim_query,
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
            if (
                _job_mutation_binding(latest or {}) is not None
                and not _uses_current_dispatch_protocol(latest or {})
            ):
                raise MutationConflictError(
                    "State proposal Provider dispatch evidence is unknown"
                )
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
        persisted_finalization_intent: bool = False,
    ) -> None:
        proposal_id = to_object_id(str(claim["proposal_id"]))
        decision_digest = str(claim["decision_digest"])
        current = await self.collection.find_one(
            {"_id": proposal_id},
            session=session,
        )
        if current is None:
            if persisted_finalization_intent:
                return
            raise MutationConflictError(
                "State proposal disappeared before applied publication"
            )
        stored_job_binding = _job_mutation_binding(current)
        stored_required_binding = _required_state_generation_binding(current)
        raw_claim_binding = claim.get("job_mutation_binding")
        raw_required_binding = claim.get(
            "required_state_generation_binding"
        )
        supplied_job_binding: JobMutationRecoveryBindingV1 | None = None
        supplied_required_binding: RequiredStateGenerationBinding | None = None
        if raw_claim_binding is not None:
            try:
                supplied_job_binding = JobMutationRecoveryBindingV1.model_validate(
                    raw_claim_binding
                )
            except (TypeError, ValueError) as exc:
                raise MutationConflictError(
                    "State proposal applied Job binding is invalid"
                ) from exc
        if raw_required_binding is not None:
            try:
                supplied_required_binding = (
                    RequiredStateGenerationBinding.model_validate(
                        raw_required_binding
                    )
                )
            except (TypeError, ValueError) as exc:
                raise MutationConflictError(
                    "State proposal applied required binding is invalid"
                ) from exc
        required_finalization_handoff = False
        if stored_required_binding is not None:
            if (
                supplied_required_binding is None
                or supplied_job_binding is None
            ):
                raise MutationConflictError(
                    "Required state applied result lost its successor binding"
                )
            try:
                _validate_required_state_finalization_handoff(
                    current,
                    required_binding=supplied_required_binding,
                    finalization_binding=supplied_job_binding,
                )
            except StaleStatePreview as exc:
                raise MutationConflictError(str(exc)) from exc
            required_finalization_handoff = True
        elif supplied_required_binding is not None:
            raise MutationConflictError(
                "State proposal applied result invented a required binding"
            )
        if stored_job_binding is not None:
            if supplied_job_binding is None:
                raise MutationConflictError(
                    "State proposal applied result lost its Job binding"
                )
            if supplied_job_binding != stored_job_binding:
                raise MutationConflictError(
                    "State proposal applied result belongs to another Job binding"
                )
            if not _uses_current_dispatch_protocol(current):
                raise MutationConflictError(
                    "State proposal Provider dispatch evidence is unknown"
                )
        elif (
            supplied_job_binding is not None
            and not required_finalization_handoff
        ):
            raise MutationConflictError(
                "State proposal applied result has an unexpected Job binding"
            )
        applied_query: dict[str, Any] = {
            "_id": proposal_id,
            "status": "claimed",
            "claim.decision_digest": decision_digest,
        }
        if stored_job_binding is not None:
            applied_query["dispatch_protocol_revision"] = (
                _dispatch_protocol_query()
            )
            applied_query.update(_job_mutation_binding_query(stored_job_binding))
        if required_finalization_handoff:
            applied_query["dispatch_protocol_revision"] = (
                _dispatch_protocol_query()
            )
            applied_query.update(
                _required_state_generation_binding_query(
                    stored_required_binding
                )
            )
        applied = await self.collection.find_one_and_update(
            applied_query,
            {
                "$set": {
                    "status": "applied",
                    "accept_result": deepcopy(result),
                    "applied_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                    "expires_at": get_utc_now()
                    + timedelta(seconds=PROPOSAL_TTL_SECONDS),
                }
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if applied is not None:
            return
        current = await self.collection.find_one({"_id": proposal_id}, session=session)
        if (
            stored_job_binding is not None
            or stored_required_binding is not None
        ) and not _uses_current_dispatch_protocol(current or {}):
            raise MutationConflictError(
                "State proposal Provider dispatch evidence is unknown"
            )
        current_claim = (current or {}).get("claim") or {}
        if (
            (current or {}).get("status") == "applied"
            and current_claim.get("decision_digest") == decision_digest
        ):
            return
        raise MutationConflictError("State proposal could not be marked applied")


state_proposal_module = StateProposalModule()
