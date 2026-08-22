"""Fail-closed handling for protected chapter-generation parameters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ProtectedGenerationParamsError(ValueError):
    """A persisted job contains an override the protected workflows forbid."""


def validate_protected_generation_params(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a copy only when no free protected-prompt override is present."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProtectedGenerationParamsError(
            "批量作业的生成参数无效；请终止该作业并以新 readiness 启动 successor"
        )
    result = dict(value)
    if result.get("system_prompt") is not None:
        raise ProtectedGenerationParamsError(
            "历史作业包含已废止的自由 system_prompt；"
            "请终止该作业并以新 readiness 启动 successor"
        )
    return result
