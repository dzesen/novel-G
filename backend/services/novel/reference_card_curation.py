"""Persisted AI proposal and recoverable apply flow for reference cards."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import unicodedata
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from backend.config import config as config_module
from backend.config.lifecycle import FileSecretVersionStore
from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.mutation import (
    MutationCommand,
    MutationConflictError,
    commit_mutation,
    resume_persisted_mutation,
)
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.prompts.prompt_selector import load_prompt_config
from backend.llm.schemas.reference_card_pydantic import (
    CharacterCandidateSchema,
    ItemCandidateSchema,
    LocationCandidateSchema,
    ReferenceCardCandidatesSchema,
    RuleCandidateSchema,
)
from backend.services.llm.generation_runtime import (
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)
from backend.services.novel.reference_card_service import get_card_repository


WORKFLOW_NAME = "create_reference_cards_by_ai"
WORKFLOW_STEP = "reference_cards"
PROPOSAL_LIFETIME = timedelta(days=7)
PROPOSAL_RETENTION = timedelta(days=30)
FUZZY_MATCH_THRESHOLD = 0.82
SOURCE_FIELDS = (
    "title",
    "subtitle",
    "genre",
    "tags",
    "plot",
    "core_idea",
    "tone",
    "target_audience",
    "introduction",
    "summary",
    "core_seed",
    "worldview",
    "writing_style",
    "narrative_pov",
    "era_background",
    "number_of_chapters",
    "words_per_chapter",
)
TYPE_TO_GROUP = {
    "character": "characters",
    "location": "locations",
    "item": "items",
    "rule": "rules",
}
GROUP_TO_TYPE = {value: key for key, value in TYPE_TO_GROUP.items()}
SCHEMA_BY_TYPE = {
    "character": CharacterCandidateSchema,
    "location": LocationCandidateSchema,
    "item": ItemCandidateSchema,
    "rule": RuleCandidateSchema,
}
EDITABLE_FIELDS = {
    "name",
    "subtitle",
    "description",
    "importance",
    "tags",
    "details",
    "character_profile",
}
REFERENCE_CARD_EDITABLE_FIELDS = frozenset(EDITABLE_FIELDS)
_PREPARE_LOCKS: dict[str, asyncio.Lock] = {}


class ReferenceCardProposalError(ValueError):
    """A proposal cannot be inspected or accepted in its current state."""


class StaleReferenceCardProposal(ReferenceCardProposalError):
    """The novel or its card set changed after proposal generation."""


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
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_card_name(value: str) -> str:
    """Normalize only for comparisons; the user's display spelling is preserved."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split()).casefold()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _proposal_key() -> bytes:
    store = FileSecretVersionStore(
        Path(config_module.CONFIG_PATH).with_name(".config-secret-versions.json")
    )
    return store.derive_key("reference-card-curation")


def _token_payload(proposal: dict[str, Any]) -> str:
    expires_at = proposal.get("expires_at")
    if not isinstance(expires_at, datetime):
        raise ReferenceCardProposalError("Reference-card proposal has no valid expiry")
    return (
        f"{proposal['_id']}:{proposal['source_digest']}:{proposal['card_set_digest']}:"
        f"{proposal['candidate_digest']}:{int(_as_utc(expires_at).timestamp())}"
    )


def _acceptance_token(proposal: dict[str, Any]) -> str:
    return hmac.new(
        _proposal_key(),
        _token_payload(proposal).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _card_view(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "_id": str(card.get("_id")),
        "card_type": str(card.get("card_type") or ""),
        "name": str(card.get("name") or ""),
        "subtitle": str(card.get("subtitle") or ""),
        "description": str(card.get("description") or ""),
        "details": deepcopy(card.get("details") or {}),
        "character_profile": deepcopy(card.get("character_profile") or {}),
        "tags": list(card.get("tags") or []),
        "importance": str(card.get("importance") or "sub"),
        "sort_order": card.get("sort_order"),
        "is_deleted": bool(card.get("is_deleted")),
        "updated_at": card.get("updated_at"),
    }


def _card_digest(card: dict[str, Any]) -> str:
    return _digest(_card_view(card))


def _source_snapshot(novel: dict[str, Any]) -> dict[str, Any]:
    return {field: deepcopy(novel.get(field)) for field in SOURCE_FIELDS}


async def _load_cards(novel_id: str, *, session: Any = None) -> list[dict[str, Any]]:
    novel_object_id = to_object_id(novel_id)
    cards: list[dict[str, Any]] = []
    for collection_name in (collections.CHARACTERS, collections.WORLDBOOK):
        cursor = get_database()[collection_name].find(
            {"novel_id": novel_object_id},
            session=session,
        )
        cards.extend(await cursor.to_list(length=None))
    return sorted(cards, key=lambda item: (str(item.get("card_type")), str(item.get("_id"))))


def _card_set_digest(cards: list[dict[str, Any]]) -> str:
    return _digest([_card_view(card) for card in cards])


def _clean_candidate(card_type: str, candidate: dict[str, Any]) -> dict[str, Any]:
    validated = SCHEMA_BY_TYPE[card_type].model_validate(candidate).model_dump(
        exclude_none=True
    )
    validated["name"] = " ".join(validated["name"].split())
    validated["subtitle"] = validated["subtitle"].strip()
    validated["description"] = validated["description"].strip()
    validated["tags"] = list(
        dict.fromkeys(
            str(tag).strip()
            for tag in validated.get("tags") or []
            if str(tag).strip()
        )
    )
    validated["details"] = {
        key: str(value).strip()
        for key, value in (validated.get("details") or {}).items()
        if str(value).strip()
    }
    if "character_profile" in validated:
        validated["character_profile"] = deepcopy(
            validated.get("character_profile") or {}
        )
    return validated


def validate_reference_card_candidate(
    card_type: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Apply the same bounded schema used by the existing four-decision UI."""

    return _clean_candidate(card_type, candidate)


def _prepare_candidates(
    generated: ReferenceCardCandidatesSchema,
    cards: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    prepared: dict[str, list[dict[str, Any]]] = {
        group: [] for group in GROUP_TO_TYPE
    }
    generated_names: dict[str, dict[str, str]] = {
        card_type: {} for card_type in TYPE_TO_GROUP
    }
    for group, card_type in GROUP_TO_TYPE.items():
        for raw in getattr(generated, group):
            candidate = _clean_candidate(card_type, raw.model_dump())
            normalized_name = normalize_card_name(candidate["name"])
            warnings: list[dict[str, Any]] = []
            action = "create"
            target_id: str | None = None
            target_digest: str | None = None
            target_preview: dict[str, Any] | None = None
            conflicts: list[dict[str, Any]] = []

            duplicate_candidate_id = generated_names[card_type].get(normalized_name)
            if duplicate_candidate_id:
                action = "skip"
                warnings.append(
                    {
                        "code": "generated_duplicate",
                        "message": "同一批候选中已有同类型同名卡片，默认跳过。",
                        "candidate_id": duplicate_candidate_id,
                    }
                )
            else:
                generated_names[card_type][normalized_name] = "pending"
                exact_same_type = [
                    card
                    for card in cards
                    if str(card.get("card_type")) == card_type
                    and normalize_card_name(str(card.get("name") or ""))
                    == normalized_name
                ]
                active_exact = next(
                    (card for card in exact_same_type if not card.get("is_deleted")),
                    None,
                )
                deleted_exact = next(
                    (card for card in exact_same_type if card.get("is_deleted")),
                    None,
                )
                match = active_exact or deleted_exact
                if match is not None:
                    action = "merge" if match is active_exact else "restore_merge"
                    target_id = str(match["_id"])
                    target_digest = _card_digest(match)
                    target_preview = _card_view(match)
                    for field in ("name", "subtitle", "description", "importance"):
                        existing_value = match.get(field)
                        candidate_value = candidate.get(field)
                        if (
                            existing_value not in (None, "", [], {})
                            and candidate_value not in (None, "", [], {})
                            and existing_value != candidate_value
                        ):
                            conflicts.append(
                                {
                                    "field": field,
                                    "existing": deepcopy(existing_value),
                                    "candidate": deepcopy(candidate_value),
                                }
                            )
                    for key, candidate_value in candidate.get("details", {}).items():
                        existing_value = (match.get("details") or {}).get(key)
                        if (
                            existing_value not in (None, "", [], {})
                            and candidate_value not in (None, "", [], {})
                            and existing_value != candidate_value
                        ):
                            conflicts.append(
                                {
                                    "field": f"details.{key}",
                                    "existing": deepcopy(existing_value),
                                    "candidate": deepcopy(candidate_value),
                                }
                            )
                    for key, candidate_value in (
                        candidate.get("character_profile") or {}
                    ).items():
                        existing_value = (
                            match.get("character_profile") or {}
                        ).get(key)
                        if (
                            existing_value not in (None, "", [], {})
                            and candidate_value not in (None, "", [], {})
                            and existing_value != candidate_value
                        ):
                            conflicts.append(
                                {
                                    "field": f"character_profile.{key}",
                                    "existing": deepcopy(existing_value),
                                    "candidate": deepcopy(candidate_value),
                                }
                            )
                else:
                    cross_type = next(
                        (
                            card
                            for card in cards
                            if str(card.get("card_type")) != card_type
                            and normalize_card_name(str(card.get("name") or ""))
                            == normalized_name
                        ),
                        None,
                    )
                    if cross_type is not None:
                        action = "skip"
                        warnings.append(
                            {
                                "code": "cross_type_same_name",
                                "message": "其他卡片类型中存在同名项，需人工确认，默认跳过。",
                                "card_id": str(cross_type["_id"]),
                                "card_type": str(cross_type.get("card_type")),
                            }
                        )
                    else:
                        fuzzy: tuple[float, dict[str, Any]] | None = None
                        for card in cards:
                            if str(card.get("card_type")) != card_type:
                                continue
                            score = SequenceMatcher(
                                None,
                                normalized_name,
                                normalize_card_name(str(card.get("name") or "")),
                            ).ratio()
                            if score >= FUZZY_MATCH_THRESHOLD and (
                                fuzzy is None or score > fuzzy[0]
                            ):
                                fuzzy = (score, card)
                        if fuzzy is not None:
                            action = "skip"
                            warnings.append(
                                {
                                    "code": "fuzzy_same_type",
                                    "message": "存在名称相近的同类型卡片，默认跳过。",
                                    "card_id": str(fuzzy[1]["_id"]),
                                    "name": str(fuzzy[1].get("name") or ""),
                                    "score": round(fuzzy[0], 3),
                                }
                            )

            candidate_id = uuid4().hex
            if generated_names[card_type].get(normalized_name) == "pending":
                generated_names[card_type][normalized_name] = candidate_id
            prepared[group].append(
                {
                    **candidate,
                    "candidate_id": candidate_id,
                    "card_type": card_type,
                    "reserved_card_id": str(ObjectId()),
                    "normalized_name": normalized_name,
                    "recommended_action": action,
                    "recommended_target_card_id": target_id,
                    "recommended_target_digest": target_digest,
                    "recommended_target": target_preview,
                    "field_conflicts": conflicts,
                    "warnings": warnings,
                }
            )
    return prepared


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in candidate.items()
        if key
        not in {
            "normalized_name",
            "recommended_target_digest",
            "reserved_card_id",
        }
    }


def _public_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    result = {
        "proposal_id": str(proposal["_id"]),
        "novel_id": str(proposal["novel_id"]),
        "status": str(proposal.get("status") or ""),
        "candidates": {
            group: [_public_candidate(item) for item in proposal.get("candidates", {}).get(group, [])]
            for group in GROUP_TO_TYPE
        },
        "generation_audit": deepcopy(proposal.get("generation_audit") or {}),
        "proposal_expires_at": _as_utc(proposal["expires_at"]).isoformat(),
        "acceptance_token": _acceptance_token(proposal),
    }
    if proposal.get("apply_result") is not None:
        result["apply_result"] = deepcopy(proposal["apply_result"])
    return result


def _build_prompts(novel: dict[str, Any], cards: list[dict[str, Any]]) -> PromptPlan:
    prompts = load_prompt_config().get(WORKFLOW_NAME) or {}
    base = str(prompts.get("reference_cards_prompt_base") or "")
    if not base:
        raise ValueError("Reference-card generation prompt is missing")
    args = {
        "novel_json": json.dumps(
            _jsonable(_source_snapshot(novel)),
            ensure_ascii=False,
            indent=2,
        ),
        "existing_cards_json": json.dumps(
            [
                {
                    "card_type": card.get("card_type"),
                    "name": card.get("name"),
                    "is_deleted": bool(card.get("is_deleted")),
                }
                for card in cards
            ],
            ensure_ascii=False,
            indent=2,
        ),
    }
    rendered = base.format(**args)
    return PromptPlan(
        native_schema_prompt=f"{rendered}\n{prompts.get('reference_cards_prompt_with_schema_suffix', '')}".strip(),
        prompt_json_prompt=f"{rendered}\n{prompts.get('reference_cards_prompt_without_schema_suffix', '')}".strip(),
    )


def _generation_audit(generated: Any) -> dict[str, Any]:
    usage = getattr(generated, "usage", None)
    plan = getattr(generated, "plan", None)
    return {
        "provider_alias": str(getattr(plan, "provider_alias", "")),
        "structured_output_mode": str(
            getattr(getattr(plan, "mode", None), "value", "")
        ),
        "attempt_count": len(getattr(generated, "attempts", ()) or ()),
        "usage": (
            usage.model_dump()
            if hasattr(usage, "model_dump")
            else deepcopy(usage or {})
        ),
    }


def _find_candidate(proposal: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for group in GROUP_TO_TYPE:
        for candidate in proposal.get("candidates", {}).get(group, []):
            if candidate.get("candidate_id") == candidate_id:
                return candidate
    raise ReferenceCardProposalError(
        f"Unknown reference-card candidate: {candidate_id}"
    )


def _merge_value(
    existing: Any,
    proposed: Any,
    *,
    overwrite: bool,
) -> Any:
    if overwrite or existing in (None, "", [], {}):
        return deepcopy(proposed)
    return deepcopy(existing)


def _merged_card_data(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    overwrite_fields: set[str],
) -> dict[str, Any]:
    merged = {
        field: _merge_value(
            existing.get(field),
            candidate.get(field),
            overwrite=field in overwrite_fields,
        )
        for field in ("name", "subtitle", "description", "importance")
    }
    merged["tags"] = list(
        dict.fromkeys(
            [
                *(str(item) for item in existing.get("tags") or []),
                *(str(item) for item in candidate.get("tags") or []),
            ]
        )
    )
    existing_details = deepcopy(existing.get("details") or {})
    for key, value in (candidate.get("details") or {}).items():
        path = f"details.{key}"
        existing_details[key] = _merge_value(
            existing_details.get(key),
            value,
            overwrite=path in overwrite_fields or "details" in overwrite_fields,
        )
    merged["details"] = existing_details
    existing_profile = deepcopy(existing.get("character_profile") or {})
    for key, value in (candidate.get("character_profile") or {}).items():
        path = f"character_profile.{key}"
        existing_profile[key] = _merge_value(
            existing_profile.get(key),
            value,
            overwrite=(
                path in overwrite_fields
                or "character_profile" in overwrite_fields
            ),
        )
    if existing_profile or "character_profile" in candidate:
        merged["character_profile"] = existing_profile
    return merged


def merge_reference_card_data(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    overwrite_fields: set[str],
) -> dict[str, Any]:
    """Apply the existing fill-empty/explicit-overwrite merge semantics."""

    return _merged_card_data(existing, candidate, overwrite_fields)


class ReferenceCardCurationService:
    @property
    def collection(self):
        return get_database()[collections.REFERENCE_CARD_PROPOSALS]

    async def _snapshot(
        self, novel_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        novel = await novel_repo.get_novel_by_id(novel_id)
        cards = await _load_cards(novel_id)
        return (
            novel,
            cards,
            _digest(_source_snapshot(novel)),
            _card_set_digest(cards),
        )

    async def prepare(
        self,
        novel_id: str,
        *,
        actor_id: str,
        force_regenerate: bool = False,
    ) -> dict[str, Any]:
        lock = _PREPARE_LOCKS.setdefault(str(novel_id), asyncio.Lock())
        async with lock:
            novel, cards, source_digest, card_set_digest = await self._snapshot(
                novel_id
            )
            now = get_utc_now()
            generating = await self.collection.find_one(
                {
                    "novel_id": to_object_id(novel_id),
                    "status": "generating",
                },
                sort=[("updated_at", -1)],
            )
            if generating is not None:
                lease_expires_at = generating.get("generation_lease_expires_at")
                if (
                    isinstance(lease_expires_at, datetime)
                    and _as_utc(lease_expires_at) > _as_utc(now)
                ):
                    raise ReferenceCardProposalError(
                        "Reference-card generation is already in progress; "
                        "retry after the active generation lease expires"
                    )
                await self.collection.update_one(
                    {"_id": generating["_id"], "status": "generating"},
                    {
                        "$set": {
                            "status": "generation_uncertain",
                            "failure": {
                                "error_type": "GenerationLeaseExpired",
                                "message": (
                                    "The worker stopped reporting before the paid "
                                    "generation result was persisted"
                                ),
                            },
                            "failed_at": now,
                            "updated_at": now,
                        }
                    },
                )
            reusable = await self.collection.find_one(
                {
                    "novel_id": to_object_id(novel_id),
                    "status": {"$in": ["proposed", "claimed"]},
                    "source_digest": source_digest,
                    "card_set_digest": card_set_digest,
                    "expires_at": {"$gt": now},
                },
                sort=[("updated_at", -1)],
            )
            superseded_proposal_id: ObjectId | None = None
            if reusable is not None:
                if not force_regenerate:
                    return _public_proposal(reusable)
                if reusable.get("status") == "claimed":
                    raise ReferenceCardProposalError(
                        "Reference-card proposal is being applied and cannot be regenerated"
                    )
                superseded_proposal_id = reusable["_id"]

            proposal_id = ObjectId()
            expires_at = now + PROPOSAL_LIFETIME
            try:
                await self.collection.insert_one(
                    {
                        "_id": proposal_id,
                        "novel_id": to_object_id(novel_id),
                        "actor_id": to_object_id(actor_id),
                        "status": "generating",
                        "source_digest": source_digest,
                        "card_set_digest": card_set_digest,
                        "source_snapshot": _source_snapshot(novel),
                        "generation_started_at": now,
                        "generation_lease_expires_at": now + timedelta(minutes=10),
                        "supersedes_proposal_id": superseded_proposal_id,
                        "expires_at": expires_at,
                        "purge_after": now + PROPOSAL_RETENTION,
                        "created_at": now,
                        "updated_at": now,
                        "is_deleted": False,
                    }
                )
            except DuplicateKeyError as exc:
                raise ReferenceCardProposalError(
                    "Reference-card generation is already in progress; "
                    "retry after the active generation lease expires"
                ) from exc
            try:
                runtime = create_generation_runtime()
                plan = runtime.plan_structured(
                    WorkflowStepTarget(WORKFLOW_NAME, WORKFLOW_STEP)
                )
                generated = await runtime.generate_structured(
                    plan,
                    ReferenceCardCandidatesSchema,
                    _build_prompts(novel, cards),
                    temperature=0.35,
                    max_tokens=6000,
                )
                parsed = ReferenceCardCandidatesSchema.model_validate(
                    generated.value.model_dump()
                )
                candidates = _prepare_candidates(parsed, cards)
                candidate_digest = _digest(candidates)
                proposal = {
                    "_id": proposal_id,
                    "source_digest": source_digest,
                    "card_set_digest": card_set_digest,
                    "candidate_digest": candidate_digest,
                    "expires_at": expires_at,
                }
                token = _acceptance_token(proposal)
                updated = await self.collection.find_one_and_update(
                    {"_id": proposal_id, "status": "generating"},
                    {
                        "$set": {
                            "status": "proposed",
                            "candidates": candidates,
                            "candidate_digest": candidate_digest,
                            "token_digest": hashlib.sha256(
                                token.encode("ascii")
                            ).hexdigest(),
                            "generation_audit": _generation_audit(generated),
                            "proposed_at": get_utc_now(),
                            "updated_at": get_utc_now(),
                        }
                    },
                    return_document=True,
                )
                if updated is None:
                    raise ReferenceCardProposalError(
                        "Reference-card generation lease was lost"
                    )
                if superseded_proposal_id is not None:
                    try:
                        superseded = await self.collection.update_one(
                            {
                                "_id": superseded_proposal_id,
                                "status": "proposed",
                            },
                            {
                                "$set": {
                                    "status": "superseded",
                                    "superseded_by": proposal_id,
                                    "superseded_at": get_utc_now(),
                                    "updated_at": get_utc_now(),
                                }
                            },
                        )
                    except BaseException:
                        await self.collection.update_one(
                            {"_id": proposal_id, "status": "proposed"},
                            {
                                "$set": {
                                    "status": "failed",
                                    "failure": {
                                        "error_type": "ProposalReplacementFailed",
                                        "message": (
                                            "The previous proposal could not be "
                                            "safely replaced"
                                        ),
                                    },
                                    "failed_at": get_utc_now(),
                                    "updated_at": get_utc_now(),
                                }
                            },
                        )
                        raise
                    if superseded.modified_count != 1:
                        await self.collection.update_one(
                            {"_id": proposal_id, "status": "proposed"},
                            {
                                "$set": {
                                    "status": "stale",
                                    "failure": {
                                        "error_type": "ProposalChanged",
                                        "message": (
                                            "The previous proposal changed while "
                                            "regeneration was in progress"
                                        ),
                                    },
                                    "failed_at": get_utc_now(),
                                    "updated_at": get_utc_now(),
                                }
                            },
                        )
                        raise ReferenceCardProposalError(
                            "Previous reference-card proposal changed during regeneration"
                        )
                return _public_proposal(updated)
            except BaseException as exc:
                await self.collection.update_one(
                    {"_id": proposal_id, "status": "generating"},
                    {
                        "$set": {
                            "status": "failed",
                            "failure": {
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                            },
                            "failed_at": get_utc_now(),
                            "updated_at": get_utc_now(),
                        }
                    },
                )
                raise

    async def inspect(self, novel_id: str) -> dict[str, Any] | None:
        proposal = await self.collection.find_one(
            {
                "novel_id": to_object_id(novel_id),
                "status": {"$in": ["proposed", "claimed"]},
                "expires_at": {"$gt": get_utc_now()},
            },
            sort=[("updated_at", -1)],
        )
        return _public_proposal(proposal) if proposal is not None else None

    async def _load_verified(
        self,
        *,
        novel_id: str,
        proposal_id: str,
        acceptance_token: str,
    ) -> dict[str, Any]:
        proposal = await self.collection.find_one(
            {
                "_id": to_object_id(proposal_id),
                "novel_id": to_object_id(novel_id),
            }
        )
        if proposal is None:
            raise ReferenceCardProposalError("Reference-card proposal is missing")
        if proposal.get("status") not in {"proposed", "claimed", "applied"}:
            raise ReferenceCardProposalError(
                "Reference-card proposal is not available for application"
            )
        expires_at = proposal.get("expires_at")
        if not isinstance(expires_at, datetime) or _as_utc(expires_at) <= datetime.now(
            timezone.utc
        ):
            await self.collection.update_one(
                {"_id": proposal["_id"], "status": "proposed"},
                {"$set": {"status": "expired", "updated_at": get_utc_now()}},
            )
            raise ReferenceCardProposalError("Reference-card proposal has expired")
        supplied_digest = hashlib.sha256(
            acceptance_token.encode("ascii")
        ).hexdigest()
        if not hmac.compare_digest(
            supplied_digest,
            str(proposal.get("token_digest") or ""),
        ) or not hmac.compare_digest(
            acceptance_token,
            _acceptance_token(proposal),
        ):
            raise ReferenceCardProposalError(
                "Reference-card proposal token is invalid"
            )
        return proposal

    def _prepare_decisions(
        self,
        proposal: dict[str, Any],
        decisions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        all_candidates = [
            candidate
            for group in GROUP_TO_TYPE
            for candidate in proposal.get("candidates", {}).get(group, [])
        ]
        candidate_ids = {item["candidate_id"] for item in all_candidates}
        received_ids = [str(item.get("candidate_id") or "") for item in decisions]
        if len(received_ids) != len(set(received_ids)):
            raise ReferenceCardProposalError(
                "Each reference-card candidate must be decided exactly once"
            )
        if set(received_ids) != candidate_ids:
            raise ReferenceCardProposalError(
                "Decisions must cover every reference-card candidate exactly once"
            )

        normalized: list[dict[str, Any]] = []
        for raw_decision in decisions:
            candidate = _find_candidate(
                proposal, str(raw_decision["candidate_id"])
            )
            action = str(raw_decision.get("action") or "")
            if action not in {"create", "merge", "restore_merge", "skip"}:
                raise ReferenceCardProposalError(
                    f"Unsupported reference-card decision: {action}"
                )
            overrides = raw_decision.get("overrides") or {}
            if not isinstance(overrides, dict):
                raise ReferenceCardProposalError("Candidate overrides must be an object")
            unknown = set(overrides) - EDITABLE_FIELDS
            if unknown:
                raise ReferenceCardProposalError(
                    f"Unsupported candidate override fields: {sorted(unknown)}"
                )
            edited = {
                key: deepcopy(value)
                for key, value in candidate.items()
                if key in EDITABLE_FIELDS
            }
            edited.update(deepcopy(overrides))
            edited = _clean_candidate(str(candidate["card_type"]), edited)
            overwrite_fields = sorted(
                {
                    str(item)
                    for item in raw_decision.get("overwrite_fields") or []
                    if str(item)
                }
            )
            if any(
                field not in EDITABLE_FIELDS
                and not field.startswith("details.")
                and not field.startswith("character_profile.")
                for field in overwrite_fields
            ):
                raise ReferenceCardProposalError(
                    "Unsupported merge overwrite field"
                )
            target_id = raw_decision.get("target_card_id")
            if action in {"merge", "restore_merge"} and not target_id:
                raise ReferenceCardProposalError(
                    f"{action} requires target_card_id"
                )
            normalized.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "card_type": candidate["card_type"],
                    "action": action,
                    "target_card_id": str(target_id) if target_id else None,
                    "reserved_card_id": candidate["reserved_card_id"],
                    "candidate": edited,
                    "overwrite_fields": overwrite_fields,
                }
            )
        normalized.sort(key=lambda item: item["candidate_id"])
        return normalized, _digest(normalized)

    async def _validate_current_snapshot(
        self,
        novel_id: str,
        proposal: dict[str, Any],
        decisions: list[dict[str, Any]],
    ) -> None:
        novel, cards, source_digest, card_set_digest = await self._snapshot(novel_id)
        del novel
        if source_digest != proposal.get("source_digest"):
            raise StaleReferenceCardProposal(
                "Novel source changed after reference-card generation"
            )
        if card_set_digest != proposal.get("card_set_digest"):
            raise StaleReferenceCardProposal(
                "Reference-card set changed after reference-card generation"
            )
        by_id = {str(card["_id"]): card for card in cards}
        for decision in decisions:
            if decision["action"] not in {"merge", "restore_merge"}:
                continue
            target = by_id.get(str(decision["target_card_id"]))
            if (
                target is None
                or str(target.get("card_type")) != decision["card_type"]
            ):
                raise StaleReferenceCardProposal(
                    "Merge target changed after reference-card generation"
                )
            if decision["action"] == "merge" and target.get("is_deleted"):
                raise StaleReferenceCardProposal("Merge target moved to trash")
            if decision["action"] == "restore_merge" and not target.get("is_deleted"):
                raise StaleReferenceCardProposal(
                    "Restore-and-merge target is no longer in trash"
                )

    @staticmethod
    async def _execute_apply(session: Any, mutation: Any) -> dict[str, Any]:
        command = mutation.journal["command"]["payload"]
        proposal_id = to_object_id(command["proposal_id"])
        decision_digest = str(command["decision_digest"])
        proposals = get_database()[collections.REFERENCE_CARD_PROPOSALS]
        proposal = await proposals.find_one({"_id": proposal_id}, session=session)
        if proposal is None:
            raise MutationConflictError(
                "Reference-card proposal disappeared before application"
            )
        existing_claim = proposal.get("claim") or {}
        if proposal.get("status") == "applied":
            if existing_claim.get("decision_digest") != decision_digest:
                raise MutationConflictError(
                    "Reference-card proposal was applied with another decision"
                )
            return deepcopy(proposal.get("apply_result") or {})
        if proposal.get("status") == "proposed":
            claimed = await proposals.update_one(
                {"_id": proposal_id, "status": "proposed"},
                {
                    "$set": {
                        "status": "claimed",
                        "claim": {
                            "decision_digest": decision_digest,
                            "idempotency_key": mutation.journal["idempotency_key"],
                        },
                        "claimed_at": get_utc_now(),
                        "updated_at": get_utc_now(),
                    }
                },
                session=session,
            )
            if claimed.modified_count != 1:
                proposal = await proposals.find_one(
                    {"_id": proposal_id}, session=session
                )
        elif (
            proposal.get("status") != "claimed"
            or existing_claim.get("decision_digest") != decision_digest
        ):
            raise MutationConflictError(
                "Reference-card proposal is claimed by another decision"
            )

        counts = {
            "created": 0,
            "merged": 0,
            "restored_merged": 0,
            "skipped": 0,
        }
        mappings: list[dict[str, Any]] = []
        for decision in command["decisions"]:
            receipt_key = f"candidate_{decision['candidate_id']}"
            if mutation.was_received(receipt_key):
                receipt = deepcopy(mutation.journal["receipts"][receipt_key])
            else:
                action = decision["action"]
                repository = get_card_repository(decision["card_type"])
                card_id: str | None = None
                if action == "skip":
                    receipt = {
                        "candidate_id": decision["candidate_id"],
                        "action": "skip",
                        "card_id": None,
                    }
                elif action == "create":
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
                    receipt = {
                        "candidate_id": decision["candidate_id"],
                        "action": "create",
                        "card_id": card_id,
                    }
                else:
                    card_id = decision["target_card_id"]
                    current = await repository.get_card(
                        command["novel_id"],
                        decision["card_type"],
                        card_id,
                        include_deleted=True,
                        session=session,
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
                    elif action == "merge" and current.get("is_deleted"):
                        raise MutationConflictError(
                            "Reference-card merge target is in trash"
                        )
                    merged = _merged_card_data(
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
                    receipt = {
                        "candidate_id": decision["candidate_id"],
                        "action": action,
                        "card_id": card_id,
                    }
                await mutation.receipt(receipt_key, receipt)
            mappings.append(receipt)
            count_key = {
                "create": "created",
                "merge": "merged",
                "restore_merge": "restored_merged",
                "skip": "skipped",
            }[receipt["action"]]
            counts[count_key] += 1

        result = {
            "proposal_id": str(proposal_id),
            "counts": counts,
            "mappings": mappings,
        }
        await proposals.update_one(
            {
                "_id": proposal_id,
                "status": {"$in": ["claimed", "applied"]},
                "claim.decision_digest": decision_digest,
            },
            {
                "$set": {
                    "status": "applied",
                    "apply_result": deepcopy(result),
                    "applied_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                }
            },
            session=session,
        )
        return result

    async def apply(
        self,
        *,
        novel_id: str,
        proposal_id: str,
        acceptance_token: str,
        decisions: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        proposal = await self._load_verified(
            novel_id=novel_id,
            proposal_id=proposal_id,
            acceptance_token=acceptance_token,
        )
        normalized, decision_digest = self._prepare_decisions(proposal, decisions)
        existing_claim = proposal.get("claim") or {}
        if proposal.get("status") in {"claimed", "applied"}:
            if existing_claim.get("decision_digest") != decision_digest:
                raise MutationConflictError(
                    "Reference-card proposal is already claimed by another decision"
                )
            if proposal.get("status") == "applied":
                return deepcopy(proposal.get("apply_result") or {})
            journal = await get_database()[collections.MUTATION_JOURNALS].find_one(
                {
                    "novel_id": to_object_id(novel_id),
                    "idempotency_key": f"reference-card-plan:{proposal_id}:{decision_digest}",
                }
            )
            if journal is None:
                raise MutationConflictError(
                    "Claimed reference-card proposal has no recoverable mutation"
                )
            return await resume_persisted_mutation(
                journal,
                ReferenceCardCurationService._execute_apply,
            )

        await self._validate_current_snapshot(novel_id, proposal, normalized)
        if all(item["action"] == "skip" for item in normalized):
            result = {
                "proposal_id": proposal_id,
                "counts": {
                    "created": 0,
                    "merged": 0,
                    "restored_merged": 0,
                    "skipped": len(normalized),
                },
                "mappings": [
                    {
                        "candidate_id": item["candidate_id"],
                        "action": "skip",
                        "card_id": None,
                    }
                    for item in normalized
                ],
            }
            updated = await self.collection.update_one(
                {"_id": proposal["_id"], "status": "proposed"},
                {
                    "$set": {
                        "status": "applied",
                        "claim": {
                            "decision_digest": decision_digest,
                            "idempotency_key": None,
                        },
                        "apply_result": deepcopy(result),
                        "applied_by": to_object_id(actor_id),
                        "applied_at": get_utc_now(),
                        "updated_at": get_utc_now(),
                    }
                },
            )
            if updated.modified_count != 1:
                raise MutationConflictError(
                    "Reference-card proposal was claimed concurrently"
                )
            return result

        command = MutationCommand(
            novel_id=novel_id,
            idempotency_key=f"reference-card-plan:{proposal_id}:{decision_digest}",
            operation="apply_reference_card_plan",
            version=1,
            payload={
                "novel_id": novel_id,
                "proposal_id": proposal_id,
                "actor_id": actor_id,
                "decision_digest": decision_digest,
                "decisions": normalized,
            },
            child_ids={
                item["candidate_id"]: item["reserved_card_id"]
                for item in normalized
                if item["action"] == "create"
            },
        )
        return await commit_mutation(
            command,
            ReferenceCardCurationService._execute_apply,
            advances_narrative_revision=True,
        )


reference_card_curation_service = ReferenceCardCurationService()
