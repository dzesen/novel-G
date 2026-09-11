"""Retire restored execution authority while retaining known local usage."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from backend.db import collections as c
from backend.db.restored_authorization import RESTORED_AUTHORITY_FIELD


class BackupRestoreBusyError(RuntimeError):
    """Restoring now could erase a dispatched request's settlement."""


_JOB_USAGE_FIELDS = (
    "tokens_used", "tokens_reserved", "usage_attempt_claimed", "usage_attempt_ids",
    "usage_attempt_summaries", "attempt_slots", "active_token_reservations",
    "has_uncertain_attempts", "uncertain_attempt_ids",
)
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
_AUTHORITY_COLLECTIONS = (
    c.GENERATION_JOBS, c.AGENT_RUNTIME_RUNS, c.AGENT_RUNTIME_READINESS,
    c.PROSE_RUNS, c.BLUEPRINT_RUNS, c.IMAGE_JOBS, c.IMAGE_BATCHES,
)


def _number(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return 0


def _live_lease(value: Any, now: datetime) -> bool:
    if not value:
        return False
    if not isinstance(value, dict):
        return True
    expiry = value.get("expires_at")
    if not isinstance(expiry, datetime):
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > now


def _pending_attempts(value: Any, states: set[str]) -> bool:
    if value is None:
        return False
    if not isinstance(value, list):
        return True
    return any(not isinstance(item, dict) or item.get("state") in states for item in value)


def assert_restore_quiescent(captured: dict[str, list[dict]], now: datetime) -> None:
    """Called inside the database lock shared by attempt reservations."""
    for name in _AUTHORITY_COLLECTIONS:
        for row in captured.get(name, []):
            if row.get(RESTORED_AUTHORITY_FIELD) is not None:
                continue
            if name == c.GENERATION_JOBS:
                busy = (
                    _live_lease(row.get("execution_lease"), now)
                    or bool(row.get("interactive_execution_claim"))
                    or bool(row.get("active_token_reservations"))
                    or _number(row.get("tokens_reserved")) > 0
                    or _pending_attempts(row.get("attempt_slots"), {"claimed", "uncertain"})
                )
            elif name == c.AGENT_RUNTIME_RUNS:
                busy = (
                    _live_lease(row.get("lease"), now)
                    or _number(row.get("tokens_reserved")) > 0
                    or _number(row.get("paid_attempts_reserved")) > 0
                    or _pending_attempts(row.get("attempts"), {"reserved", "dispatched", "uncertain"})
                )
            elif name == c.PROSE_RUNS:
                busy = (
                    _live_lease(row.get("lease"), now)
                    or bool(row.get("active_token_reservation"))
                    or _number(row.get("tokens_reserved")) > _number(row.get("frozen_tokens_reserved"))
                )
            elif name == c.BLUEPRINT_RUNS:
                attempts = row.get("attempts") or {}
                busy = (
                    _live_lease(row.get("lease"), now)
                    or _number(row.get("tokens_reserved")) > 0
                    or not isinstance(attempts, dict)
                    or _pending_attempts(list(attempts.values()), {"reserved"})
                )
            elif name in {c.IMAGE_JOBS, c.IMAGE_BATCHES}:
                busy = row.get("is_terminal") is False or bool(row.get("cleanup_pending"))
            else:
                busy = False
            if busy:
                raise BackupRestoreBusyError(
                    "存在进行中的生成或尚未结算的调用，请先结束作业并处理未确认调用，再恢复备份。"
                )


def _same_scope(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("_id", "novel_id", "owner_id", "draft_id", "chapter_id"))


def _keep_job_usage(row: dict, current: dict | None) -> None:
    if not current or not _same_scope(row, current):
        return
    old_used = _number(row.get("tokens_used"))
    live_used = _number(current.get("tokens_used"))
    old_claimed = _number(row.get("usage_attempt_claimed"))
    if live_used >= old_used:
        for field in _JOB_USAGE_FIELDS:
            if field in current:
                row[field] = deepcopy(current[field])
    # Neither imported nor local history can authorize another request after
    # restore. Preserve counter floors; the safety snapshot holds all local
    # attempt evidence, including histories imported from another installation.
    row["restore_usage_checkpoint"] = {
        "local_tokens_used": live_used,
        "snapshot_tokens_used": old_used,
    }
    row["tokens_used"] = max(old_used, live_used)
    row["usage_attempt_claimed"] = max(
        old_claimed, _number(current.get("usage_attempt_claimed")),
    )


def retire_restored_authority(
    incoming: dict[str, list[dict]],
    captured: dict[str, list[dict]],
    now: datetime,
) -> dict[str, list[dict]]:
    restored = deepcopy(incoming)
    for name in _AUTHORITY_COLLECTIONS:
        current = {row.get("_id"): row for row in captured.get(name, [])}
        for row in restored.get(name, []):
            previous = current.get(row.get("_id"))
            row[RESTORED_AUTHORITY_FIELD] = {"version": 1, "restored_at": now}
            if name == c.GENERATION_JOBS:
                _keep_job_usage(row, previous)
                row["execution_lease"] = None
                row["interactive_execution_claim"] = None
                row["execution_epoch"] = min(2**63 - 1, max(
                    _number(row.get("execution_epoch")),
                    _number((previous or {}).get("execution_epoch")),
                ) + 1)
                if row.get("status") not in _TERMINAL_JOB_STATUSES:
                    row["status"] = "cancelled"
                    row["pause_reason"] = None
            elif name == c.AGENT_RUNTIME_RUNS:
                # These projections are replayed from immutable step/events.
                # Keep them intact as history and retain local accounting next
                # to them. The marker blocks lease, reserve, dispatch and bind.
                if previous and _same_scope(row, previous):
                    row["restore_usage_checkpoint"] = {
                        "local_usage": deepcopy(previous.get("usage") or {}),
                        "snapshot_usage": deepcopy(row.get("usage") or {}),
                    }
            elif name == c.AGENT_RUNTIME_READINESS:
                row["status"] = "expired"
                row["expires_at"] = now
            elif name in {c.PROSE_RUNS, c.BLUEPRINT_RUNS}:
                counters = ("tokens_used", "tokens_reserved", "frozen_tokens_reserved",
                            "provider_attempt_count", "calls_reserved")
                if previous and _same_scope(row, previous):
                    row["restore_usage_checkpoint"] = {
                        "local_usage": {key: previous[key] for key in counters if key in previous},
                        "snapshot_usage": {key: row[key] for key in counters if key in row},
                    }
                    for key in counters:
                        if key in row or key in previous:
                            row[key] = max(_number(row.get(key)), _number(previous.get(key)))
                row["lease"] = None
                if name == c.PROSE_RUNS and row.get("status") in {"active", "incomplete"}:
                    row["status"] = "stale"
                elif name == c.BLUEPRINT_RUNS and row.get("status") in {"ready", "running", "paused"}:
                    row["status"] = "failed"
                    row["failure_code"] = "blueprint_new_readiness_required"
            else:
                if name == c.IMAGE_BATCHES:
                    if row.get("status") not in {"completed", "completed_with_failures", "cancelled"}:
                        row["status"] = "cancelled"
                    for item in row.get("items") or []:
                        if isinstance(item, dict) and item.get("status") in {"pending", "starting", "running"}:
                            item.update(status="cancelled", start_claim_token=None, start_claimed_at_epoch=None)
                elif row.get("status") not in {"succeeded", "failed", "cancelled"}:
                    row["status"] = "cancelled"
                row["is_terminal"] = True
                row["cleanup_pending"] = False
    return restored
