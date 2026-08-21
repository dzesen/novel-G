"""Structured, content-free diagnostics for batch generation failures."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping

from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.generation.chapter_pipeline import IncompleteProseGeneration
from backend.services.generation.prose_generation import (
    ProseContinuationLimit,
    UncertainProseAttempt,
)
from backend.services.llm.context_builder import ContextBudgetError
from backend.services.llm.pre_dispatch_boundaries import (
    pre_dispatch_boundary_code,
)
from backend.services.novel.state_proposal import StaleStatePreview


DIAGNOSTIC_SCHEMA_VERSION = 1
CATEGORY_ORDER = (
    "model_output_incomplete",
    "provider_or_transport",
    "validation_logic",
    "source_changed",
    "context_or_budget",
    "user_action",
    "unknown_system",
)
EVIDENCE_LEVELS = ("confirmed", "strong_inference", "insufficient")
_SAFE_COMPLETION_KEYS = (
    "status",
    "requested_word_count",
    "actual_word_count",
    "raw_character_count",
    "scene_count",
    "completed_scene_count",
    "finish_reason",
    "raw_finish_reason",
    "completion_reason",
    "mode",
)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _safe_text(value: Any, *, limit: int = 100) -> str:
    return str(value or "")[:limit]


def _safe_completion(completion: Mapping[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for key in _SAFE_COMPLETION_KEYS:
        value = completion.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            details[key] = _safe_text(value) if isinstance(value, str) else value
    details["reason_codes"] = [
        _safe_text(value)
        for value in list(completion.get("reason_codes") or [])[:20]
    ]
    return details


def _attempt_details(attempts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = list(attempts)
    aliases = sorted({
        _safe_text(item.get("provider_alias"), limit=60)
        for item in values
        if item.get("provider_alias")
    })
    models = sorted({
        _safe_text(item.get("provider_model") or item.get("model"), limit=100)
        for item in values
        if item.get("provider_model") or item.get("model")
    })
    details: dict[str, Any] = {"attempt_count": len(values)}
    if aliases:
        details["provider_aliases"] = aliases
    if models:
        details["provider_models"] = models
    return details


def build_failure_diagnostic(
    exc: BaseException,
    *,
    step: str,
    chapter_id: str,
    attempts: Iterable[Mapping[str, Any]] = (),
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Classify a runtime failure without retaining prompts, prose, or raw messages."""
    chain = _exception_chain(exc)
    attempt_values = list(attempts)
    category = "unknown_system"
    code = "unclassified_failure"
    evidence = "insufficient"
    details = _attempt_details(attempt_values)
    boundary_code = next(
        filter(None, (pre_dispatch_boundary_code(item) for item in chain)),
        None,
    )

    incomplete = next(
        (item for item in chain if isinstance(item, IncompleteProseGeneration)),
        None,
    )
    if incomplete is not None:
        completion = dict(incomplete.completion)
        category = "model_output_incomplete"
        code = _safe_text(
            completion.get("completion_reason") or "completion_contract_failed"
        )
        evidence = "confirmed"
        details.update(_safe_completion(completion))
    elif any(isinstance(item, StaleStatePreview) for item in chain):
        category = "source_changed"
        code = "chapter_or_narrative_changed"
        evidence = "confirmed"
    elif any(isinstance(item, NotFoundError) for item in chain):
        category = "source_changed"
        code = "chapter_deleted_during_generation"
        evidence = "strong_inference"
    elif any(isinstance(item, ContextBudgetError) for item in chain):
        category = "context_or_budget"
        code = "context_budget_exceeded"
        evidence = "confirmed"
    elif boundary_code is not None:
        category = "context_or_budget"
        code = boundary_code
        evidence = "confirmed"
    elif any(isinstance(item, ProseContinuationLimit) for item in chain):
        category = "context_or_budget"
        code = "continuation_limit_reached"
        evidence = "confirmed"
    elif any(isinstance(item, InvalidIdError) for item in chain):
        category = "validation_logic"
        code = "invalid_internal_id"
        evidence = "confirmed"
    elif any(isinstance(item, UncertainProseAttempt) for item in chain):
        category = "provider_or_transport"
        code = "provider_attempt_uncertain"
        evidence = "confirmed"
    elif any(isinstance(item, ValueError) for item in chain):
        category = "validation_logic"
        code = "validation_rejected"
        evidence = "insufficient"
    elif attempt_values:
        category = "provider_or_transport"
        code = "provider_or_transport_failure"
        evidence = "strong_inference"

    event = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "category": category,
        "code": code,
        "evidence": evidence,
        "source": "runtime",
        "step": _safe_text(step, limit=60) or "run_chapter",
        "chapter_id": _safe_text(chapter_id, limit=80),
        "details": details,
    }
    if occurred_at is not None:
        event["occurred_at"] = occurred_at
    return event


def _historical_failure(job: Mapping[str, Any]) -> dict[str, Any] | None:
    error = job.get("error")
    status = _safe_text(job.get("status"), limit=40)
    if not isinstance(error, Mapping):
        if status == "aborted":
            return {
                "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
                "category": "user_action",
                "code": "job_aborted",
                "evidence": "confirmed",
                "source": "historical_inference",
                "step": "job",
                "chapter_id": "",
                "details": {},
                "occurred_at": job.get("updated_at") or job.get("created_at"),
            }
        return None

    message = str(error.get("message") or "")
    lower = message.lower()
    reason_codes = [
        code for code in (
            "finish_reason_length",
            "finish_reason_content_filter",
            "finish_reason_tool_call",
            "finish_reason_cancelled",
            "finish_reason_error",
            "scenes_incomplete",
            "below_minimum_word_ratio",
        )
        if code in lower
    ]
    if reason_codes:
        category = "model_output_incomplete"
        code = "historical_completion_contract_failed"
        evidence = "strong_inference"
        details: dict[str, Any] = {"reason_codes": reason_codes}
    elif "objectid" in lower or "invalid id" in lower or "invalid_id" in lower:
        category = "validation_logic"
        code = "historical_invalid_internal_id"
        evidence = "strong_inference"
        details = {}
    elif status == "interrupted" or job.get("pause_reason") == "uncertain_attempt":
        category = "provider_or_transport"
        code = "historical_provider_attempt_uncertain"
        evidence = "strong_inference"
        details = {}
    else:
        category = "unknown_system"
        code = "historical_unclassified_failure"
        evidence = "insufficient"
        details = {"error_type": _safe_text(error.get("type"), limit=80)}

    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "category": category,
        "code": code,
        "evidence": evidence,
        "source": "historical_inference",
        "step": _safe_text(error.get("step"), limit=60) or "run_chapter",
        "chapter_id": _safe_text(error.get("chapter_id"), limit=80),
        "details": details,
        "occurred_at": job.get("updated_at") or job.get("created_at"),
    }

def infer_job_diagnostics(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    persisted = job.get("diagnostics")
    if isinstance(persisted, list) and persisted:
        return [dict(item) for item in persisted if isinstance(item, Mapping)]
    inferred = _historical_failure(job)
    return [inferred] if inferred is not None else []


def summarize_jobs(
    jobs: Iterable[Mapping[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    selected = list(jobs)[: max(1, int(limit))]
    category_events: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    all_events: list[tuple[str, dict[str, Any]]] = []
    affected_jobs: set[str] = set()
    inferred_count = 0

    for job in selected:
        job_id = _safe_text(job.get("_id"), limit=80)
        for event in reversed(infer_job_diagnostics(job)):
            category = _safe_text(event.get("category"), limit=80)
            if category not in CATEGORY_ORDER:
                category = "unknown_system"
                event = {**event, "category": category}
            category_events[category].append((job_id, event))
            all_events.append((job_id, event))
            affected_jobs.add(job_id)
            if event.get("source") == "historical_inference":
                inferred_count += 1

    categories = []
    for category in CATEGORY_ORDER:
        entries = category_events.get(category, [])
        if not entries:
            continue
        evidence_counts = {
            level: sum(1 for _, event in entries if event.get("evidence") == level)
            for level in EVIDENCE_LEVELS
        }
        categories.append({
            "category": category,
            "event_count": len(entries),
            "job_count": len({job_id for job_id, _ in entries}),
            "evidence_counts": evidence_counts,
        })

    recent_events = [
        {"job_id": job_id, **event}
        for job_id, event in all_events[:10]
    ]
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "window_job_count": len(selected),
        "affected_job_count": len(affected_jobs),
        "event_count": len(all_events),
        "inferred_event_count": inferred_count,
        "categories": categories,
        "recent_events": recent_events,
    }
