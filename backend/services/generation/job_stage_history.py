"""Read-only stage history from persisted attempts and completion receipts."""
from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any

STAGE_HISTORY_LIMIT = 200
_ID = re.compile(r"^[0-9a-f]{24}$")

def _chapter(value: Any) -> str | None:
    text = str(value or "")
    return text if _ID.fullmatch(text) else None

def _time(value: Any) -> str | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None

def _stage(step: Any) -> str:
    value = str(step or "")
    if value in {"outline", "chapter_outline"}: return "outline"
    if re.fullmatch(r"candidate-prose(?:(?:-repair)?:[0-9]+)?", value) or value in {"prose", "chapter_content"}: return "prose"
    if re.fullmatch(r"candidate-state(?:-(?:repair|retry):[0-9]+)?", value) or value in {"state", "chapter_state"}: return "state"
    if (
        re.fullmatch(r"candidate-outline-adherence(?:-(?:repair|retry):[0-9]+)?", value)
        or re.fullmatch(r"candidate-adherence(?:-retry:[0-9]+)?", value)
        or value in {"review", "adherence"}
    ): return "review"
    return "other"

def project_stage_history(job: Mapping[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    readiness = job.get("readiness")
    work = readiness.get("work") if isinstance(readiness, Mapping) else None
    chapters = work.get("chapters") if isinstance(work, Mapping) else None
    orders = {}
    for row in chapters if isinstance(chapters, list) else []:
        if isinstance(row, Mapping) and _chapter(row.get("chapter_id")) and type(row.get("order_index")) is int:
            orders[_chapter(row["chapter_id"])] = row["order_index"]
    for index, slot in enumerate(job.get("attempt_slots") or []):
        if not isinstance(slot, Mapping): continue
        state = slot.get("state")
        if state not in {"claimed", "accounted", "uncertain"}: continue
        chapter = _chapter(slot.get("chapter_id"))
        stage = _stage(slot.get("step_id"))
        phase = slot.get("phase") if slot.get("phase") in {"primary", "repair", "text"} else "other"
        retry = re.fullmatch(r"candidate-(?:state|adherence)-retry:([0-9]+)", str(slot.get("step_id") or ""))
        tokens = slot.get("charged_tokens")
        events.append({
            "id": "request-" + sha256(str(slot.get("attempt_id") or index).encode()).hexdigest()[:24],
            "kind": "request", "stage": stage, "phase": phase,
            "status": "settled" if state == "accounted" else "running" if state == "claimed" and job.get("status") in {"running", "completion_running", "pausing"} else "uncertain",
            "chapter_id": chapter, "order_index": orders.get(chapter),
            "started_at": _time(slot.get("claimed_at")),
            "finished_at": _time(slot.get("accounted_at")) if state == "accounted" else None,
            "tokens": max(0, tokens) if type(tokens) is int else None,
            "retry_index": min(int(retry[1]), 10000) if retry and len(retry[1]) < 8 else None,
        })
    for index, progress in enumerate(job.get("progress") or []):
        if not isinstance(progress, Mapping) or progress.get("finalization_status") != "committed": continue
        chapter = _chapter(progress.get("chapter_id"))
        if chapter is None: continue
        order = progress.get("order_index")
        events.append({"id": f"chapter-{chapter}-{index}", "kind": "chapter_complete", "stage": "completion", "status": "completed",
                       "chapter_id": chapter, "order_index": order if type(order) is int else orders.get(chapter),
                       "started_at": _time(progress.get("completed_at")), "finished_at": _time(progress.get("completed_at"))})
    status = job.get("status")
    if status in {"completed", "paused", "failed", "interrupted", "aborted"}:
        events.append({"id": "job-" + status, "kind": "job_status", "stage": "job", "status": status,
                       "started_at": _time(job.get("updated_at")), "finished_at": _time(job.get("updated_at"))})
    # Stable input order breaks equal timestamps, preserving dispatch-before-receipt.
    events.sort(key=lambda event: event.get("started_at") or "")
    return {"stage_history": events[-STAGE_HISTORY_LIMIT:], "stage_history_total": len(events)}
