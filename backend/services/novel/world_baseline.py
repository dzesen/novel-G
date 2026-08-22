"""世界资料初始化基线：零 Provider 的显式复核与自动成书闸门。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import get_utc_now, to_object_id


WORLD_BASELINE_DECISION_KEYS = (
    "character",
    "location",
    "item",
    "rule",
    "lore",
    "factions",
    "relationships",
)
WORLD_BASELINE_DECISION_VALUES = {"reviewed", "not_applicable"}


class WorldBaselineError(ValueError):
    """携带稳定 code 的世界资料基线失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _current_structure_digest(novel_id: str) -> str:
    """Hash only active volume/chapter topology, never generated prose or outlines."""
    database = get_database()
    novel_obj_id = to_object_id(novel_id)
    volumes = await database[collections.VOLUMES].find(
        {"novel_id": novel_obj_id, "is_deleted": False},
        projection={
            "_id": 1,
            "order_index": 1,
            "chapter_range": 1,
            "chapter_count": 1,
        },
    ).sort([("order_index", 1), ("_id", 1)]).to_list(length=None)
    chapters = await database[collections.CHAPTERS].find(
        {"novel_id": novel_obj_id, "is_deleted": False},
        projection={
            "_id": 1,
            "volume_id": 1,
            "order_index": 1,
        },
    ).sort([("order_index", 1), ("_id", 1)]).to_list(length=None)
    return _digest({
        "projection_revision": "world_baseline_structure.v1",
        "volumes": volumes,
        "chapters": chapters,
    })


async def _material_snapshot(novel_id: str, structure_digest: str) -> dict[str, Any]:
    database = get_database()
    novel_obj_id = to_object_id(novel_id)
    projection = {
        "_id": 1,
        "updated_at": 1,
        "card_type": 1,
        "is_active": 1,
    }

    async def active_documents(collection_name: str) -> list[dict[str, Any]]:
        cursor = database[collection_name].find(
            {"novel_id": novel_obj_id, "is_deleted": False},
            projection=projection,
        ).sort("_id", 1)
        return await cursor.to_list(length=None)

    characters = await active_documents(collections.CHARACTERS)
    worldbook = await active_documents(collections.WORLDBOOK)
    factions = await active_documents(collections.FACTIONS)
    relationships = await active_documents(collections.FACTION_RELATIONS)
    active_relationships = [
        item for item in relationships if item.get("is_active", True) is not False
    ]

    counts = {
        "character": len(characters),
        **{
            card_type: sum(
                1 for item in worldbook if item.get("card_type") == card_type
            )
            for card_type in ("location", "item", "rule", "lore")
        },
        "factions": len(factions),
        "relationships": len(active_relationships),
    }
    markers = {
        "character": characters,
        "worldbook": worldbook,
        "factions": factions,
        "relationships": relationships,
    }
    return {
        "counts": counts,
        "material_digest": _digest(
            {
                "projection_revision": "world_baseline_projection.v2",
                "structure_digest": structure_digest,
                "markers": markers,
            }
        ),
    }


async def _pending_decisions(novel_id: str) -> dict[str, int]:
    database = get_database()
    novel_obj_id = to_object_id(novel_id)
    queries = {
        "reference_card_proposals": (
            collections.REFERENCE_CARD_PROPOSALS,
            {"status": {"$in": ["generating", "proposed", "claimed"]}},
        ),
        "emergent_candidates": (
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES,
            {"status": "pending"},
        ),
        "card_import_proposals": (
            collections.CARD_IMPORT_PROPOSALS,
            # A pending preview has not changed formal material and may be
            # safely abandoned by closing its dialog. Only a mutation already
            # being applied blocks the baseline confirmation.
            {"status": "applying"},
        ),
    }
    result: dict[str, int] = {}
    for key, (collection_name, status_query) in queries.items():
        result[key] = await database[collection_name].count_documents(
            {"novel_id": novel_obj_id, **status_query}
        )
    return result


class WorldBaselineService:
    """隐藏材料投影、确认写入和自动成书所需的有效状态。"""

    @staticmethod
    async def inspect(novel_id: str) -> dict[str, Any]:
        novel = await novel_repo.get_novel_by_id(novel_id)
        requirement = novel.get("world_baseline_requirement")
        if not isinstance(requirement, Mapping):
            material = await _material_snapshot(novel_id, "legacy-unbound")
            pending = await _pending_decisions(novel_id)
            return {
                "schema_version": "world_baseline_view.v1",
                "state": "not_required_legacy",
                "counts": material["counts"],
                "decisions": {},
                "pending_decisions": pending,
                "stale_reasons": [],
                "confirmed_at": None,
                "next_route": {
                    "area": "auto-book",
                    "view": "readiness",
                },
            }

        requirement_digest = str(requirement.get("structure_digest") or "")
        if not requirement_digest:
            raise WorldBaselineError(
                "blueprint_structure_incomplete",
                "卷章结构证据不完整，无法确认世界资料基线。",
            )
        structure_digest = await _current_structure_digest(novel_id)
        material = await _material_snapshot(novel_id, structure_digest)
        pending = await _pending_decisions(novel_id)
        baseline = novel.get("world_baseline")
        stored = baseline if isinstance(baseline, Mapping) else None
        stale_reasons: list[str] = []
        state = "required"
        if any(pending.values()):
            state = "blocked_pending_decisions"
        elif stored is not None:
            if stored.get("structure_digest") != structure_digest:
                stale_reasons.append("blueprint_structure_changed")
            if stored.get("material_digest") != material["material_digest"]:
                stale_reasons.append("world_materials_changed")
            state = "stale" if stale_reasons else "current"

        confirmed_at = stored.get("confirmed_at") if stored else None
        return {
            "schema_version": "world_baseline_view.v1",
            "state": state,
            "counts": material["counts"],
            "decisions": dict(stored.get("decisions") or {}) if stored else {},
            "pending_decisions": pending,
            "stale_reasons": stale_reasons,
            "confirmed_at": _jsonable(confirmed_at),
            "next_route": (
                {"area": "auto-book", "view": "readiness"}
                if state == "current"
                else None
            ),
        }

    @staticmethod
    async def confirm(
        novel_id: str,
        *,
        decisions: Mapping[str, Any],
        confirmed_by: str,
    ) -> dict[str, Any]:
        if set(decisions) != set(WORLD_BASELINE_DECISION_KEYS) or any(
            decisions.get(key) not in WORLD_BASELINE_DECISION_VALUES
            for key in WORLD_BASELINE_DECISION_KEYS
        ):
            raise WorldBaselineError(
                "world_baseline_decisions_incomplete",
                "每类世界资料都必须明确选择已复核或本书不适用。",
            )

        novel = await novel_repo.get_novel_by_id(novel_id)
        requirement = novel.get("world_baseline_requirement")
        if not isinstance(requirement, Mapping):
            raise WorldBaselineError(
                "world_baseline_confirmation_not_available",
                "这本旧书没有待确认的世界资料初始化步骤。",
            )
        requirement_digest = str(requirement.get("structure_digest") or "")
        if not requirement_digest:
            raise WorldBaselineError(
                "blueprint_structure_incomplete",
                "卷章结构证据不完整，无法确认世界资料基线。",
            )
        structure_digest = await _current_structure_digest(novel_id)
        pending = await _pending_decisions(novel_id)
        if any(pending.values()):
            raise WorldBaselineError(
                "world_baseline_pending_proposals",
                "仍有资料候选或导入决策未处理，暂不能确认基线。",
            )
        material = await _material_snapshot(novel_id, structure_digest)
        baseline = {
            "schema_version": "world_baseline.v1",
            "structure_digest": structure_digest,
            "projection_revision": "world_baseline_projection.v2",
            "material_digest": material["material_digest"],
            "counts": material["counts"],
            "decisions": {
                key: str(decisions[key]) for key in WORLD_BASELINE_DECISION_KEYS
            },
            "confirmed_by": to_object_id(confirmed_by),
            "confirmed_at": get_utc_now(),
        }
        await novel_repo.update_novel_info(
            novel_id,
            {"world_baseline": baseline},
        )
        result = await WorldBaselineService.inspect(novel_id)
        if result["state"] != "current":
            raise WorldBaselineError(
                "world_baseline_materials_changed",
                "确认期间世界资料发生变化，请重新复核后再确认。",
            )
        return result
