"""Conservative local retention maintenance; dry-run unless explicitly confirmed."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.db import collections
from backend.db.mongo import close_mongo_connection, connect_to_mongo, get_database


MIN_COMPLETED_JOURNAL_DAYS = 7
DEFAULT_COMPLETED_JOURNAL_DAYS = 30


def _cutoff(days: int, now: datetime | None = None) -> datetime:
    if days < MIN_COMPLETED_JOURNAL_DAYS:
        raise ValueError(
            f"completed journal retention must be at least {MIN_COMPLETED_JOURNAL_DAYS} days"
        )
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


async def audit_retention(
    *, completed_journal_days: int = DEFAULT_COMPLETED_JOURNAL_DAYS
) -> dict[str, Any]:
    cutoff = _cutoff(completed_journal_days)
    collection = get_database()[collections.MUTATION_JOURNALS]
    eligible = await collection.count_documents(
        {"status": "completed", "updated_at": {"$lt": cutoff}}
    )
    protected = await collection.count_documents(
        {"status": {"$in": ["failed", "unsupported", "conflict", "quarantined"]}}
    )
    return {
        "apply": False,
        "completed_journal_days": completed_journal_days,
        "cutoff": cutoff,
        "completed_journals_eligible": int(eligible),
        "protected_problem_journals": int(protected),
    }


async def apply_retention(
    *, completed_journal_days: int = DEFAULT_COMPLETED_JOURNAL_DAYS
) -> dict[str, Any]:
    report = await audit_retention(completed_journal_days=completed_journal_days)
    result = await get_database()[collections.MUTATION_JOURNALS].delete_many(
        {"status": "completed", "updated_at": {"$lt": report["cutoff"]}}
    )
    return {
        **report,
        "apply": True,
        "completed_journals_deleted": int(result.deleted_count),
    }


async def _main(args: argparse.Namespace) -> int:
    await connect_to_mongo()
    try:
        if args.apply and not args.confirm_delete_completed_journals:
            raise ValueError(
                "--apply requires --confirm-delete-completed-journals"
            )
        report = (
            await apply_retention(completed_journal_days=args.completed_journal_days)
            if args.apply
            else await audit_retention(completed_journal_days=args.completed_journal_days)
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        await close_mongo_connection()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--completed-journal-days",
        type=int,
        default=DEFAULT_COMPLETED_JOURNAL_DAYS,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-delete-completed-journals", action="store_true")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
