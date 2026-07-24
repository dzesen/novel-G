"""Audit and migrate legacy numeric chapter references to stable chapter IDs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from bson import ObjectId

from backend.db import collections
from backend.db.mongo import close_mongo_connection, connect_to_mongo, get_database
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import to_object_id
from backend.services.backup.backup_service import (
    build_novel_backup,
    parse_backup,
    save_snapshot_file,
    validate_backup_payload,
)


MAPPING_FORMAT = "novel-generator-chapter-identity-map"
MAPPING_VERSION = 1


def _stable_fact_id(state_id: Any, index: int, fact: dict[str, Any]) -> ObjectId:
    digest = hashlib.sha256(
        f"{state_id}:{index}:{fact.get('chapter_order')}:{fact.get('fact', '')}".encode(
            "utf-8"
        )
    ).hexdigest()
    return ObjectId(digest[:24])


def _volume_by_id(volumes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(volume["_id"]): volume for volume in volumes}


def _candidate_details(
    volumes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    legacy_order: int,
) -> list[dict[str, Any]]:
    by_id = _volume_by_id(volumes)
    candidates = []
    for chapter in chapters:
        if int(chapter.get("order_index") or 0) != int(legacy_order):
            continue
        volume = by_id.get(str(chapter.get("volume_id"))) or {}
        volume_order = int(volume.get("order_index") or 0)
        chapter_order = int(chapter.get("order_index") or 0)
        deleted = bool(chapter.get("is_deleted") or volume.get("is_deleted"))
        candidates.append(
            {
                "chapter_id": str(chapter["_id"]),
                "volume_id": str(chapter.get("volume_id") or ""),
                "volume_order": volume_order,
                "chapter_order": chapter_order,
                "label": f"第{volume_order}卷·第{chapter_order}章",
                "is_deleted": deleted,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["volume_order"],
            item["chapter_order"],
            item["chapter_id"],
        )
    )
    return candidates


def _mapping_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(item.get("novel_id") or ""),
        str(item.get("kind") or ""),
        str(item.get("document_id") or ""),
        str(item.get("fact_id") or ""),
        str(item.get("field") or ""),
    )


def _mapping_index(mappings: list[dict[str, Any]] | None) -> dict[tuple[str, str, str, str, str], str]:
    result = {}
    for mapping in mappings or []:
        key = _mapping_key(mapping)
        chapter_id = str(mapping.get("chapter_id") or "")
        if not all(key) and key[3] == "":
            # fact_id is optional for non-fact mappings; all other components are required.
            required = key[:3] + key[4:]
            if not all(required):
                raise ValueError(f"Invalid chapter identity mapping key: {mapping}")
        if not chapter_id:
            raise ValueError(f"Mapping chapter_id is required: {mapping}")
        if key in result and result[key] != chapter_id:
            raise ValueError(f"Conflicting chapter identity mappings for {key}")
        result[key] = chapter_id
    return result


def _resolve_reference(
    *,
    novel_id: str,
    kind: str,
    document_id: str,
    fact_id: str = "",
    field: str,
    legacy_value: int,
    volumes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    mappings: dict[tuple[str, str, str, str, str], str],
) -> tuple[ObjectId | None, dict[str, Any] | None]:
    candidates = _candidate_details(volumes, chapters, legacy_value)
    key = (novel_id, kind, document_id, fact_id, field)
    mapped = mappings.get(key)
    if mapped:
        allowed = {item["chapter_id"] for item in candidates}
        if mapped not in allowed:
            raise ValueError(
                f"Reviewed mapping {mapped} is not a candidate for {kind} "
                f"{document_id}.{field}={legacy_value}"
            )
        return ObjectId(mapped), None
    if len(candidates) == 1 and not candidates[0]["is_deleted"]:
        return ObjectId(candidates[0]["chapter_id"]), None
    reason = (
        "no_match"
        if not candidates
        else "deleted_only"
        if all(item["is_deleted"] for item in candidates)
        else "multiple_matches"
    )
    issue = {
        "novel_id": novel_id,
        "kind": kind,
        "document_id": document_id,
        "field": field,
        "legacy_value": legacy_value,
        "reason": reason,
        "candidates": candidates,
    }
    if fact_id:
        issue["fact_id"] = fact_id
    return None, issue


def build_migration_plan(
    volumes: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    states: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    *,
    novel_id: str = "",
    mappings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mapping_by_key = _mapping_index(mappings)
    state_updates: list[dict[str, Any]] = []
    thread_updates: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for original in states:
        state = deepcopy(original)
        changed = False
        document_id = str(state["_id"])
        if state.get("as_of_chapter_id") and state.get("history_status") != "tracked":
            state["history_status"] = "tracked"
            changed = True
        if not state.get("as_of_chapter_id") and state.get("as_of_chapter_order"):
            resolved, issue = _resolve_reference(
                novel_id=novel_id,
                kind="character_state",
                document_id=document_id,
                field="as_of_chapter_order",
                legacy_value=int(state["as_of_chapter_order"]),
                volumes=volumes,
                chapters=chapters,
                mappings=mapping_by_key,
            )
            if resolved:
                state["as_of_chapter_id"] = resolved
                state["history_status"] = "tracked"
            else:
                state["history_status"] = "legacy_unknown"
                ambiguous.append(issue)
            changed = True
        facts = []
        for index, original_fact in enumerate(state.get("permanent_facts") or []):
            fact = dict(original_fact)
            if not fact.get("id"):
                fact["id"] = _stable_fact_id(state["_id"], index, fact)
                changed = True
            fact_id = str(fact["id"])
            if fact.get("source_chapter_id") and fact.get("source_status") != "tracked":
                fact["source_status"] = "tracked"
                changed = True
            if not fact.get("source_chapter_id") and fact.get("chapter_order"):
                resolved, issue = _resolve_reference(
                    novel_id=novel_id,
                    kind="permanent_fact",
                    document_id=document_id,
                    fact_id=fact_id,
                    field="chapter_order",
                    legacy_value=int(fact["chapter_order"]),
                    volumes=volumes,
                    chapters=chapters,
                    mappings=mapping_by_key,
                )
                if resolved:
                    fact["source_chapter_id"] = resolved
                    fact["source_status"] = "tracked"
                else:
                    fact["source_status"] = "legacy_unknown"
                    ambiguous.append(issue)
                changed = True
            facts.append(fact)
        if changed:
            state_updates.append(
                {
                    "_id": state["_id"],
                    "as_of_chapter_id": state.get("as_of_chapter_id"),
                    "history_status": state.get("history_status", "tracked"),
                    "permanent_facts": facts,
                }
            )

    for original in threads:
        document_id = str(original["_id"])
        update: dict[str, Any] = {}
        for legacy_field, id_field in (
            ("planted_chapter_order", "planted_chapter_id"),
            ("resolved_chapter_order", "resolved_chapter_id"),
        ):
            if original.get(id_field) or not original.get(legacy_field):
                continue
            resolved, issue = _resolve_reference(
                novel_id=novel_id,
                kind="plot_thread",
                document_id=document_id,
                field=legacy_field,
                legacy_value=int(original[legacy_field]),
                volumes=volumes,
                chapters=chapters,
                mappings=mapping_by_key,
            )
            if resolved:
                update[id_field] = resolved
            else:
                ambiguous.append(issue)
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


async def _load_novel_inputs(novel_id: str) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    database = get_database()
    novel_oid = to_object_id(novel_id)
    volumes = await database[collections.VOLUMES].find(
        {"novel_id": novel_oid}
    ).to_list(length=None)
    chapters = await database[collections.CHAPTERS].find(
        {"novel_id": novel_oid}
    ).to_list(length=None)
    states = await database[collections.CHARACTER_STATES].find(
        {"novel_id": novel_oid}
    ).to_list(length=None)
    threads = await database[collections.PLOT_THREADS].find(
        {"novel_id": novel_oid}
    ).to_list(length=None)
    return volumes, chapters, states, threads


async def rollback_novel_backup(snapshot: dict[str, Any]) -> dict[str, Any]:
    validated = validate_backup_payload(snapshot)
    if validated.get("scope") != "novel":
        raise ValueError("Chapter identity rollback requires a single-novel backup")
    novels = validated["collections"].get(collections.NOVELS) or []
    if len(novels) != 1:
        raise ValueError("Single-novel backup must contain exactly one novel")
    novel_id = str(novels[0]["_id"])
    novel_oid = to_object_id(novel_id)

    async def _restore(session):
        restored = {}
        for name in (collections.CHARACTER_STATES, collections.PLOT_THREADS):
            await get_database()[name].delete_many(
                {"novel_id": novel_oid}, session=session
            )
            documents = deepcopy(validated["collections"].get(name) or [])
            if documents:
                await get_database()[name].insert_many(documents, session=session)
            restored[name] = len(documents)
        return restored

    counts = await run_mongo_write_unit(_restore, "rollback_chapter_identity")
    return {"novel_id": novel_id, "restored": counts}


async def migrate_novel(
    novel_id: str,
    *,
    apply: bool = False,
    mappings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    database = get_database()
    novel = await database[collections.NOVELS].find_one({"_id": to_object_id(novel_id)})
    if not novel:
        raise ValueError(f"Novel {novel_id} was not found")
    volumes, chapters, states, threads = await _load_novel_inputs(novel_id)
    plan = build_migration_plan(
        volumes,
        chapters,
        states,
        threads,
        novel_id=str(novel_id),
        mappings=mappings,
    )
    report = {
        "novel_id": str(novel_id),
        "title": str(novel.get("title") or ""),
        "apply": apply,
        "state_update_count": len(plan["state_updates"]),
        "thread_update_count": len(plan["thread_updates"]),
        "ambiguous": plan["ambiguous"],
        "backup_path": None,
    }
    if not apply:
        return report
    if plan["ambiguous"]:
        raise ValueError(
            f"Migration has {len(plan['ambiguous'])} unresolved chapter references; "
            "review a mapping file before apply"
        )
    if not plan["state_updates"] and not plan["thread_updates"]:
        return report

    backup = await build_novel_backup(novel_id)
    backup_path = await save_snapshot_file(backup, prefix=f"pre-identity-{novel_id}")
    report["backup_path"] = str(backup_path)

    async def _apply(session):
        for update in plan["state_updates"]:
            fields = {
                "permanent_facts": deepcopy(update["permanent_facts"]),
                "history_status": update["history_status"],
            }
            if update.get("as_of_chapter_id"):
                fields["as_of_chapter_id"] = update["as_of_chapter_id"]
            await database[collections.CHARACTER_STATES].update_one(
                {"_id": update["_id"], "novel_id": to_object_id(novel_id)},
                {"$set": fields},
                session=session,
            )
        for update in plan["thread_updates"]:
            await database[collections.PLOT_THREADS].update_one(
                {"_id": update["_id"], "novel_id": to_object_id(novel_id)},
                {"$set": deepcopy(update["fields"])},
                session=session,
            )

    try:
        await run_mongo_write_unit(_apply, "migrate_chapter_identity")
    except Exception:
        await rollback_novel_backup(backup)
        raise
    return report


async def migrate_all(
    *,
    apply: bool = False,
    mappings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    novels = await get_database()[collections.NOVELS].find(
        {"is_deleted": False}, projection={"_id": 1}
    ).sort("_id", 1).to_list(length=None)
    reports = [
        await migrate_novel(str(novel["_id"]), apply=apply, mappings=mappings)
        for novel in novels
    ]
    return {
        "apply": apply,
        "novel_count": len(reports),
        "state_update_count": sum(item["state_update_count"] for item in reports),
        "thread_update_count": sum(item["thread_update_count"] for item in reports),
        "ambiguous_count": sum(len(item["ambiguous"]) for item in reports),
        "novels": reports,
    }


def load_mapping_file(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("format") != MAPPING_FORMAT:
        raise ValueError("Unsupported chapter identity mapping format")
    if payload.get("version") != MAPPING_VERSION:
        raise ValueError(f"Unsupported mapping version: {payload.get('version')}")
    mappings = payload.get("mappings")
    if not isinstance(mappings, list) or any(not isinstance(item, dict) for item in mappings):
        raise ValueError("Mapping file must contain a mappings list")
    _mapping_index(mappings)
    return mappings


async def _main(args) -> int:
    await connect_to_mongo()
    try:
        if args.rollback:
            snapshot = parse_backup(Path(args.rollback).read_bytes())
            report = await rollback_novel_backup(snapshot)
        else:
            mappings = load_mapping_file(args.mapping) if args.mapping else None
            if args.all_novels:
                report = await migrate_all(apply=args.apply, mappings=mappings)
            elif args.novel_id:
                report = await migrate_novel(
                    args.novel_id, apply=args.apply, mappings=mappings
                )
            else:
                raise ValueError("Provide a novel_id, --all, or --rollback")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        ambiguous_count = int(
            report.get("ambiguous_count", len(report.get("ambiguous") or []))
        )
        return 0 if ambiguous_count == 0 else 2
    finally:
        await close_mongo_connection()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("novel_id", nargs="?")
    parser.add_argument("--all", dest="all_novels", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mapping")
    parser.add_argument("--rollback")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
