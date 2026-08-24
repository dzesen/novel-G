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


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_span(span: Mapping[str, Any], *, prose: str) -> dict[str, Any]:
    start = span.get("start")
    end = span.get("end")
    quote = span.get("quote")
    if (
        type(start) is not int
        or type(end) is not int
        or not isinstance(quote, str)
        or start < 0
        or end <= start
        or end > len(prose)
        or prose[start:end] != quote
    ):
        raise StateFactAccountingError("状态事实正文 span 与当前正文不匹配")
    return {
        "start": start,
        "end": end,
        "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
    }


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
    for fact in parsed.facts:
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
        spans = [
            _canonical_span(span.model_dump(mode="python"), prose=prose)
            for span in fact.spans
        ]
        if spans != sorted(spans, key=lambda item: (item["start"], item["end"])):
            raise StateFactAccountingError("状态事实正文 span 顺序无效")
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
                _canonical_span(span.model_dump(mode="python"), prose=prose)
                for span in parsed.no_change.spans
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
                automatic_reason = None
                if fact.support == "unsupported":
                    automatic_reason = "unsupported_by_prose"
                elif fact.kind in _NON_CANONICAL_DROP_REASONS:
                    automatic_reason = _NON_CANONICAL_DROP_REASONS[fact.kind]
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
