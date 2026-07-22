"""把历史裸章号迁移到稳定 chapter_id；歧义项只报告、不猜测。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from typing import Any

from bson import ObjectId

from backend.db.mongo import close_mongo_connection, connect_to_mongo
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import to_object_id
from backend.services.novel.chapter_timeline import ChapterTimeline


def build_migration_plan(
    volumes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    states: list[dict[str, Any]],
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    timeline = ChapterTimeline(volumes, chapters)
    state_updates: list[dict[str, Any]] = []
    thread_updates: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for original in states:
        state = deepcopy(original)
        changed = False
        if state.get("as_of_chapter_id") and state.get("history_status") != "tracked":
            state["history_status"] = "tracked"
            changed = True
        if not state.get("as_of_chapter_id") and state.get("as_of_chapter_order"):
            position = timeline.unique_legacy_order(int(state["as_of_chapter_order"]))
            if position:
                state["as_of_chapter_id"] = ObjectId(position.chapter_id)
                state["history_status"] = "tracked"
                changed = True
            else:
                state["history_status"] = "legacy_unknown"
                changed = True
                ambiguous.append({
                    "kind": "character_state",
                    "document_id": str(state["_id"]),
                    "field": "as_of_chapter_order",
                    "legacy_value": state["as_of_chapter_order"],
                })
        facts = []
        for original_fact in state.get("permanent_facts") or []:
            fact = dict(original_fact)
            if fact.get("source_chapter_id") and fact.get("source_status") != "tracked":
                fact["source_status"] = "tracked"
                changed = True
            if not fact.get("id"):
                fact["id"] = ObjectId()
                changed = True
            if not fact.get("source_chapter_id") and fact.get("chapter_order"):
                position = timeline.unique_legacy_order(int(fact["chapter_order"]))
                if position:
                    fact["source_chapter_id"] = ObjectId(position.chapter_id)
                    fact["source_status"] = "tracked"
                    changed = True
                else:
                    fact["source_status"] = "legacy_unknown"
                    changed = True
                    ambiguous.append({
                        "kind": "permanent_fact",
                        "document_id": str(state["_id"]),
                        "fact_id": str(fact["id"]),
                        "field": "chapter_order",
                        "legacy_value": fact["chapter_order"],
                    })
            facts.append(fact)
        state["permanent_facts"] = facts
        if changed:
            state_updates.append({
                "_id": state["_id"],
                "as_of_chapter_id": state.get("as_of_chapter_id"),
                "history_status": state.get("history_status", "tracked"),
                "permanent_facts": facts,
            })

    for original in threads:
        update: dict[str, Any] = {}
        for legacy_field, id_field in (
            ("planted_chapter_order", "planted_chapter_id"),
            ("resolved_chapter_order", "resolved_chapter_id"),
        ):
            if original.get(id_field) or not original.get(legacy_field):
                continue
            position = timeline.unique_legacy_order(int(original[legacy_field]))
            if position:
                update[id_field] = ObjectId(position.chapter_id)
            else:
                ambiguous.append({
                    "kind": "plot_thread",
                    "document_id": str(original["_id"]),
                    "field": legacy_field,
                    "legacy_value": original[legacy_field],
                })
        if not original.get("due_target") and original.get("due_chapter_order"):
            update["due_target"] = {
                "kind": "planned_ordinal",
                "ordinal": int(original["due_chapter_order"]),
            }
        if update:
            thread_updates.append({"_id": original["_id"], "fields": update})

    return {
        "state_updates": state_updates,
        "thread_updates": thread_updates,
        "ambiguous": ambiguous,
    }


async def migrate_novel(novel_id: str, *, apply: bool = False) -> dict[str, Any]:
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    states = await character_state_repo.list_states(novel_id)
    threads = await plot_thread_repo.list_threads(novel_id)
    plan = build_migration_plan(volumes, chapters, states, threads)
    if apply:
        for update in plan["state_updates"]:
            fields = {"permanent_facts": update["permanent_facts"]}
            fields["history_status"] = update["history_status"]
            if update.get("as_of_chapter_id"):
                fields["as_of_chapter_id"] = update["as_of_chapter_id"]
            await character_state_repo.collection.update_one(
                {"_id": update["_id"], "novel_id": to_object_id(novel_id)},
                {"$set": fields},
            )
        for update in plan["thread_updates"]:
            await plot_thread_repo.collection.update_one(
                {"_id": update["_id"], "novel_id": to_object_id(novel_id)},
                {"$set": update["fields"]},
            )
    return {
        "apply": apply,
        "state_update_count": len(plan["state_updates"]),
        "thread_update_count": len(plan["thread_updates"]),
        "ambiguous": plan["ambiguous"],
    }


async def _main(novel_id: str, apply: bool) -> int:
    await connect_to_mongo()
    try:
        report = await migrate_novel(novel_id, apply=apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report["ambiguous"] else 2
    finally:
        await close_mongo_connection()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("novel_id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.novel_id, args.apply)))
