"""Validate Provider state facts and deterministically account every outcome."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from backend.llm.schemas.state_fact_pydantic import (
    STATE_FACT_ACCOUNTING_VERSION,
    STATE_FACT_EVIDENCE_VERSION,
    ChapterStateFactEvidenceSchema,
    StateFactAccountingSchema,
    StateFactManualDropReason,
    StateFactSourceBindingSchema,
    ValidatedChapterStateFactEvidenceSchema,
)


class StateFactAccountingError(ValueError):
    """Provider evidence is malformed, stale, or not bound to the supplied prose."""

    def __init__(
        self, message: str, *, reason: str = "state_fact_evidence_invalid",
        location: tuple[str | int, ...] = (),
    ):
        super().__init__(message)
        self.reason = reason
        self.location = location

    def errors(self, **_kwargs: Any) -> list[dict[str, Any]]:
        """Only machine codes and locally constructed paths enter repair guidance."""
        return [{"loc": self.location, "type": self.reason}]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_span(
    span: Mapping[str, Any], *, prose: str,
    location: tuple[str | int, ...] = (),
) -> dict[str, Any]:
    start = span.get("start")
    end = span.get("end")
    quote = span.get("quote")
    if (
        type(start) is not int
        or type(end) is not int
        or not isinstance(quote, str)
        or not quote
        or start < 0
        or end <= start
    ):
        raise StateFactAccountingError(
            "状态事实正文 span 与当前正文不匹配",
            reason="state_fact_span_bounds_invalid", location=location,
        )
    if end > len(prose) or prose[start:end] != quote:
        # Reanchor a wrong model offset only when the quotation is unique.
        # If literal matching fails, ignore CR/LF alone and map back to the
        # original prose. Spaces, punctuation and every other character stay exact.
        search_prose, search_quote = prose, quote
        source_positions = None
        located = search_prose.find(search_quote)
        if located < 0:
            search_quote = quote.replace("\r", "").replace("\n", "")
            if search_quote:
                source_positions = [index for index, char in enumerate(prose) if char not in "\r\n"]
                search_prose = "".join(prose[index] for index in source_positions)
                located = search_prose.find(search_quote)
        if located < 0:
            raise StateFactAccountingError(
                "状态事实正文 span 与当前正文不匹配",
                reason="state_fact_span_quote_not_found", location=location,
            )
        if search_prose.find(search_quote, located + 1) >= 0:
            raise StateFactAccountingError(
                "状态事实正文 span 与当前正文不匹配",
                reason="state_fact_span_quote_ambiguous", location=location,
            )
        if source_positions is None:
            start, end = located, located + len(search_quote)
        else:
            start = source_positions[located]
            end = source_positions[located + len(search_quote) - 1] + 1
    return {
        "start": start,
        "end": end,
        "quote_hash": hashlib.sha256(prose[start:end].encode("utf-8")).hexdigest(),
    }


def _canonical_fact_spans(fact: Any, *, prose: str, fact_index: int) -> list[dict[str, Any]]:
    path = ("fact_evidence", "facts", fact_index, "spans")
    spans = [
        _canonical_span(span.model_dump(mode="python"), prose=prose, location=(*path, index))
        for index, span in enumerate(fact.spans)
    ]
    if spans != sorted(spans, key=lambda item: (item["start"], item["end"])):
        raise StateFactAccountingError(
            "状态事实正文 span 顺序无效",
            reason="state_fact_span_order_invalid", location=path,
        )
    return spans


def validate_state_fact_spans(evidence: ChapterStateFactEvidenceSchema, *, prose: str) -> None:
    """Use the publication checks inside the existing, bounded repair attempt."""
    for fact_index, fact in enumerate(evidence.facts):
        _canonical_fact_spans(fact, prose=prose, fact_index=fact_index)
    if evidence.no_change is not None:
        for index, span in enumerate(evidence.no_change.spans):
            _canonical_span(
                span.model_dump(mode="python"), prose=prose,
                location=("fact_evidence", "no_change", "spans", index),
            )


class StateFactActionCoverageError(StateFactAccountingError):
    def __init__(self, issues: list[dict[str, Any]]):
        super().__init__("状态候选动作与事实证据未逐项对应", reason=issues[0]["type"], location=issues[0]["loc"])
        self._issues = issues

    def errors(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(issue) for issue in self._issues]


def _validate_planned_thread_resolution(parsed: Any, planned_thread_ids: Iterable[str]) -> None:
    """Require an explicit payoff check without inventing a state mutation.

    A supported resolved action proves the positive outcome. An unsupported or
    unknown non-action fact explicitly checks the proposed payoff without falsely
    updating it. Global unknown retains the existing manual-review boundary.
    """
    evidence = parsed.fact_evidence
    if evidence.extraction_status == "unknown":
        return
    for thread_id in dict.fromkeys(str(tid) for tid in planned_thread_ids):
        facts = [fact for fact in evidence.facts
                 if fact.target_type == "thread" and fact.target_id == thread_id
                 and fact.kind == "canonical_fact"]
        resolved_updates = [update for update in parsed.thread_updates
                            if update.thread_id == thread_id and update.status == "resolved"]
        positive = [fact for fact in facts if fact.support == "supported"
                    and fact.action_ref is not None
                    and fact.action_ref.action_type == "thread_status"
                    and fact.action_ref.value == "resolved"]
        negative = [fact for fact in facts if fact.support in {"unsupported", "unknown"}
                    and fact.action_ref is None]
        if resolved_updates:
            valid = len(resolved_updates) == 1 and len(positive) == 1 and not negative
        else:
            valid = bool(negative) and not positive
        if not valid:
            raise StateFactAccountingError(
                "计划回收的伏笔缺少正文支持的回收更新，或明确的未完成／未知判断",
                reason="state_planned_thread_unassessed",
                location=("fact_evidence", "facts"),
            )


def validate_state_provider_evidence(
    value: Any, *, prose: str, chapter_id: str, require_action_coverage: bool = False,
    planned_thread_ids: Iterable[str] = (),
) -> None:
    """Expose automatic-job action linkage errors within the existing repair."""
    import re
    from backend.llm.schemas.novel_pydantic import ChapterStateResultSchema

    parsed = ChapterStateResultSchema.model_validate(value)
    evidence = parsed.fact_evidence
    validate_state_fact_spans(evidence, prose=prose)
    _validate_planned_thread_resolution(parsed, planned_thread_ids)
    if not require_action_coverage or evidence.extraction_status == "unknown":
        return
    actions = _candidate_actions(parsed.model_dump(mode="python"), chapter_id=chapter_id)
    keys = {action["semantic_key"] for action in actions}
    covered = set()
    issues = []
    if evidence.no_change is not None:
        covered.update(action["semantic_key"] for action in actions if action["action_type"] == "chapter_summary")
    for index, fact in enumerate(evidence.facts):
        ref = fact.action_ref
        if ref is None:
            continue
        key = _action_semantic_key(action_type=ref.action_type, target_id=ref.target_id,
            value=ref.value, permanent_fact_kind=ref.permanent_fact_kind)
        reason = ("state_fact_action_reference_not_found" if key not in keys else
                  "state_fact_action_claim_duplicate" if key in covered else None)
        if reason is not None:
            issues.append({"loc": ("fact_evidence", "facts", index, "action_ref"), "type": reason})
        else:
            covered.add(key)
    for action in actions:
        if action["semantic_key"] not in covered:
            path = tuple(int(part) if part.isdigit() else part
                         for part in re.findall(r"[a-z_]+|[0-9]+", action["path"]))
            issues.append({"loc": path, "type": "state_fact_action_unaccounted"})
    if issues:
        raise StateFactActionCoverageError(issues)


def _normalize_provider_zero_offset_spans(
    container: Any, *, prose: str, location: tuple[str | int, ...],
) -> None:
    """Resolve an explicit 0/0 placeholder only from a unique source quote."""
    from backend.llm.schemas.scene_contract_pydantic import ProseEvidenceSpanSchema

    spans = container.get("spans") if isinstance(container, dict) else None
    if not isinstance(spans, list):
        return
    for index, span in enumerate(spans):
        if not isinstance(span, dict) or not (
            type(span.get("start")) is int and type(span.get("end")) is int
            and span["start"] == span["end"] == 0
        ):
            continue
        # An out-of-source probe forces unique lookup even for one-character
        # quotes at offset zero. All other span fields keep their strict schema.
        try:
            probe = ProseEvidenceSpanSchema.model_validate(
                {**span, "start": len(prose), "end": len(prose) + 1}
            )
        except ValidationError as exc:
            issues = [
                {**issue, "loc": (*location, "spans", index, *issue["loc"])}
                for issue in exc.errors(include_url=False)
            ]
            raise ValidationError.from_exception_data(exc.title, issues) from exc
        canonical = _canonical_span(
            probe.model_dump(mode="python"), prose=prose,
            location=(*location, "spans", index),
        )
        span.update(start=canonical["start"], end=canonical["end"])


def _resolve_provider_source_references(
    holder: Any, *, catalog: Mapping[str, Any], location: tuple[str | int, ...],
) -> None:
    from backend.services.novel.state_evidence_catalog import STATE_SOURCE_REFERENCE_PREFIX

    if not isinstance(holder, dict) or not isinstance(holder.get("spans"), list):
        return
    for index, span in enumerate(holder["spans"]):
        if not isinstance(span, dict):
            continue
        quote = span.get("quote")
        if not isinstance(quote, str) or not quote.startswith(STATE_SOURCE_REFERENCE_PREFIX):
            continue
        entry = catalog.get(quote)
        if (entry is None or set(span) != {"start", "end", "quote"}
                or type(span.get("start")) is not int or span["start"] != 0
                or type(span.get("end")) is not int or span["end"] != 1):
            raise StateFactAccountingError(
                "状态原文编号不属于当前正文或定位占位无效",
                reason="state_fact_source_reference_invalid",
                location=(*location, "spans", index),
            )
        span.update(entry.as_span())


def normalize_state_provider_result(
    value: Any, *, chapter_id: str, prose: str, allow_source_references: bool = False,
) -> Any:
    """Compile only lossless wire-format differences before strict validation.

    Formal schemas stay strict. Only the state extraction workflow opts in;
    raw Provider output is retained before this local transformation.
    """
    from copy import deepcopy
    from backend.llm.schemas.novel_pydantic import ChapterStateResultSchema
    from backend.llm.schemas.state_fact_pydantic import StateFactEvidenceItemSchema

    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    catalog = None
    if allow_source_references:
        from backend.services.novel.state_evidence_catalog import build_state_evidence_catalog
        catalog = {entry.reference: entry for entry in build_state_evidence_catalog(prose)}
    evidence = result.get("fact_evidence")
    facts = evidence.get("facts") if isinstance(evidence, dict) else None
    if isinstance(facts, list):
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            # Undeclared fact-envelope metadata cannot become evidence or actions.
            facts[index] = fact = {key: item for key, item in fact.items()
                                   if key in StateFactEvidenceItemSchema.model_fields}
            if fact.get("target_type") == "chapter" and fact.get("target_id") == chapter_id:
                fact["target_id"] = None
            if catalog is not None:
                _resolve_provider_source_references(
                    fact, catalog=catalog, location=("fact_evidence", "facts", index),
                )
            _normalize_provider_zero_offset_spans(
                fact, prose=prose, location=("fact_evidence", "facts", index),
            )
    if isinstance(evidence, dict):
        if catalog is not None:
            _resolve_provider_source_references(
                evidence.get("no_change"), catalog=catalog,
                location=("fact_evidence", "no_change"),
            )
        _normalize_provider_zero_offset_spans(
            evidence.get("no_change"), prose=prose, location=("fact_evidence", "no_change"),
        )
    parsed = ChapterStateResultSchema.model_validate(result)
    for index, fact in enumerate(parsed.fact_evidence.facts):
        # Every quote must independently bind to the exact prose before sorting.
        spans = [(_canonical_span(span.model_dump(mode="python"), prose=prose,
                                 location=("fact_evidence", "facts", index, "spans", number)), number)
                 for number, span in enumerate(fact.spans)]
        if spans:
            original = result["fact_evidence"]["facts"][index]["spans"]
            result["fact_evidence"]["facts"][index]["spans"] = [
                original[number] for _span, number in sorted(spans, key=lambda item: (item[0]["start"], item[0]["end"]))
            ]
    return result


def _action_semantic_key(
    *,
    action_type: str,
    target_id: str,
    value: str,
    permanent_fact_kind: str | None,
) -> tuple[str, str, str, str | None]:
    return (
        action_type,
        target_id,
        value.strip(),
        permanent_fact_kind,
    )


def _candidate_actions(
    candidate: Mapping[str, Any],
    *,
    chapter_id: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    def append(
        *,
        action_type: str,
        target_id: Any,
        value: Any,
        permanent_fact_kind: Any = None,
        selection_id: Any = None,
        path: str,
    ) -> None:
        target = str(target_id or "")
        text = str(value or "").strip()
        kind = str(permanent_fact_kind) if permanent_fact_kind else None
        if not target or not text:
            return
        ordinal = len(actions)
        identity = {
            "schema_version": "state_fact_action.v1",
            "action_type": action_type,
            "target_id": target,
            "value": text,
            "permanent_fact_kind": kind,
            "ordinal": ordinal,
        }
        actions.append(
            {
                **identity,
                "action_id": _digest(identity),
                "selection_id": (
                    str(selection_id) if isinstance(selection_id, str) else None
                ),
                "path": path,
                "semantic_key": _action_semantic_key(
                    action_type=action_type,
                    target_id=target,
                    value=text,
                    permanent_fact_kind=kind,
                ),
            }
        )

    append(
        action_type="chapter_summary",
        target_id=chapter_id,
        value=candidate.get("summary"),
        path="summary",
    )
    for character_index, character in enumerate(
        candidate.get("character_updates") or []
    ):
        if not isinstance(character, Mapping):
            continue
        append(
            action_type="character_state",
            target_id=character.get("card_id"),
            value=character.get("current_state"),
            selection_id=character.get("selection_id"),
            path=f"character_updates[{character_index}].current_state",
        )
        for fact_index, fact in enumerate(
            character.get("new_permanent_facts") or []
        ):
            if not isinstance(fact, Mapping):
                continue
            append(
                action_type="permanent_fact",
                target_id=character.get("card_id"),
                value=fact.get("fact"),
                permanent_fact_kind=fact.get("kind"),
                selection_id=fact.get("selection_id"),
                path=(
                    f"character_updates[{character_index}]"
                    f".new_permanent_facts[{fact_index}]"
                ),
            )
    for thread_index, thread in enumerate(candidate.get("thread_updates") or []):
        if not isinstance(thread, Mapping):
            continue
        append(
            action_type="thread_status",
            target_id=thread.get("thread_id"),
            value=thread.get("status"),
            selection_id=thread.get("selection_id"),
            path=f"thread_updates[{thread_index}]",
        )
    return actions


def validate_state_fact_evidence(
    evidence: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    prose: str,
    binding: Mapping[str, Any],
    known_character_ids: Iterable[str],
    known_thread_ids: Iterable[str],
) -> dict[str, Any]:
    """Turn Provider quotes into a locally bound, hash-only evidence sidecar."""

    try:
        parsed = ChapterStateFactEvidenceSchema.model_validate(evidence)
        source_binding = StateFactSourceBindingSchema.model_validate(binding)
    except ValidationError as exc:
        raise StateFactAccountingError("状态事实证据结构无效") from exc
    if parsed.schema_version != STATE_FACT_EVIDENCE_VERSION:
        raise StateFactAccountingError("状态事实证据版本未知")
    if source_binding.source_content_digest != hashlib.sha256(
        prose.encode("utf-8")
    ).hexdigest():
        raise StateFactAccountingError("状态事实证据正文摘要不匹配")

    actions = _candidate_actions(
        candidate,
        chapter_id=source_binding.chapter_id,
    )
    actions_by_key: dict[tuple[str, str, str, str | None], list[dict[str, Any]]] = {}
    for action in actions:
        actions_by_key.setdefault(action["semantic_key"], []).append(action)

    characters = {str(value) for value in known_character_ids}
    threads = {str(value) for value in known_thread_ids}
    invalid_refs: set[tuple[str, str]] = set()
    matched_action_ids: set[str] = set()
    action_fact_owners: dict[str, str] = {}
    dangling = 0
    canonical_facts: list[dict[str, Any]] = []
    for fact_index, fact in enumerate(parsed.facts):
        target_id = (
            source_binding.chapter_id
            if fact.target_type == "chapter"
            else str(fact.target_id or "")
        )
        if (
            fact.target_type == "character"
            and target_id not in characters
        ) or (
            fact.target_type == "thread"
            and target_id not in threads
        ):
            invalid_refs.add((fact.target_type, target_id))

        action_ids: list[str] = []
        if fact.action_ref is not None:
            ref = fact.action_ref
            key = _action_semantic_key(
                action_type=ref.action_type,
                target_id=ref.target_id,
                value=ref.value,
                permanent_fact_kind=ref.permanent_fact_kind,
            )
            matches = actions_by_key.get(key, [])
            if not matches:
                dangling += 1
            else:
                action_ids = [str(action["action_id"]) for action in matches]
                conflicting = {
                    action_fact_owners[action_id]
                    for action_id in action_ids
                    if action_id in action_fact_owners
                }
                if conflicting:
                    raise StateFactAccountingError(
                        "同一状态动作被多条事实证据重复认领"
                    )
                for action_id in action_ids:
                    action_fact_owners[action_id] = fact.fact_id
                matched_action_ids.update(action_ids)
        spans = _canonical_fact_spans(fact, prose=prose, fact_index=fact_index)
        fact_signature = _digest(
            {
                "schema_version": "state_fact_signature.v1",
                "source_binding": source_binding.model_dump(mode="json"),
                "kind": fact.kind,
                "support": fact.support,
                "statement": fact.statement.strip(),
                "target_type": fact.target_type,
                "target_id": target_id,
                "spans": spans,
            }
        )
        canonical_facts.append(
            {
                "fact_id": fact.fact_id,
                "fact_signature": fact_signature,
                "kind": fact.kind,
                "support": fact.support,
                "statement_digest": hashlib.sha256(
                    fact.statement.strip().encode("utf-8")
                ).hexdigest(),
                "target_type": fact.target_type,
                "target_id": target_id,
                "action_ids": tuple(action_ids),
                "spans": tuple(spans),
                "explanation": fact.explanation,
            }
        )

    no_change = None
    if parsed.no_change is not None:
        no_change_action_ids = tuple(
            str(action["action_id"])
            for action in actions
            if action["action_type"] == "chapter_summary"
        )
        matched_action_ids.update(no_change_action_ids)
        no_change = {
            "action_ids": no_change_action_ids,
            "spans": tuple(
                _canonical_span(
                    span.model_dump(mode="python"), prose=prose,
                    location=("fact_evidence", "no_change", "spans", index),
                )
                for index, span in enumerate(parsed.no_change.spans)
            ),
            "explanation": parsed.no_change.explanation,
        }
    # A Provider proposal without any fact/no-op evidence is an unexplained
    # action, not a legal empty result. Empty values never become actions.
    dangling += len(
        {str(action["action_id"]) for action in actions}
        - matched_action_ids
    )
    raw = {
        "evidence_schema_version": STATE_FACT_EVIDENCE_VERSION,
        "extraction_status": parsed.extraction_status,
        "facts": tuple(canonical_facts),
        "no_change": no_change,
        "unknown_reason": parsed.unknown_reason,
        "source_binding": source_binding.model_dump(mode="json"),
        "invalid_internal_references": len(invalid_refs),
        "dangling_references": dangling,
    }
    raw["evidence_digest"] = _digest(raw)
    try:
        validated = ValidatedChapterStateFactEvidenceSchema.model_validate(raw)
    except ValidationError as exc:
        raise StateFactAccountingError("本地状态事实证据投影无效") from exc
    return validated.model_dump(mode="json")


_NON_CANONICAL_DROP_REASONS = {
    "character_cognition": "character_cognition",
    "rumor": "rumor",
    "deception": "deception",
    "temporary_fact": "temporary_fact",
}
_ACCEPT_REASONS = {
    "chapter_summary": "accepted_chapter_summary",
    "character_state": "accepted_character_state",
    "permanent_fact": "accepted_permanent_fact",
    "thread_status": "accepted_thread_update",
}


def _validated_fact_evidence(
    evidence: Mapping[str, Any],
    *,
    error_message: str,
) -> ValidatedChapterStateFactEvidenceSchema:
    """Normalize persisted JSON containers at the single validation seam."""

    normalized = dict(evidence)
    raw_facts = normalized.get("facts")
    if isinstance(raw_facts, list):
        normalized_facts = []
        for raw_fact in raw_facts:
            fact = dict(raw_fact) if isinstance(raw_fact, Mapping) else raw_fact
            if isinstance(fact, dict):
                if isinstance(fact.get("action_ids"), list):
                    fact["action_ids"] = tuple(fact["action_ids"])
                if isinstance(fact.get("spans"), list):
                    fact["spans"] = tuple(fact["spans"])
            normalized_facts.append(fact)
        normalized["facts"] = tuple(normalized_facts)
    raw_no_change = normalized.get("no_change")
    if isinstance(raw_no_change, Mapping):
        no_change = dict(raw_no_change)
        if isinstance(no_change.get("action_ids"), list):
            no_change["action_ids"] = tuple(no_change["action_ids"])
        if isinstance(no_change.get("spans"), list):
            no_change["spans"] = tuple(no_change["spans"])
        normalized["no_change"] = no_change
    try:
        return ValidatedChapterStateFactEvidenceSchema.model_validate(
            normalized
        )
    except ValidationError as exc:
        raise StateFactAccountingError(error_message) from exc


def _automatic_drop_reason(fact: Any) -> str | None:
    if fact.support == "unsupported":
        return "unsupported_by_prose"
    return _NON_CANONICAL_DROP_REASONS.get(fact.kind)


def state_selection_policy(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only UI projection; acceptance still recomputes the full accounting."""
    evidence = candidate.get("fact_evidence")
    if not isinstance(evidence, Mapping):
        return {}
    validated = _validated_fact_evidence(evidence, error_message="状态选择证据无效")
    actions = _candidate_actions(candidate, chapter_id=validated.source_binding.chapter_id)
    facts_by_action: dict[str, list[Any]] = {}
    for fact in validated.facts:
        for action_id in fact.action_ids:
            facts_by_action.setdefault(action_id, []).append(fact)
    policy: dict[str, Any] = {}
    # Empty values have selection IDs but are deliberately not narrative actions.
    items = [*candidate.get("character_updates", []), *candidate.get("thread_updates", [])]
    for character in candidate.get("character_updates", []):
        items.extend(character.get("new_permanent_facts", []))
    for item in items:
        if item.get("selection_id"):
            policy[item["selection_id"]] = {
                "eligible": False, "reason": "legal_no_op", "requires_drop_reason": False,
            }
    for action in actions:
        selection_id = action.get("selection_id")
        if not selection_id:
            continue
        facts = facts_by_action.get(action["action_id"], [])
        eligible = bool(facts) and all(
            fact.kind == "canonical_fact" and fact.support == "supported" for fact in facts
        )
        blockers = [fact for fact in facts if fact.kind != "canonical_fact" or fact.support != "supported"]
        reason = "canonical_fact" if eligible else (
            next((_automatic_drop_reason(fact) or "unknown" for fact in blockers), "unknown")
        )
        if any(fact.support == "unknown" and fact.kind == "canonical_fact" for fact in facts):
            reason = "unknown"
        policy[selection_id] = {
            "eligible": eligible,
            "reason": reason,
            "requires_drop_reason": not facts or any(_automatic_drop_reason(fact) is None for fact in facts),
        }
    return policy


def account_state_fact_evidence(
    evidence: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    selected_character_ids: Iterable[str],
    selected_fact_ids: Iterable[str],
    selected_thread_ids: Iterable[str],
    drop_reasons: Mapping[str, StateFactManualDropReason] | None = None,
) -> dict[str, Any]:
    """Apply one exact selection decision to already validated evidence."""

    validated = _validated_fact_evidence(
        evidence,
        error_message="状态事实核算输入证据无效",
    )
    actions = _candidate_actions(
        candidate,
        chapter_id=validated.source_binding.chapter_id,
    )
    actions_by_id = {str(action["action_id"]): action for action in actions}
    claimed_action_ids = {
        str(action_id)
        for fact in validated.facts
        for action_id in fact.action_ids
    }
    if validated.no_change is not None:
        claimed_action_ids.update(validated.no_change.action_ids)
    recomputed_dangling = len(
        set(actions_by_id) - claimed_action_ids
    ) + len(claimed_action_ids - set(actions_by_id))
    dangling_references = max(
        validated.dangling_references,
        recomputed_dangling,
    )
    selected_by_type = {
        "character_state": {str(value) for value in selected_character_ids},
        "permanent_fact": {str(value) for value in selected_fact_ids},
        "thread_status": {str(value) for value in selected_thread_ids},
    }
    selected_action_ids = {
        action_id
        for action_id, action in actions_by_id.items()
        if str(action["action_type"]) in selected_by_type
        and action.get("selection_id")
        in selected_by_type[str(action["action_type"])]
    }
    selected_action_ids.update(
        action_id
        for fact in validated.facts
        if fact.kind == "canonical_fact" and fact.support == "supported"
        for action_id in fact.action_ids
        if action_id in actions_by_id
        and actions_by_id[action_id]["action_type"] == "chapter_summary"
    )
    raw_drop_reasons = {
        str(key): str(value) for key, value in dict(drop_reasons or {}).items()
    }
    selection_to_action = {
        str(action["selection_id"]): action_id
        for action_id, action in actions_by_id.items()
        if action.get("selection_id")
    }
    supplied_drop_reasons = {
        selection_to_action.get(key, key): value
        for key, value in raw_drop_reasons.items()
    }
    allowed_drop_reasons = {
        "duplicate_existing_fact",
        "duplicate_proposal",
        "legal_no_op",
        "unsupported_by_prose",
    }
    if any(value not in allowed_drop_reasons for value in supplied_drop_reasons.values()):
        raise StateFactAccountingError("状态事实丢弃原因码无效")
    unknown_drop_targets = set(supplied_drop_reasons) - set(actions_by_id)
    if unknown_drop_targets:
        raise StateFactAccountingError("状态事实丢弃原因引用了未知动作")

    accounts: list[dict[str, Any]] = []
    action_accounts: list[dict[str, Any]] = []
    canonical_count = 0
    accounted_canonical = 0
    accepted_actions: set[str] = set()
    dropped_actions: set[str] = set()
    for fact in validated.facts:
        fact_spans = tuple(
            span.model_dump(mode="json") for span in fact.spans
        )
        fact_action_ids = tuple(
            action_id
            for action_id in fact.action_ids
            if action_id in actions_by_id
        )
        selected = tuple(
            action_id
            for action_id in fact_action_ids
            if action_id in selected_action_ids
        )
        reason: str
        is_canonical = fact.kind == "canonical_fact" and fact.support != "unsupported"
        if is_canonical:
            canonical_count += 1
        if selected and (
            fact.support != "supported" or fact.kind != "canonical_fact"
        ):
            raise StateFactAccountingError(
                "非正式或正文不支持的状态动作不得写入正式状态"
            )

        fact_action_reasons: list[str] = []
        for action_id in fact_action_ids:
            action = actions_by_id[action_id]
            if action_id in selected_action_ids:
                action_reason = _ACCEPT_REASONS[str(action["action_type"])]
                decision = "accepted"
                accepted_actions.add(action_id)
            else:
                automatic_reason = _automatic_drop_reason(fact)
                action_reason = (
                    automatic_reason
                    or supplied_drop_reasons.get(action_id)
                    or "unaccounted"
                )
                decision = "dropped"
                if action_reason != "unaccounted":
                    dropped_actions.add(action_id)
            fact_action_reasons.append(action_reason)
            action_accounts.append(
                {
                    "action_id": action_id,
                    "fact_id": fact.fact_id,
                    "fact_signature": fact.fact_signature,
                    "action_type": action["action_type"],
                    "target_id": action["target_id"],
                    "decision": decision,
                    "reason_code": action_reason,
                    "spans": fact_spans,
                }
            )

        if fact.support == "unsupported":
            reason = "unsupported_by_prose"
        elif fact.kind in _NON_CANONICAL_DROP_REASONS:
            reason = _NON_CANONICAL_DROP_REASONS[fact.kind]
        elif fact.support == "unknown":
            reason = "unaccounted"
        elif not fact_action_ids:
            reason = "unaccounted"
        elif "unaccounted" in fact_action_reasons:
            reason = "unaccounted"
        elif selected:
            selected_action = actions_by_id[selected[0]]
            reason = _ACCEPT_REASONS[str(selected_action["action_type"])]
        else:
            reason = fact_action_reasons[0]

        if is_canonical and reason != "unaccounted":
            accounted_canonical += 1
        accounts.append(
            {
                "fact_id": fact.fact_id,
                "fact_signature": fact.fact_signature,
                "kind": fact.kind,
                "support": fact.support,
                "target_type": fact.target_type,
                "target_id": fact.target_id,
                "action_ids": fact_action_ids,
                "selected_action_ids": selected,
                "reason_code": reason,
                "spans": fact_spans,
            }
        )

    if validated.no_change is not None:
        no_change_spans = tuple(
            span.model_dump(mode="json")
            for span in validated.no_change.spans
        )
        for action_id in validated.no_change.action_ids:
            action = actions_by_id.get(action_id)
            if action is None or action["action_type"] != "chapter_summary":
                raise StateFactAccountingError(
                    "合法 no-op 引用了未知状态动作"
                )
            signature = _digest({
                "schema_version": "state_no_change_signature.v1",
                "evidence_digest": validated.evidence_digest,
                "action_id": action_id,
            })
            action_accounts.append({
                "action_id": action_id,
                "fact_id": "no-change",
                "fact_signature": signature,
                "action_type": "chapter_summary",
                "target_id": action["target_id"],
                "decision": "dropped",
                "reason_code": "legal_no_op",
                "spans": no_change_spans,
            })
            dropped_actions.add(action_id)

    extraction_failure_count = int(validated.extraction_status == "unknown")
    no_change_account = (
        {
            "reason_code": "legal_no_op",
            "action_ids": validated.no_change.action_ids,
            "spans": tuple(
                span.model_dump(mode="json")
                for span in validated.no_change.spans
            ),
        }
        if validated.no_change is not None
        else None
    )
    unaccounted = (
        canonical_count - accounted_canonical + extraction_failure_count
    )
    gate_passed = (
        unaccounted == 0
        and validated.invalid_internal_references == 0
        and dangling_references == 0
    )
    raw = {
        "schema_version": STATE_FACT_ACCOUNTING_VERSION,
        "evidence_digest": validated.evidence_digest,
        "source_binding": validated.source_binding.model_dump(mode="json"),
        "extraction_status": validated.extraction_status,
        "accounts": tuple(accounts),
        "action_accounts": tuple(action_accounts),
        "no_change_account": no_change_account,
        "canonical_fact_count": canonical_count,
        "accounted_canonical_fact_count": accounted_canonical,
        "unaccounted_canonical_facts": unaccounted,
        "invalid_internal_references": validated.invalid_internal_references,
        "dangling_references": dangling_references,
        "extraction_failure_count": extraction_failure_count,
        "accepted_action_count": len(accepted_actions),
        "dropped_action_count": len(dropped_actions),
        "gate_passed": gate_passed,
    }
    raw["accounting_digest"] = _digest(raw)
    try:
        accounting = StateFactAccountingSchema.model_validate(raw)
    except ValidationError as exc:
        raise StateFactAccountingError("状态事实核算投影无效") from exc
    return accounting.model_dump(mode="json")


def validate_state_fact_accounting(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Revalidate a persisted JSON projection, including its stable digest."""

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        accounting = StateFactAccountingSchema.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError) as exc:
        raise StateFactAccountingError("状态事实核算投影无效") from exc
    return accounting.model_dump(mode="json")


def automatic_state_fact_decision(
    evidence: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Select only prose-supported canonical actions and type every legal drop."""

    validated = _validated_fact_evidence(
        evidence,
        error_message="自动状态事实决策证据无效",
    )

    actions = _candidate_actions(
        candidate,
        chapter_id=validated.source_binding.chapter_id,
    )
    actions_by_id = {str(action["action_id"]): action for action in actions}
    selected_action_ids: set[str] = set()
    drop_reasons: dict[str, str] = {}
    for fact in validated.facts:
        action_ids = [
            action_id
            for action_id in fact.action_ids
            if action_id in actions_by_id
        ]
        if fact.kind == "canonical_fact" and fact.support == "supported":
            if action_ids:
                selected_action_ids.add(action_ids[0])
                for duplicate_id in action_ids[1:]:
                    drop_reasons[duplicate_id] = "duplicate_proposal"
            continue
        # Unsupported and non-world actions are typed from the validated fact
        # itself inside account_state_fact_evidence. Keeping those reasons out
        # of this caller-supplied map prevents a human from relabelling a
        # supported canonical fact as rumor/cognition to bypass the gate.

    selected_by_type: dict[str, list[str]] = {
        "character_state": [],
        "permanent_fact": [],
        "thread_status": [],
    }
    for action_id in sorted(selected_action_ids):
        action = actions_by_id[action_id]
        if action["action_type"] == "chapter_summary":
            continue
        selection_id = action.get("selection_id")
        if isinstance(selection_id, str) and selection_id:
            selected_by_type[str(action["action_type"])].append(selection_id)
    accounting = account_state_fact_evidence(
        validated.model_dump(mode="python"),
        candidate=candidate,
        selected_character_ids=selected_by_type["character_state"],
        selected_fact_ids=selected_by_type["permanent_fact"],
        selected_thread_ids=selected_by_type["thread_status"],
        drop_reasons=drop_reasons,
    )
    return {
        "selected_character_ids": tuple(selected_by_type["character_state"]),
        "selected_fact_ids": tuple(selected_by_type["permanent_fact"]),
        "selected_thread_ids": tuple(selected_by_type["thread_status"]),
        "drop_reasons": drop_reasons,
        "fact_accounting": accounting,
    }
