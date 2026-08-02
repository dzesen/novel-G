"""Execute a prose plan through one bounded call per planned segment.

This module owns segmentation and deterministic assembly. It knows nothing about
HTTP, MongoDB, jobs, or a concrete Provider; callers inject the streaming Adapter
and persist both the pre-dispatch checkpoint and terminal segment through
``on_segment``.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping

from backend.llm.models import TokenUsage
from backend.llm.stream_terminal import normalize_finish_reason
from backend.services.generation.prose_completion import (
    ProseCompletion,
    ProseExecutionPlan,
    prose_completion_module,
)
from backend.services.generation.prose_continuation import ProseContinuationPolicy
from backend.services.generation.prose_protocol import (
    is_scene_continuation_v3_family,
)
from backend.services.novel.chapter_service import count_chapter_words


StreamCall = Callable[[str, dict[str, Any]], AsyncIterator[str]]
DeltaCallback = Callable[[str], Awaitable[None] | None]
SegmentCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class UncertainProseAttempt(ValueError):
    """A Provider call may already have been billed and needs explicit retry."""


class ProseContinuationLimit(ValueError):
    """A short segment exhausted its bounded resume allowance."""


@dataclass(frozen=True)
class ProseGenerationResult:
    text: str
    segments: tuple[dict[str, Any], ...]
    usage: TokenUsage
    completion: ProseCompletion
    outline_revision: str
    scene_progress: tuple[dict[str, Any], ...] = ()
    pause_reason: str | None = None


@dataclass(frozen=True)
class _CallSpec:
    sequence_index: int
    scene_index: int
    part_index: int
    part_count: int
    target_words: int


async def _notify(callback: Callable[[Any], Any] | None, value: Any) -> None:
    if callback is None:
        return
    produced = callback(value)
    if inspect.isawaitable(produced):
        await produced


def _split_budget(total: int, count: int) -> list[int]:
    base, remainder = divmod(max(1, total), max(1, count))
    return [base + (1 if index >= count - remainder else 0) for index in range(count)]


def _call_specs(plan: ProseExecutionPlan) -> list[_CallSpec]:
    if plan.mode == "single_call":
        return [
            _CallSpec(
                sequence_index=0,
                scene_index=0,
                part_index=0,
                part_count=1,
                target_words=plan.requested_word_count,
            )
        ]
    specs: list[_CallSpec] = []
    sequence = 0
    for scene_index, scene_budget in enumerate(plan.segment_budgets):
        part_count = max(1, math.ceil(scene_budget / plan.safe_output_budget))
        for part_index, part_budget in enumerate(
            _split_budget(scene_budget, part_count)
        ):
            specs.append(
                _CallSpec(
                    sequence_index=sequence,
                    scene_index=scene_index,
                    part_index=part_index,
                    part_count=part_count,
                    target_words=part_budget,
                )
            )
            sequence += 1
    return specs


def planned_base_call_target_words(plan: ProseExecutionPlan) -> tuple[int, ...]:
    """Expose the exact v3/legacy base-call targets without executing them."""
    return tuple(spec.target_words for spec in _call_specs(plan))


def _segment_prompt(
    *,
    plan: ProseExecutionPlan,
    base_prompt: str,
    outline: dict[str, Any],
    spec: _CallSpec,
    prior_tail: str,
    is_resume: bool = False,
) -> str:
    scenes = list((outline or {}).get("scenes") or [])
    if plan.mode == "single_call":
        continuation = (
            "这是同一章的续写。紧接前文继续，不重写开头，不总结已写内容。"
            if is_resume
            else f"按章细纲顺序完整写完全部 {plan.scene_count} 个场景，不遗漏后续场景。"
        )
        return (
            f"{base_prompt}\n\n"
            "【Novel-G 单次正文协议】\n"
            "SCENE_INDEX=ALL\n"
            f"本章目标约 {spec.target_words} 字。\n"
            f"{continuation}\n"
            f"完整场景列表：{json.dumps(scenes, ensure_ascii=False, default=str)}\n"
            f"已完成正文尾部（仅用于衔接，禁止复述）：{prior_tail or '无'}\n"
            "只输出小说正文，不输出场景标题、协议字段、解释或完成声明。"
        )
    current_scene = scenes[spec.scene_index] if spec.scene_index < len(scenes) else {}
    remaining = scenes[spec.scene_index + 1 :]
    continuation = (
        "这是同一场景的续写。紧接前文继续，不重写开头，不总结已写内容。"
        if spec.part_index or is_resume
        else "只写当前场景，不提前写后续场景。"
    )
    return (
        f"{base_prompt}\n\n"
        "【Novel-G 分段正文协议】\n"
        f"SCENE_INDEX={spec.scene_index}\n"
        f"PART_INDEX={spec.part_index}\n"
        f"PART_COUNT={spec.part_count}\n"
        "上方“本章目标字数”是全章总量，不是本次调用的目标。"
        "本次只写当前场景，以本段目标为准；达到目标后完整收束当前场景并停止，"
        "不要为了凑全章字数继续扩写。\n"
        f"本段目标约 {spec.target_words} 字。\n"
        f"{continuation}\n"
        f"当前场景：{json.dumps(current_scene, ensure_ascii=False, default=str)}\n"
        f"后续场景（本次禁止提前写）：{json.dumps(remaining, ensure_ascii=False, default=str)}\n"
        f"已完成正文尾部（仅用于衔接，禁止复述）：{prior_tail or '无'}\n"
        "只输出小说正文，不输出场景标题、协议字段、解释或完成声明。"
    )


def _usage_sum(items: Iterable[TokenUsage]) -> TokenUsage:
    values = list(items)
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in values),
        output_tokens=sum(item.output_tokens for item in values),
        total_tokens=sum(item.total_tokens for item in values),
    )


async def _execute_legacy_prose_plan(
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
    confirm_uncertain_retry: bool = False,
    manual_continuation: bool = False,
    on_delta: DeltaCallback | None = None,
    on_segment: SegmentCallback | None = None,
) -> ProseGenerationResult:
    """Run missing call specs and return a completion-checked chapter draft."""
    specs = _call_specs(plan)
    by_sequence: dict[int, dict[str, Any]] = {}
    for raw in existing_segments:
        segment = dict(raw)
        sequence = int(segment.get("sequence_index", -1))
        if sequence < 0 or sequence >= len(specs) or sequence in by_sequence:
            raise ValueError("Persisted prose segments do not match the current plan")
        spec = specs[sequence]
        if (
            int(segment.get("scene_index", -1)) != spec.scene_index
            or int(segment.get("part_index", -1)) != spec.part_index
        ):
            raise ValueError("Persisted prose segment order is stale")
        by_sequence[sequence] = segment

    uncertain_sequences = sorted(
        sequence
        for sequence, segment in by_sequence.items()
        if segment.get("status") == "uncertain"
    )
    if uncertain_sequences and not confirm_uncertain_retry:
        raise UncertainProseAttempt(
            "存在已派发但未确认结果的正文请求，可能已经计费；"
            "请明确确认可能重复计费后再继续"
        )

    usages: list[TokenUsage] = [
        TokenUsage.model_validate(item.get("usage") or {})
        for item in by_sequence.values()
    ]
    kwargs = dict(gen_kwargs or {})
    for spec in specs:
        existing_segment = by_sequence.get(spec.sequence_index)
        if existing_segment is not None and existing_segment.get("status") == "completed":
            continue
        if (
            existing_segment is not None
            and not manual_continuation
            and not confirm_uncertain_retry
        ):
            raise ProseContinuationLimit(
                "这份正文草稿尚未完成；默认不会自动继续付费调用，"
                "请由用户明确发起一次手动续写"
            )
        prior_text = "\n\n".join(
            str(by_sequence[index].get("text") or "").strip()
            for index in sorted(by_sequence)
        )
        existing_text = str((existing_segment or {}).get("text") or "").strip()
        remaining_words = max(
            1,
            spec.target_words - count_chapter_words(existing_text),
        )
        effective_spec = _CallSpec(
            sequence_index=spec.sequence_index,
            scene_index=spec.scene_index,
            part_index=spec.part_index,
            part_count=spec.part_count,
            target_words=remaining_words,
        )
        prompt = _segment_prompt(
            plan=plan,
            base_prompt=base_prompt,
            outline=outline,
            spec=effective_spec,
            prior_tail=prior_text[-2_000:],
            is_resume=bool(existing_text),
        )
        call_kwargs = dict(kwargs)
        if not call_kwargs.get("max_tokens"):
            call_kwargs["max_tokens"] = max(
                256,
                math.ceil(remaining_words / 0.65),
            )

        # Persist before crossing the Provider boundary. If the process dies after
        # dispatch but before the first chunk/usage, this checkpoint survives and
        # prevents an automatic paid retry.
        previous_usage = TokenUsage.model_validate(
            (existing_segment or {}).get("usage") or {}
        )
        dispatch_checkpoint = {
            "sequence_index": spec.sequence_index,
            "scene_index": spec.scene_index,
            "part_index": spec.part_index,
            "part_count": spec.part_count,
            "target_word_count": spec.target_words,
            "status": "uncertain",
            "text": existing_text,
            "text_digest": hashlib.sha256(
                existing_text.encode("utf-8")
            ).hexdigest(),
            "word_count": count_chapter_words(existing_text),
            "raw_character_count": len(existing_text),
            "finish_reason": "unreported",
            "raw_finish_reason": "unreported",
            "usage": previous_usage.model_dump(),
            "continuation_count": int(
                (existing_segment or {}).get("continuation_count") or 0
            ),
        }
        by_sequence[spec.sequence_index] = dispatch_checkpoint
        await _notify(on_segment, dispatch_checkpoint)

        chunks: list[str] = []
        try:
            async for chunk in stream_call(prompt, call_kwargs):
                if not chunk:
                    continue
                chunks.append(chunk)
                await _notify(on_delta, chunk)
        except asyncio.CancelledError:
            generated_text = "".join(chunks).strip()
            partial_text = "\n\n".join(
                part for part in (existing_text, generated_text) if part
            )
            if partial_text:
                partial_segment = {
                    "sequence_index": spec.sequence_index,
                    "scene_index": spec.scene_index,
                    "part_index": spec.part_index,
                    "part_count": spec.part_count,
                    "target_word_count": spec.target_words,
                    "status": "incomplete",
                    "text": partial_text,
                    "text_digest": hashlib.sha256(
                        partial_text.encode("utf-8")
                    ).hexdigest(),
                    "word_count": count_chapter_words(partial_text),
                    "raw_character_count": len(partial_text),
                    "finish_reason": "cancelled",
                    "raw_finish_reason": "cancelled",
                    "usage": previous_usage.model_dump(),
                    "continuation_count": int(
                        (existing_segment or {}).get("continuation_count") or 0
                    ) + (1 if existing_segment is not None else 0),
                }
                await asyncio.shield(_notify(on_segment, partial_segment))
            raise
        except Exception:
            generated_text = "".join(chunks).strip()
            partial_text = "\n\n".join(
                part for part in (existing_text, generated_text) if part
            )
            if partial_text:
                # The Provider boundary failed after yielding text. Preserve what
                # the user already received, but keep the segment uncertain because
                # final usage/billing cannot be proven and retry needs confirmation.
                try:
                    observed_usage = usage_reader()
                except Exception:
                    observed_usage = TokenUsage()
                uncertain_segment = {
                    "sequence_index": spec.sequence_index,
                    "scene_index": spec.scene_index,
                    "part_index": spec.part_index,
                    "part_count": spec.part_count,
                    "target_word_count": spec.target_words,
                    "status": "uncertain",
                    "text": partial_text,
                    "text_digest": hashlib.sha256(
                        partial_text.encode("utf-8")
                    ).hexdigest(),
                    "word_count": count_chapter_words(partial_text),
                    "raw_character_count": len(partial_text),
                    "finish_reason": "error",
                    "raw_finish_reason": "error",
                    "usage": _usage_sum(
                        [previous_usage, observed_usage]
                    ).model_dump(),
                    "continuation_count": int(
                        (existing_segment or {}).get("continuation_count") or 0
                    ),
                }
                by_sequence[spec.sequence_index] = uncertain_segment
                await _notify(on_segment, uncertain_segment)
            raise

        generated_text = "".join(chunks).strip()
        text = "\n\n".join(
            part for part in (existing_text, generated_text) if part
        )
        observed_finish_reason = finish_reason_reader()
        finish_reason = normalize_finish_reason(observed_finish_reason)
        raw_finish_reason = (
            raw_finish_reason_reader()
            if raw_finish_reason_reader is not None
            else observed_finish_reason
        )
        raw_finish_reason = str(
            getattr(raw_finish_reason, "value", raw_finish_reason) or "unreported"
        ).strip() or "unreported"
        usage = usage_reader()
        usages.append(usage)
        persisted_usage = _usage_sum([previous_usage, usage])
        status = "completed"
        minimum_words = math.ceil(spec.target_words * plan.minimum_completion_ratio)
        if not text or finish_reason in {
            "content_filter",
            "tool_call",
            "cancelled",
            "error",
        }:
            status = "incomplete"
        elif finish_reason == "length":
            status = "incomplete"
        elif count_chapter_words(text) < minimum_words:
            status = "incomplete"

        segment = {
            "sequence_index": spec.sequence_index,
            "scene_index": spec.scene_index,
            "part_index": spec.part_index,
            "part_count": spec.part_count,
            "target_word_count": spec.target_words,
            "status": status,
            "text": text,
            "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "word_count": count_chapter_words(text),
            "raw_character_count": len(text),
            "finish_reason": finish_reason,
            "raw_finish_reason": raw_finish_reason,
            "usage": persisted_usage.model_dump(),
            "continuation_count": int(
                (existing_segment or {}).get("continuation_count") or 0
            ) + (1 if existing_segment is not None else 0),
        }
        by_sequence[spec.sequence_index] = segment
        await _notify(on_segment, segment)
        if status != "completed":
            break

    scene_segments: list[dict[str, Any]] = []
    completed_scene_indexes: list[int] = []
    for scene_index in range(plan.scene_count):
        scene_specs = [item for item in specs if item.scene_index == scene_index]
        scene_calls = [
            by_sequence.get(item.sequence_index)
            for item in scene_specs
        ]
        if not scene_calls or any(
            item is None or item.get("status") != "completed"
            for item in scene_calls
        ):
            continue
        scene_segments.append(
            {
                "scene_index": scene_index,
                "status": "completed",
                "text": "\n\n".join(
                    str(item.get("text") or "").strip()
                    for item in scene_calls
                    if item is not None
                ),
            }
        )
        completed_scene_indexes.append(scene_index)

    if plan.mode == "single_call":
        single = by_sequence.get(0)
        text = str((single or {}).get("text") or "")
        if single and single.get("status") == "completed":
            completed_scene_indexes = list(range(plan.scene_count))
    else:
        # Incomplete calls remain a previewable/resumable draft. Formal writing is
        # still gated by completed_scene_indexes and the completion contract.
        text = "\n\n".join(
            str(by_sequence[index].get("text") or "").strip()
            for index in sorted(by_sequence)
        )

    ordered_segments = tuple(by_sequence[index] for index in sorted(by_sequence))
    last_reason = (
        ordered_segments[-1].get("finish_reason")
        if ordered_segments
        else "unreported"
    )
    last_raw_reason = (
        ordered_segments[-1].get("raw_finish_reason")
        if ordered_segments
        else "unreported"
    )
    completion = prose_completion_module.inspect(
        text=text,
        plan=plan,
        finish_reason=last_reason,
        raw_finish_reason=last_raw_reason,
        completed_scene_indexes=completed_scene_indexes,
        outline_revision=outline_revision,
        expected_outline_revision=outline_revision,
    )
    return ProseGenerationResult(
        text=text,
        segments=ordered_segments,
        usage=_usage_sum(usages),
        completion=completion,
        outline_revision=outline_revision,
    )

async def execute_prose_plan(
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
    on_delta: DeltaCallback | None = None,
    on_segment: SegmentCallback | None = None,
    on_scene_progress: Callable[
        [tuple[dict[str, Any], ...]], Awaitable[None] | None
    ] | None = None,
) -> ProseGenerationResult:
    """Run the current prose protocol while preserving v2 draft readability."""
    if is_scene_continuation_v3_family(plan.protocol_revision):
        # Delayed import keeps the legacy executor import-safe for historical
        # persistence probes while letting v3 own the per-scene state machine.
        from backend.services.generation.prose_scene_execution import (
            execute_v3_prose_plan,
        )

        return await execute_v3_prose_plan(
            plan=plan,
            outline=outline,
            base_prompt=base_prompt,
            stream_call=stream_call,
            finish_reason_reader=finish_reason_reader,
            usage_reader=usage_reader,
            outline_revision=outline_revision,
            raw_finish_reason_reader=raw_finish_reason_reader,
            gen_kwargs=gen_kwargs,
            existing_segments=existing_segments,
            existing_scene_progress=existing_scene_progress,
            continuation_policy=continuation_policy,
            confirm_uncertain_retry=confirm_uncertain_retry,
            manual_continuation=manual_continuation,
            on_delta=on_delta,
            on_segment=on_segment,
            on_scene_progress=on_scene_progress,
        )
    return await _execute_legacy_prose_plan(
        plan=plan,
        outline=outline,
        base_prompt=base_prompt,
        stream_call=stream_call,
        finish_reason_reader=finish_reason_reader,
        usage_reader=usage_reader,
        outline_revision=outline_revision,
        raw_finish_reason_reader=raw_finish_reason_reader,
        gen_kwargs=gen_kwargs,
        existing_segments=existing_segments,
        confirm_uncertain_retry=confirm_uncertain_retry,
        manual_continuation=manual_continuation,
        on_delta=on_delta,
        on_segment=on_segment,
    )
