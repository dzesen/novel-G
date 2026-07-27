"""批量生成步骤结果与持久化提醒的稳定契约。

调用方只需要提交某一步发生的截断或引用清理；本 Module 负责有界化详情并产出
前后端都能长期读取的机器码。旧的 ``steps_*`` / ``dropped_ids`` / ``truncations``
仍由管线保留，迁移期不要求历史作业重写。
"""
from __future__ import annotations

from typing import Any, Iterable

_MAX_FIELDS = 32
_MAX_VALUES_PER_FIELD = 50
_MAX_VALUE_LENGTH = 200


def step_outcome(step: str, status: str, reason_code: str | None = None) -> dict[str, Any]:
    return {"step": step, "status": status, "reason_code": reason_code}


def _bounded_values(value: Any) -> list[str]:
    values: Iterable[Any]
    if isinstance(value, (list, tuple, set)):
        values = value
    elif value is None:
        values = ()
    else:
        values = (value,)
    return [
        str(item)[:_MAX_VALUE_LENGTH]
        for item in list(values)[:_MAX_VALUES_PER_FIELD]
        if str(item).strip()
    ]


def reference_drop_notice(step: str, dropped: dict[str, Any]) -> dict[str, Any] | None:
    fields = []
    for field_name in sorted(dropped)[:_MAX_FIELDS]:
        values = _bounded_values(dropped[field_name])
        if values:
            fields.append({"field": str(field_name), "values": values})
    if not fields:
        return None
    return {
        "code": "reference_ids_dropped",
        "severity": "warning",
        "category": "reference",
        "step": step,
        "details": {"fields": fields},
        "impact": "references_not_applied",
        "action_codes": ["review_reference_cards"],
        "requires_pause": False,
    }


def reference_remap_notice(
    step: str,
    remapped: list[dict[str, Any]],
) -> dict[str, Any] | None:
    mappings = []
    for item in remapped[:_MAX_VALUES_PER_FIELD]:
        source = str(item.get("from") or "")[:_MAX_VALUE_LENGTH]
        target = str(item.get("to") or "")[:_MAX_VALUE_LENGTH]
        if not source or not target:
            continue
        mappings.append(
            {
                "field": str(item.get("field") or "")[:_MAX_VALUE_LENGTH],
                "from": source,
                "to": target,
                "matched_by": str(item.get("matched_by") or "")[
                    :_MAX_VALUE_LENGTH
                ],
            }
        )
    if not mappings:
        return None
    return {
        "code": "reference_ids_remapped",
        "severity": "info",
        "category": "reference_remap",
        "step": step,
        "details": {"mappings": mappings},
        "impact": "references_mapped_to_formal_ids",
        "action_codes": [],
        "requires_pause": False,
    }


def context_truncation_notice(step: str, truncation: dict[str, Any]) -> dict[str, Any] | None:
    sections = [
        str(item)[:_MAX_VALUE_LENGTH]
        for item in list(truncation.get("truncated_sections") or [])[:_MAX_VALUES_PER_FIELD]
    ]
    raw_counts = truncation.get("dropped_item_counts") or {}
    counts = {
        str(key)[:_MAX_VALUE_LENGTH]: max(0, int(value))
        for key, value in list(raw_counts.items())[:_MAX_FIELDS]
    }
    if not sections and not counts:
        return None
    return {
        "code": "context_truncated",
        "severity": "warning",
        "category": "context",
        "step": step,
        "details": {
            "truncated_sections": sections,
            "dropped_item_counts": counts,
        },
        "impact": "generated_with_reduced_context",
        "action_codes": ["review_context"],
        "requires_pause": False,
    }


def prose_incomplete_notice(completion: dict[str, Any]) -> dict[str, Any]:
    """Describe an incomplete prose draft without persisting its text."""
    return {
        "code": "prose_incomplete",
        "severity": "error",
        "category": "completion",
        "step": "prose",
        "details": {
            "requested_word_count": int(
                completion.get("requested_word_count") or 0
            ),
            "actual_word_count": int(completion.get("actual_word_count") or 0),
            "scene_count": int(completion.get("scene_count") or 0),
            "completed_scene_count": int(
                completion.get("completed_scene_count") or 0
            ),
            "finish_reason": str(
                completion.get("finish_reason") or "unreported"
            )[:_MAX_VALUE_LENGTH],
            "reason_codes": _bounded_values(completion.get("reason_codes") or []),
        },
        "impact": "formal_prose_not_written_state_not_run",
        "action_codes": ["resume_prose_run", "review_partial_prose"],
        "requires_pause": True,
    }


def partial_prose_blocks_state_notice() -> dict[str, Any]:
    return {
        "code": "partial_prose_blocks_state",
        "severity": "error",
        "category": "completion",
        "step": "state",
        "details": {"prose_acceptance_state": "partial_manual_required"},
        "impact": "state_backfill_not_run",
        "action_codes": ["complete_prose_manually", "mark_chapter_completed"],
        "requires_pause": True,
    }


def state_all_character_updates_dropped_notice(
    dropped: dict[str, Any],
) -> dict[str, Any]:
    return {
        "code": "state_all_character_updates_dropped",
        "severity": "error",
        "category": "reference",
        "step": "state",
        "details": {
            "dropped_character_ids": _bounded_values(
                dropped.get("character_updates") or []
            )
        },
        "impact": "character_state_not_written",
        "action_codes": [
            "review_reference_cards",
            "rerun_chapter_state_backfill",
        ],
        "requires_pause": True,
    }
