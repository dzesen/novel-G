"""Versioned, explicitly projected Job views; journals never become API fields."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from backend.scene_contract_versions import current_outline_adherence_decision
from backend.services.generation.failure_diagnostics import infer_job_diagnostics
from backend.services.generation.job_relations import related_prose_run_ids


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PublicGenerationParams(_PublicModel):
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    allow_failure_retry: bool = False
    prose_continuation_policy: dict[str, JsonValue] | None = None


class PublicJobProgress(_PublicModel):
    """Both legacy checkpoints and committed candidate-pipeline receipts."""

    schema_version: str | None = None
    chapter_id: str | None = None
    order_index: int | None = None
    status: str | None = None
    finalization_status: str | None = None
    steps_done: list[str] = Field(default_factory=list)
    steps_skipped: list[str] = Field(default_factory=list)
    tokens: int = 0
    consistency_issues: list[dict[str, JsonValue]] = Field(default_factory=list)
    outline_adherence: dict[str, JsonValue] | None = None
    facts_added: int = 0
    threads_advanced: int = 0
    summary_written: bool = False
    dropped_ids: dict[str, JsonValue] = Field(default_factory=dict)
    truncations: list[dict[str, JsonValue]] = Field(default_factory=list)
    prose_completion: dict[str, JsonValue] | None = None
    incomplete_prose: dict[str, JsonValue] | None = None
    step_outcomes: list[dict[str, JsonValue]] = Field(default_factory=list)
    notices: list[dict[str, JsonValue]] = Field(default_factory=list)
    source: dict[str, JsonValue] | None = None
    state_proposal_id: str | None = None
    repair_cycles_used: int = 0
    attempt_count: int = 0
    truncation_count: int = 0
    outline_issue_categories: list[str] = Field(default_factory=list)
    scene_coverage_count: int = 0
    consistency_issue_count: int = 0
    completed_at: str | None = None


class PublicGenerationJob(_PublicModel):
    schema_version: Literal["generation_job_public.v1"] = "generation_job_public.v1"
    id: str = Field(alias="_id")
    novel_id: str
    root_job_id: str
    parent_job_id: str | None = None
    required_book_successor_parent_job_id: str | None = None
    job_kind: str | None = None
    scope: str | None = None
    volume_id: str | None = None
    status: str | None = None
    pause_reason: str | None = None
    checkpoint_interval: int | None = None
    outline_deviation_policy: str | None = None
    generation_params: PublicGenerationParams = Field(default_factory=PublicGenerationParams)
    token_budget: int | None = None
    tokens_used: int = 0
    tokens_reserved: int = 0
    current_chapter_id: str | None = None
    progress: list[PublicJobProgress] = Field(default_factory=list)
    last_checkpoint_index: int = 0
    error: dict[str, JsonValue] | None = None
    diagnostics: list[dict[str, JsonValue]] = Field(default_factory=list)
    related_prose_run_ids: list[str] = Field(default_factory=list)
    reference_card_auto_creation_events: list[dict[str, JsonValue]] = Field(default_factory=list)
    reference_card_repair_events: list[dict[str, JsonValue]] = Field(default_factory=list)
    completion_audit: dict[str, JsonValue] | None = None
    prose_continuation_authorization: dict[str, JsonValue] | None = None
    readiness: dict[str, JsonValue] | None = None
    usage_attempt_capacity: int = 0
    usage_attempt_claimed: int = 0
    usage_attempt_summaries: list[dict[str, JsonValue]] = Field(default_factory=list)
    has_uncertain_attempts: bool = False
    resume_original_writeback_available: bool = False
    current_stage: str | None = None
    detail_version: str | None = None
    progress_count: int = 0
    progress_chapter_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class PublicJobSummary(_PublicModel):
    schema_version: Literal["generation_job_summary.v1"] = "generation_job_summary.v1"
    id: str = Field(alias="_id")
    novel_id: str
    root_job_id: str
    parent_job_id: str | None = None
    job_kind: str | None = None
    scope: str | None = None
    volume_id: str | None = None
    status: str | None = None
    pause_reason: str | None = None
    token_budget: int | None = None
    tokens_used: int = 0
    tokens_reserved: int = 0
    current_chapter_id: str | None = None
    usage_attempt_capacity: int = 0
    usage_attempt_claimed: int = 0
    has_uncertain_attempts: bool = False
    progress_count: int = 0
    progress_chapter_count: int = 0
    diagnostics_count: int = 0
    latest_diagnostic: dict[str, JsonValue] | None = None
    related_prose_run_ids: list[str] = Field(default_factory=list)
    provider_aliases: list[str] = Field(default_factory=list)
    provider_models: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    current_stage: str | None = None
    detail_version: str
    created_at: str | None = None
    updated_at: str | None = None


class PublicJobPage(_PublicModel):
    items: list[PublicJobSummary]
    next_cursor: str | None = None


def _json_value(value: Any) -> JsonValue:
    """Convert known storage scalars only after selecting the public fields."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("Unsupported value in generation Job public projection")


def _pick(value: Any, fields) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        return {}
    return {field: _json_value(value[field]) for field in fields if field in value}


def _records(value: Any, fields) -> list[dict[str, JsonValue]]:
    return [_pick(item, fields) for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _text(value: Any, limit: int = 160) -> str | None:
    return (value.strip()[:limit] or None) if isinstance(value, str) else None


def _texts(value: Any, *, item_limit=160, count_limit=100) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value[:count_limit] if (text := _text(item, item_limit))]


def public_job_error(value: Any) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, JsonValue] = {}
    for field, limit in (("step", 80), ("chapter_id", 100), ("audit_digest", 128)):
        if (text := _text(value.get(field), limit)) is not None:
            result[field] = text
    for field, item_limit, count_limit in (
        ("candidate_ids", 100, 100), ("candidate_names", 160, 100),
        ("reason_codes", 100, 50), ("blocking_issue_codes", 100, 100),
    ):
        items = _texts(value.get(field), item_limit=item_limit, count_limit=count_limit)
        if items:
            result[field] = items
    auto = value.get("auto_creation")
    if isinstance(auto, Mapping) and auto.get("outcome") in {
        "manual_review_required", "auto_created", "not_applicable", "repair_exhausted",
    }:
        count = auto.get("created_count")
        projected: dict[str, JsonValue] = {
            "outcome": auto["outcome"],
            "created_count": max(0, count) if type(count) is int else 0,
            "deny_reasons": _texts(auto.get("deny_reasons"), item_limit=80, count_limit=50),
            "denials": [],
        }
        if (pause_reason := _text(auto.get("pause_reason"), 80)) is not None:
            projected["pause_reason"] = pause_reason
        for denial in list(auto.get("denials") or [])[:100]:
            if not isinstance(denial, Mapping) or not (reason := _text(denial.get("reason"), 80)):
                continue
            item = {"reason": reason}
            if candidate_id := _text(denial.get("candidate_id"), 100):
                item["candidate_id"] = candidate_id
            projected["denials"].append(item)
        result["auto_creation"] = projected
    return result or None


_DIAGNOSTIC_FIELDS = (
    "schema_version", "event_id", "fingerprint", "category", "code", "evidence",
    "impact", "action_codes", "source", "step", "chapter_id", "occurred_at",
)
_DIAGNOSTIC_DETAIL_FIELDS = (
    "status", "requested_word_count", "actual_word_count", "raw_character_count",
    "scene_count", "completed_scene_count", "finish_reason", "raw_finish_reason",
    "completion_reason", "mode", "reason_codes", "attempt_count", "provider_aliases",
    "provider_models", "candidate_gate", "repair_cycles_used", "repair_cycles_limit",
    "consistency_issue_count", "dropped_reference_count", "affected_card_ids",
    "outline_issue_categories", "prose_run_id", "prose_run_revision", "repair_component",
    "component_used", "component_limit", "next_step", "exception_family",
    "validation_code", "termination_reason_code",
)


def public_diagnostics(job: Mapping[str, Any]) -> list[dict[str, JsonValue]]:
    return [{**_pick(event, _DIAGNOSTIC_FIELDS), "details": _pick(event.get("details"), _DIAGNOSTIC_DETAIL_FIELDS)} for event in infer_job_diagnostics(job)]


def _public_progress(value: Any) -> list[dict[str, JsonValue]]:
    records = []
    if not isinstance(value, (list, tuple)):
        return records
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        item = _pick(raw, PublicJobProgress.model_fields)
        review = raw.get("outline_adherence")
        if isinstance(review, dict) and current_outline_adherence_decision(review) is not None:
            item["outline_adherence"] = _pick(review, (
                "evidence_schema_version", "issue_policy_version", "decision",
                "summary", "local_issues", "scene_coverage", "reason_codes",
            ))
            if not isinstance(item["outline_adherence"].get("local_issues"), list):
                item["outline_adherence"]["local_issues"] = []
        elif isinstance(review, dict) and review.get("verdict") in {"pass", "warn", "fail"}:
            item["outline_adherence"] = _pick(review, ("verdict", "summary", "scene_coverage", "issues"))
            if not isinstance(item["outline_adherence"].get("issues"), list):
                item["outline_adherence"]["issues"] = []
        else:
            item.pop("outline_adherence", None)
        if isinstance(item.get("source"), dict):
            item["source"] = _pick(item["source"], ("source_run_id", "source_run_revision", "source_content_digest"))
        if isinstance(item.get("incomplete_prose"), dict):
            item["incomplete_prose"] = _pick(item["incomplete_prose"], (
                "chapter_id", "source_run_id", "source_run_revision", "status",
                "pause_reason", "reason_codes", "scene_count", "completed_scene_count",
            ))
        records.append(item)
    return records


_AUTOMATION_FIELDS = (
    "schema_version", "event_id", "chapter_id", "authorization_digest", "readiness_digest",
    "authorization_revision", "policy_revision", "outcome", "created_count", "mappings",
    "deny_reasons", "denials", "limit_usage", "source_mutation_id", "mutation_receipt_id",
    "occurred_at", "cycle", "resolution", "created_reference_card_candidate_ids",
    "reason", "proposal_digest",
)
_CONTINUATION_FIELDS = (
    "policy", "authorization_revision", "max_base_calls", "max_automatic_continuation_calls",
    "max_logical_prose_calls", "base_output_token_bound", "continuation_output_token_bound",
    "conservative_base_token_bound", "conservative_continuation_token_bound",
    "conservative_token_bound", "conservative_total_token_bound", "token_bound_known",
    "budget_coverage", "token_budget", "readiness_digest",
)


def project_job(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    # This allowlist is the contract. Never start with dict(job) and remove a few keys.
    projected_fields = {
        "generation_params", "progress", "diagnostics", "error", "readiness",
        "reference_card_auto_creation_events", "reference_card_repair_events",
        "prose_continuation_authorization",
    }
    source_fields = tuple("_id" if key == "id" else key for key in PublicGenerationJob.model_fields if key not in projected_fields)
    out = _pick(job, source_fields)
    out["schema_version"] = "generation_job_public.v1"
    parent_id = job.get("required_book_successor_parent_job_id")
    out["parent_job_id"] = str(parent_id) if parent_id is not None else None
    out["root_job_id"] = str(parent_id) if parent_id is not None else str(job["_id"])
    out["progress_count"] = len(job.get("progress") or [])
    out["progress_chapter_count"] = len({str(item.get("chapter_id")) for item in job.get("progress", []) if isinstance(item, Mapping) and item.get("chapter_id")})
    out["detail_version"] = detail_version(job)
    out["generation_params"] = _pick(job.get("generation_params"), PublicGenerationParams.model_fields)
    out["progress"] = _public_progress(job.get("progress"))
    # Inference still reads the original evidence, before the public projection discards it.
    out["diagnostics"] = public_diagnostics(job)
    out["related_prose_run_ids"] = list(related_prose_run_ids(job))
    out["error"] = public_job_error(job.get("error"))
    for field in ("reference_card_auto_creation_events", "reference_card_repair_events"):
        if field in job:
            out[field] = _records(job[field], _AUTOMATION_FIELDS)
    if job.get("prose_continuation_authorization") is not None:
        out["prose_continuation_authorization"] = _pick(job["prose_continuation_authorization"], _CONTINUATION_FIELDS)
    readiness = job.get("readiness")
    if isinstance(readiness, Mapping):
        planning = readiness.get("planning")
        out["readiness"] = {
            **_pick(readiness, ("version", "status", "digest")),
            "planning": {"prose_strategy": _pick(
                planning.get("prose_strategy") if isinstance(planning, Mapping) else None,
                ("provider_alias", "provider_model"),
            )},
        }
    stage = (job.get("required_book_successor_action") or {}).get("stage")
    if stage is None:
        stage = (job.get("required_book_successor_journal") or {}).get("phase")
    if stage in {
        "review", "state", "finalization", "book_audit", "completed", "blocked",
    }:
        out["current_stage"] = stage
    return PublicGenerationJob.model_validate(out).model_dump(mode="json", by_alias=True, exclude_unset=True)


def _count(job: Mapping[str, Any], count_field: str, array_field: str) -> int:
    count = job.get(count_field)
    return count if type(count) is int else len(job.get(array_field) or [])


def detail_version(job: Mapping[str, Any]) -> str:
    """A read token, never an execution authorization or narrative revision."""
    value = _pick(job, (
        "updated_at", "status", "pause_reason", "current_chapter_id",
        "last_checkpoint_index", "tokens_used", "tokens_reserved",
        "usage_attempt_claimed", "has_uncertain_attempts",
    ))
    value["error"] = _pick(public_job_error(job.get("error")), (
        "step", "chapter_id", "reason_codes", "blocking_issue_codes", "audit_digest",
    ))
    for target, source in (
        ("progress_count", "progress"), ("diagnostics_count", "diagnostics"),
        ("auto_creation_event_count", "reference_card_auto_creation_events"),
        ("repair_event_count", "reference_card_repair_events"),
    ):
        value[target] = _count(job, target, source)
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def project_job_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    fields = tuple("_id" if key == "id" else key for key in PublicJobSummary.model_fields)
    out = _pick(job, fields)
    parent_id = job.get("required_book_successor_parent_job_id")
    out.update({
        "schema_version": "generation_job_summary.v1",
        "parent_job_id": str(parent_id) if parent_id is not None else None,
        "root_job_id": str(parent_id) if parent_id is not None else str(job["_id"]),
        "detail_version": detail_version(job),
        "progress_count": _count(job, "progress_count", "progress"),
        "diagnostics_count": _count(job, "diagnostics_count", "diagnostics"),
        "related_prose_run_ids": list(related_prose_run_ids(job)),
    })
    events = public_diagnostics(job)
    out["latest_diagnostic"] = events[-1] if events else None
    out["reason_codes"] = sorted(set([
        *_texts((public_job_error(job.get("error")) or {}).get("reason_codes")),
        *_texts(job.get("diagnostic_code_values")),
        *[event["code"] for event in events if isinstance(event.get("code"), str)],
    ]))
    planning = (job.get("readiness") or {}).get("planning") or {}
    strategy = planning.get("prose_strategy") or {}
    for output, strategy_field in (("provider_aliases", "provider_alias"), ("provider_models", "provider_model")):
        values = {text for event in events for text in _texts(event.get("details", {}).get(output))}
        values.update(_texts(job.get(output)))
        if text := _text(strategy.get(strategy_field)):
            values.add(text)
        out[output] = sorted(values)
    stage = (job.get("required_book_successor_action") or {}).get("stage")
    if stage is None:
        stage = (job.get("required_book_successor_journal") or {}).get("phase")
    if stage in {"review", "state", "finalization", "book_audit", "completed", "blocked"}:
        out["current_stage"] = stage
    return PublicJobSummary.model_validate(out).model_dump(mode="json", by_alias=True)
