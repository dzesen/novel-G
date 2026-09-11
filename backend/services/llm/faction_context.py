"""Typed, novel-scoped faction card material for planning and declared prose.

The canonical card_id is the faction document ObjectId. Legacy fac_* identifiers
are only used to resolve stored relation endpoints inside the same novel; they
never become selectable references. Planning sees a bounded directory, while
prose receives only the bodies explicitly declared by the accepted outline.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from bson import ObjectId

from backend.db.repositories.faction_repository import faction_repo
from backend.db.repositories.faction_relation_repository import faction_relation_repo
from backend.db.faction_identity import FactionIdentityIndex
from backend.db.utils import to_object_id
from backend.db.narrative_revision import narrative_revision_store, NarrativeRevisionConflict

FACTION_INDEX_MAX_CHARACTERS = 6000
FACTION_CARD_FIELDS = (
    "name", "alias", "faction_type", "level_type", "positioning", "public_stance",
    "core_goal", "hidden_goal", "resources_and_advantages", "organization_style",
    "core_values", "conflict_with_mainline", "is_public", "influence_scope",
    "active_status", "expandability", "tags",
)
FACTION_RELATION_FIELDS = (
    "relation_type", "current_state", "core_conflict", "hidden_tension",
    "possible_change", "intensity",
)
FACTION_INDEX_HEADER = (
    "【正式势力卡紧凑目录】\n"
    "以下仅为规划索引，不代表已发生的剧情。章纲须把本章使用的势力 card_id "
    "显式写入 referenced_faction_card_ids；正文只装配已声明的势力及其相互关系。"
    "不得用名称、fac_* 编号或未知 ID 代替 card_id，不需要的势力无需选入。\n"
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def project_faction_material(
    documents: list[dict], relations: list[dict], *, identity_documents: list[dict] | None = None,
) -> dict[str, Any]:
    """Project canonical IDs and whitelisted fields; ignore ambiguous edges."""
    active = [row for row in documents if not row.get("is_deleted")
              and row.get("_id") is not None and ObjectId.is_valid(str(row["_id"]))]
    active.sort(key=lambda row: (int(row.get("sort_order") or 0), str(row["_id"])))
    identities = FactionIdentityIndex(identity_documents if identity_documents is not None else documents)
    cards = {str(row["_id"]): {key: row[key] for key in FACTION_CARD_FIELDS if key in row}
             for row in active}
    edges = []
    for row in sorted(relations, key=lambda item: str(item.get("_id") or "")):
        if row.get("is_deleted") or row.get("is_active") is False:
            continue
        endpoints = identities.resolve(row, require_active=True)
        if endpoints is None or any(cid not in cards for cid in endpoints):
            continue
        source, target = endpoints
        edges.append({"source_card_id": source, "target_card_id": target,
                      **{key: row[key] for key in FACTION_RELATION_FIELDS if key in row}})
    return {"cards": cards, "relations": edges}


async def fetch_faction_material(
    novel_id: str, *, declared_card_ids: list[str] | None = None,
) -> dict[str, Any]:
    """None loads the selectable catalog; [] loads no card bodies or edges."""
    if not novel_id or not ObjectId.is_valid(str(novel_id)):
        raise ValueError("有效的小说 ID 必须提供")
    query: dict[str, Any] = {"novel_id": to_object_id(novel_id), "is_deleted": False}
    if declared_card_ids is not None:
        if not declared_card_ids:
            return {"cards": {}, "relations": []}
        if any(not isinstance(cid, str) or not ObjectId.is_valid(cid) for cid in declared_card_ids):
            raise ValueError("势力引用必须使用正式势力卡 ID，请重新确认章纲")
        query["_id"] = {"$in": [ObjectId(cid) for cid in declared_card_ids]}
    revision = await narrative_revision_store.current_for_audit(novel_id)
    documents = await faction_repo.collection.find(query, projection={
        "_id": 1, "faction_id": 1, "sort_order": 1,
        **{key: 1 for key in FACTION_CARD_FIELDS},
    }).to_list(length=None)
    identity_documents = await faction_repo.collection.find(
        {"novel_id": to_object_id(novel_id)},
        projection={"_id": 1, "faction_id": 1, "is_deleted": 1},
    ).to_list(length=None)
    business_ids = [row.get("faction_id") for row in documents if row.get("faction_id")]
    relations = []
    if business_ids:
        relations = await faction_relation_repo.collection.find({
            "novel_id": to_object_id(novel_id), "is_deleted": False,
            "is_active": {"$ne": False},
            "source_faction_id": {"$in": business_ids},
            "target_faction_id": {"$in": business_ids},
        }, projection={"_id": 1, "source_faction_id": 1, "target_faction_id": 1,
                       "source_faction_card_id": 1, "target_faction_card_id": 1, "identity_ambiguous": 1,
                       **{key: 1 for key in FACTION_RELATION_FIELDS}}).to_list(length=None)
    if await narrative_revision_store.current_for_audit(novel_id) != revision:
        raise NarrativeRevisionConflict("势力资料读取期间发生变化，请重新生成")
    result = project_faction_material(documents, relations, identity_documents=identity_documents)
    if declared_card_ids is not None and set(declared_card_ids) - result["cards"].keys():
        raise ValueError("章纲引用的势力已删除或不属于本书，请重新确认势力选择")
    return result


def faction_catalog(material: Mapping[str, Any]) -> tuple[str, list[str], int]:
    """Return a bounded planning directory and exactly its selectable IDs."""
    cards = material.get("cards") or {}
    payload: dict[str, list] = {"factions": [], "relationships": []}
    selected: list[str] = []
    for card_id, card in cards.items():
        entry = {"card_id": card_id, "name": _text(card.get("name"), 80),
                 "type": _text(card.get("faction_type"), 40),
                 "positioning": _text(card.get("positioning"), 120),
                 "goal": _text(card.get("core_goal"), 120),
                 "mainline_conflict": _text(card.get("conflict_with_mainline"), 120)}
        candidate = {**payload, "factions": [*payload["factions"], entry]}
        if len(FACTION_INDEX_HEADER) + len(_json(candidate)) > FACTION_INDEX_MAX_CHARACTERS:
            break
        payload = candidate
        selected.append(str(card_id))
    selected_ids = set(selected)
    dropped = len(cards) - len(selected)
    for edge in material.get("relations") or []:
        if edge.get("source_card_id") not in selected_ids or edge.get("target_card_id") not in selected_ids:
            dropped += 1
            continue
        brief = {key: edge[key] for key in ("source_card_id", "target_card_id")}
        brief.update({key: _text(edge.get(key), 120) for key in ("relation_type", "current_state", "core_conflict")})
        candidate = {**payload, "relationships": [*payload["relationships"], brief]}
        if len(FACTION_INDEX_HEADER) + len(_json(candidate)) > FACTION_INDEX_MAX_CHARACTERS:
            dropped += 1
            continue
        payload = candidate
    return ((FACTION_INDEX_HEADER + _json(payload)) if selected else "", selected, dropped)


def declared_faction_text(material: Mapping[str, Any], declared_ids: list[str]) -> str:
    """Defend the declaration boundary again at pure prompt assembly."""
    cards = material.get("cards") or {}
    selected = list(dict.fromkeys(str(cid) for cid in declared_ids))
    if not selected:
        return ""
    if any(cid not in cards for cid in selected):
        raise ValueError("章纲引用的正式势力卡不可用，请重新确认章纲")
    allowed = set(selected)
    return "【本章声明的势力设定与关系（作者设定，不自动视为已经发生的剧情）】\n" + _json({
        "factions": [{"card_id": cid, **{key: cards[cid][key] for key in FACTION_CARD_FIELDS if key in cards[cid]}}
                     for cid in selected],
        "relationships": [{key: edge[key] for key in ("source_card_id", "target_card_id", *FACTION_RELATION_FIELDS) if key in edge}
                          for edge in material.get("relations") or []
                          if edge.get("source_card_id") in allowed and edge.get("target_card_id") in allowed],
    })
