"""Freeze scoped faction effects before a recoverable, revision-fenced write."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from bson import ObjectId

from backend.db.faction_identity import FactionIdentityIndex
from backend.db.errors import DuplicateKeyError, NotFoundError
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.faction_repository import faction_repo
from backend.db.repositories.faction_relation_repository import faction_relation_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.llm.faction_context import FACTION_CARD_FIELDS

FACTION_MUTATION = "change_faction_context"
_REPOSITORIES = {"factions": faction_repo, "relations": faction_relation_repo}


async def execute_faction_mutation(session, mutation):
    """Replay only frozen document IDs; each effect is independently idempotent."""
    payload = mutation.journal["command"]["payload"]
    novel_id = str(mutation.journal["novel_id"])
    for index, effect in enumerate(payload["effects"]):
        key = f"effect_{index}"
        if mutation.was_received(key):
            continue
        repo = _REPOSITORIES[effect["collection"]]
        query = {"_id": to_object_id(effect["id"]), "novel_id": to_object_id(novel_id)}
        if effect["kind"] == "insert":
            existing = await repo.collection.find_one(query, session=session)
            if existing is None:
                data = {**deepcopy(effect["data"]), **query}
                if effect["collection"] == "factions":
                    await faction_repo.create_faction(data, session=session)
                else:
                    await faction_relation_repo.create_relation(data, session=session)
        elif effect["kind"] == "delete":
            await repo.collection.delete_one(query, session=session)
        else:
            await repo.collection.update_one(query, {"$set": effect["data"]}, session=session)
        await mutation.receipt(key, True)
    return payload["result"]


async def mutate_factions(operation: str, novel_id: str, data: dict, *, faction_id: str | None = None):
    from backend.services.novel.faction_service import FactionService

    await novel_repo.get_novel_by_id(novel_id)
    revision = await narrative_revision_store.current(novel_id)
    scope = {"novel_id": to_object_id(novel_id)}
    effects: list[dict[str, Any]] = []
    now = get_utc_now()

    def effect(collection, row, fields=None, *, kind="set"):
        effects.append({"collection": collection, "id": str(row["_id"]), "kind": kind,
                        "data": {**(fields or {}), "updated_at": now}})

    async def prepare_create(raw, business_id, sort_order):
        prepared = deepcopy(raw)
        prepared["name"] = str(prepared.get("name") or "").strip()
        prepared["level_type"] = str(prepared.get("level_type") or "core").strip() or "core"
        if not prepared["name"]:
            raise ValueError("Faction name cannot be empty")
        await FactionService._ensure_unique_active_name(novel_id, prepared["level_type"], prepared["name"])
        if await faction_repo.exists({**scope, "faction_id": business_id}, include_deleted=True):
            raise DuplicateKeyError("同一小说下已存在相同 faction_id 的历史阵营，不能复用")
        prepared.update(faction_id=business_id)
        prepared.setdefault("sort_order", sort_order)
        card_id = str(ObjectId())
        effect("factions", {"_id": card_id}, prepared, kind="insert")
        return card_id

    if operation == "create":
        business_id = data.get("faction_id") or await faction_repo._get_next_faction_id(novel_id)
        count = await faction_repo.count_factions_by_level_type(novel_id, data.get("level_type") or "core")
        card_id = await prepare_create(data, business_id, (count + 1) * 10)
        result: Any = [card_id, business_id]
    elif operation == "bulk_create":
        core = list(data.get("core_factions") or [])
        relations = list(data.get("faction_relations") or [])
        if not 2 <= len(core) <= 6:
            raise ValueError("核心阵营数量必须为 2 到 6 个")
        names = [str(row.get("name") or "").strip() for row in core]
        if len(names) != len(set(names)):
            raise ValueError("核心阵营名称不能重复")
        if await FactionService.has_core_factions_initialized(novel_id):
            raise DuplicateKeyError("核心阵营已初始化，请改用手动新增或先清空核心势力与垃圾桶")
        business_ids = FactionService._next_business_ids(await faction_repo._get_next_faction_id(novel_id), "fac", len(core))
        name_map = dict(zip(names, business_ids))
        card_ids = []
        for index, (row, business_id) in enumerate(zip(core, business_ids), 1):
            prepared = FactionService._normalize_generated_core_faction(row, faction_id=business_id, sort_order=index * 10)
            card_ids.append(await prepare_create(prepared, business_id, index * 10))
        business_to_card = dict(zip(business_ids, card_ids))
        relation_ids = FactionService._next_business_ids(await faction_relation_repo._get_next_relation_id(novel_id), "fr", len(relations))
        relation_card_ids = []
        for row, relation_id in zip(relations, relation_ids):
            prepared = FactionService._normalize_generated_relation(row, relation_id=relation_id, name_to_faction_id=name_map)
            for endpoint in ("source", "target"):
                prepared[f"{endpoint}_faction_card_id"] = ObjectId(business_to_card[prepared[f"{endpoint}_faction_id"]])
            rid = str(ObjectId())
            relation_card_ids.append(rid)
            effect("relations", {"_id": rid}, prepared, kind="insert")
        result = {"faction_ids": card_ids, "relation_ids": relation_card_ids}
    elif operation == "sort":
        rows = await faction_repo.get_factions_by_novel(novel_id)
        for row in rows:
            fid = row.get("faction_id")
            if fid in data and row.get("sort_order") != data[fid]:
                effect("factions", row, {"sort_order": data[fid]})
        result = len(effects)
    else:
        if not faction_id:
            raise ValueError("faction_id is required")
        if operation in {"restore", "hard_delete"}:
            try:
                current = await faction_repo.get_deleted_faction(novel_id, faction_id)
            except NotFoundError:
                if operation == "hard_delete" and await faction_repo.exists({**scope, "faction_id": faction_id}):
                    raise ValueError("Only soft-deleted factions can be permanently deleted")
                raise
        else:
            current = await faction_repo.get_faction(novel_id, faction_id)
        related = await faction_relation_repo.collection.find({**scope, "$or": [
            {"source_faction_id": faction_id}, {"target_faction_id": faction_id},
        ]}).to_list(length=None)
        identities = FactionIdentityIndex(await faction_repo.collection.find(
            scope, projection={"_id": 1, "faction_id": 1, "is_deleted": 1},
        ).to_list(length=None))
        bound_relations = []
        for row in related:
            endpoints = identities.resolve(row)
            if endpoints is None:
                # Freeze ambiguity before deletion can erase the evidence of duplicate fac_* IDs.
                effect("relations", row, {"identity_ambiguous": True, "is_active": False})
                continue
            if str(current["_id"]) not in endpoints:
                continue
            bindings = {f"{endpoint}_faction_card_id": ObjectId(cid)
                        for endpoint, cid in zip(("source", "target"), endpoints)}
            if any(row.get(key) != value for key, value in bindings.items()):
                effect("relations", row, bindings)
            bound_relations.append(row)
        related = bound_relations
        if operation == "update":
            allowed = {*FACTION_CARD_FIELDS, "parent_faction_id", "first_appearance_volume_id", "first_appearance_chapter_id", "sort_order", "extra"}
            fields = {key: value for key, value in data.items() if key in allowed}
            for key in ("name", "level_type"):
                if key in fields:
                    fields[key] = str(fields[key] or "").strip()
            next_name = fields.get("name", current.get("name", ""))
            if not next_name:
                raise ValueError("Faction name cannot be empty")
            await FactionService._ensure_unique_active_name(novel_id, fields.get("level_type", current.get("level_type") or "core"), next_name, exclude_faction_id=faction_id)
            if fields:
                effect("factions", current, fields)
            if "name" in fields:
                for row in related:
                    if row.get("is_deleted"):
                        continue
                    names = {f"{endpoint}_faction_name": next_name for endpoint in ("source", "target") if row.get(f"{endpoint}_faction_id") == faction_id}
                    effect("relations", row, names)
            result = bool(fields)
        elif operation == "restore":
            if await faction_repo.exists({**scope, "faction_id": faction_id}):
                raise DuplicateKeyError("同一小说下已存在相同 faction_id 的未删除阵营，无法恢复")
            await FactionService._ensure_unique_active_name(novel_id, current.get("level_type") or "core", current.get("name", ""))
            effect("factions", current, {"is_deleted": False, "deleted_at": None})
            for row in related:
                if row.get("is_deleted") or "disabled_by_faction_delete_ids" not in row:
                    continue
                disabled = [fid for fid in row["disabled_by_faction_delete_ids"] if str(fid) not in {faction_id, str(current["_id"])}]
                fields = {"disabled_by_faction_delete_ids": disabled}
                if not disabled:
                    fields["is_active"] = True
                effect("relations", row, fields)
            result = True
        elif operation in {"soft_delete", "hard_delete"}:
            is_hard = operation == "hard_delete"
            same_active = is_hard and await faction_repo.exists({**scope, "faction_id": faction_id})
            children = [] if same_active else await faction_repo.find_many({**scope, "parent_faction_id": faction_id})
            for row in children:
                effect("factions", row, {"parent_faction_id": None})
            removed_relations = 0
            if not same_active:
                for row in related:
                    if is_hard:
                        effect("relations", row, kind="delete")
                        removed_relations += 1
                    elif not row.get("is_deleted") and (row.get("is_active") is True or row.get("disabled_by_faction_delete_ids")):
                        disabled = list(dict.fromkeys([*(row.get("disabled_by_faction_delete_ids") or []), str(current["_id"])]))
                        effect("relations", row, {"is_active": False, "disabled_by_faction_delete_ids": disabled})
            effect("factions", current, {"is_deleted": True, "deleted_at": now}, kind="delete" if is_hard else "set")
            result = {"faction_deleted": 1, "children_unlinked": len(children), "relations_deleted": removed_relations} if is_hard else True
        else:
            raise ValueError(f"Unknown faction operation: {operation}")

    if not effects:
        return result
    result = await commit_mutation(MutationCommand(
        novel_id=novel_id, idempotency_key=f"faction:{ObjectId()}", operation=FACTION_MUTATION,
        expected_narrative_revision=revision,
        payload={"effects": effects, "result": result},
    ), execute_faction_mutation, persistent_narrative_fence=True)
    if operation == "bulk_create":
        return {
            "factions": [await faction_repo.collection.find_one({**scope, "_id": to_object_id(cid)}) for cid in result["faction_ids"]],
            "faction_relations": [await faction_relation_repo.collection.find_one({**scope, "_id": to_object_id(cid)}) for cid in result["relation_ids"]],
        }
    return tuple(result) if operation == "create" else result
