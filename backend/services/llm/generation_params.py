"""Transport-independent generation parameter request contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


GEN_PARAM_KEYS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
    "system_prompt",
)


class GenerationParamsMixin(BaseModel):
    """Bounded per-request overrides shared by HTTP and runtime callers."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    system_prompt: str | None = Field(default=None)
    allow_failure_retry: bool = Field(
        default=True,
        description="是否允许沿用 Provider 配置进行传输层失败自动重试",
    )


def build_gen_kwargs(request: Any) -> dict[str, Any]:
    """Project only explicit generation overrides from a typed request."""

    return {
        key: value
        for key in GEN_PARAM_KEYS
        if (value := getattr(request, key, None)) is not None
    }


def build_runtime_kwargs(request: Any) -> dict[str, int]:
    """Disable transport retries only when the caller explicitly requests it."""

    if getattr(request, "allow_failure_retry", True):
        return {}
    return {"max_provider_retries": 0}
