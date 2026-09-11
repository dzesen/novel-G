"""Resolve relation endpoints without guessing between historical identities."""
from __future__ import annotations

from collections import defaultdict

from bson import ObjectId


class FactionIdentityIndex:
    def __init__(self, documents: list[dict]):
        self.by_card = {
            str(row["_id"]): row for row in documents
            if row.get("_id") is not None and ObjectId.is_valid(str(row["_id"]))
        }
        self.by_business: dict[str, list[str]] = defaultdict(list)
        for card_id, row in self.by_card.items():
            if row.get("faction_id"):
                self.by_business[str(row["faction_id"])].append(card_id)

    def resolve(self, relation: dict, *, require_active: bool = False) -> tuple[str, str] | None:
        if relation.get("identity_ambiguous"):
            return None
        endpoints = []
        for endpoint in ("source", "target"):
            business_id = str(relation.get(f"{endpoint}_faction_id") or "")
            key = f"{endpoint}_faction_card_id"
            if key in relation:
                # Explicit null/foreign/unknown canonical IDs must never fall back to names or fac_*.
                card_id = str(relation[key])
            else:
                matches = self.by_business.get(business_id, [])
                if len(matches) != 1:
                    return None
                card_id = matches[0]
            card = self.by_card.get(card_id)
            if not card or str(card.get("faction_id") or "") != business_id:
                return None
            if require_active and card.get("is_deleted"):
                return None
            endpoints.append(card_id)
        if endpoints[0] == endpoints[1]:
            return None
        return endpoints[0], endpoints[1]
