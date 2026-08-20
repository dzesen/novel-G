"""章节正文相对细纲的语义门禁与处理策略。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Mapping


class OutlineIssueCategory(StrEnum):
    """Stable categories shared by review, repair, and checkpoint contracts."""

    SCENE_COVERAGE = "scene_coverage"
    SCENE_ORDER = "scene_order"
    CORE_CONFLICT = "core_conflict"
    ENDING_HOOK = "ending_hook"
    UNPLANNED_MAJOR_EVENT = "unplanned_major_event"
    VOLUME_ARC = "volume_arc"


OUTLINE_ISSUE_CATEGORIES = frozenset(OutlineIssueCategory)
OutlineIssueCategoryValue = Literal[
    *tuple(category.value for category in OutlineIssueCategory)
]

PAUSE_FOR_REWRITE = "pause_for_rewrite"
ACCEPT_AND_CONTINUE = "accept_and_continue"
OUTLINE_DEVIATION_POLICIES = frozenset(
    {PAUSE_FOR_REWRITE, ACCEPT_AND_CONTINUE}
)


class OutlineAdherenceValidationError(ValueError):
    """章纲符合度证据不足以解锁正式正文。"""


def validate_outline_deviation_policy(value: str) -> str:
    policy = str(value or "").strip()
    if policy not in OUTLINE_DEVIATION_POLICIES:
        raise ValueError(
            "outline_deviation_policy 必须为 "
            "pause_for_rewrite 或 accept_and_continue"
        )
    return policy


def normalize_outline_adherence(result: dict[str, Any]) -> dict[str, Any]:
    """让 verdict 与结构化问题保持一致，防止模型自相矛盾。"""

    normalized = dict(result)
    issues = [dict(item) for item in result.get("issues") or []]
    coverage = [dict(item) for item in result.get("scene_coverage") or []]
    reported = str(result.get("verdict") or "warn")
    severities = {str(item.get("severity") or "") for item in issues}

    if "error" in severities or reported == "fail":
        verdict = "fail"
    elif "warning" in severities or reported == "warn":
        verdict = "warn"
    else:
        verdict = "pass"

    normalized["verdict"] = verdict
    normalized["summary"] = str(result.get("summary") or "未提供审查摘要")
    normalized["scene_coverage"] = coverage
    normalized["issues"] = issues
    return normalized


def is_material_deviation(result: dict[str, Any]) -> bool:
    return str(result.get("verdict") or "") == "fail"


def validate_complete_outline_adherence(
    result: Mapping[str, Any],
    *,
    outline: Mapping[str, Any],
) -> dict[str, Any]:
    """验证 exact-pass 闸门，并仅返回可持久化的元数据投影。"""

    if result.get("verdict") != "pass":
        raise OutlineAdherenceValidationError(
            "正文候选未精确通过章纲符合度"
        )
    issues = result.get("issues")
    if not isinstance(issues, list) or issues:
        raise OutlineAdherenceValidationError("章纲符合度仍包含偏离问题")
    scenes = outline.get("scenes")
    coverage = result.get("scene_coverage")
    if (
        not isinstance(scenes, list)
        or not scenes
        or not isinstance(coverage, list)
        or len(coverage) != len(scenes)
    ):
        raise OutlineAdherenceValidationError("章纲符合度没有覆盖全部场景")

    scene_indexes: list[int] = []
    for item in coverage:
        if not isinstance(item, Mapping):
            raise OutlineAdherenceValidationError("章纲场景覆盖证据格式无效")
        scene_index = item.get("scene_index")
        if type(scene_index) is not int or item.get("status") != "covered":
            raise OutlineAdherenceValidationError("章纲场景尚未全部落实")
        scene_indexes.append(scene_index)
    if (
        len(set(scene_indexes)) != len(scene_indexes)
        or set(scene_indexes) != set(range(1, len(scenes) + 1))
    ):
        raise OutlineAdherenceValidationError(
            "章纲场景覆盖不是完整唯一集合"
        )

    run_id = result.get("source_prose_run_id")
    revision = result.get("source_prose_run_revision")
    digest = result.get("source_content_digest")
    if not isinstance(run_id, str) or not run_id.strip():
        raise OutlineAdherenceValidationError("章纲符合度缺少正文运行身份")
    if type(revision) is not int or revision < 0:
        raise OutlineAdherenceValidationError("章纲符合度正文版本无效")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise OutlineAdherenceValidationError("章纲符合度正文摘要无效")

    return {
        "verdict": "pass",
        "scene_count": len(coverage),
        "issue_count": 0,
        "issue_categories": [],
        "source_prose_run_id": run_id,
        "source_prose_run_revision": revision,
        "source_content_digest": digest,
    }
