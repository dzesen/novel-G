"""Digest-bound, deterministic creation of unique emergent reference cards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Annotated, Any, Literal

from bson import ObjectId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.mutation import (
    MutationCommand,
    MutationConflictError,
    MutationEngine,
    MutationHandlerSpec,
)
from backend.db.utils import get_utc_now, to_object_id
from backend.services.novel.reference_card_curation import (
    FUZZY_MATCH_THRESHOLD,
    normalize_card_name,
    parse_reference_card_candidate_source,
    validate_reference_card_candidate,
)
from backend.services.novel.reference_card_service import (
    get_card_repository,
)


AUTO_CREATE_MUTATION_NAME = "auto_create_unique_reference_cards"
AUTO_CREATE_MUTATION_VERSION = 1
AUTO_CREATE_MUTATION_OPERATION = (
    f"{AUTO_CREATE_MUTATION_NAME}@{AUTO_CREATE_MUTATION_VERSION}"
)
IDENTITY_NORMALIZATION_REVISION = "reference-card-identity-nfkc-casefold-r1"
CANDIDATE_SCHEMA_REVISION = "reference-card-candidate-schema-r1"
APPLICATION_REVISION = "reference-card-auto-create-application-r1"
REFERENCE_CARD_CREATION_POLICY_REVISION = 1
MAX_AUTO_CREATES_PER_CHAPTER = 3
MAX_AUTO_CREATES_PER_BOOK = 20
MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER = 2
REPAIR_TOOL = "repair_reference_dependency.v1"
CARD_TYPE_ORDER = ("character", "location", "item", "rule", "lore")
AUTO_CARD_FIELDS = (
    "name",
    "subtitle",
    "description",
    "importance",
    "tags",
    "details",
    "character_profile",
)
FORMAL_CONFLICT_FIELDS = (
    "name",
    "subtitle",
    "description",
    "importance",
)
REVIEWABLE_IDENTITY_STATUSES = ("pending", "deferred")
_HEX_64_PATTERN = r"^[0-9a-f]{64}$"
_OBJECT_ID_PATTERN = r"^[0-9a-f]{24}$"
_FORMAL_CARD_CONTENT_DIGEST_EXCLUDED_FIELDS = frozenset({
    "_id",
    "novel_id",
    "created_at",
    "updated_at",
    "deleted_at",
    "is_deleted",
    "is_favorite",
})
_NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
_PositiveInt = Annotated[StrictInt, Field(ge=1)]
ReferenceCardType = Literal["character", "location", "item", "rule", "lore"]


class ReferenceCardAutoCreationPolicy(BaseModel):
    """User-selected readiness policy before it becomes formal authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    allowed_card_types: tuple[ReferenceCardType, ...] = CARD_TYPE_ORDER
    max_auto_creates_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_AUTO_CREATES_PER_CHAPTER),
    ] = MAX_AUTO_CREATES_PER_CHAPTER
    max_auto_creates_per_book: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_AUTO_CREATES_PER_BOOK),
    ] = MAX_AUTO_CREATES_PER_BOOK
    max_candidate_repair_cycles_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER),
    ] = 0

    @model_validator(mode="after")
    def validate_closed_policy(self) -> "ReferenceCardAutoCreationPolicy":
        canonical_types = tuple(
            card_type
            for card_type in CARD_TYPE_ORDER
            if card_type in self.allowed_card_types
        )
        if not canonical_types or canonical_types != self.allowed_card_types:
            raise ValueError("allowed_card_types must be unique and canonical")
        if not self.enabled and self.max_candidate_repair_cycles_per_chapter:
            raise ValueError("disabled auto-creation cannot authorize candidate repair")
        return self

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> "ReferenceCardAutoCreationPolicy":
        if value is None:
            return cls()
        return cls.model_validate(dict(value))


class ReferenceCardRepairProviderBoundV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_alias: str = Field(min_length=1, max_length=120)
    maximum_paid_attempts_total: _NonNegativeInt
    maximum_tokens_total: _NonNegativeInt


class ReferenceCardCreationAuthorizationV1(BaseModel):
    """Closed policy authority frozen into one batch-generation readiness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["reference_card_creation_authorization.v1"]
    mode: Literal["auto_create_unique"]
    policy_revision: Literal[1]
    owner_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    novel_id: str = Field(pattern=_OBJECT_ID_PATTERN)
    scope: Literal["volume", "book"]
    volume_id: str | None = Field(default=None, pattern=_OBJECT_ID_PATTERN)
    chapter_ids: tuple[str, ...]
    worklist_digest: str = Field(pattern=_HEX_64_PATTERN)
    baseline_narrative_revision: _NonNegativeInt
    authorization_revision: _PositiveInt
    allowed_card_types: tuple[ReferenceCardType, ...]
    max_auto_creates_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_AUTO_CREATES_PER_CHAPTER),
    ]
    max_auto_creates_per_book: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_AUTO_CREATES_PER_BOOK),
    ]
    max_candidate_repair_cycles_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER),
    ]
    identity_normalization_revision: Literal[
        "reference-card-identity-nfkc-casefold-r1"
    ]
    fuzzy_identity_threshold: Literal[0.82]
    candidate_schema_revision: Literal["reference-card-candidate-schema-r1"]
    application_revision: Literal["reference-card-auto-create-application-r1"]
    mutation_operation: Literal["auto_create_unique_reference_cards@1"]
    allowed_change_classes: tuple[Literal["reference_card_create"], ...]
    repair_tool_whitelist: tuple[str, ...]
    repair_provider_bounds: tuple[ReferenceCardRepairProviderBoundV1, ...]
    maximum_repair_provider_attempts_total: _NonNegativeInt
    maximum_repair_tokens_total: _NonNegativeInt
    authorization_digest: str = Field(pattern=_HEX_64_PATTERN)

    @model_validator(mode="after")
    def validate_closed_authority(self) -> "ReferenceCardCreationAuthorizationV1":
        if self.scope == "volume" and self.volume_id is None:
            raise ValueError("volume-scoped authorization requires volume_id")
        if self.scope == "book" and self.volume_id is not None:
            raise ValueError("book-scoped authorization must not include volume_id")
        if not self.chapter_ids or len(self.chapter_ids) != len(set(self.chapter_ids)):
            raise ValueError("authorization chapter_ids must be non-empty and unique")
        if any(not ObjectId.is_valid(chapter_id) for chapter_id in self.chapter_ids):
            raise ValueError("authorization chapter_ids contain an invalid ObjectId")
        canonical_types = tuple(
            card_type
            for card_type in CARD_TYPE_ORDER
            if card_type in self.allowed_card_types
        )
        if not canonical_types or canonical_types != self.allowed_card_types:
            raise ValueError("allowed_card_types must be unique and canonical")
        if self.allowed_change_classes != ("reference_card_create",):
            raise ValueError("only reference_card_create may be authorized")
        aliases = [bound.provider_alias for bound in self.repair_provider_bounds]
        if len(aliases) != len(set(aliases)):
            raise ValueError("repair Provider aliases must be unique")
        if self.maximum_repair_provider_attempts_total != sum(
            bound.maximum_paid_attempts_total
            for bound in self.repair_provider_bounds
        ):
            raise ValueError("repair Provider attempt total changed")
        if self.maximum_repair_tokens_total != sum(
            bound.maximum_tokens_total for bound in self.repair_provider_bounds
        ):
            raise ValueError("repair Provider token total changed")
        if self.max_candidate_repair_cycles_per_chapter == 0:
            if (
                self.repair_tool_whitelist
                or self.repair_provider_bounds
                or self.maximum_repair_provider_attempts_total
                or self.maximum_repair_tokens_total
            ):
                raise ValueError("disabled candidate repair must have zero authority")
        else:
            if self.repair_tool_whitelist != (REPAIR_TOOL,):
                raise ValueError("candidate repair has an invalid Tool whitelist")
            if (
                not self.repair_provider_bounds
                or self.maximum_repair_provider_attempts_total <= 0
                or self.maximum_repair_tokens_total <= 0
            ):
                raise ValueError("enabled candidate repair requires bounded Provider authority")
        return self


class AutoReferenceCardCreationPolicyDenied(MutationConflictError):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("Reference-card auto-creation policy denied the batch")
        self.result = result


class AutoReferenceCardCreationRecoveryBlocked(RuntimeError):
    """A partial standalone write no longer satisfies exact recovery identity."""


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
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def reference_card_content_digest(document: Mapping[str, Any]) -> str:
    """Digest author-visible card content while excluding lifecycle metadata."""

    return _digest({
        str(key): deepcopy(value)
        for key, value in document.items()
        if str(key) not in _FORMAL_CARD_CONTENT_DIGEST_EXCLUDED_FIELDS
    })


def reference_card_projection_digest(projection: Mapping[str, Any]) -> str:
    """Digest the exact candidate projection frozen by the creation Gate."""

    return _digest(dict(projection))


def _completed_source_narrative_revision(
    journal: Mapping[str, Any],
) -> int | None:
    """Return a source receipt only when its revision transition is coherent."""

    revision = (
        (journal.get("receipts") or {})
        .get("narrative_revision", {})
        .get("revision")
    )
    if type(revision) is not int or revision < 1:
        return None
    command = journal.get("command")
    expected = (
        command.get("expected_narrative_revision")
        if isinstance(command, Mapping)
        else None
    )
    if expected is not None and (
        type(expected) is not int
        or expected < 0
        or revision != expected + 1
    ):
        return None
    return revision


def _source_authorized_for_job(
    *,
    source_journal: Mapping[str, Any],
    source_payload: Mapping[str, Any],
    authorization: ReferenceCardCreationAuthorizationV1,
    job_id: str,
    chapter_id: str,
    readiness_digest: str,
) -> bool:
    """Bind a current-Job source or an immutable source present at readiness."""

    if str(source_payload.get("chapter_id") or "") != chapter_id:
        return False
    source_operation = str(source_journal.get("operation") or "")
    if source_operation == "accept_chapter_outline":
        binding = source_payload.get("job_mutation_binding")
        bound_to_current_job = (
            isinstance(binding, Mapping)
            and str(binding.get("schema_version") or "")
            == "job_mutation_recovery_binding.v1"
            and str(binding.get("novel_id") or "") == authorization.novel_id
            and str(binding.get("job_id") or "") == job_id
            and str(binding.get("chapter_id") or "") == chapter_id
            and str(binding.get("readiness_digest") or "") == readiness_digest
            and binding.get("authorization_revision")
            == authorization.authorization_revision
            and str(binding.get("operation") or "")
            == "accept_chapter_outline"
            and str(binding.get("idempotency_key") or "")
            == str(source_journal.get("idempotency_key") or "")
        )
        source_revision = _completed_source_narrative_revision(source_journal)
        present_at_readiness = (
            source_revision is not None
            and source_revision <= authorization.baseline_narrative_revision
        )
        return bound_to_current_job or present_at_readiness
    if source_operation != "apply_reference_dependency_repair":
        return False
    try:
        repair_authorization = parse_reference_card_creation_authorization(
            source_payload.get("authorization")
        )
    except (TypeError, ValueError):
        return False
    return bool(
        repair_authorization == authorization
        and str(source_payload.get("novel_id") or "")
        == authorization.novel_id
        and str(source_payload.get("job_id") or "") == job_id
        and str(source_payload.get("readiness_digest") or "")
        == readiness_digest
        and source_payload.get("authorization_revision")
        == authorization.authorization_revision
        and type(source_payload.get("cycle")) is int
        and 1
        <= source_payload["cycle"]
        <= authorization.max_candidate_repair_cycles_per_chapter
    )


def parse_reference_card_creation_authorization(
    value: Mapping[str, Any],
) -> ReferenceCardCreationAuthorizationV1:
    if not isinstance(value, Mapping):
        raise ValueError("reference-card creation authorization must be an object")
    expected_fields = set(ReferenceCardCreationAuthorizationV1.model_fields)
    if set(value) != expected_fields:
        raise ValueError("reference-card creation authorization shape changed")
    parsed = ReferenceCardCreationAuthorizationV1.model_validate(dict(value))
    canonical = parsed.model_dump(mode="json")
    supplied_digest = canonical.pop("authorization_digest")
    if _digest(canonical) != supplied_digest:
        raise ValueError("reference-card creation authorization digest changed")
    return parsed


def build_reference_card_creation_authorization(
    *,
    owner_id: str,
    novel_id: str,
    scope: Literal["volume", "book"],
    volume_id: str | None,
    chapter_ids: Iterable[str],
    worklist_digest: str,
    baseline_narrative_revision: int,
    authorization_revision: int,
    allowed_card_types: Iterable[ReferenceCardType],
    max_auto_creates_per_chapter: int,
    max_auto_creates_per_book: int,
    max_candidate_repair_cycles_per_chapter: int,
    repair_provider_bounds: Iterable[Mapping[str, Any]] = (),
    maximum_repair_provider_attempts_total: int = 0,
    maximum_repair_tokens_total: int = 0,
) -> dict[str, Any]:
    requested_types = tuple(str(item) for item in allowed_card_types)
    payload: dict[str, Any] = {
        "schema_version": "reference_card_creation_authorization.v1",
        "mode": "auto_create_unique",
        "policy_revision": REFERENCE_CARD_CREATION_POLICY_REVISION,
        "owner_id": str(owner_id),
        "novel_id": str(novel_id),
        "scope": scope,
        "volume_id": str(volume_id) if volume_id is not None else None,
        "chapter_ids": tuple(str(chapter_id) for chapter_id in chapter_ids),
        "worklist_digest": str(worklist_digest),
        "baseline_narrative_revision": baseline_narrative_revision,
        "authorization_revision": authorization_revision,
        "allowed_card_types": requested_types,
        "max_auto_creates_per_chapter": max_auto_creates_per_chapter,
        "max_auto_creates_per_book": max_auto_creates_per_book,
        "max_candidate_repair_cycles_per_chapter": (
            max_candidate_repair_cycles_per_chapter
        ),
        "identity_normalization_revision": IDENTITY_NORMALIZATION_REVISION,
        "fuzzy_identity_threshold": FUZZY_MATCH_THRESHOLD,
        "candidate_schema_revision": CANDIDATE_SCHEMA_REVISION,
        "application_revision": APPLICATION_REVISION,
        "mutation_operation": AUTO_CREATE_MUTATION_OPERATION,
        "allowed_change_classes": ("reference_card_create",),
        "repair_tool_whitelist": (
            (REPAIR_TOOL,)
            if max_candidate_repair_cycles_per_chapter > 0
            else ()
        ),
        "repair_provider_bounds": tuple(
            dict(bound) for bound in repair_provider_bounds
        ),
        "maximum_repair_provider_attempts_total": (
            maximum_repair_provider_attempts_total
        ),
        "maximum_repair_tokens_total": maximum_repair_tokens_total,
    }
    payload["authorization_digest"] = _digest(payload)
    parsed = parse_reference_card_creation_authorization(payload)
    return parsed.model_dump(mode="json")


def _candidate_projection(card_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    projected = {
        field: deepcopy(value.get(field))
        for field in AUTO_CARD_FIELDS
        if field in value
    }
    return validate_reference_card_candidate(card_type, projected)


def _candidate_source_snapshot(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": str(document.get("_id") or ""),
        "novel_id": str(document.get("novel_id") or ""),
        "volume_id": str(document.get("volume_id") or ""),
        "chapter_id": str(document.get("chapter_id") or ""),
        "chapter_order": int(document.get("chapter_order") or 0),
        "source_kind": str(document.get("source_kind") or ""),
        "source_mutation_id": str(document.get("source_mutation_id") or ""),
        "card_type": str(document.get("card_type") or ""),
        "candidate_data": deepcopy(document.get("candidate_data") or {}),
        "requires_review_before_next_chapter": bool(
            document.get("requires_review_before_next_chapter")
        ),
        "evidence": deepcopy(document.get("evidence") or {}),
        "reserved_card_id": str(document.get("reserved_card_id") or ""),
    }


def reference_card_source_candidate_evidence(
    *,
    source_entry: Mapping[str, Any],
    source_payload: Mapping[str, Any],
    candidate_document: Mapping[str, Any],
    novel_id: str,
    chapter_id: str,
    source_mutation_id: str,
) -> dict[str, Any]:
    """Rebuild the exact immutable source projection and its three digests."""

    candidate_id = str(source_entry.get("candidate_id") or "")
    reserved_card_id = str(source_entry.get("reserved_card_id") or "")
    raw_candidate = source_entry.get("candidate")
    if (
        not ObjectId.is_valid(candidate_id)
        or not ObjectId.is_valid(reserved_card_id)
        or not isinstance(raw_candidate, Mapping)
    ):
        raise ValueError("reference-card source candidate identity is invalid")
    card_type, blocking, evidence_summary, projection = (
        parse_reference_card_candidate_source(raw_candidate)
    )
    expected_snapshot = {
        "candidate_id": candidate_id,
        "novel_id": str(novel_id),
        "volume_id": str(source_payload.get("volume_id") or ""),
        "chapter_id": str(chapter_id),
        "chapter_order": int(source_payload.get("chapter_order") or 0),
        "source_kind": "chapter_outline",
        "source_mutation_id": str(source_mutation_id),
        "card_type": card_type,
        "candidate_data": projection,
        "requires_review_before_next_chapter": blocking,
        "evidence": {
            "summary": evidence_summary,
            "chapter_id": str(chapter_id),
            "chapter_order": int(source_payload.get("chapter_order") or 0),
            "chapter_title": str(source_payload.get("chapter_title") or ""),
            "source_kind": "chapter_outline",
        },
        "reserved_card_id": reserved_card_id,
    }
    if _candidate_source_snapshot(candidate_document) != expected_snapshot:
        raise ValueError("reference-card persisted source projection changed")
    return {
        **expected_snapshot,
        "source_snapshot_digest": _digest(expected_snapshot),
        "source_candidate_digest": _digest(source_entry),
        "projection_digest": _digest(projection),
    }


def _identity_keys(value: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    name = str(value.get("name") or "").strip()
    normalized_name = normalize_card_name(name)
    if normalized_name:
        result.append((normalized_name, "name", name))
    profile = value.get("character_profile")
    aliases = profile.get("aliases") if isinstance(profile, Mapping) else []
    if isinstance(aliases, (list, tuple)):
        for alias in aliases:
            display = str(alias or "").strip()
            normalized = normalize_card_name(display)
            if normalized:
                result.append((normalized, "alias", display))
    return result


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _field_conflicts(
    candidate: Mapping[str, Any],
    other: Mapping[str, Any],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for field in FORMAL_CONFLICT_FIELDS:
        left = candidate.get(field)
        right = other.get(field)
        if _has_value(left) and _has_value(right) and left != right:
            conflicts.append(
                {"field": field, "candidate": deepcopy(left), "other": deepcopy(right)}
            )
    for group in ("details", "character_profile"):
        left_group = candidate.get(group)
        right_group = other.get(group)
        if not isinstance(left_group, Mapping) or not isinstance(right_group, Mapping):
            continue
        for key in sorted(set(left_group).intersection(right_group)):
            left = left_group.get(key)
            right = right_group.get(key)
            if _has_value(left) and _has_value(right) and left != right:
                conflicts.append(
                    {
                        "field": f"{group}.{key}",
                        "candidate": deepcopy(left),
                        "other": deepcopy(right),
                    }
                )
    return conflicts


def _source_conflicts(
    candidate: Mapping[str, Any],
    other: Mapping[str, Any],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for field in ("chapter_id", "source_mutation_id"):
        left = str(candidate.get(field) or "")
        right = str(other.get(field) or "")
        if left and right and left != right:
            conflicts.append({"field": field, "candidate": left, "other": right})
    left_evidence = candidate.get("evidence")
    right_evidence = other.get("evidence")
    if isinstance(left_evidence, Mapping) and isinstance(right_evidence, Mapping):
        left_summary = str(left_evidence.get("summary") or "").strip()
        right_summary = str(right_evidence.get("summary") or "").strip()
        if left_summary and right_summary and left_summary != right_summary:
            conflicts.append(
                {
                    "field": "evidence.summary",
                    "candidate": left_summary,
                    "other": right_summary,
                }
            )
    return conflicts


class AutoReferenceCardCreationService:
    def __init__(
        self,
        *,
        after_card_write: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._after_card_write = after_card_write

    @staticmethod
    def _denied_result(
        *,
        denials: list[dict[str, Any]],
        limit_usage: Mapping[str, Any] | None,
        next_narrative_revision: int,
        source_mutation_id: str = "",
    ) -> dict[str, Any]:
        ordered = sorted(
            denials,
            key=lambda item: (
                str(item.get("candidate_id") or ""),
                str(item.get("reason") or ""),
                _digest(item.get("evidence") or {}),
            ),
        )
        return {
            "status": "denied",
            "created_count": 0,
            "mappings": [],
            "denials": ordered,
            "deny_reasons": sorted(
                {str(item.get("reason") or "") for item in ordered}
            ),
            "limit_usage": dict(limit_usage or {}),
            "next_narrative_revision": int(next_narrative_revision),
            **(
                {"source_mutation_id": source_mutation_id}
                if source_mutation_id
                else {}
            ),
        }

    @staticmethod
    def _source_denial(
        reason: str,
        *,
        candidate_id: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "candidate_id": str(candidate_id),
            "reason": str(reason),
            "evidence": dict(evidence or {}),
        }

    async def _freeze_candidates(
        self,
        *,
        authorization: ReferenceCardCreationAuthorizationV1,
        job_id: str,
        chapter_id: str,
        readiness_digest: str,
        source_mutation_id: str,
        auto_mutation_key: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        database = get_database()
        source_journal = await database[collections.MUTATION_JOURNALS].find_one(
            {
                "novel_id": to_object_id(authorization.novel_id),
                "idempotency_key": source_mutation_id,
                "operation": {
                    "$in": [
                        "accept_chapter_outline",
                        "apply_reference_dependency_repair",
                    ]
                },
                "status": "completed",
            }
        )
        if source_journal is None:
            return [], [self._source_denial("source_changed")], ""
        source_command = dict(source_journal.get("command") or {})
        source_payload = dict(source_command.get("payload") or {})
        if not _source_authorized_for_job(
            source_journal=source_journal,
            source_payload=source_payload,
            authorization=authorization,
            job_id=job_id,
            chapter_id=chapter_id,
            readiness_digest=readiness_digest,
        ):
            return [], [self._source_denial("source_changed")], ""
        source_digest = str(source_journal.get("command_digest") or "")
        try:
            recomputed_source_digest = MutationCommand.from_journal(
                source_journal
            ).digest()
        except (KeyError, TypeError, ValueError):
            recomputed_source_digest = ""
        if not source_digest or recomputed_source_digest != source_digest:
            return [], [self._source_denial("source_changed")], ""
        raw_candidates = source_payload.get("reference_card_candidates")
        if not isinstance(raw_candidates, list):
            return [], [self._source_denial("source_changed")], source_digest
        source_candidate_receipts = {
            str(receipt.get("candidate_id") or "")
            for key, receipt in dict(source_journal.get("receipts") or {}).items()
            if str(key).startswith("reference_card_candidate_")
            and isinstance(receipt, Mapping)
        }
        candidate_collection = database[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ]
        documents = await candidate_collection.find(
            {
                "novel_id": to_object_id(authorization.novel_id),
                "chapter_id": to_object_id(chapter_id),
                "source_mutation_id": source_mutation_id,
                "is_deleted": False,
            }
        ).to_list(length=None)
        by_id = {str(document.get("_id")): document for document in documents}
        frozen: list[dict[str, Any]] = []
        denials: list[dict[str, Any]] = []
        for raw_entry in raw_candidates:
            if not isinstance(raw_entry, Mapping):
                denials.append(self._source_denial("source_changed"))
                continue
            candidate_id = str(raw_entry.get("candidate_id") or "")
            if (
                not ObjectId.is_valid(candidate_id)
                or candidate_id not in source_candidate_receipts
            ):
                denials.append(
                    self._source_denial(
                        "source_changed",
                        candidate_id=candidate_id,
                    )
                )
                continue
            document = by_id.get(candidate_id)
            if document is None:
                denials.append(
                    self._source_denial(
                        "source_changed",
                        candidate_id=candidate_id,
                    )
                )
                continue
            try:
                frozen_candidate = reference_card_source_candidate_evidence(
                    source_entry=raw_entry,
                    source_payload=source_payload,
                    candidate_document=document,
                    novel_id=authorization.novel_id,
                    chapter_id=chapter_id,
                    source_mutation_id=source_mutation_id,
                )
            except (TypeError, ValueError):
                denials.append(
                    self._source_denial(
                        "candidate_changed",
                        candidate_id=candidate_id,
                    )
                )
                continue
            if not frozen_candidate[
                "requires_review_before_next_chapter"
            ]:
                continue
            status = str(document.get("status") or "")
            decision = dict(document.get("decision") or {})
            is_exact_replay = (
                status == "resolved"
                and str(decision.get("action") or "") == "auto_create_unique"
                and str(decision.get("job_id") or "") == job_id
                and str(decision.get("authorization_digest") or "")
                == authorization.authorization_digest
                and str(decision.get("mutation_idempotency_key") or "")
                == auto_mutation_key
            )
            if status != "pending" and not is_exact_replay:
                denials.append(
                    self._source_denial(
                        "candidate_changed",
                        candidate_id=candidate_id,
                        evidence={"status": status},
                    )
                )
                continue
            frozen.append(frozen_candidate)
        frozen.sort(key=lambda item: item["candidate_id"])
        return frozen, denials, source_digest

    @staticmethod
    async def _load_formal_cards(
        novel_id: str,
        *,
        session: Any,
    ) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for collection_name in (collections.CHARACTERS, collections.WORLDBOOK):
            cursor = get_database()[collection_name].find(
                {"novel_id": to_object_id(novel_id)},
                session=session,
            )
            cards.extend(await cursor.to_list(length=None))
        return cards

    async def _evaluate_gate(
        self,
        *,
        command: Mapping[str, Any],
        journal: Mapping[str, Any],
        session: Any,
    ) -> dict[str, Any]:
        authorization = parse_reference_card_creation_authorization(
            command["authorization"]
        )
        novel_id = str(command["novel_id"])
        chapter_id = str(command["chapter_id"])
        candidate_collection = get_database()[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ]
        denials: list[dict[str, Any]] = []
        partial_self_write = False
        novel = await get_database()[collections.NOVELS].find_one(
            {
                "_id": to_object_id(novel_id),
                "owner_id": to_object_id(str(command["owner_id"])),
                "is_deleted": False,
            },
            session=session,
        )
        if novel is None:
            denials.append(self._source_denial("authorization_invalid"))

        job = await get_database()[collections.GENERATION_JOBS].find_one(
            {
                "_id": to_object_id(str(command["job_id"])),
                "novel_id": to_object_id(novel_id),
                "is_deleted": False,
                "status": {"$in": ["running", "paused"]},
            },
            session=session,
        )
        job_authorization: ReferenceCardCreationAuthorizationV1 | None = None
        if job is not None:
            raw_readiness = job.get("readiness")
            readiness = raw_readiness if isinstance(raw_readiness, Mapping) else {}
            planning = readiness.get("planning")
            raw_job_authorization = (
                planning.get("reference_card_creation_authorization")
                if isinstance(planning, Mapping)
                else None
            )
            try:
                job_authorization = parse_reference_card_creation_authorization(
                    raw_job_authorization
                )
            except (TypeError, ValueError):
                job_authorization = None
            work = readiness.get("work")
            work_chapters = (
                work.get("chapters") if isinstance(work, Mapping) else None
            )
            job_chapter_ids = {
                str(item.get("chapter_id") or "")
                for item in work_chapters or []
                if isinstance(item, Mapping)
            }
            resources = readiness.get("resources")
            job_volume_id = (
                str(job.get("volume_id"))
                if job.get("volume_id") is not None
                else None
            )
            if (
                job_authorization is None
                or job_authorization.model_dump(mode="json")
                != authorization.model_dump(mode="json")
                or str(readiness.get("digest") or "")
                != str(command["readiness_digest"])
                or str((resources or {}).get("owner_id") or "")
                != authorization.owner_id
                or str(job.get("scope") or "") != authorization.scope
                or job_volume_id != authorization.volume_id
                or job.get("authorization_revision")
                != authorization.authorization_revision
                or job.get("expected_narrative_revision")
                != command.get("expected_narrative_revision")
                or chapter_id not in job_chapter_ids
            ):
                job = None
        if job is None:
            denials.append(self._source_denial("authorization_invalid"))

        source_journal = await get_database()[collections.MUTATION_JOURNALS].find_one(
            {
                "novel_id": to_object_id(novel_id),
                "idempotency_key": str(command["source_mutation_id"]),
                "operation": {
                    "$in": [
                        "accept_chapter_outline",
                        "apply_reference_dependency_repair",
                    ]
                },
                "status": "completed",
                "command_digest": str(command["source_command_digest"]),
            },
            session=session,
        )
        if source_journal is None:
            denials.append(self._source_denial("source_changed"))
            source_entries_by_id: dict[str, Mapping[str, Any]] = {}
            source_candidate_receipts: set[str] = set()
            source_operation = ""
        else:
            source_operation = str(source_journal.get("operation") or "")
            try:
                actual_source_digest = MutationCommand.from_journal(
                    source_journal
                ).digest()
            except (KeyError, TypeError, ValueError):
                actual_source_digest = ""
            source_payload = dict(
                (source_journal.get("command") or {}).get("payload") or {}
            )
            raw_source_entries = source_payload.get("reference_card_candidates")
            if (
                actual_source_digest != str(command["source_command_digest"])
                or not isinstance(raw_source_entries, list)
            ):
                denials.append(self._source_denial("source_changed"))
                source_entries_by_id = {}
                source_candidate_receipts = set()
            else:
                source_entries_by_id = {
                    str(entry.get("candidate_id") or ""): entry
                    for entry in raw_source_entries
                    if isinstance(entry, Mapping)
                }
                source_candidate_receipts = {
                    str(receipt.get("candidate_id") or "")
                    for key, receipt in dict(
                        source_journal.get("receipts") or {}
                    ).items()
                    if str(key).startswith("reference_card_candidate_")
                    and isinstance(receipt, Mapping)
                }
                if not _source_authorized_for_job(
                    source_journal=source_journal,
                    source_payload=source_payload,
                    authorization=authorization,
                    job_id=str(command["job_id"]),
                    chapter_id=chapter_id,
                    readiness_digest=str(command["readiness_digest"]),
                ):
                    denials.append(self._source_denial("source_changed"))

        if (
            authorization.owner_id != str(command.get("owner_id") or "")
            or authorization.novel_id != novel_id
            or authorization.authorization_revision
            != command.get("authorization_revision")
            or chapter_id not in authorization.chapter_ids
            or int(command.get("expected_narrative_revision") or -1)
            < authorization.baseline_narrative_revision
        ):
            denials.append(self._source_denial("authorization_invalid"))

        frozen_candidates = list(command.get("candidates") or [])
        selected_ids = [str(item.get("candidate_id") or "") for item in frozen_candidates]
        selected_id_set = set(selected_ids)
        documents = await candidate_collection.find(
            {"_id": {"$in": [to_object_id(item) for item in selected_ids]}},
            session=session,
        ).to_list(length=None)
        by_id = {str(document.get("_id")): document for document in documents}

        self_card_ids: set[str] = set()
        for frozen in frozen_candidates:
            candidate_id = str(frozen["candidate_id"])
            document = by_id.get(candidate_id)
            if document is None:
                denials.append(
                    self._source_denial("source_changed", candidate_id=candidate_id)
                )
                continue
            if _digest(_candidate_source_snapshot(document)) != str(
                frozen.get("source_snapshot_digest") or ""
            ):
                denials.append(
                    self._source_denial("candidate_changed", candidate_id=candidate_id)
                )
            source_entry = source_entries_by_id.get(candidate_id)
            if (
                source_entry is None
                or _digest(source_entry)
                != str(frozen.get("source_candidate_digest") or "")
                or candidate_id not in source_candidate_receipts
            ):
                denials.append(
                    self._source_denial("source_changed", candidate_id=candidate_id)
                )
            if (
                authorization.scope == "volume"
                and str(frozen.get("volume_id") or "") != authorization.volume_id
            ):
                denials.append(
                    self._source_denial(
                        "authorization_invalid",
                        candidate_id=candidate_id,
                    )
                )
            status = str(document.get("status") or "")
            decision = dict(document.get("decision") or {})
            exact_decision = (
                status == "resolved"
                and str(decision.get("action") or "") == "auto_create_unique"
                and str(decision.get("job_id") or "") == str(command["job_id"])
                and str(decision.get("authorization_digest") or "")
                == authorization.authorization_digest
                and str(decision.get("mutation_idempotency_key") or "")
                == str(command["mutation_idempotency_key"])
                and str(document.get("resolved_card_id") or "")
                == str(frozen["reserved_card_id"])
            )
            if status != "pending" and not exact_decision:
                denials.append(
                    self._source_denial(
                        "candidate_changed",
                        candidate_id=candidate_id,
                        evidence={"status": status},
                    )
                )
            if str(frozen["card_type"]) not in authorization.allowed_card_types:
                denials.append(
                    self._source_denial(
                        "unauthorized_type",
                        candidate_id=candidate_id,
                        evidence={"card_type": str(frozen["card_type"])},
                    )
                )
            repository = get_card_repository(str(frozen["card_type"]))
            try:
                existing_reserved = await repository.get_card(
                    novel_id,
                    str(frozen["card_type"]),
                    str(frozen["reserved_card_id"]),
                    include_deleted=True,
                    session=session,
                )
            except NotFoundError:
                existing_reserved = None
            if existing_reserved is not None:
                card_write_receipt = (journal.get("receipts") or {}).get(
                    f"card_write_{candidate_id}"
                )
                try:
                    existing_projection = _candidate_projection(
                        str(frozen["card_type"]),
                        existing_reserved,
                    )
                except (TypeError, ValueError):
                    existing_projection = {}
                if (
                    str(existing_reserved.get("novel_id") or "") != novel_id
                    or str(existing_reserved.get("card_type") or "")
                    != str(frozen["card_type"])
                    or _digest(existing_projection)
                    != str(frozen["projection_digest"])
                    or str(journal.get("idempotency_key") or "")
                    != str(command["mutation_idempotency_key"])
                    or str(journal.get("command_digest") or "")
                    != str(command["command_digest"])
                ):
                    if card_write_receipt is not None or journal.get("error_type"):
                        partial_self_write = True
                    denials.append(
                        self._source_denial(
                            "journal_drift",
                            candidate_id=candidate_id,
                        )
                    )
                else:
                    partial_self_write = True
                    self_card_ids.add(str(existing_reserved["_id"]))

        current_pending = await candidate_collection.find(
            {
                "novel_id": to_object_id(novel_id),
                "chapter_id": to_object_id(chapter_id),
                "requires_review_before_next_chapter": True,
                "status": {"$in": list(REVIEWABLE_IDENTITY_STATUSES)},
            },
            projection={"_id": 1},
            session=session,
        ).to_list(length=None)
        if any(str(item["_id"]) not in selected_id_set for item in current_pending):
            denials.append(self._source_denial("source_changed"))

        formal_cards = await self._load_formal_cards(novel_id, session=session)
        formal_records = [
            {
                "kind": "formal",
                "object_id": str(card.get("_id") or ""),
                "card_type": str(card.get("card_type") or ""),
                "is_deleted": bool(card.get("is_deleted")),
                "data": card,
                "keys": _identity_keys(card),
            }
            for card in formal_cards
            if str(card.get("_id") or "") not in self_card_ids
        ]
        other_documents = await candidate_collection.find(
            {
                "novel_id": to_object_id(novel_id),
                "status": {"$in": list(REVIEWABLE_IDENTITY_STATUSES)},
                "_id": {"$nin": [to_object_id(item) for item in selected_ids]},
            },
            session=session,
        ).to_list(length=None)
        repair_predecessors: list[dict[str, Any]] = []
        if source_operation == "apply_reference_dependency_repair":
            frontier = {str(command["source_mutation_id"])}
            seen_mutation_ids: set[str] = set()
            seen_candidate_ids: set[str] = set()
            lineage_invalid = False
            for _depth in range(MAX_CANDIDATE_REPAIR_CYCLES_PER_CHAPTER):
                frontier -= seen_mutation_ids
                if not frontier:
                    break
                seen_mutation_ids.update(frontier)
                predecessors = await candidate_collection.find(
                    {
                        "novel_id": to_object_id(novel_id),
                        "chapter_id": to_object_id(chapter_id),
                        "status": "superseded",
                        "superseded_by_source_mutation_id": {
                            "$in": sorted(frontier)
                        },
                        "is_deleted": False,
                    },
                    session=session,
                ).to_list(length=101)
                if len(predecessors) > 100:
                    lineage_invalid = True
                    break
                next_frontier: set[str] = set()
                for predecessor in predecessors:
                    candidate_id = str(predecessor.get("_id") or "")
                    if candidate_id and candidate_id not in seen_candidate_ids:
                        repair_predecessors.append(predecessor)
                        seen_candidate_ids.add(candidate_id)
                    source_mutation_id = str(
                        predecessor.get("source_mutation_id") or ""
                    )
                    if source_mutation_id:
                        next_frontier.add(source_mutation_id)
                frontier = next_frontier
            remaining_frontier = frontier - seen_mutation_ids
            if not lineage_invalid and remaining_frontier:
                overflow = await candidate_collection.find_one(
                    {
                        "novel_id": to_object_id(novel_id),
                        "chapter_id": to_object_id(chapter_id),
                        "status": "superseded",
                        "superseded_by_source_mutation_id": {
                            "$in": sorted(remaining_frontier)
                        },
                        "is_deleted": False,
                    },
                    session=session,
                )
                lineage_invalid = overflow is not None
            if lineage_invalid:
                denials.append(self._source_denial("source_changed"))
        pending_records = [
            {
                "kind": "candidate",
                "object_id": str(document.get("_id") or ""),
                "card_type": str(document.get("card_type") or ""),
                "is_deleted": False,
                "data": dict(document.get("candidate_data") or {}),
                "keys": _identity_keys(document.get("candidate_data") or {}),
                "source": document,
            }
            for document in (*other_documents, *repair_predecessors)
        ]

        def add_denial(
            candidate_id: str,
            reason: str,
            evidence: Mapping[str, Any] | None = None,
        ) -> None:
            item = self._source_denial(
                reason,
                candidate_id=candidate_id,
                evidence=evidence,
            )
            identity = _digest(item)
            if all(_digest(existing) != identity for existing in denials):
                denials.append(item)

        def compare_record(
            frozen: Mapping[str, Any],
            record: Mapping[str, Any],
        ) -> None:
            candidate_id = str(frozen["candidate_id"])
            candidate_keys = _identity_keys(frozen["candidate_data"])
            record_keys = list(record.get("keys") or [])
            exact_pairs = [
                (left, right)
                for left in candidate_keys
                for right in record_keys
                if left[0] == right[0]
            ]
            evidence = {
                "object_id": str(record.get("object_id") or ""),
                "card_type": str(record.get("card_type") or ""),
            }
            if exact_pairs:
                if record.get("kind") == "candidate":
                    add_denial(candidate_id, "pending_candidate_identity", evidence)
                else:
                    alias_match = any(
                        left[1] == "alias" or right[1] == "alias"
                        for left, right in exact_pairs
                    )
                    deleted_match = bool(record.get("is_deleted"))
                    cross_type_match = str(record.get("card_type") or "") != str(
                        frozen["card_type"]
                    )
                    if alias_match:
                        add_denial(candidate_id, "confirmed_alias", evidence)
                    if deleted_match:
                        add_denial(candidate_id, "deleted_identity", evidence)
                    if cross_type_match:
                        add_denial(candidate_id, "cross_type_identity", evidence)
                    if not (alias_match or deleted_match or cross_type_match):
                        add_denial(candidate_id, "existing_name", evidence)
                conflicts = _field_conflicts(
                    frozen["candidate_data"],
                    record.get("data") or {},
                )
                if conflicts:
                    add_denial(
                        candidate_id,
                        "field_conflict",
                        {**evidence, "conflicts": conflicts},
                    )
                if record.get("kind") == "candidate":
                    source_conflicts = _source_conflicts(
                        frozen,
                        record.get("source") or {},
                    )
                    if source_conflicts:
                        add_denial(
                            candidate_id,
                            "source_conflict",
                            {**evidence, "conflicts": source_conflicts},
                        )
                return
            fuzzy_matches = [
                {
                    "candidate_identity": left[2],
                    "other_identity": right[2],
                    "score": round(SequenceMatcher(None, left[0], right[0]).ratio(), 3),
                }
                for left in candidate_keys
                for right in record_keys
                if SequenceMatcher(None, left[0], right[0]).ratio()
                >= authorization.fuzzy_identity_threshold
            ]
            if fuzzy_matches:
                add_denial(
                    candidate_id,
                    "fuzzy_identity",
                    {**evidence, "matches": fuzzy_matches},
                )
                conflicts = _field_conflicts(
                    frozen["candidate_data"],
                    record.get("data") or {},
                )
                if conflicts:
                    add_denial(
                        candidate_id,
                        "field_conflict",
                        {**evidence, "conflicts": conflicts},
                    )
                if record.get("kind") == "candidate":
                    source_conflicts = _source_conflicts(
                        frozen,
                        record.get("source") or {},
                    )
                    if source_conflicts:
                        add_denial(
                            candidate_id,
                            "source_conflict",
                            {**evidence, "conflicts": source_conflicts},
                        )

        for frozen in frozen_candidates:
            seen: dict[str, str] = {}
            for normalized, role, display in _identity_keys(frozen["candidate_data"]):
                if normalized in seen:
                    add_denial(
                        str(frozen["candidate_id"]),
                        "confirmed_alias",
                        {"first": seen[normalized], "duplicate": display, "role": role},
                    )
                else:
                    seen[normalized] = display
            for record in (*formal_records, *pending_records):
                compare_record(frozen, record)

        for index, frozen in enumerate(frozen_candidates):
            for other in frozen_candidates[index + 1 :]:
                record = {
                    "kind": "candidate",
                    "object_id": str(other["candidate_id"]),
                    "card_type": str(other["card_type"]),
                    "data": other["candidate_data"],
                    "keys": _identity_keys(other["candidate_data"]),
                    "source": other,
                }
                compare_record(frozen, record)
                reverse_record = {
                    **record,
                    "object_id": str(frozen["candidate_id"]),
                    "card_type": str(frozen["card_type"]),
                    "data": frozen["candidate_data"],
                    "keys": _identity_keys(frozen["candidate_data"]),
                    "source": frozen,
                }
                compare_record(other, reverse_record)

        counted = await candidate_collection.find(
            {
                "novel_id": to_object_id(novel_id),
                "auto_creation.counted": True,
            },
            projection={"_id": 1, "chapter_id": 1},
            session=session,
        ).to_list(length=None)
        counted_ids = {str(item["_id"]) for item in counted}
        chapter_counted_ids = {
            str(item["_id"])
            for item in counted
            if str(item.get("chapter_id") or "") == chapter_id
        }
        new_ids = selected_id_set - counted_ids
        limit_usage = {
            "book_before": len(counted_ids),
            "chapter_before": len(chapter_counted_ids),
            "requested_new": len(new_ids),
            "book_after": len(counted_ids) + len(new_ids),
            "chapter_after": len(chapter_counted_ids) + len(new_ids),
            "book_limit": authorization.max_auto_creates_per_book,
            "chapter_limit": authorization.max_auto_creates_per_chapter,
        }
        if (
            limit_usage["book_after"] > authorization.max_auto_creates_per_book
            or limit_usage["chapter_after"]
            > authorization.max_auto_creates_per_chapter
        ):
            for candidate_id in selected_ids:
                add_denial(candidate_id, "limit_reached", limit_usage)

        return {
            "denials": denials,
            "limit_usage": limit_usage,
            "partial_self_write": partial_self_write,
        }

    async def _execute_apply(self, session: Any, mutation: Any) -> dict[str, Any]:
        command = mutation.journal["command"]["payload"]
        gate_command = {
            **command,
            "command_digest": str(mutation.journal.get("command_digest") or ""),
        }
        gate = await self._evaluate_gate(
            command=gate_command,
            journal=mutation.journal,
            session=session,
        )
        revision = int(
            (mutation.journal.get("receipts") or {})
            .get("narrative_revision", {})
            .get("revision")
            or command["expected_narrative_revision"]
        )
        if gate["denials"]:
            result = self._denied_result(
                denials=gate["denials"],
                limit_usage=gate["limit_usage"],
                next_narrative_revision=(
                    int(command["expected_narrative_revision"])
                    if session is not None
                    else revision
                ),
                source_mutation_id=str(command["source_mutation_id"]),
            )
            if gate["partial_self_write"]:
                raise AutoReferenceCardCreationRecoveryBlocked(
                    "Partial auto-created reference cards require exact recovery"
                )
            if session is not None:
                raise AutoReferenceCardCreationPolicyDenied(result)
            return result

        await mutation.receipt(
            "authorization",
            {
                "authorization_digest": command["authorization"][
                    "authorization_digest"
                ],
                "readiness_digest": command["readiness_digest"],
                "authorization_revision": command["authorization_revision"],
                "job_id": command["job_id"],
            },
        )
        if not mutation.was_received("event_clock"):
            occurred_at = get_utc_now()
            occurred_at = occurred_at.replace(
                microsecond=(occurred_at.microsecond // 1000) * 1000
            )
            await mutation.receipt(
                "event_clock",
                {"occurred_at": occurred_at},
            )
        occurred_at = (
            (mutation.journal.get("receipts") or {})
            .get("event_clock", {})
            .get("occurred_at")
        )
        if not isinstance(occurred_at, datetime):
            raise MutationConflictError(
                "Reference-card auto-creation event clock is invalid"
            )
        candidate_collection = get_database()[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ]
        mappings: list[dict[str, Any]] = []
        for frozen in command["candidates"]:
            candidate_id = str(frozen["candidate_id"])
            card_id = str(frozen["reserved_card_id"])
            receipt_key = f"candidate_{candidate_id}"
            if mutation.was_received(receipt_key):
                mappings.append(deepcopy(mutation.journal["receipts"][receipt_key]))
                continue
            document = await candidate_collection.find_one(
                {
                    "_id": to_object_id(candidate_id),
                    "novel_id": to_object_id(command["novel_id"]),
                },
                session=session,
            )
            if document is None:
                raise MutationConflictError(
                    "Reference-card candidate disappeared during auto-creation"
                )
            decision = dict(document.get("decision") or {})
            exact_decision = (
                str(document.get("status") or "") == "resolved"
                and str(decision.get("action") or "") == "auto_create_unique"
                and str(decision.get("mutation_idempotency_key") or "")
                == command["mutation_idempotency_key"]
                and str(document.get("resolved_card_id") or "") == card_id
            )
            if not exact_decision:
                repository = get_card_repository(str(frozen["card_type"]))
                try:
                    await repository.get_card(
                        command["novel_id"],
                        str(frozen["card_type"]),
                        card_id,
                        include_deleted=True,
                        session=session,
                    )
                except NotFoundError:
                    await repository.create_card(
                        command["novel_id"],
                        str(frozen["card_type"]),
                        frozen["candidate_data"],
                        session=session,
                        card_id=card_id,
                    )
                current_card = await repository.get_card(
                    command["novel_id"],
                    str(frozen["card_type"]),
                    card_id,
                    include_deleted=True,
                    session=session,
                )
                formal_card_content_digest = reference_card_content_digest(
                    current_card
                )
                await mutation.receipt(
                    f"card_write_{candidate_id}",
                    {
                        "card_id": card_id,
                        "projection_digest": frozen["projection_digest"],
                        "formal_card_content_digest": (
                            formal_card_content_digest
                        ),
                    },
                )
                if self._after_card_write is not None:
                    await self._after_card_write(candidate_id, card_id)
                now = get_utc_now()
                updated = await candidate_collection.update_one(
                    {
                        "_id": to_object_id(candidate_id),
                        "novel_id": to_object_id(command["novel_id"]),
                        "status": "pending",
                    },
                    {
                        "$set": {
                            "status": "resolved",
                            "resolved_card_id": to_object_id(card_id),
                            "decision": {
                                "action": "auto_create_unique",
                                "actor_id": command["owner_id"],
                                "job_id": command["job_id"],
                                "readiness_digest": command["readiness_digest"],
                                "authorization_revision": command[
                                    "authorization_revision"
                                ],
                                "authorization_digest": command["authorization"][
                                    "authorization_digest"
                                ],
                                "mutation_idempotency_key": command[
                                    "mutation_idempotency_key"
                                ],
                                "source_mutation_id": command[
                                    "source_mutation_id"
                                ],
                                "projection_digest": frozen["projection_digest"],
                                "formal_card_content_digest": (
                                    formal_card_content_digest
                                ),
                                "decided_at": now,
                            },
                            "auto_creation": {
                                "counted": True,
                                "mutation_idempotency_key": command[
                                    "mutation_idempotency_key"
                                ],
                                "authorization_digest": command["authorization"][
                                    "authorization_digest"
                                ],
                            },
                            "updated_at": now,
                        }
                    },
                    session=session,
                )
                if updated.modified_count != 1:
                    raise MutationConflictError(
                        "Reference-card candidate changed during auto-creation"
                    )
            else:
                repository = get_card_repository(str(frozen["card_type"]))
                current_card = await repository.get_card(
                    command["novel_id"],
                    str(frozen["card_type"]),
                    card_id,
                    include_deleted=True,
                    session=session,
                )
                formal_card_content_digest = reference_card_content_digest(
                    current_card
                )
            receipt = {
                "candidate_id": candidate_id,
                "action": "auto_create_unique",
                "card_id": card_id,
                "card_type": str(frozen["card_type"]),
                "projection_digest": frozen["projection_digest"],
                "formal_card_content_digest": formal_card_content_digest,
            }
            await mutation.receipt(receipt_key, receipt)
            mappings.append(receipt)
        return {
            "status": "created",
            "created_count": len(mappings),
            "mappings": mappings,
            "denials": [],
            "deny_reasons": [],
            "limit_usage": gate["limit_usage"],
            "next_narrative_revision": revision,
            "source_mutation_id": command["source_mutation_id"],
            "mutation_idempotency_key": command["mutation_idempotency_key"],
            "occurred_at": occurred_at,
        }

    async def recover_applied_chapter(
        self,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
        chapter_id: str,
        readiness_digest: str,
        authorization_revision: int,
        expected_narrative_revision: int,
        authorization: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Replay one committed Gate whose Job event/cursor was not persisted."""

        parsed = parse_reference_card_creation_authorization(authorization)
        recovered_candidate = await get_database()[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ].find_one({
            "novel_id": to_object_id(parsed.novel_id),
            "chapter_id": to_object_id(chapter_id),
            "status": "resolved",
            "requires_review_before_next_chapter": True,
            "decision.action": "auto_create_unique",
            "decision.job_id": str(job_id),
            "decision.authorization_digest": parsed.authorization_digest,
            "is_deleted": False,
        })
        if recovered_candidate is None:
            return None
        result = await self.apply_chapter(
            owner_id=owner_id,
            novel_id=novel_id,
            job_id=job_id,
            chapter_id=chapter_id,
            readiness_digest=readiness_digest,
            authorization_revision=authorization_revision,
            expected_narrative_revision=expected_narrative_revision,
            authorization=authorization,
        )
        if (
            str(result.get("status") or "") != "created"
            or result.get("next_narrative_revision")
            != expected_narrative_revision + 1
            or not str(result.get("mutation_idempotency_key") or "")
        ):
            raise AutoReferenceCardCreationRecoveryBlocked(
                "Committed reference-card Gate recovery changed"
            )
        return result

    async def apply_chapter(
        self,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
        chapter_id: str,
        readiness_digest: str,
        authorization_revision: int,
        expected_narrative_revision: int,
        authorization: Mapping[str, Any],
    ) -> dict[str, Any]:
        parsed = parse_reference_card_creation_authorization(authorization)
        if (
            parsed.owner_id != str(owner_id)
            or parsed.novel_id != str(novel_id)
            or parsed.authorization_revision != authorization_revision
            or type(authorization_revision) is not int
            or authorization_revision < 1
            or str(chapter_id) not in parsed.chapter_ids
            or not ObjectId.is_valid(str(job_id))
            or not str(readiness_digest or "").strip()
            or len(str(readiness_digest)) != 64
            or any(character not in "0123456789abcdef" for character in str(readiness_digest))
            or type(expected_narrative_revision) is not int
            or expected_narrative_revision < parsed.baseline_narrative_revision
        ):
            return self._denied_result(
                denials=[self._source_denial("authorization_invalid")],
                limit_usage=None,
                next_narrative_revision=expected_narrative_revision,
            )
        candidate_collection = get_database()[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ]
        source_documents = await candidate_collection.find(
            {
                "novel_id": to_object_id(parsed.novel_id),
                "chapter_id": to_object_id(chapter_id),
                "status": {"$in": sorted(REVIEWABLE_IDENTITY_STATUSES)},
                "requires_review_before_next_chapter": True,
                "is_deleted": False,
            },
            projection={"source_mutation_id": 1},
        ).to_list(length=10)
        if not source_documents:
            source_documents = await candidate_collection.find(
                {
                    "novel_id": to_object_id(parsed.novel_id),
                    "chapter_id": to_object_id(chapter_id),
                    "status": "resolved",
                    "requires_review_before_next_chapter": True,
                    "decision.action": "auto_create_unique",
                    "decision.job_id": str(job_id),
                    "decision.authorization_digest": (
                        parsed.authorization_digest
                    ),
                    "is_deleted": False,
                },
                projection={"source_mutation_id": 1},
            ).to_list(length=10)
        source_mutation_ids = {
            str(item.get("source_mutation_id") or "")
            for item in source_documents
        }
        if not source_documents:
            return {
                "status": "not_applicable",
                "created_count": 0,
                "mappings": [],
                "denials": [],
                "deny_reasons": [],
                "limit_usage": {},
                "next_narrative_revision": expected_narrative_revision,
            }
        if len(source_mutation_ids) != 1 or "" in source_mutation_ids:
            return self._denied_result(
                denials=[self._source_denial("source_changed")],
                limit_usage=None,
                next_narrative_revision=expected_narrative_revision,
            )
        source_mutation_id = next(iter(source_mutation_ids))
        auto_mutation_key = (
            f"auto-create-reference-cards:{job_id}:{chapter_id}:"
            f"{parsed.authorization_digest}:{_digest(source_mutation_id)[:16]}"
        )
        frozen, source_denials, source_digest = await self._freeze_candidates(
            authorization=parsed,
            job_id=str(job_id),
            chapter_id=str(chapter_id),
            readiness_digest=str(readiness_digest),
            source_mutation_id=source_mutation_id,
            auto_mutation_key=auto_mutation_key,
        )
        if source_denials:
            return self._denied_result(
                denials=source_denials,
                limit_usage=None,
                next_narrative_revision=expected_narrative_revision,
                source_mutation_id=source_mutation_id,
            )
        if not frozen:
            return {
                "status": "not_applicable",
                "created_count": 0,
                "mappings": [],
                "denials": [],
                "deny_reasons": [],
                "limit_usage": {},
                "next_narrative_revision": expected_narrative_revision,
                "source_mutation_id": source_mutation_id,
            }
        payload = {
            "novel_id": str(novel_id),
            "owner_id": str(owner_id),
            "job_id": str(job_id),
            "chapter_id": str(chapter_id),
            "readiness_digest": str(readiness_digest),
            "authorization_revision": authorization_revision,
            "expected_narrative_revision": expected_narrative_revision,
            "source_mutation_id": source_mutation_id,
            "source_command_digest": source_digest,
            "mutation_idempotency_key": auto_mutation_key,
            "authorization": parsed.model_dump(mode="json"),
            "candidates": frozen,
        }
        command = MutationCommand(
            novel_id=str(novel_id),
            idempotency_key=auto_mutation_key,
            operation=AUTO_CREATE_MUTATION_NAME,
            version=AUTO_CREATE_MUTATION_VERSION,
            expected_narrative_revision=expected_narrative_revision,
            payload=payload,
            child_ids={
                str(item["candidate_id"]): str(item["reserved_card_id"])
                for item in frozen
            },
        )
        engine = MutationEngine({
            (AUTO_CREATE_MUTATION_NAME, AUTO_CREATE_MUTATION_VERSION): (
                MutationHandlerSpec(
                    self._execute_apply,
                    advances_narrative_revision=True,
                    persistent_narrative_fence=True,
                )
            )
        })
        try:
            result = await engine.execute(command)
        except AutoReferenceCardCreationPolicyDenied as exc:
            result = deepcopy(exc.result)
        return {
            **result,
            "mutation_idempotency_key": auto_mutation_key,
        }


auto_reference_card_creation_service = AutoReferenceCardCreationService()
