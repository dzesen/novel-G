"""Recognize a settled, positively rejected initial outline after reauthorization.

This never rewrites the attempt ledger or invents a missing mutation receipt.
Only the exact two-call truncation evidence qualifies; uncertain or unexplained
responses, later paid work, and calls without renewed authority remain blocked.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from backend.services.llm.generation_runtime import safe_structured_repair_failure_diagnostics


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def recognizes_reauthorized_outline_truncation(
    job: Mapping[str, Any], *, execution_id: str, novel_id: str,
    chapter_id: str, readiness_digest: str, narrative_revision: int,
    authorization_revision: int, attempt_slots: Sequence[Mapping[str, Any]],
) -> bool:
    readiness = job.get("readiness")
    if (
        str(job.get("_id") or "") != execution_id
        or str(job.get("novel_id") or "") != novel_id
        or job.get("expected_narrative_revision") != narrative_revision
        or not isinstance(readiness, Mapping)
        or readiness.get("digest") != readiness_digest
        or type(authorization_revision) is not int or authorization_revision < 2
        or job.get("authorization_revision") != authorization_revision
        or job.get("has_uncertain_attempts")
        or any(isinstance(c, Mapping) and str(c.get("chapter_id") or "") == chapter_id
               for c in job.get("candidate_pipeline_checkpoints", ()))
    ):
        return False
    slots = list(attempt_slots)
    durable = [s for s in job.get("attempt_slots", ())
               if isinstance(s, Mapping) and str(s.get("chapter_id") or "") == chapter_id]
    if (
        slots != durable or len(slots) != 2
        or tuple(s.get("phase") for s in slots) != ("primary", "repair")
        or any(str(s.get("chapter_id") or "") != chapter_id
               or s.get("step_id") != "outline" or s.get("state") != "accounted"
               for s in slots)
    ):
        return False
    ids = [s.get("attempt_id") for s in slots]
    providers = [s.get("provider_alias") for s in slots]
    if (any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != 2
            or any(not isinstance(p, str) or not p for p in providers)
            or providers[0] != providers[1]):
        return False
    claimed = [_utc(s.get("claimed_at")) for s in slots]
    settled = [_utc(s.get("accounted_at")) for s in slots]
    if (any(t is None for t in (*claimed, *settled))
            or any(start > end for start, end in zip(claimed, settled))
            or settled[0] > claimed[1]):
        return False
    events = job.get("diagnostics")
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, Mapping):
            continue
        details = event.get("details")
        occurred = _utc(event.get("occurred_at"))
        if (
            event.get("schema_version") != 1 or event.get("source") != "runtime"
            or event.get("step") != "candidate_pipeline" or event.get("chapter_id") != chapter_id
            or event.get("category") != "validation_logic"
            or event.get("code") != "structured_output_truncated"
            or event.get("evidence") != "confirmed" or event.get("impact") != "generated_result_rejected"
            or not isinstance(event.get("event_id"), str) or not event["event_id"]
            or not isinstance(details, Mapping) or type(details.get("attempt_count")) is not int
            or details["attempt_count"] != 2 or details.get("provider_aliases") != [providers[0]]
            or occurred is None or occurred < settled[1]
        ):
            continue
        safe = safe_structured_repair_failure_diagnostics(details.get("structured_validation"))
        if safe is None or any(safe.get(key) != "length"
                               for key in ("primary_finish_reason", "repair_finish_reason")):
            continue
        if all(safe[key]["issues"] == [{"path": "$", "error_type": "json_decode_error"}]
               for key in ("primary_validation", "repair_validation")):
            return True
    return False
