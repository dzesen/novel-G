"""v3 per-scene prose execution with bounded automatic continuations.

The legacy executor in :mod:`prose_generation` treats a planned segment as the
unit of recovery.  v3 intentionally makes the *scene* the recovery unit: long
scenes may still have several baseline parts, but their extra-call allowance is
shared and persisted once for the whole scene.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping

from backend.llm.models import TokenUsage
from backend.db.repositories.generation_job_repository import TokenBudgetExceeded
from backend.llm.stream_terminal import normalize_finish_reason
from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.prose_continuation import ProseContinuationPolicy
from backend.services.generation.prose_protocol import (
    scene_continuation_seam_window_characters,
)
from backend.services.generation.prose_token_bounds import v3_output_token_bound
from backend.services.generation.prose_generation import (
    DeltaCallback,
    ProseGenerationResult,
    StreamCall,
    UncertainProseAttempt,
    _CallSpec,
    _call_specs,
    _notify,
    _usage_sum,
    prose_completion_module,
)
from backend.services.novel.chapter_service import count_chapter_words


SceneProgressCallback = Callable[[tuple[dict[str, Any], ...]], Awaitable[None] | None]

_AUTOMATIC_SEQUENCE_FLOOR = 1_000_000
# Healthy observed repeats were 10–23 characters, while pathological replays
# were 230–2,045.  100 is deliberately in that measured gap.
MIN_REPLAY_CHARACTERS = 100
_TRUNCATED_OUTPUT_CONTINUATION_MODES = frozenset(
    {"fill", "converge", "final_converge"}
)
CONTINUATION_PROMPT_MODES = frozenset(
    {
        "base",
        "fill",
        "converge",
        "final_converge",
        "recovery",
        "final_recovery",
        "manual",
        "uncertain_retry",
    }
)
# The written-prose window is already included verbatim in the continuation
# prompt. Keep the separately-labelled anchor small so it cannot duplicate a
# second full window and invalidate existing readiness bounds.
MAX_CONTINUATION_ANCHOR_CHARACTERS = 160

TRUNCATED_OUTPUT_CONTINUATION_INSTRUCTION = (
    " 上一次输出因输出上限被硬截断，可能停在句子中途；"
    "本次必须先把被截断的句子写完，再继续，不得另起新的一拍。"
)


@dataclass(frozen=True)
class _SceneReplayMeasurement:
    raw_word_count: int
    effective_word_count: int
    replayed_characters_total: int


def _scene_target_words(plan: ProseExecutionPlan, scene_index: int) -> int:
    budgets = plan.segment_budgets or (plan.requested_word_count,)
    budget = budgets[min(max(0, scene_index), len(budgets) - 1)]
    return max(1, int(budget))


def _scene_minimum_words(plan: ProseExecutionPlan, scene_index: int) -> int:
    return math.ceil(
        _scene_target_words(plan, scene_index) * plan.minimum_completion_ratio
    )


def _call_kind(segment: Mapping[str, Any], *, base_count: int) -> str:
    explicit = str(segment.get("call_kind") or "").strip()
    if explicit:
        return explicit
    try:
        sequence = int(segment.get("sequence_index") or 0)
    except (TypeError, ValueError):
        sequence = -1
    return "base" if 0 <= sequence < base_count else "automatic"


def _segment_sort_key(segment: Mapping[str, Any]) -> tuple[int, int, int]:
    """Keep v3 prose in scene/call order even when continuation ids are sparse."""
    return (
        int(segment.get("scene_index") or 0),
        int(
            segment.get(
                "scene_call_index",
                segment.get("part_index") or 0,
            )
            or 0
        ),
        int(segment.get("sequence_index") or 0),
    )


def prose_segment_order_key(segment: Mapping[str, Any]) -> tuple[int, int, int]:
    """Public ordering key also used when reconstructing persisted v3 drafts."""
    return _segment_sort_key(segment)


def _ordered_scene_segments(
    segments: Iterable[Mapping[str, Any]],
    *,
    scene_index: int,
) -> list[dict[str, Any]]:
    return sorted(
        (
            dict(segment)
            for segment in segments
            if int(segment.get("scene_index") or 0) == scene_index
        ),
        key=_segment_sort_key,
    )


def _scene_text(segments: Iterable[Mapping[str, Any]], *, scene_index: int) -> str:
    return "\n\n".join(
        str(segment.get("text") or "").strip()
        for segment in _ordered_scene_segments(segments, scene_index=scene_index)
        if str(segment.get("text") or "").strip()
    )


def _deduplicate_exact_seam(existing_text: str, generated_text: str) -> str:
    """Remove only an exact suffix/prefix overlap; never fuzzy-deduplicate prose."""
    existing = str(existing_text or "")
    generated = str(generated_text or "")
    largest = min(len(existing), len(generated))
    for size in range(largest, 0, -1):
        if existing[-size:] == generated[:size]:
            return generated[size:]
    return generated


def _ending_exact_common_substring_lengths(
    earlier_text: str,
    later_text: str,
) -> list[int]:
    """Return the exact-match length ending at every later-text character."""
    earlier = str(earlier_text or "")
    later = str(later_text or "")
    if not earlier or not later:
        return [0] * len(later)

    links = [-1]
    lengths = [0]
    transitions: list[dict[str, int]] = [{}]
    last_state = 0
    for character in earlier:
        current_state = len(lengths)
        lengths.append(lengths[last_state] + 1)
        links.append(0)
        transitions.append({})
        probe = last_state
        while probe != -1 and character not in transitions[probe]:
            transitions[probe][character] = current_state
            probe = links[probe]
        if probe == -1:
            links[current_state] = 0
        else:
            next_state = transitions[probe][character]
            if lengths[probe] + 1 == lengths[next_state]:
                links[current_state] = next_state
            else:
                clone_state = len(lengths)
                lengths.append(lengths[probe] + 1)
                links.append(links[next_state])
                transitions.append(dict(transitions[next_state]))
                while (
                    probe != -1
                    and transitions[probe].get(character) == next_state
                ):
                    transitions[probe][character] = clone_state
                    probe = links[probe]
                links[next_state] = clone_state
                links[current_state] = clone_state
        last_state = current_state

    state = 0
    matched = 0
    ending_lengths: list[int] = []
    for character in later:
        while state and character not in transitions[state]:
            state = links[state]
            matched = lengths[state]
        next_state = transitions[state].get(character)
        if next_state is None:
            state = 0
            matched = 0
            ending_lengths.append(0)
            continue
        state = next_state
        matched += 1
        ending_lengths.append(matched)
    return ending_lengths


def _longest_exact_common_substring_characters(
    earlier_text: str,
    later_text: str,
) -> int:
    """Measure an exact cross-call repeat in O(len(earlier) + len(later))."""
    return max(
        _ending_exact_common_substring_lengths(earlier_text, later_text),
        default=0,
    )


def _merged_character_ranges(
    ranges: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Merge overlapping half-open character ranges deterministically."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _replayed_character_ranges(
    earlier_text: str,
    later_text: str,
) -> tuple[tuple[int, int], ...]:
    """Locate only later-text ranges that exactly replay earlier scene prose."""
    return _merged_character_ranges(
        (index - length + 1, index + 1)
        for index, length in enumerate(
            _ending_exact_common_substring_lengths(earlier_text, later_text)
        )
        if length >= MIN_REPLAY_CHARACTERS
    )


def _exclude_character_ranges(
    text: str,
    ranges: Iterable[tuple[int, int]],
) -> str:
    """Keep all prose except the exact replay positions used for counting."""
    cursor = 0
    kept: list[str] = []
    for start, end in _merged_character_ranges(ranges):
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    return "".join(kept)


def _scene_replay_measurement(
    scene_segments: Iterable[Mapping[str, Any]],
) -> _SceneReplayMeasurement:
    """Measure deterministic replay coverage without modifying stored prose."""
    raw_parts: list[str] = []
    effective_parts: list[str] = []
    replayed_characters_total = 0
    earlier_text = ""
    for segment in scene_segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        replayed_ranges = _replayed_character_ranges(earlier_text, text)
        replayed_characters_total += sum(
            end - start for start, end in replayed_ranges
        )
        raw_parts.append(text)
        effective_parts.append(_exclude_character_ranges(text, replayed_ranges))
        earlier_text = "\n\n".join(raw_parts)

    raw_word_count = count_chapter_words("\n\n".join(raw_parts))
    effective_word_count = count_chapter_words("\n\n".join(effective_parts))
    # The input text is only ever removed from the effective representation.
    # Clamp defensively so a future tokenizer change cannot weaken the gate.
    return _SceneReplayMeasurement(
        raw_word_count=raw_word_count,
        effective_word_count=max(0, min(raw_word_count, effective_word_count)),
        replayed_characters_total=replayed_characters_total,
    )


def _continuation_anchor(prior_text: str) -> tuple[str, bool]:
    """Return a bounded last-sentence anchor and whether it was shortened."""
    text = str(prior_text or "").strip()
    if not text:
        return "（无）", False
    boundaries = [
        index for index, character in enumerate(text) if character in "。！？!?…"
    ]
    sentence_start = (
        (boundaries[-2] + 1)
        if boundaries and boundaries[-1] == len(text) - 1 and len(boundaries) >= 2
        else (boundaries[-1] + 1 if boundaries else 0)
    )
    anchor = text[sentence_start:].strip() or text
    return anchor[-MAX_CONTINUATION_ANCHOR_CHARACTERS:], (
        len(anchor) > MAX_CONTINUATION_ANCHOR_CHARACTERS
    )


def _normal_finish_reason(value: Any, raw_value: Any = None) -> tuple[str, str]:
    normalized = normalize_finish_reason(value)
    raw_source = value if raw_value is None else raw_value
    raw = str(getattr(raw_source, "value", raw_source) or "unreported").strip()
    return normalized, raw or "unreported"


def _provider_failure_reason(finish_reason: str) -> str | None:
    if finish_reason in {"content_filter", "tool_call", "cancelled", "error"}:
        return f"finish_reason_{finish_reason}"
    if finish_reason == "unreported":
        return "finish_reason_unreported"
    return None


def _is_safe_pre_dispatch_budget_refusal(segment: Mapping[str, Any]) -> bool:
    """Only retry the terminal form produced before Provider dispatch."""
    try:
        word_count = int(segment.get("word_count") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        str(segment.get("error_code") or "")
        == "token_budget_exceeded_before_dispatch"
        and str(segment.get("status") or "") == "incomplete"
        and str(segment.get("finish_reason") or "") == "budget"
        and str(segment.get("raw_finish_reason") or "") == "budget"
        and not str(segment.get("text") or "").strip()
        and word_count == 0
    )


def _is_scene_complete(
    *,
    scene_text: str,
    effective_word_count: int,
    finish_reason: str,
    plan: ProseExecutionPlan,
    scene_index: int,
) -> bool:
    # A length terminal is deliberately never accepted as a scene boundary, even
    # when it happened to reach the target length.
    return bool(
        finish_reason == "stop"
        and scene_text.strip()
        and max(0, int(effective_word_count))
        >= _scene_minimum_words(plan, scene_index)
    )


def _next_automatic_sequence(segments: Iterable[Mapping[str, Any]]) -> int:
    used = {
        int(segment.get("sequence_index") or 0)
        for segment in segments
        if segment.get("sequence_index") is not None
    }
    candidate = max(
        _AUTOMATIC_SEQUENCE_FLOOR,
        max((value for value in used if value >= _AUTOMATIC_SEQUENCE_FLOOR), default=0)
        + 1,
    )
    while candidate in used:
        candidate += 1
    return candidate


def _next_scene_call_index(
    segments: Iterable[Mapping[str, Any]],
    *,
    scene_index: int,
) -> int:
    scene_segments = _ordered_scene_segments(segments, scene_index=scene_index)
    if not scene_segments:
        return 0
    return max(
        int(
            segment.get(
                "scene_call_index",
                segment.get("part_index") or 0,
            )
            or 0
        )
        for segment in scene_segments
    ) + 1


def _scene_progress_snapshot(
    *,
    scene_index: int,
    plan: ProseExecutionPlan,
    segments: Iterable[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scene_segments = _ordered_scene_segments(segments, scene_index=scene_index)
    state = dict(previous or {})
    state["scene_index"] = scene_index
    state["base_calls_used"] = sum(
        1 for segment in scene_segments if _call_kind(segment, base_count=len(_call_specs(plan))) == "base"
    )
    state["automatic_continuations_used"] = sum(
        1
        for segment in scene_segments
        if _call_kind(segment, base_count=len(_call_specs(plan))) == "automatic"
    )
    state["manual_continuations_used"] = sum(
        1
        for segment in scene_segments
        if _call_kind(segment, base_count=len(_call_specs(plan))) == "manual"
    )
    scene_text = _scene_text(scene_segments, scene_index=scene_index)
    replay_measurement = _scene_replay_measurement(scene_segments)
    # Preserve the established `word_count` contract while making both count
    # meanings explicit for v3 progress and the stricter completion gate.
    state["word_count"] = replay_measurement.raw_word_count
    state["raw_word_count"] = replay_measurement.raw_word_count
    state["effective_word_count"] = replay_measurement.effective_word_count
    state["replayed_characters_total"] = (
        replay_measurement.replayed_characters_total
    )
    state["scene_target_words"] = _scene_target_words(plan, scene_index)
    converge_segments = [
        segment
        for segment in scene_segments
        if str(segment.get("prompt_mode") or "")
        in {"converge", "final_converge"}
    ]
    state["converge_attempts"] = len(converge_segments)
    state["converge_attempts_without_stop"] = sum(
        str(segment.get("finish_reason") or "unreported") != "stop"
        for segment in converge_segments
    )
    state["continues_truncated_output_count"] = sum(
        bool(segment.get("continues_truncated_output"))
        for segment in scene_segments
    )
    state["max_cross_call_repeat_characters"] = max(
        (
            max(0, int(segment.get("cross_call_repeat_characters") or 0))
            for segment in scene_segments
        ),
        default=0,
    )
    if scene_segments:
        latest = scene_segments[-1]
        state["last_prompt_mode"] = str(latest.get("prompt_mode") or "base")
        state["last_finish_reason"] = str(latest.get("finish_reason") or "unreported")
        state["last_raw_finish_reason"] = str(
            latest.get("raw_finish_reason") or "unreported"
        )
        if latest.get("no_progress"):
            latest_sequence = int(latest.get("sequence_index") or 0)
            if int(state.get("last_no_progress_sequence") or -1) != latest_sequence:
                state["consecutive_no_progress"] = (
                    int(state.get("consecutive_no_progress") or 0) + 1
                )
                state["last_no_progress_sequence"] = latest_sequence
            else:
                state["consecutive_no_progress"] = max(
                    1, int(state.get("consecutive_no_progress") or 0)
                )
        elif latest.get("text"):
            state["consecutive_no_progress"] = 0
            state["last_no_progress_sequence"] = None
    else:
        state.setdefault("consecutive_no_progress", 0)
        state.setdefault("last_prompt_mode", None)
        state.setdefault("last_finish_reason", "unreported")
        state.setdefault("last_raw_finish_reason", "unreported")

    if _is_scene_complete(
        scene_text=scene_text,
        effective_word_count=replay_measurement.effective_word_count,
        finish_reason=str(state.get("last_finish_reason") or "unreported"),
        plan=plan,
        scene_index=scene_index,
    ):
        state["status"] = "complete"
        state["pause_reason"] = None
    elif state.get("status") != "paused":
        state["status"] = "incomplete" if scene_segments else "pending"
        state.setdefault("pause_reason", None)
    return state


def _scene_prompt(
    *,
    plan: ProseExecutionPlan,
    base_prompt: str,
    outline: Mapping[str, Any],
    scene_index: int,
    target_words: int,
    prior_text: str,
    prompt_mode: str,
    continues_truncated_output: bool = False,
) -> str:
    scenes = list((outline or {}).get("scenes") or [])
    current_scene = scenes[scene_index] if scene_index < len(scenes) else {}
    seam_window = scene_continuation_seam_window_characters(
        _scene_target_words(plan, scene_index)
    )
    tail = str(prior_text or "")[-seam_window:] or "（无）"
    mode_instructions = {
        "base": (
            "只写当前场景，不提前进入后续场景；在合适的位置自然收束当前场景。"
        ),
        "fill": (
            "当前场景尚未达到最低字数。紧接已有正文补足必要动作、反应和结果，"
            "不要复述或重写前文，也不要提前写后续场景。"
        ),
        "converge": (
            "当前场景已达到最低字数。不要新增事件、不要回顾前文；只完成当前动作或"
            "反应，落下场景结果后立即停止。"
        ),
        "final_converge": (
            "这是本场最后一次已授权自动尝试。优先保证衔接：如仍低于最低字数，只补"
            "必要内容；无论如何都不要展开新支线，完成当前动作、反应和场景结果后停止。"
        ),
        "final_recovery": (
            "这是本场最后一次已授权自动尝试，且上一段与已有正文完全重复。不要重复任何"
            "已有句子，也不要展开新支线；从尾部未完成的动作或反应补足必要内容并立即收束。"
        ),
        "recovery": (
            "上一段输出与已写正文完全重复。不要重复任何已有句子；从正文末尾尚未完成"
            "的动作或反应继续，只写新的连续正文。"
        ),
        "manual": (
            "这是用户明确发起的一次手动续写。紧接已有正文继续，不重写开头、不总结前文；"
            "只处理当前场景。"
        ),
        "uncertain_retry": (
            "上一请求的结果不确定，用户已确认重试。保留已有正文，只从其末尾继续，"
            "避免复述或重复。"
        ),
    }
    if frozenset(mode_instructions) != CONTINUATION_PROMPT_MODES:
        raise RuntimeError("Continuation prompt mode registry drifted")
    instruction = mode_instructions.get(prompt_mode, mode_instructions["base"])
    if (
        continues_truncated_output
        and prompt_mode in _TRUNCATED_OUTPUT_CONTINUATION_MODES
    ):
        instruction += TRUNCATED_OUTPUT_CONTINUATION_INSTRUCTION
    if prompt_mode == "base":
        # Keep the initial-call prompt byte-for-byte stable. This slice only
        # changes continuation framing, never the base generation task.
        return (
            f"{base_prompt}\n\n"
            "【Novel-G 场景正文协议】\n"
            f"SCENE_INDEX={scene_index}\n"
            f"CONTINUATION_MODE={prompt_mode}\n"
            f"本次目标约 {max(1, int(target_words))} 字；这是近似写作目标，不是硬性截断上限。\n"
            "本次只写当前场景，以本段目标为准。\n"
            f"{instruction}\n"
            f"当前场景：{current_scene}\n"
            f"已写正文尾部（仅用于衔接，禁止复述）：{tail}\n"
            "只输出小说正文，不输出场景标题、协议字段、解释或完成声明。"
        )

    anchor, anchor_was_shortened = _continuation_anchor(tail)
    anchor_line = (
        "已写正文的最后一句（或被截断的最后句段）超过 "
        f"{MAX_CONTINUATION_ANCHOR_CHARACTERS} 字，以下仅显示其末尾：{anchor}\n"
        if anchor_was_shortened
        else f"已写正文的最后一句（或被截断的最后句段）是：{anchor}\n"
    )
    return (
        f"{base_prompt}\n\n"
        "【Novel-G 场景正文协议】\n"
        f"SCENE_INDEX={scene_index}\n"
        f"CONTINUATION_MODE={prompt_mode}\n"
        f"本次目标约 {max(1, int(target_words))} 字；这是近似写作目标，不是硬性截断上限。\n"
        "本次只写当前场景，以本段目标为准。\n"
        f"{instruction}\n"
        "【本场已完成正文】\n"
        "以下是本场已经完成的正文（按续写窗口保留其末段），不是待写任务；"
        "不得重写、复述或从场景开头重新开始。\n"
        f"{tail}\n"
        "【续写锚点】\n"
        f"{anchor_line}"
        "必须从这句之后继续；若该句未完，先自然接完它，再写新的连续正文。\n"
        "【当前场景细纲（仅作参考）】\n"
        f"{current_scene}\n"
        "细纲描述整场目标；已经在本场已完成正文中实现的部分不得当作待写任务。\n"
        "重复已经完成的正文不会增加有效字数。\n"
        "只输出小说正文，不输出场景标题、协议字段、解释或完成声明。"
    )


def _ordered_segments(by_sequence: Mapping[int, Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(segment)
        for segment in sorted(by_sequence.values(), key=_segment_sort_key)
    )


def _result(
    *,
    plan: ProseExecutionPlan,
    by_sequence: Mapping[int, Mapping[str, Any]],
    progress_by_scene: Mapping[int, Mapping[str, Any]],
    outline_revision: str,
    pause_reason: str | None,
) -> ProseGenerationResult:
    ordered = _ordered_segments(by_sequence)
    text = "\n\n".join(
        _scene_text(ordered, scene_index=scene_index)
        for scene_index in range(plan.scene_count)
        if _scene_text(ordered, scene_index=scene_index).strip()
    )
    completed_scene_indexes = [
        scene_index
        for scene_index in range(plan.scene_count)
        if str((progress_by_scene.get(scene_index) or {}).get("status")) == "complete"
    ]
    effective_chapter_word_count = sum(
        max(0, int((progress_by_scene.get(scene_index) or {}).get(
            "effective_word_count",
            0,
        ) or 0))
        for scene_index in range(plan.scene_count)
    )
    last = ordered[-1] if ordered else {}
    completion = prose_completion_module.inspect(
        text=text,
        plan=plan,
        finish_reason=last.get("finish_reason") or "unreported",
        raw_finish_reason=last.get("raw_finish_reason") or "unreported",
        completed_scene_indexes=completed_scene_indexes,
        outline_revision=outline_revision,
        expected_outline_revision=outline_revision,
        effective_word_count=effective_chapter_word_count,
    )
    usage_items: list[TokenUsage] = []
    for segment in ordered:
        try:
            usage_items.append(TokenUsage.model_validate(segment.get("usage") or {}))
        except Exception:
            usage_items.append(TokenUsage())
    return ProseGenerationResult(
        text=text,
        segments=ordered,
        usage=_usage_sum(usage_items),
        completion=completion,
        outline_revision=outline_revision,
        scene_progress=tuple(
            dict(progress_by_scene[index])
            for index in sorted(progress_by_scene)
        ),
        pause_reason=pause_reason,
    )


async def execute_v3_prose_plan(
    *,
    plan: ProseExecutionPlan,
    outline: dict[str, Any],
    base_prompt: str,
    stream_call: StreamCall,
    finish_reason_reader: Callable[[], Any],
    usage_reader: Callable[[], TokenUsage],
    outline_revision: str,
    raw_finish_reason_reader: Callable[[], Any] | None = None,
    gen_kwargs: Mapping[str, Any] | None = None,
    existing_segments: Iterable[dict[str, Any]] = (),
    existing_scene_progress: Iterable[dict[str, Any]] = (),
    continuation_policy: ProseContinuationPolicy | None = None,
    confirm_uncertain_retry: bool = False,
    manual_continuation: bool = False,
    stop_after_scene_index: int | None = None,
    on_delta: DeltaCallback | None = None,
    on_segment: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    on_scene_progress: SceneProgressCallback | None = None,
) -> ProseGenerationResult:
    """Execute v3 with a shared continuation quota for every logical scene."""
    policy = continuation_policy or ProseContinuationPolicy()
    if stop_after_scene_index is not None and not (
        0 <= int(stop_after_scene_index) < plan.scene_count
    ):
        raise ValueError("stop_after_scene_index must name a planned scene")
    specs = _call_specs(plan)
    base_by_sequence = {spec.sequence_index: spec for spec in specs}
    by_sequence: dict[int, dict[str, Any]] = {}
    for raw in existing_segments:
        segment = dict(raw)
        try:
            sequence = int(segment.get("sequence_index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Persisted prose segment is missing sequence_index") from exc
        if sequence in by_sequence:
            raise ValueError("Persisted prose segments contain duplicate sequences")
        if sequence in base_by_sequence:
            spec = base_by_sequence[sequence]
            if (
                int(segment.get("scene_index", -1)) != spec.scene_index
                or int(segment.get("part_index", -1)) != spec.part_index
            ):
                raise ValueError("Persisted prose segment order is stale")
        by_sequence[sequence] = segment

    # A budget refusal is created only when the local reservation rejects a
    # call before `stream_call` can reach a Provider. On a later, new execution
    # (for example after the batch budget is re-authorized), that logical call
    # may safely reuse its original sequence. Keeping it active would otherwise
    # make every resume immediately pause again without inspecting the new cap.
    # The next dispatch checkpoint replaces the terminal record at this same
    # sequence, so a paid/uncertain Provider attempt is never silently retried.
    retryable_budget_refusals = {
        sequence
        for sequence, segment in by_sequence.items()
        if _is_safe_pre_dispatch_budget_refusal(segment)
    }
    if retryable_budget_refusals:
        by_sequence = {
            sequence: segment
            for sequence, segment in by_sequence.items()
            if sequence not in retryable_budget_refusals
        }

    uncertain = [
        segment
        for segment in by_sequence.values()
        if str(segment.get("status") or "") == "uncertain"
    ]
    if uncertain and not confirm_uncertain_retry:
        raise UncertainProseAttempt(
            "存在已派发但结果未确认的正文请求，可能已经计费；请明确确认可能重复计费后再继续。"
        )

    persisted_progress = {
        int(item.get("scene_index")): dict(item)
        for item in existing_scene_progress
        if item.get("scene_index") is not None
        and 0 <= int(item.get("scene_index")) < plan.scene_count
    }
    progress_by_scene: dict[int, dict[str, Any]] = {
        scene_index: _scene_progress_snapshot(
            scene_index=scene_index,
            plan=plan,
            segments=by_sequence.values(),
            previous=persisted_progress.get(scene_index),
        )
        for scene_index in range(plan.scene_count)
    }

    async def publish_progress() -> None:
        await _notify(
            on_scene_progress,
            tuple(dict(progress_by_scene[index]) for index in sorted(progress_by_scene)),
        )

    def refresh_scene(scene_index: int) -> dict[str, Any]:
        progress_by_scene[scene_index] = _scene_progress_snapshot(
            scene_index=scene_index,
            plan=plan,
            segments=by_sequence.values(),
            previous=progress_by_scene.get(scene_index),
        )
        return progress_by_scene[scene_index]

    async def perform_call(
        *,
        scene_index: int,
        spec: _CallSpec | None,
        call_kind: str,
        prompt_mode: str,
    ) -> dict[str, Any]:
        current_text = _scene_text(by_sequence.values(), scene_index=scene_index)
        previous_scene_segments = _ordered_scene_segments(
            by_sequence.values(), scene_index=scene_index
        )
        previous_segment = (
            previous_scene_segments[-1] if previous_scene_segments else {}
        )
        continues_truncated_output = (
            prompt_mode in _TRUNCATED_OUTPUT_CONTINUATION_MODES
            and str(previous_segment.get("finish_reason") or "") == "length"
        )
        target_words = (
            max(1, int(spec.target_words))
            if call_kind == "base" and spec is not None
            else policy.continuation_target_words
        )
        if call_kind == "base" and spec is not None:
            sequence = spec.sequence_index
            part_index = spec.part_index
            part_count = spec.part_count
        else:
            sequence = _next_automatic_sequence(by_sequence.values())
            existing_scene_specs = [item for item in specs if item.scene_index == scene_index]
            part_index = existing_scene_specs[-1].part_index if existing_scene_specs else 0
            part_count = existing_scene_specs[-1].part_count if existing_scene_specs else 1
        scene_call_index = _next_scene_call_index(
            by_sequence.values(), scene_index=scene_index
        )
        prompt = _scene_prompt(
            plan=plan,
            base_prompt=base_prompt,
            outline=outline,
            scene_index=scene_index,
            target_words=target_words,
            prior_text=current_text,
            prompt_mode=prompt_mode,
            continues_truncated_output=continues_truncated_output,
        )
        call_kwargs = dict(gen_kwargs or {})
        call_kwargs["max_tokens"] = v3_output_token_bound(
            target_words=target_words,
            inherited_max_tokens=call_kwargs.get("max_tokens"),
        )

        state = refresh_scene(scene_index)
        state["status"] = "generating"
        state["pause_reason"] = None
        state["last_prompt_mode"] = prompt_mode
        await publish_progress()

        checkpoint = {
            "sequence_index": sequence,
            "scene_index": scene_index,
            "part_index": part_index,
            "part_count": part_count,
            "scene_call_index": scene_call_index,
            "target_word_count": target_words,
            "call_kind": call_kind,
            "prompt_mode": prompt_mode,
            "continues_truncated_output": continues_truncated_output,
            "status": "uncertain",
            "text": "",
            "text_digest": hashlib.sha256(b"").hexdigest(),
            "word_count": 0,
            "raw_character_count": 0,
            "cross_call_repeat_characters": 0,
            "finish_reason": "unreported",
            "raw_finish_reason": "unreported",
            "usage": TokenUsage().model_dump(),
            "continuation_count": int(
                state.get("automatic_continuations_used") or 0
            ) + int(state.get("manual_continuations_used") or 0),
        }
        by_sequence[sequence] = checkpoint
        await _notify(on_segment, checkpoint)

        chunks: list[str] = []
        try:
            async for chunk in stream_call(prompt, call_kwargs):
                if not chunk:
                    continue
                chunks.append(chunk)
                await _notify(on_delta, chunk)
        except asyncio.CancelledError:
            generated = "".join(chunks).strip()
            contribution = _deduplicate_exact_seam(current_text, generated).strip()
            cross_call_repeat_characters = (
                _longest_exact_common_substring_characters(current_text, contribution)
            )
            terminal = {
                **checkpoint,
                "status": "incomplete",
                "text": contribution,
                "text_digest": hashlib.sha256(contribution.encode("utf-8")).hexdigest(),
                "word_count": count_chapter_words(contribution),
                "raw_character_count": len(contribution),
                "cross_call_repeat_characters": cross_call_repeat_characters,
                "finish_reason": "cancelled",
                "raw_finish_reason": "cancelled",
                "empty_output": not bool(generated),
                "no_progress": bool(generated) and not bool(contribution),
            }
            by_sequence[sequence] = terminal
            refresh_scene(scene_index)
            await asyncio.shield(_notify(on_segment, terminal))
            await asyncio.shield(publish_progress())
            raise
        except TokenBudgetExceeded:
            # This is a proven pre-dispatch refusal: it must pause the scene,
            # but it must never masquerade as an uncertain paid request.
            generated = "".join(chunks).strip()
            contribution = _deduplicate_exact_seam(current_text, generated).strip()
            cross_call_repeat_characters = (
                _longest_exact_common_substring_characters(current_text, contribution)
            )
            terminal = {
                **checkpoint,
                "status": "incomplete",
                "text": contribution,
                "text_digest": hashlib.sha256(contribution.encode("utf-8")).hexdigest(),
                "word_count": count_chapter_words(contribution),
                "raw_character_count": len(contribution),
                "cross_call_repeat_characters": cross_call_repeat_characters,
                "finish_reason": "budget",
                "raw_finish_reason": "budget",
                "error_code": "token_budget_exceeded_before_dispatch",
                "usage": TokenUsage().model_dump(),
                "empty_output": not bool(generated),
                "no_progress": bool(generated) and not bool(contribution),
            }
            by_sequence[sequence] = terminal
            state = refresh_scene(scene_index)
            state["status"] = "paused"
            state["pause_reason"] = terminal["error_code"]
            await _notify(on_segment, terminal)
            await publish_progress()
            return terminal
        except Exception:
            generated = "".join(chunks).strip()
            contribution = _deduplicate_exact_seam(current_text, generated).strip()
            cross_call_repeat_characters = (
                _longest_exact_common_substring_characters(current_text, contribution)
            )
            try:
                observed_usage = usage_reader()
            except Exception:
                observed_usage = TokenUsage()
            terminal = {
                **checkpoint,
                "status": "uncertain",
                "text": contribution,
                "text_digest": hashlib.sha256(contribution.encode("utf-8")).hexdigest(),
                "word_count": count_chapter_words(contribution),
                "raw_character_count": len(contribution),
                "cross_call_repeat_characters": cross_call_repeat_characters,
                "finish_reason": "error",
                "raw_finish_reason": "error",
                "usage": observed_usage.model_dump(),
                "empty_output": not bool(generated),
                "no_progress": bool(generated) and not bool(contribution),
            }
            by_sequence[sequence] = terminal
            refresh_scene(scene_index)
            await _notify(on_segment, terminal)
            await publish_progress()
            raise

        generated = "".join(chunks).strip()
        contribution = _deduplicate_exact_seam(current_text, generated).strip()
        cross_call_repeat_characters = (
            _longest_exact_common_substring_characters(current_text, contribution)
        )
        observed_finish = finish_reason_reader()
        raw_finish = (
            raw_finish_reason_reader()
            if raw_finish_reason_reader is not None
            else observed_finish
        )
        finish_reason, raw_finish_reason = _normal_finish_reason(
            observed_finish,
            raw_finish,
        )
        try:
            usage = usage_reader()
        except Exception:
            usage = TokenUsage()
        candidate_text = "\n\n".join(
            part for part in (current_text, contribution) if part
        )
        candidate_replay_measurement = _scene_replay_measurement(
            [
                *previous_scene_segments,
                {
                    **checkpoint,
                    "text": contribution,
                },
            ]
        )
        scene_complete = _is_scene_complete(
            scene_text=candidate_text,
            effective_word_count=(
                candidate_replay_measurement.effective_word_count
            ),
            finish_reason=finish_reason,
            plan=plan,
            scene_index=scene_index,
        )
        terminal = {
            **checkpoint,
            "status": "completed" if scene_complete else "incomplete",
            "text": contribution,
            "text_digest": hashlib.sha256(contribution.encode("utf-8")).hexdigest(),
            "word_count": count_chapter_words(contribution),
            "raw_character_count": len(contribution),
            "cross_call_repeat_characters": cross_call_repeat_characters,
            "finish_reason": finish_reason,
            "raw_finish_reason": raw_finish_reason,
            "usage": usage.model_dump(),
            "empty_output": not bool(generated),
            "no_progress": bool(generated) and not bool(contribution),
        }
        by_sequence[sequence] = terminal
        state = refresh_scene(scene_index)
        if scene_complete:
            state["status"] = "complete"
            state["pause_reason"] = None
        await _notify(on_segment, terminal)
        await publish_progress()
        return terminal

    pause_reason: str | None = None
    retry_used = False
    for scene_index in range(plan.scene_count):
        while True:
            state = refresh_scene(scene_index)
            if state.get("status") == "complete":
                break

            scene_segments = _ordered_scene_segments(
                by_sequence.values(), scene_index=scene_index
            )
            latest = scene_segments[-1] if scene_segments else None
            scene_specs = [spec for spec in specs if spec.scene_index == scene_index]
            base_sequences = {spec.sequence_index for spec in scene_specs}
            missing_base = next(
                (spec for spec in scene_specs if spec.sequence_index not in by_sequence),
                None,
            )

            # A manual click is deliberately one provider call only. It never
            # silently spills into another base part, scene, or automatic quota.
            if manual_continuation:
                await perform_call(
                    scene_index=scene_index,
                    spec=missing_base or (scene_specs[-1] if scene_specs else None),
                    call_kind="manual",
                    prompt_mode="manual",
                )
                state = refresh_scene(scene_index)
                if state.get("status") != "complete":
                    state["status"] = "paused"
                    state["pause_reason"] = "manual_continuation_finished"
                    await publish_progress()
                    pause_reason = "manual_continuation_finished"
                return _result(
                    plan=plan,
                    by_sequence=by_sequence,
                    progress_by_scene=progress_by_scene,
                    outline_revision=outline_revision,
                    pause_reason=pause_reason,
                )

            if latest is not None and latest.get("status") == "uncertain":
                # Explicit acknowledgement lets exactly one retry cross the
                # boundary. Its result then re-enters ordinary scene evaluation.
                if retry_used:
                    state["status"] = "paused"
                    state["pause_reason"] = "uncertain_provider_attempt"
                    await publish_progress()
                    pause_reason = state["pause_reason"]
                    break
                retry_used = True
                await perform_call(
                    scene_index=scene_index,
                    spec=missing_base or (scene_specs[-1] if scene_specs else None),
                    call_kind="uncertain_retry",
                    prompt_mode="uncertain_retry",
                )
                continue

            if latest is None:
                if missing_base is None:
                    state["status"] = "paused"
                    state["pause_reason"] = "missing_scene_base_call"
                    await publish_progress()
                    pause_reason = state["pause_reason"]
                    break
                await perform_call(
                    scene_index=scene_index,
                    spec=missing_base,
                    call_kind="base",
                    prompt_mode="base",
                )
                continue

            if latest.get("error_code") == "token_budget_exceeded_before_dispatch":
                state["status"] = "paused"
                state["pause_reason"] = "token_budget_exceeded_before_dispatch"
                await publish_progress()
                pause_reason = state["pause_reason"]
                break

            if latest.get("empty_output"):
                state["status"] = "paused"
                state["pause_reason"] = "prose_empty_output"
                await publish_progress()
                pause_reason = state["pause_reason"]
                break

            failure = _provider_failure_reason(
                str(latest.get("finish_reason") or "unreported")
            )
            if failure is not None:
                state["status"] = "paused"
                state["pause_reason"] = failure
                await publish_progress()
                pause_reason = state["pause_reason"]
                break

            no_progress = bool(latest.get("no_progress"))
            consecutive_no_progress = int(state.get("consecutive_no_progress") or 0)
            if no_progress and consecutive_no_progress >= 2:
                state["status"] = "paused"
                state["pause_reason"] = "prose_no_progress"
                await publish_progress()
                pause_reason = state["pause_reason"]
                break

            # Baseline parts come first for normal progress. An exact duplicate
            # instead gets one recovery attempt, so an old prefix cannot consume
            # the remaining planned parts as if new prose had been written.
            if missing_base is not None and not no_progress:
                await perform_call(
                    scene_index=scene_index,
                    spec=missing_base,
                    call_kind="base",
                    prompt_mode="base",
                )
                continue

            automatic_used = int(state.get("automatic_continuations_used") or 0)
            remaining_automatic = (
                policy.automatic_continuations_per_scene - automatic_used
            )
            if no_progress and remaining_automatic <= 0:
                state["status"] = "paused"
                state["pause_reason"] = "prose_no_progress_without_quota"
                await publish_progress()
                pause_reason = state["pause_reason"]
                break

            if remaining_automatic <= 0:
                state["status"] = "paused"
                state["pause_reason"] = "automatic_continuations_exhausted"
                await publish_progress()
                pause_reason = state["pause_reason"]
                break

            scene_word_count = int(state.get("word_count") or 0)
            minimum_words = _scene_minimum_words(plan, scene_index)
            if no_progress:
                prompt_mode = (
                    "final_recovery" if remaining_automatic == 1 else "recovery"
                )
            elif remaining_automatic == 1:
                prompt_mode = "final_converge"
            elif scene_word_count < minimum_words:
                prompt_mode = "fill"
            else:
                prompt_mode = "converge"
            await perform_call(
                scene_index=scene_index,
                spec=scene_specs[-1] if scene_specs else None,
                call_kind="automatic",
                prompt_mode=prompt_mode,
            )

        if pause_reason is not None:
            break
        if (
            stop_after_scene_index is not None
            and scene_index >= int(stop_after_scene_index)
        ):
            pause_reason = "target_scene_reached"
            break

    return _result(
        plan=plan,
        by_sequence=by_sequence,
        progress_by_scene=progress_by_scene,
        outline_revision=outline_revision,
        pause_reason=pause_reason,
    )
