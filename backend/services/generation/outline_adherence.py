"""章节正文相对细纲的语义门禁与处理策略。"""

from __future__ import annotations

from typing import Any

PAUSE_FOR_REWRITE = "pause_for_rewrite"
ACCEPT_AND_CONTINUE = "accept_and_continue"
OUTLINE_DEVIATION_POLICIES = frozenset(
    {PAUSE_FOR_REWRITE, ACCEPT_AND_CONTINUE}
)


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
