"""Structured, content-free diagnostics for batch generation failures."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping
from uuid import uuid4

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.mutation import MutationConflictError
from backend.db.narrative_revision import NarrativeRevisionConflict
from backend.llm.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMError,
    LLMHTTPStatusError,
    LLMRateLimitError,
    LLMResponseError,
    LLMSchemaError,
    LLMSchemaUnsupportedError,
    LLMStructuredValidationError,
    LLMTimeoutError,
)
from backend.services.generation.chapter_pipeline import IncompleteProseGeneration
from backend.services.generation.chapter_candidate_pipeline import (
    ChapterCandidatePipelineBlocked,
)
from backend.services.generation.prose_generation import (
    ProseContinuationLimit,
    UncertainProseAttempt,
)
from backend.services.generation.reference_card_auto_creation import (
    parse_reference_card_creation_authorization,
)
from backend.services.llm.context_builder import ContextBudgetError
from backend.services.llm.pre_dispatch_boundaries import (
    pre_dispatch_boundary_code,
    restore_pre_dispatch_boundary,
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
_CANDIDATE_REPAIR_EXHAUSTED_CODES = {
    "completion": "candidate_completion_repair_exhausted",
    "outline_adherence": "candidate_adherence_repair_exhausted",
    "state": "candidate_state_repair_exhausted",
}

ACTIVE_FAILURE_PAUSE_REASONS = frozenset({
    "attempt_capacity",
    "cost_cap",
    "incomplete_scene",
    "reference_card_auto_creation_recovery",
    "reference_card_repair_exhausted",
    "source_changed",
    "uncertain_attempt",
})


class ActiveFailureEventState(str, Enum):
    NOT_ACTIVE = "not_active"
    MISSING = "missing"
    INVALID = "invalid"
    RESOLVED = "resolved"


class ActiveFailureKind(str, Enum):
    ACTIVE = "active"
    SOURCE_CHANGED = "source_changed"
    REPAIR_EXHAUSTED = "repair_exhausted"


@dataclass(frozen=True)
class ActiveFailureEventResolution:
    state: ActiveFailureEventState
    event: Mapping[str, Any] | None = None
    event_id: str | None = None
    kind: ActiveFailureKind | None = None


def incomplete_prose_pre_dispatch_boundary_code(
    completion: Mapping[str, Any],
) -> str | None:
    """Restore only a canonical boundary recorded by the prose runtime."""

    raw_reason_codes = completion.get("reason_codes")
    reason_codes = (
        list(raw_reason_codes)
        if isinstance(raw_reason_codes, (list, tuple))
        else []
    )
    for raw_code in (
        completion.get("pause_reason"),
        completion.get("completion_reason"),
        *reason_codes,
    ):
        restored = restore_pre_dispatch_boundary(raw_code)
        if restored is not None:
            return pre_dispatch_boundary_code(restored)
    return None


def _repair_event_matches_authority(
    event: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    require_final_cycle: bool,
) -> bool:
    readiness = job.get("readiness")
    planning = (
        readiness.get("planning")
        if isinstance(readiness, Mapping)
        else None
    )
    authorization = (
        planning.get("reference_card_creation_authorization")
        if isinstance(planning, Mapping)
        else None
    )
    try:
        parsed = parse_reference_card_creation_authorization(authorization)
    except (TypeError, ValueError):
        return False
    if not isinstance(readiness, Mapping):
        return False
    readiness_digest = readiness.get("digest")
    chapter_id = str(event.get("chapter_id") or "")
    cycle = event.get("cycle")
    event_authorization_revision = event.get("authorization_revision")
    job_authorization_revision = job.get("authorization_revision")
    event_policy_revision = event.get("policy_revision")
    if (
        not isinstance(readiness_digest, str)
        or len(readiness_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in readiness_digest
        )
        or type(cycle) is not int
        or cycle < 1
        or cycle > parsed.max_candidate_repair_cycles_per_chapter
        or (require_final_cycle and cycle != parsed.max_candidate_repair_cycles_per_chapter)
        or type(event_authorization_revision) is not int
        or type(job_authorization_revision) is not int
        or type(event_policy_revision) is not int
    ):
        return False
    work = readiness.get("work")
    raw_chapters = (
        work.get("chapters")
        if isinstance(work, Mapping)
        else None
    )
    if not isinstance(raw_chapters, list) or any(
        not isinstance(item, Mapping) for item in raw_chapters
    ):
        return False
    readiness_chapter_ids = tuple(
        str(item.get("chapter_id") or "") for item in raw_chapters
    )
    job_volume_id = str(job.get("volume_id") or "") or None
    return bool(
        str(job.get("novel_id") or "") == parsed.novel_id
        and str(job.get("scope") or "") == parsed.scope
        and job_volume_id == parsed.volume_id
        and readiness_chapter_ids == parsed.chapter_ids
        and chapter_id in parsed.chapter_ids
        and str(event.get("actor_owner_id") or "") == parsed.owner_id
        and str(event.get("authorization_digest") or "")
        == parsed.authorization_digest
        and str(event.get("readiness_digest") or "") == readiness_digest
        and event_authorization_revision == parsed.authorization_revision
        and job_authorization_revision == parsed.authorization_revision
        and event_policy_revision == parsed.policy_revision
        and str(event.get("event_id") or "")
        == f"{parsed.authorization_digest}:{chapter_id}:{cycle}"
    )


def _event_matches_pause_reason(
    event: Mapping[str, Any],
    pause_reason: str,
    job: Mapping[str, Any],
) -> bool:
    schema = event.get("schema_version")
    category = str(event.get("category") or "")
    code = str(event.get("code") or "")
    step = str(event.get("step") or "")
    outcome = str(event.get("outcome") or "")
    is_diagnostic = schema == DIAGNOSTIC_SCHEMA_VERSION
    is_repair = schema == "reference_card_repair_event.v1"

    if not pause_reason:
        return is_diagnostic
    if pause_reason == "source_changed":
        return is_diagnostic and category == "source_changed"
    if pause_reason == "cost_cap":
        return bool(
            is_diagnostic
            and category == "context_or_budget"
            and code == "token_budget_exceeded_before_dispatch"
        )
    if pause_reason == "attempt_capacity":
        return bool(
            is_diagnostic
            and category == "context_or_budget"
            and code == "attempt_capacity_exhausted"
        )
    if pause_reason == "incomplete_scene":
        return is_diagnostic and category == "model_output_incomplete"
    if pause_reason in {"uncertain_attempt", "uncertain_skipped"}:
        return bool(
            (
                is_diagnostic
                and category == "provider_or_transport"
                and code == "provider_attempt_uncertain"
            )
            or (
                is_repair
                and outcome == "uncertain"
                and _repair_event_matches_authority(
                    event,
                    job,
                    require_final_cycle=False,
                )
            )
        )
    if pause_reason == "process_restart":
        return bool(
            is_diagnostic
            and category == "unknown_system"
            and code == "process_restart"
            and step == "execution_recovery"
        )
    if pause_reason == "reference_card_auto_creation_recovery":
        return is_diagnostic and step == pause_reason
    if pause_reason == "final_audit":
        return is_diagnostic and step == pause_reason
    if pause_reason == "reference_card_repair_exhausted":
        return bool(
            is_repair
            and outcome in {"applied", "exhausted"}
            and _repair_event_matches_authority(
                event,
                job,
                require_final_cycle=True,
            )
        )
    return False


def resolve_active_failure_event(
    job: Mapping[str, Any],
) -> ActiveFailureEventResolution:
    """Resolve and validate only the active event explicitly owned by a Job."""
    status = str(job.get("status") or "")
    pause_reason = str(job.get("pause_reason") or "")
    active = status in {"failed", "interrupted"} or (
        status == "paused" and pause_reason in ACTIVE_FAILURE_PAUSE_REASONS
    )
    if not active:
        return ActiveFailureEventResolution(ActiveFailureEventState.NOT_ACTIVE)

    raw_pointer = job.get("current_failure_event_id")
    if not isinstance(raw_pointer, str) or not raw_pointer.strip():
        return ActiveFailureEventResolution(ActiveFailureEventState.MISSING)
    pointer = raw_pointer.strip()
    if pointer != raw_pointer or len(pointer) > 240:
        return ActiveFailureEventResolution(
            ActiveFailureEventState.INVALID,
            event_id=pointer,
        )

    matches: list[Mapping[str, Any]] = []
    for field in (
        "diagnostics",
        "reference_card_repair_events",
        "reference_card_auto_creation_events",
    ):
        raw_events = job.get(field)
        if not isinstance(raw_events, list):
            continue
        matches.extend(
            event
            for event in raw_events
            if isinstance(event, Mapping)
            and str(event.get("event_id") or "") == pointer
        )
    if len(matches) != 1 or not _event_matches_pause_reason(
        matches[0],
        pause_reason,
        job,
    ):
        return ActiveFailureEventResolution(
            ActiveFailureEventState.INVALID,
            event_id=pointer,
        )

    kind = (
        ActiveFailureKind.SOURCE_CHANGED
        if pause_reason == "source_changed"
        else ActiveFailureKind.REPAIR_EXHAUSTED
        if pause_reason == "reference_card_repair_exhausted"
        else ActiveFailureKind.ACTIVE
    )
    return ActiveFailureEventResolution(
        ActiveFailureEventState.RESOLVED,
        event=matches[0],
        event_id=pointer,
        kind=kind,
    )


def _exception_family(chain: Iterable[BaseException]) -> str:
    """Return a bounded family name without retaining exception messages."""
    values = list(chain)
    checks: tuple[tuple[type[BaseException], str], ...] = (
        (LLMStructuredValidationError, "structured_output"),
        (LLMSchemaUnsupportedError, "provider_schema_unsupported"),
        (LLMSchemaError, "structured_output"),
        (LLMAuthError, "provider_auth"),
        (LLMRateLimitError, "provider_rate_limit"),
        (LLMTimeoutError, "provider_timeout"),
        (LLMConnectionError, "provider_connection"),
        (LLMHTTPStatusError, "provider_http_status"),
        (LLMResponseError, "provider_response"),
        (json.JSONDecodeError, "structured_output"),
        (TimeoutError, "transport_timeout"),
        (ConnectionError, "transport_connection"),
        (ValueError, "validation"),
        (RuntimeError, "runtime"),
        (LLMError, "provider"),
    )
    for expected, family in checks:
        if any(isinstance(item, expected) for item in values):
            return family
    return "unknown"


def _diagnostic_outcome(category: str, code: str) -> tuple[str, list[str]]:
    """Map a diagnosis to a stable user-visible impact and recovery actions."""
    if category == "model_output_incomplete":
        return (
            "formal_prose_not_written",
            ["open_incomplete_prose", "review_provider_output_limit"],
        )
    if category == "provider_or_transport":
        if code == "provider_authentication_failed":
            return "generation_step_not_committed", ["open_provider_settings"]
        if code == "provider_rate_limited":
            return (
                "generation_step_not_committed",
                ["retry_after_provider_check", "open_provider_settings"],
            )
        return (
            "generation_step_not_committed",
            ["retry_generation_step", "open_provider_settings"],
        )
    if category == "validation_logic":
        if code == "candidate_finalization_writeback_failed":
            return (
                "formal_write_recovery_pending",
                ["resume_generation_job"],
            )
        if code in _CANDIDATE_REPAIR_EXHAUSTED_CODES.values():
            return (
                "candidate_not_committed",
                [
                    "open_affected_chapter",
                    "refresh_generation_readiness",
                    "restart_generation_job",
                ],
            )
        if code == "structured_output_invalid":
            return (
                "generated_result_rejected",
                ["review_generation_record", "retry_generation_step"],
            )
        return "generated_result_rejected", ["review_generation_record"]
    if category == "source_changed":
        return (
            "authorization_snapshot_stale",
            ["refresh_generation_readiness", "restart_generation_job"],
        )
    if category == "context_or_budget":
        return "generation_paused_before_commit", ["review_generation_authorization"]
    if category == "user_action":
        return "job_stopped_by_user", []
    return "cause_not_identified", ["review_generation_record"]


def _fingerprint(event: Mapping[str, Any]) -> str:
    details = event.get("details")
    family = details.get("exception_family") if isinstance(details, Mapping) else ""
    material = "|".join((
        _safe_text(event.get("category"), limit=80),
        _safe_text(event.get("code"), limit=100),
        _safe_text(event.get("step"), limit=60),
        _safe_text(family, limit=60),
    ))
    return sha256(material.encode("utf-8")).hexdigest()[:16]


def _event_identity(
    event: Mapping[str, Any],
    *,
    job_id: str,
    index: int,
) -> str:
    existing = _safe_text(event.get("event_id"), limit=80)
    if existing:
        return existing
    occurred_at = event.get("occurred_at") or event.get("created_at") or ""
    material = "|".join((
        job_id,
        str(index),
        _safe_text(event.get("category"), limit=80),
        _safe_text(event.get("code"), limit=100),
        _safe_text(event.get("chapter_id"), limit=80),
        _safe_text(occurred_at, limit=100),
    ))
    return sha256(material.encode("utf-8")).hexdigest()[:24]


def _enrich_event(
    event: Mapping[str, Any],
    *,
    job_id: str,
    index: int,
) -> dict[str, Any]:
    enriched = dict(event)
    impact, action_codes = _diagnostic_outcome(
        _safe_text(enriched.get("category"), limit=80),
        _safe_text(enriched.get("code"), limit=100),
    )
    enriched.setdefault("impact", impact)
    enriched.setdefault("action_codes", action_codes)
    enriched.setdefault("fingerprint", _fingerprint(enriched))
    enriched["event_id"] = _event_identity(enriched, job_id=job_id, index=index)
    return enriched


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


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_object_ids(values: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values[:limit]:
        text = str(value or "")
        if (
            len(text) == 24
            and all(character in "0123456789abcdef" for character in text)
            and text not in result
        ):
            result.append(text)
    return result


def _candidate_exception_details(
    failure: ChapterCandidatePipelineBlocked,
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    gate = getattr(failure, "gate", None)
    if gate in _CANDIDATE_REPAIR_EXHAUSTED_CODES:
        details["candidate_gate"] = gate
    repair_cycles_used = _safe_non_negative_int(
        failure.progress.repair_cycles_used
    )
    repair_limit = _safe_non_negative_int(getattr(failure, "repair_limit", None))
    if repair_cycles_used is not None:
        details["repair_cycles_used"] = repair_cycles_used
    if repair_limit is not None:
        details["repair_cycles_limit"] = repair_limit
    for field in ("consistency_issue_count", "dropped_reference_count"):
        value = _safe_non_negative_int(getattr(failure, field, None))
        if value is not None:
            details[field] = value
    affected_card_ids = _safe_object_ids(
        getattr(failure, "affected_card_ids", ())
    )
    if affected_card_ids:
        details["affected_card_ids"] = affected_card_ids
    prose_run_id = str(failure.progress.prose_run_id or "")
    if len(prose_run_id) == 24 and all(
        character in "0123456789abcdef" for character in prose_run_id
    ):
        details["prose_run_id"] = prose_run_id
    prose_run_revision = _safe_non_negative_int(
        failure.progress.prose_run_revision
    )
    if prose_run_revision is not None:
        details["prose_run_revision"] = prose_run_revision
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
    details["exception_family"] = _exception_family(chain)
    boundary_code = next(
        filter(None, (pre_dispatch_boundary_code(item) for item in chain)),
        None,
    )
    declared_diagnostic = next(
        (
            (
                getattr(item, "diagnostic_category", None),
                getattr(item, "diagnostic_code", None),
                getattr(item, "diagnostic_evidence", None),
            )
            for item in chain
            if getattr(item, "diagnostic_category", None) in CATEGORY_ORDER
            and isinstance(getattr(item, "diagnostic_code", None), str)
            and getattr(item, "diagnostic_code", None)
            and getattr(item, "diagnostic_evidence", None) in EVIDENCE_LEVELS
        ),
        None,
    )

    incomplete = next(
        (item for item in chain if isinstance(item, IncompleteProseGeneration)),
        None,
    )
    incomplete_boundary_code = (
        incomplete_prose_pre_dispatch_boundary_code(incomplete.completion)
        if incomplete is not None
        else None
    )
    if (
        incomplete is not None
        and str(incomplete.completion.get("completion_reason") or "")
        == "outline_revision_stale"
    ):
        completion = dict(incomplete.completion)
        category = "source_changed"
        code = "outline_revision_stale"
        evidence = "confirmed"
        details.update(_safe_completion(completion))
    elif incomplete is not None and incomplete_boundary_code is not None:
        completion = dict(incomplete.completion)
        category = "context_or_budget"
        code = incomplete_boundary_code
        evidence = "confirmed"
        details.update(_safe_completion(completion))
    elif incomplete is not None:
        completion = dict(incomplete.completion)
        category = "model_output_incomplete"
        code = _safe_text(
            completion.get("completion_reason") or "completion_contract_failed"
        )
        evidence = "confirmed"
        details.update(_safe_completion(completion))
    elif any(
        isinstance(item, ChapterCandidatePipelineBlocked)
        and item.code in _CANDIDATE_REPAIR_EXHAUSTED_CODES.values()
        for item in chain
    ):
        failure = next(
            item
            for item in chain
            if isinstance(item, ChapterCandidatePipelineBlocked)
            and item.code in _CANDIDATE_REPAIR_EXHAUSTED_CODES.values()
        )
        category = "validation_logic"
        code = failure.code
        evidence = "confirmed"
        details.update(_candidate_exception_details(failure))
    elif declared_diagnostic is not None:
        category, code, evidence = declared_diagnostic
    elif any(
        isinstance(
            item,
            (StaleStatePreview, NarrativeRevisionConflict, MutationConflictError),
        )
        for item in chain
    ):
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
    elif any(isinstance(item, LLMStructuredValidationError) for item in chain):
        category = "validation_logic"
        code = "structured_output_invalid"
        evidence = "confirmed"
    elif any(isinstance(item, LLMSchemaUnsupportedError) for item in chain):
        category = "provider_or_transport"
        code = "provider_schema_unsupported"
        evidence = "confirmed"
    elif any(isinstance(item, LLMSchemaError) for item in chain):
        category = "validation_logic"
        code = "structured_output_invalid"
        evidence = "confirmed"
    elif any(isinstance(item, json.JSONDecodeError) for item in chain):
        category = "validation_logic"
        code = "structured_output_invalid"
        evidence = "confirmed"
    elif any(isinstance(item, LLMAuthError) for item in chain):
        category = "provider_or_transport"
        code = "provider_authentication_failed"
        evidence = "confirmed"
    elif any(isinstance(item, LLMRateLimitError) for item in chain):
        category = "provider_or_transport"
        code = "provider_rate_limited"
        evidence = "confirmed"
    elif any(isinstance(item, (LLMTimeoutError, TimeoutError)) for item in chain):
        category = "provider_or_transport"
        code = "provider_timeout"
        evidence = (
            "confirmed"
            if any(isinstance(item, LLMTimeoutError) for item in chain)
            else "strong_inference"
        )
    elif any(isinstance(item, LLMConnectionError) for item in chain):
        category = "provider_or_transport"
        code = "provider_connection_failed"
        evidence = "confirmed"
    elif any(isinstance(item, ConnectionError) for item in chain):
        category = "provider_or_transport"
        code = "provider_connection_failed"
        evidence = "strong_inference"
    elif any(isinstance(item, LLMHTTPStatusError) for item in chain):
        category = "provider_or_transport"
        code = "provider_http_status_error"
        evidence = "confirmed"
    elif any(isinstance(item, LLMResponseError) for item in chain):
        category = "provider_or_transport"
        code = "provider_response_invalid"
        evidence = "confirmed"
    elif any(isinstance(item, LLMError) for item in chain):
        category = "provider_or_transport"
        code = "provider_or_transport_failure"
        evidence = "insufficient"
    elif any(isinstance(item, ValueError) for item in chain):
        category = "validation_logic"
        code = "validation_rejected"
        evidence = "insufficient"
    elif attempt_values:
        category = "provider_or_transport"
        code = "provider_or_transport_failure"
        evidence = "strong_inference"

    impact, action_codes = _diagnostic_outcome(category, code)
    event = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "event_id": uuid4().hex,
        "category": category,
        "code": code,
        "evidence": evidence,
        "impact": impact,
        "action_codes": action_codes,
        "source": "runtime",
        "step": _safe_text(step, limit=60) or "run_chapter",
        "chapter_id": _safe_text(chapter_id, limit=80),
        "details": details,
    }
    event["fingerprint"] = _fingerprint(event)
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


def _candidate_repair_limit(job: Mapping[str, Any]) -> int | None:
    readiness = job.get("readiness")
    planning = readiness.get("planning") if isinstance(readiness, Mapping) else None
    if not isinstance(planning, Mapping):
        return None
    repair = planning.get("candidate_repair_authorization")
    if isinstance(repair, Mapping):
        limit = _safe_non_negative_int(
            repair.get("max_repair_cycles_per_chapter")
        )
        if limit is not None:
            return limit
    finalization = planning.get("chapter_finalization_authorization")
    if isinstance(finalization, Mapping):
        return _safe_non_negative_int(finalization.get("max_repair_cycles"))
    return None


def _candidate_checkpoint_projection(
    job: Mapping[str, Any],
    *,
    chapter_id: str,
) -> dict[str, Any] | None:
    repair_limit = _candidate_repair_limit(job)
    if repair_limit is None:
        return None
    raw = job.get("candidate_pipeline_checkpoints")
    if not isinstance(raw, list):
        return None
    checkpoints = [
        item
        for item in raw[-100:]
        if isinstance(item, Mapping)
        and _safe_text(item.get("chapter_id"), limit=80) == chapter_id
    ]
    if not checkpoints:
        return None
    checkpoints.sort(key=lambda item: (
        _safe_non_negative_int(item.get("sequence")) or 0
    ))
    latest = checkpoints[-1]
    cycle = _safe_non_negative_int(latest.get("cycle"))
    if cycle is None or cycle < repair_limit:
        return None

    gate: str | None = None
    details: dict[str, Any] = {
        "repair_cycles_used": cycle,
        "repair_cycles_limit": repair_limit,
    }
    kind = latest.get("kind")
    if kind == "state_candidate":
        issue_count = _safe_non_negative_int(
            latest.get("consistency_issue_count")
        )
        dropped_count = _safe_non_negative_int(
            latest.get("dropped_reference_count")
        )
        if (issue_count or 0) > 0 or (dropped_count or 0) > 0:
            gate = "state"
            details["consistency_issue_count"] = issue_count or 0
            details["dropped_reference_count"] = dropped_count or 0
    elif kind == "outline_adherence":
        coverage = latest.get("scene_coverage")
        coverage_failed = isinstance(coverage, (list, tuple)) and any(
            isinstance(item, Mapping) and item.get("status") != "covered"
            for item in coverage[:20]
        )
        categories = latest.get("issue_categories")
        if (
            latest.get("verdict") != "pass"
            or bool(categories)
            or coverage_failed
        ):
            gate = "outline_adherence"
            if isinstance(categories, (list, tuple)):
                details["outline_issue_categories"] = [
                    _safe_text(value, limit=60) for value in categories[:20]
                ]
    elif kind == "prose_candidate":
        completion = latest.get("completion")
        if isinstance(completion, Mapping) and (
            completion.get("status") != "complete"
            or completion.get("can_write_formal_prose") is not True
            or completion.get("finish_reason") != "stop"
        ):
            gate = "completion"
            details.update(_safe_completion(completion))
    if gate is None:
        return None

    details["candidate_gate"] = gate
    source = latest.get("source")
    if isinstance(source, Mapping):
        run_ids = _safe_object_ids([source.get("source_run_id")], limit=1)
        if run_ids:
            details["prose_run_id"] = run_ids[0]
        revision = _safe_non_negative_int(source.get("source_run_revision"))
        if revision is not None:
            details["prose_run_revision"] = revision
    return {
        "code": _CANDIDATE_REPAIR_EXHAUSTED_CODES[gate],
        "details": details,
    }


def _enrich_candidate_checkpoint_failure(
    event: Mapping[str, Any],
    *,
    job: Mapping[str, Any],
) -> dict[str, Any]:
    value = dict(event)
    if (
        _safe_text(value.get("step"), limit=60) != "candidate_pipeline"
        or _safe_text(value.get("code"), limit=100)
        not in {
            "validation_rejected",
            "candidate_gate_blocked",
            "historical_unclassified_failure",
        }
    ):
        return value
    projection = _candidate_checkpoint_projection(
        job,
        chapter_id=_safe_text(value.get("chapter_id"), limit=80),
    )
    if projection is None:
        return value
    existing_details = value.get("details")
    value["details"] = {
        **(dict(existing_details) if isinstance(existing_details, Mapping) else {}),
        **projection["details"],
    }
    value["category"] = "validation_logic"
    value["code"] = projection["code"]
    value["evidence"] = "confirmed"
    impact, actions = _diagnostic_outcome("validation_logic", projection["code"])
    value["impact"] = impact
    value["action_codes"] = actions
    value.pop("fingerprint", None)
    return value

def infer_job_diagnostics(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    job_id = _safe_text(job.get("_id"), limit=80)
    persisted = job.get("diagnostics")
    if isinstance(persisted, list) and persisted:
        values = [item for item in persisted if isinstance(item, Mapping)]
        return [
            _enrich_event(
                _enrich_candidate_checkpoint_failure(item, job=job)
                if index == len(values) - 1
                else item,
                job_id=job_id,
                index=index,
            )
            for index, item in enumerate(values)
        ]
    inferred = _historical_failure(job)
    if inferred is not None:
        inferred = _enrich_candidate_checkpoint_failure(inferred, job=job)
    return [
        _enrich_event(inferred, job_id=job_id, index=0)
    ] if inferred is not None else []


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
    insufficient_count = 0
    unresolved_count = 0

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
            if event.get("evidence") == "insufficient":
                insufficient_count += 1
            if event.get("category") == "unknown_system":
                unresolved_count += 1

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
        "insufficient_event_count": insufficient_count,
        "unresolved_event_count": unresolved_count,
        "categories": categories,
        "recent_events": recent_events,
    }
