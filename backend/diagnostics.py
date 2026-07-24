"""Read-only local recovery and readiness diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from backend.db import collections
from backend.db.mongo import close_mongo_connection, connect_to_mongo, get_database
from backend.db.utils import to_object_id
from backend.migrations.chapter_identity_migration import migrate_all, migrate_novel
from backend.runtime_reports import read_runtime_report
from backend.services.backup.backup_service import get_backup_status
from backend.services.novel.narrative_timeline import narrative_timeline


BAD_JOURNAL_STATUSES = ("failed", "unsupported", "conflict", "quarantined")
BAD_PROPOSAL_STATUSES = ("stale", "expired", "failed")


def _id(value: Any) -> str:
    return str(value) if value is not None else ""


async def collect_diagnostics(novel_id: str | None = None) -> dict[str, Any]:
    database = get_database()
    novel_filter = {"novel_id": to_object_id(novel_id)} if novel_id else {}
    journals = await database[collections.MUTATION_JOURNALS].find(
        {**novel_filter, "status": {"$in": list(BAD_JOURNAL_STATUSES)}},
        projection={
            "novel_id": 1,
            "operation": 1,
            "status": 1,
            "recovery_attempts": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1).to_list(length=200)
    jobs = await database[collections.GENERATION_JOBS].find(
        {**novel_filter, "has_uncertain_attempts": True, "is_deleted": False},
        projection={
            "novel_id": 1,
            "status": 1,
            "pause_reason": 1,
            "uncertain_attempt_ids": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1).to_list(length=200)
    proposals = await database[collections.STATE_PREVIEWS].find(
        {**novel_filter, "status": {"$in": list(BAD_PROPOSAL_STATUSES)}},
        projection={
            "novel_id": 1,
            "chapter_id": 1,
            "status": 1,
            "stale_reason": 1,
            "updated_at": 1,
            "expires_at": 1,
        },
    ).sort("updated_at", -1).to_list(length=200)

    if novel_id:
        identity = await migrate_novel(novel_id, apply=False)
        novel_ids = [novel_id]
    else:
        identity = await migrate_all(apply=False)
        novels = await database[collections.NOVELS].find(
            {"is_deleted": False}, projection={"_id": 1}
        ).sort("_id", 1).to_list(length=None)
        novel_ids = [_id(item["_id"]) for item in novels]

    timeline = []
    for target_novel_id in novel_ids:
        timeline.append(await narrative_timeline.audit(target_novel_id))

    journal_items = [
        {
            "journal_id": _id(item.get("_id")),
            "novel_id": _id(item.get("novel_id")),
            "operation": str(item.get("operation") or ""),
            "status": str(item.get("status") or ""),
            "recovery_attempts": int(item.get("recovery_attempts") or 0),
            "updated_at": item.get("updated_at"),
        }
        for item in journals
    ]
    job_items = [
        {
            "job_id": _id(item.get("_id")),
            "novel_id": _id(item.get("novel_id")),
            "status": str(item.get("status") or ""),
            "pause_reason": item.get("pause_reason"),
            "uncertain_attempt_ids": list(item.get("uncertain_attempt_ids") or []),
            "updated_at": item.get("updated_at"),
        }
        for item in jobs
    ]
    proposal_items = [
        {
            "proposal_id": _id(item.get("_id")),
            "novel_id": _id(item.get("novel_id")),
            "chapter_id": _id(item.get("chapter_id")),
            "status": str(item.get("status") or ""),
            "reason": str(item.get("stale_reason") or ""),
            "updated_at": item.get("updated_at"),
            "expires_at": item.get("expires_at"),
        }
        for item in proposals
    ]
    ambiguous_count = int(
        identity.get("ambiguous_count", len(identity.get("ambiguous") or []))
    )
    pending_identity_writes = int(identity.get("state_update_count") or 0) + int(
        identity.get("thread_update_count") or 0
    )
    stale_timeline_count = sum(bool(item.get("stale_chapter_ids")) for item in timeline)
    last_verification = read_runtime_report("verification")
    actions = []
    if journal_items:
        actions.append("Inspect mutation journals and run the scoped recovery endpoint/command")
    if job_items:
        actions.append("Acknowledge each uncertain attempt before retrying or skipping its job")
    if proposal_items:
        actions.append("Regenerate stale/expired/failed state proposals when still needed")
    if ambiguous_count or pending_identity_writes:
        actions.append(
            "Review chapter identity dry-run results before applying the migration"
        )
    if stale_timeline_count:
        actions.append("Run the timeline refresh endpoint for novels with stale chapter IDs")
    if not last_verification or last_verification.get("status") != "passed":
        actions.append("Run verify.bat and resolve the first failing local release check")

    return {
        "format": "novel-generator-local-diagnostics",
        "version": 1,
        "scope": {"novel_id": novel_id},
        "ready": not actions,
        "actions": actions,
        "mutation_journals": journal_items,
        "uncertain_jobs": job_items,
        "state_proposals": proposal_items,
        "identity_migration": identity,
        "timeline": timeline,
        "backup": await get_backup_status(),
        "last_verification": last_verification,
        "last_restore": read_runtime_report("restore"),
    }


async def _main(args: argparse.Namespace) -> int:
    await connect_to_mongo()
    try:
        report = await collect_diagnostics(args.novel_id)
        rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(f"Diagnostics written to {args.output}")
        else:
            print(rendered)
        return 0 if report["ready"] else 2
    finally:
        await close_mongo_connection()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel", dest="novel_id")
    parser.add_argument("--output", type=Path)
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
