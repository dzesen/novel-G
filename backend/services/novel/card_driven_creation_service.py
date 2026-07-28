"""Create a novel from reviewed card proposals, then recoverably apply them."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.interop.card_import_proposal_service import (
    CardImportProposalError,
    StaleCardImportProposal,
    card_import_proposal_service,
)


class CardDrivenCreationConflict(CardImportProposalError):
    """The stable creation id was already used with different reviewed input."""


def _request_digest(
    novel_data: dict[str, Any],
    *,
    creation_id: str,
    card_imports: list[dict[str, Any]],
) -> str:
    payload = {
        "creation_id": creation_id,
        "novel": {
            key: value
            for key, value in novel_data.items()
            if key not in {"creation_provenance"}
        },
        "creative_director": deepcopy(
            (novel_data.get("creation_provenance") or {}).get(
                "creative_director"
            )
        ),
        "card_imports": deepcopy(card_imports),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CardDrivenCreationService:
    """Orchestrate the post-confirmation novel/card creation seam."""

    async def create_or_resume(
        self,
        novel_data: dict[str, Any],
        *,
        owner_id: str,
        creation_id: str,
        card_imports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not card_imports:
            raise ValueError("card_imports cannot be empty")
        proposal_ids = [
            str(item.get("proposal_id") or "") for item in card_imports
        ]
        if any(not proposal_id for proposal_id in proposal_ids):
            raise ValueError("card_imports proposal ids cannot be empty")
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("card_imports proposal ids must be unique")

        owner_object_id = to_object_id(owner_id)
        request_digest = _request_digest(
            novel_data,
            creation_id=creation_id,
            card_imports=card_imports,
        )
        novels = get_database()[collections.NOVELS]
        existing = await novels.find_one(
            {
                "owner_id": owner_object_id,
                "creation_provenance.card_imports.creation_id": creation_id,
            }
        )

        if existing is None:
            for item in card_imports:
                await card_import_proposal_service.validate_pre_novel_decisions(
                    item["proposal_id"],
                    owner_id=owner_object_id,
                    digest=item["digest"],
                    decisions=item["decisions"],
                )

            direction = (novel_data.get("creation_provenance") or {}).get(
                "creative_director"
            ) or {}
            expected_context_digest = str(
                direction.get("card_context_digest") or ""
            )
            direction_context = (
                await card_import_proposal_service.build_direction_context(
                    [
                        {
                            "proposal_id": item["proposal_id"],
                            "digest": item["digest"],
                        }
                        for item in card_imports
                    ],
                    owner_id=owner_object_id,
                )
            )
            if (
                not expected_context_digest
                or expected_context_digest
                != direction_context["context_digest"]
            ):
                raise StaleCardImportProposal(
                    "The selected direction no longer matches the reviewed card projection"
                )

            prepared = deepcopy(novel_data)
            provenance = deepcopy(prepared.get("creation_provenance") or {})
            provenance["card_imports"] = {
                "creation_id": creation_id,
                "request_digest": request_digest,
                "card_context_digest": expected_context_digest,
                "status": "applying",
                "proposals": [
                    {
                        "proposal_id": item["proposal_id"],
                        "reviewed_pre_novel_digest": item["digest"],
                    }
                    for item in card_imports
                ],
            }
            prepared["creation_provenance"] = provenance
            try:
                novel_id = await novel_repo.create_novel(prepared)
            except DuplicateKeyError:
                existing = await novels.find_one(
                    {
                        "owner_id": owner_object_id,
                        "creation_provenance.card_imports.creation_id": (
                            creation_id
                        ),
                    }
                )
                if existing is None:
                    raise
                novel_id = str(existing["_id"])
            else:
                existing = await novels.find_one(
                    {"_id": to_object_id(novel_id)}
                )
        else:
            novel_id = str(existing["_id"])

        stored_import = (
            (existing or {}).get("creation_provenance") or {}
        ).get("card_imports") or {}
        if stored_import.get("request_digest") != request_digest:
            raise CardDrivenCreationConflict(
                "card creation id is already bound to different reviewed input"
            )

        apply_results: list[dict[str, Any]] = []
        for item in card_imports:
            rebound = await card_import_proposal_service.bind_to_created_novel(
                item["proposal_id"],
                owner_id=owner_object_id,
                reviewed_digest=item["digest"],
                novel_id=novel_id,
                creation_id=creation_id,
            )
            apply_results.append(
                await card_import_proposal_service.apply(
                    item["proposal_id"],
                    owner_id=owner_object_id,
                    digest=str(rebound["digest"]),
                    decisions=item["decisions"],
                )
            )

        completed = await novels.update_one(
            {
                "_id": to_object_id(novel_id),
                "owner_id": owner_object_id,
                "creation_provenance.card_imports.creation_id": creation_id,
                "creation_provenance.card_imports.request_digest": request_digest,
            },
            {
                "$set": {
                    "creation_provenance.card_imports.status": "applied",
                    "creation_provenance.card_imports.apply_results": deepcopy(
                        apply_results
                    ),
                    "creation_provenance.card_imports.applied_at": get_utc_now(),
                    "updated_at": get_utc_now(),
                }
            },
        )
        if completed.matched_count != 1:
            raise CardDrivenCreationConflict(
                "created novel changed before card imports were finalized"
            )
        return {
            "id": novel_id,
            "message": "Novel created",
            "card_imports": apply_results,
        }


card_driven_creation_service = CardDrivenCreationService()
